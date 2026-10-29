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
#: A ``cover`` card is a ``PARTIAL_REDACTION`` finding: the crop of the
#: blocks a box covers in part, each outlined, over the redacted page,
#: so a person sees whether any word shows past the box. "Looks right"
#: dismisses it.
COVER = "cover"

#: The least crop of a ``cover`` card, in points: a column's width and
#: five lines, so a block of one word shows the redaction beside it.
COVER_MIN_WIDTH_PT = 220.0
COVER_MIN_HEIGHT_PT = 5 * LINE_PT

#: A voted block with more open words than this is one ``block`` card
#: and not a card per word: 71 words of a table Mistral read as noise
#: are one bad reading, not 71 decisions.
MANY_OPEN_WORDS = 8

#: What an engine's reading says when it read nothing at the word.
READ_NOTHING = ""

#: The flags of ``ensemble.vote_words`` a card opens: the words no two
#: engines agree on in the blocking review, the words a majority
#: settled over one engine in the warnings review.
LOW_CONFIDENCE = "low_confidence"
MAJORITY = "majority"


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


def _run_words(inserted: dict, position: int, put_in: dict) -> str:
    """Return the words one engine read at a base position that the
    base did not and the vote dropped, as the engine wrote them, or ""
    for none. A run that is only a mark has no key and reads as
    nothing, the vote's rule; a run the vote put in is in the text as
    inserted tokens already (``put_in``, by position), and a row that
    showed it would write it twice."""
    runs = inserted.get(position) or []
    return " ".join(
        word
        for run in runs
        if any(key for key, _ in run)
        and tuple(key for key, _ in run if key) != put_in.get(position)
        for _, word in run
    )


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


#: The first document schema whose kept tokens of a person's block
#: carry positions counted over the words as ``ensemble._pairs`` joins
#: them. An older document counted them over the text split on
#: whitespace, and the cards align the engines with the join, so a
#: word joined earlier in the block would shift every later card onto
#: the readings of the wrong word: those tokens open no word card
#: until the document is written again (``rerun_opinion_ensemble``).
POSITIONS_SCHEMA = 13


