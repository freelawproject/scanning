"""The approved text of an opinion: a text flow, not the blocks of a page (#375).

The ensemble writes the text of an opinion as the groups of each page,
because a page is what the curator reads against the PDF. The layout
cuts those groups: a paragraph that ends at the bottom of a column and
goes on at the top of the next column, or of the next page, is two
groups. The final XML and the tagger read paragraphs, so the approval
puts such a paragraph together again, and keeps every break the text
itself makes (a new paragraph, a heading, the edge of a blockquote).

**The join rule** (:func:`body`) puts group B after group A in one
paragraph when all four are true:

1. A layout edge is between them: A is the last body group of the left
   column and B the first of the right column of the same page, or A
   is the last body group of a page and B the first of the next page.
   The running head and the running foot are not body, so they are
   no edge.
2. Nothing went between them: no group a redaction or a mask took is
   between A and B in the reading order (``after`` on the drop, schema
   9 of the ensemble document).
3. The same kind of block: two paragraphs or two list items, with the
   same blockquote flag. A group a curator quoted in part
   (``quote_span``, #419) is its parts: the text before the span, the
   span, and the text after, each a paragraph of its own with its own
   flag, and only the first part can join the group above it. A list
   item group that starts an item at its first word starts a block
   (#428): it is the next item, not the rest of the one above.
4. The text says the sentence goes on (:func:`continues`).

A group the ensemble stamped ``syllabus_start`` (#518) starts a block
whatever the four say, and its paragraph carries the stamp: the final
XML opens the syllabus there, and a join would hide the start inside
the paragraph above.

A column that ends at the end of a sentence, with a capital letter at
the top of the next, is a break the rule keeps. The legacy pipeline
read the indent of the first line to tell it apart; a group box has no
line boxes, and a wrong join merges two paragraphs, which is worse
than one break too many.

The join writes ``\\n``, the character of a line break inside a group,
so the dehyphenation of the tagger's projection (#310) treats a word
cut at a column edge and a word cut at a line end by one rule. This
module never removes a hyphen.

**The footnotes** (:func:`footnotes`) are a list keyed by label: a
group that starts with a label starts a footnote, and a group with no
label goes on with the footnote before it, as a paragraph of its own
or joined to the last one by the same rule. A concurrence or a dissent
can number its notes from 1 again (#442), so a small number after a
larger one starts a footnote where the marks of the body start again
at that number too, and a label repeats in the list.

Everything here reads the ensemble document alone and imports no
Django module (``test_paragraphs`` pins that), so a better rule is a
new :data:`JOIN_RULE` and ``rewrite_approved_text``, never a new
approval.
"""

import re

#: Version of the approved object this module writes. The
#: ``syllabus_start`` stamp of a paragraph (#518) is an addition under
#: this version and no new one: the export and the tagger refuse an
#: object of another version (``tagger.check_approved``), and the
#: rewrite moves a row only when its key changes, so a new number
#: would refuse every stored text with no command to write it again.
#: A text without the stamp opens its syllabus by the heading's words
#: alone (``casebody.syllabus_range``).
APPROVED_SCHEMA = 1

#: Version of the join rule and of the footnote rule. It is in the
#: object and in its key: raise it when the same ensemble document
#: gives another text. Rule 4 takes the footnotes of the opinion before
#: out of the notes of a first page (#457).
JOIN_RULE = 4

#: What a join writes between two groups: a line break, never a space,
#: so the dehyphenation downstream reads the two edges as one.
JOIN = "\n"

#: The two reasons of a join.
COLUMN = "column"
PAGE = "page"

#: The kinds of block that join (condition 3).
LIST_ITEM = "list_item"
JOINABLE_KINDS = frozenset({"paragraph", LIST_ITEM})

#: A picture (#463), ``markup.FIGURE``: a paragraph of its own, never
#: joined, with no text.
FIGURE = "figure"

#: The kind of a heading, which no footnote group keeps.
HEADING = "heading"

#: The mark of one list item (``markup.ITEM``, #428), spelled here to
#: keep this module free of imports; ``test_paragraphs`` pins it.
ITEM = "li"

#: The band of the body, and the two bands of the page furniture.
BODY_BAND = "body"
FURNITURE_BANDS = ("head", "foot")

#: The two sections of the ensemble (``ensemble.BODY``,
#: ``ensemble.FOOTNOTES``), spelled here to keep this module free of
#: the Django import of ``ensemble``; ``test_paragraphs`` pins both.
BODY_SECTION = "text"
FOOTNOTE_SECTION = "footnotes"

#: The left and the right column (``ensemble._side``).
LEFT = "L"
RIGHT = "R"

#: A text that ends a sentence: end punctuation, then any closing
#: quotes or brackets, then a footnote mark an engine did not mark as
#: ``sup`` (up to three digits, or superscript digits, with no space
#: before them), then space. A citation puts a space before its number
#: ("p. 12", "§ 12"), so the mark does not take one.
_SENTENCE_END = re.compile(
    r"[.?!:;][\"'”’)\]]*(?:\d{1,3}|[\u00b9\u00b2\u00b3\u2070-\u2079]{1,3})?\s*$"
)

