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

# Maximum accepted size (in bytes) of one page-edit upload of review 1: a
# replacement of one page, or an insert of a missing leaf (#232). The
# default is a sixth of MAX_ORIGINAL_UPLOAD_SIZE (512 MiB at the 3 GB
# default): an insert is a part of a volume and never the whole, and a
# file above it is a whole volume sent by mistake. PAGE_UPLOAD_MAX_MB,
# in whole MB, overrides it. The cap was 50 MB, which refused a rescan
# of 90 pages (138 MB) that a scanner made for a real gap; a gap takes
# one insert (#256), so the file could not be split. The web pod reads
# the file from its temporary file, not into memory, so the cap costs
# no RAM there. The apply (#224) makes one job shard of each uploaded
# file, so a large insert is one shard above SHARD_TARGET_BYTES.
_page_upload_max_mb = env.int("PAGE_UPLOAD_MAX_MB", default=None)
PAGE_UPLOAD_MAX_BYTES = (
    _page_upload_max_mb * 1024**2
    if _page_upload_max_mb
    else MAX_ORIGINAL_UPLOAD_SIZE // 6
)

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

# Lifetime (in seconds) of the presigned GET the viewer uses to read the
# original PDF straight from S3 (issue #185). Long on purpose: pdf.js
# fetches page ranges lazily while the user scrolls, so a range request
# can come hours after the URL was issued. An expired signature 403s
# mid-scroll. Default 8 hours, a long review session.
ORIGINAL_VIEW_PRESIGN_TTL = env.int(
    "ORIGINAL_VIEW_PRESIGN_TTL", default=8 * 3600
)
