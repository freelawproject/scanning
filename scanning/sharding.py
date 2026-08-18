"""Split the original scan PDF into shards for external job execution.

External workers (bitonal on doctor, dots.mocr on RunPod) each process a
slice of the volume in parallel instead of the whole multi-GB original
(issue #164). The split is done with PyMuPDF, which round-trips the scan
byte-identically (no recompression, +0.0% size) at ~3 s per volume.

The shard set lives under ``shards/`` in the scan's processing prefix,
next to a ``manifest.json`` that records the page range and size of every
shard plus a fingerprint (size + page count) of the original it was cut
from. The manifest is the commit marker: it is written and uploaded
*last*, so its presence implies every shard it lists is in place.

Shards are a fingerprint-keyed cache for **full-volume jobs only**. Page
processing (YOLO, OCR, bitonal) is strictly page-level, so a smart page
insert/delete never re-runs shard-level work: the edited page is
processed individually and merged into the volume-level artifacts
(``bitonal.pdf``, detections, ocr_results), exactly as the smart-edit
paths already do. Edits therefore do NOT refresh the shard set -- the
stored shards go stale against the edited original, and that is fine,
because every consumer entry point calls :func:`ensure_shards` first: on
a fingerprint mismatch the whole set is re-cut (~3 s split), which is
noise against the full-volume work that follows.

Shards are deliberately excluded from the generic S3 processing-file sync
(see ``s3_sync.SHARDS_SUBDIR``): they duplicate the original's bytes, so
dragging them along with every viewer pull or end-of-pipeline push would
move gigabytes for nothing. They are uploaded once here, and consumers
fetch individual shards via presigned GET. After a successful upload the
local copies are removed for the same reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import time
from pathlib import Path

import fitz
from django.conf import settings

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1

# Warn (don't fail) when a shard lands this far over the byte target.
# Page density within a volume is uniform enough (see the #164 corpus
# census) that a big overshoot signals something odd worth a log line.
OVERSHOOT_WARN_RATIO = 1.25


class ShardingError(Exception):
    """A shard set could not be computed or failed verification."""


def plan_shards(
    page_count: int, size_bytes: int, target_bytes: int
) -> list[tuple[int, int]]:
    """Split ``page_count`` pages into contiguous byte-targeted ranges.

    The shard count comes from the byte size (``ceil(size / target)``),
    then pages are spread across the shards as evenly as possible.
    Balanced ranges rather than fixed pages-per-shard, so there is never
    a tiny tail shard. Assumes roughly uniform bytes per page, which
    holds for our scanned volumes (KB/page varies ~1.3x across the whole
    corpus and far less within one volume).

    :param page_count: Number of pages in the source PDF.
    :param size_bytes: Byte size of the source PDF.
    :param target_bytes: Target byte size per shard.
    :returns: List of 0-based inclusive ``(from_page, to_page)`` ranges,
        contiguous and covering every page exactly once.
    :rtype: list[tuple[int, int]]
    """
    if page_count < 1:
        raise ShardingError(f"cannot shard a {page_count}-page PDF")
    if size_bytes < 1 or target_bytes < 1:
        raise ShardingError(
            f"invalid sizes: size_bytes={size_bytes} "
            f"target_bytes={target_bytes}"
        )

    n_shards = min(page_count, max(1, math.ceil(size_bytes / target_bytes)))
    base, remainder = divmod(page_count, n_shards)

    ranges = []
    start = 0
    for i in range(n_shards):
        length = base + (1 if i < remainder else 0)
        ranges.append((start, start + length - 1))
        start += length
    return ranges


def _box_tuple(box) -> tuple[float, float, float, float]:
    """Return a fitz Rect's coordinates as a plain comparable tuple.

    :param box: A ``fitz.Rect`` (e.g. a page's MediaBox).
    :returns: ``(x0, y0, x1, y1)``.
    :rtype: tuple[float, float, float, float]
    """
    return (box.x0, box.y0, box.x1, box.y1)


def _page_image_digests(doc: fitz.Document, page_index: int) -> list[str]:
    """Digest the raw, undecoded image streams on one page.

    The digests are taken over the compressed bytes exactly as stored in
    the PDF (``xref_stream_raw``), so any tool or version that silently
    recompresses the scan data changes them. This is the one check in
    the #164 benchmark that catches that failure mode. Soft masks are
    digested too: an /SMask stream is scan data like any other (MRC-style
    layered scans), and a tool that recompresses only the mask would
    otherwise slip through on matching base-image digests.

    :param doc: The open document.
    :param page_index: 0-based page index within ``doc``.
    :returns: Sorted hex digests of every image stream on the page,
        soft-mask streams included.
    :rtype: list[str]
    """
    digests = []
    for img in doc[page_index].get_images(full=True):
        for xref in (img[0], img[1]):  # base image, then its smask (0 = none)
            if not xref:
                continue
            raw = doc.xref_stream_raw(xref)
            digests.append(hashlib.sha256(raw or b"").hexdigest())
    return sorted(digests)


def shard_pdf(
    source_path: str | Path, dest_dir: str | Path, target_bytes: int
) -> dict:
    """Split ``source_path`` into byte-targeted shards under ``dest_dir``.

    Shards are named ``0001.pdf``, ``0002.pdf``, ... in page order. The
    returned manifest is complete except that it has not been written to
    disk; :func:`ensure_shards` persists it once the shard set is
    verified and uploaded.

    :param source_path: The original scan PDF.
    :param dest_dir: Directory to write the shard PDFs into (created if
        missing).
    :param target_bytes: Target byte size per shard.
    :returns: The manifest dict describing the shard set.
    :rtype: dict
    """
    source_path = Path(source_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    size_bytes = source_path.stat().st_size
    t0 = time.monotonic()
    shards = []
    with fitz.open(str(source_path)) as src:
        ranges = plan_shards(src.page_count, size_bytes, target_bytes)
        for i, (from_page, to_page) in enumerate(ranges):
            name = f"{i + 1:04d}.pdf"
            out_path = dest_dir / name
            with fitz.open() as shard:
                shard.insert_pdf(src, from_page=from_page, to_page=to_page)
                shard.save(str(out_path))
            shard_size = out_path.stat().st_size
            if shard_size > target_bytes * OVERSHOOT_WARN_RATIO:
                logger.warning(
                    "Shard %s of %s is %.1f MB, more than %.0f%% over the "
                    "%.1f MB target",
                    name,
                    source_path.name,
                    shard_size / 1024 / 1024,
                    (OVERSHOOT_WARN_RATIO - 1) * 100,
                    target_bytes / 1024 / 1024,
                )
            shards.append(
                {
                    "name": name,
                    "index": i,
                    "from_page": from_page,
                    "to_page": to_page,
                    "page_count": to_page - from_page + 1,
                    "size_bytes": shard_size,
                }
            )
        page_count = src.page_count
    split_seconds = time.monotonic() - t0

    return {
        "version": MANIFEST_VERSION,
        "source": {
            "name": source_path.name,
            "size_bytes": size_bytes,
            "page_count": page_count,
        },
        "target_bytes": target_bytes,
        "tool": {"name": "pymupdf", "version": fitz.version[0]},
        "shards": shards,
        "timings": {"split_seconds": round(split_seconds, 3)},
    }


def verify_shards(
    source_path: str | Path, shard_dir: str | Path, manifest: dict
) -> None:
    """Assert a shard set is equivalent to the original it was cut from.

    Checks, per page: geometry (MediaBox) and the digests of the raw
    undecoded image streams -- the compressed scan data must be copied
    through bit-for-bit, never recompressed. Also checks the manifest's
    ranges are contiguous and cover the source exactly once.

    :param source_path: The original scan PDF.
    :param shard_dir: Directory holding the shard PDFs.
    :param manifest: The manifest returned by :func:`shard_pdf`.
    :raises ShardingError: On the first mismatch found.
    """
    shard_dir = Path(shard_dir)

    with fitz.open(str(source_path)) as src:
        expected_next = 0
        for entry in manifest["shards"]:
            from_page, to_page = entry["from_page"], entry["to_page"]
            if from_page != expected_next:
                raise ShardingError(
                    f"shard {entry['name']} starts at page {from_page}, "
                    f"expected {expected_next}: ranges are not contiguous"
                )
            expected_next = to_page + 1

            shard_path = shard_dir / entry["name"]
            if not shard_path.is_file():
                raise ShardingError(f"missing shard file {shard_path}")
            with fitz.open(str(shard_path)) as shard:
                if shard.page_count != entry["page_count"]:
                    raise ShardingError(
                        f"shard {entry['name']} has {shard.page_count} "
                        f"pages, manifest says {entry['page_count']}"
                    )
                for local_idx in range(shard.page_count):
                    global_idx = from_page + local_idx
                    src_box = _box_tuple(src[global_idx].mediabox)
                    shard_box = _box_tuple(shard[local_idx].mediabox)
                    if src_box != shard_box:
                        raise ShardingError(
                            f"page {global_idx} geometry differs in shard "
                            f"{entry['name']}: {src_box} vs {shard_box}"
                        )
                    src_digests = _page_image_digests(src, global_idx)
                    shard_digests = _page_image_digests(shard, local_idx)
                    if src_digests != shard_digests:
                        raise ShardingError(
                            f"page {global_idx} image streams differ in "
                            f"shard {entry['name']}: the scan data was "
                            "not copied through byte-identically"
                        )

        if expected_next != src.page_count:
            raise ShardingError(
                f"shards cover {expected_next} pages, "
                f"source has {src.page_count}"
            )


def _source_fingerprint(source_path: Path) -> dict:
    """Return the identity a manifest records for its source PDF.

    Size plus page count: cheap to compute, and both change whenever a
    smart page insert/delete rewrites the original in place. A full
    content hash would cost a multi-GB read per pipeline run for no
    extra protection against the mutations that actually happen.

    :param source_path: The original scan PDF.
    :returns: ``{"name", "size_bytes", "page_count"}``.
    :rtype: dict
    """
    with fitz.open(str(source_path)) as doc:
        page_count = doc.page_count
    return {
        "name": source_path.name,
        "size_bytes": source_path.stat().st_size,
        "page_count": page_count,
    }


def _manifest_matches(manifest: object, fingerprint: dict) -> bool:
    """Return True when an existing manifest describes this exact source.

    Matches on manifest version plus source size and page count. The
    version check means bumping ``MANIFEST_VERSION`` forces a cheap
    re-cut instead of handing consumers a manifest in a layout they no
    longer read. The shard byte target is deliberately not part of the
    identity: retuning ``SHARD_TARGET_BYTES`` must not silently re-shard
    every volume in the corpus ("once computed, never recompute").

    A manifest that is structurally broken (not a dict, no source dict,
    no non-empty shard list) never matches: it flows into the re-shard
    path, which deletes it and cuts a fresh set, rather than crashing
    the pipeline on every retry.

    :param manifest: A previously stored manifest -- any parsed-JSON
        value is tolerated, matching what a corrupt store can hold.
    :param fingerprint: Current source fingerprint.
    :returns: Whether the manifest is still valid for this source.
    :rtype: bool
    """
    if not isinstance(manifest, dict):
        return False
    source = manifest.get("source")
    if not isinstance(source, dict):
        return False
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        return False
    return (
        manifest.get("version") == MANIFEST_VERSION
        and source.get("size_bytes") == fingerprint["size_bytes"]
        and source.get("page_count") == fingerprint["page_count"]
    )


def _existing_manifest(scan, shards_dir: Path) -> dict | None:
    """Load the stored manifest for a scan, if any.

    When S3 sync is active, S3 is the authority: the local ``shards/``
    tree is deleted after upload and is excluded from the generic pull,
    so a local manifest may simply not exist on this pod. Without S3
    (DEVELOPMENT), the local file is all there is.

    :param scan: The scan to look up.
    :param shards_dir: The scan's local ``shards/`` directory.
    :returns: The manifest dict, or None when none is stored.
    :rtype: dict | None
    """
    from scanning import s3_sync

    manifest = s3_sync.fetch_shard_manifest(scan)
    if manifest is not None:
        return manifest

    if s3_sync._s3_enabled():
        # S3 answered "no manifest", so there is no committed shard set.
        # A local manifest here is at best a leftover from a run that
        # died before its upload committed; trusting it would report a
        # shard set S3 never got.
        return None

    local = shards_dir / s3_sync.SHARD_MANIFEST_NAME
    if local.is_file():
        try:
            manifest = json.loads(local.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(
                "Unreadable local shard manifest for scan %s at %s",
                scan.pk,
                local,
            )
        else:
            if isinstance(manifest, dict):
                return manifest
            logger.warning(
                "Malformed local shard manifest for scan %s at %s: "
                "expected an object, got %s",
                scan.pk,
                local,
                type(manifest).__name__,
            )
    return None


def ensure_shards(scan) -> dict | None:
    """Compute, verify and store the scan's shard set, exactly once.

    Idempotent: when a stored manifest already matches the original's
    fingerprint this is a no-op returning that manifest. Otherwise --
    first run, or the original changed since the set was cut (smart page
    edits, a re-upload) -- the whole set is computed from scratch:
    split, verified against the original, uploaded to S3 (manifest last,
    as the commit marker), and the local shard PDFs dropped once they
    are safely uploaded.

    Smart page edits deliberately do NOT call this: the edited page is
    processed individually, and the stale shard set just sits in S3
    until the next full-volume job runs through here and replaces it.

    :param scan: The scan to shard. Its original PDF must be available
        locally or in S3.
    :returns: The manifest describing the shard set, or None when
        sharding is disabled.
    :rtype: dict | None
    :raises ShardingError: When the original PDF is unavailable or the
        shard set fails verification. No manifest is left behind on
        failure, so the next pipeline run retries naturally.
    """
    from scanning import s3_sync
    from scanning.utils import has_s3_credentials, local_original_pdf

    if not settings.SHARDING_ENABLED:
        return None

    # Same loud misconfig signal as _push_processing_files_to_s3: without
    # creds every S3 helper below silently no-ops, the shard PDFs pile up
    # on ephemeral disk, and external jobs find an empty shards/ prefix
    # with nothing in Sentry pointing at the root cause.
    if (
        not settings.DEVELOPMENT
        and not getattr(settings, "TESTING", False)
        and not has_s3_credentials()
    ):
        logger.error(
            "Sharding scan %s without AWS credentials: shards will stay "
            "on local disk and never reach S3",
            scan.pk,
        )

    source = local_original_pdf(scan)
    if source is None:
        raise ShardingError(
            f"scan {scan.pk} has no original PDF available to shard"
        )
    source_path = Path(source)
    fingerprint = _source_fingerprint(source_path)

    shards_dir = Path(scan.output_dir) / s3_sync.SHARDS_SUBDIR.rstrip("/")
    existing = _existing_manifest(scan, shards_dir)
    if existing is not None:
        if _manifest_matches(existing, fingerprint):
            logger.info(
                "Shards for scan %s are up to date (%d shard(s), source "
                "%d bytes); skipping recompute",
                scan.pk,
                len(existing["shards"]),
                fingerprint["size_bytes"],
            )
            return existing
        # .get chains, not indexing: a structurally broken manifest (the
        # other way _manifest_matches says no) may have no source dict at
        # all, and this log line must not crash ahead of the cleanup that
        # would remove the bad manifest.
        old_source = existing.get("source")
        if not isinstance(old_source, dict):
            old_source = {}
        logger.info(
            "Shard manifest for scan %s is stale (source was %s bytes / "
            "%s pages, now %d / %d); re-sharding",
            scan.pk,
            old_source.get("size_bytes"),
            old_source.get("page_count"),
            fingerprint["size_bytes"],
            fingerprint["page_count"],
        )

    # Stale shard objects must not survive next to a new manifest: page
    # ranges shift when the source changes, and a leftover 0007.pdf from
    # a larger previous set would look like part of the new one.
    s3_sync.delete_shard_objects(scan)
    if shards_dir.is_dir():
        shutil.rmtree(shards_dir)

    try:
        manifest = shard_pdf(
            source_path, shards_dir, settings.SHARD_TARGET_BYTES
        )
        t0 = time.monotonic()
        verify_shards(source_path, shards_dir, manifest)
        manifest["timings"]["verify_seconds"] = round(time.monotonic() - t0, 3)
        # The manifest hits disk only after verification passed, and S3
        # only after every shard PDF is up (upload_shards sends it last).
        (shards_dir / s3_sync.SHARD_MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2)
        )
        uploaded = s3_sync.upload_shards(scan, shards_dir)
    except Exception:
        # Shards are a byte-for-byte copy of the original: recomputing is
        # cheap and deterministic, so reclaim the disk instead of leaving
        # a broken multi-GB set behind for the TTL sweep. Removing the
        # tree also drops any just-written local manifest, keeping the
        # "no manifest survives a failure" guarantee when the *upload*
        # is what failed (S3 got shard PDFs but never the manifest).
        shutil.rmtree(shards_dir, ignore_errors=True)
        raise

    if uploaded:
        for entry in manifest["shards"]:
            (shards_dir / entry["name"]).unlink(missing_ok=True)

    total_mb = fingerprint["size_bytes"] / 1024 / 1024
    logger.info(
        "Sharded scan %s: %d shard(s) of ~%d page(s) from %.1f MB / %d "
        "pages, split %.1fs, verify %.1fs%s",
        scan.pk,
        len(manifest["shards"]),
        manifest["shards"][0]["page_count"],
        total_mb,
        fingerprint["page_count"],
        manifest["timings"]["split_seconds"],
        manifest["timings"]["verify_seconds"],
        ", uploaded to S3" if uploaded else "",
    )
    return manifest
