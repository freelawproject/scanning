import environ

env = environ.FileAwareEnv()

# How often the daemon polls for unprocessed scans (seconds).
DAEMON_PROCESS_SCANS_INTERVAL = env.int(
    "DAEMON_PROCESS_SCANS_INTERVAL", default=60
)

# Seconds before a PROCESSING scan is considered stuck.
DAEMON_PROCESSING_TIMEOUT = env.int("DAEMON_PROCESSING_TIMEOUT", default=3600)
