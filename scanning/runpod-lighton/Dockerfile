# syntax=docker/dockerfile:1.6
#
# GPU worker image for RunPod Serverless: LightOnOCR tiebreak reads.
#
# Runs lightonai/LightOnOCR-2-1B (1B VLM) behind a local vLLM server,
# driven by handler.py. No Django, no AWS credentials, no DB client.
# Model weights are baked in so cold start skips the network.
#
# Build from the scanning repo root so the context is only this dir:
#     docker build -t lighton-gpu-worker:local scanning/runpod-lighton/
#
# Run locally (requires --gpus all + nvidia-container-toolkit):
#     docker run --rm --gpus all lighton-gpu-worker:local
#
# ── vLLM VERSION MATTERS ─────────────────────────────────────────────
# LightOnOCR-2's architecture is `LightOnOcrForConditionalGeneration`,
# which has to be registered in the running vLLM -- the model ships no
# vLLM plugin. If the base tag below predates that support the server
# refuses the model at load with "Model architectures [...] are not
# supported".
#
# The build gates on this explicitly (see "arch gate" below) so an
# unsupported base fails here, on a cheap build host, instead of on a
# billed GPU worker several minutes into cold start. Bump VLLM_TAG if
# the gate fails.
#
# Do NOT pin transformers in this image. Recent vLLM needs transformers
# v5, and pinning it is the documented way to break this model.
ARG VLLM_TAG=v0.20.1
FROM vllm/vllm-openai:${VLLM_TAG}

# Two Python environments live in this image, on purpose:
#  - the base image's system env, which owns vllm/torch and serves the
#    model (started as a subprocess by handler.py);
#  - /opt/venv, a uv-managed env that owns the handler and its client
#    deps. Keeping them apart means a handler dep can never disturb
#    vLLM's carefully-pinned stack.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install uv (same tag as the other worker images for parity).
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
# NB: the registry key is the `architectures` entry in the model's
# config.json -- LightOn*OCR*, all caps. The model card's Transformers
# example names a `LightOnOcrProcessor`/`LightOnOcrForConditionalGeneration`
# Python class instead; gating on that spelling reports a false miss on a
# base image that supports the model perfectly well.
ARG LIGHTON_MODEL=lightonai/LightOnOCR-2-1B
ARG LIGHTON_ARCH=LightOnOCRForConditionalGeneration
RUN python3 - <<PY
import sys
arch = "${LIGHTON_ARCH}"
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
    f"!!   docker build --build-arg VLLM_TAG=<newer> scanning/runpod-lighton/",
    file=sys.stderr,
)
sys.exit(1)
PY

# ── Handler venv ─────────────────────────────────────────────────────
# pyproject.toml declares the handler deps; uv.lock pins them.
# ``uv sync --frozen`` installs exactly the locked versions into
# /opt/venv and errors out if the lock is stale vs. pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-cache --python python3

# ── Bake model weights ───────────────────────────────────────────────
# Downloads the full HF snapshot (~2 GB safetensors + processor code)
# into /opt/hf using the base image's huggingface_hub. Retries 3 times
# so a flaky HF doesn't fail the build, then verifies the snapshot
# actually holds the weights -- a truncated download would otherwise
# surface as a cryptic vLLM load error at runtime.
ENV LIGHTON_MODEL=${LIGHTON_MODEL}
RUN set -e; \
    for i in 1 2 3; do \
      python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${LIGHTON_MODEL}')" \
        && break; \
      echo "snapshot_download attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python3 -c "\
import pathlib; \
files = list(pathlib.Path('/opt/hf').rglob('*.safetensors')); \
total = sum(f.stat().st_size for f in files); \
assert total > 1_500_000_000, f'weights missing or truncated: {total} bytes in {len(files)} files'; \
print(f'weights OK: {total // 1024 // 1024} MB in {len(files)} files')"

# Everything the worker needs is now on disk; never touch the network
# for model files at runtime. (Overriding LIGHTON_MODEL at runtime
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
# arrival.
RUN python3 - <<PY
import sys
from transformers import AutoConfig, AutoProcessor
model = "${LIGHTON_MODEL}"
try:
    cfg = AutoConfig.from_pretrained(model, trust_remote_code=True)
    proc = AutoProcessor.from_pretrained(model, trust_remote_code=True)
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

COPY handler.py .

# The base image's entrypoint launches the vLLM API server directly;
# this worker instead boots the RunPod handler, which spawns and
# supervises ``vllm serve`` itself (see handler._preload).
ENTRYPOINT []
CMD ["/opt/venv/bin/python", "-u", "handler.py"]
