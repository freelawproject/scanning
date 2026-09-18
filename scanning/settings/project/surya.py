"""Surya OCR settings (issue #364).

Surya reads the original shards on RunPod Serverless: one job per
shard, input from a presigned GET, output to a presigned PUT (see
``scanning/surya.py``, ``scanning/runpod_client.py`` and the worker
image in ``scanning/runpod-surya/``, issue #320).

Five variables, the shape of ``dots_mocr.py``. The account-level ones
already exist in ``runpod.py`` and are reused rather than duplicated
per engine: ``RUNPOD_ENABLED``, ``RUNPOD_API_KEY``,
``RUNPOD_PRESIGNED_TTL`` and ``RUNPOD_REQUEST_TIMEOUT``. The render
resolution and the thread count are not here at all -- they are module
constants in ``scanning/surya.py``, since neither has an operational
reason to change per deploy. ``ExternalJob.input_manifest`` carries a
per-row override for a one-off experiment, and that override starts a
new run over the shard it names, because the manifest is the shard
identity (see ``surya.DPI``).
"""

import environ

env = environ.FileAwareEnv()

# Master switch for **dispatching** Surya shards to RunPod.
#
# Read the verb carefully: this gates whether the daemon submits rows
# that already exist. It does not create anything, and turning it on
# starts no work on its own.
#
# Nothing auto-creates a Surya row, and that is structural rather than
# a promise: ``surya.ensure_extract_jobs`` is the only thing that
# creates these rows, and ``views_process.start_surya_ocr`` -- the
# staff-only button -- is its only caller. The AST test of
# ``TestKnownEnqueuePaths`` holds that line, so a future daemon caller
# has to update it deliberately rather than add one by accident.
#
# On by default, therefore, so a deploy needs no secret-store change to
# make the button work. The cost stays deliberate because a person
# still has to press it, and because the endpoint id below is blank
# until an operator sets it.
SURYA_ENABLED = env.bool("SURYA_ENABLED", default=True)

# The engine's own RunPod serverless endpoint id (from the RunPod
# console). Per engine, not per account: each engine is a separate
# endpoint on the shared key, with its own image, its own GPU class and
# its own worker cap. Blank turns this engine off without touching the
# others, which is what every environment holds until the endpoint of
# #320 exists.
RUNPOD_SURYA_ENDPOINT_ID = env.str("RUNPOD_SURYA_ENDPOINT_ID", default="")

# How many shards may be in flight at once, which is also how many rows
# one submit tick claims. Bounded by this endpoint's own scaling, since
# each endpoint has its own worker pool. The worker README asks for a
# ``max_workers`` of at least this value, or the extra rows wait in the
# provider's queue with the ceiling clock running on them.
SURYA_MAX_CONCURRENCY = env.int("SURYA_MAX_CONCURRENCY", default=8)

# Attempts per shard before its job is failed. Serverless workers are
# preempted, scheduled without a GPU, or land with a dead inference
# server, and the worker reports all three as retryable.
SURYA_MAX_ATTEMPTS = env.int("SURYA_MAX_ATTEMPTS", default=3)

# Per-page allowance added to RUNPOD_REQUEST_TIMEOUT to bound a
# *running* job (``jobs.runpod_execution_deadline``). Never applied from
# submission: queue time is free and unbounded by design, so the budget
# starts when /status first reports IN_PROGRESS.
#
# 4.0 is a first guess. The worker's smoke run read 15 pages in 43 s
# (2.9 s a page) on one 24 GB card, and the kit measured about 3 s a
# page on an A40; a page whose answer loops pays surya's 12288-token
# ladder on top. Measure a shard of 100 pages and change this then.
SURYA_SECONDS_PER_PAGE = env.float("SURYA_SECONDS_PER_PAGE", default=4.0)
