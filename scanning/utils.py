"""Small reusable helpers shared across views and services."""

from pathlib import Path

from django.conf import settings
from django.shortcuts import get_object_or_404

from scanning.models import Volume


def get_volume(reporter_slug, vol):
    """Look up a Volume by reporter slug and number."""
    return get_object_or_404(
        Volume.objects.select_related("reporter", "assigned_to"),
        reporter__short_name=reporter_slug,
        volume_number=vol,
    )


def get_output_base(scan):
    """Resolve the output directory for a scan.

    Uses scan.output_dir if set, otherwise falls back to
    MEDIA_ROOT/processed/<pk>.
    """
    if scan.output_dir:
        return Path(scan.output_dir)
    return Path(settings.MEDIA_ROOT) / "processed" / str(scan.pk)


def find_json_file(output_base, filename):
    """Search output_base and its parents for a JSON file.

    Returns the Path if found, or None.
    """
    for candidate in [
        output_base / filename,
        output_base.parent / filename,
        output_base.parent.parent / filename,
    ]:
        if candidate.exists():
            return candidate
    return None


def find_ocr_pdf(output_dir):
    """Find the OCR PDF in output_dir (excludes bitonal, redacted, original).

    Returns the Path if found, or None.
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