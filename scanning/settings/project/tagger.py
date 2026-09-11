"""Case-law block tagger settings.

The tagger labels the structural parts of each opinion (party, docket
number, court, judges, disposition, ...) on RunPod Serverless: one job
per **volume**, input from a presigned GET of a JSON document the
daemon writes, output to a presigned PUT (see ``scanning/tagger.py``,
``scanning/tagger_input.py`` and ``scanning/runpod-caselaw-tagger/``).

Five variables, the shape of ``dots_mocr.py`` and ``yolo.py``. The
account-level ones already exist in ``runpod.py`` and are reused rather
than duplicated per engine: ``RUNPOD_ENABLED``, ``RUNPOD_API_KEY``,
``RUNPOD_PRESIGNED_TTL`` and ``RUNPOD_REQUEST_TIMEOUT``.
"""

import environ

env = environ.FileAwareEnv()

# Master switch for **dispatching** tagger jobs to RunPod. Like the
# dots.mocr switch it gates the submit of rows that already exist; it
# enqueues nothing. Nothing auto-enqueues this stage yet: the only
# creator of TAG rows is ``tagger.ensure_tag_jobs`` and its one caller
# is the ``enqueue_caselaw_tagger`` command, a staff decision, pinned
# by ``TestKnownEnqueuePaths``. A tick pass comes after the stage has
# been watched on a few volumes.
TAGGER_ENABLED = env.bool("TAGGER_ENABLED", default=True)

# The engine's own RunPod serverless endpoint id (from the RunPod
# console): the ``caselaw-tagger-gpu-worker`` endpoint. Per engine, not
# per account, like the other two; blank turns this engine off without
# touching them. The production key must be allowed to invoke this
# endpoint, or every submit answers 403.
RUNPOD_TAGGER_ENDPOINT_ID = env.str("RUNPOD_TAGGER_ENDPOINT_ID", default="")

# How many jobs may be in flight at once, which is also how many rows
# one submit tick claims. One job is one volume, so this is volumes in
# flight; the endpoint's ``max_workers`` (3) must be at least this.
TAGGER_MAX_CONCURRENCY = env.int("TAGGER_MAX_CONCURRENCY", default=3)

# Attempts per volume before its job is failed.
TAGGER_MAX_ATTEMPTS = env.int("TAGGER_MAX_ATTEMPTS", default=3)

# Per-page allowance added to RUNPOD_REQUEST_TIMEOUT to bound a
# *running* job (``jobs.runpod_execution_deadline`` reads the row's
# ``page_count``: for this stage, the pages the sent text covers, so a
# two-hundred-page case counts as two hundred). The first GPU run
# tagged 65 pages of text in four seconds, so one second a page is
# generous; the base timeout carries the cold start.
TAGGER_SECONDS_PER_PAGE = env.float("TAGGER_SECONDS_PER_PAGE", default=1.0)
