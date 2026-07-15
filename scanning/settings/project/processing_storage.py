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

# How long a presigned direct-to-S3 upload (PendingUpload) may sit
# unconfirmed before cleanup_processing_tmp deletes it -- and, if the
# upload never landed, its fileless scan. Covers users who close the tab
# mid-upload. Deliberately longer than S3_UPLOAD_PRESIGNED_TTL: that TTL
# only bounds when the browser may *start* the POST (which happens
# seconds after presign); S3 lets an in-flight upload run past policy
# expiry, so the real bound is transfer time -- a 2 GB file on a slow
# uplink (~0.8 Mbps) needs about 6 hours. Sweeping sooner would delete
# the pending row and fileless scan out from under a live upload.
PENDING_UPLOAD_TTL_HOURS = env.float("PENDING_UPLOAD_TTL_HOURS", default=6.0)
