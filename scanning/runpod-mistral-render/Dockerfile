# syntax=docker/dockerfile:1.6
#
# CPU worker image for RunPod Serverless: the Mistral page render.
#
# Runs one step, ``render``: download one shard, render every page as
# the ensemble's canonical PNG, upload each to Mistral's files API and
# answer the file ids. No GPU, no Django, no AWS credentials, no DB
# client. The Mistral key comes from the endpoint template's env.
#
# Build with the scanning/ directory as context (handler.py imports the
# shared runpod_common.py and perpendiculars.py that live there):
#     docker build -t mistral-render-worker:local \
#         -f scanning/runpod-mistral-render/Dockerfile scanning/
#
# Run locally:
#     docker run --rm -e MISTRAL_API_KEY=... mistral-render-worker:local

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

# The three *_NUM_THREADS above: the pages are rendered by a pool of
# processes already sized to the cores, so a BLAS or OpenMP pool of one
# thread per core inside each of them only thrashes. OpenCV's own pool
# is capped in handler.py.

# ca-certificates for HTTPS to S3 and Mistral; libglib for the headless
# OpenCV the perpendiculars whiteout imports.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Install uv (same tag as the other worker images for parity).
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

WORKDIR /app

# pyproject.toml declares the deps; uv.lock pins them. ``uv sync
# --frozen`` installs exactly the locked versions into /opt/venv and
# errors out if the lock is stale against pyproject.toml.
COPY runpod-mistral-render/pyproject.toml runpod-mistral-render/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --python python3.12

# Prove the render stack imports: a wheel missing a system library
# fails the build here and not the first paid job.
RUN python -c "import fitz, PIL, cv2, numpy; print('render stack OK', fitz.__doc__)"

COPY runpod_common.py perpendiculars.py runpod-mistral-render/handler.py ./

# RunPod's serverless runtime imports handler.py; running it directly
# (through ``runpod.serverless.start``) works for both local smoke
# tests and the production worker lifecycle.
CMD ["python", "-u", "handler.py"]
