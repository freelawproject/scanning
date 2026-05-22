"""Sync intermediate processing files between MEDIA_ROOT, S3, and /tmp/.

Files produced by the scanning pipeline (bitonal PDF, OCR PDF,
detections.json, stamped PDF, page images, original PDF) are small
enough that re-running the pipeline to regenerate them is expensive.
Pushing them to S3 lets us recover after pod redeploys without a full
re-run. Pulling them to /tmp/ when the viewer opens gives editing
code fast local-filesystem access (PyMuPDF, Pillow, blackletter).

All sync helpers short-circuit when running under ``TESTING``, when
AWS credentials are missing, and during ``DEVELOPMENT`` unless
``RUNPOD_ENABLED`` is also True. The RunPod-enabled dev path needs
the full sync so a local end-to-end test exercises the same recovery
behavior as prod.
"""

from __future__ import annotations

import logging
from pathlib import Path

import boto3
from django.conf import settings

from scanning.models import Scan
from scanning.utils import has_s3_credentials

logger = logging.getLogger(__name__)

# File relative-path prefixes considered deliverables. When a scan is
# approved, every file under one of these subdirs gets server-side
# copied from processing/ to approved/ (the processing/ copy is kept).
# Everything else in processing/ (bitonal, detections, unredacted/,
# stamped, etc.) stays only under processing/.
APPROVED_SUBDIR_PREFIXES = ("redacted/", "images/")
APPROVED_FILE_SUFFIXES = (".original.pdf", ".redacted.pdf")


def _s3_enabled() -> bool:
    """Return True when we should actually use S3.

    Skipped during TESTING (so unit tests don't reach live AWS) and
    when AWS credentials are missing. Also skipped during DEVELOPMENT
    unless RUNPOD_ENABLED is True, since the RunPod-enabled dev path
    needs full S3 sync to mirror the prod recovery behavior.

    :returns: Whether S3 sync is active in the current environment.
    :rtype: bool
    """
    if getattr(settings, "TESTING", False):
        return False
    if settings.DEVELOPMENT and not settings.RUNPOD_ENABLED:
        return False
    return has_s3_credentials()


def _scan_path_parts(scan: Scan) -> tuple[str, str, str]:
    """Return (reporter_slug, volume, start_page) as strings.

    :param scan: The scan to derive path parts from.
    :returns: Triple of string parts for building paths.
    :rtype: tuple[str, str, str]
    """
    reporter_slug = scan.reporter.short_name if scan.reporter else "unknown"
    volume = str(scan.volume) if scan.volume else "0"
    start_page = str(scan.start_page or 1)
    return reporter_slug, volume, start_page


def s3_processing_prefix(scan: Scan) -> str:
    """Return the S3 key prefix for a scan's processing files.

    :param scan: The scan to build the prefix for.
    :returns: Prefix of the form ``processing/{pk}/{reporter}/{vol}/{start}/``.
    :rtype: str
    """
    reporter_slug, volume, start_page = _scan_path_parts(scan)
    return f"processing/{scan.pk}/{reporter_slug}/{volume}/{start_page}/"


def tmp_output_dir(scan: Scan) -> Path:
    """Return the /tmp/ path for a scan's downloaded processing files.

    :param scan: The scan to build the path for.
    :returns: Absolute path under ``settings.PROCESSING_TMP_DIR``.
    :rtype: Path
    """
    reporter_slug, volume, start_page = _scan_path_parts(scan)
    return (
        Path(settings.PROCESSING_TMP_DIR)
        / str(scan.pk)
        / reporter_slug
        / volume
        / start_page
    )


def _iter_files_to_sync(local_root: Path):
    """Yield every file recursively under ``local_root``.

    :param local_root: Directory to walk.
    :yields: Paths of files under ``local_root``.
    :rtype: Iterator[Path]
    """
    for path in local_root.rglob("*"):
        if path.is_file():
            yield path


def _is_approved_deliverable(relative_path: str) -> bool:
    """Return True if ``relative_path`` (under processing/) belongs in approved/.

    Approved deliverables are opinion PDFs under ``redacted/``,
    extracted figure images under ``images/``, plus the full-book
    original and redacted PDFs. Everything else (bitonal,
    detections.json, unredacted/, stamped, llm/) stays under
    processing/.

    :param relative_path: Path relative to the scan's processing prefix.
    :returns: Whether to copy this file into approved/.
    :rtype: bool
    """
    if relative_path.startswith(APPROVED_SUBDIR_PREFIXES):
        # One-level deep only: redacted/foo.pdf, not redacted/sub/foo.pdf.
        return relative_path.count("/") == 1
    if "/" not in relative_path and relative_path.endswith(
        APPROVED_FILE_SUFFIXES
    ):
        return True
    return False


