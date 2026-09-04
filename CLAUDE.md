# Scanning Portal

Upload portal for FLP volunteer scanners. Single-app Django project where `scanning/` is both the project package and the only app.

## Quick Reference

```bash
# Run tests
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py test scanning.tests -v 2

# Run a single test class
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py test scanning.tests.TestScanUpload -v 2

# Survey the layout-JSON repair over the corpus, changing nothing (#242)
docker exec scanning-daemon python manage.py reglue_dots_mocr --dry-run

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
and YOLO detection came back as a rebuilt worker image (#194) with its
job rows and its staff button (#195) and the redaction work that reads
its output (#196), all below; what is still missing is step 3, the
file generation (#206), so:

- `run_full_pipeline` shards the original (#164), sets `page_count`,
  enqueues the dots.mocr read (#207) when `_can_analyze` allows,
  then either starts the bitonal conversion (`Status.AWAITING`) or
  parks the scan in `Status.AWAITING_VALIDATION`.
- Every user-facing action that would re-trigger a legacy stage
  (`start_validate`, `reprocess`, `generate_files`) refuses with
  `utils.PIPELINE_PAUSED_MESSAGE` — one constant, flashed as a warning
  banner in HTML views. `start_validate` splits by scan (#151): only a
  legacy row hears "paused", because a new-pipeline volume is refused
  permanently, not temporarily. `start_detect` left that set with #196:
  detection works, so a volume with no detections is told where its
  run stands (`detection_message`), not that a pipeline is paused. The daemon parks pre-cutover queued rows
  carrying a legacy `queued_action` back to PENDING_REVIEW with the
  same message; admin re-queue resets `queued_action` to
  FULL_PIPELINE.
- Pairing and the redaction geometry have a caller again (#196, below).
  `run_generate_files` and `upload_approved_files` — step 3 — are kept
  but nothing queues them.
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
only writer of DONE, and both detection stages trigger off it: #195
writes **no** scan status while it reads, and #196 borrows the scan
from DONE and hands it straight back (`_park_after_redactions`), so
neither review is ever blocked by redaction work. Both are parked human states outside
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

One `PageEdit` row per curator decision in review 1: a printed number
(`SET_NUMBER`, blank value = cleared), a delete, an insert, a
replacement, a rotation, and the dismissal of an issue. It replaced
three storages in two address spaces. The pieces: `models.PageEdit`,
`page_edits.py` (queries and overlay), the endpoints in
`views_process.py`. What must not be broken:

- **Every address is a physical page of the original as uploaded**,
  1-based (`pdf_page` / `anchor_pdf_page`) — the space
  `Detection.page_index` and the shard manifest use. A printed number
  locates nothing (front matter has none, duplicates exist); it rides
  along as the `logical_page` label.
- **An insert is addressed by a gap**: `anchor_pdf_page` is the page
  the image follows (0 = before page 1), `ordinal` orders one gap.
  `project_inserts` stamps the anchor on every `missing` placeholder,
  and the upload sends it back.
- **Two stamps close an edit, and neither rewrites or deletes it**:
  `applied_at` (the apply, #206) and `withdrawn_at` (the curator took
  it back, #232). So every unique key is partial over the rows that
  carry neither (`applied_at__isnull=True &
  withdrawn_at__isnull=True`) — else a page could not be edited again
  — and so is every lookup of `get_or_create` / `update_or_create` in
  `views_process`, or one would match a row that decides nothing.
- **A dismissal is unique per check, not per page**: rebuilds give
  `Issue` rows new PKs, so the check name is the only stable handle.
  Its address space follows `models.PHYSICAL_PAGE_CHECKS`;
  `logical_page` is in the key.
- **`Scan.ocr_results` is a cache**: rebuilt whole from the glued run
  (`page_numbers.ocr_results_from_volume`) plus the rows
  (`page_edits.overlay_page_numbers`) by `recalculate_issues`,
  `rebuild_page_map` and `run_compute_issues`. The `"manual"` stamp is
  a derived marker, not how a number survives a rerun.
- **An unplaceable edit is reported, never applied**: a
  `Scan.source_fingerprint` mismatch (stamped by `ensure_shards`,
  copied onto each row; blank = legacy, matches anything) or an absent
  page raises a `stale_page_edit` issue. Every acting reader
  (`deleted_pages`, `inserts_by_gap`, `has_pending_changes`,
  `overlay_page_numbers`) goes through `current_edits`, never
  `open_edits`. A stale row does not hold the review open.
- **Every open insert reaches the viewer**: `project_inserts` appends
  an unplaceable one flagged `unplaced` — Remove is the only way to
  take an insert back, so a dropped image would strand its row.
- **An undo stamps, and nothing is ever deleted** (#232):
  `undo_delete_page`, `remove_page_insert` and `undo_replace_page` all
  call `page_edits.withdraw` (which also writes `date_modified`, since
  `update()` skips `auto_now`), and the file of a withdrawn insert or
  replacement stays in the bucket. An undo of a decision that no
  longer stands is a no-op, not an error: a second tab or a second
  click must not fail a page that is already back. The audit must show every decision
  a person made and every page they sent. It replaced three hard
  deletes and the `update_or_create` of `replace_page`, which wrote
  over `image` and left the first object with no row naming it.
- **`has_pending_changes` counts `STRUCTURAL_KINDS` only** — a number
  or a dismissal needs no apply. It raises the step-1 banner and
  badge; `has_pending_inserts` comes from the same read
  (`page_edits.pending_edit_flags` via `_review_flags`) and waits for
  #206, whose apply button is the one that must warn about a paid
  run. Computed apart, the two disagreed on stale rows.
- **The image is on the default storage** under the scan's
  `page_edits/` prefix: excluded from the generic sync, swept by admin
  deletion, presignable for #206. `PageInsert` used
  `LocalProcessingStorage`, so a preempted web pod lost the image.
- **The printed label is free text: narrowed** (`_page_label`) **and
  escaped** (`escapeHtml` in `shared.js`) — both layers on purpose.
  `SET_NUMBER.value` stays numeric: the sequence analysis parses it.
- **An upload is an image or a PDF** (#232, `_accept_page_upload`).
  The first bytes decide, not the content type -- for an image too
  (`_IMAGE_MAGIC`: PNG, JPEG, GIF, TIFF, BMP, the formats MuPDF opens;
  an SVG sent as `image/svg+xml` used to pass and fail in
  `insert_image` at the export) -- and the stored name is given the
  extension of what they say: `page_edit_image_path` keeps that
  extension and `export_pdf` reads it to choose between `insert_pdf`
  and `insert_image`. A replacement must be one page; an insert may be
  several, because a missing leaf often is. Both cap at
  `PAGE_UPLOAD_MAX_BYTES`.
- **The file is stored before the row, and taken back if the row
  loses** (`_save_page_file_row`). Two uploads for one page or one gap
  at the same moment both find nothing to withdraw and both insert;
  the second loses to the partial unique key and answers 409. With
  `objects.create` the losing request left an object in the bucket
  that no row named.
- `rotate_page` is an endpoint without a button (the interface belongs
  to #206/#151). `replace_page` has one since #232. `export_pdf`
  applies deletes and inserts only.
- Data migrations: 0013 (manual readings), 0015 (the retired models),
  0016 (the drop). Run `migrate_page_insert_images` on the pod holding
  the files; until then a migrated insert names an absent S3 key.

## Replace a page, and approve with edits pending (issue #232)

The Replace button of review 1, beside Delete on every page of step 1.
It writes a `REPLACE_PAGE` row (#214) and shows a note on the page and
a `REPL` badge in the sidebar row; the note's "View" link goes through
`page_edit_file`, a redirect that signs a URL at the moment of the
click (the storage's own URL expires within the hour, and a review
page stays open longer). Nothing builds the row into the volume: that
is #206.

- **The approve button no longer waits for an apply.** The step-1 bar
  answered one open structural edit with "Rebuild & Validate" alone,
  and hid the recompute and approve buttons while it did. That branch
  is deleted. Two things had made it a dead end: `reprocess` refuses
  for every scan since #173, and nothing stamps `PageEdit.applied_at`,
  so "pending" lasted for the rest of the review rather than until the
  next rebuild. One deleted duplicate page therefore ended a curator's
  review. The apply runs *after* the approval by design, so a bar that
  waited for it waited for the button it was hiding.
- **The pending banner says the whole truth**, and
  `PENDING_EDITS_SAVED_MESSAGE` says the same words: the changes are
  saved, the corrected volume is not built from them yet, each
  inserted or replaced page must go through the conversion and the OCR
  on its own, and we apply them when that pass is ready. Do not
  shorten it back to "not applied yet" — a curator who cannot see why
  has no way to judge the risk of approving.
- **The viewer binds Replace by delegation on the container.**
  `shared.js markPageAsDeleted` writes over the whole `page-label`,
  and its undo restores the saved label and re-binds the delete button
  alone, so a listener on the Replace button would not survive a
  deletion and an undo. `refreshSavedLabel` copies the label into
  `label.dataset.originalHtml` after a note is added or removed, or
  the undo of a deletion would drop the change -- on a live page only,
  because while the page is marked for deletion the label is the
  deletion mark, and saving that would make the undo restore the mark.
- **`start_detect` checks the approval before the pending edits.** The
  other order told a scan already approved, with one open edit, to
  approve again, and sent it to a step 1 with no approve button.
- **A PDF insert draws its first page in a canvas** with the pdf.js
  the page already loads, under a link that opens the whole file. The
  link stands whatever the render does: a cross-origin read of the
  bucket needs the CORS rule of #185, which a deployment may not carry
  yet.

## Page repair requests (issue #249)

A reviewer with no book finds a blurry page or a missing leaf, and
cannot fix it. `models.PageRepairRequest` keeps the finding until a
scanner does the work. The pieces: the model, `repairs.py` (queries
and the derived state), two endpoints in `views_process.py`
(`request_page_repair`, `dismiss_page_repair`), the
`views.repair_queue` page at `/repairs/`, and the "Ask for a rescan"
and "Ask for this page" buttons of step 1 (`viewer_step1.js`). A
read-back endpoint was written and deleted before it shipped: nothing
called it, and an endpoint nobody reaches still carries its surface
(#219). What must not be broken:

- **A request is not a `PageEdit`.** A `PageEdit` is a decision the
  apply builds into the volume; a request is work for a person. It
  needs a free-text `note` (500 characters, `repairs.NOTE_MAX_CHARS`)
  and it has an end state a decision does not have. A reader of
  `page_edits.open_edits` never sees one, so the apply cannot mistake
  it for a decision, and `has_pending_changes` ignores it.
- **The address is the `PageEdit` address**: `pdf_page` for a rescan
  (REPLACE), `anchor_pdf_page` for a missing leaf (INSERT, 0 = before
  page 1), both in the physical space of the original as uploaded.
  The printed number rides along in `logical_page` as a label. The
  endpoint resolves both through `_pdf_page_of` and `_anchor_of`, so
  a page outside the volume is 404.
- **Fulfilled is derived, never stamped.** `repairs.annotate_fulfilled`
  marks a request whose address carries a `REPLACE_PAGE` or
  `INSERT_PAGE` edit that is **later than the request**, not
  withdrawn, and made against the **same upload** (same fingerprint,
  or a blank one). The date is load-bearing: a reviewer who finds the
  replacement blurry too asks again, and without it that request is
  born fulfilled and no scanner ever sees it. `applied_at` is **not**
  read: it says whether a decision is built into an output, and the
  fingerprint says which upload it is counted against. The two are
  independent, and neither overrides the other. So the upload cannot
  race a stamp, an undo of the upload (#232) reopens the request with
  no second writer, and the "Fulfilled" tab of the queue costs one
  `Exists` subquery.
- **The original never changes.** Every address is a page of the
  original as uploaded, and the apply (#206) writes another file and
  leaves it alone. So an apply never invalidates an address, and a
  request goes stale for one reason only: somebody re-uploaded the
  volume. The same holds for `PageEdit.source_fingerprint`.
- **A fulfilled row is still an open row, and the key matches it.**
  SQL cannot index a derived flag, so the partial unique key cannot
  exclude a fulfilled request, and a reviewer who finds the new scan
  bad too gets `created: false` from `get_or_create`. The endpoint
  says so (`already_fulfilled`, `REPAIR_ALREADY_FULFILLED_MESSAGE`),
  the viewer shows that message and not "already requested", and the
  fulfilled note keeps its Dismiss button so the reviewer can close
  the answered request and ask again. Do not let that path go quiet:
  it is the mirror of the born-fulfilled case, one step later.
- **Dismissed, never deleted.** `repairs.dismiss` stamps `dismissed_at`
  and `dismissed_by` (and writes `date_modified`, since `update()`
  skips `auto_now`). Any logged-in user may dismiss, the rule of every
  review-1 button. A second dismissal is a no-op. One open row per
  address (`uniq_open_repair_request_per_address`, partial over
  `dismissed_at IS NULL`, `nulls_distinct=False`): a second request
  answers the first row with `created: false`.
- **A stale request is marked, not dropped**: `PageRepairRequest.is_stale`
  (and `repairs.is_stale` for a caller holding the scan) is the
  fingerprint rule of `page_edits.is_stale`. The step-1 viewer and the
  queue page both show "EARLIER UPLOAD". Nothing applies a request, so
  nothing needs to refuse one.
- **The note is escaped where it is drawn**: `escapeHtml` in the
  viewer, the auto-escape in the templates, and `json_script` for the
  script block. It is not narrowed, because it is prose, not a page
  number. The label goes through `_page_label` like every other label.
  For a rescan the server reads it off `ocr_results` itself and drops
  a reading the narrowing refuses; a label sent by the viewer is
  ignored, since refusing it would fail the button on exactly the page
  whose reading is junk. For a missing leaf the label is an address
  (`_anchor_of` places an older viewer's gap by it), so a refused one
  is a 400.
- **The queue page paginates scans, not rows** (`repairs.queue_scan_ids`
  then `repairs.group_by_scan`): a row is never deleted, so the `all`
  and `dismissed` states grow for good, and a page that loaded every
  row first would grow with them.
- **A missing leaf can be asked for only where a placeholder is
  drawn**: the button sits on the `missing` entry the sequence
  analysis produced. A gap the page numbers do not reveal has no
  button yet.
- **Step 1 draws everything from one list** (`SCAN_CONFIG.repairRequests`):
  the note on the page (with Dismiss while it waits), the `NEED`
  sidebar badge, the "Repairs requested" section and the header badge.
  A request is not an `Issue` row: an issue card has its own dismiss,
  and one finding must have one. The buttons bind by delegation on
  the container, as Replace does (#232), and `refreshSavedLabel` runs
  after a note is added or removed.
- **The queue links to a page**: `scan_process?step=1&goto=<pdf_index>`
  scrolls to the placeholder once it exists (`goToRequestedPage`).
  The header count (`context_processors.waiting_repairs`) is one
  query for a logged-in user.

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
- **The queue ceiling is stamped once per attempt, at the attempt's
  first claim** (`submit_deadline_fields`) — the moment the row is
  handed to the provider. A row waiting in **our own** queue carries no
  deadline at all (issue #218, below): an admitted backlog may lawfully
  wait longer than any ceiling. Nothing after the first claim moves it.
  A defer does not, a re-claim after a defer does not (the ceiling is
  written only over `deadline=None`), and only the `IN_PROGRESS`
  crossing replaces it. Any of those re-stamping would forgive the
  row's wait on every tick, so a paused or saturated endpoint would
  hold a scan forever instead of failing it — which is the one outcome
  the ceiling exists to produce. A retry clears the deadline, so the
  next attempt's first claim restarts the wait.
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
- **The worker retries a page itself, on a changed input (#238).**
  Every unread page in 30 days of production was a repetition loop on
  the last, mostly blank page of an opinion whose verso showed
  through; greedy decoding is deterministic, so the same render loops
  again. `_parse_page` climbs a two-rung ladder: greedy on the render,
  then greedy on the render thresholded at `RETRY_THRESHOLD` (100
  removes the show-through and keeps the text; doctor's 160 does not).
  The render is the only change -- no sampling, no repetition penalty
  (a penalty cannot tell a loop from the keys layout JSON repeats) --
  so the stage stays deterministic for the same page. Only a page with
  no usable output climbs; the page dict says what happened
  (`attempts`, `recovered_by`, `render`, `errors`)
  and the summary carries `recovered_pages` and `filtered_pages`,
  logged at INFO beside the WARNING. **A filtered page is a hole
  too**: the answer was text but not layout JSON, so there is no cell
  to place a number in; the page keeps upstream's cleaned text in
  `md`. Since #242 (its own section below) the repair runs first and
  most such pages are given back, so a page that is still filtered is
  a shape nobody has measured, and it is a WARNING.
  **Every page keeps the answer as the model wrote it in
  `raw`** (a failed page: the last truncated answer): `cells` is
  upstream's parsed and rescaled copy with `int()` on every
  coordinate, and the cleaner discards a broken JSON, so `raw` is the
  only thing a later post-processor can start from. It lives in the
  shard result object, which is kept for good; the glue leaves it out
  of the volume document the apply downloads. The
  cap is `HANDLER_MAX_COMPLETION_TOKENS` = 6144, about
  twice the longest measured page (3114) and not 16384: every rung
  pays the cap once, and at the old value one looping page made its
  shard four times slower. **A result with a hole is never carried**
  (`jobs.has_unread_pages` in `_reusable_results`, either list). The
  lists are read off the row's stored summary, and the glue stamps
  them there from the result object itself (`_stamp_page_lists`): a
  row completed by an S3 HEAD stores `output=None`, and would
  otherwise pass as clean for good. **A stable hole is carried after
  all** (`jobs.hole_is_stable`): the worker is deterministic, so when
  the previous run already re-read the shard and its summary holds
  the same lists, a third read buys the same answer; the backfill
  skips a volume whose every hole is stable, so it may be run again
  after each new worker image and reads only what that image has not
  tried. That is what makes
  `reread_failed_pages` the backfill: it forces a new run
  (`ensure_shard_jobs(force_new_run=True)`) that re-pays only the
  shards with unread pages. Run it after the worker image is live.
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
  worker pool, and no presigned PUT at all. YOLO on RunPod was exactly
  a payload builder, an endpoint id and a cap, which is what
  `jobs.RunpodEngine` now tabulates (#195); it shares `submit_job` and
  `poll_once` unchanged.

## Reading the page number (issue #228)

A reporter's head band carries rival numbers: the volume number in the
title, the parallel citation page, the first page in the `Cite as`
line, a headnote digit dots labels `Page-header` too, a case name that
ends in a digit.

- **Position tells them apart, so the rank in `page_numbers.py` is
  geometric**: header before footer, then the score, then the corner
  distance, then the line inside the cell. The printed number is at the
  outer corner, every rival nearer the middle.
  The score stays ahead of the distance because it carries the band:
  dots labels a headnote digit `Page-header` wherever it sits, and a
  distance-first rank hands the page to a column of them printed in the
  margin of the body. Before #228 the rank was the score alone, which
  left the model's cell order to break every tie: 666 of one volume's
  1294 pages carried a rival value, and 8 read the wrong one.
- **The distance is per token**: one bbox covers the whole running
  head, so the end of the line the token was read at says which edge to
  measure. `CORNER_BAND` only grades the score -- a number centred in a
  footer has no rival to lose to -- and `_score` counts the corner in
  place of "only digits", which is what a headnote number is.
- **Each line of a cell is read alone**: dots returns the running head
  and the `Cite as` line as two lines of one cell, which puts the
  number mid-text. They share the one bbox, so the line index separates
  them.
- **`_resolve_by_neighbours` is the second net, not the rule**: it
  moves a pick only when **both** neighbours name one number the page
  offers, off the *geometric* picks throughout. One neighbour is not
  enough -- the rivals run in sequence themselves, so a single misread
  page would hand its own sequence to the page beside it. It never
  moves a range, and it never invents a number.
- **A reading change reaches no volume already applied** (`applied_at`
  closes the run, #204). Run `reapply_page_numbers` after the deploy:
  it re-reads the stored glued document at no GPU cost, keeps the
  numbers a curator typed (#214), and leaves an approved volume alone.

## The compressed page (issue #233)

A book prints several pages on one physical page, and the head band
then carries a range (`913–925`) in place of the number. The stage
that answers it is the reading, not the issue computation:
`blackletter.validate` already breaks the sequence at a `type="range"`
entry and counts every page the range covers as present, so a volume
that shows "Large gap" is a volume whose range nobody read.

- **A range is read at the corner of its line**, like a single number.
  The whole-line rule alone left the compressed page of a reporter
  with no number at all, because the line reads
  `913–925 ATLANTIC REPORTER, 2d SERIES`.
- **Two numbers joined by a dash are not always a range.**
  `page_numbers._range_value` guards every range: forward, and at most
  `MAX_RANGE_SPAN` pages. A docket number (`19-1234`) runs past the
  end of any volume and a split year (`1996-97`) runs backward.
- **The curator may type one**, in step 1 and in `assign_page`, with
  the dash the page prints: `views_process._page_number_value` reads
  an en dash and an em dash and stores one hyphen, which is the shape
  every reader of a range parses.
- **A range the curator typed is a note, not a question.**
  `services._note_curator_ranges` lowers the `page_range` card to
  `info` and rewords it. The card stays: the range is a fact about the
  volume the next reader must see. A range the *model* read keeps the
  warning and its "Verify this is expected".

## The layout JSON that broke on one character (issue #242)

A `filtered` dots.mocr page is a page whose answer was **good** and
whose JSON broke on one character. Measured on four production volumes
(scans 2726, 2702, 2665, 2705): one filtered page each, one edit each,
and the printed page number recovered every time. Upstream
`post_process_output` throws the whole array away and returns the
words, so the page reached the reader with no cell and no number.
`scanning/layout_json.py` moves the character back.

- **Three arms, one per measured shape**, and each moves one
  character: `Expecting ',' delimiter` at a lone `"` inside a string
  (put a backslash in front of it), `Invalid \escape` at a lone `\`
  (put a quotation mark after it), `Extra data` at a doubled closer
  (cut at the first complete parse). `MAX_EDITS` is 3, so a page with
  two faults still repairs and a rewrite of a different answer cannot
  run away. An arm whose message matches but whose text does not
  refuses, and the page stays filtered.
- **One module, two callers, and that is the point.** The **worker**
  (`handler._repair_layout_json`) stops the next filtered page, and
  the **glue** (`dots_mocr._repair_shard`) recovers the shard results
  already in the bucket, which no new worker image reaches. The
  Dockerfile copies `layout_json.py` next to `handler.py` (so it
  imports it as a top-level module, like `runpod_common`) and the
  build workflow watches it, because one arm that drifted between the
  two sides would repair a page differently depending on which side
  read it. No Django import in that module, ever.
- **A repaired page is checked, not trusted.** `_check_cells` demands
  a list of objects, a `bbox` of four numbers, a legal box
  (upstream's `is_legal_bbox`) and a `category`. The worker hands the
  repaired array back through upstream's own `post_process_output`,
  so a repaired page reaches the caller by the path every other page
  takes, bboxes included, and upstream refusing it again reads as
  "not repaired". The glue has no page image, so it rescales with
  `layout_json.rescale` (upstream's `post_process_cells` arithmetic)
  and keeps upstream's cleaned text as `md` — the apply reads `cells`
  only.
- **`raw` is never written over.** It is the answer as the model wrote
  it (#238), the only thing a later post-processor can start from, and
  the reason `reglue_dots_mocr` can be run again after a new arm
  lands. A repaired page carries `repaired` (the edits, with their
  offsets) and `repaired_by`, `"worker"` or `"glue"`.
- **The repair runs before the ladder climbs.** The fault is in the
  escape and not in the render, so a second render makes the same
  mistake: on each of the four measured pages rung 2 spent 90 to 120
  seconds and recovered nothing. The climb is **kept** for a page the
  arms do not reach, because that is an unmeasured shape and nothing
  says the render is innocent there. `reglue_dots_mocr --dry-run`
  counts what the rung recovers on those, and that number is what may
  retire the climb.
- **`repaired_pages` is the fourth page list** (`jobs.PAGE_LIST_NAMES`,
  `dots_mocr.PAGE_LISTS`, `_SUMMARY_FIELDS`), and a repaired page is
  **not** in `filtered_pages`: the repair clears `filtered` on the
  page dict. So `jobs.has_unread_pages` reads false, the carry keeps
  the shard's paid result and `reread_failed_pages` leaves the volume
  alone. The glue repairs **before** `_stamp_page_lists`, which is
  what makes that true of the rows too.
- **A page still filtered is a WARNING**, not an INFO. Issue #242 asks
  for it by name: every measured shape is repaired now, so a survivor
  is a new shape, and the log line is the whole triage path. The
  glue's line carries the parser's own message and an excerpt of `raw`
  around the fault, so classifying the next one needs no S3 read and
  no shell on the pod.
- **`reglue_dots_mocr` is the backfill**, and it costs no GPU time: it
  glues a run again over the stored results and clears the apply stamp
  (`reopen_apply`). Safe by the two properties of
  `reapply_page_numbers`: a READY volume is a recompute that keeps its
  status, the numbers a curator typed are `PageEdit` rows and survive,
  and an approved volume is not in `APPLY_STATUSES`. **Run it before
  `reread_failed_pages`**: that command reads `filtered_pages` off the
  row, a run glued before the deploy still carries them, and a re-read
  started first pays RunPod for the shards this repairs for nothing.
- **The `--dry-run` is the corpus survey** of items 1 to 3 of the
  issue, and it reads *every* glued volume in review 1, not only the
  ones whose rows report a filtered page. A volume whose retry rung
  recovered every filtered answer carries no filtered page to be found
  by, and it is exactly the evidence that the rung works; it is also
  part of the page total the rate divides by.
- Upstream has no repair, and `main` is byte-identical to the pin
  `23f3e56` on `layout_utils.py` (checked 2026-09-03), so a pin bump
  buys nothing here. Constrained decoding on the vLLM call
  (`extra_body={"structured_outputs": {"json": ...}}`; `guided_json`
  went in 0.12) would remove two of the three shapes at the source and
  is the next step, gated and measured.

## Generalized YOLO worker image (issue #194)

`scanning/runpod/` is the RunPod Serverless image that runs detection
with `bl_warm`, one 18-class checkpoint that replaced the
small/medium/large trio (blackletter #73). It is the image only: the job
rows and the daemon path are #195 (below), and the redaction work that
reads its output is #196 (below). One staff button is still the only
way in — the pipeline enqueues no detection until #211. What must not
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

## YOLO detection via RunPod (issue #195)

The caller the #194 image was waiting for. Detection runs on RunPod
Serverless, one job per **original** shard, tracked on `ExternalJob`
rows at `DETECT`/`BLACKLETTER`/`RUNPOD`. The pieces: `yolo.py` (the
stage), `settings/project/yolo.py` (five variables), the
`jobs.RunpodEngine` table, and `views_process.start_yolo_detect` (the
button). It reuses the whole #190 machinery — the claim, the poll, the
deadlines, the cancel, the retry, the carry-over — so what follows is
only what is new or specific:

- **The daemon starts it, once per shard set (#250).**
  `yolo.enqueue_missing_runs` is the submit tick's first pass, before
  the wave, so the rows it creates go out on the same tick. Its rule:
  a scan whose current shard set (`Scan.source_fingerprint`) has
  **no** detection row, alive or dead, gets exactly one run. So a new
  upload, a volume from before the sweep and a volume uploaded while
  the stage was off are one case, and a dead run is **not** re-run by
  a tick -- `YOLO_MAX_ATTEMPTS` were spent on a shard, and a fourth
  attempt is a staff decision (a one-off `yolo.ensure_detect_jobs` in
  a shell, which carries every good shard). The rule is one query
  through `ExternalJob.source_fingerprint`, stamped by
  `ensure_shard_jobs` on every row of every stage from the manifest's
  source; it is **not** in the `input_manifest` identity, which
  `_still_describes` compares exactly, and a blank (pre-column) row
  matches anything, so no button-era run is re-paid. Candidates come
  from the database (`SWEEP_STATUSES`: the parked states between the
  pipeline and the end of review 2; QUEUED/PROCESSING are the
  pipeline's, ERROR and beyond come back through the re-queue), newest
  first and **at most `YOLO_MAX_CONCURRENCY` per tick** -- each costs
  two S3 calls (`sharding.committed_manifest`: the manifest and a HEAD
  of the original) and the scheduler is serial, so an unbounded first
  tick over the corpus would hold every poll for minutes. One value
  for shards in flight and volumes per tick, on purpose: a second knob
  would be one nobody tunes, and a small batch only means the rows are
  created over more ticks. A refused set (re-uploaded or missing
  original, a `MANIFEST_VERSION` bump) is memoed in the daemon process
  for `REFUSAL_RETRY_SECONDS` and logged once at INFO, then DEBUG --
  looked at every 5 s it would cost two S3 calls and a line 17,000
  times a day, and it would hold a place in the batch. The memo is a
  cost saver only: the rows are what prevent a second run, so a
  restart that forgets it starts nothing twice.
  `yolo.ensure_detect_jobs` is the only creator and the sweep its
  only caller, pinned by `TestKnownEnqueuePaths`. The staff button of
  #195 is deleted: it started nothing the sweep does not, and a
  whole-volume re-run over an edited volume belongs to #224. "Next:
  Detect" only walks to step 2 when detections exist (#196), and
  otherwise says where the run stands (`views_process.detection_message`,
  shared by the flash and the button title). The run is usually merged
  during review 1 (the glue reads the rows, not the status) and waits
  there at no cost; `queue_ready_runs` takes it on the tick after the
  approval, because the apply reads `bitonal.pdf` and moves the scan.
- **Each RunPod engine is its own endpoint, and `jobs.RunpodEngine` is
  where that lives.** dots.mocr's endpoint id, concurrency cap, attempt
  cap and per-page allowance used to be read by name for every RunPod
  row, so a detection row would silently have taken them. The table
  holds settings **by name** and reads them with `getattr` at call
  time, or `override_settings` would not reach them, and it is rebuilt
  per call so a patched `enabled` reaches it too. A row naming an
  engine with no entry raises `UnknownRunpodEngine`, a `RunpodError`
  subclass, so the poll and the cancel already handle it the way they
  handle an unconfigured endpoint: one warning, every other row on the
  tick still judged. **This is not a provider layer** — see the
  `jobs.py` docstring; a second engine on one provider is a payload
  builder, an endpoint id and a cap, and that is all the table holds.
- **`RUNPOD_YOLO_ENDPOINT_ID` names the endpoint the deleted
  `RUNPOD_ENDPOINT_ID` named.** #194 pushed the rebuilt image to the
  same Docker Hub repository and PATCHed the same RunPod template, so
  a deploy copies the old value under the new name and creates
  nothing. Blank turns detection off and leaves dots.mocr running.
- **Detection reads the original shards, never the bitonal copies.**
  bl-warm was trained on greyscale renders and its large region classes
  collapse on 1-bit pages (caption F1 0.99 -> 0.25, measured in #167).
  Same as dots.mocr, so both fan out over the one shard set of #164 and
  neither cuts its own.
- **The rows stop at `COMPLETED` until the merge takes them.** The
  merge is #196, below: it offsets each `page_index` by its shard's own
  `from_page` (the worker counts from zero inside its shard) and flips
  the rows to `CONSUMED`. **Nothing may delete a detect result** — a
  re-read must never cost a paid run, which is also why
  `ensure_detect_jobs` passes `reuse_results=True` while the bitonal
  stage, whose merge deletes its results, must not.
- **A pending run holds nothing else up.** Intake has no cap and a row
  waiting in our own queue carries no clock (#218): detect rows drain
  at their engine's own concurrency limit, and the other engines count
  only their own rows against theirs.
- **Only the admin scan deletion may cancel a detect run.**
  `abandon_open` is scoped by stage everywhere else and every caller
  passes `CONVERT`; an unscoped re-queue would cancel a finished run
  and make the next press pay for output already in S3.
- `MODELS = ["bl_warm"]` and `CONFIDENCE = 0.20` are module constants,
  like the dots.mocr `DPI`: no operational reason to retune per deploy,
  and `input_manifest` already carries a per-row override for a one-off
  experiment. Only `bl_warm.pt` is baked, so another name would reach
  Hugging Face from inside a paid job; the worker refuses it up front.
  The payload sends **no** `dpi` (blackletter fixes 200, matching
  `DOCTOR_BITONAL_DPI`) and no `max_pages` (the worker has its own
  ceiling, and a partial detection merged as a whole volume is worse
  than a failure).
- `JobEngine.BLACKLETTER` keeps its value and lost its PaddleOCR label,
  which went with the legacy pipeline (#173). The engine names the
  library, not the checkpoint: the weights are a worker input, so a
  later checkpoint is a payload change and not a new engine.
- `YOLO_SECONDS_PER_PAGE = 2.0` is a first guess, deliberately
  generous. #211 replaces it with a measured value.
  `YOLO_MAX_CONCURRENCY` is 3 since #250; the endpoint's own
  `max_workers` must be at least that, or the extra rows wait in the
  provider's queue with the ceiling clock running.

## Redactions from the detection run (issue #196)

What reads the #195 output, and what re-opened review 2. Two steps,
and the split between them is the whole design: `yolo.finish_ready_runs`
merges the run on the collect tick, and
`services.run_compute_redactions` measures the geometry as queued work.
The pieces: the merge half of `yolo.py`,
`services.run_compute_redactions` / `_import_detections` /
`queue_redaction_compute`, `QueuedAction.COMPUTE_REDACTIONS`, and the
two review-2 endpoints in `views_api.py`. What must not be broken:

- **The apply is queued work, and the collect tick only triggers it.**
  Three of its steps read the page ink, so they render every page of
  the volume: 83 seconds for 1364 pages, measured, plus the pull of
  `bitonal.pdf`. The tick runs every 15 seconds on a serial scheduler
  (#156), so a pass that measured inline would stop every submit and
  every poll for minutes. `yolo.queue_ready_runs` writes one status and
  returns; `process_next_scan` runs the work. This is the one place
  where the shape differs from the page-number apply (#204), which
  stays off the queue because it is seconds of work over a JSON file.
- **The apply never writes ERROR.** It parks the scan back in
  `PAGE_COMPLETENESS_REVIEW_DONE` on every path
  (`_park_after_redactions`, guarded on the busy statuses) and raises
  nothing, so the generic ERROR arm in `process_next_scan` never sees
  it. An ERROR on an approved volume needs an admin re-queue, and that
  re-queue runs the whole pipeline again. Failures are counted on the
  run instead (`provider_meta["apply"]`, `APPLY_MAX_ATTEMPTS`), the
  same loud-then-quiet ledger the glue uses. `record_apply_failure`
  returns whether that failure spent the last attempt, and the park
  message follows it: "runs again by itself" only while the trigger
  will, and "stopped, ask a staff member" at the crossing.
- **`queued_at` is the claim, `applied_at` is the stamp.** The trigger
  writes `queued_at` so it does not queue the same scan on every tick;
  the work drops it as it starts, so a failed apply is queued again.
  `applied_at` closes the run for good.
- **Only `PAGE_COMPLETENESS_REVIEW_DONE` is taken**, with a
  compare-and-swap, and the staff button refuses every other status for
  the same reason: review 2 follows review 1, and a run started earlier
  would be paid for and never read.
- **The detections are imported once per run.** A run already stamped
  is a *recompute*, which a curator asks for after they edit a box: it
  keeps every row in the database and measures again from those.
  Importing again there would throw the curator's edits away, which is
  the whole reason they pressed the button. A first import keeps the
  `MANUAL` rows and replaces every other one, and a volume with no
  detect rows at all (the legacy pipeline wrote its detections)
  measures what the database holds.
- **The model read the original; the geometry reads the bitonal copy.**
  bl-warm collapses on 1-bit pages, so detection fans out over the
  original shards (#167/#194), while the rects are stamped on the
  bitonal copy and must be measured against its ink (PR #167). Both
  files carry the page geometry of the original.
- **`found_by` is load-bearing, in three places.** The confidence gates
  are per model family since blackletter #73
  (`label_confidence(label, document.bl_warm)`), so the provenance has
  to survive the merge, the `Detection` row and `detections.json` —
  `blackletter.api.pair` reads `rows_are_bl_warm` off that *file*. On
  one volume of 1364 pages the wrong family keeps 13 editorial notes
  bl-warm drops and loses 8 header boxes it keeps. A hand-added box
  carries no `found_by` on purpose: one would read as a second family
  and send the whole volume back to the legacy gates. `add_single_detection`
  used to write a `manual` claim there, so the two collectors
  (`_sync_detections_to_disk`, `_detections_for_geometry`) copy the
  field off non-`MANUAL` rows only — the row kind is the guard, because
  the rows written before the fix are still in the database and a
  re-import keeps them.
- **The per-shard results are kept.** Issue #196 asks for it: a page
  insert or a replacement recomputes the merge from them. The bitonal
  merge deletes its results, and this stage must not copy that.
- **The merged document carries `Scan.source_fingerprint`**, and the
  apply refuses one from another original. The shard identity ties the
  *rows* to today's bytes, but the document outlives its run. A blank
  on either side matches anything, the rule the page edits use (#214).
- **The merge checks less than the dots.mocr glue, and has to.** That
  payload lists every page, so a lost page is visible; this one lists
  detections, and a page with none reads exactly like a page nobody
  looked at. What is checkable is checked: the shard sequence, the
  shard's own page count as the worker reported it, every page index
  inside its shard, and one model family for the whole volume.
- **Nothing a curator does starts a measurement, for now.** The
  step-2 template used to fire `compute-redactions` on load whenever
  the rects were missing; the add path in `viewer_step2.js` and the
  approve path in `viewer_sidebar.js` used to re-pair after every box.
  All three are gone: a volume whose apply failed would have started a
  volume-wide render on every reload, and a curator approving five
  captions would have taken the volume out of review five times. The
  edits show a toast that says the redactions are not recomputed from
  them yet. The "Re-pair Opinions" button is commented out and the two
  review-2 endpoints answer 409 behind
  `views_api.REPAIR_ON_REQUEST_ENABLED = False`: until the stage has
  been watched on a few volumes (#211), the daemon's one run after a
  detection run is the only computation wanted. Turning it back on is
  the flag plus the button; the queueing code behind them is kept, and
  `viewer_progress.js` already reloads the page when the scan parks.
  The button recomputes the pairing, the rects and the strips together,
  which is right, since all three are measured from the same detections.
- **"Next: Detect" carries no paid confirm when there are no
  detections.** `start_detect` starts nothing since #195: it walks to
  step 2 when detections exist and otherwise flashes
  `detection_message(yolo.run_summary(scan))` -- running, failed,
  finished-and-waiting, or `NO_DETECTIONS_MESSAGE` for a volume with
  no run. The button's confirm used to name RunPod and a cost for a
  run the view then did not start; the no-detections branch now says
  what the view says. Nothing in the viewer pays since #250: the
  daemon starts the one run.

## The glued outputs, by scan id (issue #243)

Three routes under `scans/<pk>/glued/<output>/`, for `dots-mocr` and
`yolo`, so a developer needs no shell on the daemon pod to read a
run. `views_process.GLUED_OUTPUTS` maps the slug to the stage, the
engine and the glued key function, and that table is the whole
difference between the two outputs: a third engine is one entry.

- **The index reads the rows only.** `glued_output_index` lists every
  run newest first with its shards, their 1-based volume page ranges
  (the manifest holds fitz indexes), their states, the dots.mocr page
  lists (shard-local, as the worker reports them), and the URL of
  each file. No S3 call, so it answers in every environment, and a
  scan nothing read gets `runs: []`, not an error. `glued` is "every
  row CONSUMED"; the volume route still checks the object.
- **The file routes redirect, never stream.** `serve_glued_volume`
  presigns the run's glued key and `serve_glued_shard` the row's own
  `result_key` (the worker's answer, `raw` included, which the glue
  leaves out). A glued document of a long volume is tens of MB, and
  #185 already took the large stream out of the preview endpoint.
  The presign carries `Content-Disposition: attachment` so the
  browser saves a named file.
- **One HEAD before the redirect**, or a run that is not glued yet
  would send the browser to an S3 XML error. A non-missing S3 error
  propagates. Without S3 the file routes answer 404: the glue returns
  before any work when S3 is off, and the workers write into the
  bucket.
- `@login_required` like `/pdf/`; the "files" link in the step-1 bar
  is staff-only. `GLUED_OUTPUT_PRESIGN_TTL` is ten minutes, one
  download; `ORIGINAL_VIEW_PRESIGN_TTL` serves a viewer that scrolls
  for hours and is the wrong size.

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
  (`bitonal.MERGE_TMP_PREFIX`, `dots_mocr.GLUE_TMP_PREFIX`,
  `yolo.MERGE_TMP_PREFIX`) that a
  SIGKILL orphans in the system temp dir. Off under TESTING; the
  command's tests point `gettempdir` at a scratch root.

## Uncapped intake, and where the queue clock starts (issue #218)

Intake has no cap: `process_next_scan` claims every QUEUED scan in
order, and the per-engine concurrency limits pace the providers. What
makes that safe is where the queue ceiling starts. A job row waiting in
**our own** queue (PENDING, never claimed) carries no deadline; the
6-hour `DAEMON_JOB_MAX_QUEUE_SECONDS` ceiling is stamped at the
attempt's first claim, when the row is handed to the provider.

- **The clock placement is the whole fix.** The ceiling used to start
  at row creation, so on 2026-08-31 an uncapped intake put 2023
  conversion rows behind 27 parked scans — three times what 6h can
  drain — and 29 volumes died of `QUEUE_TIMEOUT`, unsubmitted. A first
  patch capped intake at `DAEMON_MAX_ACTIVE_SCANS` (5) scans, which
  then held 532 queued scans behind five slots that slow dots.mocr rows
  occupied for hours. Both the cap and the creation-time stamp are
  gone. Do not restore one without the other.
- **A backlog in our queue is not a fault, however long.** Rows drain
  in creation order at each engine's concurrency limit
  (`DOCTOR_MAX_CONCURRENCY`, `DOTS_MOCR_MAX_CONCURRENCY`), and nothing
  expires while they wait. The trade, accepted: an engine switched off
  with PENDING rows now parks its scans until it returns or an admin
  re-queues them, instead of erroring them at 6h.
- **The stranded sweep still exists** for the rows that carry a
  ceiling: a deferred row an endpoint kept declining past it (and rows
  stamped before the move). `sweep_jobs` fails those `QUEUE_TIMEOUT`,
  terminally — a paused-for-good endpoint must still end its scans.
- **Only the provider's queue is bounded**, and the submit wave keeps
  it shallow: at most the concurrency cap is in flight, so a healthy
  endpoint's queue wait sits far under the ceiling.

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
