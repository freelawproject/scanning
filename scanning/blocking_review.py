"""The blocking review: every open blocking card, one after the next.

A volume of 48 opinions leaves a few dozen words no two engines agree
on, spread over twenty review pages. The review page shows them one
opinion at a time, beside everything else on the page. This module
turns them into cards a curator answers in a row: the scan around the
word, the sentence it sits in, what each engine read there, and a row
to type what the page says. The page that shows them
(``views.opinion_blocking_review``) writes nothing of its own: a choice
is a text edit of the block through ``views_api.edit_opinion_text``, a
"looks right" is the dismissal of the card, and the approval is the
approval, so the review page and this one hold one set of rules.

**The crop is an estimate.** No engine gives a box below the block, so
the line of a word is read off its place in the block's text: a block
of so many points holds so many lines, and the word's characters are
so far into it (:func:`crop_of`). The highlight is widened to cover
the guess, and the card says so.
"""

from __future__ import annotations

from scanning import ensemble, markup

#: The height of one printed line, in points: the estimate every crop
#: is cut by. 9-point type on 11-point leading is the body of a
#: reporter volume; a footnote's lines are shorter and the guess lands
#: a line early at the bottom of a long one, inside the crop still.
LINE_PT = 11.0

#: Lines of context above and below the estimated line of the word.
CONTEXT_LINES = 1

#: How far the crop reaches past the block on each side, in points.
CROP_PAD_PT = 6.0

#: How far the highlight is widened on each side of the guess, as a
#: share of the line.
HIGHLIGHT_SLACK = 0.08

#: Words of the block shown on each side of the open word.
CONTEXT_WORDS = 3

#: What a card offers (``kind``). A ``word`` card is one word of a
#: voted block no two engines agree on, answered by a text edit. A
#: ``block`` card is a voted block taken whole: a table, which no text
#: edit takes, or a block most of whose words are open, which is not
#: a word to decide but a reading to pick; it shows each engine's
#: whole reading. A ``single`` card is a block one engine alone read,
#: answered by a dismissal or a text edit. A ``link`` card is a
#: blocking finding with no answer here: the document is the place
#: for it.
WORD = "word"
BLOCK = "block"
SINGLE = "single"
LINK = "link"

#: A voted block with more open words than this is one ``block`` card
#: and not a card per word: 71 words of a table Mistral read as noise
#: are one bad reading, not 71 decisions.
MANY_OPEN_WORDS = 8

#: What an engine's reading says when it read nothing at the word.
READ_NOTHING = ""


def _present(group: dict) -> list[str]:
    """Return the engines that read the group, ranked."""
    engines = group["engines"]
    return [
        name
        for name in ensemble._ranked(engines)
        if ensemble.compare_text(engines[name]["text"])
    ]


def _reading(
    at: dict, inserted: dict, position: int, run_index: int | None
) -> str:
    """Return one other engine's word at a base position.

    :param at: ``_candidates``'s first answer for that engine.
    :param inserted: Its second answer.
    :param position: The base position of the token.
    :param run_index: For a token of a run the base did not read, the
        token's place in that run; None for a word the base read. A
        run of two words is two cards, and each shows its own word of
        the engine's run, never the run whole: the card's answer goes
        into one word's place of the text.
    :returns: The word as the engine wrote it, or :data:`READ_NOTHING`.
    :rtype: str
    """
    if run_index is not None:
        runs = inserted.get(position) or []
        words = [word for _, word in runs[0]] if runs else []
        return words[run_index] if run_index < len(words) else READ_NOTHING
    entries = at.get(position) or []
    if not entries:
        return READ_NOTHING
    entry = entries[0]
    if isinstance(entry, ensemble.Carried):
        # The engine's reading of this word is in the span it joined
        # at the head of the run (#391): show the span.
        head = at.get(entry.head) or []
        return head[0][1] if head else READ_NOTHING
    return entry[1]


def _span_ends(at: dict) -> dict[int, int]:
    """Return ``{head: last position}`` of every span one engine joined.

    ``_candidates`` records an engine's reading of several base words
    as one at the first of them and marks the rest ``Carried`` (#391).

    :param at: ``_candidates``'s first answer for one engine.
    :returns: The last base position of each span, by its head.
    :rtype: dict[int, int]
    """
    ends: dict[int, int] = {}
    for position, entries in at.items():
        for entry in entries:
            if isinstance(entry, ensemble.Carried):
                ends[entry.head] = max(
                    ends.get(entry.head, entry.head), position
                )
    return ends


