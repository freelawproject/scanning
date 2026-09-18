# syntax=docker/dockerfile:1.6
#
# GPU worker image for RunPod Serverless: Surya OCR 2 full-page reads.
#
# Runs datalab-to/surya-ocr-2 (a Qwen3.5-based vision-language OCR
# model) behind a local vLLM server, driven by handler.py. No Django,
# no AWS credentials, no DB client. Model weights are baked in so cold
# start skips the network.
#
# Build with the scanning/ directory as context (handler.py imports the
# shared runpod_common.py that lives there):
#     docker build -t surya-gpu-worker:local \
#         -f scanning/runpod-surya/Dockerfile scanning/
#
# Run locally (requires --gpus all + nvidia-container-toolkit):
#     docker run --rm --gpus all surya-gpu-worker:local
#
# ── vLLM VERSION MATTERS ─────────────────────────────────────────────
# surya-ocr-2's architecture is `Qwen3_5ForConditionalGeneration`,
# registered in vLLM only from 0.20.1 (Datalab pins
# vllm/vllm-openai:v0.20.1, and so does surya's own launcher). The
# dots.mocr image's 0.17.1 refuses the model at load with "Model
# architectures [...] are not supported". surya-ocr has no vLLM
# dependency and registers no plugin -- its vLLM backend is an OpenAI
# client -- so the arch has to be native to the running server.
#
# The build gates on this explicitly (see "arch gate" below) so an
# unsupported base fails here, on a cheap build host, instead of on a
# billed GPU worker several minutes into cold start. Bump VLLM_TAG if
# the gate fails. The plain (non -cu130) tag is used deliberately:
# RunPod community hosts still run drivers below CUDA 13.
ARG VLLM_TAG=v0.20.1
FROM vllm/vllm-openai:${VLLM_TAG}

# Two Python environments live in this image, on purpose:
#  - the base image's system env, which owns vllm/torch and serves the
#    model (started as a subprocess by handler.py);
#  - /opt/venv, a uv-managed env that owns the handler and its client
#    deps (runpod, surya-ocr and the CPU torch its import needs).
#    Keeping them apart means surya's pins (pillow < 11, an exact
#    opencv) can never fight vLLM's carefully-pinned stack.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install uv (same tag as the other worker images for parity). The
# vLLM base image pip-installs its own uv into /usr/local/bin, which
# precedes /bin on PATH and would silently shadow this pin -- so the
# sync below invokes /bin/uv by absolute path.
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

WORKDIR /app

# ── Arch gate ────────────────────────────────────────────────────────
# Ask the running vLLM's model registry whether it knows this
# architecture. No GPU needed -- this is a lookup, not a load.
#
# A registry miss is fatal (the image would be dead on arrival). A
# failure of the check ITSELF is not: vLLM moves this API around
# between releases, and a build must not break just because the probe
# went stale. In that case it warns and the runtime health check
# remains the backstop.
ARG SURYA_MODEL=datalab-to/surya-ocr-2
ARG SURYA_ARCH=Qwen3_5ForConditionalGeneration
RUN python3 - <<PY
import sys
arch = "${SURYA_ARCH}"
try:
    from vllm.model_executor.models.registry import ModelRegistry
    supported = set(ModelRegistry.get_supported_archs())
except Exception as exc:
    print(f"!! arch gate could not run ({exc!r}); skipping", file=sys.stderr)
    sys.exit(0)
if arch in supported:
    import vllm
    print(f"arch gate OK: {arch} registered in vLLM {vllm.__version__}")
    sys.exit(0)
print(
    f"!! vLLM does not register {arch}.\n"
    f"!! Rebuild with a newer base, e.g.:\n"
    f"!!   docker build --build-arg VLLM_TAG=<newer> -f scanning/runpod-surya/Dockerfile scanning/",
    file=sys.stderr,
)
sys.exit(1)
PY