#: A word cut by a hyphen at the end of the text.
_HYPHEN_END = re.compile(r"[^\W\d_]-\s*$")

#: A text that starts with a lowercase letter, after any opening quotes
#: or brackets.
_LOWER_START = re.compile(r"^\s*[\"'“‘(\[]*[a-z]")

#: A footnote label at the start of a text: a number of up to three
#: digits or one to three footnote symbols, then a period, a closing
#: parenthesis or a space.
_LABEL = re.compile(r"^\s*(\d{1,3}|[*†‡§¶]{1,3})(?:[.)]\s*|\s+)")

#: A label alone, the text of a ``sup`` mark at the start of a group.
_LABEL_ALONE = re.compile(r"^\s*(\d{1,3}|[*†‡§¶]{1,3})\s*$")

#: What follows a label in a ``sup`` mark: a period or a parenthesis,
#: then space.
_AFTER_LABEL = re.compile(r"[.)]?\s*")

#: A footnote label inside a text: after the end of a sentence (or a
#: comma an engine read for its period), a number, a period or a closing
#: parenthesis, and a word that starts a sentence.
_INLINE_LABEL = re.compile(
    r"(?<=[.,;:!?\"”’')\]])\s+(\d{1,3})[.)]\s+(?=[\"“‘'(\[]*[A-Z§])"
)

#: What takes a number after it, so the number is no label: an
#: abbreviation of a citation ("ECF No. 5. The", "Fed. R. Civ. P. 13.
#: The", "Doc. 14. The"), any single capital with a period, and a number
#: and a comma, the list of a pin cite ("Id. at 12, 13. The").
_TAKES_A_NUMBER = re.compile(
    r"(?:\b(?:Nos?|nn?|pp?|fn|[Aa]rts?|[Ss]ecs?|[Cc]hs?|[Vv]ols?|[Ee]xs?"
    r"|[Dd]ocs?|[Dd]kts?|[Tt]rs?|[Aa]pps?|[Ff]igs?|[Cc]ls?|[Pp]ts?|[Tt]its?"
    r"|[Rr]ecs?|[Ee]xhs?|[A-Z])\.|\d,)$"
)

#: A label that starts a text with its period or parenthesis: the form
#: no continuation takes ("28 U.S.C." has neither).
_OWN_LABEL = re.compile(r"^\s*(\d{1,3})[.)]")

#: How far past the last number a number may go and still be a label.
#: A footnote can be missing from the opinion (on a page a redaction
#: took), so the next label is not always the last plus one; a larger
#: jump is the start of a continuation that begins with a number, such
#: as "28 U.S.C. 1291".
LABEL_STEP = 3

#: The reason of a drop the mask of a neighbour opinion took
#: (``opinion_ocr.verdict``): the page holds text of another opinion.
OUTSIDE = "outside"


def continues(before: str, after: str) -> bool:
    """Return whether the text says a sentence goes on (condition 4).

    :param before: The text of the group above the edge.
    :param after: The text of the group below it.
    :returns: True when ``before`` has no end punctuation, when it ends
        in a word cut by a hyphen, or when ``after`` starts with a
        lowercase letter.
    :rtype: bool
    """
    if not before.strip() or not after.strip():
        return False
    if _HYPHEN_END.search(before) or _LOWER_START.match(after):
        return True
    return not _SENTENCE_END.search(before)


def _flow(document: dict, section: str) -> list[dict]:
    """Return the items of one section of every page, in reading order.

    An item is ``{"page": n, "group": g}`` for a kept group,
    ``{"page": n, "dropped": True}`` for a group a redaction, a mask or
    a missing read took, and ``{"barrier": True}`` for a page nobody
    read, which no join crosses. Only the body band of the body section
    is in the flow: the furniture of a page is no part of a paragraph.

    :param document: The ensemble document.
    :param section: ``BODY_SECTION`` or ``FOOTNOTE_SECTION``.
    :returns: The items.
    :rtype: list[dict]
    """
    band = BODY_BAND if section == BODY_SECTION else FOOTNOTE_SECTION
    items: list[dict] = []
    for page in document.get("pages") or []:
        number = page.get("page_in_opinion")
        if page.get("error"):
            items.append({"barrier": True})
            continue
        # A drop sits after the kept group whose id it names, or first
        # on the page when it names none (``ensemble.build_page``).
        drops: dict = {}
        for drop in page.get("dropped") or []:
            if drop.get("section") == section and drop.get("band") == band:
                drops.setdefault(drop.get("after"), []).append(drop)
        items.extend(
            {"page": number, "dropped": True} for _ in drops.get(None, [])
        )
        for group in sorted(page.get("groups") or [], key=lambda g: g["id"]):
            if group.get("section") == section and group.get("band") == band:
                items.append({"page": number, "group": group})
            items.extend(
                {"page": number, "dropped": True}
                for _ in drops.get(group["id"], [])
            )
    return items