def _span_at(at: dict, ends: dict[int, int], position: int) -> tuple[int, int]:
    """Return the base positions one engine's reading at ``position``
    covers: the span it joined, or the position alone."""
    for entry in at.get(position) or []:
        if isinstance(entry, ensemble.Carried):
            return entry.head, ends[entry.head]
    if position in ends:
        return position, ends[position]
    return position, position


def _span_reading(at: dict, first: int, last: int) -> str:
    """Return one engine's words over the base positions ``first`` to
    ``last``: a joined span once, at its head, and a word per position
    elsewhere."""
    words = []
    for position in range(first, last + 1):
        for entry in at.get(position) or []:
            if isinstance(entry, ensemble.Carried):
                continue
            if entry[1]:
                words.append(entry[1])
    return " ".join(words)


def open_words(group: dict) -> list[dict]:
    """Return the places of one voted group no two engines agree on.

    The vote is run again over the group's own readings, the inputs
    the build had, so the tokens are the ones the document holds and
    each carries the base position it answers for. Each other engine's
    word at that position comes from the same alignment the vote used
    (``ensemble._candidates``), so the rows of the card are the votes
    that were cast.

    **A card covers the span the engines joined.** dots.mocr writes
    ``Ill. Adm. Code`` as three words and the others as one, so the
    vote aligns them at the first word and the other engines' reading
    there is the whole span. A card per word would put that span into
    one word's place of the text, and the row for the one engine that
    split the span would show a word while the others show three. So
    every open position is widened to the span any engine joined over
    it, the spans that touch are one card, and each row is that
    engine's words over the whole span: the answer replaces the span.
    A word of a run the base did not read is a card of its own.

    :param group: One group of a stamped ensemble document.
    :returns: One entry per open place: ``{start, length, token,
        before, after, readings: [{engine, word}]}``, with ``start`` an
        offset into the group's ``text`` and ``token`` the text there.
    :rtype: list[dict]
    """
    if group.get("agreement") != ensemble.VOTED:
        return []
    present = _present(group)
    if len(present) < 2:
        return []
    engines = group["engines"]
    base = present[0]
    base_pairs = ensemble._pairs(engines[base]["text"])
    others = [
        (name, ensemble._pairs(engines[name]["text"])) for name in present[1:]
    ]
    tokens, _ = ensemble.vote_words(base_pairs, [pairs for _, pairs in others])
    candidates = {
        name: ensemble._candidates(base_pairs, pairs) for name, pairs in others
    }
    ends = {name: _span_ends(at) for name, (at, _) in candidates.items()}
    shown = [token["text"] for token in tokens if token["text"]]

    # Every token with its offset, its index among the shown words and
    # its place in an inserted run.
    placed: list[dict] = []
    offset = 0
    run_index: int | None = None
    for token in tokens:
        if not token["text"]:
            continue
        if token.get("inserted"):
            run_index = 0 if run_index is None else run_index + 1
        else:
            run_index = None
        placed.append(
            {
                "token": token,
                "start": offset,
                "end": offset + len(token["text"]),
                "index": len(placed),
                "run_index": run_index,
            }
        )
        offset += len(token["text"]) + 1

    def context(first_index: int, last_index: int) -> tuple[str, str]:
        return (
            " ".join(shown[max(0, first_index - CONTEXT_WORDS) : first_index]),
            " ".join(shown[last_index + 1 : last_index + 1 + CONTEXT_WORDS]),
        )

    words: list[dict] = []
    # The open positions the base read, widened to the spans the
    # engines joined over them, then merged where they touch.
    spans: list[list[int]] = []
    for entry in placed:
        token = entry["token"]
        if not token.get("low_confidence") or entry["run_index"] is not None:
            continue
        first = last = token["position"]
        for name, (at, _) in candidates.items():
            lo, hi = _span_at(at, ends[name], token["position"])
            first, last = min(first, lo), max(last, hi)
        # Spans that overlap are one card; two open words that merely
        # stand side by side are two decisions.
        if spans and first <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], last)
        else:
            spans.append([first, last])
    for first, last in spans:
        covered = [
            e
            for e in placed
            if e["run_index"] is None
            and first <= e["token"]["position"] <= last
        ]
        if not covered:
            continue
        before, after = context(covered[0]["index"], covered[-1]["index"])
        readings = [
            {
                "engine": base,
                "word": " ".join(
                    base_pairs[p][1]
                    for p in range(first, last + 1)
                    if p < len(base_pairs)
                ),
            }
        ]
        for name, (at, _) in candidates.items():
            readings.append(
                {"engine": name, "word": _span_reading(at, first, last)}
            )
        words.append(
            {
                "start": covered[0]["start"],
                "length": covered[-1]["end"] - covered[0]["start"],
                "token": " ".join(e["token"]["text"] for e in covered),
                "before": before,
                "after": after,
                "readings": readings,
            }
        )
    # The words of a run the base did not read, one card each.
    for entry in placed:
        token = entry["token"]
        if not token.get("low_confidence") or entry["run_index"] is None:
            continue
        position = token["position"]
        before, after = context(entry["index"], entry["index"])
        readings = [{"engine": base, "word": READ_NOTHING}]
        for name, (at, inserted) in candidates.items():
            readings.append(
                {
                    "engine": name,
                    "word": _reading(
                        at, inserted, position, entry["run_index"]
                    ),
                }
            )
        words.append(
            {
                "start": entry["start"],
                "length": entry["end"] - entry["start"],
                "token": token["text"],
                "before": before,
                "after": after,
                "readings": readings,
            }
        )
    words.sort(key=lambda w: w["start"])
    return words


