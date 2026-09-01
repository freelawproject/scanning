"""Group compound page completeness issues (issue #227).

One OCR artifact -- a stray number read on many pages -- makes
``blackletter.validate.build_issues`` write many cards for one fact:
one ``missing_page`` error per overlapping gap, one ``backward_page``
warning per occurrence, and so on. This pass collapses those cards
into one card per fact. It runs right after ``build_issues`` and
before the dismissal filter, so the dismissal match sees the grouped
cards and the cards scanning appends afterwards pass through.

The pass parses no message prose: ``services.recalculate_issues``
holds the ``build_analysis`` output, and the sequence tuples in
``analysis["seq_issues"]`` carry the structure the messages were
built from. A grouped card puts its page list in ``metadata`` (the
``Issue.metadata`` JSONField): the viewer reads
``metadata["pdf_pages"]`` for navigation and highlights, and
``dismiss_issue`` fans a physical dismissal out over it.

Invariants:
- Only the issue list changes. ``page_map`` and ``missing_pages`` do
  not pass through here.
- A card the pass does not recognize passes through unchanged.
- The duplicate card keeps the ``[pages]`` bracket list in its
  message: ``deleteDuplicates`` in the viewer parses it.
- The dismissal key (check name plus one page) keeps working. A
  grouped card keeps a ``page_number`` in its check's own space.
"""

import itertools
from bisect import bisect_left


def _join_pages(pages: list[int]) -> str:
    """Format a page list for prose: ``[3, 5, 7]`` -> ``"3, 5 and 7"``.

    :param pages: The page numbers, already sorted.
    :returns: The formatted list.
    :rtype: str
    """
    parts = [str(p) for p in pages]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _detected_sequence(analysis: dict) -> list[tuple[int, int]]:
    """List the in-range single readings as ``(pdf_page, number)``.

    Mirrors the filter of the sequence walk in
    ``blackletter.validate.build_analysis``: no blank pages, no
    ranges, no out-of-range pages, no unparsable values.

    :param analysis: The ``build_analysis`` output.
    :returns: The readings, sorted by ``pdf_page``.
    :rtype: list[tuple[int, int]]
    """
    out_pages = {r["pdf_page"] for r in analysis.get("out_of_range", [])}
    sequence = []
    for r in analysis.get("results", []):
        if not r.get("detected") or r.get("type") == "range":
            continue
        if r["pdf_page"] in out_pages:
            continue
        try:
            num = int(r["detected"])
        except (TypeError, ValueError):
            continue
        sequence.append((r["pdf_page"], num))
    sequence.sort()
    return sequence


def _no_number_candidates(analysis: dict) -> dict[int, list[int]]:
    """Map each interpolated printed number to its undetected pages.

    For each page without a detected number, the detected neighbors
    say which printed number the page would hold: the previous number
    plus the physical distance. When the next neighbor exists, it
    must agree, or the page names no number. A missing printed number
    with exactly one such page is probably that page, read blank --
    the screenshot-3 case of #227.

    :param analysis: The ``build_analysis`` output.
    :returns: ``{printed number: [pdf pages]}``.
    :rtype: dict[int, list[int]]
    """
    sequence = _detected_sequence(analysis)
    pages = [p for p, _ in sequence]
    candidates: dict[int, list[int]] = {}
    for r in analysis.get("not_detected", []):
        page = r["pdf_page"]
        pos = bisect_left(pages, page)
        before = sequence[pos - 1] if pos > 0 else None
        after = sequence[pos] if pos < len(sequence) else None
        if before and after:
            expected = before[1] + (page - before[0])
            if after[1] - (after[0] - page) != expected:
                continue
        elif before:
            expected = before[1] + (page - before[0])
        elif after:
            expected = after[1] - (after[0] - page)
        else:
            continue
        candidates.setdefault(expected, []).append(page)
    return candidates


def _coverage_spans(analysis: dict) -> dict[int, tuple[int, int]]:
    """Find the large runs of missing numbers, keyed by their start.

    Mirrors the run grouping in ``build_issues``, which writes one
    ``large_gap`` card per run of more than six consecutive missing
    numbers, with ``page_number = run[0]``.

    :param analysis: The ``build_analysis`` output.
    :returns: ``{run start: (start, end)}``.
    :rtype: dict[int, tuple[int, int]]
    """
    missing = [p for p in analysis.get("missing_pages", []) if p >= 1]
    spans: dict[int, tuple[int, int]] = {}
    for _, group in itertools.groupby(
        enumerate(missing), lambda item: item[0] - item[1]
    ):
        run = [value for _, value in group]
        if len(run) > 6:
            spans[run[0]] = (run[0], run[-1])
    return spans


def _with_metadata(card: dict, **extra) -> dict:
    """Copy one card and extend its ``metadata`` dict.

    :param card: The issue dict to copy.
    :param extra: The metadata keys to add.
    :returns: The copied card.
    :rtype: dict
    """
    card = dict(card)
    metadata = dict(card.get("metadata") or {})
    metadata.update(extra)
    card["metadata"] = metadata
    return card


