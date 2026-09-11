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

# Write the review-2 findings of the volumes already in review 2, once after a deploy (#240 PR D)
docker exec scanning-daemon python manage.py rebuild_review2_findings

# Fit the standing text redaction boxes to the read text, once after a deploy (#279)
docker exec scanning-daemon python manage.py refit_text_redactions --dry-run

# Generate migrations
DEVELOPMENT=True DB_HOST=localhost DB_SSL_MODE=prefer python manage.py makemigrations scanning

# Start dev environment
docker compose -f docker/scanning/docker-compose.yml up --build

# Install dependencies
uv sync --all-extras
```

## This file

This file holds what the code and the git history cannot tell a reader: the commands, the conventions, and the invariants a later change could break in silence. Every session loads it, so every line has a cost.

- A PR adds a line here only when it creates a new invariant or changes one listed here. One line per rule, with the issue number. No section per feature.
- The design rationale goes in the PR description, the issue, or a docstring next to the code it protects.
- Delete a rule that no longer holds. Do not annotate it.

## Project Structure

- `scanning/` is both the Django project (settings, urls, asgi, wsgi) and the single app (models, views, forms, admin)
- Settings are split into modules: `settings/django.py`, `settings/project/`, `settings/third_party/`
- Templates: `scanning/assets/templates/` for the base layout, cotton components and error pages; `scanning/templates/scanning/` for app templates
- Worker images: `scanning/runpod/` (YOLO, `bl_warm`), `scanning/runpod-dotsmocr/`, `scanning/runpod-caselaw-tagger/`

## Testing

- Use `django.test.TestCase`, not pytest-style classes
- Tests live in `scanning/tests/`, one module per area. Shared synthetic-PDF builders are in `scanning/tests/pdf_fixtures.py`
- Test classes inherit from `ScanningTestCase` (`make_user()`, `make_staff_user()`, `make_pdf()`, `make_image()`)
- Use `ScanFactory` and `UserFactory` from `scanning/factories.py`. Factory docstrings describe defaults as prose, not `:param:` entries. Use `skip_postgeneration_save = True` and save changed fields in `@factory.post_generation` hooks
- Handler tests load the worker modules without the worker stack (`test_runpod_*_handler.py`)

## Views

- All views are function-based with `@login_required`. Auth views wrap Django's `LoginView`/`LogoutView`
- Never pass `next` query strings into templates (open redirect). Let the auth views validate the redirect
- All authenticated users see all scans. Staff-only is the review form on the detail page, the "files" links and the reopen button
- A gate lives in the view, not only in the template: `start_detect`, `approve_page_completeness`, `generate_files` refuse a direct POST
- A refused request answers `{status: "error", message}` (409 for a rule, 404 for an address outside the volume), and the viewer shows the message and changes nothing

## Pipeline

State is `Scan.status`. The stages, and where each runs:

1. `run_full_pipeline` (daemon, `process_next_scan`): shards the original (#164), sets `page_count`, creates the CONVERT rows (doctor bitonal, #176) and the ANALYZE rows (dots.mocr on RunPod, #190/#207), then parks the scan in AWAITING or AWAITING_VALIDATION.
2. Daemon ticks (`submit_external_jobs`, `collect_external_jobs`, serial scheduler, #156). The submit tick starts one detection run per shard set (`yolo.enqueue_missing_runs`, #250). The collect tick merges the bitonal shards, glues the dots.mocr run, applies the page numbers (`run_compute_issues`, #204), triggers the apply (#224), merges the detection run and queues the redaction compute (#196), and promotes the review states (#263).
3. Review 1: READY_FOR_PAGE_COMPLETENESS_REVIEW, then PAGE_COMPLETENESS_REVIEW_DONE (`approve_page_completeness`, #151/#154).
4. The apply (#224): queued work (`APPLY_PAGE_EDITS`) that builds the corrected volume from the `PageEdit` rows under `jobs/apply/a{n}/`.
5. The redaction compute (#196): queued work (`COMPUTE_REDACTIONS`) that renders every page; parks in READY_FOR_REDACTION_REVIEW, then REDACTION_REVIEW_DONE (`approve_redaction_review`, #263).
6. Step 3, the file generation, is paused (#173/#206). `start_validate`, `reprocess` and `generate_files` refuse with `utils.PIPELINE_PAUSED_MESSAGE`; nothing queues `run_generate_files`.

- A legacy row (before #173) holds `PENDING_REVIEW` for both reviews, never enters the #154/#263 states, gets no apply and keeps the old buttons. `legacy_review` reads the status; `has_legacy_ocr` asks who read the page numbers. They are different questions
- `RUNPOD_ENABLED` gates only whether GPU jobs dispatch. Upload paths must work without it
- Work that takes seconds over a JSON file runs on the collect tick (the page-number apply, the glues); work that pulls a PDF or renders pages is queued (the apply, the redaction compute)
- `process_next_scan` claims by action before age (`CLAIM_PRIORITY`: full pipeline, redaction compute, apply), and an apply waiting `CLAIM_LIFT_SECONDS` is claimed next. The external job wave ranks an apply row before the volume rows (`jobs._pending_slice`, #291). Rank the row, not the status

## Status writes

- Every status write is a compare-and-swap over the current status, never a full `save()`. A second writer is always live: the collect tick, a second tab, the daemon shutdown handler
- The four review statuses (`models.REVIEW_STATUSES`) and AWAITING are not `BUSY_STATUSES`: no polling, no stale sweep. Only PROCESSING is swept
- ERROR is terminal; the way back is the admin re-queue. `run_compute_redactions` and `run_apply_page_edits` never write ERROR; their failures count on the run (`provider_meta["apply"]`, `ApplyRun.attempts`), loud then quiet
- The dots.mocr and detection stages write no scan status while they read. The bitonal stage alone owns AWAITING
- A derived state has one rule function that every writer and every reader calls: `review_states.redaction_review_ready`, `review_states.final_run`, `yolo.redactions_current`, `_review_flags`, `repairs.has_waiting`, `boundaries.standing`. Never write a second copy
- There is no user cancel (#219). `Status.CANCELLED` has no writer. A replacement goes on `jobs.abandon_open`, with the status left to the daemon
- A step chooser and a button never send a user to a step whose only action refuses

## External jobs (`jobs.py`)

- Row writes are compare-and-swaps (`jobs._write`); no lock is held across an HTTP call. The rival writer is the web process (`abandon_open` from the re-queue, the deletion, `start_dots_mocr`)
- Mark the row SUBMITTED before the request. Doctor is sync: a lost answer is recovered with an S3 HEAD on `result_key` (`sweep_jobs`), never by a resubmit. `result_key` is scoped to run, shard and attempt
- Intake has no cap, and the queue ceiling (`DAEMON_JOB_MAX_QUEUE_SECONDS`) is stamped at the attempt's first claim, not at row creation (#218). Never restore one without the other. The `IN_PROGRESS` crossing replaces it with the execution deadline; a defer (409/429) costs no attempt and moves no deadline
- `abandon_open` is scoped by stage and every caller passes CONVERT. COMPLETED is in `OPEN_JOB_STATUSES`, so an unscoped call cancels paid results. Only the admin deletion cancels a detect run
- A RunPod job nothing will read is cancelled (`_cancel_provider_job`), including a job whose id arrives after the row was cancelled
- Results go to S3 through a presigned PUT signed with the content type (`application/json`, `application/pdf`), never inline. A summary carries no `pages`
- `poll_once` never raises and never sleeps; `status=None` means "learned nothing". A 404 from `/status` asks S3 before it writes the job off
- The RunPod wave goes before doctor's wave (doctor holds the socket). The concurrency cap is per engine (`jobs.RunpodEngine`, settings read by name with `getattr`)
- The bitonal merge deletes its results; dots.mocr and detection keep theirs and pass `reuse_results=True`. Never delete an analyze or detect result. The row identity carries `size_bytes`
- Run reuse compares page ranges, not shard keys. `_still_describes` compares `input_manifest` exactly, so a run-scoped counter goes in `provider_meta`, never in `input_manifest`
- A stable hole (`jobs.hole_is_stable`) is carried; `reread_failed_pages` re-pays only the shards with unstable holes. `repaired_pages` is a page list, and a repaired page is not a filtered one (#242)
- The job creators are pinned by an AST test (`TestKnownEnqueuePaths`): the pipeline, `start_dots_mocr`, `yolo.ensure_detect_jobs` (through the sweep and `enqueue_yolo_detect`), `apply.py`, `reread_failed_pages`. Row creation is what costs GPU money
- No provider abstraction, on purpose: branch on `job.provider`. Mistral (#191, switched off) is where the branches get promoted
- Do not add a pass that revives FAILED rows: the admin re-queue changes the status in a second write, and a reviver races it
- A failure names the volume page range (`jobs._failure_location`). Page numbers are logged 1-based; `from_page`/`to_page` are fitz indexes

## Sharding (#164)

- `sharding.ensure_shards` is the only way in; never read `shards/` directly. It is idempotent on the fingerprint (size plus page count). `SHARD_TARGET_BYTES` and `SHARD_MAX_PAGES` are not in it; a corpus re-cut needs a `MANIFEST_VERSION` bump
- The manifest is uploaded last. Only a missing manifest reads as "no shard set"; every other S3 error re-raises
- `shards/` and `jobs/` are excluded from the generic S3 sync both ways. The admin deletion cancels the jobs first, then sweeps both prefixes
- Both GPU stages read the original shards, never the bitonal copies. A worker counts pages from zero inside its shard; the glue offsets by `from_page`
- `committed_manifest` verifies with one HEAD on the original. A web pod never pulls the original

## The original never changes

Every address is a 1-based physical page of the original as uploaded: `PageEdit.pdf_page` and `anchor_pdf_page` (0 = before page 1), `PageRepairRequest`, `Detection` in the original's space, and the `original` entries of the apply's page map. `Scan.source_fingerprint` is stamped by `ensure_shards` and copied onto every row that addresses the original; a mismatch is a `stale_*` finding, never a silent apply; a blank fingerprint matches anything. The apply writes another file and touches neither the original nor the review-1 artifacts. `applied_at` and the fingerprint are independent questions.

## Review 1

- Approving is a compare-and-swap on READY, open to every logged-in user. Open issues do not block it; a waiting repair request does (#266). The approval gates "Next: Detect" in the view
- A new-pipeline volume is never re-run from the viewer; `start_validate` refuses it for good. A re-run is the admin re-queue
- `PageEdit` (#214): one standing row per address, partial unique keys over `withdrawn_at IS NULL`. Write through `page_edits.supersede`; undo through `page_edits.withdraw`. Nothing is deleted. `applied_at` is a ledger, not a close. Acting readers go through `current_edits`, never `standing_edits`
- `has_pending_changes` counts the structural kinds the standing apply run has not built, not the `applied_at` stamp alone
- The file is stored before the row and removed if the row loses (`_save_page_file_row`). The first bytes decide the kind (`_accept_page_upload`). The cap is `settings.PAGE_UPLOAD_MAX_BYTES` (a sixth of `MAX_ORIGINAL_UPLOAD_SIZE`, or `PAGE_UPLOAD_MAX_MB`), and the page count is read off the temporary file, not from memory. Images live on the default storage under `page_edits/`
- `Scan.ocr_results` is a cache, rebuilt from the glued run plus the rows. The `"manual"` stamp is derived
- DONE and every later status lock the eight page-edit endpoints (`_refuse_locked_edits`, `LOCKED_STATUSES`, 409 with `EDITS_LOCKED_MESSAGE`); `dismiss_issue` is not locked. The viewer follows `page_edits_locked`. The reopen is a staff button: DONE to READY by compare-and-swap, and it supersedes the run
- `PageRepairRequest` (#249) is not a `PageEdit`. Fulfilled is derived (`annotate_fulfilled`: a later, standing insert or replace at the same address under the same fingerprint), never stamped. Dismissed, never deleted. A stale request still waits (#266), unlike a stale `PageEdit`. `repairs.py` owns the one definition of "waiting"
- Page numbers (`page_numbers.py`, #228/#233): the rank is geometric (band, score, corner distance, line); `_resolve_by_neighbours` moves a pick only when both neighbours agree; `_range_value` guards every range; curator input stores one hyphen. After a reading change, run `reapply_page_numbers`
- `_project_trailing_gap` (#256) puts one range placeholder on a collapsed missing run at the end of the volume only, from both `page_map` builders
- A deletion answers the cards of the page it names (`CHECKS_A_DELETION_ANSWERS`, #255), never a `duplicate_page` or `missing_page` card
- Every page label is narrowed (`_page_label`) and escaped (`escapeHtml`). A note is escaped only
- Step-1 buttons bind by delegation on the container, and `refreshSavedLabel` runs after a note changes on a live page

## The apply (#224, #269)

- Two phases as queued work (`phase_due`: build, then glue). The scan stays DONE between them and is parked back guarded on PROCESSING. At most `MAX_SCANS_IN_FLIGHT` scans are out at once
- `plan_run` never opens the original. An identity run aliases the volume artifacts and writes only `printed_pages.json`
- The page map is written once (`page_map.json`, and on the run); every glue reads it. Page shards are keyed by edit (`jobs/apply/pages/e{pk}.pdf`), never by run, so `a{n+1}` reuses paid results. `supersede_runs` cancels only unstarted rows
- An apply row carries `apply_run` and no `source_fingerprint`; every volume query filters `apply_run__isnull=True`
- A closed stage gate (`gates_closed`) holds the scan out of the queue and spends no attempt. Each glue is judged on the rows of its own stage (`glues_due`), so a slow detection worker holds only the detections glue
- A dead row is noted once (`dead_row_noted_at`), and review 2 never opens until an operator supersedes the run
- `review_states.final_run(scan)` is the one predicate for "the corrected volume exists"; `yolo.redactions_current(rows, run)` for "measured against it". Step 2 reads the final space only when both hold, else the review-1 copy with a note. A final PDF under boxes of another space is the one thing the page must never show
- `apply.local_copy` mirrors one key under `output_dir`; never the whole-prefix pull. `geometry_pdf_path` is the one rule for which PDF the geometry reads
- The OCR glue repairs the one-page reads too (`_repair_edit_pages`). Run `reglue_dots_mocr` before `reread_failed_pages`
- `export_pdf` runs the apply's own walk (`build_final_pdf`) and answers an `ApplyError` with 409

## Review 2 (#195, #196, #240, #263)

- The daemon starts one detection run per `Scan.source_fingerprint` (`yolo.enqueue_missing_runs`, at most `YOLO_MAX_CONCURRENCY` scans per tick). A dead run is re-run only by `enqueue_yolo_detect`. The merge runs on the collect tick; the compute is queued
- `found_by` must survive the merge, the `Detection` row and `services.detection_entries`: it picks the confidence gates. A hand-drawn row carries none
- Model rows are disposable and rebuilt at every import or compute; human rows are withdrawn, never deleted: MANUAL `Detection`, `DetectionDecision`, `Redaction` add and dismiss, `OpinionBoundary` add and dismiss. A second withdrawal is a no-op
- A decision names its target by address plus a copy of the box, never by a write on the model row. `resolve` lands it on the rebuilt row by IoU at least `IOU_THRESHOLD` (a boundary: the start point within `ANCHOR_TOLERANCE_PT`), each row taken once, and reads no model row when no decision stands. An unresolved decision is logged and left standing
- The address is (`source_edit`, `source_page`); `page_index` plus `apply_run` is the position in the imported space. `detections.relocate_rows` moves the human rows through the page map after a compute under a run. A row with no address is refused (409, `*_UNADDRESSABLE_MESSAGE`)
- A move is a dismiss plus an add that names it in `replaces`; the endpoint answers the id that holds the box now, and the viewer follows it. `undo_move` alone gives a computed box back; `withdraw` cascades to nothing. A second move replaces the addition, not the computed row
- `Redaction` rows are in PDF points; `Detection` rows in 200-dpi pixels. Every output of the compute is a row: `detections.json`, `redaction_rects`, `margin_rects` and `opinions_json` are gone
- The compute pairs once (`_snapped_document`); `bl_pair` is not imported. A recompute keeps every row; only a first import under a run replaces the model rows
- Nothing a curator does starts a compute (`REPAIR_ON_REQUEST_ENABLED = False`). `REDACTION_REVIEW_DONE` is recomputed by nothing but the re-queue
- `approve_redaction_review` is the only writer of REDACTION_REVIEW_DONE, and its log line is the only record of who decided. It gates step 3; a legacy volume keeps its link
- The findings of review 2 are `Issue` rows (`REVIEW2_CHECKS`, `Issue.target`), and `findings.rebuild` is their one writer (#240 PR D). It derives every finding from the detection, boundary and redaction rows, with no S3 read and no render, and runs at the end of the compute (which passes the run it measured in, because its ledger stamp is written after the park) and in every review-2 write endpoint (`_rebuild_findings`), since the recompute is off. `recalculate_issues` excludes `REVIEW2_CHECKS`
- Stale is read off the rows, never threaded from `resolve`: a standing decision no `decision` FK points at, or a human row whose `apply_run` is not the measured run, is a `stale_*` card. No measured finding is written without a computed boundary
- `ReviewDismissal` names its target by address plus a copy of the box; `findings.resolve` lands it by `IOU_THRESHOLD` and the finding is written with `Issue.dismissal` set (muted, with Undo). A stale card is withdrawn (`withdraw_stale`), never dismissed (`UndismissableFinding`, 409). Nothing deletes a dismissal
- A review-2 `page_number` is the 1-based position in the space the rows are drawn in, so step 1 and `process_actions` list `scan.issues.exclude(check_name__in=REVIEW2_CHECKS)`, and `dismiss_issue` refuses a review-2 row. The page and the `review_findings` fragment render `_review_findings.html` from `findings.viewer_groups`
- The review-2 approval warns and obeys: `_review_flags` carries `review2_open` and `review2_stale`, the bar asks for a confirm, the view blocks nothing
- No migration writes a finding: `rebuild_review2_findings` writes them once after the deploy. `flag_issue`, `remove_flag` and the three user-action checks are gone
- A text redaction box is fitted to the dots.mocr cells under it (`text_fit.fit_span`, #279): the horizontal limits only, never wider, only a `TEXT_RECT_TYPES` box, and only on a page whose cells were read. The vertical limits stay on the ink. `text_fit.load_document` is the one rule for which OCR document any reader of the cells reads, the twin of `geometry_pdf_path`
- The fit leaves a computed row a standing dismiss points at alone (`refit_text_redactions`, #279), so every decision keeps its IoU on the box it named. Nothing sets `Page.col_*` or `midpoint`, so blackletter measures every box from the fallback 50/50 split

## Worker images

- The scaffold is in `runpod_common`, once: Sentry, the result envelope, `execute_action`. Only `BadInputError` maps to `BAD_INPUT`. Without `result_url` a worker answers inline
- YOLO: only `bl_warm.pt` is baked, and the handler passes no `imgsz` (the checkpoint carries 1024). No CUDA base layer
- dots.mocr: `DPI = 200` and `PROMPT_MODE` are module constants; `HANDLER_MAX_COMPLETION_TOKENS = 6144`; the retry ladder changes only the render (deterministic); the layout repair (`layout_json.py`, no Django import, copied into the image) runs before the ladder; `raw` is never written over
- Tagger: GPU-only (`HANDLER_ALLOW_CPU=1` for a laptop only), transformers 5, model baked and opened at build time
- One build workflow per image, and each PATCHes its own template id

## Files, disk and pages

- `serve_scan_pdf` never streams the original (#185): 202 or 409 with `original_available`. The viewer reads the original through a presigned GET (`scan_original_url`), which needs the bucket CORS rule. A URL a `fetch` reader consumes comes as JSON; a developer's route redirects (#243/#262)
- `release_local_processing` frees `/tmp/scanning/{pk}` after a successful push, on the exit from AWAITING when the park won, on a terminal failure, and at the end of the two queued workers. Never on a re-queue (#215). `cleanup_processing_tmp` judges by `_tree_mtime` and sweeps the `*_TMP_PREFIX` scratch dirs
- `/stats/` (#260): `STATUS_GROUPS` covers every `Status` value once, test-pinned. A legacy status counts in `LEGACY_STATUSES` only. When #206 lands, move APPROVED into `REDACTION_REVIEW_COMPLETE_STATUSES`
- A list page costs one query per page for its badges (`repairs.waiting_counts` over the page's ids, after the pagination)

## Tailwind CSS

- Config: `scanning/assets/tailwind/tailwind.config.js`; input: `scanning/assets/tailwind/input.css`; output: `scanning/assets/static-global/css/tailwind_styles.css` (gitignored, built by npm)
- Component classes in `input.css`: `.btn-primary`, `.btn-outline`, `.btn-danger`, `.btn-ghost`, `.card`, `.input-text`, `.alert-*`, `.badge-*`
- Templates use cotton components: `<c-header />`, `<c-footer />`

## Environment

- `DEVELOPMENT=True` enables the debug toolbar, local filesystem storage and the dev S3 buckets
- `TESTING=True` is auto-detected from `sys.argv`: LocMemCache, MD5 password hasher, no debug toolbar URLs
- `DB_SSL_MODE=prefer` is needed outside Docker
- `DOCTOR_ENABLED` and `DOCTOR_HOST` default to working values; `RUNPOD_YOLO_ENDPOINT_ID` blank turns detection off and leaves dots.mocr on
