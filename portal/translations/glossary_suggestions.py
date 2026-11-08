"""Glossary suggestions from a talk's script or slides (#1172): review rows, optional LLM translations, acceptance.

Nothing in this module writes to the database. Suggestions go to the event owner for review,
and only the rows the owner accepts reach :func:`validate_accepted_rows`, which runs each
one through the glossary's own checks (``validate_vocabulary_entry``, formula characters
and the overlap index) exactly as a CSV import does.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.models import AIVocabularyEntry, Event, Room, RoomTranslationLanguage
from portal.room_setup import MAX_LANGUAGES, Engine, engine_catalog, language_name
from portal.translations.constants import TranslationProviderEnum
from portal.translations.keys import get_translation_api_key
from portal.translations.prompts import MAX_TERM_CHARS
from portal.translations.term_extraction import ACRONYM, TermCandidate
from portal.translations.vocabulary import (
    VocabularyEntryInput,
    VocabularyOverlapIndex,
    VocabularyRowError,
    validate_vocabulary_entry,
)
from portal.translations.worker import PROVIDERS

logger = logging.getLogger(__name__)

ALL_LANGUAGES = "all"
#: Rows shown for review at once; the strongest candidates come first.
MAX_SUGGESTIONS = 60
MAX_SAVE_ROWS = 500
#: The whole translation step, across languages and batches, ends after this.
TRANSLATION_BUDGET_SECS = 30.0
#: Terms per request: the providers' per-request timeout was sized for live caption segments.
TERMS_PER_REQUEST = 20
MAX_PARALLEL_REQUESTS = 4

#: A translation request's language code, the rows it covered and their parsed translations.
_BatchResult = tuple[str, list["Suggestion"], list[str | None] | None]

_NUMBERED_LINE = re.compile(r"\(?(\d{1,3})[.):\]]\s*(.+)")
_ARROW = re.compile(r"\s(?:→|->|=>)\s")
_WRAPPING = "\"'“”‘’`*_ "


@dataclass(frozen=True)
class TargetLanguage:
    code: str
    name: str


@dataclass
class Suggestion:
    """One row of the review table. ``in_glossary`` lists languages the glossary already covers."""

    source_term: str
    kind: str
    occurrences: int
    match_type: str
    case_sensitive: bool
    in_glossary: list[str]
    translations: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "source_term": self.source_term,
            "kind": self.kind,
            "occurrences": self.occurrences,
            "match_type": self.match_type,
            "case_sensitive": self.case_sensitive,
            "in_glossary": self.in_glossary,
            "translations": self.translations,
        }


@dataclass(frozen=True)
class TranslationReport:
    """How the optional translation step went: ``unavailable`` (no key), ``off`` (not asked for),
    ``skipped`` (nothing to translate), ``complete``, ``partial`` or ``failed``."""

    status: str
    service: str | None = None
    failed_languages: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {"status": self.status, "service": self.service, "failed_languages": list(self.failed_languages)}


@dataclass(frozen=True)
class RowError:
    row: int
    language: str | None
    message: str

    def as_json(self) -> dict[str, Any]:
        return {"row": self.row, "language": self.language, "message": self.message}


_LanguageCode = Annotated[str, StringConstraints(max_length=20)]
_TermText = Annotated[str, StringConstraints(max_length=1000)]


class AcceptedRow(BaseModel):
    """A suggestion the owner accepted, with the owner's edits. Validated again on save."""

    row: int = Field(ge=1, le=10_000)
    source_term: _TermText
    translations: dict[_LanguageCode, _TermText] = Field(max_length=MAX_LANGUAGES + 1)
    match_type: str = Field(default="phrase", max_length=20)
    case_sensitive: bool = False


class SaveRequest(BaseModel):
    rows: list[AcceptedRow] = Field(min_length=1, max_length=MAX_SAVE_ROWS)


async def room_target_languages(session: AsyncSession, room: Room) -> list[TargetLanguage]:
    """The room's AI languages, the languages its glossary translates into; "all" if it has none."""
    codes = await session.scalars(
        select(RoomTranslationLanguage.language_code)
        .where(RoomTranslationLanguage.room_id == room.id, RoomTranslationLanguage.enabled.is_(True))
        .order_by(RoomTranslationLanguage.id)
    )
    languages = [
        TargetLanguage(code, language_name(code)) for code in dict.fromkeys(codes) if code != room.floor_language_code
    ]
    return languages or [TargetLanguage(ALL_LANGUAGES, "All languages")]


