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

# Seconds of deadline allowance per page a job covers, on top of
# RUNPOD_REQUEST_TIMEOUT. Batching against a deliberately narrow worker
# pool makes queue time unbounded in a way it wasn't when one scan ran
# at a time, so a big volume needs more headroom than a small one before
# it is written off as wedged (issue #156).
DAEMON_JOB_SECONDS_PER_PAGE = env.float(
    "DAEMON_JOB_SECONDS_PER_PAGE", default=1.0
)

# How often (in seconds) the daemon submits jobs waiting to go out. Fast,
# because submitting blocks on nothing: the cost of a tick is one HTTP
# call per pending job.
DAEMON_SUBMIT_INTERVAL = env.int("DAEMON_SUBMIT_INTERVAL", default=5)

# How often (in seconds) the daemon sweeps every in-flight external job.
# One sweep costs one status call per open job, so this is the knob that
# trades API chatter against how long a finished job waits to be noticed.
# Slower than DAEMON_POLL_INTERVAL because the jobs it watches are
# measured in minutes, not seconds (issue #156).
DAEMON_COLLECT_INTERVAL = env.int("DAEMON_COLLECT_INTERVAL", default=15)