def open_words(
    group: dict, flag: str = LOW_CONFIDENCE, schema: int | None = None
) -> list[dict]:
    """Return the places of one voted group no two engines agree on.

    With ``flag`` :data:`MAJORITY`, the places a majority settled over
    one engine instead, in a voted group or in a group a majority read
    alike: the warnings review opens those, so a person blesses the
    majority's word or picks the other reading.

    The vote is run again over the group's own readings, the inputs
    the build had, so the tokens are the ones the document holds and
    each carries the base position it answers for. Each other engine's
    word at that position comes from the same alignment the vote used
    (``ensemble._candidates``), so the rows of the card are the votes
    that were cast. A block a person wrote keeps the open words they
    left as they were, as the document's own tokens, placed in the
    person's text: an answer to one card closes that card alone.

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

    **A gap is a card of its own** (``gap`` on the entry): a place some
    engine read words at that the base did not, which did not go in,
    or where the base read words a majority dropped. The card sits on
    the gap, ``start`` the offset of the word after it (or the end of
    the last word) and ``length`` 0, its rows are what each engine
    read there with "" for nothing (the base's row holds its dropped
    words), and the answer goes into the gap: an insertion, never a
    rewrite of the word beside it. The level of the gap picks the
    review it opens in.

    :param group: One group of a stamped ensemble document.
    :param flag: The flag of the tokens to open.
    :param schema: The document's ``schema_version``, or None for a
        group built now: a person's block of a document under
        :data:`POSITIONS_SCHEMA` opens no word card.
    :returns: One entry per open place: ``{start, length, token,
        before, after, readings: [{engine, word}], position}``, with
        ``start`` an offset into the group's ``text``, ``token`` the
        text there and ``position`` the base position the card answers
        for: a span's first, a gap's the word after it, and a word of a
        run its position with ``run_index`` beside it. The browser keys
        a card kept as shown by its block's ``box_pt`` and the
        position, never by the finding pk, the group id or the offset:
        every rebuild renews the finding rows, a drop renumbers the
        later groups of the page, and an edit before the word moves
        its offset.
    :rtype: list[dict]
    """
    agreements = (
        (ensemble.VOTED, ensemble.MAJORITY, ensemble.HUMAN)
        if flag == MAJORITY
        else (ensemble.VOTED, ensemble.HUMAN)
    )
    if group.get("agreement") not in agreements:
        return []
    present = _present(group)
    if len(present) < 2:
        return []
    engines = group["engines"]
    # The base is the engine whose text is shown, so a card's offsets
    # are offsets into that text: a block a majority read alike shows
    # the first agreeing engine's, which need not rank first of all
    # that read. A person's block names the engine its tokens count
    # against (``ensemble._human_read``); one from before that stamp
    # counted against the first that read.
    source = group.get("source")
    if group.get("agreement") == ensemble.HUMAN:
        source = group.get("base")
    base = source if source in present else present[0]
    base_pairs = ensemble._pairs(engines[base]["text"])
    others = [
        (name, ensemble._pairs(engines[name]["text"]))
        for name in present
        if name != base
    ]
    voted, _ = ensemble.vote_words(base_pairs, [pairs for _, pairs in others])
    if group.get("agreement") == ensemble.HUMAN:
        # A person wrote the text; the open words they left as they
        # were are the document's own tokens, each at its base
        # position (``ensemble._kept_open``), placed in that text. A
        # position of an older document counts other words.
        tokens = group.get("tokens") or []
        if schema is not None and schema < POSITIONS_SCHEMA:
            tokens = [
                {
                    k: v
                    for k, v in token.items()
                    if k not in ("position", "run_index", "gap_at")
                }
                for token in tokens
            ]
    else:
        tokens = voted
    # The base positions the vote kept a word for: a base word with no
    # token is one the vote dropped, and the gap card before the next
    # kept word shows it. The vote's own tokens, never the person's,
    # whose rewritten word carries no position.
    voted_at = {
        t["position"]
        for t in voted
        if t.get("position") is not None and not t.get("inserted")
    }
    # The runs the vote put in, by position: the keys of its inserted
    # tokens there, which a gap card's rows must not show again.
    put_in: dict[int, tuple] = {}
    for t in voted:
        if t.get("inserted") and t.get("position") is not None:
            put_in[t["position"]] = put_in.get(t["position"], ()) + (
                ensemble.compare_word(t["text"]),
            )
    candidates = {
        name: ensemble._candidates(base_pairs, pairs) for name, pairs in others
    }
    ends = {name: _span_ends(at) for name, (at, _) in candidates.items()}
    shown = [token["text"] for token in tokens if token["text"]]

    # Every token with its offset in the group's text, its index among
    # the shown words and its place in an inserted run.
    text = group.get("text") or " ".join(shown)
    placed: list[dict] = []
    offset = 0
    run_index: int | None = None
    for token in tokens:
        if not token["text"]:
            continue
        if "run_index" in token:
            # A block a person wrote: the place of an inserted word in
            # its run is carried on the token (``ensemble._kept_open``),
            # because the person may have changed its neighbour.
            run_index = token["run_index"]
        elif token.get("inserted"):
            run_index = 0 if run_index is None else run_index + 1
        else:
            run_index = None
        start = text.find(token["text"], offset)
        if start < 0:
            start = offset
        placed.append(
            {
                "token": token,
                "start": start,
                "end": start + len(token["text"]),
                "index": len(placed),
                "run_index": run_index,
            }
        )
        offset = start + len(token["text"])

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
        if (
            not token.get(flag)
            or entry["run_index"] is not None
            or token.get("position") is None
        ):
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
            and e["token"].get("position") is not None
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
                "position": first,
            }
        )
    # The words of a run the base did not read, one card each.
    for entry in placed:
        token = entry["token"]
        if (
            not token.get(flag)
            or entry["run_index"] is None
            or token.get("position") is None
        ):
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
                "position": position,
                "run_index": entry["run_index"],
            }
        )
    # The gaps, one card each, on the gap itself.
    level = ensemble.BLOCKING if flag == LOW_CONFIDENCE else ensemble.WARNING
    for entry in placed:
        token = entry["token"]
        for key in ("gap", "gap_after"):
            if token.get(key) != level:
                continue
            after_word = key == "gap_after"
            # The base position the gap stands before: the token's
            # own, or ``gap_at`` on a word a person wrote in place of
            # the vote's (``ensemble._kept_open``).
            at = token.get("gap_at", token.get("position"))
            if not after_word and at is None:
                continue
            position = len(base_pairs) if after_word else at
            index = entry["index"]
            before, after = (
                context(index + 1, index)
                if after_word
                else context(index, index - 1)
            )
            # The base positions inside the gap: the words the base
            # read there that a majority dropped, between the base
            # word before the gap and the one after it.
            # The gap runs from the vote's last kept word before this
            # position: the vote's own positions, never the person's
            # tokens, whose rewritten word carries none.
            last_base = max((p for p in voted_at if p < position), default=-1)
            # A base word with no vote token is dropped; a bare mark
            # has no key and no token either, and is no word.
            dropped = [
                p
                for p in range(last_base + 1, position)
                if p not in voted_at and base_pairs[p][0]
            ]
            readings = [
                {
                    "engine": base,
                    "word": " ".join(base_pairs[p][1] for p in dropped),
                }
            ]
            for name, (at, inserted) in candidates.items():
                read_there = []
                for p in dropped:
                    for reading in at.get(p) or []:
                        if isinstance(reading, ensemble.Carried):
                            continue
                        if reading[1]:
                            read_there.append(reading[1])
                # The runs over the whole gap: the vote carries a gap
                # past a bare mark to the next word with a token.
                for p in range(last_base + 1, position + 1):
                    run = _run_words(inserted, p, put_in)
                    if run:
                        read_there.append(run)
                readings.append({"engine": name, "word": " ".join(read_there)})
            words.append(
                {
                    "start": entry["end"] if after_word else entry["start"],
                    "length": 0,
                    "gap": True,
                    "token": "",
                    "before": before,
                    "after": after,
                    "readings": readings,
                    "position": position,
                    # The crop is cut at the word beside the gap.
                    "crop_span": [
                        entry["start"],
                        entry["end"] - entry["start"],
                    ],
                }
            )
    words.sort(key=lambda w: (w["start"], w["length"]))
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


