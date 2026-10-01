"""Per-page image features for the bad-page classifier.

The measures of two scancheck tools, ``portal-code/unwarp.py`` (the
text-line finder and the warp scores of a tool that straightens
curled lines) and ``portal-code/scancheck/features.py`` (the page
statistics built on those lines), without their correction, training
and sampling code. The model in ``badpage-v2.joblib`` was trained on
these exact measures, so each is kept line for line with its source:
a changed feature here is a different column for the model, and a
retrain comes with the change.

The feature families, all plain pixel statistics, no OCR:

- faint / bleed-through: ink density in the letter band, fragmentation,
  faint-line runs
- smear / blur: fused-blob share, lost letter counters (holes), stroke
  thickness, blob width and height, each also over the worst run of 5
  consecutive lines
- crop / chip: ink in the corners of the running-head zone, ink touching
  each image border, the text block's margins, the first line's start
  relative to its column edge
- warp / skew: tilt, curl, edge lean

Pages are split into 6 regions (2 columns x 3 bands) and each measure
is summarised per region (worst / best / spread) as well as over the
page. ``scoring.add_relative`` then expresses every measure relative to
the volume's median for pages of the same parity, so typeface and
layout differences between reporters cancel out.

The first half of the module is the line finder: the ink threshold,
the smear that merges the words of a line, the rule removers, the
column-edge tracks and the three warp scores. The second half is the
page features that read them.
"""

import multiprocessing as mp

import cv2
import numpy as np

REGIONS = [(c, b) for c in (0, 1) for b in (0, 1, 2)]
LINE_STATS = ["dens", "frag", "fused", "big", "medw", "p90w", "medh", "thick", "holes"]
RUN = 5                      # "worst run" length in lines
HEAD_ZONE = (40, 190)        # rows of the running-head zone at 200 dpi
CORNER_W = 300               # width of the corner boxes in the head zone
BORDER = 6                   # px: ink this close to the image edge counts as touching it
DPI = 200                    # the render the measures are calibrated for
SMEAR_W = 24          # horizontal closing width: joins words, not columns
MIN_LINE_W = 140      # ignore blobs narrower than this (page numbers, rules)
MIN_LINE_H = 6
MAX_LINE_H = 45       # taller blobs are merged lines / graphics / headers
SAMPLE_STEP = 8       # sample the centre line every N pixels of x
MAX_LINE_WOBBLE = 5.0 # rms deviation from a running median above this = not text
MIN_FRAG_W = 40       # fragments narrower than this are ignored even for merging
MERGE_GAP = 70        # same-row fragments closer than this are one line...
COL_START_MIN = 5     # ...unless a column edge (>= this many lines start there) lies between
RULE_LEN = 30         # ink runs at least this long and thin are rules/underlines
RULE_MAX_THICK = 7    # ...thin meaning at most this many rows (underlines are 3-5 with fuzz)
MIN_LINES = 6         # fewer detected lines than this -> leave page untouched
EDGE_MIN_LINES = 8    # lines needed to trust a column-edge track
EDGE_MIN_SPAN = 500   # px of page height the track must cover
EDGE_TOL_IN = 6       # a flush line may sit this far inside the fitted edge...
EDGE_TOL_OUT = 10     # ...or this far outside it (specks) and still count
EDGE_OUTSIDE = 14     # a line this far OUTSIDE a candidate edge says the candidate is wrong
EDGE_OUT_PENALTY = 6  # ...and costs this many inliers in the candidate's score
EDGE_MAX_SLOPE = 0.035  # px of sideways drift per px of height; steeper candidates are nonsense
CLIP_MARGIN = 15      # line starts/ends within this many px of the scan border are clipped, not flush
EXTREME_TRIM = 20     # starts below the 10th percentile (or ends above the 90th) by more than this are dropped
STEP_MIN_LINES = 20   # look for margin steps only in columns with at least this many lines
STEP_FLUSH_MIN = 4    # a margin block must have at least this many lines...
STEP_FLUSH_TOL = 8    # ...within this many px of its margin, or it is headings/captions, not a block
NEIGHBOUR_GAP = 120   # edges closer than this in x (the two sides of a column gap)...
NEIGHBOUR_TOL = 8     # ...must agree in drift shape to within this many px
COLUMN_TOL = 25       # the two edges of one column differing by more than this is suspicious...
COLUMN_REFIT = 30     # ...and the odd edge is overruled if it departs from the rest of the page by more than this
NEIGHBOUR_REFIT = 16  # same, for edges facing across a column gap
FAINT_DENSITY = 0.27  # a line below this is faint


