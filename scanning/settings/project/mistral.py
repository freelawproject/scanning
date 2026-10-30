"""Mistral OCR settings (issue #191).

The Mistral read runs on Mistral's batch API: the daemon renders the
pages of one shard, uploads them, and submits one batch job per shard
(see ``scanning/mistral_ocr.py`` and ``scanning/mistral_client.py``).

The read is over the original shard set, the one dots.mocr and YOLO
read, so a set key is the only thing the stage waits for.

The key and the model are the two the ai-research runner reads
(``runpod/mistral/.env.example`` on its ``extraction_align`` branch). A
set key is the switch: a wasted run costs money, so an environment that
must not spend leaves the key unset.

The three throughput knobs are settings because they are tuned per
deploy against the workspace's rate limit and the pod's memory, which
no constant can know. Every other knob -- the render size, the attempt
cap, the batch timeout -- is a module constant in
``scanning/mistral_ocr.py``, and ``ExternalJob.input_manifest`` carries
a per-row override for a one-off experiment.
"""

import environ

env = environ.FileAwareEnv()

MISTRAL_API_KEY = env.str("MISTRAL_API_KEY", default="")
MISTRAL_MODEL = env.str("MISTRAL_MODEL", default="mistral-ocr-latest")

# Shards one submit wave renders and uploads at once. Zero is "by mode"
# (``mistral_ocr.submits_per_tick``): one under the daemon, whose
# serial scheduler the wave blocks, and a volume's worth under Celery,
# where the wave is its own locked task.
MISTRAL_SUBMITS_PER_TICK = env.int("MISTRAL_SUBMITS_PER_TICK", default=0)

# Batch jobs in flight at Mistral at once, over every volume. Batches
# do not count against the real-time rate limits, so this bounds blast
# radius and nothing else; it must hold more than one volume's shards,
# or the next volume waits on this one's batches.
MISTRAL_MAX_CONCURRENCY = env.int("MISTRAL_MAX_CONCURRENCY", default=64)

# Shards downloaded and rendered at once inside a wave. Each holds its
# shard PDF on disk (up to SHARD_TARGET_BYTES), an open document and a
# page in hand, so this bounds the wave's disk and memory where the
# upload backlog bounds its PNGs; the shards past it wait their turn.
MISTRAL_RENDER_WORKERS = env.int("MISTRAL_RENDER_WORKERS", default=8)

# Page uploads in flight at once, over every shard of the wave: the
# one number to lower when the throttled-page log line gets busy, and
# to raise when the workspace's limit allows. The wave holds at most
# twice this many rendered pages in memory, whatever the shard count.
MISTRAL_UPLOAD_WORKERS = env.int("MISTRAL_UPLOAD_WORKERS", default=32)

# The RunPod CPU endpoint that renders and uploads the pages of a shard
# (``scanning/runpod-mistral-render/``). Set, beside ``RUNPOD_ENABLED``
# and ``RUNPOD_API_KEY``, the wave sends every shard it claims to that
# endpoint as its own job and only writes the manifest and creates the
# batch itself; blank, the wave renders on the pod as before, and the
# two render knobs above apply. Per engine like the other endpoint ids,
# because it is a separate endpoint with its own image and worker pool.
RUNPOD_MISTRAL_RENDER_ENDPOINT_ID = env.str(
    "RUNPOD_MISTRAL_RENDER_ENDPOINT_ID", default=""
)
