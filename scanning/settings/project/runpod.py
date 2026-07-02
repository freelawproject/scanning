"""RunPod Serverless client settings.

These configure the daemon side (``scanning/runpod_client.py``). The
worker-side env vars (``SENTRY_DSN_GPU``, ``PADDLEX_HOME``, etc.) live
on the RunPod endpoint configuration, not in Django settings.
"""

import environ

env = environ.FileAwareEnv()

# Master switch. When False, runpod_client.detect / analyze fall
# through to the in-process blackletter calls. Default False so dev
# and CI keep working without RunPod credentials.
RUNPOD_ENABLED = env.bool("RUNPOD_ENABLED", default=False)

# RunPod serverless endpoint id (from the RunPod console).
RUNPOD_ENDPOINT_ID = env.str("RUNPOD_ENDPOINT_ID", default="")

# RunPod API key. Treat as a secret; never log.
RUNPOD_API_KEY = env.str("RUNPOD_API_KEY", default="")

# Ceiling (seconds) on how long the handler may *run* once a worker picks
# up the job (status IN_PROGRESS). Does not include the queue wait, which is
# bounded separately by RUNPOD_QUEUE_TIMEOUT, so a serverless cold start
# never eats into execution time. Client cancels the job and raises if
# exceeded.
RUNPOD_REQUEST_TIMEOUT = env.int("RUNPOD_REQUEST_TIMEOUT", default=3600)

# Ceiling (seconds) on how long to wait for a worker to pick the job up
# (status IN_QUEUE) before giving up. Serverless cold starts with no warm
# workers can take several minutes; exceeding this re-queues the scan
# (transient) rather than failing it.
RUNPOD_QUEUE_TIMEOUT = env.int("RUNPOD_QUEUE_TIMEOUT", default=1200)

# Retries on transport errors when submitting a job (network blips,
# 5xx from the RunPod API). Terminal job failures are NOT retried.
RUNPOD_MAX_RETRIES = env.int("RUNPOD_MAX_RETRIES", default=2)

# Lifetime of presigned GET URLs handed to the worker (seconds).
# Default 86400 (1 day): generous headroom for cold start + queue +
# retries + GPU-capacity-shortage delays, so a URL never expires
# before the worker actually fetches it. Trivially scoped (read on a
# single object key, so even a leaked URL lets someone fetch one PDF
# they couldn't otherwise see); AWS SigV4 max is 7 days.
RUNPOD_PRESIGNED_TTL = env.int("RUNPOD_PRESIGNED_TTL", default=86400)

# Maximum number of transient RunPod failures (e.g. NO_GPU) before a
# scan is escalated to ERROR instead of being re-queued. A scan that
# hits this cap must be manually re-queued by staff. Default 5 keeps
# a wedged endpoint from burning API quota indefinitely.
RUNPOD_MAX_TRANSIENT_RETRIES = env.int(
    "RUNPOD_MAX_TRANSIENT_RETRIES", default=5
)
