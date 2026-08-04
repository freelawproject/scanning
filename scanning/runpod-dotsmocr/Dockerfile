# syntax=docker/dockerfile:1.6
#
# GPU worker image for RunPod Serverless: dots.mocr document parsing.
#
# Runs rednote-hilab/dots.mocr (3B Qwen-VL-based multimodal OCR) behind
# a local vLLM server, driven by handler.py. No Django, no AWS
# credentials, no DB client. Model weights are baked in so cold start
# skips the network.
#
# Build from the scanning/runpod-dotsmocr/ directory:
#     docker build -t dotsmocr-gpu-worker:local scanning/runpod-dotsmocr/
#
# Run locally (requires --gpus all + nvidia-container-toolkit):
#     docker run --rm --gpus all dotsmocr-gpu-worker:local
#
# The base is the official vLLM OpenAI-server image, which is exactly
# what upstream dots.mocr recommends for deployment ("Since vLLM
# version 0.11.0, Dots OCR has been officially integrated into vLLM").
# The plain (non -cu130) tag is used deliberately: RunPod community
# hosts still run drivers below CUDA 13, and the default-CUDA build
# works on both.

FROM vllm/vllm-openai:v0.17.1

# Two Python environments live in this image, on purpose:
#  - the base image's system env, which owns vllm/torch and serves the
#    model (started as a subprocess by handler.py);
#  - /opt/venv, a uv-managed env that owns the handler and its client
#    deps (runpod, openai, dots_mocr utils). Keeping them apart means
#    dots_mocr's pins can never fight vLLM's carefully-pinned stack.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# System deps: git so uv can fetch the dots_mocr git pin, libcairo2
# for cairosvg (imported at module scope by dots_mocr's package
# __init__ chain, even though this worker never renders SVG).
RUN apt-get update && apt-get install -y --no-install-recommends \
      git libcairo2 \
 && rm -rf /var/lib/apt/lists/*

# Install uv (same tag as the other worker images for parity).
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

WORKDIR /app

# ── Handler venv ─────────────────────────────────────────────────────
# pyproject.toml declares the handler deps; uv.lock pins them
# (including dots_mocr at an exact upstream commit). ``uv sync
# --frozen`` installs exactly the locked versions into /opt/venv and
# errors out if the lock is stale vs. pyproject.toml.
# --mount=type=cache keeps uv's wheel cache outside the image layer;
# --no-cache tells uv not to keep its own hash-addressed cache next to
# the installed site-packages either.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-cache --python python3

# ── Bake model weights ───────────────────────────────────────────────
# Downloads the full HF snapshot (~6 GB safetensors + processor code)
# into /opt/hf using the base image's huggingface_hub. Retries 3 times
# so a flaky HF doesn't fail the build, then verifies the snapshot
# actually contains the weights (a truncated download would otherwise
# surface as a cryptic vLLM load error at runtime).
ARG DOTSMOCR_MODEL=rednote-hilab/dots.mocr
ENV DOTSMOCR_MODEL=${DOTSMOCR_MODEL}
RUN set -e; \
    for i in 1 2 3; do \
      python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${DOTSMOCR_MODEL}')" \
        && break; \
      echo "snapshot_download attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python3 -c "\
import pathlib; \
files = list(pathlib.Path('/opt/hf').rglob('*.safetensors')); \
total = sum(f.stat().st_size for f in files); \
assert total > 4_000_000_000, f'weights missing or truncated: {total} bytes in {len(files)} files'; \
print(f'weights OK: {total // 1024 // 1024} MB in {len(files)} files')"

# Everything the worker needs is now on disk; never touch the network
# for model files at runtime. (Overriding DOTSMOCR_MODEL at runtime
# therefore requires either a rebuild with the matching build arg or
# unsetting HF_HUB_OFFLINE on the endpoint.)
ENV HF_HUB_OFFLINE=1

COPY handler.py .

# The base image's entrypoint launches the vLLM API server directly;
# this worker instead boots the RunPod handler, which spawns and
# supervises ``vllm serve`` itself (see handler._preload).
ENTRYPOINT []
CMD ["/opt/venv/bin/python", "-u", "handler.py"]