def _edge(before: dict, after: dict) -> str | None:
    """Return the layout edge between two items, or None (condition 1).

    The two items are next to each other in the flow, so a page change
    between them is the last item of a page before the first of the
    next. A page that holds no item of the section between them is no
    edge: the text of that page is not in this flow.

    :param before: The item above.
    :param after: The item below.
    :returns: ``COLUMN``, ``PAGE``, or None.
    """
    if before["page"] == after["page"]:
        left = before["group"].get("column")
        right = after["group"].get("column")
        return COLUMN if (left, right) == (LEFT, RIGHT) else None
    if after["page"] == before["page"] + 1:
        return PAGE
    return None


def _joins(before: dict | None, after: dict) -> str | None:
    """Return why group ``after`` joins group ``before``, or None.

    :param before: The item before it in the flow, or None.
    :param after: The item of the group.
    :returns: The edge of the join, or None when the break stays.
    """
    if before is None or "group" not in before:
        return None
    edge = _edge(before, after)
    if edge is None:
        return None
    above, below = before["group"], after["group"]
    kind = above.get("kind")
    if kind not in JOINABLE_KINDS or below.get("kind") != kind:
        return None
    if bool(above.get("blockquote")) != bool(below.get("blockquote")):
        return None
    if kind == LIST_ITEM and _starts_item(below):
        return None
    if below.get("syllabus_start"):
        return None
    if not continues(_before_mark(above), below.get("text") or ""):
        return None
    return edge


def _starts_item(group: dict) -> bool:
    """Return whether a group starts a list item at its first word."""
    return any(
        mark.get("kind") == ITEM and mark["start"] == 0
        for mark in group.get("marks") or []
    )


def _before_mark(group: dict) -> str:
    """Return the text of a group without a footnote mark at its end.

    A sentence at the foot of a column often ends in a footnote mark,
    "held so.12", with the number as a ``sup`` mark. The test of the
    end reads the text before that mark, or the digit of the mark would
    say the sentence goes on and join two paragraphs.

    :param group: The group above the edge.
    :returns: Its text, cut at the start of a ``sup`` mark that ends it.
    """
    text = group.get("text") or ""
    end = len(text.rstrip())
    for mark in group.get("marks") or []:
        if mark.get("kind") == "sup" and mark["end"] >= end > mark["start"]:
            return text[: mark["start"]]
    return text


def _paragraph(item: dict, text: str | None = None, marks=None) -> dict:
    """Return a new paragraph of one group."""
    group = item["group"]
    if text is None:
        text = group.get("text") or ""
    if marks is None:
        marks = group.get("marks") or []
    paragraph = {
        "kind": group.get("kind") or "paragraph",
        "blockquote": bool(group.get("blockquote")),
        "syllabus_start": bool(group.get("syllabus_start")),
        "text": text,
        "marks": [dict(mark) for mark in marks],
        "pages": [item["page"]],
        "page_breaks": [],
        "joins": [],
        "human": bool(group.get("human")),
    }
    if group.get("table") is not None:
        paragraph["table"] = group["table"]
    if paragraph["kind"] == LIST_ITEM:
        paragraph["list"] = group.get("list")
    if paragraph["kind"] == FIGURE:
        # The size the picture has on the page, and its box, which the
        # approval reads to find the cut and then takes away (#463).
        paragraph["figure"] = {
            **(group.get("figure") or {}),
            "box_pt": list(group.get("box_pt") or []),
        }
    return paragraph


def _append(paragraph: dict, item: dict, edge: str) -> None:
    """Put the text of one group at the end of a paragraph."""
    group = item["group"]
    offset = len(paragraph["text"]) + len(JOIN)
    paragraph["text"] += JOIN + (group.get("text") or "")
    paragraph["marks"].extend(
        {**mark, "start": mark["start"] + offset, "end": mark["end"] + offset}
        for mark in group.get("marks") or []
    )
    if paragraph["kind"] == LIST_ITEM:
        _go_on_with_item(paragraph, group, offset)
    paragraph["joins"].append({"offset": offset, "at": edge})
    if item["page"] != paragraph["pages"][-1]:
        paragraph["pages"].append(item["page"])
        paragraph["page_breaks"].append(
            {"offset": offset, "page_in_opinion": item["page"]}
        )
    paragraph["human"] = paragraph["human"] or bool(group.get("human"))


def _quote_parts(item: dict) -> list[dict]:
    """Return the items of one group, cut at its quoted span (#419).

    A group with no ``quote_span`` is one item. A group with one is up
    to three: the text before the span, the span with the blockquote
    flag, and the text after, each a copy of the group with its own
    text and its own marks. The whitespace at the edge of a part is no
    word of it, and a part with no word is left out.

    :param item: One item of the flow with a ``group``.
    :returns: The items.
    :rtype: list[dict]
    """
    group = item["group"]
    span = group.get("quote_span")
    if not span:
        return [item]
    text = group.get("text") or ""
    parts = []
    for start, end, quoted in (
        (0, span[0], False),
        (span[0], span[1], True),
        (span[1], len(text), False),
    ):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            continue
        marks = [
            {
                **mark,
                "start": max(mark["start"], start) - start,
                "end": min(mark["end"], end) - start,
            }
            for mark in group.get("marks") or []
            if min(mark["end"], end) > max(mark["start"], start)
        ]
        parts.append(
            {
                **item,
                "group": {
                    **group,
                    "text": text[start:end],
                    "marks": marks,
                    "blockquote": quoted,
                },
            }
        )
    return parts


