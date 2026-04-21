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
