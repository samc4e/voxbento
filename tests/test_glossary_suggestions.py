"""Glossary suggestions from an uploaded talk script or slides (#1172)."""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import threading
import time
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import func, select

from fastapi_app import app
from portal.auth import create_user_token
from portal.config import settings
from portal.crypto import encrypt_val
from portal.database import (
    configure,
    create_event,
    create_room,
    dispose,
    get_session,
    init_db,
    set_event_membership,
    set_room_membership,
)
from portal.models import AIVocabularyEntry, Room, RoomTranslationLanguage, User
from portal.roles import EVENT_OWNER, ROOM_COORDINATOR
from portal.routers import glossary_suggestions as router_module
from portal.translations import glossary_suggestions as suggestions_module
from portal.translations import source_material, term_extraction
from portal.translations.glossary_suggestions import (
    AcceptedRow,
    Suggestion,
    TargetLanguage,
    build_suggestions,
    parse_numbered_terms,
    propose_translations,
    translation_service,
    validate_accepted_rows,
)
from portal.translations.source_material import SourceMaterialError, read_source_material, strip_markdown
from portal.translations.term_extraction import (
    ACRONYM,
    MAX_TEXT_CHARS,
    MIXED_CASE,
    NAME,
    RARE,
    TermCandidate,
    extract_candidate_terms,
)
from portal.translations.vocabulary import VocabularyEntryInput, serialize_vocabulary_csv
from portal.translations.worker import PROVIDERS

ORIGIN = {"origin": "http://test"}
TEN_MB = 10 * 1024 * 1024

ABSTRACT = """# Real-Time Interpretation With VoxBento

VoxBento is an open source interpretation platform built by FOSSASIA. Interpreters publish
audio over WHIP to MediaMTX, and listeners play it back with WHEP. In this talk, Linus Torvalds
explains how the NLLB model and Whisper turn the floor audio into captions.

We also show how diarization keeps captions readable, and why diarization matters for panels.
The Open Source Summit audience asked about WebRTC and APIs.
"""


# ── Helpers ────────────────────────────────────────────────────────────────


def _pdf(*pages: list[str]) -> bytes:
    """A minimal, valid PDF whose pages carry the given lines as real text."""

    def escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    page_count = len(pages)
    font_id = 3 + 2 * page_count
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (3 + 2 * index) for index in range(page_count)), page_count),
    ]
    for index, lines in enumerate(pages):
        content = "BT /F1 12 Tf 14 TL 72 720 Td " + " ".join(f"({escape(line)}) Tj T*" for line in lines) + " ET"
        content_bytes = content.encode("latin-1")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (4 + 2 * index, font_id)
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content_bytes), content_bytes))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _encrypted_pdf(user_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf(["Kubernetes and WebRTC at FOSSASIA"]))))
    writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm="AES-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _read(data: bytes, filename: str = "talk.txt", content_type: str | None = "text/plain", max_bytes: int = TEN_MB):
    return read_source_material(data, filename=filename, content_type=content_type, max_bytes=max_bytes)


def _terms(text: str, **kwargs) -> dict[str, TermCandidate]:
    return {candidate.term: candidate for candidate in extract_candidate_terms(text, **kwargs)}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _path(event_id: int, room_id: int, *, prefix: str = "/workspace", save: bool = False) -> str:
    path = f"{prefix}/events/{event_id}/rooms/{room_id}/ai-vocabulary/suggestions"
    return f"{path}/save" if save else path


async def _suggest(
    client: AsyncClient,
    event_id: int,
    room_id: int,
    data: bytes,
    cookies: dict[str, str] | None,
    *,
    filename: str = "talk.txt",
    content_type: str = "text/plain",
    translate: bool = False,
    prefix: str = "/workspace",
    origin: dict[str, str] = ORIGIN,
):
    url = _path(event_id, room_id, prefix=prefix) + ("?translate=1" if translate else "")
    headers = {"content-type": content_type, "x-source-filename": quote(filename), **origin}
    return await client.post(url, content=data, headers=headers, cookies=cookies)


async def _save(client: AsyncClient, event_id: int, room_id: int, rows: list[dict], cookies, *, prefix="/workspace"):
    return await client.post(
        _path(event_id, room_id, prefix=prefix, save=True), json={"rows": rows}, headers=ORIGIN, cookies=cookies
    )


async def _entries(room_id: int) -> list[AIVocabularyEntry]:
    async with get_session() as session:
        return list(
            await session.scalars(
                select(AIVocabularyEntry).where(AIVocabularyEntry.room_id == room_id).order_by(AIVocabularyEntry.id)
            )
        )


async def _entry_count() -> int:
    async with get_session() as session:
        return await session.scalar(select(func.count()).select_from(AIVocabularyEntry))