def _cover_card(page: dict, finding) -> dict | None:
    """Return the ``cover`` card of a page's ``PARTIAL_REDACTION`` card.

    The blocks are the page's partial drops the card counts (a drop of
    a group that lost a bracket token is in ``partial_bracket`` and not
    the card's, ``ensemble._partial_message``), each with the box the
    alignment found. The crop is their union, :data:`CROP_PAD_PT` wider
    and a line taller on each side, so the edge of the redaction shows.
    None when the document records no box for them (a document written
    before the drops carried one): the link card stands in.

    :param page: The page of the stamped document.
    :param finding: The page's open ``PARTIAL_REDACTION`` finding.
    :returns: The card, or None.
    :rtype: dict | None
    """
    boxes = [
        drop["box_pt"]
        for drop in page.get("dropped") or []
        if drop.get("partial")
        and not drop.get("bracket")
        and drop.get("box_pt")
    ]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes) - CROP_PAD_PT
    y0 = min(b[1] for b in boxes) - LINE_PT
    x1 = max(b[2] for b in boxes) + CROP_PAD_PT
    y1 = max(b[3] for b in boxes) + LINE_PT
    # A block of a word or two is shown with the page around it, or the
    # crop is a few letters blown up and nothing can be judged.
    grow_x = max(0.0, COVER_MIN_WIDTH_PT - (x1 - x0)) / 2
    grow_y = max(0.0, COVER_MIN_HEIGHT_PT - (y1 - y0)) / 2
    return {
        **_link_card(finding),
        "kind": COVER,
        "boxes": boxes,
        "crop": {
            "crop": [
                round(max(0.0, x0 - grow_x), 2),
                round(max(0.0, y0 - grow_y), 2),
                round(x1 + grow_x, 2),
                round(y1 + grow_y, 2),
            ],
            "highlight": None,
            "line": None,
            "lines": None,
        },
    }


def _stale_open(group: dict, schema: int | None) -> int:
    """Return how many open places a person's block of an older
    document holds, which the word cards cannot show.

    The stale gate of :func:`open_words` strips the positions of such
    a block's tokens, so it opens no word card; with none, the block
    would make no card at all, and an answer elsewhere on the page
    would settle the page's finding with this block unseen. The count
    is the block's own (``n_low_confidence``): a flagged word or a
    blocking gap each.

    :param group: One group of the document.
    :param schema: The document's ``schema_version``.
    :returns: The count, zero for any other block.
    :rtype: int
    """
    if (
        group.get("agreement") != ensemble.HUMAN
        or schema is None
        or schema >= POSITIONS_SCHEMA
    ):
        return 0
    return int(group.get("n_low_confidence") or 0)


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


