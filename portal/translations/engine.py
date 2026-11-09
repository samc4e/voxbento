"""The interface the translation worker uses to run a translation model.

An engine receives segments and the target languages wanted for them, and returns
one translation per (segment, target) pair. How the model is loaded and run, and how
languages map to its own codes, stays inside the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

from portal.translations.prompts import VocabularyEntry


@dataclass(frozen=True, slots=True)
class TranslationLanguage:
    """A source or target language as the room or booth stores it.

    ``code`` is the ISO 639-1 code, or an empty string when a room has no floor
    language set. ``name`` is the stored language name, usually in English (e.g.
    "French"); some rows store the code itself, and a source language without a
    code falls back to "English". The local NLLB engine looks languages up by name.
    """

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class TranslationContext:
    """Settings that apply to every pair in one ``translate_batch`` call."""

    model: str
    #: Kept out of ``repr`` so a logged context never shows the key.
    api_key: str | None = field(default=None, repr=False)
    persona: str | None = None
    style: str | None = None
    vocabulary_entries: Sequence[VocabularyEntry] = ()


class TranslationEngine(ABC):
    """Translates text from one source language into one or more target languages."""

    @abstractmethod
    async def translate_batch(
        self,
        source_lang: TranslationLanguage,
        targets: Sequence[TranslationLanguage],
        texts: Sequence[str],
        context: TranslationContext,
    ) -> list[list[str | None]]:
        """Translate every text into every target language.

        Returns one row per text, in the order of ``texts``. Each row holds one entry
        per target, in the order of ``targets``, so ``results[i][j]`` is ``texts[i]``
        in ``targets[j]``. An entry is None when that pair failed (for example, the
        engine does not support the language) and may be an empty string when there
        was nothing to translate; callers treat both as no translation.

        An engine may raise, and callers treat that as a failure of the whole batch.
        The caller owns the time budget and may cancel the call.
        """