def _go_on_with_item(paragraph: dict, group: dict, offset: int) -> None:
    """Stretch the last item of a paragraph over the words of a joined
    group that start no item (#428): a column cut the item in two.

    :param paragraph: The paragraph, with the group's text and marks in
        it already.
    :param group: The joined group.
    :param offset: Where the group's text starts in the paragraph.
    """
    text = group.get("text") or ""
    starts = [
        mark["start"]
        for mark in group.get("marks") or []
        if mark.get("kind") == ITEM
    ]
    lead = text[: min(starts)] if starts else text
    items = [
        mark
        for mark in paragraph["marks"]
        if mark.get("kind") == ITEM and mark["start"] < offset
    ]
    if items and lead.strip():
        items[-1]["end"] = offset + len(lead.rstrip())
    paragraph["list"] = paragraph.get("list") or group.get("list")


def body(document: dict) -> list[dict]:
    """Return the body text of an opinion, one entry per paragraph.

    :param document: The ensemble document.
    :returns: The paragraphs, in reading order.
    :rtype: list[dict]
    """
    paragraphs: list[dict] = []
    before = None
    for item in _flow(document, BODY_SECTION):
        if "group" not in item:
            before = item
            continue
        for place, part in enumerate(_quote_parts(item)):
            edge = _joins(before, part) if place == 0 else None
            if edge and paragraphs:
                _append(paragraphs[-1], part, edge)
            else:
                paragraphs.append(_paragraph(part))
            before = part
    return paragraphs


def split_label(group: dict) -> tuple[str, str, list[dict]] | None:
    """Return ``(label, text, marks)`` of a group that starts a footnote.

    The label is a ``sup`` mark at the start of the text, or a number or
    a symbol followed by a period, a parenthesis or a space. The label
    leaves the text, and the marks move with the cut.

    :param group: One footnote group.
    :returns: The label, the text after it and its marks, or None.
    """
    text = group.get("text") or ""
    marks = group.get("marks") or []
    cut = None
    label = None
    for mark in marks:
        if mark.get("kind") == "sup" and not text[: mark["start"]].strip():
            found = _LABEL_ALONE.match(text[mark["start"] : mark["end"]])
            if found:
                label = found.group(1)
                rest = _AFTER_LABEL.match(text, mark["end"])
                cut = rest.end() if rest else mark["end"]
            break
    if label is None:
        found = _LABEL.match(text)
        if not found:
            return None
        label, cut = found.group(1), found.end()
    kept = []
    for mark in marks:
        start, end = max(mark["start"] - cut, 0), mark["end"] - cut
        if end > start:
            kept.append({**mark, "start": start, "end": end})
    return label, text[cut:], kept


def _is_next_label(label: str, last: int | None) -> bool:
    """Return whether a number follows the last label (``LABEL_STEP``)."""
    if not label.isdigit() or last is None:
        return True
    return last < int(label) <= last + LABEL_STEP


def _starts_again(label: str, last: int | None) -> bool:
    """Return whether a number starts the numbers again (#442).

    A sub-opinion numbers its notes from 1, or from the next number a
    redaction left, so the number is at most ``LABEL_STEP`` and not
    above the last label.
    """
    return (
        label.isdigit()
        and last is not None
        and int(label) <= min(last, LABEL_STEP)
    )


def _is_marked_label(
    label: str, last: int | None, page: int, marked: dict[int, set[int]]
) -> bool:
    """Return whether a number past ``LABEL_STEP`` is still a label.

    The body is the evidence: a ``sup`` mark of that number on the page
    of the group or on the page before it (a mark in the last line of a
    page sends its note to the next). Without it one label the engines
    missed ends the series, because every label after it is more than
    ``LABEL_STEP`` past the last one read: notes 3 to 7 read as two
    blocks left the last label at 3, and notes 8 to 29 went into note 3.
    A continuation that starts with a number ("28 U.S.C. 1291") is a
    label only where the body cites a note of that number there.

    :param label: The label :func:`split_label` read.
    :param last: The last number that started a note.
    :param page: The page of the group.
    :param marked: :func:`mark_pages`.
    """
    if not label.isdigit() or last is None or int(label) <= last:
        return False
    return bool(marked.get(int(label), set()) & {page, page - 1})


