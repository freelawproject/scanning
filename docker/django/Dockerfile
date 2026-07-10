# syntax=docker/dockerfile:1

##############################################################################
# build-base: full image with compilers and build tooling.                   #
# Shared parent for the dependency- and static-build stages. Nothing from     #
# here ships to prod unless it is explicitly COPYed into the runtime stage.   #
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
# deps-prod / deps-dev: build the virtualenv at /opt/venv.                    #
# Split so each build only resolves the deps it needs and layer caching keys  #
# on pyproject.toml + uv.lock alone (source changes don't bust the venv).     #
##############################################################################
FROM build-base AS deps-prod

COPY pyproject.toml uv.lock ./
# No --all-extras: the only extra is `dev` (mypy, pytest, debug-toolbar, ...),
# which must not ship to prod. Extras are opt-in, so plain sync excludes it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync

FROM build-base AS deps-dev

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras

##############################################################################
# static-builder: install npm packages and compile Tailwind CSS.             #
# node/npm/node_modules live here only and never reach the runtime image.    #
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
# for the entrypoint's editable blackletter install and runserver.            #
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
# prod: slim runtime image (default build target).                           #
# Carries only the prebuilt venv, the app source, and the compiled CSS.       #
# Build-only tooling (nodejs, npm, node_modules, git, curl, uv, compilers)    #
# is left behind. The daemon runs the full ML stack from this same image, so  #
# the runtime apt libs below must cover torch/paddle/opencv/tesseract.        #
##############################################################################
FROM python:3.12-slim AS prod

# Runtime-only apt libs (no -dev packages, no build tooling):
#   libpq5           - PostgreSQL client library
#   tesseract-ocr*   - OCR engine + English data (pytesseract / ocrmypdf)
#   libgl1, libglib2 - OpenCV / PaddleOCR shared libs
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
# gunicorn/daemon run as www-data, whose home (/var/www) isn't writable. Point
# HOME at /tmp so ML libs that cache under ~ (paddle's ~/.cache/paddle, which
# has no env override; torch hub; etc.) can write instead of crashing on
# import/first use.
ENV HOME=/tmp
ENV PATH="/opt/venv/bin:$PATH"

ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

WORKDIR /opt/scanning

# Prebuilt virtualenv (CPU-only torch/paddle per pyproject [tool.uv.sources]).
COPY --from=deps-prod /opt/venv /opt/venv

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