def suggestion_settings(candidate: TermCandidate) -> tuple[str, bool]:
    """Match type and case sensitivity to suggest.

    Acronyms match whole words in their exact case ("US" is not "us" or part of "USB");
    very short terms match whole words, so "Go" doesn't match inside "good".
    """
    if candidate.kind == ACRONYM:
        return "exact", True
    return ("exact" if len(candidate.term) <= 3 else "phrase"), False


def _in_glossary(index: VocabularyOverlapIndex, term: str, language: str, case_sensitive: bool) -> bool:
    """Whether saving ``term`` for ``language`` would overlap an entry already in ``index``."""
    return index.conflicts(VocabularyEntryInput(term, language, term, case_sensitive=case_sensitive))


def build_suggestions(
    candidates: Iterable[TermCandidate],
    languages: Sequence[TargetLanguage],
    existing: Iterable[AIVocabularyEntry],
    limit: int = MAX_SUGGESTIONS,
) -> list[Suggestion]:
    """Review rows for the strongest candidates, leaving out terms the glossary already covers."""
    index = VocabularyOverlapIndex()
    for stored in existing:
        index.add(stored)
    codes = [language.code for language in languages]
    suggestions: list[Suggestion] = []
    for candidate in candidates:
        if len(suggestions) >= limit:
            break
        if len(candidate.term) > MAX_TERM_CHARS:
            continue
        match_type, case_sensitive = suggestion_settings(candidate)
        if _in_glossary(index, candidate.term, ALL_LANGUAGES, case_sensitive):
            continue
        in_glossary = [code for code in codes if _in_glossary(index, candidate.term, code, case_sensitive)]
        if len(in_glossary) == len(codes):
            continue
        suggestions.append(
            Suggestion(candidate.term, candidate.kind, candidate.occurrences, match_type, case_sensitive, in_glossary)
        )
    return suggestions


def translation_service(event: Event, room: Room) -> Engine | None:
    """The LLM that may propose translations, if the event has a translation key.

    The room's own translation service when it is one of those, else the event's first
    (recommended) keyed service. The built-in translator (NLLB) is not an LLM and takes no
    key, so it is never used for suggestions.
    """
    local = TranslationProviderEnum.LOCAL.value
    llms = [engine for engine in engine_catalog(event).translation if engine.provider != local]
    for engine in llms:
        if engine.provider == room.floor_translation_provider:
            return Engine(engine.provider, room.floor_translation_model or engine.model, engine.label, engine.note)
    return llms[0] if llms else None


def _clean_term(value: str) -> str | None:
    value = value.strip().strip(_WRAPPING)
    # "Term -> Übersetzung": keep the translation.
    parts = _ARROW.split(value)
    value = re.sub(r"\s+", " ", parts[-1]).strip().strip(_WRAPPING)
    return value if 0 < len(value) <= MAX_TERM_CHARS else None


def parse_numbered_terms(reply: str, count: int) -> list[str | None]:
    """Translations from a model's reply to a numbered list, aligned with the terms sent.

    Numbered lines are matched by number, so a reply that skips, reorders or adds lines
    still lines up. A reply without numbers is used only when it has one line per term, and
    never for a single term, where a one-line refusal would look like a translation.
    Anything else yields no translations rather than misaligned ones.
    """
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    numbered: dict[int, str | None] = {}
    for line in lines:
        match = _NUMBERED_LINE.fullmatch(line)
        if match and 1 <= int(match.group(1)) <= count:
            numbered.setdefault(int(match.group(1)), _clean_term(match.group(2)))
    if numbered:
        return [numbered.get(number) for number in range(1, count + 1)]
    if count > 1 and len(lines) == count:
        return [_clean_term(line) for line in lines]
    return [None] * count


