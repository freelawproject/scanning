# syntax=docker/dockerfile:1

##############################################################################
# build-base: full image with compilers and build tooling.                   #
# Shared parent for the dependency- and static-build stages. Nothing from     #
# here ships to prod unless it is explicitly COPYed into a runtime stage.      #
##############################################################################
FROM python:3.12 AS build-base

# Install apt dependencies
# caching: https://docs.docker.com/build/cache/optimize/#use-cache-mounts
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update --quiet=2 && \
    apt-get install \
    --no-install-recommends \
    --assume-yes \
    --quiet=2 \
    libpq-dev postgresql-client \
    nodejs npm \
    curl git \
    tesseract-ocr tesseract-ocr-eng \
    libgl1 libglib2.0-0

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics
# paddlex reads PADDLE_PDX_CACHE_HOME (not PADDLEX_HOME); point it at /tmp.
ENV PADDLE_PDX_CACHE_HOME=/tmp/paddlex

ENV \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /opt/scanning

##############################################################################
# deps-web / deps-ml / deps-dev: build the virtualenv at /opt/venv.           #
# Split so each image resolves only the deps it needs and layer caching keys  #
# on pyproject.toml + uv.lock alone (source changes don't bust the venv).     #
#   deps-web  lean base only         -> web image (detection offloaded)        #
#   deps-ml   base + local-ml extra  -> daemon image + offline/local detection #
#   deps-dev  everything incl. dev   -> local dev (docker-compose)             #
##############################################################################
FROM build-base AS deps-web

COPY pyproject.toml uv.lock ./
# Base deps only. The only opt-in extras are `dev` and `local-ml`; plain sync
# excludes both, so no torch/paddle/ultralytics lands here. Detection is
# offloaded to RunPod, so the web tier never runs a model in-process.
# blackletter >=0.1.1 ships no YOLO weights in the wheel, so nothing to
# strip here; the daemon image (deps-ml) bakes the weights it needs.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync

FROM build-base AS deps-ml

COPY pyproject.toml uv.lock ./
# local-ml pulls the CPU torch/paddle/ultralytics stack for in-process
# detection (offline daemon, or RUNPOD_ENABLED=False). No dev tooling.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra local-ml

# Bake the small/medium YOLO weights into the venv. blackletter >=0.1.1
# ships no weights in the wheel; ensure_weights() downloads missing ones
# from Hugging Face into site-packages/blackletter/weights/. That must
# happen here at build time: prod-ml runs as www-data with a root-owned
# /opt/venv (a first-use download would raise PermissionError), and the
# offline/no-network daemon needs the weights on disk anyway. Weights
# already on disk resolve without touching the network, so this makes
# ensure_weights a noop at runtime. Retry so a flaky HF doesn't fail the
# build; then assert both files landed and aren't truncated.
RUN set -e; \
    for i in 1 2 3; do \
      python -c "from blackletter.api import ensure_weights; ensure_weights(['small', 'medium'])" \
        && break; \
      echo "ensure_weights attempt $i failed; sleeping 10s"; \
      sleep 10; \
    done \
 && python -c "\
import blackletter, pathlib; \
wd = pathlib.Path(blackletter.__file__).resolve().parent / 'weights'; \
sizes = {'small.pt': 15_000_000, 'medium.pt': 40_000_000}; \
missing = [n for n, s in sizes.items() if not (wd / n).is_file() or (wd / n).stat().st_size < s]; \
assert not missing, f'weights missing or truncated: {missing}'; \
print('baked weights OK:', sorted(p.name for p in wd.glob('*.pt')))"

FROM build-base AS deps-dev

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras

##############################################################################
# static-builder: install npm packages and compile Tailwind CSS.             #
# node/npm/node_modules live here only and never reach a runtime image.       #
##############################################################################
FROM build-base AS static-builder

COPY package.json ./
COPY package-lock.jso[n] ./
RUN npm install

COPY . .
RUN npm run build

##############################################################################
# dev: full image used for local development (docker-compose `target: dev`).  #
# Source is bind-mounted over /opt/scanning at run time; keeps uv + dev deps  #
# and the full ML stack so RUNPOD_ENABLED can be toggled off and everything   #
# runs locally.                                                                #
##############################################################################
FROM build-base AS dev

ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

COPY --from=deps-dev /opt/venv /opt/venv
COPY . .

RUN mkdir -p /opt/scanning/scanning/assets/static/ \
             /opt/scanning/scanning/assets/media/
RUN chmod +x /opt/scanning/docker/django/docker-entrypoint.sh
RUN chown www-data:www-data /opt/scanning/docker/django/docker-entrypoint.sh \
                            /opt/scanning/scanning/assets/media/

ENTRYPOINT ["/bin/sh", "/opt/scanning/docker/django/docker-entrypoint.sh"]

