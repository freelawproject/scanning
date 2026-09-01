import environ

env = environ.FileAwareEnv()

# How often (in seconds) the daemon checks for queued scans.
DAEMON_POLL_INTERVAL = env.int("DAEMON_POLL_INTERVAL", default=5)

# How long (in seconds) before a PROCESSING scan is considered stale.
DAEMON_PROCESSING_TIMEOUT = env.int("DAEMON_PROCESSING_TIMEOUT", default=3600)

# How many times the daemon may re-queue a single scan after being killed or
# timing out mid-pipeline before giving up and flagging it ERROR_INTERRUPTED.
# Guards against a churning daemon pod re-queueing the same scan forever
# without ever consuming its RunPod retry budget (issue #124).
DAEMON_MAX_INTERRUPTIONS = env.int("DAEMON_MAX_INTERRUPTIONS", default=5)

# How often (in seconds) the daemon submits a wave of pending external
# jobs. One tick claims at most DOCTOR_MAX_CONCURRENCY rows and blocks
# until their requests answer, so this is a floor on the gap between
# waves rather than a promise about them.
DAEMON_SUBMIT_INTERVAL = env.int("DAEMON_SUBMIT_INTERVAL", default=5)

# How often (in seconds) the daemon confirms in-flight external jobs
# (an S3 HEAD per row) and finishes any scan whose jobs are all done.
DAEMON_COLLECT_INTERVAL = env.int("DAEMON_COLLECT_INTERVAL", default=15)

# How long (in seconds) a job may sit in a provider's queue before it
# is written off. The clock starts at the attempt's first claim -- the
# moment the row is handed to the provider -- never at row creation: a
# creation-stamped clock expired 2023 rows unsubmitted in our own queue
# on 2026-08-31 and sank 29 volumes to ERROR (issue #218). A row
# waiting in our own queue has no clock at all. Not an execution
# budget either: the run budget starts at the IN_PROGRESS crossing.
DAEMON_JOB_MAX_QUEUE_SECONDS = env.int(
    "DAEMON_JOB_MAX_QUEUE_SECONDS", default=6 * 60 * 60
)