def group_issues(issues: list[dict], analysis: dict) -> list[dict]:
    """Collapse the compound cards into one card per fact (#227).

    :param issues: The ``build_issues`` output list.
    :param analysis: The ``build_analysis`` output the list was built
        from.
    :returns: The grouped list, in the original card order.
    :rtype: list[dict]
    """
    # The structure behind the repeated cards, from the raw tuples.
    gap_pairs: dict[int, list[tuple[int, int]]] = {}
    backward_pages: dict[int, list[int]] = {}
    seq_spans: dict[int, list[tuple[int, int]]] = {}
    for seq in analysis.get("seq_issues", []):
        if seq[0] == "GAP":
            _kind, _pdf_page, num, _prev_pdf, prev_num, gap = seq
            if len(gap) > 6:
                seq_spans.setdefault(prev_num, []).append(
                    (prev_num + 1, num - 1)
                )
                continue
            for gap_num in gap:
                if gap_num >= 1:
                    gap_pairs.setdefault(gap_num, []).append((prev_num, num))
        elif seq[0] == "BACKWARD":
            _kind, pdf_page, num, _prev_pdf, _prev_num = seq
            backward_pages.setdefault(num, []).append(pdf_page)

    stray_value_by_page: dict[int, str] = {}
    stray_pages_by_value: dict[str, list[int]] = {}
    for r in analysis.get("out_of_range", []):
        value = str(r.get("detected"))
        stray_value_by_page[r["pdf_page"]] = value
        stray_pages_by_value.setdefault(value, []).append(r["pdf_page"])

    # The missing/no-number merge (rule 6): a missing number with
    # exactly one candidate page absorbs that page's info card,
    # wherever the two cards sit in the list.
    candidates = _no_number_candidates(analysis)
    missing_numbers = {
        card.get("page_number")
        for card in issues
        if card.get("check_name") == "missing_page"
    }
    merges = {
        number: pages[0]
        for number, pages in candidates.items()
        if number in missing_numbers and len(pages) == 1
    }
    absorbed_pages = set(merges.values())

    coverage_spans = _coverage_spans(analysis)
    detected_numbers = set(analysis.get("seen_nums", {}))

    grouped: list[dict] = []
    seen: dict[str, set] = {
        "missing_page": set(),
        "backward_page": set(),
        "duplicate_page": set(),
        "large_gap": set(),
    }
    seen_stray_values: set[str] = set()
    kept_spans: list[tuple[int, int]] = []

    for card in issues:
        check = card.get("check_name")
        page = card.get("page_number")

        if page is None:
            # No grouped check writes a page-less card; the ones that
            # do (mislabeled_document) pass through.
            grouped.append(card)

        elif check == "missing_page":
            if page in seen["missing_page"]:
                continue
            seen["missing_page"].add(page)
            if page in detected_numbers:
                # The stray number that made the gap also put a
                # detected copy of this number inside it, so the
                # number is on some page. The duplicate or backward
                # card carries the suspicion; a "missing" card for a
                # number the volume shows is noise.
                continue
            pairs = sorted(set(gap_pairs.get(page, [])))
            extra: dict = {}
            if pairs:
                extra["gaps"] = [[prev, num] for prev, num in pairs]
            if page in merges:
                extra["pdf_pages"] = [merges[page]]
            if extra:
                card = _with_metadata(card, **extra)
            if page in merges:
                card["message"] = (
                    f"Page {page} is missing; PDF page {merges[page]} "
                    f"has no detected number and may be it."
                )
            elif pairs:
                prev, num = min(pairs, key=lambda pair: pair[1] - pair[0])
                message = (
                    f"Page {page} appears missing "
                    f"(gap between {prev} and {num})."
                )
                if len(pairs) > 1:
                    message += f" Reported by {len(pairs)} gaps."
                card["message"] = message
            grouped.append(card)

        elif check == "backward_page":
            if page in seen["backward_page"]:
                continue
            seen["backward_page"].add(page)
            pages = sorted(set(backward_pages.get(page, [])))
            if pages:
                card = _with_metadata(card, pdf_pages=pages)
            if len(pages) > 1:
                card["message"] = (
                    f"Page {page} goes backward on PDF pages "
                    f"{_join_pages(pages)}."
                )
            grouped.append(card)

        elif check == "duplicate_page":
            if page in seen["duplicate_page"]:
                continue
            seen["duplicate_page"].add(page)
            pages = sorted(analysis.get("duplicates", {}).get(page, []))
            if pages:
                # The coverage-form message, whichever form the card
                # had: the viewer's delete button parses the bracket
                # list, and the sequence form has none.
                card = _with_metadata(card, pdf_pages=pages)
                card["message"] = (
                    f"Page number {page} found on PDF pages {pages}."
                )
            grouped.append(card)

        elif check == "suspicious_reading":
            value = stray_value_by_page.get(page)
            if value is None:
                grouped.append(card)
                continue
            pages = sorted(set(stray_pages_by_value.get(value, [])))
            if len(pages) <= 1:
                grouped.append(card)
                continue
            if value in seen_stray_values:
                continue
            seen_stray_values.add(value)
            card = _with_metadata(card, pdf_pages=pages)
            card["page_number"] = pages[0]
            card["message"] = (
                f"PDF pages {_join_pages(pages)} detected as '{value}' "
                f"which is outside the expected page range, likely a "
                f"stray number."
            )
            grouped.append(card)

        elif check == "large_gap":
            if page in seen["large_gap"]:
                continue
            seen["large_gap"].add(page)
            spans = list(seq_spans.get(page, []))
            if page in coverage_spans:
                spans.append(coverage_spans[page])
            span = min(spans, key=lambda s: s[1]) if spans else None
            if span and any(
                start <= span[1] and span[0] <= end
                for start, end in kept_spans
            ):
                continue
            if span:
                kept_spans.append(span)
            grouped.append(card)

        elif check == "no_page_number":
            if page in absorbed_pages:
                continue
            grouped.append(card)

        else:
            grouped.append(card)

    return grouped
