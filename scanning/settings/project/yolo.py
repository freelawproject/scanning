"""Generalized YOLO detection settings (issue #195).

Detection runs the ``blackletter-gpu-worker`` image (issue #194) on
RunPod Serverless: one job per **original** shard, input from a
presigned GET, output to a presigned PUT (see ``scanning/yolo.py`` and
``scanning/runpod_client.py``).

Five variables, the shape of ``dots_mocr.py``. The account-level ones
already exist in ``runpod.py`` and are reused rather than duplicated per
engine: ``RUNPOD_ENABLED``, ``RUNPOD_API_KEY``, ``RUNPOD_PRESIGNED_TTL``
and ``RUNPOD_REQUEST_TIMEOUT``. The weight name and the confidence
threshold are not here at all -- they are module constants in
``scanning/yolo.py``, since neither has an operational reason to change
per deploy, and ``ExternalJob.input_manifest`` already carries a per-row
override for a one-off experiment.
"""

import environ

env = environ.FileAwareEnv()

# Master switch for the detection stage: the sweep that creates the
# rows and the wave that dispatches them, together.
#
# Since #250 the daemon starts detection by itself: on every submit
# tick ``yolo.enqueue_missing_runs`` creates one row per shard for
# every fingerprinted shard set that has no detection run yet, alive
# or dead, and ``jobs.submit_pending`` sends them on the same tick.
# Both read this switch, so turning it off stops the spend at once
# and turning it on starts the backlog draining on the next tick.
# ``yolo.ensure_detect_jobs`` is the only thing that creates DETECT
# rows, and the sweep is its only caller; a test holds that line
# (``TestKnownEnqueuePaths``), so a future caller has to change it
# deliberately rather than add one by accident.
#
# On by default, so a deploy needs no secret-store change to run the
# stage. The cost is bounded by the rule of the sweep: one run per
# shard set, ever.
YOLO_ENABLED = env.bool("YOLO_ENABLED", default=True)

# The engine's own RunPod serverless endpoint id (from the RunPod
# console). Per engine, not per account: detection and dots.mocr are
# separate endpoints on the shared key, each with its own image, its own
# graphics processing unit (GPU) class and its own worker cap. Blank
# turns this engine off without touching the other.
#
# This is the endpoint the legacy ``RUNPOD_ENDPOINT_ID`` named: #194
# pushed the rebuilt image to the same Docker Hub repository and patched
# the same RunPod template, so the id did not change. A deploy copies
# the old value under this name.
RUNPOD_YOLO_ENDPOINT_ID = env.str("RUNPOD_YOLO_ENDPOINT_ID", default="")

# How many shards may be in flight at once, which is also how many rows
# one submit tick claims. Bounded by this endpoint's own scaling, since
# each endpoint has its own worker pool.
#
# This is a debug guard on blast radius, **not** a cost control. RunPod
# bills each worker's cold start, so shards running in parallel on cold
# workers pay boot several times over, and three shards in series on one
# warm worker may cost less than three at once. The real cost control is
# the endpoint's own ``max_workers``, which must be at least this value
# or the extra rows wait in the provider's queue with the ceiling clock
# running on them (#250 raised this from 2 to 3).
YOLO_MAX_CONCURRENCY = env.int("YOLO_MAX_CONCURRENCY", default=3)

# Attempts per shard before its job is failed. Serverless workers are
# preempted, scheduled without a GPU, or land with a dead inference
# server, and all three are reported as retryable.
YOLO_MAX_ATTEMPTS = env.int("YOLO_MAX_ATTEMPTS", default=3)

# Per-page allowance added to RUNPOD_REQUEST_TIMEOUT to bound a *running*
# job (``jobs.runpod_execution_deadline``). Never applied from
# submission: queue time is free and unbounded by design, so the budget
# starts when /status first reports IN_PROGRESS.
#
# 2.0 is a first guess, deliberately generous: detection renders each
# page at 200 dots per inch (DPI) and runs one 18-class checkpoint over
# it, which is less work per page than a full page read. #211 replaces
# this with a measured value.
YOLO_SECONDS_PER_PAGE = env.float("YOLO_SECONDS_PER_PAGE", default=2.0)