def inline_labels(
    text: str,
    start: int,
    last: int | None,
    page: int,
    marked: dict[int, set[int]],
    taken: frozenset[int] = frozenset(),
) -> list[int]:
    """Return the offsets at which a footnote starts inside a text.

    The engines read several short notes of a page as one block, cut at
    the column edge and not at the notes, so a label is in the middle of
    a group ("... shortly before. 4. In both ..."). It starts a note
    only when all of this holds: it follows the end of a sentence, it is
    the next number of the series and no other (the last label plus
    one), the body cites that number on the page of the group or the
    page before, nothing that takes a number is before it
    (``_TAKES_A_NUMBER``), and no later group of the page or the next
    starts with that number. The body is the proof, as in
    :func:`_is_marked_label`: a sentence that ends in a number and a
    period is no note without its mark.

    A wrong cut costs more than a missed one. A missed label leaves a
    note inside the note before it, with its text. A number of a
    citation taken for a label ("See App. 10, 14. The witness") makes a
    note of the rest of the sentence, and the real note of that number,
    a group of its own further down, then reads as a step back and goes
    into it. So the step is one, and a number a later group owns is
    that group's.

    :param text: The text of one footnote group.
    :param start: Where the search starts: after the label of the group.
    :param last: The last number that started a note.
    :param page: The page of the group.
    :param marked: :func:`mark_pages`.
    :param taken: The numbers that start a later group of this page or
        the next (:func:`_own_labels`).
    :returns: The offset of each label, in order.
    :rtype: list[int]
    """
    cuts: list[int] = []
    if last is None:
        return cuts
    for found in _INLINE_LABEL.finditer(text, start):
        number = int(found.group(1))
        if number != last + 1 or number in taken:
            continue
        if not marked.get(number, set()) & {page, page - 1}:
            continue
        if _TAKES_A_NUMBER.search(text[: found.start()]):
            continue
        cuts.append(found.start(1))
        last = number
    return cuts


def _cut(item: dict, cuts: list[int]) -> list[dict]:
    """Return the items of one group cut at the labels inside it.

    Each part is the group with its share of the text and of the marks,
    so a part is joined and written by the rules of a whole group.

    :param item: The item of the group.
    :param cuts: :func:`inline_labels`.
    :returns: One item per part, in order.
    :rtype: list[dict]
    """
    group = item["group"]
    text = group.get("text") or ""
    parts = []
    for start, end in zip([0, *cuts], [*cuts, len(text)], strict=True):
        share = text[start:end].rstrip()
        stop = start + len(share)
        marks = [
            {
                **mark,
                "start": max(mark["start"], start) - start,
                "end": min(mark["end"], stop) - start,
            }
            for mark in group.get("marks") or []
            if min(mark["end"], stop) > max(mark["start"], start)
        ]
        parts.append(
            {**item, "group": {**group, "text": share, "marks": marks}}
        )
    return parts


def _number_marks(document: dict) -> list[tuple[int, int]]:
    """Return ``(number, page)`` of each numbered mark of the body.

    A mark is a ``sup`` mark whose text is a number alone, in the order
    of the body flow; the page is its ``page_in_opinion``.

    :param document: The ensemble document.
    :rtype: list[tuple[int, int]]
    """
    found = []
    for item in _flow(document, BODY_SECTION):
        group = item.get("group")
        if group is None:
            continue
        text = group.get("text") or ""
        for mark in sorted(
            (m for m in group.get("marks") or [] if m.get("kind") == "sup"),
            key=lambda m: m["start"],
        ):
            label = _LABEL_ALONE.match(text[mark["start"] : mark["end"]])
            if label and label.group(1).isdigit():
                found.append((int(label.group(1)), item["page"]))
    return found


def mark_pages(document: dict) -> dict[int, set[int]]:
    """Return the pages on which the body cites each footnote number.

    :param document: The ensemble document.
    :returns: ``{number: the pages of its marks}``.
    :rtype: dict[int, set[int]]
    """
    pages: dict[int, set[int]] = {}
    for number, page in _number_marks(document):
        pages.setdefault(number, set()).add(page)
    return pages


def mark_restarts(document: dict) -> list[tuple[str, int]]:
    """Return where the footnote marks of the body start again.

    A mark is a ``sup`` mark whose text is a number alone, in the order
    of the body flow. It starts the numbers again when it passes
    :func:`_starts_again` over the mark before it: the first note of a
    concurrence or a dissent that numbers its notes from 1 (#442), or
    from 2 where a redaction took note 1. An engine that marked no
    ``sup`` gives no restart, and the footnotes keep the rule of one
    series.

    :param document: The ensemble document.
    :returns: ``(label, page)`` of each restart, in reading order: the
        page is the ``page_in_opinion`` of the mark.
    :rtype: list[tuple[str, int]]
    """
    restarts = []
    last = None
    for number, page in _number_marks(document):
        if _starts_again(str(number), last):
            restarts.append((str(number), page))
        last = number
    return restarts


def _plain_note(item: dict) -> dict:
    """Return a footnote item as the paragraph it is.

    An engine that takes the first line of a note for a section heading
    ("21. Specifically, Giraud II ruled as follows:") labels it one and
    marks it bold, and the marks of a group are a union, so one engine
    is enough. A footnote holds no heading: the kind is a paragraph,
    and a ``strong`` mark over the whole group goes. A mark over a part
    of it stays, because the print has bold there: the label of a note,
    and the rule a note names ("3. ORPC 1.1 provides:").

    A list label with no item is no list either: one engine labels a
    numbered note a list item, and a footnote keeps no ``li`` mark. The
    half of a note in the next column is a paragraph, and two kinds
    never join, so the note broke in the middle of a word.

    :param item: The item of a footnote group.
    :returns: The item, or a copy with the kind and the marks changed.
    :rtype: dict
    """
    group = item["group"]
    end = len((group.get("text") or "").rstrip())
    marks = [
        mark
        for mark in group.get("marks") or []
        if not (
            mark.get("kind") == "strong"
            and mark["start"] == 0
            and mark["end"] >= end
        )
    ]
    kind = group.get("kind")
    if kind == HEADING or (
        kind == LIST_ITEM and not any(m.get("kind") == ITEM for m in marks)
    ):
        kind = "paragraph"
    if kind == group.get("kind") and len(marks) == len(
        group.get("marks") or []
    ):
        return item
    return {**item, "group": {**group, "kind": kind, "marks": marks}}


