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

import functools
import logging
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from django.conf import settings

from scanning.models import Scan
from scanning.utils import has_s3_credentials

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _cached_s3_client():
    """Build and memoize a single S3 client (see :func:`_s3_client`)."""
    return boto3.client("s3")


def _s3_client():
    """Return a shared, lazily-built S3 client.

    ``boto3.client("s3")`` loads the service model and runs the
    credential-provider chain on every call, which is wasted work when a
    request triggers several S3 operations (or when many requests hit the
    same worker). boto3 clients are thread-safe for making calls, so we
    build one and reuse it. Cached on first use, not at import, so
    credential resolution happens once the app is actually serving.

    Under ``TESTING`` the cache is bypassed and a fresh client is built
    each call, so tests that patch ``scanning.s3_sync.boto3`` always see
    their own mock -- a cached client from an earlier test can't silently
    defeat the patch. (Tests that flip ``TESTING=False`` to exercise the
    real S3 path still call ``_cached_s3_client.cache_clear()`` in setUp.)

    :returns: A boto3 S3 client (process-wide outside tests).
    """
    if getattr(settings, "TESTING", False):
        return boto3.client("s3")
    return _cached_s3_client()


# File relative-path prefixes considered deliverables. When a scan is
# approved, every file under one of these subdirs gets server-side
# copied from processing/ to approved/ (the processing/ copy is kept).
# Everything else in processing/ (bitonal, detections, unredacted/,
# stamped, etc.) stays only under processing/.
APPROVED_SUBDIR_PREFIXES = ("redacted/", "images/")
APPROVED_FILE_SUFFIXES = (".original.pdf", ".redacted.pdf")

# Subdirectory under a scan's processing prefix where GPU workers PUT
# their job results (see ``scanning/runpod_client.py``). Deliberately
# excluded from the processing-file sync in both directions: these are
# wire artifacts the daemon consumes once and writes to Postgres, not
# files the viewer or the pipeline ever reads off disk.
JOB_RESULTS_SUBDIR = "jobs/"


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


def direct_upload_enabled() -> bool:
    """Return True when the browser can upload straight to S3.

    Mirrors ``_s3_enabled()`` -- the same gate ``generate_presigned_post``
    and ``verify_uploaded_object`` use -- so the upload view and template
    agree on whether to offer the presigned direct-to-S3 path or fall
    back to the through-Django ``queue_upload``.

    :returns: Whether presigned direct-to-S3 uploads are available.
    :rtype: bool
    """
    return _s3_enabled()


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


def s3_job_result_key(scan: Scan, stage: str) -> str:
    """Return the S3 key a GPU worker PUTs one job's result to.

    One object per scan and stage: a re-run overwrites the previous
    attempt's output instead of leaving an orphan behind. Nothing
    reads the object without first checking it was written after the
    job that's reading it was submitted, so an overwritten-in-place
    key can't be mistaken for a stale one (see
    ``runpod_client._result_object_is_fresh``).

    :param scan: The scan the job belongs to.
    :param stage: Pipeline stage / handler action (``detect`` /
        ``analyze``).
    :returns: Key of the form
        ``{processing_prefix}jobs/{stage}/result.json``.
    :rtype: str
    """
    return (
        f"{s3_processing_prefix(scan)}{JOB_RESULTS_SUBDIR}{stage}/result.json"
    )


def _is_job_result(rel: str) -> bool:
    """Return True if a processing-prefix relative key is a job result.

    :param rel: Object key relative to the scan's processing prefix.
    :returns: Whether the key lives under the ``jobs/`` subtree.
    :rtype: bool
    """
    return rel.startswith(JOB_RESULTS_SUBDIR)


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
    s3 = _s3_client()

    count = 0
    for path in _iter_files_to_sync(local_root):
        rel = path.relative_to(local_root).as_posix()
        if _is_job_result(rel):
            continue
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

    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    downloaded = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix) :]
            if not rel or _is_job_result(rel):
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


def _is_preview_pdf(rel: str) -> bool:
    """Return True if a processing-prefix relative key is a preview PDF.

    A "preview" is the small, browser-viewable PDF the process viewer
    shows: the OCR PDF if present, else ``bitonal.pdf``. This mirrors the
    exclusion rules in ``utils.find_ocr_pdf`` and adds ``bitonal.pdf``.
    Deliberately excludes the multi-GB ``*.original.pdf`` and anything
    under a subdirectory (``images/``, ``redacted/``, etc.).

    :param rel: Object key relative to the scan's processing prefix.
    :returns: Whether the key is the bitonal or OCR preview PDF.
    :rtype: bool
    """
    if "/" in rel or not rel.endswith(".pdf"):
        return False
    if rel == "bitonal.pdf":
        return True
    if rel in ("stamped.pdf",):
        return False
    if rel.endswith((".redacted.pdf", ".original.pdf")):
        return False
    return "redacted" not in rel and "bitonal" not in rel