def cards(
    document: dict, findings: list, level: str = ensemble.BLOCKING
) -> list[dict]:
    """Return the cards of one opinion, in the order of the pages.

    With ``level`` ``ensemble.WARNING``, the cards of the warnings
    review instead (:func:`_warning_cards`), over the open warning
    findings: a page of the same format that gates nothing.

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
    :param findings: The open ``OpinionFinding`` rows of the level.
    :param level: ``ensemble.BLOCKING`` or ``ensemble.WARNING``.
    :returns: The cards.
    :rtype: list[dict]
    """
    from scanning.models import OpinionCheck

    by_page: dict[int, list] = {}
    for finding in findings:
        by_page.setdefault(finding.page_in_opinion, []).append(finding)
    out: list[dict] = []
    schema = document.get("schema_version")
    for page in document.get("pages") or []:
        number = page["page_in_opinion"]
        page_findings = by_page.pop(number, [])
        if level == ensemble.WARNING:
            out.extend(_warning_cards(page, number, page_findings, schema))
            continue
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
            # A block is a card only while the finding of its page is
            # open: the finding is what the approval waits on, and a
            # dismissed one leaves its blocks in the document as they
            # were. A card with no finding would offer an answer with
            # nothing to close.
            if group.get("agreement") == ensemble.SINGLE:
                if single_card is None:
                    continue
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
            if word_card is None:
                continue
            words = open_words(group, schema=schema)
            # A person's block of an older document: its open words
            # cannot be placed, so the whole block is the card, and
            # the page's finding waits on it.
            stale = _stale_open(group, schema) if not words else 0
            if (
                group.get("kind") == markup.TABLE
                or len(words) > MANY_OPEN_WORDS
                or stale
            ):
                out.append(
                    {
                        **common,
                        "kind": BLOCK,
                        "table": group.get("kind") == markup.TABLE,
                        "open": len(words) or stale,
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
                            *(
                                word.get("crop_span")
                                or (word["start"], word["length"])
                            ),
                        ),
                    }
                )
        answered = {
            card["finding_pk"] for card in out if card.get("finding_pk")
        }
        for finding in page_findings:
            if finding.pk in answered:
                continue
            if finding.check_name == OpinionCheck.PARTIAL_REDACTION:
                cover = _cover_card(page, finding)
                if cover is not None:
                    out.append(cover)
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


def _warning_cards(
    page: dict, number: int, page_findings: list, schema: int | None
) -> list:
    """Return the warning cards of one page.

    One ``word`` card per place a majority settled over one engine in
    a WARNING group, while the page's ``ENGINES_DISAGREE`` card is
    open; a ``block`` card for a WARNING group with no such place (a
    silent engine, a box one engine did not draw), whose rows all read
    alike, so choosing one is "looks right"; and a ``link`` card for
    every other open warning of the page, with its dismissal. A
    BLOCKING group is no card here: the blocking review holds it.

    :param page: The page of the document.
    :param number: Its ``page_in_opinion``.
    :param page_findings: The open warning findings of the page.
    :returns: The cards.
    :rtype: list[dict]
    """
    from scanning.models import OpinionCheck

    disagree = next(
        (
            f
            for f in page_findings
            if f.check_name == OpinionCheck.ENGINES_DISAGREE
        ),
        None,
    )
    out: list[dict] = []
    answered: set = set()
    for group in page.get("groups") or []:
        if group.get("level") != ensemble.WARNING or disagree is None:
            continue
        common = {
            "page_in_opinion": number,
            "group_id": group["id"],
            "where": _place(page, group),
            "box_pt": group["box_pt"],
            "text": group["text"],
            "level": ensemble.WARNING,
            "finding_pk": disagree.pk,
        }
        answered.add(disagree.pk)
        table = group.get("kind") == markup.TABLE
        words = [] if table else open_words(group, MAJORITY, schema=schema)
        if words and len(words) <= MANY_OPEN_WORDS:
            for word in words:
                out.append(
                    {
                        **common,
                        "kind": WORD,
                        **word,
                        "crop": crop_of(
                            group["box_pt"],
                            len(group["text"]),
                            *(
                                word.get("crop_span")
                                or (word["start"], word["length"])
                            ),
                        ),
                    }
                )
            continue
        out.append(
            {
                **common,
                "kind": BLOCK,
                "table": table,
                "open": len(words),
                "silent": list(group.get("silent") or []),
                "readings": [
                    {"engine": name, "text": group["engines"][name]["text"]}
                    for name in _present(group)
                ],
                "crop": block_crop(group["box_pt"]),
            }
        )
    for finding in page_findings:
        if finding.pk in answered:
            continue
        # A text built before the card blocked keeps it as a warning
        # until its next build; it is the same card here.
        cover = (
            _cover_card(page, finding)
            if finding.check_name == OpinionCheck.PARTIAL_REDACTION
            else None
        )
        out.append(
            {**(cover or _link_card(finding)), "level": ensemble.WARNING}
        )
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
