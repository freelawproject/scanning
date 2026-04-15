"""Small reusable helpers shared across views and services."""

from __future__ import annotations

import itertools
import os
from pathlib import Path

from django.shortcuts import get_object_or_404

from scanning.models import Scan, Volume


def get_volume(reporter_slug: str, vol: int) -> Volume:
    """Look up a Volume by reporter slug and number.

    :param reporter_slug: The reporter's short name.
    :param vol: The volume number.
    :return: The matching Volume instance.
    """
    return get_object_or_404(
        Volume.objects.select_related("reporter", "assigned_to"),
        reporter__short_name=reporter_slug,
        volume_number=vol,
    )


def find_json_file(output_base: Path, filename: str) -> Path | None:
    """Search output_base and its parents for a JSON file.

    :param output_base: The directory to start searching from.
    :param filename: The JSON filename to look for.
    :return: The Path if found, or None.
    """
    for candidate in [
        output_base / filename,
        output_base.parent / filename,
        output_base.parent.parent / filename,
    ]:
        if candidate.exists():
            return candidate
    return None


def find_ocr_pdf(output_dir: str | Path) -> Path | None:
    """Find the OCR PDF in output_dir (excludes bitonal, redacted, original).

    :param output_dir: The directory to search for the OCR PDF.
    :return: The Path if found, or None.
    """
    for f in sorted(Path(output_dir).glob("*.pdf")):
        if (
            f.name not in ("bitonal.pdf", "stamped.pdf")
            and not f.name.endswith(".redacted.pdf")
            and not f.name.endswith(".original.pdf")
            and "redacted" not in f.name
            and "bitonal" not in f.name
        ):
            return f
    return None


def has_s3_credentials() -> bool:
    """Check whether AWS credentials are configured.

    :return: True if the required AWS env vars are set.
    :rtype: bool
    """
    return bool(
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_DEV_ACCESS_KEY_ID")
    ) and bool(
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("AWS_DEV_SECRET_ACCESS_KEY")
    )


def ensure_output_dir(scan: Scan) -> Path:
    """Return the scan's output directory, creating it if it doesn't exist.

    The path is computed by ``Scan.output_dir`` (a property).

    :param scan: The Scan instance.
    :return: The output directory as a Path.
    :rtype: Path
    """
    output_dir = Path(scan.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def compute_coverage_gaps(
    opinions: list[dict],
    start_page: int | None,
    end_page: int | None,
) -> list[tuple[int, int, int]]:
    """Compute contiguous page runs not covered by any paired opinion.

    A page in ``[start_page, end_page]`` is covered if it falls within
    an opinion's ``caption_page`` through ``key_page + page_count - 1``
    span (opinion page indices are offset by ``start_page``).

    :param opinions: List of opinion dicts with caption_page, key_page,
        and page_count.
    :param start_page: First page in the scan's expected range.
    :param end_page: Last page in the scan's expected range.
    :returns: List of ``(start, end, count)`` tuples, one per gap run.
    :rtype: list[tuple[int, int, int]]
    """
    if not opinions or not start_page or not end_page:
        return []

    covered: set[int] = set()
    for op in opinions:
        cp = op.get("caption_page", 0) + (start_page or 1)
        kp = (
            op.get("key_page", 0)
            + (start_page or 1)
            + op.get("page_count", 1)
            - 1
        )
        for p in range(cp, kp + 1):
            covered.add(p)
    expected = set(range(start_page, end_page + 1))
    missing = sorted(expected - covered)
    if not missing:
        return []

    gaps = []
    for _, g in itertools.groupby(enumerate(missing), lambda x: x[0] - x[1]):
        run = [v for _, v in g]
        gaps.append((run[0], run[-1], len(run)))
    return gaps