def _opening_mark(document: dict) -> tuple[int, int] | None:
    """Return ``(number, page)`` of the first numbered mark of the body.

    The first page of an opinion that starts in the middle of a page can
    hold the last footnotes of the opinion before it. Their labels are
    the end of another series ("26."), so the opinion's own note 1 reads
    as a step back and went into that note, with every note up to the
    foreign label. The body says where its own series starts: its first
    mark (:func:`_opens`).

    :param document: The ensemble document.
    :returns: The mark, or None when the body has none.
    """
    marks = _number_marks(document)
    return marks[0] if marks else None


def _is_foreign(
    last: int | None, last_page: int | None, marked: dict[int, set[int]]
) -> bool:
    """Return whether the last note is one the body does not cite.

    A note of the opinion has its mark on its page or the page before.
    The last note of the opinion before has none there: its number is
    cited further on, if at all.

    :param last: The last number that started a note.
    :param last_page: The page of that note.
    :param marked: :func:`mark_pages`.
    """
    if last is None or last_page is None:
        return False
    return not marked.get(last, set()) & {last_page, last_page - 1}


def _opens(
    split: tuple | None,
    item: dict,
    last: int | None,
    opening: tuple[int, int] | None,
) -> bool:
    """Return whether a group's label is the first note of the opinion,
    after the notes of the opinion before it.

    The label is the number of the body's first mark, on the page of
    that mark or the next, and a step back from the last label. The
    caller passes ``opening`` only while the last note is foreign
    (:func:`_is_foreign`) and the first note has not started, so a
    continuation that starts with a small number after one of the
    opinion's own notes stays text.

    :param split: :func:`split_label` of the group.
    :param item: The item of the group.
    :param last: The last number that started a note.
    :param opening: :func:`_opening_mark`, or None.
    """
    return (
        split is not None
        and opening is not None
        and split[0].isdigit()
        and int(split[0]) == opening[0]
        and opening[1] <= item["page"] <= opening[1] + 1
        and _starts_again(split[0], last)
    )


def _own_labels(flow: list[dict]) -> list[tuple[int, int, int]]:
    """Return ``(index in the flow, number, page)`` of each group that
    starts with a label of its own: a number with its period or its
    parenthesis, or a ``sup`` mark. :func:`inline_labels` leaves such a
    number to its group."""
    found = []
    for index, item in enumerate(flow):
        group = item.get("group")
        if group is None:
            continue
        text = group.get("text") or ""
        own = _OWN_LABEL.match(text)
        if own:
            found.append((index, int(own.group(1)), item["page"]))
            continue
        split = split_label(group)
        if (
            split
            and split[0].isdigit()
            and any(
                mark.get("kind") == "sup" and not text[: mark["start"]].strip()
                for mark in group.get("marks") or []
            )
        ):
            found.append((index, int(split[0]), item["page"]))
    return found


def _restarts_here(
    split: tuple | None,
    item: dict,
    last: int | None,
    restarts: list[tuple[str, int]],
) -> bool:
    """Return whether a group's label is the next restart of the marks."""
    return (
        split is not None
        and bool(restarts)
        and split[0].isdigit()
        and str(int(split[0])) == restarts[0][0]
        and item["page"] >= restarts[0][1]
        and _starts_again(split[0], last)
    )


def _labelled_parts(
    item: dict,
    last: int | None,
    restarts: list[tuple[str, int]],
    marked: dict[int, set[int]],
    opening: tuple[int, int] | None,
    taken: frozenset[int],
) -> list[dict]:
    """Return the items of one footnote group, cut at its inner labels.

    The series the inner labels go on from is the one the start of the
    group leaves: its own label when that label starts a note, else the
    last label before it. Every part after the first starts with a
    label :func:`inline_labels` proved.

    :param item: The item of the group.
    :param last: The last number that started a note.
    :param restarts: The restarts not taken yet.
    :param marked: :func:`mark_pages`.
    :param opening: :func:`_opening_mark`, while :func:`_opens` may
        take it.
    :param taken: The numbers a later group of the page or the next
        starts with.
    :rtype: list[dict]
    """
    group = item["group"]
    if group.get("kind") in (FIGURE, "table"):
        return [item]
    text = group.get("text") or ""
    split = split_label(group)
    start = 0
    if (
        split
        and split[0].isdigit()
        and (
            _restarts_here(split, item, last, restarts)
            or _opens(split, item, last, opening)
            or _is_next_label(split[0], last)
            or _is_marked_label(split[0], last, item["page"], marked)
        )
    ):
        last = int(split[0])
        start = len(text) - len(split[1])
    cuts = inline_labels(text, start, last, item["page"], marked, taken)
    return _cut(item, cuts) if cuts else [item]


