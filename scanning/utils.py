"""Small reusable helpers shared across views and services."""

from __future__ import annotations

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
