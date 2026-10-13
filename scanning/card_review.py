"""The card review of review 2: every box to look at, one after the next.

A volume in review 2 holds a few dozen cards a curator must answer and
a hundred boxes they should look at: the headnote brackets the reader
saw and the model did not (``brackets.missing``, 88 on one volume of
270 N.E.3d), the captions the model did not box after a key icon
(``findings``'s ``missing_caption``), and the ``IMAGE`` boxes, which
the model draws from the top of a column over the picture and on down
the page (``image_fit``), each of which the opinion PDF pastes onto the
bitonal page as a grey photograph of whatever it covers. In the viewer
each costs a click on the card, a scroll, a look and a click on the
box. This module turns them into cards a curator answers in a row,
the shape of the blocking review of review 3 (``blocking_review``):
the scan around the box, the box itself, drawn and movable, and two
or three buttons.

**The page writes nothing of its own.** An answer is one of the
viewer's own writes: a bracket or a caption is added through
``add_single_detection`` at the box as the card holds it, an image box
is kept through ``approve_detection``, resized through
``update_detection`` or taken out through ``delete_detection``, and a
card that says there is nothing there is dismissed through
``dismiss_finding``. So the viewer and this page hold one set of
rules, and every answer has the Undo of the write it made.

**The crop is exact**, unlike the blocking review's: every card here
carries a box in the space the rows are drawn in (the one the step-2
page draws its overlays in), so the crop is cut from the PDF that page
draws, the box and a pad around it, and never narrower than the text
column the box sits in (``widen_to_columns``), because a bracket or a
caption is judged by the lines beside it. The page can open any card
to the whole page, with the box still movable, for a guess that is in
the wrong column.
"""

from __future__ import annotations

from blackletter.models import Label

from scanning.models import CheckName, Detection, Issue, Scan

#: What a card offers (``kind``). A ``caption`` card is a caption the
#: model did not box after a key icon, answered by adding the box where
#: the card holds it; a ``bracket`` card a headnote bracket the reader
#: saw and the model did not box, answered the same way; a ``faint``
#: card a bracket box the model drew under blackletter's redaction
#: gate (#410), approved, resized or taken out; an ``image`` card a
#: live model ``IMAGE`` box, kept, resized or taken out; a ``link``
#: card one with no box to act on here (the first key icon of a volume
#: with no caption before it), which points at the viewer.
CAPTION = "caption"
BRACKET = "bracket"
FAINT = "faint"
IMAGE = "image"
LINK = "link"

#: The sections of the page, in order (``group``). The captions first,
#: because each one splits an opinion and the volume's pairing waits
#: on them; then the brackets, the bulk, one key press each; then the
#: images.
GROUPS = (CAPTION, BRACKET, IMAGE)

#: The checks whose open cards this review walks, and the kind of card
#: each one makes.
CHECKS = {
    CheckName.MISSING_CAPTION: CAPTION,
    CheckName.MISSING_HEADNOTE_BRACKET: BRACKET,
    CheckName.LOW_CONFIDENCE_HEADNOTE_BRACKET: FAINT,
}

#: The crop around a box, in render pixels: a fifth of an inch at
#: 200 dpi, enough to see the box is on the right thing.
CROP_PAD_PX = 40.0

#: The crop around an image box: wider, so a box drawn short of the
#: picture's edge can be pulled out to it.
IMAGE_PAD_PX = 90.0

#: A bracket's crop reaches this many bracket heights below the guess:
#: the first lines of the paragraph, enough to see the bracket and the
#: headnote number it opens, never the whole cell, which is a column.
BRACKET_DEPTH = 5.0

#: The width of a faint bracket's crop, in render pixels: a column of
#: a reporter page at 200 dpi, read from the box's left edge, because
#: the finding names the box and not the cell it opens.
BRACKET_CROP_WIDTH_PX = 620.0

Box = list[float]


