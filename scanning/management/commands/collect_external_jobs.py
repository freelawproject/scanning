"""Confirm in-flight external jobs and finish the scans they belong to.

Thirteen passes, in order.

**1. ``jobs.sweep_jobs()`` asks after every job still in flight.** How
it asks depends on the provider:

- A RunPod job is polled at ``GET /status``. This is the only way such a
  job ever finishes, because submitting it merely put it in a queue.
- A doctor job has no status endpoint, so instead we check whether its
  result object has appeared in S3. Doctor answers on the submit call, so
  this pass matters only when that answer was lost -- a killed daemon, a
  redeployed pod, an abandoned read. Doctor converts and uploads whether
  or not we are still listening, so a lost answer costs the answer, not
  the work.

**2. ``bitonal.finish_ready_scans()`` applies finished conversions.** It
merges the shards of any scan whose conversion jobs are all done and
moves it out of ``AWAITING``.

**3. ``dots_mocr.finish_ready_runs()`` glues finished OCR runs.** It
joins the per-shard payloads of any scan whose dots.mocr rows are all
``COMPLETED`` into one volume JSON on S3 and flips the rows to
``CONSUMED`` (issue #202). It writes no scan status, and it keeps the
per-shard results: a future smart glue over page inserts and deletes
re-reads them.

**4. ``dots_mocr.apply_ready_runs()`` applies glued runs (#149/#204).**
It reads each glued volume JSON, rebuilds ``Scan.ocr_results`` and the
Issues, and takes the scan over the review edge to
``READY_FOR_PAGE_COMPLETENESS_REVIEW`` with one compare-and-swap.
Deliberately not queued work (#212): the scan never transits
QUEUED/PROCESSING. A scan still ``AWAITING`` its conversion is
deferred and picked up on the tick after the bitonal park.

**5. ``yolo.finish_ready_runs()`` merges finished detection runs.** It
joins the per-shard payloads of any scan whose detection rows are all
``COMPLETED`` into one volume JSON on S3 and flips the rows to
``CONSUMED`` (issue #196). Like the dots.mocr glue it writes no scan
status and keeps the per-shard results: a page insert recomputes the
merge from them.

**6. ``yolo.queue_ready_runs()`` queues the redaction computation.**
It is a trigger, not the work: it takes a scan in
``PAGE_COMPLETENESS_REVIEW_DONE`` to ``QUEUED`` with
``COMPUTE_REDACTIONS``, and ``process_next_scan`` runs it. Since the
daemon starts detection right after the upload (#250), the run is
usually merged by pass 5 while the scan is still in review 1, and it
waits there at no cost until the approval; this pass takes it on the
tick after. The
computation renders every page of the volume three times (83s for 1364
pages, measured), and this tick's scheduler is serial (#156), so it
must not run here.

**7. ``review_states.promote_ready_scans()`` opens review 2 (#263).**
It takes an approved scan whose redactions are computed, and whose
corrected volume is built (#224), to
``READY_FOR_REDACTION_REVIEW`` with one compare-and-swap. It is the
safety net rather than the usual writer: pass 6's computation parks a
scan it just finished in that status itself, so what is left here is
the volume whose corrected build lands after its geometry, and every
volume that was already approved and measured when #263 shipped.

**8. ``mistral_ocr.finish_ready_runs()`` glues finished Mistral runs.**
It joins the stored per-shard results of any scan whose Mistral rows
are all ``COMPLETED`` into one volume JSON on S3 and flips the rows to
``CONSUMED`` (#245). Like the other two glues it writes no scan status
and keeps the results: the glue is where every transform runs, so a
better transform is a re-glue at no API cost.

**9. ``mistral_ocr.finish_ready_applies()`` reads the edited pages.**
For a scan whose Mistral volume run is glued and whose corrected
volume (#224) is built, it creates the one-page rows of the pages a
curator changed and, once those have answered, writes the corrected
volume's own Mistral document. A volume nobody read with Mistral is
never a candidate, so this pass starts no paid work of its own. It
runs after the review passes because nothing else waits for it: no
review state reads its output.

**10. ``surya.finish_ready_runs()`` glues finished Surya runs.** It
joins the stored per-shard results of any scan whose Surya rows are all
``COMPLETED`` into one volume JSON on S3 and flips the rows to
``CONSUMED`` (#368). The twin of pass 8, engine for engine: no scan
status, the results kept, and the glue as the one transform of a
result.

**11. ``surya.finish_ready_applies()`` reads the edited pages.** The
twin of pass 9 for Surya (#368): for a scan whose Surya volume run is
glued and whose corrected volume (#224) is built, it creates the
one-page rows of the pages a curator changed and, once those have
answered, writes the corrected volume's own Surya document. A volume
nobody read with Surya is never a candidate.

**12. ``opinion_ocr.glue_due()`` writes the OCR documents of the
opinions (#350).** Up to ``OPINIONS_PER_TICK`` rows of the newest scan
in ``REDACTION_REVIEW_DONE`` that owe their glue. Seconds over the
corrected volume's JSON documents, read once per tick from the local
mirror. A scan whose run still owes an engine read waits.

**13. ``ensemble.run_tick()`` writes the text of the opinions
(#365).** For up to ``ENSEMBLE_PER_TICK`` rows whose OCR documents are
written and whose ensemble stamp is older, it aligns the engines' units,
puts them in reading order, resolves each group and writes the
``OpinionText`` rows, the warnings and one document on S3. It reads
those documents alone: no volume, no PDF, no render. A row with fewer
engine documents than ``OPINION_ENSEMBLE_MIN_ENGINES`` is not due, so
nothing runs by itself until the third engine reads.

**14. ``opinions.promote_ready_opinions()`` opens the text review
(#365).** An opinion whose redacted PDF and whose text are both written
at the live revision goes from ``PROCESSING`` to
``READY_FOR_TEXT_REVIEW``. The two objects come from two passes that
know nothing of each other and either can be last, so the promotion is
a pass of its own, the twin of pass 7 for a scan. It takes a row
back to ``PROCESSING`` too, because a re-glue raises the revision and
leaves the status where it was.

Examples:

    # Run one confirm tick and exit.
    docker exec scanning-daemon python manage.py collect_external_jobs

    # Also runs automatically from run_daemon every
    # DAEMON_COLLECT_INTERVAL seconds (default 15s).
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Ask after every in-flight external job, then merge and park any "
        "scan whose conversion jobs have all finished, then glue any "
        "finished dots.mocr run into its volume document, then apply "
        "every glued run (page numbers and Issues), then merge every "
        "finished detection run, queue the page edit apply of every "
        "approved scan that owes one, queue every merged detection "
        "run's redaction computation, open the redaction review "
        "of every scan that is ready for it, then glue every finished "
        "Mistral run and every corrected volume that owes its Mistral "
        "document, then glue every finished Surya run and every "
        "corrected volume that owes its Surya document, then write "
        "the OCR documents of the opinions that owe them, then write "
        "the text of the opinions whose ensemble is older than those "
        "documents."
    )

    def handle(self, *args, **options):
        """Run one confirm tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import (
            apply,
            bitonal,
            dots_mocr,
            ensemble,
            jobs,
            mistral_ocr,
            opinion_ocr,
            opinions,
            review_states,
            surya,
            yolo,
        )

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                summary = jobs.sweep_jobs()
                finished = bitonal.finish_ready_scans()
                glued = dots_mocr.finish_ready_runs()
                applied = dots_mocr.apply_ready_runs()
                detected = yolo.finish_ready_runs()
                applied_edits = apply.queue_ready_scans()
                queued = yolo.queue_ready_runs()
                promoted = review_states.promote_ready_scans()
                extracted = mistral_ocr.finish_ready_runs()
                extracted_applies = mistral_ocr.finish_ready_applies()
                read_surya = surya.finish_ready_runs()
                surya_applies = surya.finish_ready_applies()
                glued_opinions = opinion_ocr.glue_due()
                read_opinions = ensemble.run_tick()
                ready_opinions = opinions.promote_ready_opinions()
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    logger.warning(
                        "DB connection failed during collect tick after "
                        "%d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return

        if any(
            (
                summary.completed,
                summary.retried,
                summary.failed,
                summary.errors,
                finished,
                glued,
                applied,
                detected,
                applied_edits,
                queued,
                promoted,
                extracted,
                extracted_applies,
                read_surya,
                surya_applies,
                glued_opinions,
                read_opinions,
                ready_opinions,
            )
        ):
            self.stdout.write(
                f"Completed {summary.completed}, retried {summary.retried}, "
                f"failed {summary.failed}, still waiting {summary.pending}, "
                f"check errors {summary.errors}; finished {finished} "
                f"scan(s), glued {glued} OCR run(s), applied {applied}, "
                f"merged {detected} detection run(s), queued "
                f"{applied_edits} page edit apply(s) and {queued} "
                f"redaction computation(s), opened {promoted} redaction "
                f"review(s), glued {extracted} Mistral run(s) and "
                f"{extracted_applies} corrected volume(s), glued "
                f"{read_surya} Surya run(s) and {surya_applies} "
                f"corrected volume(s), wrote the OCR "
                f"documents of {glued_opinions} opinion(s), wrote the "
                f"text of {read_opinions} opinion(s), opened {ready_opinions} "
                f"text review(s)"
            )
