"""Mistral OCR settings (issue #191).

The Mistral read runs on Mistral's batch API: the daemon renders the
pages of one original shard with the redaction rects painted black,
uploads them, and submits one batch job per shard (see
``scanning/mistral_ocr.py`` and ``scanning/mistral_client.py``).

Two variables, deliberately, and the same two the ai-research runner
reads (``runpod/mistral/.env.example`` on its ``extraction_align``
branch). A set key is the switch: a wasted run costs money, so an
environment that must not spend leaves the key unset. Every other knob
-- the render size, the concurrency, the attempt cap, the batch
timeout -- is a module constant in ``scanning/mistral_ocr.py``, since
none has an operational reason to change per deploy, and
``ExternalJob.input_manifest`` carries a per-row override for a
one-off experiment.
"""

import environ

env = environ.FileAwareEnv()

MISTRAL_API_KEY = env.str("MISTRAL_API_KEY", default="")
MISTRAL_MODEL = env.str("MISTRAL_MODEL", default="mistral-ocr-latest")