def _walk(document: dict, flow: list[dict]) -> tuple[list, list, int | None]:
    """Return the footnotes of a flow, a list keyed by label.

    A group that starts with a label starts a footnote. A group with no
    label goes on with the footnote before it: the first footnote group
    of a page with no label is the rest of the last footnote of the
    page before, the rule of the legacy pipeline. A label that starts
    the numbers again (:func:`_starts_again`) starts a footnote only
    where it is the next restart label of the body marks
    (:func:`mark_restarts`), on the page of that mark or later, so the
    first note of a dissent numbered from 1 is a note and not the rest
    of the last note of the majority, and a continued note that starts
    with a small number ("3 Am. Jur. 2d", "1 U.S.C. 1") stays text. A
    number more than ``LABEL_STEP`` past the last label starts a
    footnote only where the body cites it (:func:`_is_marked_label`),
    so one label the engines missed does not end the series, and a
    label inside a group cuts the group there (:func:`inline_labels`),
    so the notes the engines read as one block are notes. No group
    of a footnote is a heading (:func:`_plain_note`), and the first mark of
    the body starts the opinion's own series after the notes of the
    opinion before it (:func:`_opening_mark`). A group
    with no label is joined to the
    last paragraph of that footnote by the rule of :func:`body`, or it
    is a paragraph of its own. A group before any label has no footnote
    to go on with, so it is a footnote with ``label`` None, and no text
    goes away.

    :param document: The ensemble document.
    :param flow: The footnote flow, or its end (:func:`footnotes`).
    :returns: The notes ``[{"label", "pages", "paragraphs"}]`` in
        reading order, the index in the flow at which each starts, and
        the index of the note :func:`_opens` took, or None.
    """
    notes: list[dict] = []
    starts: list[int] = []
    opened = None
    last_number = None
    before = None
    restarts = mark_restarts(document)
    marked = mark_pages(document)
    opening = _opening_mark(document)
    own = _own_labels(flow)
    last_page = None
    for index, whole in enumerate(flow):
        if "group" not in whole:
            before = whole
            continue
        whole = _plain_note(whole)
        taken = frozenset(
            number
            for at, number, page in own
            if at > index and page in (whole["page"], whole["page"] + 1)
        )
        for at, item in enumerate(
            _labelled_parts(
                whole,
                last_number,
                restarts,
                marked,
                opening
                if _is_foreign(last_number, last_page, marked)
                else None,
                taken,
            )
        ):
            split = split_label(item["group"])
            # A note starts on the page of its mark, so a group on a
            # page before the restart mark is no restart: it is the rest
            # of a note that starts with the same number ("1 U.S.C. 1").
            again = _restarts_here(split, item, last_number, restarts)
            opens = _opens(
                split,
                item,
                last_number,
                opening
                if _is_foreign(last_number, last_page, marked)
                else None,
            )
            if split and (
                at > 0
                or again
                or opens
                or _is_next_label(split[0], last_number)
                or _is_marked_label(
                    split[0], last_number, item["page"], marked
                )
            ):
                if again:
                    restarts.pop(0)
                if opens and at == 0 and opened is None:
                    opened = len(notes)
                label, text, marks = split
                if label.isdigit():
                    last_number = int(label)
                    last_page = item["page"]
                    if (
                        opening
                        and last_number == opening[0]
                        and item["page"] >= opening[1]
                    ):
                        # The first note of the opinion has started.
                        opening = None
                starts.append(index)
                notes.append(
                    {
                        "label": label,
                        "pages": [item["page"]],
                        "paragraphs": [_paragraph(item, text, marks)],
                    }
                )
            elif not notes:
                starts.append(index)
                notes.append(
                    {
                        "label": None,
                        "pages": [item["page"]],
                        "paragraphs": [_paragraph(item)],
                    }
                )
            else:
                note = notes[-1]
                edge = _joins(before, item)
                if edge:
                    _append(note["paragraphs"][-1], item, edge)
                else:
                    note["paragraphs"].append(_paragraph(item))
                if item["page"] != note["pages"][-1]:
                    note["pages"].append(item["page"])
            before = item
    return notes, starts, opened


def _first_page(document: dict) -> tuple[int | None, bool]:
    """Return the first page of an opinion, and whether a neighbour
    opinion has text on it (a drop its mask took)."""
    pages = document.get("pages") or []
    if not pages:
        return None, False
    shared = any(
        entry.get("reason") == OUTSIDE
        for entry in pages[0].get("dropped") or []
    )
    return pages[0].get("page_in_opinion"), shared


