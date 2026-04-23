"""RunPod pod client settings.

Configures the daemon side (``scanning/runpod_client.py`` and
``scanning/pod_manager.py``). Pod-side env vars (``SENTRY_DSN_GPU``,
``POD_API_KEY``, ``PADDLEX_HOME``, etc.) live on the RunPod pod
configuration, not in Django settings.
"""

import environ

env = environ.FileAwareEnv()

# Master switch. When False, runpod_client.detect / analyze fall
# through to the in-process blackletter calls. Default False so dev
# and CI keep working without RunPod credentials.
RUNPOD_ENABLED = env.bool("RUNPOD_ENABLED", default=False)

# RunPod REST API key (used for pod lifecycle: start, stop, status).
# Treat as a secret; never log.
RUNPOD_API_KEY = env.str("RUNPOD_API_KEY", default="")

# ID of the RunPod pod that serves the FastAPI inference endpoints.
# Required when RUNPOD_ENABLED=True. The pod is expected to expose
# port ``RUNPOD_POD_PORT`` running ``scanning/runpod/server.py``.
RUNPOD_POD_ID = env.str("RUNPOD_POD_ID", default="")

# Port the FastAPI server listens on inside the pod.
RUNPOD_POD_PORT = env.int("RUNPOD_POD_PORT", default=8000)

# Bearer token the daemon sends on every request to the pod. Distinct
# from RUNPOD_API_KEY. Generate with ``python -c 'import secrets;
# print(secrets.token_urlsafe(32))'`` and set the same value on the
# pod's ``POD_API_KEY`` env var.
RUNPOD_POD_API_KEY = env.str("RUNPOD_POD_API_KEY", default="")

# Hard wall-clock ceiling (seconds) for a single detect / analyze
# HTTP call to the pod. Covers the longest observed analyze run
# (~150 s on 1359 pages) with headroom.
RUNPOD_REQUEST_TIMEOUT = env.int("RUNPOD_REQUEST_TIMEOUT", default=1800)

# Retries on network-level errors (ConnectionError / ReadTimeout)
# while dispatching to the pod. Terminal pod-side failures are not
# retried at this layer.
RUNPOD_MAX_RETRIES = env.int("RUNPOD_MAX_RETRIES", default=2)

# Max wall-clock seconds to wait for the pod to transition from
# EXITED to RUNNING with /health returning 200. Pod cold start on
# RunPod is typically 2-5 min; 600 s leaves plenty of margin.
RUNPOD_POD_BOOT_TIMEOUT = env.int("RUNPOD_POD_BOOT_TIMEOUT", default=600)

# After the last scan finishes, wait this many seconds of true queue
# idleness before stopping the pod. Prevents thrash where a new scan
# arrives seconds after the previous one finishes and the pod has
# already been told to stop.
RUNPOD_POD_IDLE_GRACE_SECONDS = env.int(
    "RUNPOD_POD_IDLE_GRACE_SECONDS", default=120
)

# How often ``stop_idle_gpu_pod`` runs in the daemon scheduler. Short
# enough to react promptly after a batch drains; long enough not to
# spam the RunPod REST API.
RUNPOD_POD_STOP_POLL_SECONDS = env.int(
    "RUNPOD_POD_STOP_POLL_SECONDS", default=30
)

# Lifetime of presigned GET URLs handed to the pod (seconds). Long
# enough to cover pod boot + execution + retries; trivially scoped
# (read on a single object key, so even a leaked URL lets someone
# fetch one PDF they couldn't otherwise see).
RUNPOD_PRESIGNED_TTL = env.int("RUNPOD_PRESIGNED_TTL", default=3600)
