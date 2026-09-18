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

# Glue the Mistral reads again after a transform change (#245)
docker exec scanning-daemon python manage.py reglue_mistral_ocr --dry-run

# Glue the Surya reads again after a transform change (#368)
docker exec scanning-daemon python manage.py reglue_surya_ocr --dry-run

# Write the review-2 findings of the volumes already in review 2, once after a deploy (#240 PR D)
docker exec scanning-daemon python manage.py rebuild_review2_findings

# Write the headnote bracket readings of the volumes already computed (#328)
docker exec scanning-daemon python manage.py stamp_bracket_readings --dry-run

# Write the OCR documents of a volume's opinions again, after a late engine read or a transform change (#350)
docker exec scanning-daemon python manage.py reglue_opinion_ocr 2845 --dry-run

# Fit the standing text redaction boxes to the read text, once after a deploy (#279)
docker exec scanning-daemon python manage.py refit_text_redactions --dry-run

# Write the text of a volume's opinions again, after a transform change or for a two-engine volume (#365)
docker exec scanning-daemon python manage.py rerun_opinion_ensemble 2845 --dry-run

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
- Worker images: `scanning/runpod/` (YOLO, `bl_warm`), `scanning/runpod-dotsmocr/`, `scanning/runpod-surya/`, `scanning/runpod-caselaw-tagger/`

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
2. Daemon ticks (`submit_external_jobs`, `collect_external_jobs`, serial scheduler, #156). The submit tick starts one detection run per shard set (`yolo.enqueue_missing_runs`, #250). The collect tick merges the bitonal shards, glues the dots.mocr run, applies the page numbers (`run_compute_issues`, #204), triggers the apply (#224), merges the detection run and queues the redaction compute (#196), and promotes the review states (#263). `build_opinion_pdfs` is the fifth daemon task, last in the schedule, and writes one opinion PDF per tick (#336).
3. Review 1: READY_FOR_PAGE_COMPLETENESS_REVIEW, then PAGE_COMPLETENESS_REVIEW_DONE (`approve_page_completeness`, #151/#154).
4. The apply (#224): queued work (`APPLY_PAGE_EDITS`) that builds the corrected volume from the `PageEdit` rows under `jobs/apply/a{n}/`.
5. The redaction compute (#196): queued work (`COMPUTE_REDACTIONS`) that renders every page; parks in READY_FOR_REDACTION_REVIEW. The approval (`approve_redaction_review`, #263) queues `CREATE_OPINIONS` (#336), whose worker writes the `Opinion` rows and parks in REDACTION_REVIEW_DONE.
6. Step 3, the file generation, is paused (#173/#206). `start_validate`, `reprocess` and `generate_files` refuse with `utils.PIPELINE_PAUSED_MESSAGE`; the volume-level generation code is deleted (#360), and `opinion_pdf` writes the per-opinion PDF.

- A legacy row (before #173) holds `PENDING_REVIEW` for both reviews, never enters the #154/#263 states, gets no apply and keeps the old buttons. `legacy_review` reads the status; `has_legacy_ocr` asks who read the page numbers. They are different questions
- `RUNPOD_ENABLED` gates only whether GPU jobs dispatch. Upload paths must work without it
- Work that takes seconds over a JSON file runs on the collect tick (the page-number apply, the glues); work that pulls a PDF or renders pages is queued (the apply, the redaction compute)
- `process_next_scan` claims by action before age (`CLAIM_PRIORITY`: full pipeline, redaction compute, apply), and an apply waiting `CLAIM_LIFT_SECONDS` is claimed next. The external job wave ranks an apply row before the volume rows (`jobs._pending_slice`, #291). Rank the row, not the status

## Status writes

- Every status write is a compare-and-swap over the current status, never a full `save()`. A second writer is always live: the collect tick, a second tab, the daemon shutdown handler
- The four review statuses (`models.REVIEW_STATUSES`) and AWAITING are not `BUSY_STATUSES`: no polling, no stale sweep. Only PROCESSING is swept
- ERROR is terminal; the way back is the admin re-queue. `run_compute_redactions`, `run_apply_page_edits` and `run_create_opinions` never write ERROR; the first two count their failures on the run (`provider_meta["apply"]`, `ApplyRun.attempts`), loud then quiet, and the third parks in READY_FOR_REDACTION_REVIEW with the reason, which the step-2 bar shows (#336)
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
- The job creators are pinned by an AST test (`TestKnownEnqueuePaths`): the pipeline, `start_dots_mocr`, `start_mistral_ocr`, `yolo.ensure_detect_jobs` (through the sweep and `enqueue_yolo_detect`), `apply.py`, `reread_failed_pages`. Row creation is what costs money
- One provider table and no deeper abstraction (`jobs.ProviderSpec`, #191): the wave, the sweep, the cancel, the caps, the deadline rules, the result suffix, whether a claim signs URLs. Insertion order is wave order (RunPod, doctor, Mistral). A fourth provider is an entry plus the three shared pieces (`claim_for_wave`, `apply_poll_outcome`, `count_sweep_outcome`), never a copied wave
- The Mistral read is over the original shards, the set dots.mocr and YOLO read (#191), and the boxes come off the text in the glue, never off the page. So a missed redaction, a moved boundary and a re-cut opinion split cost a re-glue and never a re-paid read
- The daemon renders the Mistral pages in the submit pass, 1700x2200 RGB, `pipeline/core/render.py` line for line (#191). Never grey, never bitonal: every bbox of the ensemble lives in that pixel space
- The Mistral wave takes one shard per tick (`MAX_SUBMITS_PER_TICK`), because its render and its uploads block the serial scheduler for minutes. `MAX_CONCURRENCY` is batches in flight, a separate number, and a row is polled at most once every `POLL_INTERVAL` (#191)
- The Mistral harvest stores every output and error line verbatim, plus the batch object; only `custom_id` is read, to name the holes. Our code writes `result_key`, nothing presigned, and every file at Mistral is deleted once the result is in S3 and on every path that writes a row off: the pages are unredacted, so that delete is the only limit on how long a third party holds them (#191)
- A Mistral result with a hole is never carried (`carry_stable_holes=False`, #191). The stable-hole rule of #238 trusts a deterministic decoder; a batch line fails from a transient fault
- `jobs.check_deadline` is the one rule for "this row has waited long enough", and a provider that skips a poll calls it itself. `apply_poll_outcome` returns inside its completion branch, so a finished batch is ended by `_harvest_outcome` alone: a transient fault waits, a missing output file retries, and the deadline ends a PUT that never lands (#191)
- `EXTRACT` takes either shape (`EITHER_LEVEL_STAGES`, migration 0032): a shard row with no opinion, or an opinion row for an engine that reads opinion PDFs. Only `TIEBREAK` still requires an opinion
- The Surya read is one more RunPod engine over the original shards (`EXTRACT`/`surya`, #364): an entry in `jobs._runpod_engines` plus `surya.py`, and never a second wave. Its holes are never carried (`carry_stable_holes=False`), because surya's own client reads a looped answer again at a temperature it raises itself. A Surya volume run has one creator, the staff button (`views_process.start_surya_ocr`); the collect pass creates the rows of a corrected volume's edited pages, and only for a scan whose volume run a person already started (#368)
- The Surya glues are the Mistral glues over one more engine (#368), and `surya.shard_pages` is the one transform of a stored result: both glues call it, it drops `raw` (which stays in the shard result), and a better transform is `reglue_surya_ocr` and never a re-paid read. The page lists are the worker's own and not `jobs.PAGE_LIST_NAMES`: `surya.PAGE_LISTS` is the one table of their names and their membership rule, which `views_process.SHARD_PAGE_LISTS` reads too, so `jobs.has_unread_pages` reads `failed_pages` alone and an empty page is carried
- `mistral_ocr.parse_payload` is the one transform of a stored Mistral result, and both glues call it (#245). A better transform is a re-glue (`reglue_mistral_ocr`) and never a re-paid read. The block text key is internal: the parse reads `text` or `content` and writes `content`. The block box is read from the four `top_left_*`/`bottom_right_*` keys, on the block or under `bbox`, and written as a list (#350)
- The Mistral glues run on the collect tick and never in `apply.glues_due` (#245): the apply trigger takes `PAGE_COMPLETENESS_REVIEW_DONE` alone, and the read starts later than that status
- A volume glue writes no copy of what the three stages share (#245): `jobs.volume_result_key` and `jobs.glued_volume_key` for the keys, `jobs.ready_volume_runs` and `jobs.consume_run` for the pass, `jobs.read_run_shards` and `jobs.shard_entry` for the walk and its page arithmetic, `jobs.run_ledger`/`bump_run_ledger` for the retry state (on the head row's `provider_meta`, never in `input_manifest`), `apply.walk_final_pages` for a corrected volume's page walk
- No review state reads `ApplyRun.extract_key` or `ApplyRun.surya_key`: `is_complete` and `final_volume_ready` do not change, or a volume nobody read with Mistral or with Surya would never open review 2 (#245, #368)
- The apply's EXTRACT rows are created by the collect pass, only for a scan whose live Mistral volume run is glued, and from `apply.stored_shard_manifest` so the carry matches the build's identity (#245)
- `mistral_ocr.apply_glue_due` is the one rule for writing a corrected volume's Mistral document, and `reglue_mistral_ocr` waives only its last test (#245). Every edit `apply.edit_page_counts` names must have a row first: a document written without them marks the edited pages unread and stamps a key that says the run is done
- `jobs.ready_apply_runs` is the one candidate rule for "a corrected volume owes an `EXTRACT` document" (#368), and `reglue_extract.ReglueExtractCommand` is the one walk of the two re-glue commands: each engine passes its label and its two `ApplyRun` field names, and neither writes a second copy
- `apply._note_dead_rows` counts a dead row of `GLUE_STAGES` alone (#245). A stage that blocks no glue must not spend the one `dead_row_noted_at` stamp, or the row that does stop the run is never logged
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
- A page with no page number blocks the approval too (`page_numbers.pages_without_number`, #342): the answers are a number, a number the curator cleared, a deletion or a dismissal of that page's card; a `suffixed` page carries a reading and never blocks. The gate reads the data and not the `Issue` rows, it and the repair gate are read in READY alone (the condition `_review_flags` reads), and each refusal flashes its own message
- A new-pipeline volume is never re-run from the viewer; `start_validate` refuses it for good. A re-run is the admin re-queue
- `PageEdit` (#214): one standing row per address, partial unique keys over `withdrawn_at IS NULL`. Write through `page_edits.supersede`; undo through `page_edits.withdraw`. Nothing is deleted. `applied_at` is a ledger, not a close. Acting readers go through `current_edits`, never `standing_edits`
- `has_pending_changes` counts the structural kinds the standing apply run has not built, not the `applied_at` stamp alone
- The file is stored before the row and removed if the row loses (`_save_page_file_row`). The first bytes decide the kind (`_accept_page_upload`). The cap is `settings.PAGE_UPLOAD_MAX_BYTES` (a sixth of `MAX_ORIGINAL_UPLOAD_SIZE`, or `PAGE_UPLOAD_MAX_MB`), and the page count is read off the temporary file, not from memory. Images live on the default storage under `page_edits/`
- `Scan.ocr_results` is a cache, rebuilt from the glued run plus the rows. The `"manual"` stamp is derived
- DONE and every later status lock the eight page-edit endpoints (`_refuse_locked_edits`, `LOCKED_STATUSES`, 409 with `EDITS_LOCKED_MESSAGE`); `dismiss_issue` is not locked. The viewer follows `page_edits_locked`. The reopen is a staff button: DONE to READY by compare-and-swap, and it supersedes the run
- `PageRepairRequest` (#249) is not a `PageEdit`. Fulfilled is derived (`annotate_fulfilled`: a later, standing insert or replace at the same address under the same fingerprint), never stamped. Dismissed, never deleted. A stale request still waits (#266), unlike a stale `PageEdit`. `repairs.py` owns the one definition of "waiting"
- Page numbers (`page_numbers.py`, #228/#233): the rank is geometric (band, score, corner distance, line); `_resolve_by_neighbours` moves a pick only when both neighbours agree; `_range_value` guards every range; curator input stores one hyphen. After a reading change, run `reapply_page_numbers`
- A number with a trailing letter (`2094a`, #319) is the third shape beside the number and the range: `page_numbers.number_type` is the one deriver of `type`, the plain number is tried before it (it owns the stray `L`), `_suffixed_value` guards the reading (two digits, and a letter of `SUFFIX_LETTERS`: the icon reads as an `l` or an `I`), and the page claims no number in the sequence, so it makes no sequence card and `printed_page_span` gives it no span; a model-read one gets a `suspicious_reading` card (`_ask_about_model_suffixes`), a curator's own none
- One page-number gate for the browser (`shared.isPageNumberEntry`, called by both viewers) and one for the server (`_page_number_value`, whose refusal is `PAGE_NUMBER_ERROR`), #319
- `_project_trailing_gap` (#256) puts one range placeholder on a collapsed missing run at the end of the volume only, from both `page_map` builders
- A deletion answers the cards of the page it names (`CHECKS_A_DELETION_ANSWERS`, #255), never a `duplicate_page` or `missing_page` card
- The unnumbered run before the first printed number is one `front_matter` card (`_ask_about_front_matter`), addressed by its first undeleted page, never a run later in the volume or a volume with no number at all; its button sends `pdf_pages` to `delete_page`, which checks every page before it writes one row
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
- The standing decision on a locked row is read by `detections.standing_decision`, in its own query, never through `select_related`: the lock gives the new row, but the join stays on the old snapshot (#339)
- The address is (`source_edit`, `source_page`); `page_index` plus `apply_run` is the position in the imported space. `detections.relocate_rows` moves the human rows through the page map after a compute under a run. A row with no address is refused (409, `*_UNADDRESSABLE_MESSAGE`)
- A move is a dismiss plus an add that names it in `replaces`; the endpoint answers the id that holds the box now, and the viewer follows it. `undo_move` alone gives a computed box back; `withdraw` cascades to nothing. A second move replaces the addition, not the computed row
- `Redaction` rows are in PDF points; `Detection` rows in 200-dpi pixels. Every output of the compute is a row: `detections.json`, `redaction_rects`, `margin_rects` and `opinions_json` are gone
- The compute pairs once (`_snapped_document`); `bl_pair` is not imported. A recompute keeps every row; only a first import under a run replaces the model rows
- A curator starts the compute from step 2 (`compute_redactions_api`, #305), and `REDACTION_COMPUTE_STATUSES` refuses `REDACTION_REVIEW_DONE`, whose way back is the re-queue. `findings.rebuild` is the one recompute that runs in the request, on any pod: it reads rows and nothing else
- `approve_redaction_review` is the only place a person closes review 2: a compare-and-swap READY to QUEUED with `CREATE_OPINIONS` (`opinions.queue_create_opinions`), and its log line is the only record of who decided. `run_create_opinions` parks in REDACTION_REVIEW_DONE on success and back in READY_FOR_REDACTION_REVIEW with the reason on a failure, never ERROR; the retry is the next press (#336). REDACTION_REVIEW_DONE gates step 3; a legacy volume keeps its link
- The findings of review 2 are `Issue` rows (`REVIEW2_CHECKS`, `Issue.target`), and `findings.rebuild` is their one writer (#240 PR D). It derives every finding from the detection, boundary and redaction rows, with no S3 read and no render, and runs at the end of the compute (which passes the run it measured in, because its ledger stamp is written after the park) and in every review-2 write endpoint (`_rebuild_findings`), so a card is never older than the last write. `recalculate_issues` excludes `REVIEW2_CHECKS`
- Stale is read off the rows, never threaded from `resolve`: a standing decision no `decision` FK points at, or a human row whose `apply_run` is not the measured run, is a `stale_*` card. No measured finding is written without a computed boundary
- `ReviewDismissal` names its target by address plus a copy of the box; `findings.resolve` lands it by `IOU_THRESHOLD` and the finding is written with `Issue.dismissal` set (muted, with Undo). A stale card is withdrawn (`withdraw_stale`), never dismissed (`UndismissableFinding`, 409). Nothing deletes a dismissal
- A review-2 `page_number` is the 1-based position in the space the rows are drawn in, so step 1 and `process_actions` list `scan.issues.exclude(check_name__in=REVIEW2_CHECKS)`, and `dismiss_issue` refuses a review-2 row. The page and the `review_findings` fragment render `_review_findings.html` from `findings.viewer_groups`
- The review-2 approval warns and obeys: `_review_flags` carries `review2_open` and `review2_stale`, the bar asks for a confirm, the view blocks nothing
- No migration writes a finding: `rebuild_review2_findings` writes them once after the deploy. `flag_issue`, `remove_flag` and the three user-action checks are gone
- A text redaction box is fitted to the dots.mocr cells under it (`text_fit.fit_span`, #279): the horizontal limits only, never wider, only a `TEXT_RECT_TYPES` box, and only on a page whose cells were read. The vertical limits stay on the ink. `text_fit.load_document` is the one rule for which OCR document the geometry reads, the twin of `geometry_pdf_path`, and the compute reads it once for the fit and the bracket readings
- The fit leaves a computed row a standing dismiss points at alone (`refit_text_redactions`, #279), so every decision keeps its IoU on the box it named. Nothing sets `Page.col_*` or `midpoint`, so blackletter measures every box from the fallback 50/50 split
- The two `TEXT_COLUMN` boxes of a page are separated before any geometry reads them (`columns.separate_rows`, then `separate_document` after the ink snap, #308): the inner edges come from the dots.mocr cells, inwards only, and a page with no band gets a gap of a pixel and a half on each side that keeps the gutter centre. Touching boxes leave blackletter's `clamp_to_gutters` with no neighbour, and a headnote box then grows across the gutter onto the facing column's text
- The margin content box is fitted to the dots.mocr cells of its page (`margin_fit.fit_pages`, #323): smaller only, and blackletter owns every guard (`MARGIN_MIN_KEEP_RATIO`, the header and column holds, the page frame), because the ink box it judges never leaves `margins`. A page with no cells keeps that box, so a missing read costs one page the fit and no more. Each cell is held inside its own render first, because the box is a union and one stray cell would cost the page
- The four overlay modes have one table, the rows of `_viewer_help.html`: the `r` cycle, the mode button's label and the guide all read it, and `checker.css` keys the colour by `data-mode`. Never write a second copy (#299)
- `LABEL_IDS` in `viewer_step2.js` is the browser's copy of `blackletter.models.Label`, pinned in both directions by a test (#343). The drawer's menu offers no name it has no id for, and `HEADING` and `BLOCKQUOTE` draw although no redaction reads them
- Every review-2 write answers `{status: "ok", message}` and the viewer shows it as a success toast (`showSaved`, or `showSavedAfterReload` over a reload). The text lives in the view, never in the script, and the write views are the set `test_success_messages.WRITE_VIEWS` pins (#322)
- A bracket the reader saw and the model did not is a finding (`brackets.missing`, #328): one `BracketReading` row per dots.mocr cell that **starts** with a bracket, written by the compute and disposable like a model `Detection` row. A candidate whose number its opinion already names is the star-pagination mark, never a finding
- The opinion of a reading comes from `boundaries.standing`, in the order `reading_key` defines, never from the caption rows. The count of `HEADNOTE` boxes is not the count of headnotes, so no finding is built on it
- Every page overlay is drawn from its rows at every render and never positioned once: `renderPage` calls the redaction, bounds and dim draws, because a re-render changes the scale. A selected opinion is a cache plus a draw (`_drawDimForPage`), never a one-time paint. Its masks dim, and draw in the `bounds` mode alone: a redaction mode shows the page as the output has it (#311)

## Review 3 (#335)

- An `Opinion` is keyed by `(scan, first printed page, index in that printed page)`, stamped at creation, never by the boundary anchors. A later reading change is a card, never a new key. `page_count` holds the physical count, so no check does arithmetic on the printed numbers
- `OpinionText` is one row per **page** of an opinion, and it holds an engine reading only where the engines disagree; the whole read of every engine stays on S3. `text` is a cache; `human_text` is the truth and nothing discards it. An edit moves the offsets of `disagreements`, so it answers that page
- The per-opinion glues live under `jobs/opinions/{first_printed_page}.{index_in_page}/r{glue_revision}/`, the invariant key and never the pk, and a re-glue raises the revision (#350); the creation raises it on every matched row that is not `TEXT_REVIEW_DONE`, so no revision is written twice. The detections and the redactions are not among them: they are rows since #241, and restricting them to one opinion is a query. `approved_text_key` is not a glue, and no re-glue may overwrite it
- `opinion_ocr.write` is the one writer of an opinion's OCR documents (#350): one `{engine}.json` per engine of `opinion_ocr.ENGINES` plus a `manifest.json` written last. Every unit of the opinion's pages is in the file with its verdict (`exclusion`, `share`, in the three bands of `EXCLUDE_SHARE` and `FULL_SHARE`); nothing is dropped before the ensemble aligns, and `kept_units` is the one reader that applies the verdict. A unit with no box, or on a page with no size, is `unjudged` and never clean text: it may be under a redaction, so it stays out of `kept_units` and counts in the manifest. The page `md` is never copied
- A third engine is one entry of `opinion_ocr.ENGINES` and no other code (#368): the glue, the files index, the file route and `engines_owed` all walk that table, and its order is the rank of the ensemble vote, with Surya last until a measurement moves it
- `ocr_glue_revision == glue_revision` is the one rule for "the OCR glue exists" (`opinion_ocr.is_written`, #350). The glue is pass ten of the collect tick: it walks the due scans newest first and glues the first one whose inputs load, `OPINIONS_PER_TICK` rows of it, so a held volume holds no other; a fact about the scan (no final run, a stale redaction set, an engine the run owes per `engines_owed`, whose last live step decides) holds that scan and spends no attempt; a fact about the row spends `ocr_glue_attempts`, then ERROR. A late engine is a re-glue (`reglue_opinion_ocr`), never a watcher
- A warning of review 3 is an `OpinionFinding`, never an `Issue` row. It keeps the review-2 rules: one rebuild writes them, a dismissal is its own row that nothing deletes, and a `STALE_OPINION_CHECKS` row cannot be dismissed
- `OpinionScan` is frozen (#173/#206 paused its writer). `scan.legacy_opinions` reads it, `scan.opinions` reads the new model, and `ExternalJob.opinion` is an `Opinion`
- `opinions.create_rows` is the one writer of an `Opinion` row (#336): the key is the printed number of the start page (a `suffixed` page shares the bucket of its number, a range page gives its end) plus the rank in `boundaries.standing`. A start page with no number refuses the volume; a key is never guessed from a position. A matched row is updated in place and keeps its status and every human field; only ERROR goes back to PROCESSING
- The creation writes the `STALE_OPINION_CHECKS` alone and deletes them on a match: `STALE_PAGE_NUMBER` when a live boundary shares the start address under another number, `ORPHANED_OPINION` otherwise. They need the printed numbers, which the rows do not hold; #334's rebuild writes the other checks (#336)
- A re-derived matched row that no human approved has its `glue_revision` raised by the creation (#335, #336); an approved row is raised by the second-run rule alone. Nothing else writes the revision
- The redacted PDF of an opinion (#336) is a glue at `opinion_pdf.key`, cut by `blackletter.api.generate` over a source that holds the opinion's pages alone, never the volume, so every index of the payload is in that source's space (`opinion_pdf.payload`). `redacted_pdf_revision == glue_revision` is the one rule for "the PDF exists" (`opinion_pdf.is_written`), `opinion_pdf.due` for "a PDF is owed", and `OPINION_PDF_STATUSES` for which scan statuses the pass reads: #334 extends the set, or every unwritten PDF of a moved scan stops being due in silence
- The PDF pass is a fact on the row and no hand-off: one PDF per tick (`opinion_pdf.PDFS_PER_TICK`), the scan on disk first and then the newest (`opinion_pdf.next_due`), no `QueuedAction` and no status write on the scan. The first tick of a volume pulls the bitonal copy and the first tick that needs a picture pulls a shard, inside the serial loop, the hazard of the Mistral wave. A fault the rows explain (`OpinionPdfError`) and an unexpected exception both count on `pdf_attempts`, and at `opinion_pdf.MAX_ATTEMPTS` a `PROCESSING` row is `ERROR`, loud then quiet; a `TransientFault` (a pull or a PUT) alone counts nothing. Every fault stamps `pdf_attempted_at`, and the row waits `opinion_pdf.retry_after()`. `owed` is the ledger and `due` is `owed` less that cooldown: the release of the local tree reads `owed`, or one failed PUT on a volume's last opinion re-pulls the whole volume every cooldown
- The pass never deletes an input inside a tick: the bitonal mirror and the shards stay until the scan has no owed row, or until the daemon starts (`opinion_pdf.release_mirrors`). Its outputs go in the `finally` of the tick that made them. The printed range is the download name alone (`serve_opinion_pdf`, #165)
- `/opinions/` lists the `Opinion` rows and `/opinions/legacy/` the frozen `OpinionScan` rows (#334). The step-3 tab and the step-2 "Next" button read one pair of flags: `review3_opinions` sends both to the opinions page, `legacy_pipeline` (`stats.LEGACY_STATUSES`, not `legacy_review`) keeps both on `?step=3`, and a new volume with neither gets no link. The flag is not `opinion_count`, which already names the boundaries of step 2
- The warning badge of the opinions list is `opinions.finding_counts` over the ids of one page, after the pagination, the rule every list badge follows. `/opinions/<pk>/review/` has no write endpoint until the text review lands, so it offers no control (#334)
- A template writes every opinion PDF address onto its card (`data-redacted-url`), and the viewer reads it: a path a script spells by hand goes stale in silence, because no test reverses it (#334)
- `ensemble.py` is the one transform of an opinion's OCR documents, and `ensemble_revision == ocr_glue_revision` is the one rule for "the ensemble exists" (#365). It reads those documents alone: no volume, no PDF, no render. A better transform is a re-run (`rerun_opinion_ensemble`, the button), never a re-paid read
- The pass is eleven of the collect tick and takes a row with `ocr_engine_count` at least `OPINION_ENSEMBLE_MIN_ENGINES` (3), stamped by the OCR glue (#365): a vote of two engines settles nothing. The button waives that gate, and it is the only way in until Surya joins `opinion_ocr.ENGINES`
- A fault of the row spends `ensemble_attempts` and ends at ERROR; a `TransientFault` (a read or a write of the bucket) spends nothing, because the tick is fifteen seconds and a bucket away for a minute would end every due row (#365). A missing object is a fact about the row, not a transient fault. `opinions.create_rows` resets the counter, so the next approval is the way back
- The alignment is geometry alone, in the points of the volume page, and a group any of whose units carries an exclusion is dropped after it, never before (#317/#365). The reading order comes from the boxes (three bands, a column boundary off the left edges) and reads no `Detection` row, so a curator's edit never moves the text
- The vote compares `ensemble.compare_text` and shows the winner's own text (#365): the engines differ about the quotes, the dashes and the markdown marks on almost every page, and about the words rarely. An engine that read nothing does not vote, and it is named in the group's `silent`. No document stores markup; a voted group stores tokens with a `low_confidence` flag and the viewer builds the nodes
- `ensemble._differs` is the one rule for "the engines did not read this group alike", and the `ENGINES_DISAGREE` card and the `OpinionText.disagreements` entry both read it (#365). An approved opinion is never written over: `due()` and the button refuse `TEXT_REVIEW_DONE`, the rule of `opinion_ocr.reglue`
- The ensemble is the one writer of the `OpinionText` rows (#365). `text` and `disagreements` are a cache of the documents; nothing there reads or writes `human_text`
- `ensemble.rebuild_findings` is the one writer of `ENSEMBLE_CHECKS`, and `opinions.create_rows` keeps the two stale ones (#365). A standing `OpinionFindingDismissal` of the same page and check mutes the new card
- The review page of an opinion frames `serve_opinion_pdf` under `?disposition=inline`, the one route that answers `SAMEORIGIN` where the site answers `DENY`, and `opinion_file_index` is its `files` index (#334): both read the two ledgers (`opinion_pdf.is_written`, `opinion_ocr.is_written`) and never the bucket. The OCR stamp is one over every engine document of the revision, so an engine document is written only when the run also carries that engine's key, and an entry with no `url` is an object nothing wrote

## Worker images

- The scaffold is in `runpod_common`, once: Sentry, the result envelope, `execute_action`. Only `BadInputError` maps to `BAD_INPUT`. Without `result_url` a worker answers inline
- YOLO: only `bl_warm.pt` is baked, and the handler passes no `imgsz` (the checkpoint carries 1024). No CUDA base layer
- dots.mocr: `DPI = 200` and `PROMPT_MODE` are module constants; `HANDLER_MAX_COMPLETION_TOKENS = 6144`; the retry ladder changes only the render (deterministic); the layout repair (`layout_json.py`, no Django import, copied into the image) runs before the ladder; `raw` is never written over
- Tagger: GPU-only (`HANDLER_ALLOW_CPU=1` for a laptop only), transformers 5, model baked and opened at build time
- Surya (#320): the handler owns no decode parameter and refuses one in the input (`REFUSED_INPUTS`); it calls surya's `RecognitionPredictor([image], full_page=True)` and the package's greedy pass, loop retry and block-mode fallback are the ladder. The `SURYA_*` env is set before the package is imported (its settings are read once). The base is `vllm/vllm-openai:v0.20.1` or later, the first to register `Qwen3_5ForConditionalGeneration`
- One build workflow per image, and each PATCHes its own template id

## Files, disk and pages

- `serve_scan_pdf` never streams the original (#185): 202 or 409 with `original_available`. The viewer reads the original through a presigned GET (`scan_original_url`), which needs the bucket CORS rule. A URL a `fetch` reader consumes comes as JSON; a developer's route redirects (#243/#262)
- `release_local_processing` frees `/tmp/scanning/{pk}` after a successful push, on the exit from AWAITING when the park won, on a terminal failure, and at the end of the two queued workers. Never on a re-queue (#215). `cleanup_processing_tmp` judges by `_tree_mtime` and sweeps the `*_TMP_PREFIX` scratch dirs
- `/stats/` (#260): `STATUS_GROUPS` covers every `Status` value once, test-pinned. A legacy status counts in `LEGACY_STATUSES` only. When #206 lands, move APPROVED into `REDACTION_REVIEW_COMPLETE_STATUSES`
- A list page costs one query per page for its badges (`repairs.waiting_counts` over the page's ids, after the pagination)
- The `Detection` admin reads a page of its table and no more (#359): the list walks the primary key, an unfiltered page takes its count from `pg_class` (`EstimatingPaginator`), and a filter on a free text column of a large table is a `SimpleListFilter` with static choices (`LabelFilter`), never Django's default, which runs `SELECT DISTINCT` over the table

## Tailwind CSS

- Config: `scanning/assets/tailwind/tailwind.config.js`; input: `scanning/assets/tailwind/input.css`; output: `scanning/assets/static-global/css/tailwind_styles.css` (gitignored, built by npm)
- Component classes in `input.css`: `.btn-primary`, `.btn-outline`, `.btn-danger`, `.btn-ghost`, `.card`, `.input-text`, `.alert-*`, `.badge-*`
- Templates use cotton components: `<c-header />`, `<c-footer />`

## Environment

- `DEVELOPMENT=True` enables the debug toolbar, local filesystem storage and the dev S3 buckets
- `TESTING=True` is auto-detected from `sys.argv`: LocMemCache, MD5 password hasher, no debug toolbar URLs
- `DB_SSL_MODE=prefer` is needed outside Docker
- `DOCTOR_ENABLED` and `DOCTOR_HOST` default to working values; `RUNPOD_YOLO_ENDPOINT_ID` blank turns detection off and leaves dots.mocr on
- `BLACKLETTER_LOG_LEVEL` sets the `blackletter` logger, INFO by default; without that entry `generate`'s records reach no handler (blackletter#81)