async def propose_translations(
    suggestions: Sequence[Suggestion],
    languages: Sequence[TargetLanguage],
    *,
    event: Event,
    room: Room,
    service: Engine,
) -> TranslationReport:
    """Ask the event's translation LLM for each term in each language, filling ``translations`` in place.

    A one-off request while the owner prepares the glossary; the live caption path never
    calls this. Any failure (no key, provider error, timeout, unreadable reply) only leaves
    cells empty: the deterministic suggestions stand on their own. Only the terms are sent,
    never the uploaded file. Nothing is logged beyond the provider, language and error type.
    """
    # Per language, only the terms the glossary doesn't already cover for it; a language every
    # term already covers is not asked for at all (and so can't be reported as failed).
    needed = {
        language.code: [row for row in suggestions if language.code not in row.in_glossary]
        for language in languages
        if language.code != ALL_LANGUAGES
    }
    targets = [language for language in languages if needed.get(language.code)]
    if not targets:
        return TranslationReport("skipped", service.label)
    provider = PROVIDERS.get(service.provider)
    try:
        api_key = get_translation_api_key(event, service.provider)
    except Exception as exc:  # A key that no longer decrypts must not break the suggestions.
        logger.warning("Glossary suggestion key for %s could not be read (%s)", service.provider, type(exc).__name__)
        api_key = None
    if provider is None or not api_key:
        return TranslationReport("failed", service.label, tuple(language.code for language in targets))

    source_name = language_name(room.floor_language_code) if room.floor_language_code else "the source language"
    semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

    async def translate_batch(language: TargetLanguage, batch: list[Suggestion]) -> _BatchResult:
        text = "\n".join(f"{number}. {row.source_term}" for number, row in enumerate(batch, start=1))
        async with semaphore:
            try:
                reply = await provider.translate(
                    provider_name=service.provider,
                    text=text,
                    target_lang_name=language.name,
                    target_lang_code=language.code,
                    source_lang_name=source_name,
                    model=service.model,
                    api_key=api_key,
                    persona=room.floor_ai_interpreter_persona,
                    style=room.floor_ai_interpretation_style,
                )
            except Exception as exc:  # Providers normally return None on failure; be safe either way.
                logger.warning(
                    "Glossary suggestion translation failed for %s via %s (%s)",
                    language.code,
                    service.provider,
                    type(exc).__name__,
                )
                reply = None
        return language.code, batch, parse_numbered_terms(reply, len(batch)) if reply else None

    tasks: list[asyncio.Task[_BatchResult]] = []
    for language in targets:
        rows = needed[language.code]
        tasks.extend(
            asyncio.create_task(translate_batch(language, rows[start : start + TERMS_PER_REQUEST]))
            for start in range(0, len(rows), TERMS_PER_REQUEST)
        )
    done, pending = await asyncio.wait(tasks, timeout=TRANSLATION_BUDGET_SECS)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning("Glossary suggestion translation via %s ran out of time", service.provider)
        await asyncio.gather(*pending, return_exceptions=True)

    filled: Counter[str] = Counter()
    for task in done:
        code, batch, values = task.result()
        for row, value in zip(batch, values or ()):
            if value:
                row.translations[code] = value
                filled[code] += 1
    failed = tuple(language.code for language in targets if not filled[language.code])
    if not failed:
        return TranslationReport("complete", service.label)
    return TranslationReport("failed" if len(failed) == len(targets) else "partial", service.label, failed)


def validate_accepted_rows(
    rows: Sequence[AcceptedRow], existing: Iterable[AIVocabularyEntry]
) -> tuple[list[VocabularyEntryInput], list[RowError]]:
    """Run accepted rows through the glossary's validation and overlap rules.

    Each filled translation becomes one entry, checked by ``validate_vocabulary_entry``
    (required fields, length limits, formula characters, language, match type) and then
    against the room's glossary and the other accepted rows. Returns the valid entries and
    one error per problem; the caller saves nothing unless there are no errors.
    """
    in_glossary = VocabularyOverlapIndex()
    for stored in existing:
        in_glossary.add(stored)
    accepted = VocabularyOverlapIndex()
    entries: list[VocabularyEntryInput] = []
    errors: list[RowError] = []
    for row in rows:
        filled = [(code, target) for code, target in row.translations.items() if target.strip()]
        if not filled:
            errors.append(RowError(row.row, None, f"Row {row.row}: add a translation for at least one language."))
            continue
        for code, target in filled:
            fields = {
                "source_term": row.source_term,
                "target_language": code,
                "target_term": target,
                "match_type": row.match_type,
                "case_sensitive": "true" if row.case_sensitive else "false",
            }
            try:
                entry = validate_vocabulary_entry(fields, row.row)
            except VocabularyRowError as exc:
                errors.append(RowError(row.row, code, str(exc)))
                continue
            if in_glossary.conflicts(entry):
                message = (
                    f"'{entry.source_term}' is already in the glossary for target language '{entry.target_language}'"
                )
            elif accepted.conflicts(entry):
                message = f"duplicate term '{entry.source_term}' for target language '{entry.target_language}'"
            else:
                accepted.add(entry)
                entries.append(entry)
                continue
            errors.append(RowError(row.row, code, f"Row {row.row}: {message}"))
    return entries, errors
