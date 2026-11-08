"""Read an uploaded talk script, abstract or slide deck as plain text, for glossary suggestions (#1172).

Uploads are untrusted and may be confidential. The bytes and the text only ever live in
memory for one request: nothing here writes a file, logs the content or keeps either.
The file name is used for its extension only, never as a path. PDFs are read for their
text layer alone: there is no OCR, and images, attachments, forms and scripts inside a PDF
are never decoded or run.
"""

from __future__ import annotations

import codecs
import io
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pypdf import PdfReader

from portal.translations.term_extraction import MAX_TEXT_CHARS

logger = logging.getLogger(__name__)

TEXT = "text"
MARKDOWN = "markdown"
PDF = "pdf"
SUPPORTED_EXTENSIONS = {".txt": TEXT, ".md": MARKDOWN, ".markdown": MARKDOWN, ".pdf": PDF}
#: Slide decks run to a few hundred pages at most; later pages are not read.
MAX_PDF_PAGES = 300

_PDF_TYPES = frozenset({"application/pdf", "application/x-pdf"})
#: Browsers send these when they don't know a type (often for .md).
_UNDECLARED_TYPES = frozenset({"", "application/octet-stream"})
_MARKDOWN_TYPES = frozenset({"application/markdown", "application/x-markdown"})
#: Signatures of formats that are not text, whatever the file is called.
_BINARY_SIGNATURES = (
    b"%PDF-",
    b"PK\x03\x04",  # ZIP: .docx, .pptx, .odt, .key
    b"\xd0\xcf\x11\xe0",  # OLE: .doc, .ppt
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF8",
    b"\x7fELF",
    b"\x1f\x8b",
    b"{\\rtf",
)
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
#: Control characters other than tab, newline, form feed and carriage return.
_STRAY_CONTROLS = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")

# Markdown syntax that would otherwise end up as words. Every quantifier is bounded, stays on
# one line and excludes its own opening bracket, so a run of "[" or "<" fails at once instead
# of rescanning the run from every position.
_FENCE = re.compile(r" {0,3}(?:```|~~~)")
_IMAGE_OR_LINK = re.compile(r"!?\[([^\[\]\n]{0,500})\]\([^()\n]{0,2000}\)")
_REFERENCE_LINK = re.compile(r"\[([^\[\]\n]{0,500})\]\[[^\[\]\n]{0,200}\]")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\[\]\n]{1,200}\]:[^\n]*$", re.MULTILINE)
_HTML_TAG = re.compile(r"<[^<>\n]{1,500}>")
_EMPHASIS = re.compile(r"\*+|~~|`+|(?<!\w)_+|_+(?!\w)")


class SourceMaterialError(ValueError):
    """An upload that can't be used, with a message the owner can act on."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SourceText:
    text: str
    kind: str
    #: True when only the beginning was read (page or length limit).
    truncated: bool = False


def source_kind(filename: str, content_type: str | None) -> str:
    """The format to read, from the file name's extension and the type the browser declared."""
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    kind = SUPPORTED_EXTENSIONS.get(suffix)
    if kind is None:
        raise SourceMaterialError(
            "Upload a plain text (.txt), Markdown (.md) or PDF (.pdf) file. Save slides as PDF first.", 415
        )
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared in _UNDECLARED_TYPES:
        return kind
    if kind == PDF:
        consistent = declared in _PDF_TYPES
    else:
        consistent = declared.startswith("text/") or declared in _MARKDOWN_TYPES
    if not consistent:
        raise SourceMaterialError(f"The file's type doesn't match its {suffix} name.", 415)
    return kind


def read_source_material(data: bytes, *, filename: str, content_type: str | None, max_bytes: int) -> SourceText:
    """Validate an upload and return its text. Raises SourceMaterialError for anything unusable."""
    kind = source_kind(filename, content_type)
    if not data:
        raise SourceMaterialError("The file is empty.", 400)
    if len(data) > max_bytes:
        raise SourceMaterialError(f"Files must be {format_size(max_bytes)} or smaller.", 413)
    if kind == PDF:
        text, truncated = _pdf_text(data)
    else:
        text = _plain_text(data)
        if kind == MARKDOWN:
            text = strip_markdown(text)
        truncated = False
    if len(text) > MAX_TEXT_CHARS:
        text, truncated = text[:MAX_TEXT_CHARS], True
    if not text.strip():
        if kind == PDF:
            raise SourceMaterialError(
                "No text found in this PDF. Scanned pages are images, and VoxBento doesn't read text from images.",
                422,
            )
        raise SourceMaterialError("The file has no text in it.", 422)
    return SourceText(text, kind, truncated)


def format_size(size: int) -> str:
    megabytes = size / (1024 * 1024)
    return f"{round(megabytes, 1):g} MB" if megabytes >= 1 else f"{max(1, size // 1024)} KB"


def _plain_text(data: bytes) -> str:
    if data.startswith(_BINARY_SIGNATURES):
        raise SourceMaterialError("This file isn't plain text. Upload a .txt, .md or .pdf file.", 415)
    if data.startswith(_UTF16_BOMS):
        encoding = "utf-16"
    elif b"\x00" in data:
        raise SourceMaterialError("This file isn't plain text. Upload a .txt, .md or .pdf file.", 415)
    else:
        encoding = "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        raise SourceMaterialError("Text files must be saved as UTF-8.", 422) from None
    sample = text[:MAX_TEXT_CHARS]
    if len(_STRAY_CONTROLS.findall(sample)) > len(sample) // 200:
        raise SourceMaterialError("This file isn't plain text. Upload a .txt, .md or .pdf file.", 415)
    return text


def _pdf_text(data: bytes) -> tuple[str, bool]:
    # The PDF header may follow up to 1 KB of leading bytes.
    if b"%PDF-" not in data[:1024]:
        raise SourceMaterialError("This file isn't a PDF, although its name ends in .pdf.", 415)
    parts: list[str] = []
    size = 0
    truncated = False
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        # Many PDFs are encrypted with an empty user password only to restrict editing.
        if reader.is_encrypted and not reader.decrypt(""):
            raise SourceMaterialError("This PDF is password-protected. Upload a copy without a password.", 422)
        for number, page in enumerate(reader.pages):
            if number >= MAX_PDF_PAGES or size >= MAX_TEXT_CHARS:
                truncated = True
                break
            text = page.extract_text() or ""
            parts.append(text)
            size += len(text) + 1
    except SourceMaterialError:
        raise
    except Exception as exc:  # pypdf raises many exception types on damaged or hostile files.
        # The type only: messages can quote the file.
        logger.info("Glossary source PDF could not be read (%s)", type(exc).__name__)
        raise SourceMaterialError(
            "This PDF couldn't be read. It may be damaged; try exporting it again.", 422
        ) from None
    return "\n".join(parts), truncated


def strip_markdown(text: str) -> str:
    """Drop code blocks, link targets, HTML and emphasis markers; keep headings, so they read as headings."""
    lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if not in_code:
            lines.append(line)
    text = _REFERENCE_DEFINITION.sub("", "\n".join(lines))
    text = _REFERENCE_LINK.sub(r"\1", _IMAGE_OR_LINK.sub(r"\1", text))
    return _EMPHASIS.sub("", _HTML_TAG.sub(" ", text))