class FakeProvider:
    """Stands in for an LLM provider; ``answer`` decides each reply from the language and the request text."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    async def translate(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.answer(kwargs["target_lang_code"], kwargs["text"])
        if isinstance(reply, BaseException):
            raise reply
        return await reply if asyncio.iscoroutine(reply) else reply


class TrackedSlots(router_module._ExtractionSlots):
    """Signals each time a worker slot is given back, so tests can wait for it instead of polling."""

    def __init__(self, workers: int) -> None:
        super().__init__(workers)
        self.released = threading.Event()

    def _release(self, _future=None) -> None:
        super()._release(_future)
        self.released.set()


def _numbered_reply(suffix: str):
    def answer(code: str, text: str) -> str:
        return "\n".join(
            f"{line.split('. ', 1)[0]}. {line.split('. ', 1)[1]}{suffix}-{code}" for line in text.splitlines()
        )

    return answer


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def room_setup(setup_db):
    """An event with an OpenAI translation key and a room translating English into German and French."""
    async with get_session() as session:
        event = await create_event(session, slug="glossarycon", display_name="GlossaryCon")
        event.encrypted_translation_openai_api_key = encrypt_val("sk-test-key")
        room = await create_room(session, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "openai"
        room.floor_translation_model = "gpt-4o-mini"
        session.add_all(
            [
                RoomTranslationLanguage(room_id=room.id, language_code="de", language_name="German"),
                RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French"),
            ]
        )
        other_event = await create_event(session, slug="othercon", display_name="OtherCon")
        other_room = await create_room(session, event_id=other_event.id, display_name="Other Hall")

        users = {}
        for name in ("admin", "owner", "coordinator", "other_owner"):
            user = User(email=f"{name}@example.com", display_name=name, email_verified=True, is_admin=name == "admin")
            session.add(user)
            await session.flush()
            users[name] = user
        await set_event_membership(session, user_id=users["owner"].id, event_id=event.id, role=EVENT_OWNER)
        await set_event_membership(session, user_id=users["other_owner"].id, event_id=other_event.id, role=EVENT_OWNER)
        await set_room_membership(session, user_id=users["coordinator"].id, room_id=room.id, role=ROOM_COORDINATOR)
        cookies = {
            name: {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)}
            for name, user in users.items()
        }
    return {"event": event, "room": room, "other_room": other_room, "cookies": cookies}


@pytest.fixture
def fake_openai(monkeypatch):
    provider = FakeProvider(_numbered_reply(""))
    monkeypatch.setitem(PROVIDERS, "openai", provider)
    return provider


# ── Reading files ──────────────────────────────────────────────────────────


def test_plain_text_is_read_as_utf8():
    source = _read("Café VoxBento".encode())
    assert source.text == "Café VoxBento"
    assert source.kind == "text"
    assert not source.truncated


def test_plain_text_with_bom_or_utf16_is_read():
    assert _read("\N{BYTE ORDER MARK}WebRTC".encode("utf-8")).text == "WebRTC"
    assert "WebRTC" in _read("WebRTC talk".encode("utf-16")).text


def test_markdown_drops_syntax_and_code_but_keeps_headings_and_link_text():
    text = _read(
        b"# The NLLB Talk\n\nSee [the WHIP spec](https://example.com/whip) and ![diagram](img.png).\n"
        b"```python\nimport secret_module\n```\n*Bold* and `WebRTC` and __init__ stay readable.\n"
        b"[ref]: https://example.com/hidden\n<span>HTML</span>",
        filename="talk.md",
        content_type="text/markdown",
    ).text
    assert text.startswith("# The NLLB Talk")
    assert "the WHIP spec" in text and "diagram" in text
    assert "example.com" not in text and "img.png" not in text
    assert "secret_module" not in text
    assert "Bold and WebRTC and init stay readable." in text
    assert "<span>" not in text and "HTML" in text


def test_markdown_patterns_stay_fast_on_hostile_input():
    started = time.perf_counter()
    strip_markdown("[" * 200_000 + "<" * 200_000 + "!" * 100_000)
    assert time.perf_counter() - started < 2


def test_pdf_text_is_extracted():
    source = _read(_pdf(["Kubernetes and WebRTC", "Linus Torvalds at FOSSASIA"]), "slides.pdf", "application/pdf")
    assert source.kind == "pdf"
    assert "Kubernetes and WebRTC" in source.text
    assert "Linus Torvalds at FOSSASIA" in source.text


def test_pdf_reading_stops_at_the_page_limit(monkeypatch):
    monkeypatch.setattr(source_material, "MAX_PDF_PAGES", 1)
    source = _read(_pdf(["First page NLLB"], ["Second page WHEP"]), "slides.pdf", "application/pdf")
    assert "NLLB" in source.text and "WHEP" not in source.text
    assert source.truncated


def test_pdf_with_only_an_owner_password_is_read_but_a_locked_one_is_refused():
    assert "WebRTC" in _read(_encrypted_pdf(""), "slides.pdf", "application/pdf").text
    with pytest.raises(SourceMaterialError, match="password-protected"):
        _read(_encrypted_pdf("secret"), "slides.pdf", "application/pdf")


@pytest.mark.parametrize("filename", ["slides.pptx", "talk.docx", "photo.png", "notes", "script.txt.exe", "talk.csv"])
def test_unsupported_file_types_are_refused(filename):
    with pytest.raises(SourceMaterialError) as error:
        _read(b"Kubernetes", filename, None)
    assert error.value.status_code == 415
    assert ".txt" in error.value.message and ".pdf" in error.value.message


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("talk.pdf", "text/plain"), ("talk.txt", "application/pdf"), ("talk.md", "image/png"), ("talk.txt", "x/y")],
)
def test_declared_type_must_match_the_extension(filename, content_type):
    with pytest.raises(SourceMaterialError, match="doesn't match") as error:
        _read(b"%PDF-1.4 Kubernetes", filename, content_type)
    assert error.value.status_code == 415


@pytest.mark.parametrize(
    "content_type", [None, "", "application/octet-stream", "text/x-markdown", "text/plain; charset=utf-8"]
)
def test_undeclared_or_generic_types_fall_back_to_content_checks(content_type):
    assert _read(b"WebRTC", "talk.md", content_type).text == "WebRTC"


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (b"\x89PNG\r\n\x1a\nbinary", "photo.txt"),
        (b"PK\x03\x04 zipped slides", "slides.md"),
        (b"%PDF-1.4 renamed", "talk.txt"),
        (b"text\x00with\x00nul", "talk.txt"),
        (b"Kubernetes is a word", "slides.pdf"),
        (bytes(range(1, 32)) * 20, "talk.txt"),
    ],
)
def test_content_must_match_the_extension(data, filename):
    with pytest.raises(SourceMaterialError) as error:
        _read(data, filename, None)
    assert error.value.status_code == 415


def test_empty_files_are_refused():
    with pytest.raises(SourceMaterialError, match="empty") as error:
        _read(b"")
    assert error.value.status_code == 400
    with pytest.raises(SourceMaterialError, match="no text"):
        _read(b"   \n\t  ")


def test_oversized_files_are_refused():
    with pytest.raises(SourceMaterialError, match="1 KB or smaller") as error:
        _read(b"x" * 1025, max_bytes=1024)
    assert error.value.status_code == 413


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.4\nthis is not a real pdf", b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 9 0 R >> endobj\n%%EOF"],
)
def test_malformed_pdfs_are_refused_without_logging_their_content(data, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SourceMaterialError, match="couldn't be read") as error:
        _read(data, "slides.pdf", "application/pdf")
    assert error.value.status_code == 422
    assert "not a real pdf" not in caplog.text


def test_pdf_without_a_text_layer_explains_that_ocr_is_not_done():
    with pytest.raises(SourceMaterialError, match="Scanned pages"):
        _read(_pdf([]), "scan.pdf", "application/pdf")


def test_invalid_utf8_is_refused():
    with pytest.raises(SourceMaterialError, match="UTF-8"):
        _read("Café".encode("latin-1"))


def test_file_name_is_used_only_for_its_extension():
    source = _read(b"WHIP and WHEP", "../../../../etc/cron.d/evil.md", None)
    assert source.kind == "markdown"
    assert "WHIP" in source.text


def test_long_text_is_cut_and_reported():
    source = _read(b"WebRTC " * (MAX_TEXT_CHARS // 4))
    assert len(source.text) == MAX_TEXT_CHARS
    assert source.truncated


# ── Finding terms ──────────────────────────────────────────────────────────


def test_sample_abstract_surfaces_names_products_and_acronyms():
    terms = _terms(ABSTRACT)
    expected = {
        **dict.fromkeys(("FOSSASIA", "WHIP", "WHEP", "NLLB", "API"), ACRONYM),
        **dict.fromkeys(("VoxBento", "MediaMTX", "WebRTC"), MIXED_CASE),
        **dict.fromkeys(("Linus Torvalds", "Whisper", "Open Source Summit"), NAME),
        "diarization": RARE,
    }
    assert {term: terms[term].kind for term in expected if term in terms} == expected
    # Ordinary words, headings and stop words are not suggested.
    ordinary = {"Real-Time Interpretation", "Interpretation", "The", "We", "audio", "captions", "talk", "platform"}
    assert not ordinary & set(terms)


def test_acronyms_and_their_plurals_count_as_one_term():
    terms = _terms("The API is new. Two APIs were added. GPU and CUDA power NLLB. Q&A at the end. OK then.")
    assert terms["API"].occurrences == 2
    assert {"API", "GPU", "CUDA", "NLLB"} <= set(terms)
    assert "APIs" not in terms and "Q&A" not in terms and "OK" not in terms


def test_acronyms_are_counted_in_their_own_case():
    terms = _terms("The US market grew. Tell us more. The US team agreed.")
    assert terms["US"].occurrences == 2


def test_shouted_lines_do_not_turn_ordinary_words_into_acronyms():
    terms = _terms("INTRODUCTION TO WEBRTC\nAGENDA\nWe use the WHO data.")
    assert "INTRODUCTION" not in terms and "AGENDA" not in terms and "TO" not in terms
    assert "WEBRTC" in terms and "WHO" in terms


def test_capitalised_phrases_need_more_than_a_sentence_start():
    terms = _terms(
        "We met Linus Torvalds and the team from Bank of America. Today Google joined. "
        "Mission Control lets you pick a booth. Next we open Mission Control again, as coordinators use Mission Control."
    )
    assert terms["Linus Torvalds"].kind == NAME
    assert "Bank of America" in terms
    assert "Google" in terms and "Today" not in terms and "Today Google" not in terms
    assert "Mission Control" in terms and "Control" not in terms and "Mission" not in terms


def test_title_case_slide_headings_only_keep_unusual_words():
    terms = _terms("Getting Started With Kubernetes\nNext Steps\nThank You\nLinus Torvalds\n")
    assert "Kubernetes" in terms and "Linus Torvalds" in terms
    assert not {"Getting Started", "Next Steps", "Steps", "Thank You", "You"} & set(terms)


def test_camel_case_terms_are_found():
    terms = _terms("Write it in JavaScript or TypeScript, run it on an iPhone with macOS, and ship via eBay.")
    expected = dict.fromkeys(("JavaScript", "TypeScript", "iPhone", "macOS", "eBay"), MIXED_CASE)
    assert {term: terms[term].kind for term in expected if term in terms} == expected
    # Hyphenated title case is not mixed case.
    assert "Real-Time" not in _terms("We built Real-Time captions.")


def test_rare_words_must_repeat_and_be_uncommon():
    text = "Diarization and transcoding. We tuned diarization. Latency matters, latency hurts. Transcoding once."
    terms = _terms(text)
    assert terms["diarization"].occurrences == 2
    assert terms["latency"].kind == RARE
    assert "matters" not in terms and "tuned" not in terms


def test_repeated_terms_are_counted_and_rank_first():
    text = "NLLB once. " + "WebRTC is here. " * 5 + "Kubernetes twice. Kubernetes again."
    candidates = extract_candidate_terms(text)
    terms = {candidate.term: candidate for candidate in candidates}
    assert terms["WebRTC"].occurrences == 5
    assert terms["Kubernetes"].occurrences == 2
    assert candidates[0].term == "WebRTC"


def test_duplicates_are_merged_into_one_candidate():
    text = "Python is fun. We like Python. The python docs. Google's tools and Google's cloud."
    candidates = extract_candidate_terms(text)
    names = [candidate.term for candidate in candidates]
    assert names.count("Python") == 1 and "python" not in names
    assert {c.term: c for c in candidates}["Python"].occurrences == 3
    assert names.count("Google") == 1 and "Google's" not in names


def test_unicode_text_is_handled():
    # Decomposed and composed accents are the same term; ligatures from PDFs are resolved.
    terms = _terms(
        "We visited Zu\N{COMBINING DIAERESIS}rich and Zürich. The \N{LATIN SMALL LIGATURE FI}rst Ŝtar of 東京 uses Yandex.Cloud and Яндекс."
    )
    assert terms["Zürich"].occurrences == 2
    assert "Yandex.Cloud" in terms and "Яндекс" in terms
    assert "\N{LATIN SMALL LIGATURE FI}rst" not in "".join(terms)
    russian = _terms("Привет, Москва! Мы используем API и Яндекс. Компания Яндекс большая.", source_language="ru")
    assert {"Москва", "Яндекс", "API"} <= set(russian)
    assert "Привет" not in russian and "Мы" not in russian


def test_urls_emails_numbers_and_short_noise_are_ignored():
    terms = _terms(
        "Mail talks@fossasia.org or visit https://VoxBento.example/Docs. Version 2024 costs 10ms at 3x speed."
    )
    assert not {"fossasia", "VoxBento", "Docs", "2024", "10ms", "3x"} & set(terms)


def test_nouns_in_german_are_not_mistaken_for_names():
    text = "Die Software nutzt KI. Wir zeigen die Übersetzung mit WebRTC. Die Deutsche Bahn fährt.\n" * 3
    terms = _terms(text, source_language="de")
    assert {"KI", "WebRTC", "Deutsche Bahn"} <= set(terms)
    assert "Software" not in terms and "Übersetzung" not in terms


def test_rare_word_pass_is_skipped_for_non_english_text():
    french = extract_candidate_terms("le chat mange le poisson " * 5, source_language="fr")
    assert all(c.kind != RARE for c in french)
    # A German script in a room set to English is still recognised as not English.
    german = "die katze schläft und die katze frisst gerne fisch mit freude " * 5
    assert all(c.kind != RARE for c in extract_candidate_terms(german, source_language="en"))


def test_extraction_is_deterministic_and_bounded():
    text = " ".join(f"Term{index}X and Word{index}Y" for index in range(3000))
    first = extract_candidate_terms(text)
    assert first == extract_candidate_terms(text)
    assert len(first) <= 500
    assert len(extract_candidate_terms(text, limit=10)) == 10


def test_large_and_repetitive_input_stays_fast():
    started = time.perf_counter()
    candidates = extract_candidate_terms("Kubernetes and WebRTC power VoxBento. " * 50_000)
    assert time.perf_counter() - started < 10
    assert [c.term for c in candidates][:3] == ["WebRTC", "VoxBento", "Kubernetes"]
    hostile = "a" * 400_000 + "@" + "[" * 100_000
    started = time.perf_counter()
    extract_candidate_terms(hostile)
    assert time.perf_counter() - started < 5


def test_extraction_needs_no_api_key_or_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("term extraction must not use the network")

    monkeypatch.setattr("portal.globals.get_http_client", no_network)
    assert "NLLB" in _terms("We translate with NLLB.")


def test_extraction_keeps_no_words_from_the_upload_once_it_returns():
    """Regression (review): a process-wide lru_cache used to keep every word of every upload."""
    secret = "zyxwquorbleton"
    candidates = extract_candidate_terms(f"The {secret.title()} kit. We tuned {secret} twice, then {secret} again.")
    assert secret in {candidate.term.casefold() for candidate in candidates}
    assert not hasattr(term_extraction.is_common_word, "cache_info")
    leaks = [
        f"{module.__name__}.{name}"
        for module in (term_extraction, source_material, suggestions_module, router_module)
        for name, value in vars(module).items()
        if isinstance(value, (dict, set, frozenset, list, tuple)) and secret in str(value).casefold()
    ]
    assert not leaks, f"module state kept upload text: {leaks}"


# ── Review rows ────────────────────────────────────────────────────────────

LANGUAGES = [TargetLanguage("de", "German"), TargetLanguage("fr", "French")]


def _existing(term: str, language: str, case_sensitive: bool = False) -> AIVocabularyEntry:
    return AIVocabularyEntry(
        source_term=term, target_language=language, target_term=term, case_sensitive=case_sensitive
    )


def test_review_rows_skip_terms_already_in_the_glossary():
    candidates = [
        TermCandidate("WebRTC", MIXED_CASE, 3),
        TermCandidate("NLLB", ACRONYM, 2),
        TermCandidate("Whisper", NAME, 1),
        TermCandidate("FOSSASIA", ACRONYM, 1),
    ]
    existing = [
        _existing("webrtc", "de"),
        _existing("webrtc", "fr"),
        _existing("NLLB", "de"),
        _existing("whisper", "all"),
    ]
    rows = build_suggestions(candidates, LANGUAGES, existing)
    assert [row.source_term for row in rows] == ["NLLB", "FOSSASIA"]
    assert rows[0].in_glossary == ["de"]
    assert rows[1].in_glossary == []


def test_review_rows_suggest_safe_match_settings_and_respect_the_limit():
    candidates = [TermCandidate("US", ACRONYM, 2), TermCandidate("Go", NAME, 1), TermCandidate("Kubernetes", NAME, 1)]
    rows = build_suggestions(candidates, LANGUAGES, [])
    settings_by_term = {row.source_term: (row.match_type, row.case_sensitive) for row in rows}
    assert settings_by_term == {"US": ("exact", True), "Go": ("exact", False), "Kubernetes": ("phrase", False)}
    many = [TermCandidate(f"Term{index}X", MIXED_CASE, 1) for index in range(100)]
    assert len(build_suggestions(many, LANGUAGES, [])) == 60


# ── Optional translations ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("1. Erste\n2. Zweite", ["Erste", "Zweite"]),
        ("Here you go:\n2) Zweite\n1) **Erste**", ["Erste", "Zweite"]),
        ("1. NLLB -> NLLB\n2. Pull Request → Pull-Request", ["NLLB", "Pull-Request"]),
        ("Erste\nZweite", ["Erste", "Zweite"]),
        ("Sorry, I can't help with that.", [None, None]),
        ("1. Erste\n7. Out of range", ["Erste", None]),
        ("1. " + "x" * 500 + "\n2. Zweite", [None, "Zweite"]),
    ],
)
def test_numbered_replies_are_parsed_defensively(reply, expected):
    assert parse_numbered_terms(reply, 2) == expected


@pytest.mark.anyio
async def test_translation_service_uses_the_rooms_llm_or_the_events_first_key(room_setup):
    event, room = room_setup["event"], room_setup["room"]
    service = translation_service(event, room)
    assert (service.provider, service.model) == ("openai", "gpt-4o-mini")
    room.floor_translation_provider = "local"
    assert translation_service(event, room).provider == "openai"
    event.encrypted_translation_openai_api_key = None
    assert translation_service(event, room) is None


def _rows(*terms: str, in_glossary: list[str] | None = None) -> list[Suggestion]:
    return [Suggestion(term, NAME, 1, "phrase", False, list(in_glossary or [])) for term in terms]


@pytest.mark.anyio
async def test_translations_are_proposed_per_language(room_setup, fake_openai):
    event, room = room_setup["event"], room_setup["room"]
    rows = _rows("Kubernetes", "Mission Control", in_glossary=None)
    rows[1].in_glossary = ["fr"]
    report = await propose_translations(
        rows, LANGUAGES, event=event, room=room, service=translation_service(event, room)
    )
    assert report.status == "complete"
    assert rows[0].translations == {"de": "Kubernetes-de", "fr": "Kubernetes-fr"}
    # Languages the glossary already covers are neither asked for nor filled.
    assert rows[1].translations == {"de": "Mission Control-de"}
    requests = {call["target_lang_code"]: call for call in fake_openai.calls}
    assert requests["de"]["text"] == "1. Kubernetes\n2. Mission Control"
    assert requests["fr"]["text"] == "1. Kubernetes"
    assert requests["de"]["api_key"] == "sk-test-key" and requests["de"]["model"] == "gpt-4o-mini"
    assert requests["de"]["source_lang_name"] == "English"


@pytest.mark.anyio
async def test_a_language_the_glossary_covers_for_every_term_is_not_requested(room_setup, fake_openai):
    """Regression (review): it used to be requested anyway and then reported as failed."""
    event, room = room_setup["event"], room_setup["room"]
    rows = _rows("Kubernetes", "Mission Control", in_glossary=["fr"])
    report = await propose_translations(
        rows, LANGUAGES, event=event, room=room, service=translation_service(event, room)
    )
    assert report.status == "complete" and report.failed_languages == ()
    assert [call["target_lang_code"] for call in fake_openai.calls] == ["de"]
    covered_everywhere = _rows("Kubernetes", in_glossary=["de", "fr"])
    report = await propose_translations(
        covered_everywhere, LANGUAGES, event=event, room=room, service=translation_service(event, room)
    )
    assert report.status == "skipped"
    assert len(fake_openai.calls) == 1


@pytest.mark.anyio
async def test_large_term_lists_are_sent_in_batches(room_setup, fake_openai):
    event, room = room_setup["event"], room_setup["room"]
    rows = _rows(*(f"Term{index}X" for index in range(45)))
    report = await propose_translations(
        rows, LANGUAGES[:1], event=event, room=room, service=translation_service(event, room)
    )
    assert report.status == "complete"
    assert len(fake_openai.calls) == 3
    assert all(row.translations["de"] == f"{row.source_term}-de" for row in rows)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "status", "failed"),
    [
        (lambda code, text: None, "failed", ["de", "fr"]),
        (lambda code, text: RuntimeError("provider down"), "failed", ["de", "fr"]),
        (lambda code, text: "I cannot translate that.", "failed", ["de", "fr"]),
        (lambda code, text: _numbered_reply("")(code, text) if code == "de" else None, "partial", ["fr"]),
    ],
)
async def test_translation_failures_leave_cells_empty(room_setup, monkeypatch, answer, status, failed):
    event, room = room_setup["event"], room_setup["room"]
    monkeypatch.setitem(PROVIDERS, "openai", FakeProvider(answer))
    rows = _rows("Kubernetes")
    report = await propose_translations(
        rows, LANGUAGES, event=event, room=room, service=translation_service(event, room)
    )
    assert report.status == status
    assert list(report.failed_languages) == failed
    assert set(rows[0].translations) == {"de", "fr"} - set(failed)


@pytest.mark.anyio
async def test_slow_providers_are_cut_off(room_setup, monkeypatch):
    event, room = room_setup["event"], room_setup["room"]

    async def slow():
        await asyncio.sleep(30)

    monkeypatch.setitem(PROVIDERS, "openai", FakeProvider(lambda code, text: slow()))
    monkeypatch.setattr(suggestions_module, "TRANSLATION_BUDGET_SECS", 0.05)
    started = time.perf_counter()
    report = await propose_translations(
        _rows("Kubernetes"), LANGUAGES, event=event, room=room, service=translation_service(event, room)
    )
    assert time.perf_counter() - started < 5
    assert report.status == "failed"


@pytest.mark.anyio
async def test_unreadable_key_fails_softly(room_setup, fake_openai, monkeypatch):
    event, room = room_setup["event"], room_setup["room"]

    def broken_key(*args):
        raise ValueError("bad key")

    monkeypatch.setattr(suggestions_module, "get_translation_api_key", broken_key)
    report = await propose_translations(
        _rows("Kubernetes"), LANGUAGES, event=event, room=room, service=translation_service(event, room)
    )
    assert report.status == "failed"
    assert not fake_openai.calls


@pytest.mark.anyio
async def test_nothing_to_translate_is_skipped(room_setup, fake_openai):
    event, room = room_setup["event"], room_setup["room"]
    service = translation_service(event, room)
    assert (await propose_translations([], LANGUAGES, event=event, room=room, service=service)).status == "skipped"
    only_all = [TargetLanguage("all", "All languages")]
    assert (
        await propose_translations(_rows("X"), only_all, event=event, room=room, service=service)
    ).status == "skipped"
    assert not fake_openai.calls


# ── Accepting rows ─────────────────────────────────────────────────────────


def _accepted(row: int, term: str, translations: dict[str, str], **kwargs) -> AcceptedRow:
    return AcceptedRow(row=row, source_term=term, translations=translations, **kwargs)


def test_accepted_rows_go_through_validate_vocabulary_entry(monkeypatch):
    calls = []
    real = suggestions_module.validate_vocabulary_entry

    def spy(row, row_number):
        calls.append((dict(row), row_number))
        return real(row, row_number)

    monkeypatch.setattr(suggestions_module, "validate_vocabulary_entry", spy)
    entries, errors = validate_accepted_rows(
        [_accepted(3, " NLLB ", {"de": " NLLB ", "fr": ""}, match_type="exact", case_sensitive=True)], []
    )
    assert not errors
    assert calls == [
        (
            {
                "source_term": " NLLB ",
                "target_language": "de",
                "target_term": " NLLB ",
                "match_type": "exact",
                "case_sensitive": "true",
            },
            3,
        )
    ]
    assert entries == [VocabularyEntryInput("NLLB", "de", "NLLB", None, True, "exact", 0)]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_accepted(1, "=HYPERLINK()", {"de": "x"}), "Row 1: source_term starts with a spreadsheet formula character"),
        (_accepted(2, "WebRTC", {"de": "@cmd"}), "Row 2: target_term starts with a spreadsheet formula character"),
        (_accepted(3, "x" * 121, {"de": "x"}), "Row 3: source_term is longer than 120 characters"),
        (_accepted(4, "", {"de": "x"}), "Row 4: source_term is required"),
        (_accepted(5, "WebRTC", {"xx": "x"}), "Row 5: unsupported target_language 'xx'"),
        (_accepted(6, "WebRTC", {"de": "x"}, match_type="regex"), "Row 6: unsupported match_type 'regex'"),
        (_accepted(7, "WebRTC", {"de": " ", "fr": ""}), "Row 7: add a translation for at least one language."),
    ],
)
def test_invalid_accepted_rows_are_rejected_with_the_glossarys_messages(row, message):
    entries, errors = validate_accepted_rows([row], [])
    assert not entries
    assert [error.message for error in errors] == [message]


def test_overlap_rules_apply_to_existing_entries_and_between_rows():
    existing = [_existing("WebRTC", "de"), _existing("us", "de", case_sensitive=True)]
    entries, errors = validate_accepted_rows(
        [
            _accepted(1, "webrtc", {"de": "WebRTC", "fr": "WebRTC"}),
            _accepted(2, "US", {"de": "USA"}, match_type="exact", case_sensitive=True),
            _accepted(3, "Kubernetes", {"de": "Kubernetes"}),
            _accepted(4, "KUBERNETES", {"de": "K8s"}),
        ],
        existing,
    )
    assert [(entry.source_term, entry.target_language) for entry in entries] == [
        ("webrtc", "fr"),
        ("US", "de"),
        ("Kubernetes", "de"),
    ]
    assert [(error.row, error.language, error.message) for error in errors] == [
        (1, "de", "Row 1: 'webrtc' is already in the glossary for target language 'de'"),
        (4, "de", "Row 4: duplicate term 'KUBERNETES' for target language 'de'"),
    ]


# ── Endpoints ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_upload_returns_suggestions_and_saves_nothing(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with _client() as client:
        response = await _suggest(client, event.id, room.id, ABSTRACT.encode(), cookies["owner"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["languages"] == [{"code": "de", "name": "German"}, {"code": "fr", "name": "French"}]
    terms = {row["source_term"]: row for row in data["suggestions"]}
    assert {"VoxBento", "FOSSASIA", "NLLB", "WHIP", "Linus Torvalds"} <= set(terms)
    assert terms["NLLB"]["match_type"] == "exact" and terms["NLLB"]["case_sensitive"] is True
    assert all(row["translations"] == {} for row in data["suggestions"])
    assert data["translation"] == {"status": "off", "service": "OpenAI", "failed_languages": []}
    assert data["truncated"] is False
    assert await _entry_count() == 0


@pytest.mark.anyio
async def test_markdown_and_pdf_uploads_work_end_to_end(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with _client() as client:
        markdown = await _suggest(
            client,
            event.id,
            room.id,
            b"# Talk\n\nWe use **NLLB** and `WebRTC`.",
            cookies["owner"],
            filename="talk.md",
            content_type="",
        )
        pdf = await _suggest(
            client,
            event.id,
            room.id,
            _pdf(["Kubernetes and MediaMTX by FOSSASIA"]),
            cookies["owner"],
            filename="slides.pdf",
            content_type="application/pdf",
        )
    assert {"NLLB", "WebRTC"} <= {row["source_term"] for row in markdown.json()["suggestions"]}
    assert {"MediaMTX", "FOSSASIA"} <= {row["source_term"] for row in pdf.json()["suggestions"]}


@pytest.mark.anyio
async def test_upload_skips_terms_already_in_the_room_glossary(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with get_session() as session:
        session.add_all(
            [
                AIVocabularyEntry(
                    event_id=event.id, room_id=room.id, source_term="NLLB", target_language="all", target_term="NLLB"
                ),
                AIVocabularyEntry(
                    event_id=event.id, room_id=room.id, source_term="whip", target_language="de", target_term="WHIP"
                ),
            ]
        )
    async with _client() as client:
        response = await _suggest(client, event.id, room.id, ABSTRACT.encode(), cookies["owner"])
    terms = {row["source_term"]: row for row in response.json()["suggestions"]}
    assert "NLLB" not in terms
    assert terms["WHIP"]["in_glossary"] == ["de"]


@pytest.mark.anyio
async def test_upload_proposes_translations_when_asked(room_setup, fake_openai):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with _client() as client:
        response = await _suggest(
            client, event.id, room.id, b"We use NLLB and WebRTC.", cookies["owner"], translate=True
        )
    data = response.json()
    assert data["translation"]["status"] == "complete"
    terms = {row["source_term"]: row for row in data["suggestions"]}
    assert terms["NLLB"]["translations"] == {"de": "NLLB-de", "fr": "NLLB-fr"}
    # Only the terms reach the provider, never the uploaded text.
    assert all("We use" not in call["text"] for call in fake_openai.calls)
    assert await _entry_count() == 0


@pytest.mark.anyio
async def test_provider_failure_still_returns_the_extracted_terms(room_setup, monkeypatch):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    monkeypatch.setitem(PROVIDERS, "openai", FakeProvider(lambda code, text: RuntimeError("timeout")))
    async with _client() as client:
        response = await _suggest(
            client, event.id, room.id, b"We use NLLB and WebRTC.", cookies["owner"], translate=True
        )
    assert response.status_code == 200
    data = response.json()
    assert data["translation"]["status"] == "failed"
    assert {"NLLB", "WebRTC"} <= {row["source_term"] for row in data["suggestions"]}


@pytest.mark.anyio
async def test_upload_works_without_any_api_key(room_setup, monkeypatch):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with get_session() as session:
        stored = await session.get(type(event), event.id)
        stored.encrypted_translation_openai_api_key = None
    monkeypatch.setitem(PROVIDERS, "openai", FakeProvider(lambda code, text: AssertionError("no key, no call")))
    async with _client() as client:
        response = await _suggest(
            client, event.id, room.id, b"We use NLLB and WebRTC.", cookies["owner"], translate=True
        )
    data = response.json()
    assert data["translation"] == {"status": "unavailable", "service": None, "failed_languages": []}
    assert {"NLLB", "WebRTC"} <= {row["source_term"] for row in data["suggestions"]}


@pytest.mark.anyio
async def test_room_without_ai_languages_offers_all_languages(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with get_session() as session:
        for language in await session.scalars(select(RoomTranslationLanguage)):
            language.enabled = False
    async with _client() as client:
        response = await _suggest(client, event.id, room.id, b"We use NLLB.", cookies["owner"])
    assert response.json()["languages"] == [{"code": "all", "name": "All languages"}]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("data", "filename", "content_type", "status", "message"),
    [
        (b"WebRTC", "slides.pptx", "application/vnd.ms-powerpoint", 415, "Save slides as PDF"),
        (b"WebRTC", "talk.txt", "image/png", 415, "doesn't match"),
        (b"", "talk.txt", "text/plain", 400, "empty"),
        (b"%PDF-1.4\ngarbage", "slides.pdf", "application/pdf", 422, "couldn't be read"),
        (b"\xff\xfe\xfa", "talk.txt", "text/plain", 422, "UTF-8"),
    ],
)
async def test_bad_uploads_get_clear_messages(room_setup, data, filename, content_type, status, message):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with _client() as client:
        response = await _suggest(
            client, event.id, room.id, data, cookies["owner"], filename=filename, content_type=content_type
        )
    assert response.status_code == status
    assert message in response.json()["detail"]


@pytest.mark.anyio
async def test_oversized_uploads_are_refused_before_reading(room_setup, monkeypatch):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    monkeypatch.setattr(settings, "glossary_source_max_bytes", 2048)

    def must_not_parse(*args, **kwargs):
        raise AssertionError("an oversized upload must not be parsed")

    monkeypatch.setattr(router_module, "read_source_material", must_not_parse)
    async with _client() as client:
        declared = await _suggest(client, event.id, room.id, b"x" * 4096, cookies["owner"])

        async def chunks():
            for _ in range(4):
                yield b"x" * 1024

        streamed = await client.post(
            _path(event.id, room.id),
            content=chunks(),
            headers={"content-type": "text/plain", "x-source-filename": "talk.txt", **ORIGIN},
            cookies=cookies["owner"],
        )
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json()["detail"] == "Files must be 2 KB or smaller."


@pytest.mark.anyio
async def test_slow_files_time_out_and_free_their_worker(room_setup, monkeypatch):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    release = threading.Event()

    def stuck(*args):
        release.wait(5)
        return [], False

    slots = TrackedSlots(workers=1)
    monkeypatch.setattr(router_module, "_read_terms", stuck)
    monkeypatch.setattr(router_module, "EXTRACTION_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(router_module, "_extraction", slots)
    async with _client() as client:
        first = await _suggest(client, event.id, room.id, b"NLLB", cookies["owner"])
        # The stuck read still holds the only worker, so another upload is refused, not queued.
        busy = await _suggest(client, event.id, room.id, b"NLLB", cookies["owner"])
        release.set()
        assert await asyncio.to_thread(slots.released.wait, 5), "the worker is freed once the read ends"
        monkeypatch.setattr(router_module, "EXTRACTION_TIMEOUT_SECS", 5)
        after = await _suggest(client, event.id, room.id, b"NLLB", cookies["owner"])
    assert first.status_code == 422 and "too long" in first.json()["detail"]
    assert busy.status_code == 429
    assert after.status_code == 200


@pytest.mark.anyio
async def test_uploads_never_touch_temporary_files_or_logs(room_setup, monkeypatch, tmp_path, caplog):
    """The upload is read in memory and discarded, on success and on failure alike."""
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def no_temp_files(*args, **kwargs):
        raise AssertionError("uploads must not be written to temporary files")

    for name in ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, no_temp_files)
    secret = "ConfidentialKeynoteXyzzy"
    uploads = [
        (f"{secret} launches QuantumWidget.".encode(), "talk.txt", "text/plain", 200),
        (_pdf([f"{secret} slides"]), "deck.pdf", "application/pdf", 200),
        (f"%PDF-1.4 {secret} broken".encode(), "deck.pdf", "application/pdf", 422),
        (f"{secret}\x00binary".encode(), "notes.txt", "text/plain", 415),
    ]
    async with _client() as client:
        for data, filename, content_type, status in uploads:
            response = await _suggest(
                client, event.id, room.id, data, cookies["owner"], filename=filename, content_type=content_type
            )
            assert response.status_code == status
    assert not list(tmp_path.iterdir())
    assert secret not in caplog.text
    assert await _entry_count() == 0


@pytest.mark.anyio
async def test_only_event_owners_and_admins_can_use_suggestions(room_setup):
    event, room, other_room, cookies = (room_setup[key] for key in ("event", "room", "other_room", "cookies"))
    row = [{"row": 1, "source_term": "NLLB", "translations": {"de": "NLLB"}}]
    async with _client() as client:
        for who in (None, "coordinator", "other_owner"):
            upload = await _suggest(client, event.id, room.id, b"NLLB", cookies.get(who) if who else None)
            save = await _save(client, event.id, room.id, row, cookies.get(who) if who else None)
            assert upload.status_code in (401, 403), who
            assert save.status_code in (401, 403), who
        # A room from another event is not reachable through this event, even for its owner.
        assert (await _suggest(client, event.id, other_room.id, b"NLLB", cookies["owner"])).status_code == 404
        assert (await _save(client, event.id, other_room.id, row, cookies["owner"])).status_code == 404
        # Cross-site requests are refused.
        foreign = {"origin": "https://evil.example"}
        assert (await _suggest(client, event.id, room.id, b"NLLB", cookies["owner"], origin=foreign)).status_code == 403
        forged = await client.post(
            _path(event.id, room.id, save=True), json={"rows": row}, headers=foreign, cookies=cookies["owner"]
        )
        assert forged.status_code == 403
        for who in ("owner", "admin"):
            assert (await _suggest(client, event.id, room.id, b"NLLB", cookies[who])).status_code == 200
        assert await _entry_count() == 0
        # Admins can also use the /admin URLs; owners are sent to /workspace like on every management page.
        assert (
            await _suggest(client, event.id, room.id, b"NLLB", cookies["admin"], prefix="/admin")
        ).status_code == 200
        admin_save = await _save(client, event.id, room.id, row, cookies["admin"], prefix="/admin")
    assert admin_save.status_code == 200
    assert admin_save.json()["redirect"].startswith(f"/admin/events/{event.id}/rooms/{room.id}/?")
    assert await _entry_count() == 1


@pytest.mark.anyio
async def test_save_adds_only_accepted_rows_as_normal_glossary_entries(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    rows = [
        {
            "row": 1,
            "source_term": "NLLB",
            "translations": {"de": "NLLB", "fr": ""},
            "match_type": "exact",
            "case_sensitive": True,
        },
        {
            "row": 4,
            "source_term": "Mission Control",
            "translations": {"de": "Missionskontrolle", "fr": "Contrôle de mission"},
        },
    ]
    async with _client() as client:
        response = await _save(client, event.id, room.id, rows, cookies["owner"])
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["saved"] == 3 and data["languages"] == ["de", "fr"]
        assert (
            data["redirect"]
            == f"/workspace/events/{event.id}/rooms/{room.id}/?vocab_imported=3&vocab_languages=de%2C+fr"
        )

        stored = await _entries(room.id)
        assert [(e.source_term, e.target_language, e.target_term, e.match_type, e.case_sensitive) for e in stored] == [
            ("NLLB", "de", "NLLB", "exact", True),
            ("Mission Control", "de", "Missionskontrolle", "phrase", False),
            ("Mission Control", "fr", "Contrôle de mission", "phrase", False),
        ]
        assert all(e.event_id == event.id and e.booth_id is None and e.priority == 0 for e in stored)

        # Accepted rows show up in the normal glossary list and the CSV export.
        page = await client.get(data["redirect"], cookies=cookies["owner"])
        assert "Added 3 terms" in page.text and "Missionskontrolle" in page.text
        export = await client.get(
            f"/workspace/events/{event.id}/rooms/{room.id}/ai-vocabulary/export", cookies=cookies["owner"]
        )
    assert export.status_code == 200
    # Same rows and format as the existing export, which lists by priority, then term.
    assert export.text == serialize_vocabulary_csv(sorted(stored, key=lambda e: (-e.priority, e.source_term)))
    assert "NLLB,de,NLLB,,true,exact,0" in export.text
    assert "Mission Control,fr,Contrôle de mission,,false,phrase,0" in export.text


@pytest.mark.anyio
async def test_a_save_with_any_invalid_row_saves_nothing_and_names_the_rows(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with get_session() as session:
        session.add(
            AIVocabularyEntry(
                event_id=event.id, room_id=room.id, source_term="WebRTC", target_language="de", target_term="WebRTC"
            )
        )
    rows = [
        {"row": 1, "source_term": "NLLB", "translations": {"de": "NLLB"}},
        {"row": 2, "source_term": "webrtc", "translations": {"de": "WebRTC"}},
        {"row": 3, "source_term": "+SUM(A1)", "translations": {"de": "x"}},
        {"row": 5, "source_term": "Rejected later", "translations": {"de": ""}},
    ]
    async with _client() as client:
        response = await _save(client, event.id, room.id, rows, cookies["owner"])
    assert response.status_code == 422
    body = response.json()
    assert body["detail"].startswith("Nothing was added.")
    assert [(error["row"], error["language"]) for error in body["errors"]] == [(2, "de"), (3, "de"), (5, None)]
    assert "already in the glossary" in body["errors"][0]["message"]
    assert "formula character" in body["errors"][1]["message"]
    assert [entry.source_term for entry in await _entries(room.id)] == ["WebRTC"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"rows": []}',
        b'{"rows": [{"row": 0, "source_term": "x", "translations": {}}]}',
        b'{"rows": [{"row": 1, "source_term": "x", "translations": {"de": 5}}]}',
    ],
)
async def test_malformed_save_requests_are_refused(room_setup, body):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with _client() as client:
        response = await client.post(
            _path(event.id, room.id, save=True),
            content=body,
            headers={"content-type": "application/json", **ORIGIN},
            cookies=cookies["owner"],
        )
    assert response.status_code == 400
    assert await _entry_count() == 0


@pytest.mark.anyio
async def test_double_submitted_saves_add_each_term_once(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    rows = [{"row": 1, "source_term": "NLLB", "translations": {"de": "NLLB"}}]
    async with _client() as client:
        first, second = await asyncio.gather(
            _save(client, event.id, room.id, rows, cookies["owner"]),
            _save(client, event.id, room.id, rows, cookies["owner"]),
        )
    assert sorted([first.status_code, second.status_code]) == [200, 422]
    assert len(await _entries(room.id)) == 1


@pytest.mark.anyio
async def test_workspace_urls_reach_the_same_endpoints(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    rows = [{"row": 1, "source_term": "NLLB", "translations": {"de": "NLLB"}}]
    async with _client() as client:
        upload = await _suggest(client, event.id, room.id, b"We use NLLB.", cookies["owner"], prefix="/workspace")
        save = await _save(client, event.id, room.id, rows, cookies["owner"], prefix="/workspace")
        coordinator = await _suggest(client, event.id, room.id, b"NLLB", cookies["coordinator"], prefix="/workspace")
    assert upload.status_code == 200
    assert save.status_code == 200
    assert save.json()["redirect"].startswith(f"/workspace/events/{event.id}/rooms/{room.id}/?")
    assert coordinator.status_code == 403


@pytest.mark.anyio
async def test_room_page_offers_suggestions_to_owners(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with _client() as client:
        page = await client.get(f"/workspace/events/{event.id}/rooms/{room.id}/", cookies=cookies["owner"])
    html = page.text
    assert page.status_code == 200
    assert "data-glossary-suggestions" in html
    assert f'data-suggest-url="/workspace/events/{event.id}/rooms/{room.id}/ai-vocabulary/suggestions"' in html
    assert f'data-max-bytes="{settings.glossary_source_max_bytes}"' in html
    assert "Suggest translations with OpenAI" in html
    assert "js/glossary-suggestions.js" in html


@pytest.mark.anyio
async def test_room_page_without_a_key_points_to_api_keys(room_setup):
    event, room, cookies = room_setup["event"], room_setup["room"], room_setup["cookies"]
    async with get_session() as session:
        (await session.get(type(event), event.id)).encrypted_translation_openai_api_key = None
    async with _client() as client:
        page = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/", cookies=cookies["admin"])
    assert "Suggest translations with" not in page.text
    assert "add a translation API key" in page.text


# ── Worker pool ────────────────────────────────────────────────────────────


def test_extraction_slots_refuse_work_beyond_their_workers_and_free_them_afterwards():
    slots = TrackedSlots(workers=1)
    gate = threading.Event()
    first = slots.submit(gate.wait, 5)
    assert first is not None
    assert slots.submit(lambda: None) is None
    gate.set()
    assert slots.released.wait(5)
    later = slots.submit(lambda: 42)
    assert later is not None and later.result(timeout=5) == 42