def crop_of(
    box: list[float], text_length: int, start: int, length: int
) -> dict:
    """Return where to look for a word of a block, as an estimate.

    The block holds ``height / LINE_PT`` lines, each of the same share
    of its characters, so the word's offset says its line and its place
    on it. The crop is that line with :data:`CONTEXT_LINES` on each
    side, the width of the block plus :data:`CROP_PAD_PT`; the
    highlight is the word's span on the line widened by
    :data:`HIGHLIGHT_SLACK`, and the whole rest of the line when the
    word wraps.

    :param box: The block's ``[x0, y0, x1, y1]`` in points.
    :param text_length: The length of the block's text.
    :param start: The word's offset into it.
    :param length: The word's length.
    :returns: ``{crop, highlight, line, lines}``, the boxes in points
        and the line 1-based.
    :rtype: dict
    """
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    lines = max(1, round(height / LINE_PT))
    per_line = max(1.0, text_length / lines)
    line = min(lines - 1, int(start // per_line))
    last = min(lines - 1, int((start + max(length, 1) - 1) // per_line))
    line_height = height / lines
    x_start = (start - line * per_line) / per_line
    x_end = (
        1.0 if last != line else (start + length - line * per_line) / per_line
    )
    x_start = max(0.0, x_start - HIGHLIGHT_SLACK)
    x_end = min(1.0, x_end + HIGHLIGHT_SLACK)
    return {
        "crop": [
            round(x0 - CROP_PAD_PT, 2),
            round(y0 + max(0, line - CONTEXT_LINES) * line_height, 2),
            round(x1 + CROP_PAD_PT, 2),
            round(y0 + min(lines, last + 1 + CONTEXT_LINES) * line_height, 2),
        ],
        "highlight": [
            round(x0 + width * x_start, 2),
            round(y0 + line * line_height, 2),
            round(x0 + width * x_end, 2),
            round(y0 + (last + 1) * line_height, 2),
        ],
        "line": line + 1,
        "lines": lines,
    }


def block_crop(box: list[float]) -> dict:
    """Return the crop of a whole block, with no word to point at."""
    x0, y0, x1, y1 = box
    return {
        "crop": [
            round(x0 - CROP_PAD_PT, 2),
            y0,
            round(x1 + CROP_PAD_PT, 2),
            y1,
        ],
        "highlight": None,
        "line": None,
        "lines": max(1, round((y1 - y0) / LINE_PT)),
    }


def _place(page: dict, group: dict) -> str:
    """Return where on the page a block sits, for the card's header."""
    section = (
        "footnote" if group.get("section") == ensemble.FOOTNOTES else "body"
    )
    column = group.get("column")
    where = {"L": "left column", "R": "right column"}.get(column or "", "")
    return f"page {page['page_in_opinion'] + 1}, {section}" + (
        f", {where}" if where else ""
    )


def cards(document: dict, findings: list) -> list[dict]:
    """Return the cards of one opinion, in the order of the pages.

    One ``word`` card per open word of a voted block that blocks, or
    one ``block`` card for the whole of a table or of a block with more
    than :data:`MANY_OPEN_WORDS` open; one ``single`` card per block one
    engine alone read; and one ``link`` card per open blocking finding
    of another check. The findings are
    the open blocking rows of the opinion (``opinion_review``'s rule).
    A ``word`` card carries the ``NO_MAJORITY`` card of its page and a
    ``single`` card the ``SINGLE_ENGINE`` card of its page, because
    each counts every such block of the page and the dismissal answers
    them together: the page keeps a word that is right as shown until
    every open word of the page is answered, and dismisses then.

    :param document: The stamped ensemble document.
    :param findings: The open blocking ``OpinionFinding`` rows.
    :returns: The cards.
    :rtype: list[dict]
    """
    from scanning.models import OpinionCheck

    by_page: dict[int, list] = {}
    for finding in findings:
        by_page.setdefault(finding.page_in_opinion, []).append(finding)
    out: list[dict] = []
    for page in document.get("pages") or []:
        number = page["page_in_opinion"]
        page_findings = by_page.pop(number, [])
        single_card = next(
            (
                f
                for f in page_findings
                if f.check_name == OpinionCheck.SINGLE_ENGINE
            ),
            None,
        )
        word_card = next(
            (
                f
                for f in page_findings
                if f.check_name == OpinionCheck.NO_MAJORITY
            ),
            None,
        )
        for group in page.get("groups") or []:
            if group.get("level") != ensemble.BLOCKING:
                continue
            common = {
                "page_in_opinion": number,
                "group_id": group["id"],
                "where": _place(page, group),
                "box_pt": group["box_pt"],
                "text": group["text"],
            }
            if group.get("agreement") == ensemble.SINGLE:
                out.append(
                    {
                        **common,
                        "kind": SINGLE,
                        "engine": group.get("source"),
                        # A table takes no text edit (the review page's
                        # rule), so the card offers none.
                        "table": group.get("kind") == markup.TABLE,
                        "crop": block_crop(group["box_pt"]),
                        "finding_pk": single_card.pk if single_card else None,
                    }
                )
                continue
            words = open_words(group)
            if (
                group.get("kind") == markup.TABLE
                or len(words) > MANY_OPEN_WORDS
            ):
                out.append(
                    {
                        **common,
                        "kind": BLOCK,
                        "table": group.get("kind") == markup.TABLE,
                        "open": len(words),
                        "readings": [
                            {
                                "engine": name,
                                "text": group["engines"][name]["text"],
                            }
                            for name in _present(group)
                        ],
                        "crop": block_crop(group["box_pt"]),
                        "finding_pk": word_card.pk if word_card else None,
                    }
                )
                continue
            for word in words:
                out.append(
                    {
                        **common,
                        "kind": WORD,
                        **word,
                        "finding_pk": word_card.pk if word_card else None,
                        "crop": crop_of(
                            group["box_pt"],
                            len(group["text"]),
                            word["start"],
                            word["length"],
                        ),
                    }
                )
        answered = {
            card["finding_pk"] for card in out if card.get("finding_pk")
        }
        for finding in page_findings:
            if finding.pk in answered:
                continue
            # A card the groups did not answer, the ``NO_MAJORITY`` card
            # of a page whose blocks hold no open word included: a text
            # written under another rule. The card is still open and
            # the approval still waits on it, so it is shown as a link,
            # never left out, or the page would offer an approval the
            # server refuses.
            out.append(_link_card(finding))
    for leftover in by_page.values():
        out.extend(_link_card(f) for f in leftover)
    return out


def _link_card(finding) -> dict:
    """Return the card of a finding the document alone answers."""
    return {
        "kind": LINK,
        "page_in_opinion": finding.page_in_opinion,
        "where": f"page {finding.page_in_opinion + 1}",
        "check": finding.check_name,
        "check_label": finding.get_check_name_display(),
        "message": finding.message,
        "finding_pk": finding.pk,
    }
