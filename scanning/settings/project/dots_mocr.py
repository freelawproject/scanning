"""dots.mocr settings (issue #190).

dots.mocr reads the original shards on RunPod Serverless: one job per
shard, input from a presigned GET, output to a presigned PUT (see
``scanning/dots_mocr.py`` and ``scanning/runpod_client.py``).

Five variables, deliberately. The account-level ones already exist in
``runpod.py`` and are reused rather than duplicated per engine:
``RUNPOD_ENABLED``, ``RUNPOD_API_KEY``, ``RUNPOD_PRESIGNED_TTL`` and
``RUNPOD_REQUEST_TIMEOUT``. The render resolution and the prompt mode are
not here at all -- they are module constants in ``scanning/dots_mocr.py``,
since neither has an operational reason to change per deploy, and
``ExternalJob.input_manifest`` already carries a per-row override for a
one-off experiment.
"""

import environ

env = environ.FileAwareEnv()

# Master switch for dispatching shards to dots.mocr.
#
# Off by default, unlike DOCTOR_ENABLED. Every press starts real
# graphics processing unit (GPU) work on RunPod, and issue #190 ships
# the stage as a staff-only button precisely so the cost is deliberate.
# An environment turns it on when someone means to spend money.
DOTS_MOCR_ENABLED = env.bool("DOTS_MOCR_ENABLED", default=False)

# The engine's own RunPod serverless endpoint id (from the RunPod
# console). Per engine, not per account: dots.mocr and the coming YOLO
# worker are separate endpoints on the shared key, each with its own
# image, its own GPU class and its own worker cap. Blank turns this
# engine off without touching the other.
RUNPOD_DOTSMOCR_ENDPOINT_ID = env.str(
    "RUNPOD_DOTSMOCR_ENDPOINT_ID", default=""
)

# How many shards may be in flight at once, which is also how many rows
# one submit tick claims. Bounded by this endpoint's own scaling, since
# each endpoint has its own worker pool.
#
# This is a debug guard on blast radius, **not** a cost control. RunPod
# bills each worker's cold start, so shards running in parallel on cold
# workers pay boot several times over, and three shards in series on one
# warm worker may cost less than three at once. The real cost control is
# the endpoint's own ``max_workers``. Low while the stage is new.
DOTS_MOCR_MAX_CONCURRENCY = env.int("DOTS_MOCR_MAX_CONCURRENCY", default=2)

# Attempts per shard before its job is failed. Serverless workers are
# preempted, scheduled without a GPU, or land with a dead inference
# server, and all three are reported as retryable.
DOTS_MOCR_MAX_ATTEMPTS = env.int("DOTS_MOCR_MAX_ATTEMPTS", default=3)

# Per-page allowance added to RUNPOD_REQUEST_TIMEOUT to bound a *running*
# job (``jobs.runpod_execution_deadline``). Never applied from
# submission: queue time is free and unbounded by design, so the budget
# starts when /status first reports IN_PROGRESS.
#
# 4.0 comes from the worker README's own measurement: 811s over 312
# pages, on one A5000 with 16 pages in flight. Raise it if a volume of
# dense pages starts hitting the deadline rather than finishing.
DOTS_MOCR_SECONDS_PER_PAGE = env.float(
    "DOTS_MOCR_SECONDS_PER_PAGE", default=4.0
)
