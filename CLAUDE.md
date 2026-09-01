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
back as an external job, dots.mocr as a pipeline-enqueued one (#207,
with the staff button kept for re-runs), the page numbers plus Issues
as an apply pass on the collect tick (#149/#204), review 1 got its
approve button (#151) and one model for its human page edits (#214),
and YOLO detection came back as a rebuilt worker image (#194 — the
image only), all below; still missing are the post-review stages
(#195/#196 — the callers the detect image waits for), so:

- `run_full_pipeline` shards the original (#164), sets `page_count`,
  enqueues the dots.mocr read (#207) when `_can_analyze` allows,
  then either starts the bitonal conversion (`Status.AWAITING`) or
  parks the scan in `Status.AWAITING_VALIDATION`.
- Every user-facing action that would re-trigger a legacy stage
  (`start_validate`, `start_detect` without existing detections,
  `reprocess`, `generate_files`) refuses with
  `utils.PIPELINE_PAUSED_MESSAGE` — one constant, flashed as a warning
  banner in HTML views. `start_validate` splits by scan (#151): only a
  legacy row hears "paused", because a new-pipeline volume is refused
  permanently, not temporarily. The daemon parks pre-cutover queued rows
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
tick). `views_process.approve_page_completeness` (#151, below) is the
only writer of DONE, and #195/#196
trigger off DONE writing **no** scan status, so redaction work never
blocks either review. Both are parked human states outside
`BUSY_STATUSES` (no polling, no sweep); `AWAITING_VALIDATION` now means
"review-1 prerequisites outstanding". `recalculate_issues` preserves
both (`PENDING_REVIEW` remains for legacy rows and step 2 only) with a
conditional DB update over the row's *current* status, never a full
save — a full save off a stale instance would silently write READY
back over a concurrent approval — and
`serve_scan_pdf` reads a missing preview under either as a failed S3
pull (409 + reload hint) — READY implies the conversion finished or
was skipped because the source is already bitonal (that copy nuance
belongs to the #204 follow-up).

## The review-1 interface (issue #151)

The buttons around those two statuses. Step 1 states its goal in the
sidebar, and the step-1 bar (`_process_actions.html`) is rendered by
both `scan_process_view` and the `process_actions` fragment, from the
same `_review_flags` — a bar that disagreed with itself would offer an
approve button the view refuses.

- **Approving is a compare-and-swap on READY**, never a full instance
  save: the collect tick can write READY over the same row at the same
  moment. Any logged-in user may press it (review 1 is the scanners'
  own step). Open issues do not block it — the browser asks for a
  confirm and the view obeys, because the curator, not the model, is
  the judge of a suspicion.
- **Approving is the gate for "Next: Detect"**, which is why the button
  cannot be gated on an empty issue list. The view enforces it too:
  `start_detect` sends a scan still in READY back to step 1, so a
  direct POST cannot walk past the review. READY is the one status
  where the approval is pending, and a legacy scan never holds it. A legacy `PENDING_REVIEW`
  scan never reaches the #154 states, so it keeps the old bar (the old
  "no open issues" rule, and "Validate"/"Re-validate") and still
  reaches step 2. Those two clauses are the whole gate on purpose: a
  scan that is neither approved nor legacy -- one still parked in
  `AWAITING_VALIDATION`, or errored -- holds no issue rows either, and
  a gate reading "no open issues" alone would show it a paid RunPod
  confirm that `start_detect` then refuses.
- **A new-pipeline volume is never re-run from the viewer.** Sharding,
  the bitonal conversion and dots.mocr are deterministic, so a second
  run returns what the first one stored, and charges another doctor
  conversion and another park out of the review flow for it. So
  `start_validate` refuses a non-legacy scan **for good**, not until
  the stages return, and the bar offers it no button at all. A volume
  that genuinely must be processed again goes through the admin
  re-queue, which is deliberately not a curator's button. Re-reading
  the *stored* page numbers is the recompute button, and it is
  unrelated.
- **The recompute button answers rather than obeys.** On a scan the
  retired PaddleOCR stage read (`services.has_legacy_ocr`: no `dots-`
  zone *and* no `ANALYZE` row — a volume dots read blank carries no
  zone either) it explains and does nothing. With pending inserts or
  deletes it warns and continues: it used to redirect to "Rebuild &
  Validate", which refuses since #173, so the guard had become a dead
  end. It never opens the PDF to fail: the page-count refresh inside
  `recalculate_issues` survives an absent, partial, or invalid local
  copy and keeps the stored count (#153).
- **Every page edit says it is saved and not applied.** `shared.js`
  calls the optional `window.onPageEditSaved` hook after a deletion and
  an undo; step 1 defines it (a green toast) and step 2 leaves it
  undefined. The page-number edit and the insert upload call it
  directly. Nothing applies these edits to the volume yet.

## Human page edits (issue #214)

Every decision a person makes about a page in review 1 is one
`PageEdit` row: the printed number (`SET_NUMBER`, blank value = the
curator cleared it), a delete, an insert, a replacement, a rotation,
and the dismissal of an issue. It replaced three storages in two
address spaces -- an entry inside the `Scan.ocr_results` JSON, a
`PageDeletion` addressed by PDF page, a `PageInsert` addressed by
printed page number -- plus nothing at all for a replacement. The
pieces: `models.PageEdit`, `page_edits.py` (the queries and the
overlay), and the endpoints in `views_process.py`. What must not be
broken:

- **A row's address -- which page of the volume it is about -- is
  always the physical space of the original as it was uploaded**,
  1-based, the space `Detection.page_index` and the shard manifest
  already use. Two columns carry it, `pdf_page` and `anchor_pdf_page`,
  and nothing else on the row locates anything. A printed number
  cannot: front matter has none, and two pages can print 1074, which
  is one of the defects review 1 exists to find. It rides along as
  `logical_page`, a label only.
- **An insert is addressed by a gap.** `anchor_pdf_page` is the page
  the image follows, 0 means "before page 1", and `ordinal` orders
  several images in one gap. `page_edits.project_inserts` does both
  halves in one walk: it stamps the anchor on every `missing`
  placeholder it renders, and the upload sends that back. Resolving it
  in the browser and throwing it away is what left the old model with
  no address at all.
- **Applying an edit closes it; it never rewrites it.** The apply
  (#206) stamps `applied_at`. So every unique key is **partial over the
  open rows** (`condition=Q(applied_at__isnull=True)`) -- without that,
  a curator could not edit the same page again after an apply.
- **A dismissal is unique per check, not per page.** One page raises
  several checks, and each rebuild writes new `Issue` rows with new
  primary keys, so the check's name is the only stable handle. Its
  address is in whichever space the check uses
  (`models.PHYSICAL_PAGE_CHECKS`), and `logical_page` is in the key
  because two `missing_page` dismissals differ only by printed number.
- **`Scan.ocr_results` is a cache, not a source.** It is rebuilt whole
  from the glued run (`page_numbers.ocr_results_from_volume`, now pure
  machine output) plus the rows (`page_edits.overlay_page_numbers`), by
  `recalculate_issues`, `rebuild_page_map` and `run_compute_issues`.
  Nothing edits one entry in place any more, so two curators on two
  pages no longer lose one of the two numbers. The `"manual"` stamp
  survives as a *derived* marker the sidebar and the offset heuristic
  read; it is no longer how a number survives a rerun.
- **An edit that cannot be placed is reported, never guessed at, and
  never acted on.** A fingerprint mismatch (`Scan.source_fingerprint`,
  stamped by `sharding.ensure_shards`, copied onto each row at write
  time) or a page the volume no longer has raises a `stale_page_edit`
  issue naming the page. A blank fingerprint on either side is legacy
  and matches anything. So every reader that *acts* on an edit --
  `deleted_pages`, `inserts_by_gap`, `has_pending_changes`, and
  `overlay_page_numbers` -- goes through `current_edits`, never
  `open_edits`: a stale delete would drop a page of a document the
  curator never saw. A stale row does not hold the review open either;
  the issue is the channel a person can act on.
- **Every open insert reaches the viewer**, including one this volume
  cannot place: `project_inserts` appends it flagged `unplaced` rather
  than dropping it. The viewer's Remove button is the only way to take
  an insert back, so an image the walk dropped would strand its row
  where nothing in the portal could reach it.
- **`has_pending_changes` counts the structural kinds only**
  (`PageEdit.STRUCTURAL_KINDS`). A number and a dismissal need no
  apply, so they no longer block the recompute button, and
  `dismiss_issue` lost its "reprocess first" guard with the convention
  it protected. The step-1 bar's two flags come from one read
  (`page_edits.pending_edit_flags`, through `_review_flags`, which both
  renderers of the bar already call): they answer one question about
  one set of rows, and read apart they disagreed — the insert flag
  counted a stale row the change flag refused.
- **The image is on the default storage** (S3 in production), under the
  scan's `page_edits/` prefix -- excluded from the generic sync like
  `shards/` and `jobs/`, swept by the admin scan deletion, and
  presignable, which is how #206 will hand it to doctor and RunPod as a
  one-page shard. `PageInsert` wrote to `LocalProcessingStorage`, so a
  preempted web pod took the image with it.
- **A printed page number is free text, and it is escaped, not cast.**
  A volume prints roman numerals, letter suffixes and section numbers,
  so `int()` on `PageEdit.logical_page` would lose what the curator
  read off the page. Instead `views_process._page_label` narrows the
  label to the alphabet a printed number uses, and `escapeHtml` in
  `shared.js` escapes it where the viewer builds markup with it — the
  label is one person's typing that every viewer of the scan then
  sees. Both layers, since narrowing alone is one regex change away
  from an injection and escaping alone would keep junk in the column.
  `SET_NUMBER.value` stays numeric (a number or a range) for a
  different reason: the sequence analysis parses it as an integer.
- `replace_page` and `rotate_page` have endpoints and no buttons: the
  interface belongs with #206 and #151. `export_pdf` applies the
  deletes and the inserts only.
- The data migrations are 0013 (the manual readings) and 0015 (the two
  retired models); 0016 drops them. The **image bytes** are moved by
  `migrate_page_insert_images`, which must run on the pod holding the
  files: until it does, a migrated insert names an S3 key the bucket
  does not have.

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
  runs, and the admin re-queue, the admin scan deletion and
  `start_dots_mocr` all call `jobs.abandon_open` from a request. A
  daemon that wrote PENDING over their CANCELLED would convert a shard
  nobody wants. The user cancel was a fourth such writer until #219
  deleted it as unreachable.
- **A slow page is a retry, not a dead volume.** Doctor reports a page
  it could not rasterize in time as `CONVERSION_TIMEOUT` (doctor #245,
  PR #246, in production), which is in `TRANSIENT_ERROR_CODES`, so the
  submit pass retries it up to `DOCTOR_MAX_ATTEMPTS` instead of writing
  the shard off on the first answer. A FAILED row therefore means the
  attempts are spent, and one still sinks the whole volume to ERROR.
  Do **not** add a pass that revives dead rows: the admin re-queue —
  the only thing that stops a scan since #219 — calls `abandon_open`,
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
- **There is no user cancel (#219).** `views_process.cancel_processing`
  covered PROCESSING and AWAITING, but no template ever rendered its
  button, so a POST-only endpoint nobody could reach carried a whole
  race. It is deleted; `Status.CANCELLED` stays for historical rows and
  has no writer. A replacement belongs on `jobs.abandon_open` with the
  status left to the daemon (#212) — do not restore the status write.
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

- **The pipeline starts it, the daemon runs it** (#207).
  `run_full_pipeline` creates the ANALYZE rows next to the CONVERT
  rows, gated by `services._can_analyze` (committed shards,
  `dots_mocr.enabled()`, S3 active — the mirror of `_can_convert`),
  and the daemon submits, polls and retries them. The stage is
  independent of the bitonal branch: it reads the *original* shards,
  so a volume that parks unconverted still gets its read. A lost
  status guard hands back only the *unstarted* ANALYZE rows (PENDING
  plus in-flight, via `abandon_open(statuses=...)`): the claim is lost
  most often to the daemon's own shutdown — the SIGTERM handler
  re-queues mid-flight scans and returns, so the pipeline continues on
  a scan it no longer holds — and a COMPLETED row is a paid result the
  carry re-reads on that retry. The
  staff button on `/scan/process/` remains as the manual way in — a
  re-run over an edited volume, or a backfill for scans uploaded while
  the stage was button-only. Row creation is what costs GPU money, so
  the creators' caller set stays pinned by an AST test
  (`TestKnownEnqueuePaths`): exactly the pipeline and the button.
  The button's request makes **no** call to RunPod, and it never cuts
  shards: `sharding.committed_manifest` verifies the stored set with
  one `head_object` on the original (size plus `Scan.page_count` *is*
  the whole fingerprint), so a web pod never pulls a multi-GB PDF and
  never reads `shards/` directly. A stale set is refused, not re-cut.
  A `cancel_processing` does **not** touch a running read: the results
  are kept, the apply defers a cancelled scan, and a later re-queue
  reuses the completed run for free.
- **A replacement run re-reads only the shards that need it.** When
  `ensure_shard_jobs` starts a new ANALYZE run (a dead row sank the
  old one), a shard whose identity is unchanged and whose result
  object an S3 HEAD confirms enters the run as a `COMPLETED` row
  pointing at the prior attempt's object (`jobs._reusable_results`,
  opt-in via `reuse_results` — dots.mocr only, since the bitonal
  merge deletes its results). The row identity now carries the
  shard's `size_bytes`: pages alone cannot tell a re-uploaded
  original with the same page count from the one the result was
  computed on, and size plus page count is the fingerprint the shard
  manifest itself trusts. The carry match is strict — a legacy row
  without the field re-reads — while `_still_describes` accepts the
  legacy shape, so pre-deploy live runs are still reused whole
  instead of re-paid. A carried row has a blank `external_id`, so
  every cancel path stays a provider no-op on it.
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

## Generalized YOLO worker image (issue #194)

`scanning/runpod/` is the RunPod Serverless image that runs detection
with `bl_warm`, one 18-class checkpoint that replaced the
small/medium/large trio (blackletter #73). It is the image only: the job
rows and the daemon path are #195, and the redaction work that reads its
output is #196, so a fresh deploy has no automatic caller. What must not
be broken:

- **Only `bl_warm.pt` is baked.** `api.detect` calls `ensure_weights`
  itself, so an unbaked name would reach Hugging Face from inside a paid
  job; `handler._missing_weights` refuses it up front and names what the
  image does carry. Running the legacy trio again is a rebuild, not a
  changed input. PR #167 kept the trio for exactly that rollback and it
  is deliberately gone.
- **The handler passes no `imgsz`, and that is the whole point.**
  Ultralytics keeps the training resolution off the checkpoint
  (`Model._reset_ckpt_args`) and `predict` merges those overrides ahead
  of its own defaults, which name none. `bl_warm.pt` carries 1024, so it
  predicts at 1024; passing the library default of 640 would quietly
  cost the small classes (key icon, page number, state abbreviation).
  The build prints the value and `_preload` logs it, because a
  checkpoint that lost its training arguments falls back to 640 with no
  error and a lower score.
- **`found_by` survives into the payload.** blackletter's merge writes
  that provenance in place of the per-model `model` key, and
  `bl_warm.rows_are_bl_warm` reads it to pick the bl-warm confidence
  gates. #196 needs it; stripping it silently changes every rect.
- **Detection reads the original shard, never the bitonal copy.**
  bl-warm was trained on greyscale renders and its large region classes
  collapse on 1-bit pages (caption F1 0.99 -> 0.25, measured in #167).
  This matches dots.mocr, so both stages fan out over the one shard set
  of #164.
- **`page_index` counts from zero inside the shard.** The caller offsets
  it by the shard's own `from_page`, as #149 does for dots.mocr.
- **The payload goes to S3, not inline.** The envelope
  (`schema_version`, `action`, `scan_pk`, `result_key`, `payload`) is
  PUT to a presigned URL signed `application/json`, and the response
  carries a summary plus `detection_count`. Thousands of rows would
  approach RunPod's ~20 MB response cap, which it discards ~30 min after
  the job ends. Without `result_url` the worker answers inline, so a
  rollback needs no daemon change.
- **The base image carries no CUDA layer.** The torch `cu126` wheels
  declare the CUDA runtime themselves, cuDNN included, so
  `python:3.12-slim-bookworm` is enough. The `nvidia/cuda` base and the
  build-time `libcuda.so.1` stub existed for PaddlePaddle alone and went
  with the `analyze` step (#173), along with tesseract and Ghostscript.
  About 4.2 GB to pull and 8 GB unpacked, against the ~22 GB the old
  image extracted to. `mkdir -p $YOLO_CONFIG_DIR` is
  load-bearing: ultralytics tests the parent for writability and would
  otherwise write its settings to /tmp on every boot.
- **The endpoint is reused, not replaced.** The restored
  `build-runpod-worker.yml` pushes
  `freelawproject/blackletter-gpu-worker:<sha>` and PATCHes the same
  `RUNPOD_TEMPLATE_ID`, so no new endpoint, no new secret and no
  settings change. `blackletter-dependency-pr.yml` bumps blackletter
  here and in the repository root together, which is what keeps the
  image and the application on one version.
- **The worker scaffold lives in `runpod_common`, once.** The Sentry
  setup, the boot clock and worker-meta fields, the result envelope
  (`RESULT_SCHEMA_VERSION`, which `runpod_client.py` imports too), and
  the `execute_action` runner whose except arms map exceptions to the
  error codes the daemon classifies — shared with the dots.mocr worker,
  because one daemon reads every worker's output and an arm that
  drifted in one image would misroute paid work. Each handler keeps
  thin wrappers that bind its own module globals, so the tests keep
  their patch points. Input checks raise `BadInputError` (a
  `ValueError` subclass in `runpod_common`), and only that subclass is
  answered as the terminal `BAD_INPUT`: the worker stack raises plain
  `ValueError` at run time too (a degenerate page render, a model
  shape check), and answering one of those as BAD_INPUT would write
  the shard off as a caller error with no Sentry event.
- The handler is tested without the worker stack
  (`scanning/tests/test_runpod_yolo_handler.py`): the loader stubs
  `runpod`, `torch` and `blackletter.api`, and a CUDA-less `torch` makes
  the module-level `_preload` open no weight file.

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

## Intake backpressure (issue #218)

`process_next_scan` claims a QUEUED scan only while fewer than
`DAEMON_MAX_ACTIVE_SCANS` (5) scans hold unfinished external work
(`jobs.active_scan_count`). It is the daemon's only backpressure:
uncapped intake put 2023 conversion rows behind 27 parked scans on
2026-08-31, three times what the 6-hour
`DAEMON_JOB_MAX_QUEUE_SECONDS` ceiling can drain, and 29 volumes died
of it.

- **The gate is on the claim, not the dispatch after it.** A refused
  scan stays QUEUED — nothing times out there — instead of transiting
  PROCESSING for nothing. The admin re-queue is safe at any batch size.
- **Recovery runs first and unconditionally**: returning a scan the
  daemon dropped is not intake.
- **Scans, not rows, over every stage.** A scan's whole shard set
  enters every queue at once. Counting `CONVERT` alone would watch the
  wrong queue: doctor drains ~100 rows/h, dots.mocr 24-36.
- **`COMPLETED` does not hold a slot** (`WAITING_JOB_STATUSES` is
  `OPEN_JOB_STATUSES` minus it): those rows wait on the merge, the glue
  or the apply, and a failed merge leaves one nothing moves again.
- **The knob moves by its arithmetic**: slots × the largest volume's
  shards must clear `DAEMON_JOB_MAX_QUEUE_SECONDS` at the slowest
  queue's rate. 5 × ~20 shards is ~100 dots.mocr rows, ~4.2h against
  6h. Ten slots would not fit.
- No deadlock — the submit and collect ticks drain regardless — but a
  stage switched off with rows still PENDING holds its slots until the
  queue deadline expires them. The staff buttons bypass the gate: one
  scan at a time, from a request.
- One log line per crossing (WARNING pausing, INFO resuming), the
  loud-then-quiet shape the glue and apply retries use.

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
