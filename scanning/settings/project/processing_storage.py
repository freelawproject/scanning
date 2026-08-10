import os
from pathlib import Path

import environ

env = environ.FileAwareEnv()

# How long a downloaded /tmp/ processing directory may remain idle
# before the cleanup command deletes it.
PROCESSING_TMP_TTL_HOURS = env.float("PROCESSING_TMP_TTL_HOURS", default=24.0)

# Filesystem root where intermediate processing files are cached per
# viewer session. Each scan lives under PROCESSING_TMP_DIR/{pk}/...
PROCESSING_TMP_DIR = env("PROCESSING_TMP_DIR", default="/tmp/scanning")

# How often (in seconds) run_daemon invokes cleanup_processing_tmp.
PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS = env.int(
    "PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS", default=900
)

# Maximum accepted size (in bytes) for a direct-to-S3 original PDF upload.
# Enforced in two places: the presign view pre-checks it for a fast
# client-facing error, and the presigned POST policy's content-length-range
# condition lets S3 itself reject anything larger before it lands.
# Configured in whole GB via MAX_UPLOAD_SIZE_GB (default 3 GB).
MAX_ORIGINAL_UPLOAD_SIZE = env.int("MAX_UPLOAD_SIZE_GB", default=3) * 1024**3

# How long a presigned direct-to-S3 upload (PendingUpload) may sit
# unconfirmed before cleanup_processing_tmp deletes it -- and, if the
# upload never landed, its fileless scan. Covers users who close the tab
# mid-upload. Deliberately longer than S3_UPLOAD_PRESIGNED_TTL: that TTL
# only bounds when the browser may *start* the POST (which happens
# seconds after presign); S3 lets an in-flight upload run past policy
# expiry, so the real bound is transfer time -- a 3 GB file (the
# MAX_UPLOAD_SIZE_GB ceiling) on a slow uplink (~0.8 Mbps) needs about
# 9 hours. Sweeping sooner would delete the pending row and fileless scan
# out from under a live upload.
PENDING_UPLOAD_TTL_HOURS = env.float("PENDING_UPLOAD_TTL_HOURS", default=9.0)

# Whether Generate Files adds a Tesseract text layer to the per-page PDFs in
# ``llm/``. Off by default: the pipeline no longer embeds one anywhere (see
# scanning #145), and the model that reads these pages reads the page image
# itself. Turn it on to restore the text crops ``ai.user_prompt`` layers on
# top of the roadmap (caption first lines, column-top continuations,
# footnote snippets), at the cost of an OCR pass over every page of the
# volume. It runs after redaction, so it never OCRs content that a rect is
# about to cover.
LLM_PAGE_TEXT_LAYER = env.bool("LLM_PAGE_TEXT_LAYER", default=False)


def _cgroup_cpu_quota() -> float | None:
    """CPU quota this container was granted, in cores, or None if unlimited.

    Reads cgroup v2 first (``cpu.max``, "<quota> <period>" or "max <period>"),
    then falls back to the v1 pair. Anything unreadable means we are not
    constrained by a cgroup we can see.
    """
    v2 = Path("/sys/fs/cgroup/cpu.max")
    try:
        quota, period = v2.read_text().split()
        return None if quota == "max" else float(quota) / float(period)
    except (OSError, ValueError):
        pass

    try:
        quota = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = float(
            Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text()
        )
    except (OSError, ValueError):
        return None
    return quota / period if quota > 0 else None


def _available_cpus() -> int:
    """Cores this process may actually use.

    ``os.cpu_count()`` reports the *host's* cores, which inside a pod is
    whatever the node happens to have rather than what the pod was granted,
    so using it directly oversubscribes badly. Affinity and the cgroup quota
    are the numbers that bound us.
    """
    try:
        usable = len(os.sched_getaffinity(0))
    except AttributeError:  # non-POSIX
        usable = os.cpu_count() or 1

    quota = _cgroup_cpu_quota()
    if quota:
        usable = min(usable, int(quota))
    return max(1, usable)


# Share of the pod's CPUs bitonal conversion may occupy when BITONAL_WORKERS
# is not set explicitly. Deliberately below 1.0: the daemon has other work to
# do while a conversion runs, and throughput flattens out well before one
# worker per core anyway (rasterisation is memory-bandwidth bound, not CPU
# bound), so the last 20% of the cores buys little.
BITONAL_CPU_FRACTION = 0.8

# Worker processes blackletter splits bitonal conversion across. Set
# BITONAL_WORKERS to pin it; leave it unset to derive it from the cores this
# pod actually has. 1 converts inline, which is what blackletter does by
# default and what the tests want.
BITONAL_WORKERS = env.int("BITONAL_WORKERS", default=0) or max(
    1, int(_available_cpus() * BITONAL_CPU_FRACTION)
)
