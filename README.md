# FLP Scanning Portal

Upload portal for [Free Law Project](https://free.law) volunteer scanners to
submit scanned legal documents (PDFs) for processing. A Django application that
supports file uploads, staff review workflows, and S3-backed storage.

This project, including its code, tests, and this README, was vibe coded
with Claude Code. It has not had extensive human review. Please read everything
with skepticism!


## Quick Start (Development)

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd scanning

# 2. Copy the dev environment file
cp .env.example .env.dev

# 3. Start everything
docker compose -f docker/scanning/docker-compose.yml up --build

# 4. Create a superuser
docker compose -f docker/scanning/docker-compose.yml exec scanning-django \
    python manage.py createsuperuser
```

The portal is now running at **http://localhost:8002**. Log in at `/login/`
with the superuser credentials you just created.


## Architecture

### Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13, Django 6.0 |
| Database | PostgreSQL 16 |
| CSS | Tailwind 3.x (built via npm) |
| Templates | Django templates + django-cotton components |
| File storage | Local filesystem (dev), S3 via django-storages (prod) |
| Containers | Docker Compose for development |
| ASGI server | Gunicorn + Uvicorn workers (prod) |

### Project Structure

`scanning/` serves as both the Django project package (settings, asgi, wsgi,
urls) and the single app (models, views, forms). This is the simplest approach
for a single-app project.

```
scanning/
  models.py           Scan model with Reporter/Status enums
  views.py            Upload, list, detail, review (function-based)
  forms.py            ScanUploadForm, ScanReviewForm
  urls.py             Root URL configuration
  admin.py            Scan admin registration
  storage.py          PrivateS3Storage + static storage
  context_processors.py
  workers.py          Custom UvicornWorker
  settings/
    django.py         Core Django settings
    project/
      logging.py, security.py, testing.py
    third_party/
      aws.py, sentry.py
  templates/scanning/ Login, upload, list, detail templates
  assets/
    templates/        base.html, cotton components
    tailwind/         Config + input CSS
    static-global/    Generated CSS output
  runpod/             GPU worker image for RunPod Serverless
```

The GPU-heavy steps of the blackletter pipeline run on a RunPod Serverless
worker built from `scanning/runpod/`. See
[scanning/runpod/README.md](scanning/runpod/README.md) for the worker image,
release workflow, endpoint configuration, and operational notes.

### Settings Pattern

Settings follow the wiki project's split-file pattern.
`scanning/settings/__init__.py` uses wildcard imports to compose the final
config from:

```
settings/
  django.py              Core Django settings
  project/
    logging.py, security.py, testing.py
  third_party/
    aws.py, sentry.py
```

All settings use `environ.FileAwareEnv()` for environment-variable-based
configuration.


## Data Model

### Scan

| Field | Type | Notes |
|---|---|---|
| `reporter` | `CharField` | TextChoices enum (e.g., U.S. Reports, Federal Reporter) |
| `volume` | `PositiveIntegerField` | Volume number |
| `pages` | `PositiveIntegerField` | Number of pages |
| `book_cover` | `ImageField` | Optional cover image, S3-backed |
| `original_pdf` | `FileField` | Required PDF upload, S3-backed |
| `redacted_pdf` | `FileField` | Populated after processing |
| `status` | `CharField` | `uploaded` / `processing` / `pending_review` / `approved` / `extracted` |
| `uploaded_by` | `ForeignKey(User)` | Who uploaded the scan |
| `uploaded_at` | `DateTimeField` | Auto-set on creation |
| `processed_at` | `DateTimeField` | Set when approved |
| `notes` | `TextField` | Optional notes |

### Reporters

- U.S. Reports
- Federal Cases
- Federal Reporter (1st, 2d, 3d)
- Federal Supplement (1st, 2d, 3d)


## Views

| URL | View | Auth | Description |
|---|---|---|---|
| `/login/` | `login_view` | Public | Username/password login |
| `/logout/` | `logout_view` | Any | Logs out, redirects to `/login/` |
| `/` | `scan_list` | Login required | Own scans (regular users) or all scans (staff). Filterable, paginated. |
| `/upload/` | `scan_upload` | Login required | Upload form. Sets `uploaded_by` and `status=uploaded` automatically. |
| `/scans/<int:pk>/` | `scan_detail` | Login required | Detail page with inline PDF viewer. Staff see approve/reject form. |

### Staff Review Workflow

Staff users see a review form on the scan detail page. They can:
- **Approve**: Sets `status=approved` and records `processed_at`
- **Reject**: Resets `status=uploaded` with review notes


## Production Deployment

### Prerequisites

- Docker (or a Python 3.13 environment with PostgreSQL 16)
- An AWS account with S3 configured
- A domain with DNS and HTTPS configured (via a reverse proxy like Nginx or Caddy)


### Step 1: Environment Variables

Create a `.env` file (or set environment variables directly). Every setting
is read via `django-environ`'s `FileAwareEnv`, so you can also use Docker
secrets by pointing to files (e.g., `SECRET_KEY_FILE=/run/secrets/key`).

#### Required variables

| Variable | Description | Example |
|---|---|---|
| `SECRET_KEY` | Django secret key. Generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` | `abc123...` |
| `DEBUG` | Must be `False` in production | `False` |
| `DEVELOPMENT` | Must be `False` in production. Controls S3 storage, debug toolbar, and more | `False` |
| `ALLOWED_HOSTS` | Comma-separated list of domains | `scanning.free.law` |
| `DB_HOST` | PostgreSQL hostname | `db.example.com` |
| `DB_NAME` | PostgreSQL database name | `scanning` |
| `DB_USER` | PostgreSQL user | `scanning_user` |
| `DB_PASSWORD` | PostgreSQL password | `(strong password)` |
| `DB_SSL_MODE` | PostgreSQL SSL mode | `require` |

#### AWS S3 (file storage + static files)

When `DEVELOPMENT=False`, Django uses S3 for both media uploads and static
files. You need **two** S3 buckets:

| Variable | Description | Default |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | IAM credentials for S3 | -- |
| `AWS_SECRET_ACCESS_KEY` | IAM credentials for S3 | -- |
| `AWS_STORAGE_BUCKET_NAME` | Public bucket for static files | `com-freelawproject-scanning-storage` |
| `AWS_PRIVATE_STORAGE_BUCKET_NAME` | Private bucket for uploaded files | `com-freelawproject-scanning-private-storage` |
| `AWS_S3_CUSTOM_DOMAIN` | Custom domain for static file URLs (optional) | `<bucket>.s3.amazonaws.com` |

**Static files bucket** (`AWS_STORAGE_BUCKET_NAME`): Stores collected static
assets (CSS, JS). Files are served from the `static/` prefix within the bucket.

**Private uploads bucket** (`AWS_PRIVATE_STORAGE_BUCKET_NAME`): Stores
uploaded PDFs and cover images. All files are stored with `private` ACL and
served via 5-minute signed URLs.

##### S3 bucket configuration

For the **static files bucket**:
- Enable public access (or serve via CloudFront)
- No special CORS or lifecycle rules needed

For the **private uploads bucket**:
- **Block all public access** (files are served via signed URLs)
- Suggested bucket policy: grant the IAM user `s3:GetObject`, `s3:PutObject`,
  `s3:DeleteObject`, and `s3:ListBucket`
- **A CORS rule is required.** Uploads go straight from the browser to the
  bucket via a presigned POST, which is a cross-origin request. Without CORS
  the object still lands in S3, but the browser can't read the response, so
  the user sees an "Upload to storage failed" error and the scan is only
  linked later by `recover_pending_uploads` / the cleanup sweep. Allow the
  app origin(s) to `POST` (see the CORS example below).

##### CORS configuration (private uploads bucket)

```json
[
  {
    "AllowedMethods": ["POST"],
    "AllowedOrigins": ["https://scanning.courtlistener.com"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

Use `http://localhost:8002` as the origin for local development. List every
origin that serves the app; the value must be the scheme + host only (no
trailing slash or path).

##### IAM policy example

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::com-freelawproject-scanning-storage",
        "arn:aws:s3:::com-freelawproject-scanning-storage/*",
        "arn:aws:s3:::com-freelawproject-scanning-private-storage",
        "arn:aws:s3:::com-freelawproject-scanning-private-storage/*"
      ]
    }
  ]
}
```

#### Sentry (error tracking, optional)

| Variable | Description |
|---|---|
| `SENTRY_DSN` | Sentry DSN for error reporting. Leave empty to disable |

#### Other optional variables

| Variable | Description | Default |
|---|---|---|
| `TIMEZONE` | Server timezone | `America/Los_Angeles` |
| `MEDIA_ROOT` | Local media root (only used when `DEVELOPMENT=True`) | `scanning/assets/media/` |
| `STATIC_URL` | Static file URL prefix | `static/` |
| `NUM_WORKERS` | Gunicorn worker count | `4` |
| `MAX_REQUESTS` | Gunicorn max requests before worker restart | `2500` |


### Step 2: Build the Docker Image

```bash
docker build -t scanning-django -f docker/django/Dockerfile .
```

The Dockerfile:
- Installs Python dependencies via `uv`
- Installs Node dependencies and builds Tailwind CSS
- Copies the application code
- Runs as `www-data` user


### Step 3: Set Up the Database

Provision a PostgreSQL 16 instance (RDS, self-hosted, etc.) and create the
database:

```sql
CREATE DATABASE scanning;
CREATE USER scanning_user WITH PASSWORD 'strong-password-here';
GRANT ALL PRIVILEGES ON DATABASE scanning TO scanning_user;
```

Run migrations:

```bash
docker run --env-file .env scanning-django migrate
```

The entrypoint's fallthrough case passes arguments to `manage.py`, so
`docker run scanning-django migrate` is equivalent to
`python manage.py migrate`.

Create the cache table (used for Django's database-backed cache):

```bash
docker run --env-file .env scanning-django createcachetable
```


### Step 4: Collect Static Files

When `DEVELOPMENT=False`, static files are stored in S3. Run `collectstatic`
to upload them:

```bash
docker run --env-file .env scanning-django collectstatic --noinput
```

This uploads all static files to the `static/` prefix of your
`AWS_STORAGE_BUCKET_NAME` bucket.


### Step 5: Create a Superuser

```bash
docker run -it --env-file .env scanning-django createsuperuser
```


### Step 6: Start the Application

```bash
docker run -d \
    --name scanning-django \
    --env-file .env \
    -p 8000:8000 \
    scanning-django web-prod
