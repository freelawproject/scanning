# Scanning Portal

Upload portal for FLP volunteer scanners. Single-app Django project where `scanning/` is both the project package and the only app.

## Quick Reference

```bash
# Run tests
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py test scanning.tests -v 2

# Run a single test class
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py test scanning.tests.TestScanUpload -v 2

# Generate migrations
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py makemigrations scanning

# Start dev environment
docker compose -f docker/scanning/docker-compose.yml up --build

# Install dependencies
uv sync --all-extras
```

## Project Structure

- `scanning/` is both the Django project (settings, urls, asgi, wsgi) and the single app (models, views, forms, admin)
- Settings are split into modules: `settings/django.py`, `settings/project/`, `settings/third_party/`
- Templates live in two places:
  - `scanning/assets/templates/` for base layout and cotton components
  - `scanning/templates/scanning/` for app-specific templates (login, upload, list, detail, etc.)
- Error pages (404.html, etc.) go in `scanning/assets/templates/` (the template root)

## Testing

- Use `django.test.TestCase`, NOT pytest style classes
- Tests live in the `scanning/tests/` package, one module per area (`test_services.py`, `test_views.py`, ...). Shared synthetic-PDF builders are in `scanning/tests/pdf_fixtures.py`
- Test classes inherit from `ScanningTestCase` which provides `make_user()`, `make_staff_user()`, `make_pdf()`, `make_image()` helpers
- Use `ScanFactory` and `UserFactory` from `scanning/factories.py` for test data
- Factory docstrings describe default declarations as a prose list, not `:param:` entries (they are class attributes, not `__init__` parameters)
- Use `skip_postgeneration_save = True` on factories and explicitly save changed fields in `@factory.post_generation` hooks

## Views

- All views are function-based with `@login_required`
- Auth views wrap Django's built-in `LoginView`/`LogoutView`
- Never pass `next` query strings directly into templates (open redirect risk). Let Django's auth views handle redirect validation internally.
- All authenticated users see all scans (no per-user filtering). Staff-only distinction is the review form on the detail page.

## Legacy Pipeline Disconnect (issue #173, interim state)

The legacy processing stages — in-process bitonal conversion, the YOLO
`detect` and PaddleOCR `analyze` RunPod actions (and the whole
blackletter-gpu-worker under `scanning/runpod/`), and their
`RUNPOD_ENABLED=False` in-process fallbacks — are deleted. Bitonal came
back as an external job (see below); still missing are #147 (dots.mocr
dispatch) and #149 (page numbers), so:

- `run_full_pipeline` shards the original (#164), sets `page_count`,
  then either starts the bitonal conversion (`Status.AWAITING`) or
  parks the scan in `Status.AWAITING_VALIDATION`.
- Every user-facing action that would re-trigger a legacy stage
  (`start_validate`, `start_detect` without existing detections,
  `reprocess`, `generate_files`) refuses with
  `utils.PIPELINE_PAUSED_MESSAGE` — one constant, flashed as a warning
  banner in HTML views. The daemon parks pre-cutover queued rows
  carrying a legacy `queued_action` back to PENDING_REVIEW with the
  same message; admin re-queue resets `queued_action` to
  FULL_PIPELINE.
- The post-review-1 machinery (`run_generate_files`, pairing, redaction
  geometry, `upload_approved_files`) is kept but nothing queues it.
- `serve_scan_pdf` falls back to streaming the original when
  `bitonal.pdf` is absent — now only for volumes whose conversion was
  skipped or failed, plus every pre-#176 post-cutover upload.
- `RUNPOD_ENABLED` now only gates whether GPU jobs dispatch at all;
  without it an environment uploads and browses but runs no GPU stage.
  Upload paths must always keep working.

## Bitonal via doctor (issue #176)

Conversion runs on doctor, one request per shard, tracked on
`ExternalJob` rows. The pieces: `doctor_client.py` (HTTP), `jobs.py`
(row lifecycle), `bitonal.py` (skip check and merge), and the
`submit_external_jobs` / `collect_external_jobs` daemon commands. What
must not be broken:

- **Doctor answers with the result, not a job id.** `POST
  /convert/pdf/bitonal/` takes form fields (`input_url`, `output_url`,
  `dpi`, `threshold`), and the presigned PUT must be signed
  `ContentType="application/pdf"` or S3 403s and doctor reports
  `RESULT_URL_EXPIRED`. Nothing to poll, nothing to cancel.
- **A lost response is not lost work.** Doctor's view is sync, so a read
  timeout or a killed daemon loses the answer while the conversion
  finishes and the PUT lands. Hence: mark the row SUBMITTED *before* the
  request, and recover with an S3 HEAD on `result_key`
  (`jobs.sweep_jobs`) instead of resubmitting. Only
  `doctor_client.UNANSWERED_ERROR_CODE` takes that path — a failure
  doctor answered means it is done, and retries at once.
- **`result_key` is scoped to run, shard and attempt**
  (`s3_sync.s3_job_attempt_key`), so an abandoned attempt's late upload
  is never harvested as the current attempt's output.
- **`DOCTOR_MAX_CONCURRENCY` counts what doctor is doing, not what we
  claimed**: `submit_pending` subtracts the in-flight rows, because an
  unanswered request is still a running conversion.
- **Run reuse compares the stored page ranges, not the shard keys.**
  Shards are named by position (`0001.pdf`), so a re-cut volume with the
  same shard count gives identical keys over different pages. A run
  holding a dead row (failed, cancelled, expired) is replaced — that is
  what a cancel or admin re-queue leaves behind. A fully CONSUMED run
  means "already converted" and is never merged again, since the merge
  deletes the results it consumed.
- Every row write is a compare-and-swap on the row's current status
  (`jobs._write`), so no lock is held across an HTTP call.
- Three INFO lines carry the timings the stage is judged on, so
  benchmarking it needs no SQL: per shard (ours end to end, plus
  doctor's own `duration_ms`, whose gap is queue and transport), per
  merge, and per scan (row creation to leaving AWAITING, with
  pages/second).
- `dpi=200` / `threshold=160` are the legacy blackletter values, not
  doctor's own, so a converted volume matches every `bitonal.pdf`
  already in the corpus.
- An all-1-bpc volume skips the stage entirely
  (`bitonal.source_is_bitonal`) and gets no job rows.
- The merge (`bitonal.merge_convert_results`) needs no access to the
  original: sharding verified each shard against it byte-for-byte and
  doctor verified each result against its shard, so only the assembly —
  page counts in shard order — is left. It does not write `page_count`;
  `run_full_pipeline` owns that.
- `AWAITING` is not `PROCESSING`: only PROCESSING is swept as stale, and
  sweeping a scan that is merely waiting would charge it an interruption
  and redo work already paid for.
- Consumed shard results are deleted after the merge — they duplicate
  every byte of the `bitonal.pdf` they became. Admin scan deletion
  sweeps the whole `jobs/` prefix alongside `shards/`, since abandoned
  attempts leave copies too and nothing else removes them.
- Every status write at the end of `run_full_pipeline` is guarded on
  `status=PROCESSING`, and on losing the guard the rows it created are
  abandoned: writing AWAITING anyway would spend real capacity on a
  volume somebody cancelled.
- `cancel_processing` covers AWAITING as well as PROCESSING, and cancels
  the scan's job rows with it.
- `DOCTOR_ENABLED` and `DOCTOR_HOST` both default to working values, so
  a deploy converts with no env or secret-store change. The host is
  fully qualified because an unqualified `cl-doctor` does not resolve
  from the `scanning` namespace.
- What keeps that safe is `services._can_convert`: no job is created
  without a committed shard set, doctor configured, **and S3 active**.
  Doctor reads its input through a presigned GET, so under TESTING or in
  dev without credentials the shards never left local disk and a job
  could not be submitted — those environments park unconverted, as every
  post-#173 upload already did. Dev *with* credentials dispatches for
  real.

## Detection Workflow

YOLO models detect elements on each page (captions, key icons, headnotes, etc.) and store them as `Detection` records with a confidence score. Users review detections in the process viewer (step 2) and can:

- **Approve (boost):** Set an existing detection's confidence to 1.0, confirming the model was correct. This is called "boosting" because it raises a low-confidence detection to full confidence without changing which model found it.
- **Add:** Create a new detection manually (confidence 1.0, model_name "manual") when the model missed something. If a detection with the same label and approximate position already exists, it gets boosted instead of duplicated.
- **Delete:** Deactivate a detection (sets `active=False`). The record stays in the DB but is excluded from pairing and redaction.
- **Suppress:** Flag a detection via an Issue record so it's excluded from pairing warnings without deleting it.

Detections are stored both in the DB (`Detection` model) and on disk (`detections.json`). Both are kept in sync by `_sync_detections_to_disk()`. The JSON file is used by blackletter for opinion pairing and file generation.

## PDF Sharding

The full pipeline cuts the original PDF into shards (`scanning/sharding.py`, issue #164) so external workers (bitonal on doctor, dots.mocr on RunPod) can process page ranges in parallel. The shard count is the larger of two demands — `ceil(size / SHARD_TARGET_BYTES)` and `ceil(pages / SHARD_MAX_PAGES)` — because the byte target guards a reader's download and disk while the page cap guards conversion time, which is per page whatever the bytes. Key invariants:

- Shards live under `shards/` in the scan's S3 processing prefix, next to a `manifest.json` that maps each shard to its page range. The manifest is uploaded **last**: its presence guarantees every shard it lists is in the bucket.
- `sharding.ensure_shards(scan)` is idempotent — a no-op while the manifest's source fingerprint (size + page count) matches the original. On a mismatch the whole set is re-cut (~3s split). Retuning `SHARD_TARGET_BYTES` or `SHARD_MAX_PAGES` does NOT re-shard existing volumes — neither is part of the fingerprint, so a change reaches only volumes sharded after it (re-cutting the corpus needs a `MANIFEST_VERSION` bump).
- Shards are a cache for **full-volume jobs only**. Smart page inserts/deletes do NOT refresh them: all page processing is page-level, so an edited page is processed individually and merged into the volume-level artifacts (bitonal.pdf, detections, ocr_results), as the smart-edit paths already do. The stored shard set goes stale against the edited original and just sits in S3 until the next full-volume job calls `ensure_shards`, which detects the stale fingerprint and replaces the set. Any future consumer must go through `ensure_shards` first — never read `shards/` directly.
- `shards/` is excluded from the generic S3 processing sync in both directions (like `jobs/`) — the shards duplicate the original's bytes.
- Verification compares per-page geometry and digests of the raw undecoded image streams (soft-mask streams included), so any tool/version that silently recompresses scan data fails the run.
- Sharding is a regular pipeline stage: failures propagate to `_handle_pipeline_exception` and mark the scan ERROR — except S3/transport errors, which `services._ensure_shards` re-raises as `RunpodTransientError` so the scan re-queues and retries instead. `ensure_shards` writes no manifest on failure, so a re-queued scan retries the sharding.
- Only a *missing* manifest object may read as "no committed shard set": `fetch_shard_manifest` re-raises any other S3 error (a throttle or IAM error must not trigger a destructive delete-and-re-cut). A corrupt or wrong-`MANIFEST_VERSION` manifest never matches and flows into the re-shard path, which replaces it.
- Admin scan deletion sweeps the scan's `shards/` prefix from S3 (best effort) — the shard set duplicates the original's bytes and nothing else removes it once the row is gone.

## Tailwind CSS

- Config: `scanning/assets/tailwind/tailwind.config.js`
- Input: `scanning/assets/tailwind/input.css`
- Output: `scanning/assets/static-global/css/tailwind_styles.css` (gitignored, built by npm)
- Component classes defined in `input.css`: `.btn-primary`, `.btn-outline`, `.btn-danger`, `.btn-ghost`, `.card`, `.input-text`, `.alert-*`, `.badge-*`
- Templates use cotton components: `<c-header />`, `<c-footer />`

## Environment

- `DEVELOPMENT=True` enables debug toolbar, local filesystem storage, dev S3 buckets
- `TESTING=True` is auto-detected from `sys.argv`, switches to LocMemCache, MD5 password hasher, disables debug toolbar URLs
- The `DB_SSL_MODE=prefer` env var is needed when running locally outside Docker (avoids SSL connection errors)
