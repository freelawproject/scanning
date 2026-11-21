"""The practice cards of the volunteers' FAQ: real cards with their answers.

Each example is one card of the card review as it was on a real page
(158 F.4th), with the crop cut from the redacted PDF, the readings the
engines gave, the right answer, and why. The page draws them in the
card review's own shape, so the practice is the work; a choice is
checked in the browser and nothing is written.

To add one: cut the crop of the card (``cut_crops`` in the session's
notes, or any image of the line) into ``static/scanning/faq/practice``,
and append an entry. ``answer`` is the 1-based number of the right
reading as the keys show it, ``0`` for "leave blank", or
``{"typed": "..."}`` when the right words must be typed.
"""

from __future__ import annotations

#: The engines in the rank the card review shows them.
ENGINES = ("dots_mocr", "mistral_ocr", "surya")

EXAMPLES: list[dict] = [
    {
        "slug": "quotes-nested",
        "scenario": "Quotation marks",
        "lesson": "Nested quotation marks",
        "where": "page 21, body, right column",
        "image": "quotes-nested.png",
        "before": "prosecution's case to",
        "token": "come.'\"",
        "after": "United States v.",
        "readings": ["come.'\"", "come.''", 'come."'],
        "answer": 1,
        "why": (
            "The page closes two quotations at once: a single mark and then "
            "a double one. The first reading has both; the second reads two "
            "singles, the third drops one."
        ),
    },
    {
        "slug": "runs-on-capitals",
        "scenario": "A reading runs on",
        "lesson": "What is plainly in the block",
        "where": "page 9, body, left column",
        "image": "runs-on-capitals.png",
        "before": "in WILLIAM SHARP",
        "token": "McKECH-",
        "after": "",
        "readings": ["McKECH-", "MCKECHNIE,", "MCKECH-"],
        "answer": 1,
        "why": (
            'The line ends at "McKECH-" with a hyphen; the rest of the name '
            "is on the next line. The second reading runs on past the block "
            "and finishes the word, which we never want, and the third loses "
            'the small "c" of "Mc".'
        ),
    },
    {
        "slug": "hyphen-line-end",
        "scenario": "A word cut at the line end",
        "lesson": "The first reading is not always right",
        "where": "page 15, body, left column",
        "image": "hyphen-line-end.png",
        "before": "rather than merely",
        "token": "neglig-",
        "after": "",
        "readings": ["neglig-", "negligent", "negli-"],
        "answer": 3,
        "why": (
            'The page prints "negli-" at the end of the line. The first '
            "reading has one letter too many, the second finishes the word "
            "from the next line. Look at the print, not at the order of the "
            "readings."
        ),
    },
    {
        "slug": "gap-half-word",
        "scenario": "A gap",
        "lesson": "Words the first reader missed",
        "where": "page 13, body, right column",
        "image": "gap-half-word.png",
        "before": "",
        "token": "",
        "after": "And that is",
        "gap": True,
        "readings": ["", "representatives.", "resentatives."],
        "answer": 3,
        "why": (
            "A gap: the first reader read nothing here, the others read a "
            'word. The line starts "resentatives.", the second half of a '
            "word cut at the column end, and that is what belongs here. "
            "The second reading joins the halves, which is not what the "
            "block shows."
        ),
    },
    {
        "slug": "footnote-number-quotes",
        "scenario": "Footnote numbers",
        "lesson": "Keep the number as the line reads",
        "where": "page 7, body, left column",
        "image": "footnote-number-quotes.png",
        "before": "gun is over",
        "token": "there.”8",
        "after": "The defendant's response",
        "readings": ["there.”8", "there.'\"8", "there.8"],
        "answer": 2,
        "why": (
            "The footnote number 8 stays, as a plain number after the "
            "closing marks: the superscript is rebuilt later. The page "
            "closes a single and a double quotation before it, and only the "
            "second reading has both."
        ),
    },
    {
        "slug": "footnote-number",
        "scenario": "Footnote numbers",
        "lesson": "A number that is a footnote mark",
        "where": "page 6, body, left column",
        "image": "footnote-number.png",
        "before": "in a particular",
        "token": 'case."6',
        "after": "We lack jurisdiction",
        "readings": ['case."6', "case.6", "case.26"],
        "answer": 1,
        "why": (
            '"case." closes a quotation and footnote 6 follows. The second '
            "reading drops the quotation mark, the third misreads the mark as "
            "a 2."
        ),
    },
    {
        "slug": "bracketed-letter",
        "scenario": "Brackets in a quotation",
        "lesson": "A bracketed letter stays",
        "where": "page 8, body, right column",
        "image": "bracketed-letter.png",
        "before": "liability phase of",
        "token": "the [e]",
        "after": 'case." Although discovery',
        "readings": ["the [e]", "th[e]", "the"],
        "answer": 2,
        "why": (
            'The court quotes with a letter changed in brackets: "th[e]". '
            "The brackets are part of the text, with no space before them; "
            "the third reading drops them."
        ),
    },
    {
        "slug": "name-marks",
        "scenario": "Names",
        "lesson": "Every mark of a name",
        "where": "page 8, body, right column",
        "image": "name-marks.png",
        "before": 'another, valid conviction."',
        "token": "Al-Owhali",
        "after": "v. United States,",
        "readings": ["Al-Owhali", "Al'Owhali", "Al-'Owhali"],
        "answer": 3,
        "why": (
            "The name is printed with a hyphen and an apostrophe: "
            '"Al-\'Owhali". Only the third reading has both.'
        ),
    },
    {
        "slug": "one-engine",
        "scenario": "One engine alone",
        "lesson": "A block only one reader saw",
        "where": "page 3, body, right column",
        "image": "one-engine.png",
        "single": True,
        "before": "",
        "token": "I",
        "after": "",
        "readings": ["I"],
        "answer": 1,
        "why": (
            "Only one reader read this block, so there is nothing to vote "
            'on. The page has a section heading "I", and the reading '
            "matches it: keep it as shown. If the page had nothing there, "
            "you would type the right words or mark the block as not text."
        ),
    },
]


def cards() -> list[dict]:
    """Return the examples as the template draws them, numbered.

    Each gets its reading rows with the engine of each, the one grey
    word of context on either side (the card review's rule), and its
    number in the series.
    """
    out = []
    for n, example in enumerate(EXAMPLES, 1):
        before = (example.get("before") or "").split()
        after = (example.get("after") or "").split()
        rows = [
            {
                "key": str(i),
                "engine": ENGINES[i - 1] if i <= len(ENGINES) else "",
                "cls": f"r{min(i - 1, 2)}",
                "value": value,
                "ctx_before": before[-1] if before and value else "",
                "ctx_after": after[0] if after and value else "",
            }
            for i, value in enumerate(example["readings"], 1)
        ]
        out.append({**example, "n": n, "total": len(EXAMPLES), "rows": rows})
    return out
