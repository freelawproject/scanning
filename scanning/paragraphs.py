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
   flag, and only the first part can join the group above it.
4. The text says the sentence goes on (:func:`continues`).

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
or joined to the last one by the same rule.

Everything here reads the ensemble document alone and imports no
Django module (``test_paragraphs`` pins that), so a better rule is a
new :data:`JOIN_RULE` and ``rewrite_approved_text``, never a new
approval.
"""

import re

#: Version of the approved object this module writes.
APPROVED_SCHEMA = 1

#: Version of the join rule and of the footnote rule. It is in the
#: object and in its key: raise it when the same ensemble document
#: gives another text.
JOIN_RULE = 1

#: What a join writes between two groups: a line break, never a space,
#: so the dehyphenation downstream reads the two edges as one.
JOIN = "\n"

#: The two reasons of a join.
COLUMN = "column"
PAGE = "page"

#: The kinds of block that join (condition 3).
JOINABLE_KINDS = frozenset({"paragraph", "list_item"})

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

#: How far past the last number a number may go and still be a label.
#: A footnote can be missing from the opinion (on a page a redaction
#: took), so the next label is not always the last plus one; a larger
#: jump is the start of a continuation that begins with a number, such
#: as "28 U.S.C. 1291".
LABEL_STEP = 3


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
    if not continues(_before_mark(above), below.get("text") or ""):
        return None
    return edge


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
        "text": text,
        "marks": [dict(mark) for mark in marks],
        "pages": [item["page"]],
        "page_breaks": [],
        "joins": [],
        "human": bool(group.get("human")),
    }
    if group.get("table") is not None:
        paragraph["table"] = group["table"]
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


def footnotes(document: dict) -> list[dict]:
    """Return the footnotes of an opinion, a list keyed by label.

    A group that starts with a label starts a footnote. A group with no
    label goes on with the footnote before it: the first footnote group
    of a page with no label is the rest of the last footnote of the
    page before, the rule of the legacy pipeline. It is joined to the
    last paragraph of that footnote by the rule of :func:`body`, or it
    is a paragraph of its own. A group before any label has no footnote
    to go on with, so it is a footnote with ``label`` None, and no text
    goes away.

    :param document: The ensemble document.
    :returns: ``[{"label", "pages", "paragraphs"}]``, in reading order.
    :rtype: list[dict]
    """
    notes: list[dict] = []
    last_number = None
    before = None
    for item in _flow(document, FOOTNOTE_SECTION):
        if "group" not in item:
            before = item
            continue
        split = split_label(item["group"])
        if split and _is_next_label(split[0], last_number):
            label, text, marks = split
            if label.isdigit():
                last_number = int(label)
            notes.append(
                {
                    "label": label,
                    "pages": [item["page"]],
                    "paragraphs": [_paragraph(item, text, marks)],
                }
            )
        elif not notes:
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
    return notes


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
    }