def _box(value) -> Box | None:
    """Return ``value`` as a box, or None when it is not one."""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _pad(box: Box, pad: float, width: float, height: float) -> Box:
    """Return ``box`` grown by ``pad`` on each side, inside the page."""
    return [
        max(0.0, box[0] - pad),
        max(0.0, box[1] - pad),
        min(width, box[2] + pad),
        min(height, box[3] + pad),
    ]


def _card(
    *,
    kind: str,
    group: str,
    key: str,
    page_index: int,
    width: float,
    height: float,
    crop: Box,
    box: Box | None,
    label: str,
    message: str,
    issue_id: int | None = None,
    detection_id: int | None = None,
    confidence: float | None = None,
    anchor: Box | None = None,
) -> dict:
    return {
        "kind": kind,
        "group": group,
        "key": key,
        "issue_id": issue_id,
        "detection_id": detection_id,
        "page_index": page_index,
        "page_number": page_index + 1,
        "img_width": width,
        "img_height": height,
        "crop": crop,
        "box": box,
        "anchor": anchor,
        "label": label,
        "label_id": Label[label].value,
        "confidence": confidence,
        "message": message,
        "where": f"p. {page_index + 1}",
    }


def _finding_key(issue: Issue, page_index: int, box: Box) -> str:
    """Return the key of a finding card: its address, never its pk.

    An add deletes the finding (the box is cover now, and the rebuild
    writes it no more) and its Undo writes it again under another pk;
    the page reads the cards again after an Undo and finds the answer
    by this key.
    """
    return "f:{}:{}:{}".format(
        issue.check_name, page_index, ":".join(str(round(v)) for v in box)
    )