# ── Bake model weights ───────────────────────────────────────────────
# Downloads the full HF snapshot (~1.4 GB safetensors plus the
# processor and tokenizer files) into /opt/hf using the base image's
# huggingface_hub. The repo also carries a few MB of README assets;
# they are left out. Retries 3 times so a flaky HF doesn't fail the
# build, then verifies the snapshot actually holds the weights (a
# truncated download would otherwise surface as a cryptic vLLM load
# error at runtime).
#
# Deliberately ordered BEFORE the handler venv: the weights only need
# the base image, so a dependency bump in pyproject/uv.lock rebuilds
# the (small) venv layer without re-downloading the weights.
ENV SURYA_MODEL=${SURYA_MODEL}
RUN set -e; \
    for i in 1 2 3; do \
      python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${SURYA_MODEL}', ignore_patterns=['assets/*', '*.png', '.eval_results/*'])" \
        && break; \
      echo "snapshot_download attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python3 -c "\
import pathlib; \
files = list(pathlib.Path('/opt/hf').rglob('*.safetensors')); \
total = sum(f.stat().st_size for f in files); \
assert total > 1_200_000_000, f'weights missing or truncated: {total} bytes in {len(files)} files'; \
print(f'weights OK: {total // 1024 // 1024} MB in {len(files)} files')"

# Everything the worker needs is now on disk; never touch the network
# for model files at runtime. (Overriding SURYA_MODEL at runtime
# therefore requires a rebuild with the matching build arg, or
# unsetting HF_HUB_OFFLINE on the endpoint.)
ENV HF_HUB_OFFLINE=1

# ── Offline load gate ────────────────────────────────────────────────
# Prove the baked cache is self-sufficient under the exact conditions
# the worker boots in: HF_HUB_OFFLINE=1, no network, nothing to fetch.
# This catches a transformers too old for the model's processor -- the
# failure mode that otherwise appears as a vLLM load error minutes into
# a billed cold start. Runs after the bake so it needs no network of
# its own, and is fatal because an image that fails here is dead on
# arrival. Do NOT pin transformers in the base env to get past it:
# vLLM needs its own.
RUN python3 - <<PY
import sys
from transformers import AutoConfig, AutoProcessor
model = "${SURYA_MODEL}"
try:
    cfg = AutoConfig.from_pretrained(model)
    proc = AutoProcessor.from_pretrained(model)
except Exception as exc:
    import transformers
    print(
        f"!! transformers {transformers.__version__} cannot load {model} "
        f"offline from the baked cache: {exc!r}\n"
        f"!! This image would fail at model load. Rebuild on a newer "
        f"base (--build-arg VLLM_TAG=<newer>); do NOT pin transformers.",
        file=sys.stderr,
    )
    sys.exit(1)
import transformers
print(
    f"offline load gate OK: transformers {transformers.__version__}, "
    f"{cfg.architectures} ({cfg.model_type}), {type(proc).__name__}"
)
PY

# ── Handler venv ─────────────────────────────────────────────────────
# pyproject.toml declares the handler deps; uv.lock pins them. ``uv
# sync --frozen`` installs exactly the locked versions into /opt/venv
# and errors out if the lock is stale vs. pyproject.toml. torch comes
# from the CPU index (see pyproject): surya imports it, nothing here
# runs on it. --mount=type=cache keeps uv's wheel cache outside the
# image layer and reuses it across builds. /bin/uv by absolute path:
# see the pin comment above.
COPY runpod-surya/pyproject.toml runpod-surya/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    /bin/uv sync --frozen --python python3

# The client must import, and it must import a CPU torch: a CUDA wheel
# here would be a second GPU stack next to vLLM's, a gigabyte of it.
RUN /opt/venv/bin/python - <<'PY'
import torch, surya, surya.recognition
assert torch.version.cuda is None, f"the handler venv carries a CUDA torch: {torch.__version__}"
print(f"handler venv OK: torch {torch.__version__} (cpu), surya-ocr importable")
PY

COPY runpod_common.py runpod-surya/handler.py ./

# The base image's entrypoint launches the vLLM API server directly;
# this worker instead boots the RunPod handler, which spawns and
# supervises ``vllm serve`` itself (see handler._preload).
ENTRYPOINT []
CMD ["/opt/venv/bin/python", "-u", "handler.py"]
