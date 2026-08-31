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
back as an external job, dots.mocr as a staff-started one, and the page
numbers plus Issues as an apply pass on the collect tick (#149/#204,
all below); still missing are the #151 approve button and the
post-review stages (#195/#196), so:

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
- `serve_scan_pdf` never streams the original (#185): with no
  `bitonal.pdf` it answers 202 (a preview is coming — poll) or 409
  (none will come) with a stage-specific message and
  `original_available`. The viewer offers a "load the original" button
  instead, backed by `scan_original_url`: a presigned S3 GET that
  pdf.js reads with range requests (needs the bucket CORS rule from
  infrastructure #808; until it deploys, the viewer shows an explicit
  failure with a new-tab link), or the `serve_scan_original` local
  stream when S3 is off. Served previews carry `X-Scan-Preview` so the
  viewer can show the lower-quality banner.
- `RUNPOD_ENABLED` now only gates whether GPU jobs dispatch at all;
  without it an environment uploads and browses but runs no GPU stage.
  Upload paths must always keep working.

## Page completeness review states (issue #154)

`READY_FOR_PAGE_COMPLETENESS_REVIEW` and
`PAGE_COMPLETENESS_REVIEW_DONE` give review 1 explicit edges. #154
landed the values and the readers; the apply pass (#149/#204, below)
writes READY — the last prerequisite by construction, and it restores
READY after an admin re-queue parks a ready scan back in
`AWAITING_VALIDATION` (re-queue -> bitonal park -> the next apply
tick). The #151 approve button sets DONE, and #195/#196
trigger off DONE writing **no** scan status, so redaction work never
blocks either review. Both are parked human states outside
`BUSY_STATUSES` (no polling, no sweep); `AWAITING_VALIDATION` now means
"review-1 prerequisites outstanding". `recalculate_issues` preserves
both (`PENDING_REVIEW` remains for legacy rows and step 2 only), and
`serve_scan_pdf` reads a missing preview under either as a failed S3
pull (409 + reload hint) — READY implies the conversion finished or
was skipped because the source is already bitonal (that copy nuance
belongs to the #204 follow-up).

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
  what a cancel or admin re-queue leaves behind. In practice that path
  now sees CANCELLED rows and rows out of attempts, since `retry_dead`
  picks a merely-failed row back up long before anyone re-queues. A
  fully CONSUMED run
  means "already converted" and is never merged again, since the merge
  deletes the results it consumed.
- Every row write is a compare-and-swap on the row's current status
  (`jobs._write`), so no lock is held across an HTTP call. The writer it
  guards against is the **web process**, not a second daemon: one daemon
  runs, and both `views_process.cancel_processing` and the admin
  re-queue call `jobs.abandon_open` from a request. A daemon that wrote
  PENDING over their CANCELLED would convert a shard nobody wants.
- **A slow page is a retry, not a dead volume.** Doctor reports a page
  it could not rasterize in time as `CONVERSION_TIMEOUT` (doctor #245,
  PR #246, in production), which is in `TRANSIENT_ERROR_CODES`, so the
  submit pass retries it up to `DOCTOR_MAX_ATTEMPTS` instead of writing
  the shard off on the first answer. A FAILED row therefore means the
  attempts are spent, and one still sinks the whole volume to ERROR.
  Do **not** add a pass that revives dead rows: everything that stops a
  scan (`cancel_processing`, the admin re-queue) calls `abandon_open`,
  which touches only `OPEN_JOB_STATUSES` and leaves a FAILED row alone,
  and changes `Scan.status` in a *second*, uncommitted-in-between write
  — so a reviver gated on the scan's status races it and converts a
  shard of a volume somebody just cancelled.
- **ERROR stays terminal.** `finish_ready_scans` looks at AWAITING only.
  A pass that re-examined ERROR would re-run the merge — and its
  download of every shard result — on every 15s tick of a failure that
  is not going to change, because a failed merge leaves its rows
  COMPLETED. The way back is an admin re-queue.
- A failure names where it happened (`jobs._failure_location`): the
  shard's volume page range off the row's own `input_manifest`, plus
  doctor's `page_number` and `pixels` when it sends them (doctor #245),
  so triage needs neither S3 nor a parse of doctor's prose. Page numbers
  are logged 1-based; `from_page`/`to_page` are fitz indexes.
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

## dots.mocr via RunPod (issue #190)

OCR runs on RunPod Serverless, one job per **original** shard (not the
bitonal ones — dots wants the greyscale scan its layout model was
trained on), tracked on `ExternalJob` rows at
`ANALYZE`/`DOTS_MOCR`/`RUNPOD`. The pieces: `runpod_client.py`
(transport), `dots_mocr.py` (the stage), `jobs.py` (row lifecycle), the
`submit_external_jobs` / `collect_external_jobs` daemon commands, and
`views_process.start_dots_mocr` (the button). What must not be broken:

- **A person starts it, the daemon runs it.** There is no automatic
  enqueueing: a staff-only button on `/scan/process/` writes the rows and
  returns, and the daemon submits, polls and retries them. That is
  deliberate while the stage is debugged — every press costs GPU money.
  `DOTS_MOCR_ENABLED` (on by default) gates *dispatch*, not enqueueing,
  so it starts no work by itself. What keeps that true is structural, not
  a promise: `ensure_analyze_jobs` is the only thing that creates ANALYZE
  rows and `start_dots_mocr` is its only caller, held by
  `TestNothingAutoEnqueues`. Auto-dispatch is a follow-up that has to
  retire that test on purpose.
  The request makes **no** call to RunPod, and it never cuts shards:
  `sharding.committed_manifest` verifies the stored set with one
  `head_object` on the original (size plus `Scan.page_count` *is* the
  whole fingerprint), so a web pod never pulls a multi-GB PDF and never
  reads `shards/` directly. A stale set is refused, not re-cut.
- **The stage writes no scan status while it reads.** Progress lives
  only on the rows, read through `dots_mocr.run_summary` into the page
  context and `progress_api`. So a volume stays browsable and
  reviewable while it reads, and the bitonal stage keeps sole ownership
  of `AWAITING`. The one status write comes at the very end, from the
  apply pass (#204, below): a single compare-and-swap over the review
  edge, reaching only `AWAITING_VALIDATION` and the legacy
  `PENDING_REVIEW`.
- **Queue time is not run time.** A row keeps
  `DAEMON_JOB_MAX_QUEUE_SECONDS` (6h) until `/status` first reports
  `IN_PROGRESS`; only that *crossing* stamps
  `jobs.runpod_execution_deadline` (`RUNPOD_REQUEST_TIMEOUT` plus
  `DOTS_MOCR_SECONDS_PER_PAGE` × the row's own `page_count`). An
  endpoint with a narrow worker pool queues the excess by design, so a
  run-sized timeout from submission would cancel the tail of every
  fan-out, resubmit it to the back of the same queue, and eventually
  fail a volume for being popular.
- **The queue ceiling is stamped once per attempt**, when the row enters
  the queue: at creation, and again when a retry sends it back. Nothing
  else moves it. A claim does not (`submit_deadline_fields` writes no
  deadline for a polling provider — being accepted is not being
  started), a defer does not (the row never left our queue), and only
  the `IN_PROGRESS` crossing replaces it. Any of those re-stamping would
  forgive the row's wait on every tick, so a paused or saturated
  endpoint would hold a scan forever instead of failing it — which is
  the one outcome the ceiling exists to produce.
- **A job id with nowhere to live is still cancelled.** If a cancel
  takes the row while `POST /run` is in flight, `abandon_open` sees a
  blank `external_id` and its own cancel is a no-op, and the later write
  of the id loses the compare-and-swap. `_apply_runpod_outcome` cancels
  from `_cancel_job_id` there, or the job runs and bills with nothing
  left anywhere to find it.
- **A job nothing will read must be cancelled.** `_cancel_provider_job`
  runs from `_fail`, `_retry_or_fail` and `abandon_open`. Doctor's
  branch is a no-op; a RunPod job left running bills for nothing, and a
  deadline write-off is exactly when the old attempt is still alive.
- **`abandon_open` is scoped by stage**, and every caller passes
  `JobStage.CONVERT`. `COMPLETED` is in `OPEN_JOB_STATUSES` on purpose,
  so an unscoped re-queue would cancel a finished dots.mocr run and make
  the next press pay RunPod for output already in S3. Stage is the right
  grain: a re-queue re-runs `run_full_pipeline`, which owns `CONVERT`
  whichever engine serves it and owns no part of `ANALYZE`.
- **The result goes to S3, not inline.** The worker PUTs an envelope
  (`schema_version`, `action`, `scan_pk`, `result_key`, `payload`) to a
  presigned PUT signed `application/json`, and answers with a summary
  only. RunPod caps a response at ~20 MB and discards it ~30 min after
  the job finishes, so inline delivery would lose a paid parse to a
  daemon that was down for an hour. Dropping `pages` from the summary is
  load-bearing — echoing it back reintroduces the cap. Without
  `result_url` the worker still answers inline (dev, CI, an older
  image), so a rollback needs no daemon change.
- **A 404 from `/status` asks S3 before writing the job off.** The key
  is attempt-scoped, so presence needs no freshness window. Absent, the
  row is `EXPIRED` *and* retriable: the inputs are still there and only
  the job record is gone. That recovery carries
  `PollOutcome.confirmed_by="s3_head"` rather than letting `_complete`
  infer it, since the branch synthesises a truthy `output` and would
  otherwise be audited as a normal provider answer.
- **The endpoint declining work costs no attempt.** HTTP 409
  (`ENDPOINT_PAUSED`) and 429 (rate limited) raise
  `RunpodEndpointBusy`, and `_defer` returns the row to PENDING with its
  attempt and its deadline intact. Every other 4xx stays terminal.
- **A corrupt input is a transfer fault.** `validate_pdf` raises
  `CorruptDownloadError` (not `ValueError`), which the worker reports as
  the retriable `INPUT_DOWNLOAD_CORRUPT`. Sharding cut and verified that
  shard against the original, so a copy that will not open describes the
  download; `BAD_INPUT` there would write a volume off for a dropped
  connection.
- **Deleting a scan cancels its jobs first.** `ExternalJob.scan` is
  CASCADE, so the delete takes `external_id` with it and `sweep_jobs`
  only walks rows that exist. `admin._release_scan_external_work`
  therefore cancels before it sweeps S3 — cancel first so a still-running
  worker cannot PUT after the sweep and re-orphan an object.
- **`poll_once` never raises and never sleeps.** `status=None` means "we
  learned nothing" — a 5xx or a blip leaves the row untouched, which is
  not the same as learning it failed. An unrecognised provider status
  reads as "still at work", so RunPod adding a state cannot fail jobs.
  Pacing is the daemon's tick: the scheduler is serial (#156), so a
  client that slept would stall every other task.
- **A shard that lost some pages still completes.** `failed_pages` is
  logged as a WARNING naming *volume* pages (the worker counts from zero
  inside its shard) and kept in `provider_meta`. The apply reads a
  missing page as `detected=None` and interpolates, so re-running 99
  good pages to recover one is poor value.
- **`DPI = 200` and `PROMPT_MODE = "prompt_layout_all_en"` are module
  constants, not settings.** The dpi matches `DOCTOR_BITONAL_DPI` so a
  cell's bbox describes the same pixel space as the rest of the corpus,
  and the prompt mode is what the apply needs (cells *and* text). A
  one-off
  experiment overrides them per row through `input_manifest`.
- **The blocking wave goes last.** `submit_pending` sends the RunPod
  wave before doctor's. A RunPod submit returns as soon as the job is
  queued; a doctor submit holds its socket for the whole conversion
  (~25-45s a shard), and the daemon's scheduler is serial (#156). The
  other order would leave the GPU endpoint idle for a minute or more per
  tick, wasting the queue depth a narrow worker pool depends on.
- **The concurrency cap is per engine**, because each RunPod endpoint is
  its own scaling unit with its own `max_workers`. It is a debug guard
  on blast radius, **not** a cost control: RunPod bills each worker's
  cold start, so parallel shards on cold workers pay boot several times
  and three in series on one warm worker may cost less.
- **The glue applies the run, and it keeps the raw inputs.** Once every
  row of the live run is `COMPLETED`, `dots_mocr.finish_ready_runs`
  (on the collect tick, #202) offsets each `page_no` by its shard's
  `from_page` (`page_index = from_page + page_no`, `pdf_page` is that
  plus one), writes one volume JSON to
  `jobs/analyze/dots_mocr/r{run}-volume.json` — under `jobs/` so the
  generic sync never carries it — and flips the rows to `CONSUMED`.
  The per-shard results are deliberately **not** deleted: the future
  smart glue over page inserts and deletes re-reads them. A page the
  worker failed keeps its slot with its `error` (the apply reads it as
  `detected=None`). The glue writes no scan status (the #190
  invariant); a run holding a dead row is skipped silently, since
  `run_summary` already shows it and the button opens the fresh run. A
  glue *failure* retries next tick up to `GLUE_MAX_ATTEMPTS`, counted
  in the shard-0 row's `provider_meta["glue"]` (never in
  `input_manifest` — `_still_describes` compares that exactly), with
  one ERROR log at the crossing and silence after; the rows stay
  `COMPLETED`, so recovery is a deploy plus clearing the counter, not
  a re-paid run.
- **A glued run is applied by `apply_ready_runs` (#149/#204), the
  collect tick's fourth pass.** Deliberately NOT daemon-queued work
  (#212): the apply is seconds of local work that does not change what
  the scan is, so it never transits QUEUED/PROCESSING — the scan stays
  in the review flow throughout, there is no claim for a cancel to
  race, and no scan-wide `retry_count` is spent.
  `services.run_compute_issues` reads the glued JSON (its S3 presence
  is a checked precondition), rebuilds `Scan.ocr_results` through the
  `page_numbers` adapter (manual `assign_page` entries are carried
  over verbatim), takes the review edge with one compare-and-swap
  (`AWAITING_VALIDATION` or the legacy `PENDING_REVIEW` -> READY; a
  scan already READY is a recompute and keeps its status), and runs
  the unchanged `recalculate_issues` funnel, whose #154 preservation
  branch persists READY. Scans in any other status are deferred, not
  marked: `AWAITING` belongs to the bitonal merge, and its park is
  picked up on the next tick; a cancelled, errored or approved scan
  comes back only through the admin re-queue. The run-scoped
  `provider_meta["apply"]` on the shard-0 row is the idempotence
  marker (`applied_at`) and the retry ledger (`APPLY_MAX_ATTEMPTS`,
  same loud-then-quiet shape as the glue's); a failure leaves the
  status alone, so the pass retries next tick. The bands and token
  rules of the adapter come from ai-research `pipeline/core/order.py`
  and issue #149.
- **No provider abstraction, deliberately.** `jobs.py` branches on
  `job.provider`; ~600 of its lines are provider-agnostic and stay
  shared, and only the submit call and the in-flight check fork. Do not
  answer a third provider by copying a wave or a sweep. **Mistral is the
  point to promote the branches**, because it changes the shape again:
  opinion-level `EXTRACT`, no shard fan-out, rate limits rather than a
  worker pool, and no presigned PUT at all. YOLO on RunPod is a payload
  builder, an endpoint id and a cap — it shares `submit_job` and
  `poll_once` unchanged.

## Local disk hygiene (issue #215)

- The daemon frees `/tmp/scanning/{pk}`
  (`s3_sync.release_local_processing`) once S3 holds every byte: after
  `run_full_pipeline`'s push (only on push success), on every exit from
  `AWAITING` in `bitonal.finish_ready_scans` (only when the park won
  the row), and on the terminal failures (ERROR, ERROR_MAX_RETRIES) in
  `_handle_pipeline_exception`. Never on a re-queue — the retry reads
  the local files. No-ops in DEVELOPMENT and when S3 is inactive.
- The `cleanup_processing_tmp` sweep judges staleness on the newest
  mtime in the whole tree (`_tree_mtime`) — writes land three levels
  down, so the top mtime is only the creation time.
- The sweep also reclaims leaked `TemporaryDirectory` scratch dirs
  (`bitonal.MERGE_TMP_PREFIX`, `dots_mocr.GLUE_TMP_PREFIX`) that a
  SIGKILL orphans in the system temp dir. Off under TESTING; the
  command's tests point `gettempdir` at a scratch root.

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