def _page_of(metadata: dict) -> tuple[int, float, float] | None:
    """Return the page index and the render size a finding names."""
    page_index = metadata.get("page_index")
    width = metadata.get("img_width")
    height = metadata.get("img_height")
    if not isinstance(page_index, int) or isinstance(page_index, bool):
        return None
    try:
        width, height = float(width), float(height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return page_index, width, height


def bracket_card(issue: Issue) -> dict | None:
    """Return the card of a ``missing_headnote_bracket`` finding.

    The box is the bracket's guessed place in the cell
    (``brackets._guess``), the crop the first lines of the cell under
    it. A finding written before the guess was stamped has no box to
    add and is a ``link`` card.

    :param issue: The finding.
    :returns: The card, or None when the finding names no page.
    :rtype: dict | None
    """
    metadata = issue.metadata or {}
    page = _page_of(metadata)
    cell = _box(metadata.get("bbox"))
    if page is None or cell is None:
        return None
    page_index, width, height = page
    guess = _box(metadata.get("guess"))
    common = {
        "group": BRACKET,
        "key": _finding_key(issue, page_index, cell),
        "page_index": page_index,
        "width": width,
        "height": height,
        "label": Label.HEADNOTE_BRACKET.name,
        "message": issue.message,
        "issue_id": issue.pk,
    }
    if guess is None:
        crop = _pad(cell, CROP_PAD_PX, width, height)
        return _card(kind=LINK, crop=crop, box=None, **common)
    depth = (guess[3] - guess[1]) * BRACKET_DEPTH
    crop = _pad(
        [cell[0], guess[1], cell[2], min(cell[3], guess[3] + depth)],
        CROP_PAD_PX,
        width,
        height,
    )
    return _card(kind=BRACKET, crop=crop, box=guess, **common)


def faint_bracket_card(issue: Issue) -> dict | None:
    """Return the card of a ``low_confidence_headnote_bracket`` finding.

    The box is the model's own, under the gate (#410); the crop reads
    a column's width from its left edge and ``BRACKET_DEPTH`` bracket
    heights down, the first lines of the paragraph it opens. An
    approval lifts the row to 1.0 and the card goes at the rebuild.

    :param issue: The finding.
    :returns: The card, or None when the finding names no page or no
        row.
    :rtype: dict | None
    """
    metadata = issue.metadata or {}
    page = _page_of(metadata)
    box = _box(metadata.get("bbox"))
    detection_id = metadata.get("detection_id")
    if page is None or box is None or not isinstance(detection_id, int):
        return None
    page_index, width, height = page
    depth = (box[3] - box[1]) * BRACKET_DEPTH
    frame = [
        box[0],
        box[1],
        max(box[2], box[0] + BRACKET_CROP_WIDTH_PX),
        box[3] + depth,
    ]
    return _card(
        kind=FAINT,
        group=BRACKET,
        key=_finding_key(issue, page_index, box),
        page_index=page_index,
        width=width,
        height=height,
        crop=_pad(frame, CROP_PAD_PX, width, height),
        box=box,
        label=Label.HEADNOTE_BRACKET.name,
        message=issue.message,
        issue_id=issue.pk,
        detection_id=detection_id,
    )


def caption_card(issue: Issue) -> dict | None:
    """Return the card of a ``missing_caption`` finding.

    The box is where the caption is likely to be
    (``findings._caption_guess``), the crop that box and its pad, and
    the key icon the caption follows is the card's ``anchor``, drawn
    on the crop, when it is in the same column of the same page: the
    curator sees what the caption comes after. The finding of the
    volume's first key icon names the key, not a caption's place, so
    its card is a ``link``: the caption is before the key, on the page
    the viewer shows.

    :param issue: The finding.
    :returns: The card, or None when the finding names no page.
    :rtype: dict | None
    """
    metadata = issue.metadata or {}
    page = _page_of(metadata)
    box = _box(metadata.get("bbox"))
    if page is None or box is None:
        return None
    page_index, width, height = page
    common = {
        "group": CAPTION,
        "key": _finding_key(issue, page_index, box),
        "page_index": page_index,
        "width": width,
        "height": height,
        "label": Label.CASE_CAPTION.name,
        "message": issue.message,
        "issue_id": issue.pk,
    }
    if "key_page_index" not in metadata:
        crop = _pad(box, IMAGE_PAD_PX, width, height)
        return _card(kind=LINK, crop=crop, box=None, **common)
    anchor = _box(metadata.get("key_bbox"))
    if (
        anchor is None
        or metadata.get("key_page_index") != page_index
        or min(anchor[2], box[2]) <= max(anchor[0], box[0])
    ):
        anchor = None
    frame = box
    if anchor is not None:
        frame = [
            min(box[0], anchor[0]),
            min(box[1], anchor[1]),
            max(box[2], anchor[2]),
            max(box[3], anchor[3]),
        ]
    crop = _pad(frame, CROP_PAD_PX, width, height)
    return _card(kind=CAPTION, crop=crop, box=box, anchor=anchor, **common)


def image_card(row: Detection) -> dict | None:
    """Return the card of a live model ``IMAGE`` row.

    :param row: The row.
    :returns: The card, or None when the row has no render size.
    :rtype: dict | None
    """
    width, height = float(row.img_width), float(row.img_height)
    box = _box([row.x0, row.y0, row.x1, row.y1])
    if width <= 0 or height <= 0 or box is None:
        return None
    return _card(
        kind=IMAGE,
        group=IMAGE,
        key=f"d{row.pk}",
        page_index=row.page_index,
        width=width,
        height=height,
        crop=_pad(box, IMAGE_PAD_PX, width, height),
        box=box,
        label=Label.IMAGE.name,
        message=(
            f"The model drew an image box here at {row.confidence:.2f}. "
            "Keep it over a picture, pull it in to the picture's edges, "
            "or take it out: the opinion PDF pastes the original scan "
            "of the box over the page."
        ),
        detection_id=row.pk,
        confidence=row.confidence,
    )


def _column_of(box: Box, columns: list[Box]) -> Box | None:
    """Return the column box ``box`` sits in: the widest overlap, else
    the nearest by centre, else None."""
    if not columns:
        return None
    centre = (box[0] + box[2]) / 2

    def overlap(column: Box) -> float:
        return min(column[2], box[2]) - max(column[0], box[0])

    best = max(columns, key=overlap)
    if overlap(best) > 0:
        return best
    return min(columns, key=lambda c: abs((c[0] + c[2]) / 2 - centre))


def widen_to_columns(built: list[dict], scan: Scan) -> None:
    """Widen every crop to the text column its box sits in, in place.

    The live ``TEXT_COLUMN`` rows of the cards' pages, one query; a page
    with no column box keeps its crop.

    :param built: The cards.
    :param scan: The scan.
    """
    pages = {c["page_index"] for c in built}
    if not pages:
        return
    columns: dict[int, list[Box]] = {}
    for row in Detection.objects.live().filter(
        scan=scan, label=Label.TEXT_COLUMN.name, page_index__in=pages
    ):
        box = _box([row.x0, row.y0, row.x1, row.y1])
        if box is not None:
            columns.setdefault(row.page_index, []).append(box)
    for card in built:
        frame = card["box"] or card["crop"]
        column = _column_of(frame, columns.get(card["page_index"], []))
        if column is None:
            continue
        crop = card["crop"]
        card["crop"] = [
            max(0.0, min(crop[0], column[0] - CROP_PAD_PX)),
            crop[1],
            min(card["img_width"], max(crop[2], column[2] + CROP_PAD_PX)),
            crop[3],
        ]


def attach_others(built: list[dict], scan: Scan) -> None:
    """Put the page's other live boxes on every card, in place.

    ``others`` is what the card draws as thin outlines around its own
    box, so a curator sees what is boxed already before drawing more:
    every live row of the page but the card's own, with ``own`` for a
    row a curator drew or approved. One query over the cards' pages.

    :param built: The cards.
    :param scan: The scan.
    """
    pages = {c["page_index"] for c in built}
    if not pages:
        return
    rows: dict[int, list[dict]] = {}
    for row in (
        Detection.objects.live()
        .filter(scan=scan, page_index__in=pages)
        .order_by("pk")
    ):
        box = _box([row.x0, row.y0, row.x1, row.y1])
        if box is None:
            continue
        rows.setdefault(row.page_index, []).append(
            {
                "id": row.pk,
                "label": row.label,
                "box": box,
                "own": row.model_name == Detection.ModelName.MANUAL,
            }
        )
    for card in built:
        card["others"] = [
            o
            for o in rows.get(card["page_index"], [])
            if o["id"] != card["detection_id"]
        ]


def cards(scan: Scan) -> list[dict]:
    """Return the cards of a scan, in the order of the page.

    The open findings of ``CHECKS`` and the live model ``IMAGE`` rows,
    each group by page, every crop widened to its text column. A
    dismissed finding and a row a curator drew or approved are
    answered already and make no card.

    :param scan: The scan.
    :returns: The cards.
    :rtype: list[dict]
    """
    built: list[dict] = []
    builders = {
        CAPTION: caption_card,
        BRACKET: bracket_card,
        FAINT: faint_bracket_card,
    }
    issues = Issue.objects.filter(
        scan=scan, check_name__in=list(CHECKS), dismissal__isnull=True
    ).order_by("page_number", "pk")
    for issue in issues:
        card = builders[CHECKS[issue.check_name]](issue)
        if card is not None:
            built.append(card)
    # The confidence of a faint bracket is on its row, not the finding.
    faint = [c for c in built if c["kind"] == FAINT]
    if faint:
        confidences = dict(
            Detection.objects.filter(
                pk__in=[c["detection_id"] for c in faint]
            ).values_list("pk", "confidence")
        )
        for card in faint:
            card["confidence"] = confidences.get(card["detection_id"])
    rows = (
        Detection.objects.live()
        .filter(scan=scan, label=Label.IMAGE.name)
        .exclude(model_name=Detection.ModelName.MANUAL)
        .order_by("page_index", "y0", "pk")
    )
    for row in rows:
        card = image_card(row)
        if card is not None:
            built.append(card)
    widen_to_columns(built, scan)
    attach_others(built, scan)
    order = {group: i for i, group in enumerate(GROUPS)}
    built.sort(key=lambda c: (order[c["group"]], c["page_index"], c["key"]))
    return built
