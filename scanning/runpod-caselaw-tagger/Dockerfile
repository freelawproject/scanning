# syntax=docker/dockerfile:1.6
#
# GPU worker image for RunPod Serverless: the case-law block tagger.
#
# Runs one step, ``tag``, with freelawproject/caselaw-block-tagger (a
# ModernBERT-large token classifier). No Django, no AWS credentials, no
# DB client. The weights are baked in so cold start skips the network.
#
# Build with the scanning/ directory as context (handler.py imports the
# shared runpod_common.py that lives there):
#     docker build -t caselaw-tagger-gpu-worker:local \
#         -f scanning/runpod-caselaw-tagger/Dockerfile scanning/
#
# Run locally (requires --gpus all + nvidia-container-toolkit):
#     docker run --rm --gpus all caselaw-tagger-gpu-worker:local
#
# Run locally on a laptop with no GPU (slow, for a smoke test only):
#     docker run --rm -e HANDLER_ALLOW_CPU=1 caselaw-tagger-gpu-worker:local
#
# The base is a plain slim Python, not an nvidia/cuda image, because the
# torch cu126 wheels declare the CUDA runtime themselves (cuDNN among
# them). A CUDA base layer would only ship a second copy. Same rule as
# ../runpod/; see its Dockerfile for the fallback if a host ever fails
# to initialise CUDA under this base.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# System deps: ca-certificates for HTTPS, and gcc for Triton. Nothing
# renders a page here, so no graphics libraries and no PyMuPDF.
#
# gcc is load-bearing on the GPU, though no build step uses it: torch
# runs some eager operations through Triton kernels on CUDA (2.14
# routed the rotary-embedding outer product that way), and Triton
# compiles a small C helper on its first call, at run time, inside the
# worker. The slim base carries no compiler, so the first GPU job on
# RunPod failed with "Failed to find C compiler" after a clean boot and
# model load. The torch pin in pyproject.toml keeps to the version that
# does not take that path; the compiler is the backstop for the next
# version that does. About 60 MB.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates gcc \
 && rm -rf /var/lib/apt/lists/*

# Install uv (same tag as the other worker images for parity).
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

WORKDIR /app

# ── Python deps (GPU torch + transformers) ──────────────────────────
# pyproject.toml declares the deps; uv.lock pins them. ``uv sync
# --frozen`` installs exactly the locked versions into /opt/venv (per
# UV_PROJECT_ENVIRONMENT) and errors out if the lock is stale against
# pyproject.toml. UV_PYTHON_DOWNLOADS=never keeps it on the base image's
# interpreter instead of fetching a second one.
#
# --mount=type=cache keeps uv's wheel cache outside the image layer and
# reuses it across builds (which is why there is no --no-cache here: it
# would tell uv to bypass that mounted cache entirely).
#
# The venv comes BEFORE the weights, unlike ../runpod-dotsmocr/, because
# the bake below needs huggingface_hub and transformers from this venv
# (the dots.mocr base image carries its own). The cost is that a
# dependency bump re-downloads the 1.6 GB snapshot; the weights change
# far less often than that.
COPY runpod-caselaw-tagger/pyproject.toml runpod-caselaw-tagger/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --python python3.12

# ── Bake the model into the image ───────────────────────────────────
# Downloads the full HF snapshot (~1.6 GB fp32 safetensors, tokenizer,
# config) as plain files into /opt/model. Retries 3 times so a flaky HF
# doesn't fail the build, then verifies the weights are there and opens
# the model once on the CPU: that proves the installed transformers can
# read this checkpoint's config (it was saved by transformers 5), which
# would otherwise surface as a load error on the first paid job. The
# printed label count and context length are what the handler reads at
# run time.
#
# The last check encodes an input longer than the context with
# ``truncation=False``, the call the handler makes, and asserts nothing
# was cut. The checkpoint's tokenizer.json carries a saved truncation
# rule at 8,192 tokens; the handler relies on the explicit argument to
# turn it off, because it windows long cases itself and a silent cut
# would drop the disposition at the end of every long case. A
# transformers release that changed how the argument meets the saved
# rule would fail here, not on a paid job.
#
# Overriding TAGGER_MODEL at run time requires a rebuild with the
# matching build arg: HF_HUB_OFFLINE below keeps the worker off the
# network for weights.
ARG TAGGER_MODEL=freelawproject/caselaw-block-tagger
ENV TAGGER_MODEL=${TAGGER_MODEL} \
    TAGGER_MODEL_DIR=/opt/model
RUN set -e; \
    for i in 1 2 3; do \
      python -c "from huggingface_hub import snapshot_download; snapshot_download('${TAGGER_MODEL}', local_dir='/opt/model')" \
        && break; \
      echo "snapshot_download attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python -c "\
import pathlib; \
files = list(pathlib.Path('/opt/model').glob('*.safetensors')); \
total = sum(f.stat().st_size for f in files); \
assert total > 1_400_000_000, f'weights missing or truncated: {total} bytes in {len(files)} files'; \
print(f'weights OK: {total // 1024 // 1024} MB in {len(files)} files')" \
 && python -c "\
from transformers import AutoModelForTokenClassification, AutoTokenizer; \
tok = AutoTokenizer.from_pretrained('/opt/model'); \
m = AutoModelForTokenClassification.from_pretrained('/opt/model'); \
limit = m.config.max_position_embeddings; \
n = len(tok('word ' * (limit * 2), truncation=False)['input_ids']); \
assert n > limit, f'tokenizer cut a long input to {n} tokens: truncation=False did not turn off the saved rule'; \
print(f'model OK: labels={len(m.config.id2label)}, max_tokens={limit}, vocab={len(tok)}, long input={n} tokens untruncated')" \
 && rm -rf /opt/hf

# Nothing may reach Hugging Face at run time now: the snapshot is on
# disk and the handler loads it by path.
ENV HF_HUB_OFFLINE=1

COPY runpod_common.py runpod-caselaw-tagger/handler.py ./

# RunPod's serverless runtime imports handler.py; running it directly
# (through ``runpod.serverless.start``) works for both local smoke
# tests and the production worker lifecycle.
CMD ["python", "-u", "handler.py"]