```

This starts Gunicorn with Uvicorn workers (ASGI). Configuration:
- **Workers**: `NUM_WORKERS` env var (default: 4)
- **Timeout**: 180 seconds
- **Max requests**: `MAX_REQUESTS` env var (default: 2500, with 100 jitter)
- **Bind**: `0.0.0.0:8000`


### Step 7: Reverse Proxy

The application listens on port 8000. Put it behind a reverse proxy (Nginx,
Caddy, etc.) for HTTPS termination.

Key production security settings are enabled automatically when
`DEVELOPMENT=False`:
- `SESSION_COOKIE_SECURE = True`
- `CSRF_COOKIE_SECURE = True`
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`
- HSTS: 2 years, with subdomains and preload

Nginx example:

```nginx
server {
    listen 443 ssl;
    server_name scanning.free.law;

    ssl_certificate     /etc/letsencrypt/live/scanning.free.law/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/scanning.free.law/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 100M;
    }
}
```


## Complete `.env` Example for Production

```bash
# Django
SECRET_KEY=your-generated-secret-key-here
DEBUG=False
DEVELOPMENT=False
ALLOWED_HOSTS=scanning.free.law

# Database
DB_HOST=your-postgres-host.example.com
DB_NAME=scanning
DB_USER=scanning_user
DB_PASSWORD=your-strong-password
DB_SSL_MODE=require

# S3 (file storage + static files)
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_STORAGE_BUCKET_NAME=your-bucket-name
AWS_PRIVATE_STORAGE_BUCKET_NAME=your-private-bucket-name

# Sentry (optional)
SENTRY_DSN=https://examplePublicKey@o0.ingest.sentry.io/0

# Workers
NUM_WORKERS=4
MAX_REQUESTS=2500
```


