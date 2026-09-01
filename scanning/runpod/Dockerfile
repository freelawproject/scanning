# syntax=docker/dockerfile:1.6
#
# GPU worker image for RunPod Serverless: blackletter YOLO detection.
#
# Runs one step, ``detect``, with the bl_warm weight. No Django, no AWS
# credentials, no DB client. The weight is baked in so cold start skips
# the network.
#
# Build with the scanning/ directory as context (handler.py imports the
# shared runpod_common.py that lives there):
#     docker build -t blackletter-gpu-worker:local \
#         -f scanning/runpod/Dockerfile scanning/
#
# Run locally (requires --gpus all + nvidia-container-toolkit):
#     docker run --rm --gpus all blackletter-gpu-worker:local
#
# The base is a plain slim Python, not an nvidia/cuda image, because the
# torch cu126 wheels declare the CUDA runtime themselves (38 nvidia-*
# packages, cuDNN among them). A CUDA base layer would only ship a
# second copy. The previous image needed one for PaddlePaddle, which
# links libcuda at import and forced both the cudnn base and a
# build-time libcuda.so.1 stub; this image has no Paddle, so both are
# gone with the analyze step (#173). The wheels stay on cu126, so the
# driver requirement does not change.
#
# If a RunPod host ever fails to initialise CUDA under this base, the
# fallback is one line: FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04
# (and libglib2.0-0 becomes libglib2.0-0t64 there).

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    YOLO_CONFIG_DIR=/opt/ultralytics \
    HF_HOME=/opt/hf \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    YOLO_AUTOINSTALL=false

# System deps: git and curl + ca-certificates for HTTPS, and the two
# graphics libraries OpenGL-linked OpenCV needs. Base blackletter
# imports the non-headless opencv-python on purpose (blackletter's own
# pyproject explains why), so the libraries are not optional.
# Tesseract is gone with the analyze step.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p "$YOLO_CONFIG_DIR"

# The mkdir above is load-bearing: ultralytics writes its settings into
# a subdirectory of YOLO_CONFIG_DIR and tests the parent for
# writability, so an absent parent sends the settings to /tmp with a
# warning on every boot -- and a read-only /tmp would fail the import.

# Install uv (same tag as the other worker images for parity).
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

WORKDIR /app

# ── Python deps (GPU torch + ultralytics + blackletter) ─────────────
# pyproject.toml declares the deps; uv.lock pins them. ``uv sync
# --frozen`` installs exactly the locked versions into /opt/venv (per
# UV_PROJECT_ENVIRONMENT) and errors out if the lock is stale against
# pyproject.toml. UV_PYTHON_DOWNLOADS=never keeps it on the base image's
# interpreter instead of fetching a second one.
#
# --mount=type=cache keeps uv's wheel cache outside the image layer and
# reuses it across builds (which is why there is no --no-cache here: it
# would tell uv to bypass that mounted cache entirely).
COPY runpod/pyproject.toml runpod/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --python python3.12

# ── Bake the bl_warm weight into the image ──────────────────────────
# blackletter bundles no weights; ``ensure_weights`` downloads them from
# the public HF repo freelawproject/blackletter-weights, each pinned to
# the commit it was uploaded at. A weight already on disk resolves with
# no network, so cold start stays offline.
#
# Only bl_warm is baked. The legacy small/medium/large trio went with
# the legacy pipeline (#173), so running it again means a rebuild, not
# an input change -- which is what handler._missing_weights refuses.
#
# Retries 3 times so a flaky HF doesn't fail the build. Then verifies
# the file and opens it with YOLO(...), which catches a truncated
# download here rather than on the first paid job. The printed imgsz is
# the resolution the model predicts at: ultralytics reads it off the
# checkpoint, and 1024 is what bl_warm was trained at.
#
# HF_HOME is removed in the same layer: hf_hub_download writes the file
# into the package weights directory, and any cache copy beside it is
# 52 MB of duplicate bytes in the image.
RUN set -e; \
    for i in 1 2 3; do \
      python -c "from blackletter.api import ensure_weights; ensure_weights(['bl_warm'])" \
        && break; \
      echo "ensure_weights attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python -c "\
import blackletter, pathlib; \
from ultralytics import YOLO; \
p = pathlib.Path(blackletter.__file__).resolve().parent / 'weights' / 'bl_warm.pt'; \
assert p.is_file() and p.stat().st_size > 45_000_000, \
    f'weight missing or truncated: {p} ({p.stat().st_size if p.is_file() else 0} bytes)'; \
m = YOLO(str(p)); \
print(f'weight OK: {p.stat().st_size // 1024 // 1024} MB, imgsz={m.overrides.get(\"imgsz\")}, classes={len(m.names)}')" \
 && rm -rf /opt/hf

# Nothing may reach Hugging Face at run time now: the weight is on
# disk, and a name that is not is a caller error the handler refuses.
ENV HF_HUB_OFFLINE=1

# Base blackletter requires ocrmypdf, and this image carries neither
# Ghostscript nor tesseract, so prove the import alone is harmless.
# blackletter imports it lazily and this worker never calls it.
RUN python -c "import ocrmypdf; print('ocrmypdf imports without its binaries')"

COPY runpod_common.py runpod/handler.py ./

# RunPod's serverless runtime imports handler.py; running it directly
# (through ``runpod.serverless.start``) works for both local smoke
# tests and the production worker lifecycle.
CMD ["python", "-u", "handler.py"]
