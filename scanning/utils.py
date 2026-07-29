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

    The pipeline no longer produces one: the ocrmypdf/Tesseract text-layer
    pass was dropped from ``run_full_pipeline``. Only scans processed
    before that change still have this file, so callers that just want a
    workable PDF should use :func:`find_processing_pdf` instead.

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


def find_processing_pdf(output_dir: str | Path) -> Path | None:
    """Find the small PDF the pipeline works on.

    ``bitonal.pdf`` is the canonical one: it has the exact page geometry
    of the original (``_render_bitonal_page`` creates each page from the
    source page's rect) at a fraction of the size, so anything that only
    needs page dimensions, detection coordinates, or a preview can use it.

    Scans processed before the Tesseract text-layer pass was dropped from
    the pipeline also carry an OCR'd PDF in the same directory. It is
    preferred when present: same geometry, plus a text layer that still
    gives tighter margin bounds.

    :param output_dir: The scan's output directory.
    :return: The OCR'd PDF, else ``bitonal.pdf``, else None.
    """
    ocr_pdf = find_ocr_pdf(output_dir)
    if ocr_pdf:
        return ocr_pdf
    bitonal = Path(output_dir) / "bitonal.pdf"
    return bitonal if bitonal.exists() else None


def processing_pdf_path(scan: Scan) -> str:
    """Resolve the PDF path pipeline steps should read for a scan.

    Prefers the small processing PDF (see :func:`find_processing_pdf`) and
    falls back to the multi-GB original only when no processing PDF exists
    locally, which in production means an S3 pull, so callers should treat
    that as the slow path rather than the normal one.

    :param scan: The scan to resolve a PDF for.
    :return: Filesystem path to the best available PDF.
    :raises FileNotFoundError: If neither a processing PDF nor a local
        copy of the original exists (raised by ``Scan.pdf_path``).
    """
    if scan.output_dir:
        found = find_processing_pdf(scan.output_dir)
        if found:
            return str(found)
    return scan.pdf_path


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
    an opinion's ``caption_page`` through ``page_end`` (or ``key_page``)
    span, inclusive (opinion page indices are offset by ``start_page``).

    :param opinions: List of opinion dicts with caption_page and
        page_end or key_page.
    :param start_page: First page in the scan's expected range.
    :param end_page: Last page in the scan's expected range.
    :returns: List of ``(start, end, count)`` tuples, one per gap run.
    :rtype: list[tuple[int, int, int]]
    """
    if not opinions or not start_page or not end_page:
        return []

    covered: set[int] = set()
    for op in opinions:
        cp = op.get("caption_page", 0)
        ep = op.get("page_end", op.get("key_page", cp))
        for p in range(cp + start_page, ep + start_page + 1):
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
