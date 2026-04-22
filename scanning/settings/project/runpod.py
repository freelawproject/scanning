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

# Hard wall-clock ceiling (seconds) for the combined submit + poll
# loop. Includes cold start + queue + execution. Client cancels the
# job and raises if exceeded.
RUNPOD_REQUEST_TIMEOUT = env.int("RUNPOD_REQUEST_TIMEOUT", default=1800)

# Retries on transport errors when submitting a job (network blips,
# 5xx from the RunPod API). Terminal job failures are NOT retried.
RUNPOD_MAX_RETRIES = env.int("RUNPOD_MAX_RETRIES", default=2)

# Lifetime of presigned GET URLs handed to the worker (seconds). Long
# enough to cover cold start + queue + retries + GPU-capacity-shortage
# delays; trivially scoped (read on a single object key, so even a
# leaked URL lets someone fetch one PDF they couldn't otherwise see).
RUNPOD_PRESIGNED_TTL = env.int("RUNPOD_PRESIGNED_TTL", default=3600)