## Keyboard Shortcuts (Process Viewer)

These shortcuts work in the scan process page (`/scans/{pk}/process/`) when focus is not on an input field.

| Key | Step | Action |
|-----|------|--------|
| `←` / `→` | 2, 3 | Navigate between opinions (step 2) or opinion cards (step 3) |
| `↑` / `↓` | 2, 3 | Scroll to the previous/next page in the viewer |
| `R` | 2, 3 | Cycle overlay mode: off, opinion bounds, transparent redactions, solid redactions |
| `Escape` | 2, 3 | Clear opinion highlighting and selection |


## Key Design Decisions

### Single-App Architecture

The project uses a single Django app (`scanning/`) that also serves as the
project package (settings, asgi, wsgi). This avoids unnecessary complexity for
a focused, single-purpose application.

### Upload Path Structure

Files are organized by reporter and volume:
`uploads/{reporter}/{volume}/{uuid}.pdf`. UUIDs prevent filename collisions
while the directory structure keeps things browsable in S3.

### Staff Review Workflow

Scans follow a simple status pipeline:
`uploaded` -> `processing` -> `pending_review` -> `approved` -> `extracted`.
Staff can approve (setting `processed_at`) or reject (resetting to `uploaded`)
from the detail page.

### Private File Storage

All uploaded files use `private` ACL in S3 with 5-minute signed URLs. This
ensures scanned documents are only accessible to authenticated users through
the application.