def _is_original_pdf(rel: str) -> bool:
    """Return True if a processing-prefix relative key is the original PDF.

    The (up to 3 GB) ``*.original.pdf`` at the top of the prefix. Used to
    pull only the original -- not the ``images/`` tree -- for the crop
    endpoint, which renders high-res crops from the non-bitonal original.

    :param rel: Object key relative to the scan's processing prefix.
    :returns: Whether the key is the top-level original PDF.
    :rtype: bool
    """
    return "/" not in rel and rel.endswith(".original.pdf")


def _download_matching(scan: Scan, predicate, kind: str) -> Path | None:
    """Download processing-prefix objects whose relative key matches.

    Shared machinery for the targeted pulls. Idempotent: skips files
    whose local size already matches the S3 object's. Touches the tmp
    dir's mtime so the TTL sweep treats it as recently active.

    :param scan: Scan whose processing prefix to pull from.
    :param predicate: ``rel -> bool`` selecting which keys to download.
    :param kind: Human label for the log line (e.g. ``"preview PDF"``).
    :returns: The local tmp path, or None if sync is disabled.
    :rtype: Path | None
    """
    if not _s3_enabled():
        return None

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    prefix = s3_processing_prefix(scan)
    local_root = tmp_output_dir(scan)
    local_root.mkdir(parents=True, exist_ok=True)

    s3 = _s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    downloaded = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix) :]
            if not rel or not predicate(rel):
                continue
            local_path = local_root / rel
            if local_path.is_file() and local_path.stat().st_size == obj.get(
                "Size", -1
            ):
                continue
            s3.download_file(bucket, key, str(local_path))
            downloaded += 1

    local_root.touch(exist_ok=True)
    if downloaded:
        logger.info(
            "Downloaded %d %s(s) for scan %s from s3://%s/%s",
            downloaded,
            kind,
            scan.pk,
            bucket,
            prefix,
        )
    return local_root


def download_preview_pdf(scan: Scan) -> Path | None:
    """Download only the small preview PDF(s) from S3 to /tmp/.

    Pulls the bitonal and/or OCR PDF -- the reviewable previews -- and
    skips the multi-GB original and the ``images/`` tree. Used by the PDF
    viewer endpoint so opening a scan never drags the whole processing
    prefix (or the original) across the network, which blew past the
    gunicorn worker timeout for large scans.

    :param scan: Scan to pull the preview PDF for.
    :returns: The local tmp path, or None if sync is disabled.
    :rtype: Path | None
    """
    return _download_matching(scan, _is_preview_pdf, "preview PDF")


def download_original_pdf(scan: Scan) -> Path | None:
    """Download only the original PDF from S3 to /tmp/.

    Used by the crop endpoint, which needs the (large, non-bitonal)
    original locally to render high-res crops. Pulls just the original,
    not the ``images/`` tree, so it stays as small as the feature allows.

    :param scan: Scan to pull the original PDF for.
    :returns: The local tmp path, or None if sync is disabled.
    :rtype: Path | None
    """
    return _download_matching(scan, _is_original_pdf, "original PDF")


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
    _s3_client().upload_file(str(local_path), bucket, key)
    logger.info(
        "Uploaded %s for scan %s to s3://%s/%s",
        relative_path,
        scan.pk,
        bucket,
        key,
    )
    return True


