import environ

env = environ.FileAwareEnv()

# How often (in seconds) the web-worker monitor thread wakes up to log the
# in-flight request count + active thread count and check for hung requests.
WEB_MONITOR_INTERVAL_SECONDS = env.float(
    "WEB_MONITOR_INTERVAL_SECONDS", default=10.0
)

# A request still in flight for longer than this (seconds) is logged as hung by
# the monitor thread, together with its endpoint, scan_pk and user, *while it is
# still running* — gunicorn's access log only writes on completion, so a request
# that hangs until the worker is killed never logs there.
WEB_INFLIGHT_WARN_SECONDS = env.float(
    "WEB_INFLIGHT_WARN_SECONDS", default=30.0
)
