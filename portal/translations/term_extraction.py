"""Deterministic glossary candidates from a talk's source text (#1172).

No model, network or API key is involved, and the same text always gives the same
suggestions in the same order. The pass looks for what interpreters prepare from a script:
acronyms (NLLB, WHIP), mixed-case product names (VoxBento, WebRTC), capitalised names and
phrases mid-sentence (Linus Torvalds, Open Source Summit) and uncommon words the text repeats.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from portal.translations.common_words import COMMON_WORDS, ENGLISH_STOPWORDS, STOPWORDS

ACRONYM = "acronym"
MIXED_CASE = "mixed_case"
NAME = "name"
RARE = "rare"
#: Provisional: an uncommon word seen capitalised only at sentence starts. Never returned as a kind.
INITIAL = "initial"

#: A term found in several ways is reported as its strongest kind.
_KIND_RANK = {ACRONYM: 0, MIXED_CASE: 1, NAME: 2, RARE: 3, INITIAL: 4}
#: Ranking weight per occurrence: shapes that are almost always terms outrank plain rarity.
_KIND_WEIGHT = {ACRONYM: 3, MIXED_CASE: 3, NAME: 2, RARE: 1}

#: Longer input is cut here (roughly a 300-page script), so time and memory stay bounded.
MAX_TEXT_CHARS = 500_000
#: Upper bound on returned candidates; callers usually show fewer.
MAX_CANDIDATES = 500
#: Longer capitalised runs are titles or headings, not terms.
MAX_PHRASE_WORDS = 4
MAX_TOKEN_CHARS = 40
MIN_RARE_CHARS = 4
#: An uncommon word must recur before it is worth suggesting.
MIN_RARE_REPEATS = 2
#: English prose is roughly 40 % function words. Below this share the text is not English,
#: so "not a common English word" says nothing about rarity and the rare-word pass is skipped.
_MIN_ENGLISH_SHARE = 0.15
#: Languages that capitalise every noun, so one capitalised word does not mark a name.
_NOUN_CAPITALISING = frozenset({"de", "lb"})

_LIGATURES = str.maketrans(
    {
        "\N{LATIN SMALL LIGATURE FF}": "ff",
        "\N{LATIN SMALL LIGATURE FI}": "fi",
        "\N{LATIN SMALL LIGATURE FL}": "fl",
        "\N{LATIN SMALL LIGATURE FFI}": "ffi",
        "\N{LATIN SMALL LIGATURE FFL}": "ffl",
        "\N{LATIN SMALL LIGATURE LONG S T}": "st",
        "\N{LATIN SMALL LIGATURE ST}": "st",
    }
)
_INVISIBLE = re.compile(
    "[\N{SOFT HYPHEN}\N{MONGOLIAN VOWEL SEPARATOR}\N{ZERO WIDTH SPACE}-\N{RIGHT-TO-LEFT MARK}"
    "\N{LEFT-TO-RIGHT EMBEDDING}-\N{RIGHT-TO-LEFT OVERRIDE}\N{WORD JOINER}-\N{INVISIBLE PLUS}"
    "\N{ZERO WIDTH NO-BREAK SPACE}]"
)
_LINE_SEPARATORS = re.compile("[\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\x0b\x0c\x85]")
_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[^\S\n]+")
# Bounded quantifiers keep these linear on hostile input (no catastrophic backtracking).
_URL_OR_EMAIL = re.compile(r"(?:https?://|www\.)\S+|[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}", re.IGNORECASE)
# Words with inner apostrophes, dots, hyphens or ampersands stay whole: O'Reilly, Node.js, Wi-Fi, AT&T, C++.
_TOKEN = re.compile(r"[^\W_]+(?:['’.&+-][^\W_]+)*(?:\+\+|#)?")
_TOKEN_PARTS = re.compile(r"['’.&+-]")
_MARKDOWN_HEADING = re.compile(r"\s{0,3}#{1,6}\s")
_ROMAN_NUMERAL = re.compile(r"[IVX]{2,4}")
_PLURAL_ACRONYM = re.compile(r"([A-Z][A-Z0-9&]*[A-Z0-9])s")
_POSSESSIVE = ("'s", "’s")
#: Characters between two words that start a new sentence or clause, so the next capital is not evidence.
_SENTENCE_BREAKS = frozenset(".!?…:•·|\"'“”‘’«»([{—–-")
#: Lowercase words allowed inside a name: Bank of America, Ludwig van Beethoven.
_CONNECTORS = frozenset({"of", "de", "del", "della", "der", "den", "des", "di", "da", "du", "van", "von", "la", "le"})
_ABBREVIATIONS = frozenset(
    {"dr", "mr", "mrs", "ms", "prof", "st", "vs", "e.g", "i.e", "etc", "fig", "inc", "ltd", "jr", "sr", "no", "approx"}
)
#: All-caps tokens that are conversation, not terminology.
_ACRONYM_NOISE = frozenset(
    {"OK", "AM", "PM", "VS", "ETC", "PS", "Q&A", "FAQ", "TODO", "AKA", "FYI", "ASAP", "TBD", "TBA"}
)
_SUFFIXES = (
    ("ies", "y"),
    ("ied", "y"),
    ("ings", ""),
    ("ing", ""),
    ("ing", "e"),
    ("ers", ""),
    ("ed", ""),
    ("ed", "e"),
    ("es", ""),
    ("s", ""),
    ("ly", ""),
    ("er", ""),
    ("er", "e"),
    ("est", ""),
    ("ness", ""),
    ("ment", ""),
)


@dataclass(frozen=True)
class TermCandidate:
    """A suggested source term: its preferred spelling, why it was picked and how often it appears."""

    term: str
    kind: str
    occurrences: int


@dataclass(frozen=True)
class _Token:
    text: str
    starts_sentence: bool
    #: Only spaces separate it from the previous token, so it can continue a name.
    joined: bool


@dataclass
class _Evidence:
    first: int
    forms: dict[str, Counter[str]] = field(default_factory=dict)

    def add(self, kind: str, form: str) -> None:
        self.forms.setdefault(kind, Counter())[form] += 1

    @property
    def kind(self) -> str:
        return min(self.forms, key=_KIND_RANK.__getitem__)


def normalize_text(text: str) -> str:
    """NFC, PDF ligatures resolved, invisible and control characters removed, URLs and emails dropped."""
    text = unicodedata.normalize("NFC", text[:MAX_TEXT_CHARS]).translate(_LIGATURES)
    text = _LINE_SEPARATORS.sub("\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _CONTROL.sub(" ", _INVISIBLE.sub("", text))
    return _SPACES.sub(" ", _URL_OR_EMAIL.sub(" ", text))


def is_common_word(word: str) -> bool:
    """True for function words and everyday English words, including simple inflections ("running")."""
    key = word.casefold().replace("’", "'")
    if "-" in key:
        parts = [part for part in key.split("-") if part]
        return bool(parts) and all(is_common_word(part) for part in parts)
    if key in COMMON_WORDS or key in STOPWORDS:
        return True
    if "'" in key:
        return key.split("'", 1)[0] in STOPWORDS
    for suffix, replacement in _SUFFIXES:
        stem = key[: -len(suffix)]
        if not key.endswith(suffix) or len(stem) < 3:
            continue
        if stem + replacement in COMMON_WORDS:
            return True
        # Doubled final consonant: running -> run, bigger -> big.
        if len(stem) >= 4 and stem[-1] == stem[-2] and stem[:-1] in COMMON_WORDS:
            return True
    return False


def _strip_possessive(word: str) -> str:
    return word[:-2] if word.endswith(_POSSESSIVE) and len(word) > 3 else word


def _acronym(word: str) -> str | None:
    """The acronym a token spells, if it is one: NLLB, APIs (as API), 5G, H.264, GPT-4o."""
    if plural := _PLURAL_ACRONYM.fullmatch(word):
        word = plural.group(1)
    if word in _ACRONYM_NOISE or _ROMAN_NUMERAL.fullmatch(word):
        return None
    upper = sum(ch.isupper() for ch in word)
    lower = sum(ch.islower() for ch in word)
    if upper >= 2 and lower == 0:
        return word
    if upper >= 1 and upper >= lower and any(ch.isdigit() for ch in word):
        return word
    return None


def _is_mixed_case(word: str) -> bool:
    """VoxBento, WebRTC, iPhone: a capital inside a word part that also has lowercase letters.

    Checked per part, so hyphenated title case ("Real-Time", "Wi-Fi") stays an ordinary word.
    """
    return any(
        any(ch.islower() for ch in part) and any(ch.isupper() for ch in part[1:]) for part in _TOKEN_PARTS.split(word)
    )


def _tokens(line: str) -> list[_Token]:
    tokens: list[_Token] = []
    previous = ""
    end = 0
    for match in _TOKEN.finditer(line):
        gap = line[end : match.start()]
        if not tokens:
            starts = True
        else:
            abbreviation = gap.strip() == "." and previous.casefold() in _ABBREVIATIONS
            starts = not abbreviation and not _SENTENCE_BREAKS.isdisjoint(gap)
        tokens.append(_Token(match.group(), starts, bool(tokens) and not gap.strip()))
        previous = match.group()
        end = match.end()
    return tokens


def _is_shouted(tokens: list[_Token]) -> bool:
    """An all-caps line ("INTRODUCTION TO WEBRTC"), where capitals say nothing about names."""
    words = [token.text for token in tokens if sum(ch.isalpha() for ch in token.text) >= 2]
    return len(words) >= 2 and not any(ch.islower() for word in words for ch in word)


def _is_heading(line: str, tokens: list[_Token]) -> bool:
    """A Markdown heading or a title-case line, as on a slide ("Getting Started With Kubernetes")."""
    if _MARKDOWN_HEADING.match(line):
        return True
    if line.rstrip().endswith((".", "!", "?", ",", ";", ":")):
        return False
    content = [
        token.text
        for token in tokens
        if any(ch.isalpha() for ch in token.text)
        and token.text.casefold() not in STOPWORDS
        and token.text.casefold() not in _CONNECTORS
    ]
    return 0 < len(content) <= 12 and all(word[0].isupper() for word in content)


def _uncommon_groups(words: list[str], is_common: Callable[[str], bool]) -> list[list[str]]:
    """Runs of uncommon words, joined by connectors: ["Getting", "Started", "With", "Kubernetes"] -> [["Kubernetes"]]."""
    groups: list[list[str]] = []
    group: list[str] = []
    for word in [*words, ""]:
        if word and ((group and word in _CONNECTORS) or not is_common(word)):
            group.append(word)
            continue
        while group and group[-1] in _CONNECTORS:
            group.pop()
        if group:
            groups.append(group)
        group = []
    return groups


class _Extractor:
    def __init__(self, *, single_word_names: bool, everyday_words_known: bool) -> None:
        self.single_word_names = single_word_names
        self.everyday_words_known = everyday_words_known
        self.position = 0
        self.evidence: dict[str, _Evidence] = {}
        self.counts: Counter[str] = Counter()
        #: Words seen in lowercase, with their first position: candidates for the rare-word pass.
        self.lowercase: dict[str, int] = {}
        self.english_words = 0
        # Memo for is_common_word, scoped to this one text. A process-wide cache would keep words
        # from confidential uploads in memory after the request ends.
        self._common: dict[str, bool] = {}

    def is_common(self, word: str) -> bool:
        known = self._common.get(word)
        if known is None:
            known = self._common[word] = is_common_word(word)
        return known

    def note(self, kind: str, term: str) -> None:
        key = term.casefold()
        evidence = self.evidence.get(key)
        if evidence is None:
            evidence = self.evidence[key] = _Evidence(self.position)
        evidence.add(kind, term)

    def feed(self, line: str) -> None:
        tokens = _tokens(line)
        if not tokens:
            return
        shouted = _is_shouted(tokens)
        heading = shouted or _is_heading(line, tokens)
        run: list[str] = []
        run_starts_sentence = False
        for token in tokens:
            self.position += 1
            word = _strip_possessive(token.text)
            self.count(word)
            self.classify(word, shouted)
            if shouted:
                continue
            capitalised = word[:1].isupper()
            if run and token.joined and not token.starts_sentence and (capitalised or word in _CONNECTORS):
                run.append(word)
                continue
            self.flush(run, run_starts_sentence, heading)
            run, run_starts_sentence = ([word], token.starts_sentence) if capitalised else ([], False)
        self.flush(run, run_starts_sentence, heading)

    def count(self, word: str) -> None:
        key = word.casefold()
        self.counts[key] += 1
        if key in ENGLISH_STOPWORDS:
            self.english_words += 1
        if word.islower() and MIN_RARE_CHARS <= len(word) <= MAX_TOKEN_CHARS:
            self.lowercase.setdefault(key, self.position)

    def classify(self, word: str, shouted: bool) -> None:
        if not 2 <= len(word) <= MAX_TOKEN_CHARS:
            return
        if acronym := _acronym(word):
            # Short all-caps words in mixed-case text are real acronyms (IT, US); long or shouted ones
            # that spell ordinary words ("AGENDA", "WHO WE ARE") are emphasis.
            if not (self.is_common(acronym) and (shouted or len(acronym) > 3)):
                self.note(ACRONYM, acronym)
        elif _is_mixed_case(word):
            self.note(MIXED_CASE, word)

    def flush(self, run: list[str], starts_sentence: bool, heading: bool) -> None:
        """Record a capitalised run as a name, once capitals forced by the sentence are discounted."""
        words = list(run)
        while words and words[-1] in _CONNECTORS:
            words.pop()
        if heading:
            # Title case capitalises every word, so only runs of uncommon words are names:
            # "Getting Started With Kubernetes" gives "Kubernetes".
            for group in _uncommon_groups(words, self.is_common):
                self.note_name(group, forced=False)
            return
        # Only the sentence's first word has a forced capital ("The Open Source Summit" keeps "Open").
        forced = starts_sentence
        while words and words[0].casefold() in STOPWORDS:
            words.pop(0)
            forced = False
        # A forced capital on an everyday word is no evidence ("Today Google said" keeps "Google"). Without
        # an everyday-word list for the language, a forced capital can't be judged, so it never counts.
        if forced and words and (not self.everyday_words_known or self.is_common(words[0])):
            words.pop(0)
            forced = False
            while words and words[0].casefold() in STOPWORDS:
                words.pop(0)
            # "Mission Control lets you..." loses "Mission" above; a leftover "Control" is not a name.
            if all(self.is_common(word) for word in words):
                return
        self.note_name(words, forced)

    def note_name(self, words: list[str], forced: bool) -> None:
        if not words or len(words) > MAX_PHRASE_WORDS:
            return
        if len(words) > 1:
            self.note(NAME, " ".join(words))
            return
        word = words[0]
        # All-caps and mixed-case words are judged by `classify`, so "Q&A" or "OK" never become names.
        if not self.single_word_names or len(word) < 2 or word.isupper() or _is_mixed_case(word):
            return
        # An uncommon word capitalised only because it starts a sentence is a name ("Kubernetes makes...")
        # or just an unusual word ("Diarization splits..."); `candidates` decides once the text is read.
        self.note(INITIAL if forced else NAME, word)

    def candidates(self, rare_words: bool) -> list[tuple[TermCandidate, int]]:
        found: list[tuple[TermCandidate, int]] = []
        for key, evidence in self.evidence.items():
            kind = evidence.kind
            term, _ = evidence.forms[kind].most_common(1)[0]
            detections = sum(evidence.forms[kind].values())
            if kind == INITIAL:
                # Also written in lowercase, so it is an ordinary word: the rare-word pass weighs it.
                if key in self.lowercase:
                    continue
                kind = NAME
            # Everyday words capitalised once are emphasis ("We built Real-Time captions"), not names.
            if kind == NAME and detections < 2 and all(self.is_common(word) for word in term.split()):
                continue
            # Acronyms are counted case-sensitively ("US" is not "us"); phrases as detected; other words
            # in every spelling, since the glossary matches them case-insensitively.
            occurrences = detections if kind == ACRONYM or " " in term else max(detections, self.counts[key])
            found.append((TermCandidate(term, kind, occurrences), evidence.first))
        if rare_words:
            for key, first in self.lowercase.items():
                seen = self.evidence.get(key)
                if (seen is not None and seen.kind != INITIAL) or self.counts[key] < MIN_RARE_REPEATS:
                    continue
                if sum(ch.isalpha() for ch in key) < MIN_RARE_CHARS or self.is_common(key):
                    continue
                found.append((TermCandidate(key, RARE, self.counts[key]), first))
        return found


def extract_candidate_terms(
    text: str, *, source_language: str | None = None, limit: int = MAX_CANDIDATES
) -> list[TermCandidate]:
    """Suggest glossary source terms found in ``text``, most significant first.

    ``source_language`` is the room's spoken language (ISO 639-1). Judging a sentence's first
    word and the rare-word pass both need an everyday-word list, which exists for English only;
    single capitalised words are not taken as names in languages that capitalise every noun.
    """
    language = (source_language or "en").split("-", 1)[0].lower()
    extractor = _Extractor(single_word_names=language not in _NOUN_CAPITALISING, everyday_words_known=language == "en")
    for line in normalize_text(text).split("\n"):
        extractor.feed(line)
    english = extractor.english_words >= _MIN_ENGLISH_SHARE * max(sum(extractor.counts.values()), 1)
    ranked = extractor.candidates(rare_words=extractor.everyday_words_known and english)
    ranked.sort(key=lambda item: (-_KIND_WEIGHT[item[0].kind] * item[0].occurrences, item[1]))
    return [candidate for candidate, _ in ranked[: max(0, min(limit, MAX_CANDIDATES))]]
