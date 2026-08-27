"""RunPod Serverless client settings.

Account-level settings shared by every RunPod engine. Per-engine ones
(the endpoint id, the concurrency cap, the attempt cap, the per-page
allowance) live with their engine -- see
``scanning/settings/project/dots_mocr.py``.

The worker-side env vars (``SENTRY_DSN_GPU``, ``HANDLER_DPI``, etc.)
live on the RunPod endpoint configuration, not in Django settings.
"""

import environ

env = environ.FileAwareEnv()

# Master switch: whether GPU jobs are dispatched to RunPod at all.
# There is no local execution fallback anymore (the in-process
# blackletter calls left with the legacy pipeline, issue #173), so
# with this off an environment can upload and browse but not process.
# Default False so dev and CI never need RunPod credentials.
RUNPOD_ENABLED = env.bool("RUNPOD_ENABLED", default=False)

# RunPod API key. Treat as a secret; never log. One account key; the
# endpoint id is per engine and lives with that engine's settings
# (``RUNPOD_DOTSMOCR_ENDPOINT_ID`` in ``dots_mocr.py``), because each
# engine is a separate serverless endpoint with its own image and its
# own worker pool.
RUNPOD_API_KEY = env.str("RUNPOD_API_KEY", default="")

# Base wall-clock budget (seconds) for a *running* job, before the
# per-page allowance an engine adds (see
# ``jobs.runpod_execution_deadline``). Never counted from submission:
# queue time is free and unbounded by design, so the budget starts when
# /status first reports IN_PROGRESS. Covers cold start and model load,
# which a queued job has not paid yet.
RUNPOD_REQUEST_TIMEOUT = env.int("RUNPOD_REQUEST_TIMEOUT", default=1800)

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