def upload_fileobj_to_s3(scan: Scan, upload, relative_path: str) -> bool:
    """Stream an uploaded file straight to S3 without a local copy.

    Used by the upload view in prod so the bytes Django already spooled
    go to S3 in a single pass, with no wasted local disk write. Prefers
    the temp-file path boto3 can read directly (Django spools large
    uploads to a ``TemporaryUploadedFile``) and falls back to the file
    object for small in-memory uploads.

    :param scan: The scan the file belongs to.
    :type scan: Scan
    :param upload: The uploaded file (a Django ``UploadedFile``).
    :param relative_path: Path relative to the scan's output dir, used
        as the S3 key suffix.
    :type relative_path: str
    :returns: True if uploaded, False if S3 is disabled.
    :rtype: bool
    """
    if not _s3_enabled():
        return False

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_processing_prefix(scan)}{relative_path}"
    extra_args = {"ContentType": "application/pdf"}
    s3 = _s3_client()

    start = time.monotonic()
    temp_path = getattr(upload, "temporary_file_path", None)
    if callable(temp_path):
        s3.upload_file(temp_path(), bucket, key, ExtraArgs=extra_args)
    else:
        upload.seek(0)
        s3.upload_fileobj(upload, bucket, key, ExtraArgs=extra_args)
    elapsed = time.monotonic() - start

    size_mb = (getattr(upload, "size", 0) or 0) / (1024 * 1024)
    logger.info(
        "Streamed %.1f MB to s3://%s/%s for scan %s in %.1fs",
        size_mb,
        bucket,
        key,
        scan.pk,
        elapsed,
    )
    return True


def delete_uploaded_object(key: str) -> bool:
    """Best-effort delete of a single S3 object by full key.

    Used to reclaim the object left behind by an abandoned or rejected
    direct-to-S3 upload (browser POSTed the bytes but never confirmed, or
    the object failed PDF verification). Deleting a key that doesn't exist
    is a no-op success in S3, so this is safe to call unconditionally.

    :param key: The full S3 object key (e.g. a ``PendingUpload.s3_key``).
    :returns: True if the delete was issued, False if S3 is disabled,
        the key is empty, or the call errored.
    :rtype: bool
    """
    if not _s3_enabled() or not key:
        return False

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    try:
        _s3_client().delete_object(Bucket=bucket, Key=key)
    except ClientError:
        logger.warning(
            "Could not delete abandoned upload s3://%s/%s", bucket, key
        )
        return False
    return True


def generate_presigned_post(
    scan: Scan,
    relative_path: str,
    content_type: str,
    max_size: int,
) -> dict | None:
    """Build a presigned POST so the browser uploads straight to S3.

    Keeps the (potentially multi-GB) original PDF off the Django
    request path: the view returns this policy, the browser POSTs the
    file directly to S3, and the daemon later pulls it from the same
    key via ``download_processing_files``. The ``content-length-range``
    condition lets S3 itself reject anything larger than ``max_size``,
    so an oversized upload never reaches the bucket.

    :param scan: The scan the file belongs to.
    :param relative_path: Path relative to the scan's processing prefix,
        used as the S3 key suffix (e.g. the ``*.original.pdf`` name).
    :param content_type: MIME type the browser must send; pinned by the
        policy so the stored object's type can't be spoofed.
    :param max_size: Maximum accepted size in bytes.
    :returns: The ``{"url": ..., "fields": {...}}`` dict from boto3, or
        None when S3 sync is disabled.
    :rtype: dict | None
    """
    if not _s3_enabled():
        return None

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_processing_prefix(scan)}{relative_path}"
    ttl = int(getattr(settings, "S3_UPLOAD_PRESIGNED_TTL", 3600))
    s3 = _s3_client()
    return s3.generate_presigned_post(
        Bucket=bucket,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            ["content-length-range", 1, max_size],
            {"Content-Type": content_type},
        ],
        ExpiresIn=ttl,
    )


def verify_uploaded_object(scan: Scan, relative_path: str) -> bool:
    """Confirm a direct-to-S3 upload landed and is a real PDF.

    Called from ``confirm_scan_upload`` once the browser reports the
    direct upload finished. Since the bytes never flowed through Django,
    this replaces the in-request ``%PDF-`` header check: a 5-byte ranged
    GET both proves the object exists (a missing key raises ``ClientError``)
    and verifies the magic bytes, without pulling the whole (multi-GB) file.

    :param scan: The scan the file belongs to.
    :param relative_path: Path relative to the scan's processing prefix.
    :returns: True if the object exists and starts with ``%PDF-``.
    :rtype: bool
    """
    if not _s3_enabled():
        return False

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_processing_prefix(scan)}{relative_path}"
    s3 = _s3_client()
    try:
        obj = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-4")
        header = obj["Body"].read(5)
    except ClientError:
        logger.warning(
            "verify_uploaded_object: no valid object at s3://%s/%s "
            "for scan %s",
            bucket,
            key,
            scan.pk,
        )
        return False

    if header != b"%PDF-":
        logger.warning(
            "verify_uploaded_object: s3://%s/%s for scan %s is not a PDF "
            "(header %r)",
            bucket,
            key,
            scan.pk,
            header,
        )
        return False
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
    s3 = _s3_client()
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
