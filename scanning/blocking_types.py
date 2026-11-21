"""The type of a blocking place of the text review.

A blocking card is a word no two engines agree on, a block one engine
alone read, or a page card of another check. Most are noise a rerun can
settle (a space, a quote mark, a capital); some are words a person must
read (a number, a name, ``not`` against ``now``). :func:`type_of` names
which, from the readings alone, so the blocking review can walk one
type across the corpus and the levels can later be set per type.

The type is a label and decides nothing yet: every card it names still
blocks the approval. The rule reads the engines' strings and no
document, no row and no box, and imports no Django module.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

#: Every type, hardest first: a block or a page takes the first type of
#: any of its places. The slug is what the URL, the card and the
#: finding's ``types`` carry; the label is what a person reads.
TYPES: list[tuple[str, str]] = [
    ("different_text", "Engines read different text"),
    ("numbers", "Numbers differ"),
    ("meaning_word", "Meaning word differs"),
    ("different_word", "Different word"),
    ("name", "Name or capitalised word differs"),
    ("words_long", "Words missing or added (3+)"),
    ("single_long", "One engine alone (4+ words)"),
    ("words_short", "Words missing or added (1-2)"),
    ("star_mark", "Page or star mark"),
    ("single_short", "One engine alone (1-3 words)"),
    ("characters", "Characters differ in a word"),
    ("capitals", "Capitals only"),
    ("spacing", "Hyphen or spacing"),
    ("punctuation", "Punctuation only"),
    ("quotes", "Quotes only"),
    ("other", "Other"),
]

#: The reasons a vote leaves open that need no person: the letters and
#: digits of two readings or more agree, and only the spacing, the
#: punctuation, the quote marks or the capitals differ. A block whose
#: every open word is one of these is a warning, never blocking
#: (``ensemble.disagreement_level``).
MINOR = frozenset({"spacing", "punctuation", "quotes", "capitals"})

#: The slug of each type, to its label.
LABELS = dict(TYPES)

#: The rank of each type, hardest first.
RANK = {slug: index for index, (slug, _) in enumerate(TYPES)}

#: Short words where one letter turns the sense: a slip there is never
#: minor. Longer words that differ ("affirmed", "reversed") are far
#: apart anyway.
MEANING = frozenset(
    {
        "not",
        "no",
        "nor",
        "now",
        "never",
        "none",
        "may",
        "must",
        "shall",
        "cannot",
        "can",
        "with",
        "without",
        "and",
        "or",
        "is",
        "was",
        "be",
        "are",
        "were",
    }
)

QUOTES = "\"'‘’‚‛“”„‟`´"

#: A perpendicular's star page opening a line ("[801] General",
#: ``1746 "I cannot``, ``I76 "I``): two digits or more, the OCR's ``I``
#: or ``l`` for a 1.
STAR_LEAD = re.compile(
    r"^\s*[*]?\[?\s*[0-9Il|]{2,5}\s*\]?\s*(?=[\[\(\"'“‘{A-Za-z]|$)"
)

#: A headnote bracket opening a line, ``[9]`` or ``[4, 5]``: the OCR
#: glue deletes it from the text whoever read it.
BRACKET_LEAD = re.compile(r"^\s*\[\s*\d{1,3}\s*(?:[,\-–—]\s*\d{1,3}\s*)*\]\s*")

#: A reading of this many words or more is a block, judged as one.
BLOCK_WORDS = 4


def bare(text: str) -> str:
    """Return the text with every character but letters and digits out."""
    return re.sub(r"[^\w]", "", text)


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text).split()


def _far(a: str, b: str) -> bool:
    """Return whether two words are more than a slip or two apart."""
    match = SequenceMatcher(None, a, b)
    kept = sum(block.size for block in match.get_matching_blocks())
    return max(len(a), len(b)) - kept > 2 or match.ratio() < 0.6


def strip_star(text: str) -> str:
    """Return the text with a leading star page or headnote bracket out."""
    found = STAR_LEAD.match(text)
    if found and sum(ch.isdigit() for ch in found.group(0)) >= 2:
        text = text[found.end() :]
    found = BRACKET_LEAD.match(text)
    if found:
        text = text[found.end() :]
    return text


def _no_space(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _no_quotes(text: str) -> str:
    return re.sub(f"[{re.escape(QUOTES)}]", "", _no_space(text))


def _no_hyphen(text: str) -> str:
    return re.sub(r"[-‐-―]", "", _no_quotes(text))


#: The minor folds, lightest first. Two readings that agree under a
#: fold settle the place, whatever the odd engine read: ``¶10`` and
#: ``¶ 10`` against ``¶0`` is spacing.
MINOR_FOLDS = [
    ("spacing", _no_space),
    ("quotes", _no_quotes),
    ("spacing", _no_hyphen),
    ("punctuation", bare),
    ("capitals", lambda text: bare(text).lower()),
    ("star_mark", lambda text: bare(strip_star(text)).lower()),
]


def type_of(readings: list[str]) -> str:
    """Return the type of one place, from each engine's reading of it.

    :param readings: What each engine read there, ``""`` for nothing.
    :returns: A slug of :data:`TYPES`.
    :rtype: str
    """
    readings = [r or "" for r in readings]
    texts = [r for r in readings if r.strip()]
    if len(texts) < 2 and len(readings) < 2:
        return _single(texts)
    settled = _minor_fold(readings)
    if settled is not None:
        return settled[0]
    if not texts:
        return "other"
    if len(texts) < len(readings):
        return _missing(readings, texts)
    lowered = [bare(t).lower() for t in texts]
    longest = max(len(_words(t)) for t in texts)
    if (
        longest >= BLOCK_WORDS
        and min(
            SequenceMatcher(None, a, b).ratio()
            for i, a in enumerate(lowered)
            for b in lowered[i + 1 :]
        )
        < 0.5
    ):
        return "different_text"
    texts = [strip_star(t) for t in texts]
    return _words_differ(texts)


def _minor_fold(readings: list[str]):
    """Return ``(slug, fold, value)`` of the lightest minor fold the
    readings meet under, or None: ``value`` is what the agreeing
    readings fold to.

    Two readings alike already: the type is what the odd one differs
    by, so every reading must meet under the fold. Otherwise two
    readings meeting are enough (``¶10`` and ``¶ 10`` against ``¶0``).
    """
    need = len(readings) if _agree(readings, str) else 2
    for slug, fold in MINOR_FOLDS:
        value, count = Counter(fold(r) for r in readings).most_common(1)[0]
        if count >= need:
            return slug, fold, value
    return None


def settles_minor(readings: list[str], shown: str) -> bool:
    """Return whether a place needs no person: its reason is minor and
    the text already holds a reading of the agreeing ones.

    A minor reason says two readings agree in their letters and
    digits. That settles the place only when the text shows one of
    them: ``held,`` and ``held.`` against ``hold;`` is punctuation, and
    safe while the text reads ``held``; a text that reads ``hold`` would
    keep the odd engine's word with nothing to stop it.

    :param readings: What each engine read there, ``""`` for nothing.
    :param shown: What the text holds there now.
    :rtype: bool
    """
    readings = [r or "" for r in readings]
    if len(readings) < 2:
        return False
    settled = _minor_fold(readings)
    if settled is None or settled[0] not in MINOR:
        return False
    # The text's own reading must meet another engine's under a minor
    # fold: any of them, so a hyphen the others leave out (anti-trust
    # beside antitrust) is no odd word.
    shown = shown or ""
    others = list(readings)
    if shown in others:
        others.remove(shown)
    return any(
        fold(shown) == fold(other)
        for slug, fold in MINOR_FOLDS
        if slug in MINOR
        for other in others
    )


def _agree(readings: list[str], fold, need: int = 2) -> bool:
    """Return whether ``need`` readings or more meet under ``fold``."""
    return Counter(fold(r) for r in readings).most_common(1)[0][1] >= need


def _single(texts: list[str]) -> str:
    """Return the type of a block one engine alone read."""
    count = len(_words(texts[0])) if texts else 0
    return "single_long" if count >= BLOCK_WORDS else "single_short"


def _missing(readings: list[str], texts: list[str]) -> str:
    """Return the type of a place some engine read nothing at."""
    counts = [len(_words(t)) for t in texts]
    # Only a number where the others read nothing: a star page.
    if all(re.fullmatch(r"[\W\dIl|_]*\d[\W\dIl|_]*", t) for t in texts):
        return "star_mark"
    words = {w.lower() for t in texts for w in _words(t)}
    if words & MEANING:
        return "meaning_word"
    if any(re.search(r"\d", t) for t in texts):
        return "numbers"
    return "words_long" if max(counts) >= 3 else "words_short"


def _words_differ(texts: list[str]) -> str:
    """Return the type of a place every engine read words at."""
    words = [_words(t) for t in texts]
    lowered = [[w.lower() for w in x] for x in words]
    digits = {tuple(re.findall(r"\d+", t)) for t in texts}
    numbers_differ = len(digits) > 1
    if len({len(x) for x in lowered}) > 1:
        common = set.intersection(*[set(x) for x in lowered])
        odd = {w for x in lowered for w in x} - common
        if odd & MEANING:
            return "meaning_word"
        if odd and all(w.isdigit() for w in odd) and not numbers_differ:
            return "star_mark"
        # The same letters split differently (Tal- lahassee), or with a
        # slip or two besides: a slip, not a word.
        joined = ["".join(x) for x in lowered]
        if not numbers_differ and not any(
            _far(a, b) for i, a in enumerate(joined) for b in joined[i + 1 :]
        ):
            if len(set(joined)) == 1:
                return "spacing"
            cased = {w for x in words for w in x if w.lower() in odd}
            if any(w[:1].isupper() for w in cased):
                return "name"
            return "characters"
        if numbers_differ:
            return "numbers"
        extra = max(map(len, lowered)) - min(map(len, lowered))
        return "words_long" if extra >= 3 else "words_short"
    if numbers_differ:
        return "numbers"
    columns = list(zip(*lowered))
    for column in columns:
        if len(set(column)) > 1 and set(column) & MEANING:
            return "meaning_word"
    worst = None
    for column in columns:
        values = sorted(set(column))
        if len(values) == 1:
            continue
        # One reading close to every other: the engines slipped around
        # one word (Smith, Srnith, Smitn), not two words.
        if not any(
            all(not _far(c, o) for o in values if o != c) for c in values
        ):
            return "different_word"
        worst = "characters"
    if worst == "characters":
        for column in zip(*words):
            if len({w.lower() for w in column}) > 1 and any(
                w[:1].isupper() for w in column
            ):
                return "name"
    return worst or "other"


def hardest(types) -> str | None:
    """Return the hardest of some types, or None for none."""
    types = [t for t in types if t in RANK]
    return min(types, key=RANK.__getitem__) if types else None