def _foreign(
    notes: list[dict],
    opened: int | None,
    document: dict,
) -> int:
    """Return how many notes at the start are the opinion before's (#457).

    An opinion that starts in the middle of a page has the last
    footnotes of the opinion before at the foot of its first page. They
    are the notes of that page alone, before the opinion's first own
    note, with a number the body does not cite on that page, and one of
    two things proves them: the opinion's own series opens after them
    with a step back to the body's first mark (:func:`_opens`), or the
    number is past ``LABEL_STEP``, which no first note of an opinion
    is, on a page that holds text of a neighbour opinion. A note with
    no number (the rest of a note of the page before, a symbol) goes
    only with a numbered one after it.

    A wrong take loses a note, so every doubt keeps it: a small number
    with no step back after it may be the opinion's own note 1 whose
    mark no engine read, and a note that goes on to the next page is no
    note of the opinion before.

    :param notes: The notes of the whole flow (:func:`_walk`).
    :param opened: The index of the note :func:`_opens` took, or None.
    :param document: The ensemble document.
    :returns: The count of the notes that are not the opinion's.
    :rtype: int
    """
    first, shared = _first_page(document)
    cited = mark_pages(document)
    count = 0
    for index, note in enumerate(notes if opened is None else notes[:opened]):
        if note["pages"] != [first]:
            break
        label = note["label"]
        if label is None or not label.isdigit():
            continue
        if first in cited.get(int(label), set()):
            break
        if opened is None and not (shared and int(label) > LABEL_STEP):
            break
        count = index + 1
    return count


def _split_notes(document: dict) -> tuple[list[dict], list[dict]]:
    """Return the opinion's footnotes, and the ones of the opinion
    before that its first page holds (:func:`_foreign`).

    The opinion's own are the notes of the flow after the foreign ones,
    walked again: with no series before it, the first own label starts
    one by itself.
    """
    flow = _flow(document, FOOTNOTE_SECTION)
    notes, starts, opened = _walk(document, flow)
    count = _foreign(notes, opened, document)
    # Two notes cut from one group are one piece of the flow: the cut
    # is between groups, so the group stays whole with the opinion.
    while 0 < count < len(notes) and starts[count] == starts[count - 1]:
        count -= 1
    if not count:
        return notes, []
    cut = starts[count] if count < len(notes) else len(flow)
    return _walk(document, flow[cut:])[0], notes[:count]


def footnotes(document: dict) -> list[dict]:
    """Return the footnotes of an opinion, a list keyed by label.

    The rules of a label are :func:`_walk`'s. The last footnotes of the
    opinion before, at the foot of a first page it ends on, are no
    footnotes of this opinion (:func:`_foreign`, join rule 4, #457).

    :param document: The ensemble document.
    :returns: ``[{"label", "pages", "paragraphs"}]``, in reading order.
    :rtype: list[dict]
    """
    return _split_notes(document)[0]


def foreign_footnotes(document: dict) -> list[dict]:
    """Return the footnotes the first page holds of the opinion before.

    They leave the footnotes and stay in the approved object as plain
    text, so no text of the document goes away and a wrong take can be
    read there. No geometry and no mark: nothing builds from them.

    :param document: The ensemble document.
    :returns: ``[{"label", "pages", "text"}]``, in reading order.
    :rtype: list[dict]
    """
    return [
        {
            "label": note["label"],
            "pages": list(note["pages"]),
            "text": "\n".join(
                paragraph.get("text") or "" for paragraph in note["paragraphs"]
            ),
        }
        for note in _split_notes(document)[1]
    ]


def page_table(document: dict, printed: dict[int, str]) -> list[dict]:
    """Return the page table of the approved object.

    The order, the address and the printed number of every page, plus
    its furniture (the running head and foot the ensemble kept), so no
    text of the document goes away.

    :param document: The ensemble document.
    :param printed: ``{page_index: printed number}`` of the run.
    :returns: One entry per page.
    :rtype: list[dict]
    """
    pages = []
    for page in document.get("pages") or []:
        index = page.get("page_index")
        pages.append(
            {
                "page_in_opinion": page.get("page_in_opinion"),
                "page_index": index,
                "printed": printed.get(index)
                if isinstance(index, int)
                else None,
                "error": page.get("error") or None,
                "furniture": [
                    {"band": group["band"], "text": group.get("text") or ""}
                    for group in sorted(
                        page.get("groups") or [], key=lambda g: g["id"]
                    )
                    if group.get("section") == BODY_SECTION
                    and group.get("band") in FURNITURE_BANDS
                ],
            }
        )
    return pages


def approved_document(
    document: dict,
    printed: dict[int, str],
    approved_by: str,
    approved_at: str,
) -> dict:
    """Return the approved object of one opinion.

    :param document: The stamped ensemble document the curator approved.
    :param printed: ``{page_index: printed number}`` of the run.
    :param approved_by: The username of the curator.
    :param approved_at: The time of the approval, ISO 8601.
    :returns: The object.
    :rtype: dict
    """
    return {
        "schema": APPROVED_SCHEMA,
        "join_rule": JOIN_RULE,
        "opinion": {
            **(document.get("opinion") or {}),
            "scan": document.get("scan_pk"),
            "edit_revision": document.get("edit_revision", 0),
        },
        "approved_by": approved_by,
        "approved_at": approved_at,
        "engines": list(document.get("engines") or []),
        "pages": page_table(document, printed),
        "body": body(document),
        "footnotes": footnotes(document),
        "foreign_footnotes": foreign_footnotes(document),
    }