def upload_processing_files(scan: Scan) -> int:
    """Upload processing files from MEDIA_ROOT to S3.

    Overwrites existing S3 objects; no hash check. Intended to run at
    the end of a pipeline that leaves the scan in a viewable state.

    :param scan: Scan whose output dir to upload.
    :returns: Number of files uploaded (0 if disabled or nothing found).
    :rtype: int
    """
    if not _s3_enabled():
        return 0

    local_root = Path(scan.output_dir)
    if not local_root.is_dir():
        logger.warning(
            "upload_processing_files: no local dir for scan %s at %s",
            scan.pk,
            local_root,
        )
        return 0

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    prefix = s3_processing_prefix(scan)
    s3 = boto3.client("s3")

    count = 0
    for path in _iter_files_to_sync(local_root):
        rel = path.relative_to(local_root).as_posix()
        s3.upload_file(str(path), bucket, f"{prefix}{rel}")
        count += 1

    logger.info(
        "Uploaded %d processing file(s) for scan %s to s3://%s/%s",
        count,
        scan.pk,
        bucket,
        prefix,
    )
    return count


def download_processing_files(scan: Scan) -> Path | None:
    """Download processing files from S3 to /tmp/.

    Idempotent: skips files whose local size matches the S3 object's
    ContentLength. Touches the tmp directory's mtime so the TTL sweep
    treats it as recently active.

    :param scan: Scan to pull files for.
    :returns: The local tmp path, or None if sync is disabled.
    :rtype: Path | None
    """
    if not _s3_enabled():
        return None

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    prefix = s3_processing_prefix(scan)
    local_root = tmp_output_dir(scan)
    local_root.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    downloaded = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix) :]
            if not rel:
                continue
            local_path = local_root / rel
            if local_path.is_file() and local_path.stat().st_size == obj.get(
                "Size", -1
            ):
                continue
            local_path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(local_path))
            downloaded += 1

    local_root.touch(exist_ok=True)
    if downloaded:
        logger.info(
            "Downloaded %d processing file(s) for scan %s from s3://%s/%s",
            downloaded,
            scan.pk,
            bucket,
            prefix,
        )
    return local_root


def upload_file_to_s3(scan: Scan, relative_path: str) -> bool:
    """Upload a single file (relative to the scan's local root) to S3.

    Used when a viewer edit rewrites a file on disk (e.g.
    ``detections.json``). Overwrites unconditionally.

    :param scan: The scan the file belongs to.
    :param relative_path: Path relative to the scan's output dir.
    :returns: True if uploaded, False if disabled or file missing.
    :rtype: bool
    """
    if not _s3_enabled():
        return False

    local_path = Path(scan.output_dir) / relative_path
    if not local_path.is_file():
        logger.warning(
            "upload_file_to_s3: %s does not exist for scan %s",
            local_path,
            scan.pk,
        )
        return False

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_processing_prefix(scan)}{relative_path}"
    boto3.client("s3").upload_file(str(local_path), bucket, key)
    logger.info(
        "Uploaded %s for scan %s to s3://%s/%s",
        relative_path,
        scan.pk,
        bucket,
        key,
    )
    return True


def approved_prefix(scan: Scan) -> str:
    """Return the S3 prefix where a scan's approved deliverables live.

    :param scan: The scan to build the prefix for.
    :returns: Prefix of the form ``approved/{reporter}/{vol}/{start}/``.
    :rtype: str
    """
    short = scan.reporter.short_name if scan.reporter else "unknown"
    start = scan.start_page or 1
    return f"approved/{short}/{scan.volume}/{start}/"


def copy_processing_to_approved(scan: Scan) -> tuple[str, int]:
    """Server-side copy deliverables from processing/ to approved/ on S3.

    Lists the scan's processing prefix, picks the files flagged by
    ``_is_approved_deliverable``, and issues an S3 ``copy_object`` for
    each. No local download/upload happens; the data stays in S3.

    :param scan: The scan being approved.
    :returns: ``(approved_prefix, copied_count)``. Prefix is returned
        even when S3 is disabled so callers can still record it.
    :rtype: tuple[str, int]
    """
    dest_prefix = approved_prefix(scan)
    if not _s3_enabled():
        return dest_prefix, 0

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    src_prefix = s3_processing_prefix(scan)
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")

    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=src_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(src_prefix) :]
            if not rel or not _is_approved_deliverable(rel):
                continue
            s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": key},
                Key=f"{dest_prefix}{rel}",
            )
            count += 1

    logger.info(
        "Copied %d deliverable(s) for scan %s: s3://%s/%s -> s3://%s/%s",
        count,
        scan.pk,
        bucket,
        src_prefix,
        bucket,
        dest_prefix,
    )
    return dest_prefix, count
