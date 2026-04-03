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
ENV PADDLEX_HOME=/tmp/paddlex

ARG BUILD_ENV=prod
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

FROM build-base AS python-base

WORKDIR /opt/scanning

# uv install dependencies
COPY pyproject.toml .
COPY uv.lock .
ENV \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras

# npm install dependencies
COPY package.json ./
COPY package-lock.jso[n] ./
RUN npm install

COPY . .

# Build Tailwind CSS
RUN npm run build

# Create directories for collectstatic output and uploaded/processed files
RUN mkdir -p /opt/scanning/scanning/assets/static/ \
             /opt/scanning/scanning/assets/media/

USER root
RUN chmod +x /opt/scanning/docker/django/docker-entrypoint.sh
# media/ must be writable by www-data (gunicorn runs as www-data in prod)
RUN chown www-data:www-data /opt/scanning/docker/django/docker-entrypoint.sh \
                            /opt/scanning/scanning/assets/media/

USER www-data
ENTRYPOINT ["/bin/sh", "/opt/scanning/docker/django/docker-entrypoint.sh"]