### Dark Mode

Uses `prefers-color-scheme` (Tailwind's `darkMode: 'media'`). No manual
toggle; the portal follows the user's OS/browser setting.

### No External CDNs

All CSS is built locally via Tailwind. No external network requests for assets.


## Running Tests

Tests use Django's `TestCase` and run against a disposable test database:

```bash
# Run the full suite
docker compose -f docker/scanning/docker-compose.yml exec scanning-django \
    python manage.py test scanning.tests -v 2

# Run a specific test class
docker compose -f docker/scanning/docker-compose.yml exec scanning-django \
    python manage.py test scanning.tests.TestScanUpload -v 2
```

Or locally with uv:

```bash
uv run python manage.py test scanning.tests -v 2
```

### Test Coverage

| Test Class | Tests | Covers |
|---|---|---|
| `TestAuthentication` | 5 | Login required redirects, login page, login success, open redirect rejection |
| `TestScanUpload` | 4 | Form rendering, successful upload, validation, auto-set fields |
| `TestScanList` | 4 | All scans visible, filtering by status/reporter, pagination |
| `TestScanDetail` | 4 | Detail rendering, review form visibility, cross-user access, 404 |
| `TestStaffReview` | 3 | Review form, approve sets `processed_at`, reject resets status |
| `TestScanModel` | 1 | Upload path format |
| **Total** | **21** | |


## Development

### Services

`docker compose -f docker/scanning/docker-compose.yml up` starts:

| Service | Purpose | Port |
|---|---|---|
| `scanning-django` | Django dev server with auto-reload | `localhost:8002` |
| `scanning-postgres` | PostgreSQL 16 | `localhost:5434` |
| `scanning-tailwind` | Tailwind CSS watcher (rebuilds on file changes) | -- |

### Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

Runs ruff (lint + format) and standard checks (large files, merge conflicts,
trailing whitespace, etc.) on every commit.

### Tailwind CSS

Styles are in `scanning/assets/tailwind/input.css` using Tailwind's `@layer`
directives. The config is at `scanning/assets/tailwind/tailwind.config.js`.
The `scanning-tailwind` container watches for changes and rebuilds
automatically.

Custom component classes: `.btn-primary`, `.btn-outline`, `.btn-danger`,
`.btn-ghost`, `.card`, `.input-text`, `.alert-*`, `.badge-*` (status badges).

### Testing RunPod Locally

The daemon can offload the two GPU-heavy blackletter steps (`detect` and
`analyze_pdf`) to a [RunPod Serverless](https://docs.runpod.io/serverless/overview)
endpoint. When `RUNPOD_ENABLED=False` (the default) the daemon runs
blackletter in-process, so most local development needs no RunPod
configuration. Flip the flag when you want to exercise the remote path
end-to-end.

#### Required environment variables

RunPod settings go in `.env.dev`:

```bash
RUNPOD_ENABLED=True
RUNPOD_ENDPOINT_ID=<your-endpoint-id>
RUNPOD_API_KEY=<your-runpod-api-key>
```

The daemon also needs AWS credentials so it can upload the local PDF
to the dev private bucket and hand the worker a presigned GET URL.
Two options:

**Option A — paste the AWS console export block into your shell, then
start the containers.** This is the easiest path when you're using
temporary SSO / STS credentials that rotate. The docker-compose file
(`docker/scanning/docker-compose.yml`) forwards `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` from your shell into the
containers and maps them onto `AWS_DEV_*` automatically:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...
docker compose -f docker/scanning/docker-compose.yml up --build
```

**Option B — hard-code dev credentials in `.env.dev`.** Use this for
long-lived IAM user keys:

```bash
AWS_DEV_ACCESS_KEY_ID=<your-aws-key>
AWS_DEV_SECRET_ACCESS_KEY=<your-aws-secret>
```

Either way, the dev S3 buckets (`dev-com-freelawproject-scanning-storage`
and `dev-com-freelawproject-scanning-private-storage`) are selected
automatically when `DEVELOPMENT=True`; you do not need to set
`AWS_PRIVATE_STORAGE_BUCKET_NAME`.

Optional tuning knobs (all have sensible defaults, see
`scanning/settings/project/runpod.py`):

| Variable | Default | Purpose |
|---|---|---|
| `RUNPOD_REQUEST_TIMEOUT` | `1800` | Wall-clock ceiling (s) for submit + poll. |
| `RUNPOD_MAX_RETRIES` | `2` | Transport-error retries on `/run`. |
| `RUNPOD_PRESIGNED_TTL` | `86400` (1 day) | Lifetime (s) of the GET URL handed to the worker. |
| `RUNPOD_MAX_TRANSIENT_RETRIES` | `5` | Re-queues on `NO_GPU` before escalating to `ERROR`. |

#### Why `make_dev_data` is a no-op in this mode

`python manage.py make_dev_data` (run automatically by the `web-dev`
entrypoint) normally seeds two users (`staff` / `scanner`) and a
handful of `Scan` rows with placeholder PDF bytes
(`b"%PDF-1.4 test"`). Those placeholder bytes are not valid PDFs and
will fail the moment YOLO or PaddleOCR tries to read them on the
worker, polluting the DB and burning endpoint quota on guaranteed
errors.

When `RUNPOD_ENABLED=True`, the command short-circuits before doing
anything. Create your own user with `createsuperuser` and upload a
real PDF through the UI to drive the pipeline; the daemon will push
it to the dev private bucket on demand and dispatch the GPU steps to
RunPod.

Setting `RUNPOD_ENABLED=True` also activates the full S3 artifact
sync (`scanning/s3_sync.py`), which is otherwise skipped in
`DEVELOPMENT=True`. Intermediate processing files (`detections.json`,
`redacted/*.pdf`, etc.) get pushed to `dev-com-freelawproject-scanning-private-storage`
and pulled back on reprocess, so a local end-to-end run exercises
the same recovery path as production.

#### Quick recipe

1. Set the RunPod variables in `.env.dev`.
2. Export AWS credentials in your shell (Option A) or add `AWS_DEV_*`
   to `.env.dev` (Option B).
3. `docker compose -f docker/scanning/docker-compose.yml up --build`.
4. Create a user:
   `docker compose -f docker/scanning/docker-compose.yml exec scanning-django python manage.py createsuperuser`.
5. Log in at http://localhost:8002/login/ and upload a real PDF.
6. Watch the daemon container logs. You should see:
   - `uploading <name>.pdf to s3://dev-com-freelawproject-scanning-private-storage/<key> before presign`
     from `runpod_client._ensure_presigned_url`, immediately before
     the GPU step is dispatched.
   - RunPod poll ticks while the worker runs.
   - `Uploaded N processing file(s) for scan <pk> to s3://...`
     from `s3_sync.upload_processing_files` once the pipeline
     completes — the artifact sync that's now active in the dev
     RunPod path.
7. Set `SCANNING_LOG_LEVEL=DEBUG` for per-poll detail.

### Management Commands

```bash
# Run migrations
docker exec scanning-django python manage.py migrate

# Create the cache table (needed once after initial DB setup)
docker exec scanning-django python manage.py createcachetable

# Create a superuser
docker exec -it scanning-django python manage.py createsuperuser

# Collect static files to S3 (production)
docker exec scanning-django python manage.py collectstatic --noinput

# Open a Django shell
docker exec -it scanning-django python manage.py shell
```


## Deployment Checklist

Quick reference for going to production:

- [ ] `SECRET_KEY` set to a strong random value
- [ ] `DEBUG=False` and `DEVELOPMENT=False`
- [ ] `ALLOWED_HOSTS` set to your domain(s)
- [ ] PostgreSQL configured with `DB_SSL_MODE=require`
- [ ] S3 buckets created (public for static, private for uploads)
- [ ] `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` configured
- [ ] `collectstatic` run to upload static files to S3
- [ ] `migrate` and `createcachetable` run against the production database
- [ ] Reverse proxy configured with HTTPS
- [ ] Superuser created
- [ ] Sentry DSN configured (optional)


## License

AGPL-3.0-only
