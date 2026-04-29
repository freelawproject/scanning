# syntax=docker/dockerfile:1.6
#
# GPU worker image for RunPod Serverless.
#
# Runs the blackletter detect and analyze_pdf steps only. No Django,
# no AWS credentials, no DB client. Weights (YOLO small/medium/large
# + PaddleOCR PP-OCRv5) are baked in so cold start skips the network.
#
# Build from the scanning/runpod/ directory:
#     docker build -t blackletter-gpu-worker:local scanning/runpod/
#
# Run locally (requires --gpus all + nvidia-container-toolkit):
#     docker run --rm --gpus all blackletter-gpu-worker:local
#
# CUDA 12.6 on Ubuntu 24.04 LTS (Noble). 24.04 ships Python 3.12 as
# the default system python, so no deadsnakes PPA is needed. The
# Paddle cu120 index is frozen at paddlepaddle-gpu 2.6.x and predates
# APIs required by current paddlex/paddleocr (set_optimization_level
# was added in paddlepaddle 3.1). cu126 has 3.0.0 – 3.3.x wheels;
# PyTorch also publishes cu126 wheels so torch and paddle align on
# one CUDA minor. A5000 supports CUDA 12.6 fine.
# TODO: bump to a newer cu12x once both indexes catch up.

FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

# paddlepaddle-gpu links against libcuda.so.1 at import time, which
# only exists when the nvidia-container-toolkit bind-mounts the host
# driver (i.e. at `docker run --gpus all`, not at `docker build`).
# Without this stub the build-time warmup fails with
#     ImportError: libcuda.so.1: cannot open shared object file
# The -devel- variant of the CUDA image ships a stub `libcuda.so`
# explicitly for this purpose (link-time satisfies symbol resolution,
# but never gets called). At runtime on RunPod the host's real
# libcuda.so.1 is bind-mounted over this file, so GPU inference is
# unaffected.
COPY --from=nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04 \
     /usr/local/cuda/lib64/stubs/libcuda.so \
     /usr/lib/x86_64-linux-gnu/libcuda.so.1
RUN ldconfig

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    YOLO_CONFIG_DIR=/opt/ultralytics \
    PADDLEX_HOME=/opt/paddlex \
    HF_HOME=/opt/hf \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# System deps: Python 3.12 + dev headers from Ubuntu 24.04's own
# repos, tesseract + English pack, OpenGL stubs for OpenCV, git
# (git-based blackletter pin), curl + ca-certificates for HTTPS.
# libglib2.0-0t64 is the 64-bit-time_t rename that landed in 24.04.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-dev \
      git curl ca-certificates \
      tesseract-ocr tesseract-ocr-eng \
      libgl1 libglib2.0-0t64 \
 && rm -rf /var/lib/apt/lists/*

# Install uv (same tag as the main scanning Dockerfile for parity).
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

WORKDIR /app

# ── Python deps (GPU torch + paddlepaddle-gpu + everything else) ────
# pyproject.toml declares all deps; uv.lock pins them. ``uv sync
# --frozen`` installs exactly the locked versions into /opt/venv
# (per UV_PROJECT_ENVIRONMENT) and errors out if the lock is stale
# vs. pyproject.toml. The GPU wheels for torch and paddlepaddle-gpu
# come from custom indexes declared in pyproject.toml
# (pytorch cu126 + paddle cu126).
#
# paddleocr 3.5.x / paddlex 3.5.x no longer list paddlepaddle as a
# required dep; they use whatever ``paddle`` Python module is
# importable. Our explicit paddlepaddle-gpu==3.1.0 pin satisfies
# that, so there is no CPU/GPU paddle swap needed.
COPY pyproject.toml uv.lock ./
# --mount=type=cache keeps uv's wheel cache outside the image layer
# (shared across builds instead of baked into the final image). Without
# it, the multi-GB torch + paddlepaddle-gpu wheels live in both
# /root/.cache/uv AND /opt/venv, roughly doubling image size.
# --no-cache tells uv not to keep its own hash-addressed cache next to
# the installed site-packages either.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-cache

# ── Bake YOLO large.pt into the image ───────────────────────────────
# ``ensure_weights`` resolves bundled small/medium and downloads large
# from HuggingFace. Retries 3 times so a flaky HF doesn't fail the
# build. Then smoke-loads it with YOLO(...) to catch truncated files.
RUN set -e; \
    for i in 1 2 3; do \
      python -c "from blackletter.api import ensure_weights; ensure_weights(['large'])" \
        && break; \
      echo "ensure_weights attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python -c "\
import blackletter, pathlib; \
p = pathlib.Path(blackletter.__file__).resolve().parent / 'weights' / 'large.pt'; \
assert p.is_file() and p.stat().st_size > 50_000_000, f'large.pt missing or truncated: {p}'; \
from ultralytics import YOLO; YOLO(str(p)); \
print(f'large.pt OK: {p.stat().st_size // 1024 // 1024} MB')"

# ── Bake PaddleOCR PP-OCRv5 weights ─────────────────────────────────
# First instantiation downloads det + rec models into /opt/paddlex.
RUN python -c "\
from paddleocr import PaddleOCR; \
PaddleOCR(text_detection_model_name='PP-OCRv5_server_det', \
          text_recognition_model_name='PP-OCRv5_server_rec', \
          use_textline_orientation=False, \
          use_doc_orientation_classify=False, \
          use_doc_unwarping=False, \
          enable_mkldnn=False); \
print('PaddleOCR warmup OK')" \
 && find /opt/paddlex -name '*.pdiparams' | head -5

COPY handler.py .

# RunPod's serverless runtime imports handler.py; running it directly
# (via `runpod.serverless.start`) works for both local smoke-testing
# and the production RunPod worker lifecycle.
CMD ["python", "-u", "handler.py"]
