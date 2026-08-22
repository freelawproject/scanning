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

# How long (in seconds) a created-but-unsubmitted job may wait before
# it is written off. Not an execution budget: waiting to be submitted
# is the other way a job waits, and a scan parked behind one cannot
# tell the two apart. Guards against rows stranded by a provider being
# switched off after they were created. Default 6 hours -- long enough
# that a backlog drains, short enough that a scan is not parked for a
# week.
DAEMON_JOB_MAX_QUEUE_SECONDS = env.int(
    "DAEMON_JOB_MAX_QUEUE_SECONDS", default=6 * 60 * 60
)
