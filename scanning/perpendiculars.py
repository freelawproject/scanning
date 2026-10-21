"""West's perpendiculars on a page image: find them, paint them out.

A West reporter prints the page breaks of a parallel reporter inline,
as an inverted T with a subscript page number (``⊥505``). No engine
reads the glyph: dots.mocr drops it with its number, Mistral dresses
the number as a stray ``1`` or markup (``1$_{505}$``), Surya misreads
both as letters (``I48``, ``los``), and the ensemble's vote gets three
readings of one spot, a Blocking Card per mark. The marks are found on
the page image before any engine reads it and painted over, and their
boxes are kept with the read, so the parallel pagination is parked,
not lost.

No Django import: this module is copied into the dots.mocr and Surya
worker images next to their handlers, like ``runpod_common.py``, and
imported as a top-level module there; the daemon imports it for the
Mistral render. OpenCV and NumPy only.

Every pixel limit below was measured at 100 dpi on 339 A.3d and 224
A.3d, and is scaled by ``dpi / 100``; the pipeline renders at 200.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

#: Raise when the find or the paint changes: a result carries it, so a
#: read under an older rule can be told from one under this.
VERSION = 1

#: Rows above the bar that hold the stem and the subscript digits.
STEM_ROWS = 16

#: One mark: the bar's box and the stem's offset from its left end.
Mark = tuple[int, int, int, int, int]


def paint(image: Image.Image, dpi: int) -> tuple[Image.Image, list[list[int]]]:
    """Return the page with its marks painted over, and the marks.

    The one call the three render paths make. An image with no mark
    comes back as it was.

    :param image: The rendered page, any PIL mode.
    :param dpi: The render resolution.
    :returns: ``(image, marks)``, each mark ``[x, y, w, h, stem]`` in
        the image's pixels.
    """
    found = find(image, dpi)
    if not found:
        return image, []
    return whiteout(image, found, dpi), [list(mark) for mark in found]


def find(image: Image.Image, dpi: int) -> list[Mark]:
    """Return the marks on a page image."""
    gray = np.asarray(image.convert("L"))
    return marks(gray, scale=dpi / 100.0)


def whiteout(image: Image.Image, found: list[Mark], dpi: int) -> Image.Image:
    """Return the page with ``found`` painted over in white."""
    bgr = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    out = _whiteout(bgr, found, scale=dpi / 100.0)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


# ── the find ──────────────────────────────────────────────────────────
def marks(
    gray: np.ndarray,
    scale: float = 1.0,
    bar_w=(16, 60),
    bar_h=(3, 8),
    stem_h=12,
    top_margin=8,
) -> list[Mark]:
    """Return the marks on a grey page: ``(x, y, w, h, stem)`` each.

    A mark is a solid horizontal bar 16-60 px wide and 3-8 px tall at
    100 dpi with a vertical stem rising from its left half; ``stem``
    is the stem's offset from the bar's left end. ``scale`` is the
    image's dpi over 100.
    """
    s = scale
    bar_w = (int(bar_w[0] * s), int(bar_w[1] * s))
    bar_h = (int(bar_h[0] * s), int(bar_h[1] * s))
    stem_h = int(stem_h * s)
    top_margin = int(top_margin * s)
    stem_rows = int(STEM_ROWS * s)
    _, bw = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (bar_w[0], bar_h[0]))
    opened = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)
    _, _, stats, _ = cv2.connectedComponentsWithStats(opened)
    found: list[Mark] = []
    for x, y, w, h, _ in stats[1:]:
        if not (bar_w[0] <= w <= bar_w[1] and bar_h[0] <= h <= bar_h[1]):
            continue
        if y < top_margin:
            continue
        above = bw[max(0, y - stem_rows) : y, x : x + w // 2]
        cols = (above > 0).sum(axis=0)
        if cols.size == 0 or cols.max() < stem_h:
            continue
        stem = int(cols.argmax())
        # The stem stands inside the bar, not at its left end: an
        # underlined word (``Inc.``) has its I at the very end. Measured
        # on 339 A.3d at 100 dpi: 11 marks have the stem 2-10 px in, the
        # one underline 0. An underlined word with an ascender further
        # in would still pass; the bar's weight separates them at 200
        # dpi.
        if stem < 2 * s:
            continue
        found.append((int(x), int(y), int(w), int(h), stem))
    return found


def _binarize(image: np.ndarray) -> np.ndarray:
    gray = (
        image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    )
    _, bw = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )
    return bw


def _in_word(bw: np.ndarray, mark: Mark, scale: float = 1.0) -> bool:
    """Return whether the mark stands inside a word (``pro⊥gram666``):
    a letter follows the stem at once, so there is ink above digit
    height in the first few columns right of the stem. After a mark
    between words the digits come first, and they are shorter."""
    x, y, w, h, stem = mark
    s = scale
    zone = bw[
        y - int(STEM_ROWS * s) : y - int(9.5 * s),
        x + stem + int(3 * s) : x + stem + int(9 * s),
    ]
    return zone.size > 0 and (zone > 0).mean() > 0.05


def _t_box(
    bw: np.ndarray, mark: Mark, scale: float = 1.0
) -> tuple[int, int, int, int]:
    """The box of the T alone: the stem's columns from cap height down
    to the bar, and the bar in its own rows. The stem is the run of
    tall columns around the stem position; a tall letter beside it
    (the l of ``William``) is a separate run and stays."""
    x, y, w, h, stem = mark
    s = scale
    rows = int(STEM_ROWS * s)
    cols = (bw[y - rows : y, x : x + w // 2] > 0).sum(axis=0)
    tall = cols >= int(12 * s)
    left = right = stem
    while left - 1 >= 0 and tall[left - 1]:
        left -= 1
    while right + 1 < len(tall) and tall[right + 1]:
        right += 1
    # A stem is at most ~3.5 px wide per 100 dpi and stands about 0.42
    # of the way along its bar; a wider run holds a touching letter, so
    # keep the slice of it nearest that spot.
    width = int(3.5 * s)
    if right - left + 1 > width:
        want = 0.42 * w
        best = min(
            range(left, right - width + 2),
            key=lambda a: abs(a + width / 2 - want),
        )
        left, right = best, best + width - 1
    return (
        x + left - int(1.5 * s),
        y - rows,
        x + right + int(1.5 * s),
        y + h + int(s),
    )


def _whiteout(
    image: np.ndarray, found: list[Mark], scale: float = 1.0
) -> np.ndarray:
    """Paint the marks over in white. Between words: the bar across its
    width plus a little of its thin left tail, in its own rows only, so
    a letter whose foot touches the tail keeps its body; the stem and
    digits from the stem rightward to the first blank column after the
    digits, capped a few pixels past the bar's end, because the digits
    often touch the next word. Inside a word: the T alone, stem columns
    and bar rows, so the letters on both sides and the number after the
    word stay as printed."""
    out = image.copy()
    bw = _binarize(image)
    m = max(1, int(round(scale)))
    rows = int(STEM_ROWS * scale)
    digit_rows = int(9 * scale)
    white = (255, 255, 255)
    for mark in found:
        x, y, w, h, stem = mark
        if _in_word(bw, mark, scale):
            left, top, right, bottom = _t_box(bw, mark, scale)
            cv2.rectangle(
                out, (left, top), (right, y - 1), white, -1
            )  # the stem
            cv2.rectangle(
                out, (x - 2 * m, y - m), (x + w + m, bottom), white, -1
            )  # the bar
            continue
        cv2.rectangle(
            out, (x - 4 * m, y - m), (x + w + m, y + h + m), white, -1
        )
        # Walk right from the stem: over the digits (ink in the digit
        # band, none above it), stopping at a blank column or at the
        # first tall column, which is the next word's letter even when
        # it touches the digits; never past the bar's end by much.
        _, _, stem_right, _ = _t_box(bw, mark, scale)
        x0 = stem_right
        span = bw[:, x0 : x + w + 20 * m] > 0
        low = span[y - digit_rows : y].any(axis=0)
        # Measured on 224 A.3d at 200 dpi: digits reach 14-16 px above
        # the bar, the next word's letters 21 px or more.
        tall = span[y - rows : y - int(9.5 * scale)].any(axis=0)
        # Digits are separate glyphs a pixel or two apart, so step over
        # gaps narrower than a word space; stop at a letter or a space.
        gap_max = int(2.5 * scale)
        c = 0
        while c < len(low) and not low[c] and not tall[c]:
            c += 1
        end = c
        while c < len(low):
            if tall[c]:
                break
            if low[c]:
                c += 1
                end = c
                continue
            g = 0
            while c + g < len(low) and not low[c + g] and not tall[c + g]:
                g += 1
            if g > gap_max or c + g >= len(low) or tall[c + g]:
                break
            c += g
        right = min(x0 + end, x + w + 8 * m)
        cv2.rectangle(out, (x + stem - 2 * m, y - rows), (right, y), white, -1)
    return out