##############################################################################
# prod-web: slim runtime for the web tier (the deployment that scales and     #
# churns). Carries only the LEAN venv, app source, and compiled CSS. No       #
# torch/paddle/ultralytics. Base blackletter uses non-headless opencv-python   #
# (>=0.1.0), so libgl1 is required here. Requires detection offloaded          #
# (RUNPOD_ENABLED=True).                                                       #
##############################################################################
FROM python:3.12-slim AS prod-web

# Runtime-only apt libs for the lean tier:
#   libpq5              - PostgreSQL client library
#   tesseract-ocr*      - OCR engine + English data (pytesseract / ocrmypdf)
#   libgl1, libglib2.0-0 - required by opencv-python (cv2) in base blackletter
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update --quiet=2 && \
    apt-get install \
    --no-install-recommends \
    --assume-yes \
    --quiet=2 \
    libpq5 \
    tesseract-ocr tesseract-ocr-eng \
    libgl1 libglib2.0-0

ENV PYTHONUNBUFFERED=1
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics
ENV PADDLE_PDX_CACHE_HOME=/tmp/paddlex
# gunicorn/daemon run as www-data, whose home (/var/www) isn't writable. Point
# HOME at /tmp so any lib that caches under ~ can write instead of crashing.
ENV HOME=/tmp
ENV PATH="/opt/venv/bin:$PATH"

ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

WORKDIR /opt/scanning

# Prebuilt lean virtualenv.
COPY --from=deps-web /opt/venv /opt/venv

# App source (build context excludes node_modules and built static via
# .dockerignore) plus the full compiled static-global tree from the static
# stage. That tree (Tailwind CSS + FontAwesome CSS + webfonts) is gitignored,
# so it is absent from a fresh checkout's build context and must come from the
# builder, or collectstatic silently ships an image missing those assets.
COPY . .
COPY --from=static-builder \
     /opt/scanning/scanning/assets/static-global/ \
     /opt/scanning/scanning/assets/static-global/

# Create directories for collectstatic output and uploaded/processed files
RUN mkdir -p /opt/scanning/scanning/assets/static/ \
             /opt/scanning/scanning/assets/media/

RUN chmod +x /opt/scanning/docker/django/docker-entrypoint.sh
# media/ must be writable by www-data (gunicorn runs as www-data in prod)
RUN chown www-data:www-data /opt/scanning/docker/django/docker-entrypoint.sh \
                            /opt/scanning/scanning/assets/media/

USER www-data
ENTRYPOINT ["/bin/sh", "/opt/scanning/docker/django/docker-entrypoint.sh"]

##############################################################################
# prod-ml: slim runtime for the daemon (also the offline/local-detection      #
# image). Same layout as prod-web but carries the heavy CPU ML stack so        #
# detection/OCR can run in-process, plus the full OpenCV/torch/paddle libs.    #
##############################################################################
FROM python:3.12-slim AS prod-ml

# Runtime-only apt libs for the heavy tier:
#   libpq5           - PostgreSQL client library
#   tesseract-ocr*   - OCR engine + English data (pytesseract / ocrmypdf)
#   libgl1, libglib2 - OpenCV (full) / PaddleOCR shared libs
#   libgomp1         - OpenMP runtime for torch / paddle CPU kernels
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update --quiet=2 && \
    apt-get install \
    --no-install-recommends \
    --assume-yes \
    --quiet=2 \
    libpq5 \
    tesseract-ocr tesseract-ocr-eng \
    libgl1 libglib2.0-0 \
    libgomp1

ENV PYTHONUNBUFFERED=1
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics
ENV PADDLE_PDX_CACHE_HOME=/tmp/paddlex
# See prod-web: point HOME at /tmp so ML libs that cache under ~ (paddle's
# ~/.cache/paddle, torch hub, etc.) can write instead of crashing on import.
ENV HOME=/tmp
ENV PATH="/opt/venv/bin:$PATH"

ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

WORKDIR /opt/scanning

# Prebuilt heavy virtualenv (CPU-only torch/paddle per pyproject [tool.uv.sources]).
COPY --from=deps-ml /opt/venv /opt/venv

COPY . .
COPY --from=static-builder \
     /opt/scanning/scanning/assets/static-global/ \
     /opt/scanning/scanning/assets/static-global/

RUN mkdir -p /opt/scanning/scanning/assets/static/ \
             /opt/scanning/scanning/assets/media/

RUN chmod +x /opt/scanning/docker/django/docker-entrypoint.sh
RUN chown www-data:www-data /opt/scanning/docker/django/docker-entrypoint.sh \
                            /opt/scanning/scanning/assets/media/

USER www-data
ENTRYPOINT ["/bin/sh", "/opt/scanning/docker/django/docker-entrypoint.sh"]
