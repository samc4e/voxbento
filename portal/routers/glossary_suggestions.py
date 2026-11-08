"""Glossary suggestions from a talk's script or slides (#1172).

Two JSON endpoints beside the room glossary routes in ``admin.py``, with the same access
rule (event owner or Admin) and the same scope check. Reading a file never writes to the
database; saving stores only the rows the owner accepted, after the glossary's own validation.

The file arrives as the raw request body, so it is read into memory under a hard size cap
and never touches the disk: there is no temporary file to clean up or leak, and nothing
is kept once the response is sent.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import require_event_owner
from portal.config import settings
from portal.database import get_event_by_id, get_room_by_id, get_session
from portal.models import AIVocabularyEntry, Event, Room
from portal.translations.glossary_suggestions import (
    SaveRequest,
    TranslationReport,
    build_suggestions,
    propose_translations,
    room_target_languages,
    translation_service,
    validate_accepted_rows,
)
from portal.translations.source_material import SourceMaterialError, format_size, read_source_material
from portal.translations.term_extraction import TermCandidate, extract_candidate_terms
from portal.translations.vocabulary import VocabularyScopeError, new_vocabulary_entry, validate_vocabulary_scope
from portal.utils import verify_same_origin
from portal.workspace_routing import management_url

router = APIRouter()
logger = logging.getLogger(__name__)

SUGGESTIONS_PATH = "/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/suggestions"
#: The browser sends the file name (URL-encoded) here; it is used for its extension only.
FILENAME_HEADER = "x-source-filename"
EXTRACTION_TIMEOUT_SECS = 30.0
MAX_SAVE_BODY_BYTES = 1024 * 1024
_NO_STORE = {"Cache-Control": "no-store"}


class _ExtractionSlots:
    """Reading files is CPU work on a small pool of its own, so it can't hold up the threads live
    translation uses. When every worker is busy, more uploads are refused rather than queued."""

    def __init__(self, workers: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="glossary-source")
        self._limit = workers
        self._busy = 0
        self._lock = threading.Lock()

    def submit[T](self, fn: Callable[..., T], *args: object) -> Future[T] | None:
        with self._lock:
            if self._busy >= self._limit:
                return None
            self._busy += 1
        try:
            future = self._pool.submit(fn, *args)
        except BaseException:
            self._release()
            raise
        # Released when the work ends, even after the request gave up waiting for it, so a
        # slow file keeps its slot and can't be multiplied by retries.
        future.add_done_callback(self._release)
        return future

    def _release(self, _future: Future[Any] | None = None) -> None:
        with self._lock:
            self._busy -= 1


_extraction = _ExtractionSlots(workers=2)
_save_locks: dict[int, asyncio.Lock] = {}


def _check_origin(request: Request) -> None:
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")


async def _read_body(request: Request, limit: int, too_large: str) -> bytes:
    """The request body, refused with 413 as soon as it passes ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=too_large)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=too_large)
    return bytes(body)


async def _room_in_event(session: AsyncSession, event_id: int, room_id: int) -> tuple[Event, Room]:
    try:
        await validate_vocabulary_scope(session, event_id, room_id=room_id)
    except VocabularyScopeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.") from exc
    event = await get_event_by_id(session, event_id)
    room = await get_room_by_id(session, room_id)
    if event is None or room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
    return event, room


async def _room_glossary(session: AsyncSession, event_id: int, room_id: int) -> list[AIVocabularyEntry]:
    return list(
        await session.scalars(
            select(AIVocabularyEntry).where(
                AIVocabularyEntry.event_id == event_id, AIVocabularyEntry.room_id == room_id
            )
        )
    )


def _read_terms(
    data: bytes, filename: str, content_type: str | None, source_language: str | None
) -> tuple[list[TermCandidate], bool]:
    source = read_source_material(
        data, filename=filename, content_type=content_type, max_bytes=settings.glossary_source_max_bytes
    )
    return extract_candidate_terms(source.text, source_language=source_language), source.truncated


async def _extract_terms(
    data: bytes, filename: str, content_type: str | None, source_language: str | None
) -> tuple[list[TermCandidate], bool]:
    future = _extraction.submit(_read_terms, data, filename, content_type, source_language)
    if future is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Other files are still being read. Try again in a moment.",
        )
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), EXTRACTION_TIMEOUT_SECS)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Reading this file took too long. Try a shorter file, or save the slides as plain text.",
        ) from None
    except SourceMaterialError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@router.post(SUGGESTIONS_PATH, dependencies=[Depends(require_event_owner)])
async def suggest_glossary_terms(
    request: Request, event_id: int, room_id: int, translate: bool = False
) -> JSONResponse:
    """Read an uploaded script or slide deck and return suggested glossary rows. Nothing is saved."""
    _check_origin(request)
    async with get_session() as session:
        event, room = await _room_in_event(session, event_id, room_id)
        languages = await room_target_languages(session, room)
        existing = await _room_glossary(session, event_id, room_id)

    max_bytes = settings.glossary_source_max_bytes
    data = await _read_body(request, max_bytes, f"Files must be {format_size(max_bytes)} or smaller.")
    filename = urllib.parse.unquote(request.headers.get(FILENAME_HEADER, ""))[-255:]
    candidates, truncated = await _extract_terms(
        data, filename, request.headers.get("content-type"), room.floor_language_code
    )
    del data  # The upload is not needed past this point and is never kept.

    suggestions = build_suggestions(candidates, languages, existing)
    service = translation_service(event, room)
    if service is None:
        report = TranslationReport("unavailable")
    elif not translate:
        report = TranslationReport("off", service.label)
    else:
        report = await propose_translations(suggestions, languages, event=event, room=room, service=service)
    return JSONResponse(
        {
            "languages": [{"code": language.code, "name": language.name} for language in languages],
            "suggestions": [suggestion.as_json() for suggestion in suggestions],
            "truncated": truncated,
            "translation": report.as_json(),
        },
        headers=_NO_STORE,
    )


@router.post(f"{SUGGESTIONS_PATH}/save", dependencies=[Depends(require_event_owner)])
async def save_glossary_suggestions(request: Request, event_id: int, room_id: int) -> JSONResponse:
    """Add the accepted suggestions to the room glossary: all of them, or none if any row is invalid."""
    _check_origin(request)
    body = await _read_body(request, MAX_SAVE_BODY_BYTES, "Too many terms to add at once. Add them in smaller groups.")
    try:
        payload = SaveRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The accepted terms couldn't be read. Reload the page and try again.",
        ) from None

    # Per room, so a double-clicked save can't check for duplicates before the first save lands.
    async with _save_locks.setdefault(room_id, asyncio.Lock()):
        async with get_session() as session:
            await _room_in_event(session, event_id, room_id)
            entries, errors = validate_accepted_rows(payload.rows, await _room_glossary(session, event_id, room_id))
            if errors:
                return JSONResponse(
                    {
                        "detail": "Nothing was added. Fix or reject the rows marked below, then save again.",
                        "errors": [error.as_json() for error in errors],
                    },
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    headers=_NO_STORE,
                )
            session.add_all(new_vocabulary_entry(entry, event_id=event_id, room_id=room_id) for entry in entries)
            await session.flush()

    languages = sorted({entry.target_language for entry in entries})
    query = urllib.parse.urlencode({"vocab_imported": len(entries), "vocab_languages": ", ".join(languages)})
    return JSONResponse(
        {
            "saved": len(entries),
            "languages": languages,
            "redirect": f"{management_url(request, f'events/{event_id}/rooms/{room_id}/')}?{query}",
        },
        headers=_NO_STORE,
    )