def to_ink(gray):
    """Binary ink mask: 255 where ink, 0 where paper."""
    _, ink = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY_INV)
    return ink


def remove_vertical_rules(ink):
    """Drop tall thin components (column rules, margin marks)."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    out = ink.copy()
    for i in range(1, n):
        _, _, w, h, _ = stats[i]
        if h > 120 and w < 12:
            out[labels == i] = 0
    return out


def remove_horizontal_rules(ink):
    """Erase underlines and table rules: long ink runs no more than RULE_MAX_THICK rows thick."""
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (RULE_LEN, 1))
    long_runs = cv2.morphologyEx(ink, cv2.MORPH_OPEN, k)
    thick = cv2.morphologyEx(long_runs, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, RULE_MAX_THICK + 1)))
    rules = cv2.subtract(long_runs, thick)         # long AND thin
    rules = cv2.dilate(rules, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    return cv2.subtract(ink, rules)


def column_baseline(strip):
    """
    Baseline of a narrow vertical strip of a text line: the lowest row whose
    ink density is at least half the strip's densest row. Ignores descenders
    (thin) and is indifferent to whether the letters have ascenders, unlike a
    centroid. Returns a row index (float) or None if the strip is empty.
    """
    prof = strip.sum(axis=1).astype(float)
    if prof.sum() < 6:
        return None
    dense = np.nonzero(prof >= 0.5 * prof.max())[0]
    return float(dense[-1]) + 0.5


def running_median(v, k=5):
    pad = k // 2
    vp = np.pad(v, pad, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(vp, k), axis=1)


def find_lines(ink):
    """
    Return a list of arrays, one per detected text line, each of shape (n, 2)
    holding (x, y_centre) samples along that line, sorted by x.
    """
    ink = remove_horizontal_rules(remove_vertical_rules(ink))

    # join letters vertically a touch, then smear horizontally to fuse words
    k_v = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5))
    k_h = cv2.getStructuringElement(cv2.MORPH_RECT, (SMEAR_W, 1))
    smeared = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, k_v)
    smeared = cv2.morphologyEx(smeared, cv2.MORPH_CLOSE, k_h)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(smeared, connectivity=8)

    # 1. sample every text-like fragment
    frags = []   # (x0, x1, yc, h, pts)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if w < MIN_FRAG_W or h < MIN_LINE_H or h > MAX_LINE_H:
            continue
        region = (labels[y:y + h, x:x + w] == i)
        sub = (ink[y:y + h, x:x + w] > 0) & region   # real ink, not the smear
        pts = []
        for cx in range(0, w, SAMPLE_STEP):
            b = column_baseline(sub[:, max(0, cx - SAMPLE_STEP // 2):cx + SAMPLE_STEP + SAMPLE_STEP // 2])
            if b is not None:
                pts.append((x + cx + SAMPLE_STEP / 2.0, y + b))
        if len(pts) >= 3:
            pts = np.array(pts, dtype=np.float64)
            frags.append([x, x + w, np.median(pts[:, 1]), h, pts])

    # 2. where do columns start? x values where many fragments begin
    starts = np.array([f[0] for f in frags if f[1] - f[0] >= MIN_LINE_W])
    col_starts = []
    for sx in np.unique(starts // 6 * 6):
        if np.sum(np.abs(starts - sx) <= 8) >= COL_START_MIN:
            col_starts.append(sx)

    def crosses_column_edge(x_left_end, x_right_start):
        return any(x_left_end < c - 8 and c - 8 <= x_right_start + 8 for c in col_starts)

    # 3. merge fragments that sit on the same row with a small gap between
    frags.sort(key=lambda f: (f[2], f[0]))
    merged = []
    for f in sorted(frags, key=lambda f: f[0]):
        for m in merged:
            same_row = abs(m[2] - f[2]) < 0.6 * max(m[3], f[3])
            gap = f[0] - m[1]
            if same_row and -10 <= gap <= MERGE_GAP and not crosses_column_edge(m[1], f[0]):
                m[1] = max(m[1], f[1]); m[3] = max(m[3], f[3])
                m[4] = np.vstack([m[4], f[4]]); m[2] = np.median(m[4][:, 1])
                break
        else:
            merged.append(f)

    # 4. keep the ones that look like a line of text
    lines = []
    for x0, x1, _, _, pts in merged:
        if x1 - x0 < MIN_LINE_W or len(pts) < 6:
            continue
        pts = pts[np.argsort(pts[:, 0])]
        # a text line's centre follows a smooth curve (possibly a sharp one at
        # the spine); logos and rules wobble. Compare with a running median.
        wobble = pts[:, 1] - running_median(pts[:, 1])
        if np.sqrt((wobble ** 2).mean()) > MAX_LINE_WOBBLE:
            continue
        lines.append(pts)
    return lines


def line_extents(ink, lines):
    """
    Exact first/last ink column of each detected line, looked up in the text
    body just above the baseline. Returns array of (x_start, x_end, y).
    """
    H, W = ink.shape
    ext = []
    for l in lines:
        y = float(np.median(l[:, 1]))
        r0, r1 = int(max(0, y - 20)), int(min(H, y + 3))
        xa = int(max(0, l[0, 0] - SAMPLE_STEP - 16)); xb = int(min(W, l[-1, 0] + SAMPLE_STEP + 16))
        cols = np.nonzero((ink[r0:r1, xa:xb] > 0).any(axis=0))[0]
        if len(cols) == 0:
            ext.append((l[0, 0], l[-1, 0], y))
        else:
            ext.append((xa + cols[0], xa + cols[-1] + 1, y))
    return np.array(ext, dtype=np.float64)


def _peaks(v, min_count, min_sep=100, binw=6):
    """Modes of v, strongest first, at least min_sep apart."""
    if len(v) == 0:
        return []
    hist, edges = np.histogram(v, bins=np.arange(v.min() - binw / 2, v.max() + 1.5 * binw, binw))
    out = []
    for k in np.argsort(hist)[::-1]:
        if hist[k] < min_count:
            break
        c = edges[k] + binw / 2
        if all(abs(c - o) >= min_sep for o in out):
            out.append(c)
    return out


def _flush_stat(v, side):
    """
    The flush position of a group of lines: second-lowest start (or
    second-highest end), so one speck or one stray line cannot move it and a
    run of indented paragraph openings does not bias it.
    """
    v = np.sort(v)
    return float(v[1] if side == 0 else v[-2]) if len(v) >= 2 else float(v[0])


def split_blocks(x, y, side, win=8, jump=12, lean_allow=0.02):
    """
    Group a column's lines (starts or ends) into blocks of consistent margin.
    A block boundary is where the flush position of the next `win` lines
    differs from that of the previous `win` by more than `jump` px plus what
    lean alone could do over the vertical distance between the two windows
    (`lean_allow` px per px): e.g. the headnotes set in a narrower measure
    than the opinion, or a block quote indented on both sides. Returns a
    block id per line.
    """
    n = len(x)
    ids = np.zeros(n, int)
    if n < 2 * win or n < STEP_MIN_LINES:
        return ids
    order = np.argsort(y)
    xs, yy = x[order], y[order]
    diffs = np.zeros(n)
    for k in range(win, n - win + 1):
        before, after = xs[k - win:k], xs[k:k + win]
        dy = abs(np.median(yy[k:k + win]) - np.median(yy[k - win:k]))
        d = _flush_stat(after, side) - _flush_stat(before, side)
        thr = jump + lean_allow * dy
        diffs[k] = d / thr if abs(d) > thr else 0.0    # > 1 in magnitude means "a step here"
    sorted_ids = np.zeros(n, int)
    k = win
    while k <= n - win:
        if diffs[k] != 0.0:
            kk = k + int(np.argmax(np.abs(diffs[k:min(k + win, n - win + 1)])))
            sorted_ids[kk:] += 1
            k = kk + win
        else:
            k += 1
    ids[order] = sorted_ids
    return ids


def _margin_line(x, y, side, win=8):
    """
    The flush margin of one block as a straight line in y: per window of
    `win` consecutive lines take the flush statistic, then fit x = a + b*y
    through those points. This is what a block's own margin does under lean,
    so other blocks can be compared with it at their own height.
    """
    order = np.argsort(y)
    xs, ys = x[order], y[order]
    pts_y, pts_x = [], []
    for k in range(0, max(1, len(xs) - win + 1), max(1, win // 2)):
        seg_x, seg_y = xs[k:k + win], ys[k:k + win]
        if len(seg_x) >= 3:
            pts_y.append(float(np.median(seg_y))); pts_x.append(_flush_stat(seg_x, side))
    if len(pts_y) >= 2 and max(pts_y) - min(pts_y) > 200:
        return np.polyfit(pts_y, pts_x, 1)
    return np.array([0.0, _flush_stat(x, side)])


def destep(x, y, side):
    """
    Remove margin steps between blocks: shift every genuine block onto the
    largest block's margin, measured at the shifted block's own height (the
    margin leans too, so comparing with its position at the top of the page
    would turn lean into a spurious shift). Returns (x_destepped, block_ids).
    """
    if side == 1:                    # ends: short last lines would fake steps; a right-indented
        return x.copy(), np.zeros(len(x), int)   # block just reads as short lines, which is harmless
    ids = split_blocks(x, y, side)
    if ids.max() == 0:
        return x.copy(), ids
    offs = {b: _flush_stat(x[ids == b], side) for b in np.unique(ids)}
    # a genuine margin block has several lines sitting right on its margin; a
    # centred caption or a run of headings does not, and must not be shifted
    genuine = {b: int(np.sum((x[ids == b] >= offs[b] - 2) & (x[ids == b] <= offs[b] + STEP_FLUSH_TOL))) >= STEP_FLUSH_MIN
               for b in offs}
    primary = max(offs, key=lambda b: ((ids == b).sum() if genuine[b] else -1))
    if not genuine[primary]:
        return x.copy(), np.zeros(len(x), int)
    line = _margin_line(x[ids == primary], y[ids == primary], side)
    shift = np.zeros(len(x))
    for b in offs:
        if b != primary and genuine[b]:
            sel = ids == b
            shift[sel] = offs[b] - np.polyval(line, np.median(y[sel]))
    ids = np.where([genuine[b] for b in ids], ids, primary)     # non-genuine blocks stay raw, count as primary
    return x - shift, ids


def _ransac_track(x, y, side, min_lines):
    """
    Find the column edge among line starts (side 0) or ends (side 1).
    Every pair of lines far enough apart in y proposes a straight edge. A
    proposal scores +1 for each line hugging it from the inside (flush text)
    and -EDGE_OUT_PENALTY for each line clearly OUTSIDE it, which real text
    cannot do: that is what rules out proposals running through indented
    lists or centred headings. The best proposal is refined into a low-order
    polynomial through its inliers.
    """
    lo_tol, hi_tol = (-EDGE_TOL_OUT, EDGE_TOL_IN) if side == 0 else (-EDGE_TOL_IN, EDGE_TOL_OUT)
    n = len(x)
    best_score, best_keep = None, None
    for i in range(n):
        dy = y - y[i]
        far = np.abs(dy) >= 300
        for j in np.nonzero(far)[0]:
            if j <= i:
                continue
            slope = (x[j] - x[i]) / (y[j] - y[i])
            if abs(slope) > EDGE_MAX_SLOPE:
                continue
            r = x - (x[i] + slope * dy)
            inl = (r >= lo_tol) & (r <= hi_tol)
            outs = (r < -EDGE_OUTSIDE) if side == 0 else (r > EDGE_OUTSIDE)
            score = (int(inl.sum()) - EDGE_OUT_PENALTY * int(outs.sum()), -int(outs.sum()), int(inl.sum()))
            if best_score is None or score > best_score:
                best_score, best_keep = score, inl
    if best_keep is None or best_keep.sum() < 6:
        return None
    keep = best_keep
    co = None
    for _ in range(3):
        deg = 2 if keep.sum() >= 15 else 1
        co = np.polyfit(y[keep], x[keep], deg)
        r = x - np.polyval(co, y)
        keep = (r >= lo_tol) & (r <= hi_tol)
        if keep.sum() < 6:
            return None
    outs = (r < -EDGE_OUTSIDE) if side == 0 else (r > EDGE_OUTSIDE)
    if keep.sum() < min_lines or y[keep].max() - y[keep].min() < EDGE_MIN_SPAN or outs.sum() > 0.3 * keep.sum():
        return None
    return {"co": co, "lo": float(y[keep].min()), "hi": float(y[keep].max()), "n": int(keep.sum()), "keep": keep}


def _refit_with_shape(x, y, rows, blocks, side, lo_tol, hi_tol, ygrid, ref, how):
    """
    Fit an edge whose own lines cannot be trusted to settle it, guided by a
    reference drift shape ref(ygrid) (zero-mean): de-skew the starts/ends by
    that shape, take the outermost populated cluster as the flush edge, and
    return the reference shape offset onto it. The edge's own lines choose
    only the offset, never the shape: a free refit would just re-absorb the
    very drift we decided not to believe.
    """
    s_ = x - np.interp(y, ygrid, ref)
    hist, edges = np.histogram(s_, bins=np.arange(s_.min() - 4, s_.max() + 12, 8))
    idx = [k for k in range(len(hist)) if hist[k] >= 3]
    if not idx:
        return None
    k = idx[0] if side == 0 else idx[-1]
    mode = edges[k] + 4
    sel = (s_ >= mode - 12) & (s_ <= mode + 12)
    if sel.sum() < 3:
        return None
    co = np.polyfit(ygrid, ref + np.median(s_[sel]), 2)
    return {"co": co, "lo": float(y[sel].min()), "hi": float(y[sel].max()), "n": int(sel.sum()),
            "how": how, "rows": rows[sel], "blocks": blocks[sel]}


def _shape(t, yy):
    v = np.polyval(t["co"], np.clip(yy, t["lo"], t["hi"]))
    return v - v.mean()


def edge_tracks(ext, width=None):
    """
    Fit every column edge as a smooth x(y) curve. Returns [dict] with keys
    co (poly coefs), lo, hi (y extent), n, col, side (0 start / 1 end), how,
    rows (indices into ext of the lines the edge was fitted on).

    Left edges are the LOWER envelope of line starts (indented lines lie to
    the right); right edges are the UPPER envelope of line ends (short last
    lines lie to the left). Lines are assigned to columns by their start.

    Pass 1 finds each edge by RANSAC over the column's lines (_ransac_track).
    Pass 2 handles columns too sparse for that (a few body lines among
    headings): their edge borrows the drift shape shared by the edges found.
    Pass 3 enforces physical consistency: the two edges facing each other
    across a column gap must lean alike (they are a few dozen px apart), and
    the two edges of one column may not drift apart by more than a column
    width can plausibly change. When a pair disagrees, the edge that is
    unlike the rest of the page is refitted with the others' drift shape as a
    guide. This defeats diagonals drawn through hanging-indent blocks (party
    captions, rules blocks, numbered holdings).
    """
    starts, ends, ys = ext[:, 0], ext[:, 1], ext[:, 2]
    cols = sorted(_peaks(starts, COL_START_MIN, min_sep=300, binw=12))
    if not cols:
        return []
    assign = np.argmin(np.abs(starts[:, None] - np.array(cols)[None, :]), axis=1)
    members, tracks, failed = {}, [], []
    for ci, c in enumerate(cols):
        m = (assign == ci) & (starts >= c - 60)       # not a running head / stray start
        if ci + 1 < len(cols):                        # drop lines merged across the gap
            m &= ends < cols[ci + 1] + 100            # (loose: bottom lines drift right)
        if m.sum() < 3:
            continue
        for side in (0, 1):
            ms = m.copy()
            if width is not None:                    # text running off the scan has no edge there
                ms &= (starts > CLIP_MARGIN) if side == 0 else (ends < width - CLIP_MARGIN)
            # a line starting well left of nearly all others, or ending well
            # right of them, is a speck or a merge with a marginal mark, not
            # an edge; it would otherwise count as "outside" every candidate
            if ms.sum() >= 8:
                v = starts[ms] if side == 0 else ends[ms]
                if side == 0:
                    ms &= starts >= np.percentile(v, 10) - EXTREME_TRIM
                else:
                    ms &= ends <= np.percentile(v, 90) + EXTREME_TRIM
            if ms.sum() < 3:
                failed.append((ci, side)) if (ci, side) not in members else None
                continue
            rows = np.nonzero(ms)[0]
            x_raw = starts[ms] if side == 0 else ends[ms]
            y = ys[ms]
            x, blocks = destep(x_raw, y, side)       # margin steps are layout, not lean
            lo_tol, hi_tol = (-EDGE_TOL_OUT, EDGE_TOL_IN) if side == 0 else (-EDGE_TOL_IN, EDGE_TOL_OUT)
            members[(ci, side)] = (x, y, rows, lo_tol, hi_tol, blocks)
            t = _ransac_track(x, y, side, EDGE_MIN_LINES) if len(x) >= EDGE_MIN_LINES else None
            if t:
                keep = t.pop("keep")
                t.update(col=ci, side=side, how="ransac", rows=rows[keep], blocks=blocks[keep],
                         steps=int(blocks.max()))
                tracks.append(t)
            else:
                failed.append((ci, side))

    ygrid = np.linspace(ys.min(), ys.max(), 50)
    if tracks and failed:                              # pass 2
        ref = np.mean([_shape(t, ygrid) for t in tracks], axis=0)
        for ci, side in failed:
            x, y, rows, lo_tol, hi_tol, blocks = members[(ci, side)]
            t = _refit_with_shape(x, y, rows, blocks, side, lo_tol, hi_tol, ygrid, ref, "de-skewed")
            if t:
                t.update(col=ci, side=side, steps=int(blocks.max()))
                tracks.append(t)

    accepted = set()                                   # pass 3
    for _ in range(8):
        ymid = 0.5 * (ys.min() + ys.max())
        clash = None
        for i, a in enumerate(tracks):
            for b in tracks[i + 1:]:
                if (id(a), id(b)) in accepted:
                    continue
                gap = abs(np.polyval(b["co"], ymid) - np.polyval(a["co"], ymid))
                if gap <= NEIGHBOUR_GAP:
                    tol, refit_if = NEIGHBOUR_TOL, NEIGHBOUR_REFIT      # facing across a column gap
                elif a["col"] == b["col"] and a["side"] != b["side"]:
                    tol, refit_if = COLUMN_TOL, COLUMN_REFIT            # two sides of one column
                else:
                    continue
                y0, y1 = max(a["lo"], b["lo"]), min(a["hi"], b["hi"])
                if y1 - y0 < 300:
                    continue
                yy = np.linspace(y0, y1, 20)
                dev = float(np.ptp(_shape(a, yy) - _shape(b, yy)))   # how far the two drifts diverge
                if dev > tol and (clash is None or dev > clash[0]):
                    clash = (dev, a, b, refit_if)
        if clash is None:
            break
        _, a, b, refit_if = clash
        others = [t for t in tracks if t is not a and t is not b]
        if len(others) >= 2:
            # the odd one out is the edge unlike the rest of the page; only a
            # gross departure gets overruled (genuine differential lean is
            # ~10-30 px across a page, bogus diagonals through indent levels
            # are 40-60 px and point the other way)
            ref = np.median([_shape(t, ygrid) for t in others], axis=0)
            dev_a = float(np.ptp(_shape(a, ygrid) - ref)); dev_b = float(np.ptp(_shape(b, ygrid) - ref))
            weak, strong, dev_w = ((a, b, dev_a) if dev_a > dev_b else (b, a, dev_b))
            if dev_w <= refit_if:
                accepted.add((id(a), id(b)))
                continue
        else:
            weak, strong = (a, b) if a["n"] < b["n"] else (b, a)
            ref = _shape(strong, ygrid)
        tracks = [t for t in tracks if t is not weak]
        x, y, rows, lo_tol, hi_tol, blocks = members[(weak["col"], weak["side"])]
        t = _refit_with_shape(x, y, rows, blocks, weak["side"], lo_tol, hi_tol, ygrid, ref, "consistency")
        if t:
            t.update(col=weak["col"], side=weak["side"], steps=int(blocks.max()))
            tracks.append(t)
    return tracks


def lean_score(tracks):
    """RMS sideways drift of the edge tracks from their top to their bottom, px."""
    d = [np.polyval(t["co"], t["hi"]) - np.polyval(t["co"], t["lo"]) for t in tracks]
    return float(np.sqrt(np.mean(np.square(d)))) if d else float("nan")


def slope_score(lines, min_w=300):
    """RMS slope of the detected lines, in px per 1000 px. 0 = perfectly flat."""
    sl = [np.polyfit(l[:, 0], l[:, 1], 1)[0] * 1000
          for l in lines if l[:, 0].max() - l[:, 0].min() >= min_w]
    return float(np.sqrt(np.mean(np.square(sl)))) if sl else float("nan")


def bend_score(lines, min_w=300):
    """
    RMS of each line's end 'bend': how far its first/last 100 px sit from the
    straight-line fit of the rest. Catches the spine curl that a slope score
    averages away. px.
    """
    b = []
    for l in lines:
        if l[:, 0].max() - l[:, 0].min() < min_w:
            continue
        lo, hi = l[:, 0].min() + 100, l[:, 0].max() - 100
        body = l[(l[:, 0] >= lo) & (l[:, 0] <= hi)]
        head, tail = l[l[:, 0] < lo], l[l[:, 0] > hi]
        if len(body) < 6:
            continue
        c = np.polyfit(body[:, 0], body[:, 1], 1)
        for part in (head, tail):
            if len(part) >= 3:
                b.append(np.mean(part[:, 1] - np.polyval(c, part[:, 0])))
    return float(np.sqrt(np.mean(np.square(b)))) if b else float("nan")


def line_features(clean, x0, x1, y):
    band = (clean[int(max(0, y - 22)):int(y + 8), int(x0):int(x1)] > 0).astype(np.uint8)   # full glyph height
    n, _, st, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
    st = st[1:]
    if len(st) < 3:
        return None
    area, wd, ht = st[:, 4], st[:, 2], st[:, 3]
    xb = clean[int(max(0, y - 14)):int(y + 3), int(x0):int(x1)] > 0                          # x-height band
    dist = cv2.distanceTransform(band, cv2.DIST_L2, 3)
    thick = float(np.median(dist[band > 0]) * 2) if band.any() else 0.0                      # typical stroke width
    holes = cv2.connectedComponentsWithStats((1 - band).astype(np.uint8), connectivity=4)[0] - 2   # enclosed counters
    return dict(dens=float(xb.mean()), frag=(n - 1) / ((x1 - x0) / 100.0),
                fused=float(area[wd > 28].sum() / area.sum()), big=float(area[(wd > 28) & (ht > 20)].sum() / area.sum()),
                medw=float(np.median(wd)), p90w=float(np.percentile(wd, 90)), medh=float(np.median(ht)),
                thick=thick, holes=max(0, holes) / ((x1 - x0) / 100.0))


def worst_run(values, k=RUN, high_is_bad=True):
    """Mean over the worst k consecutive values (lines in reading order within a column)."""
    v = np.asarray(values, float)
    if len(v) == 0:
        return float("nan")
    if len(v) <= k:
        return float(v.mean())
    runs = np.convolve(v, np.ones(k) / k, mode="valid")
    return float(runs.max() if high_is_bad else runs.min())


def page_features(gray):
    h, w = gray.shape
    ink = to_ink(gray)
    ink01 = ink > 0
    lines = find_lines(ink)
    f = {"lines": len(lines), "slope": slope_score(lines), "bend": bend_score(lines), "lean": float("nan"),
         "ink_frac": float(ink01.mean())}

    # ---- crop / chip: running-head corners, borders ------------------------------------------
    # the running head is the first band of ink below the top margin; its page number sits in a corner
    # a head row carries real ink (>= 40 px) and the head is at least 6 rows tall; specks and border lines are not
    inked = ink01[:450, :].sum(axis=1) >= 40
    inked[:15] = False
    run = np.convolve(inked.astype(int), np.ones(6, int), mode="valid") == 6
    head_end = 0
    if run.any():
        r0 = int(np.argmax(run))
        hz = ink01[max(0, r0 - 5):r0 + 60, :]
        head_end = r0 + 60
    else:
        hz = ink01[HEAD_ZONE[0]:HEAD_ZONE[1], :]
    # the page number sits just outside the text block's edge, so the corner boxes reach 150 px past it
    ext_all = line_extents(ink, lines) if lines else np.zeros((0, 3))
    xl = int(min(w / 2, (ext_all[:, 0].min() + 150) if len(ext_all) else CORNER_W))
    xr = int(max(w / 2, (ext_all[:, 1].max() - 150) if len(ext_all) else w - CORNER_W))
    f["head_ink_left"] = float(hz[:, :xl].sum()); f["head_ink_right"] = float(hz[:, xr:].sum())
    f["head_ink_mid"] = float(hz[:, xl:xr].sum())
    f["head_y"] = float(head_end - 60) if head_end else float("nan")
    f["border_top"] = float(ink01[:BORDER, :].any(axis=0).mean()); f["border_bottom"] = float(ink01[h - BORDER:, :].any(axis=0).mean())
    f["border_left"] = float(ink01[:, :BORDER].any(axis=1).mean()); f["border_right"] = float(ink01[:, w - BORDER:].any(axis=1).mean())
    # ink in each page corner box (outside the head zone too: torn corners take text with them)
    cb = 350
    f["corner_tl"] = float(ink01[:cb, :cb].sum()); f["corner_tr"] = float(ink01[:cb, w - cb:].sum())
    f["corner_bl"] = float(ink01[h - cb:, :cb].sum()); f["corner_br"] = float(ink01[h - cb:, w - cb:].sum())

    per = []
    for k in ("margin_left", "margin_right", "margin_top", "margin_bottom", "first_line_indent_l", "first_line_indent_r",
              "first_line_y_l", "first_line_y_r", "faint_frac"):
        f[k] = float("nan")
    if len(lines) >= MIN_LINES:
        clean = remove_horizontal_rules(remove_vertical_rules(ink))
        ext = line_extents(clean, lines)
        tracks = edge_tracks(ext, w)
        f["lean"] = lean_score(tracks)
        f["margin_left"] = float(ext[:, 0].min()); f["margin_right"] = float(w - ext[:, 1].max())
        f["margin_top"] = float(ext[:, 2].min()); f["margin_bottom"] = float(h - ext[:, 2].max())
        # first line of each column vs that column's flush edge (a chip that eats the first word delays the start)
        for col, key in ((0, "l"), (1, "r")):
            body = ext[ext[:, 2] > head_end + 40] if head_end else ext      # below the running head
            sel = body[(body[:, 0] < w / 2) if col == 0 else (body[:, 0] >= w / 2)]
            edge = [t for t in tracks if t["col"] == col and t["side"] == 0]
            if len(sel) and edge:
                top = sel[np.argmin(sel[:, 2])]
                f[f"first_line_indent_{key}"] = float(top[0] - np.polyval(edge[0]["co"], top[2]))
                f[f"first_line_y_{key}"] = float(top[2])
        for x0, x1, y in ext:
            if x1 - x0 < 300:
                continue
            lf = line_features(clean, x0, x1, y)
            if lf:
                lf["col"] = int(x0 >= w / 2); lf["band"] = min(2, int(3 * y / h)); lf["y"] = y; per.append(lf)
        if per:
            f["faint_frac"] = float(np.mean([p["dens"] < FAINT_DENSITY for p in per]))

    # ---- page-level and region summaries of the per-line measures ---------------------------
    for k in LINE_STATS:
        v = np.array([p[k] for p in per]) if per else np.array([])
        f[f"{k}_med"] = float(np.median(v)) if len(v) else float("nan")
        f[f"{k}_p90"] = float(np.percentile(v, 90)) if len(v) else float("nan")
        f[f"{k}_p10"] = float(np.percentile(v, 10)) if len(v) else float("nan")
        meds = []
        for c, b in REGIONS:
            rv = np.array([p[k] for p in per if p["col"] == c and p["band"] == b])
            if len(rv) >= 3:
                meds.append(float(np.median(rv)))
        f[f"{k}_rmax"] = max(meds) if meds else float("nan")
        f[f"{k}_rmin"] = min(meds) if meds else float("nan")
        f[f"{k}_rspread"] = (max(meds) - min(meds)) if meds else float("nan")
        # worst run of RUN consecutive lines within a column, both directions
        hi, lo = [], []
        for c in (0, 1):
            col = sorted((p for p in per if p["col"] == c), key=lambda p: p["y"])
            vals = [p[k] for p in col]
            if len(vals) >= 3:
                hi.append(worst_run(vals, RUN, True)); lo.append(worst_run(vals, RUN, False))
        f[f"{k}_runmax"] = max(hi) if hi else float("nan")
        f[f"{k}_runmin"] = min(lo) if lo else float("nan")
    f["regions"] = sum(1 for c, b in REGIONS if sum(1 for p in per if p["col"] == c and p["band"] == b) >= 3)
    return f


_DOC = None


def _init(path):
    global _DOC
    import fitz
    cv2.setNumThreads(1)          # one OpenCV thread per worker process; the pool provides the parallelism
    _DOC = fitz.open(path)


def _work(pageno):
    import fitz
    pix = _DOC[pageno - 1].get_pixmap(dpi=DPI, colorspace=fitz.csGRAY)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    return pageno, page_features(gray)


def features_of_pdf(path, jobs=None):
    """Return ``{page: features}`` for every page of a PDF, 1-based.

    One worker process per CPU by default, each with its own open
    document; the pool is the parallelism, so OpenCV runs one thread
    per worker.

    :param path: The PDF to measure.
    :param jobs: Worker processes; None is the CPU count.
    :returns: The features of every page, keyed by 1-based page.
    :rtype: dict[int, dict]
    """
    import fitz

    with fitz.open(path) as doc:
        n = len(doc)
    rows = {}
    # Spawned, not forked: a forked worker inherits the daemon's SIGTERM
    # handler, which re-queues scans over the parent's database socket,
    # and a worker still alive at Pool.terminate() then hangs the join.
    ctx = mp.get_context("spawn")
    with ctx.Pool(jobs, initializer=_init, initargs=(str(path),)) as pool:
        for p, f in pool.imap_unordered(_work, range(1, n + 1), chunksize=8):
            rows[p] = f
    return rows
