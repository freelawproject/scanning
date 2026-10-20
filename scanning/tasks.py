"""The daemon's passes as Celery tasks (#503).

Two kinds of task, and one rule over both: **the database is the queue
of record**. Every task is either a tick (a poll over the rows, what
``run_daemon`` ran on an interval) or a claim by compare-and-swap of
a scan a person queued. So a lost message is a delay until the daemon
sends the tick again, a duplicate delivery loses its swap, and a
broker outage loses no work. Celery retries nothing and stores
nothing: the rows carry the attempt counters, and a task answers
through the rows it writes.

**The clock is the daemon.** :data:`TICKS` is the table ``run_daemon``
reads with ``CELERY_ENABLED`` on: one tick task per pass group of
``scanning.ticks`` and per one-shot command, on the daemon's own
intervals, each on the queue of its cost. A tick is sent with
``expires`` equal to its interval, so the ticks that pile up behind a
busy worker are dropped, never replayed.

**A finished step sends the next.** :func:`nudge` sends a tick now,
and the tick tasks and :func:`run_scan` call it for the pass that
reads what they wrote, so a hand-off never waits an interval; the
interval is the safety net, as CourtListener chains its steps.

**A tick that counts and then writes never overlaps itself.**
:func:`locked` wraps it in a named advisory lock (``scanning.locks``):
a second copy, from a second worker process or a tick sent while the
last one runs, skips its turn. The three opinion passes share one
lock, because they share the local mirror and one of them releases
it. ``claim_scans`` and ``run_scan`` take none: their claims are
swaps, and N copies are N scans.

The dispatch tasks are sent by the views and the triggers through
:func:`dispatch_scan` and :func:`submit_soon`, after their status
write, so a button press starts its work at once. Both helpers are
no-ops when ``CELERY_ENABLED`` is off, and both swallow a broker
fault, because the tick finds the row anyway.
"""

from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass

from celery.signals import worker_ready
from django.conf import settings
from django.core.management import call_command
from django.db import connections

from scanning import locks
from scanning.celery_init import app

logger = logging.getLogger(__name__)

QUEUE_FAST = "fast"
QUEUE_IO = "io"
QUEUE_HEAVY = "heavy"
QUEUE_OPINIONS = "opinions"
QUEUES = (QUEUE_FAST, QUEUE_IO, QUEUE_HEAVY, QUEUE_OPINIONS)

#: The lock the three opinion passes share: they read and release one
#: local mirror (``apply.local_copy`` under ``Scan.output_dir``).
OPINION_MIRROR_LOCK = "opinion_mirror"

#: How long every send is held back after a broker fault, so a view or
#: an admin loop over many scans pays one short timeout and not one
#: per scan. The rows hold the work; the clock finds it when the
#: broker is back.
BROKER_BACKOFF_SECONDS = 30
_broker_down_until = 0.0


def _broker_failed() -> None:
    """Note a broker fault: no send for :data:`BROKER_BACKOFF_SECONDS`.

    :return: None.
    """
    global _broker_down_until
    _broker_down_until = time.monotonic() + BROKER_BACKOFF_SECONDS


def _broker_ok() -> bool:
    """Return whether the broker may be asked right now.

    :returns: ``False`` inside the backoff after a fault.
    :rtype: bool
    """
    return time.monotonic() >= _broker_down_until


def reset_broker_backoff() -> None:
    """Forget a broker fault (tests).

    :return: None.
    """
    global _broker_down_until
    _broker_down_until = 0.0


def queue_depth(queue: str) -> int | None:
    """Return how many messages wait on a queue, or ``None`` when the
    broker cannot say.

    CourtListener's ``get_queue_length`` without the priority steps,
    which these queues do not use: one ``LLEN`` on the Redis list the
    queue is. A broker fault or a non-Redis transport answers ``None``,
    and the caller sends as if the queue were empty.

    :param queue: A queue name of :data:`QUEUES`.
    :returns: The depth, or ``None``.
    :rtype: int | None
    """
    try:
        with app.connection_for_read() as connection:
            return int(connection.default_channel.client.llen(queue))
    except Exception:
        _broker_failed()
        logger.warning("could not read the depth of %s", queue, exc_info=True)
        return None


def has_room(queue: str) -> bool:
    """Return whether a send to the queue is under the depth cap.

    The throttle (#503): CourtListener's ``CeleryThrottle`` waits for a
    queue to drain before a command enqueues more; the senders here are
    the daemon's clock and the views, which cannot wait, so they skip
    the send instead. Nothing is lost: every message is a tick or a
    claim, and the rows hold the work until the queue drains.

    :param queue: A queue name of :data:`QUEUES`.
    :returns: Whether to send.
    :rtype: bool
    """
    if not _broker_ok():
        logger.info(
            "broker fault %ss ago at most; not sending", BROKER_BACKOFF_SECONDS
        )
        return False
    depth = queue_depth(queue)
    if depth is None or depth < int(settings.CELERY_QUEUE_MAX_DEPTH):
        return True
    logger.warning(
        "queue %s holds %d messages, at the cap; not sending", queue, depth
    )
    return False


def _fresh_connection() -> None:
    """Open a fresh database connection for this task.

    The daemon did the same before every tick: a pooled connection
    that died while the worker waited on a socket for minutes raises on
    first use, and a fresh one is cheap next to any pass. A locked task
    gets it from :func:`locked`, before the lock; the unlocked ones
    call it themselves.

    :return: None.
    """
    connections.close_all()


def locked(name: str):
    """Run the task body under the named advisory lock, or skip it.

    :param name: The lock's name; a tick's own, or one shared with the
        passes it must not overlap.
    :returns: A decorator for a task body that returns a count.
    """

    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            # Before the lock, never inside the body: the lock is a
            # session lock on this connection, and closing the
            # connection would free it under the running pass.
            _fresh_connection()
            with locks.held(name) as held:
                if not held:
                    logger.info("%s: another worker runs it; skipped", name)
                    return 0
                return fn(*args, **kwargs)

        return run

    return decorate


# -- the tick tasks: the daemon's schedule -------------------------------


@app.task(name="scanning.tasks.claim_scans", queue=QUEUE_HEAVY)
def claim_scans() -> int:
    """Recover the stale claims, then claim and run one queued scan.

    The body of ``process_next_scan``. On the heavy queue with N
    replicas, N ticks claim N scans; the claim is ``SKIP LOCKED``, so
    no lock here.

    :returns: How many scans ran (0 or 1).
    :rtype: int
    """
    from scanning.management.commands import process_next_scan

    _fresh_connection()
    process_next_scan.recover_stale()
    claimed = process_next_scan.claim_next()
    if claimed is None:
        return 0
    scan, action = claimed
    process_next_scan.dispatch(scan, action)
    _nudge_after(action)
    return 1


@app.task(name="scanning.tasks.enqueue_runs", queue=QUEUE_FAST)
@locked("enqueue_runs")
def enqueue_runs() -> int:
    """Start the detection and OCR runs of every new shard set.

    :returns: How many runs started.
    :rtype: int
    """
    from scanning import dots_mocr, yolo

    # The same sweeps as ``submit_external_jobs``; a third engine's
    # sweep (#500) is one more line here and there.
    started = yolo.enqueue_missing_runs() + dots_mocr.enqueue_missing_runs()
    if started:
        nudge(submit_runpod)
    return started


def _submit(provider: str) -> int:
    """Run one provider's wave and return what it submitted.

    :param provider: A ``JobProvider`` value.
    :returns: The submitted count.
    :rtype: int
    """
    from scanning import jobs

    return jobs.submit_pending(providers=(provider,)).submitted


@app.task(name="scanning.tasks.submit_runpod", queue=QUEUE_FAST)
@locked("submit_runpod")
def submit_runpod() -> int:
    """Send one wave per RunPod engine: a fast ``POST /run`` each.

    Locked, because the wave counts the rows in flight against the
    engine's cap before it claims.

    :returns: The submitted count.
    :rtype: int
    """
    from scanning.models import JobProvider

    return _submit(JobProvider.RUNPOD)


@app.task(name="scanning.tasks.submit_doctor", queue=QUEUE_IO)
@locked("submit_doctor")
def submit_doctor() -> int:
    """Send one wave of conversions to doctor, holding the sockets.

    :returns: The submitted count.
    :rtype: int
    """
    from scanning.models import JobProvider

    return _submit(JobProvider.DOCTOR)


@app.task(name="scanning.tasks.submit_mistral", queue=QUEUE_HEAVY)
@locked("submit_mistral")
def submit_mistral() -> int:
    """Render and submit one shard to Mistral: minutes, on the heavy queue.

    Locked like the other waves: one render at a time keeps the batch
    cap (``mistral_ocr.MAX_CONCURRENCY``, counted before the claim)
    exact, and two workers never pay for two renders when one was
    wanted. Lifting it is a measured decision, not a default.

    :returns: The submitted count.
    :rtype: int
    """
    from scanning.models import JobProvider

    return _submit(JobProvider.MISTRAL)


@app.task(name="scanning.tasks.sweep_jobs", queue=QUEUE_FAST)
@locked("sweep_jobs")
def sweep_jobs() -> int:
    """Poll every in-flight external job, and send the passes that
    read a completion.

    :returns: How many completed.
    :rtype: int
    """
    from scanning import ticks

    completed = ticks.sweep().completed
    if completed:
        nudge(merge_conversions, glue_runs)
    return completed


@app.task(name="scanning.tasks.merge_conversions", queue=QUEUE_HEAVY)
@locked("merge_conversions")
def merge_conversions() -> int:
    """Merge the bitonal shards of every converted scan.

    :returns: How many scans finished.
    :rtype: int
    """
    from scanning import ticks

    finished = sum(ticks.merge_conversions().values())
    if finished:
        nudge(glue_runs, score_pages)
    return finished


@app.task(name="scanning.tasks.glue_runs", queue=QUEUE_FAST)
@locked("glue_runs")
def glue_runs() -> int:
    """Glue the volume runs and move the review states.

    Locked, because the apply trigger counts the applies in flight
    against ``apply.MAX_SCANS_IN_FLIGHT`` before it queues, and the
    ledgers of the glues are read and written here.

    :returns: The sum of what the passes did.
    :rtype: int
    """
    from scanning import ticks

    return sum(ticks.glue_runs().values())


@app.task(name="scanning.tasks.prepare_opinions", queue=QUEUE_OPINIONS)
@locked(OPINION_MIRROR_LOCK)
def prepare_opinions() -> int:
    """Write the opinions' documents and open their reviews.

    On the opinions queue and under the mirror lock: the OCR glue
    reads the volume's documents through the same local mirror the
    PDF pass releases, so it must run on that pod.

    :returns: The sum of what the passes did.
    :rtype: int
    """
    from scanning import ticks

    done = sum(ticks.prepare_opinions().values())
    if done:
        nudge(build_opinion_pdfs, cut_figures)
    return done


@app.task(name="scanning.tasks.score_pages", queue=QUEUE_HEAVY)
@locked("score_pages")
def score_pages() -> int:
    """Score one volume with the bad-page model.

    :returns: How many volumes were scored.
    :rtype: int
    """
    from scanning.badpage import scoring

    return scoring.run_tick()


@app.task(name="scanning.tasks.cut_figures", queue=QUEUE_OPINIONS)
@locked(OPINION_MIRROR_LOCK)
def cut_figures() -> int:
    """Cut the pictures of one opinion's text.

    :returns: How many opinions were cut.
    :rtype: int
    """
    from scanning import opinion_figures

    return opinion_figures.run_tick()


@app.task(name="scanning.tasks.build_opinion_pdfs", queue=QUEUE_OPINIONS)
@locked(OPINION_MIRROR_LOCK)
def build_opinion_pdfs() -> int:
    """Write one redacted opinion PDF.

    :returns: How many PDFs were written.
    :rtype: int
    """
    from scanning import opinion_pdf

    return opinion_pdf.run_tick()


@app.task(name="scanning.tasks.cleanup_tmp", queue=QUEUE_FAST)
def cleanup_tmp() -> int:
    """Sweep this pod's stale processing directories. The daemon sends
    one per queue, because every pod has its own ``/tmp``; the
    abandoned uploads are :func:`recover_uploads`'s, once.

    :returns: 0, for the shape of the other ticks.
    :rtype: int
    """
    _fresh_connection()
    call_command("cleanup_processing_tmp", no_uploads=True)
    return 0


@app.task(name="scanning.tasks.recover_uploads", queue=QUEUE_FAST)
@locked("recover_uploads")
def recover_uploads() -> int:
    """Recover or delete the abandoned direct uploads, once per interval.

    A database sweep and not a pod's, so one locked task and never one
    per queue: two copies would both queue the same upload, and the
    second would put a running pipeline back to QUEUED.

    :returns: 0, for the shape of the other ticks.
    :rtype: int
    """
    call_command("cleanup_processing_tmp", uploads_only=True)
    return 0


@dataclass(frozen=True)
class Tick:
    """One entry of the daemon's schedule under Celery.

    :ivar name: The schedule entry's name, and the log's.
    :ivar task: The task to send.
    :ivar setting: The settings name of the interval, in seconds.
    :ivar queue: The queue to send it on.
    """

    name: str
    task: object
    setting: str
    queue: str

    def interval(self) -> float:
        """Return the interval, read from the settings at call time.

        :returns: Seconds between sends.
        :rtype: float
        """
        return float(getattr(settings, self.setting))

    def send(self) -> bool:
        """Send the tick, with its interval as its expiry.

        :returns: Whether it was sent; a full queue (:func:`has_room`)
            or a broker fault is a warning.
        :rtype: bool
        """
        if not has_room(self.queue):
            return False
        try:
            self.task.apply_async(queue=self.queue, expires=self.interval())
        except Exception:
            _broker_failed()
            logger.warning(
                "could not send the %s tick", self.name, exc_info=True
            )
            return False
        return True


#: The daemon's schedule as tick tasks, in the order the daemon sends
#: them; the order matters nothing to the workers.
TICKS: tuple[Tick, ...] = (
    Tick("claim_scans", claim_scans, "DAEMON_POLL_INTERVAL", QUEUE_HEAVY),
    Tick("enqueue_runs", enqueue_runs, "DAEMON_SUBMIT_INTERVAL", QUEUE_FAST),
    Tick("submit_runpod", submit_runpod, "DAEMON_SUBMIT_INTERVAL", QUEUE_FAST),
    Tick("submit_doctor", submit_doctor, "DAEMON_SUBMIT_INTERVAL", QUEUE_IO),
    Tick(
        "submit_mistral", submit_mistral, "DAEMON_SUBMIT_INTERVAL", QUEUE_HEAVY
    ),
    Tick("sweep_jobs", sweep_jobs, "DAEMON_COLLECT_INTERVAL", QUEUE_FAST),
    Tick(
        "merge_conversions",
        merge_conversions,
        "DAEMON_COLLECT_INTERVAL",
        QUEUE_HEAVY,
    ),
    Tick("glue_runs", glue_runs, "DAEMON_COLLECT_INTERVAL", QUEUE_FAST),
    Tick(
        "prepare_opinions",
        prepare_opinions,
        "DAEMON_COLLECT_INTERVAL",
        QUEUE_OPINIONS,
    ),
    Tick("score_pages", score_pages, "DAEMON_BADPAGE_INTERVAL", QUEUE_HEAVY),
    Tick(
        "cut_figures",
        cut_figures,
        "DAEMON_OPINION_FIGURE_INTERVAL",
        QUEUE_OPINIONS,
    ),
    Tick(
        "build_opinion_pdfs",
        build_opinion_pdfs,
        "DAEMON_OPINION_PDF_INTERVAL",
        QUEUE_OPINIONS,
    ),
    Tick(
        "recover_uploads",
        recover_uploads,
        "PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS",
        QUEUE_FAST,
    ),
    # Every pod has its own /tmp, so every queue sweeps its own.
    *(
        Tick(
            f"cleanup_tmp_{queue}",
            cleanup_tmp,
            "PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS",
            queue,
        )
        for queue in QUEUES
    ),
)


def nudge(*ticks) -> int:
    """Send the named tick tasks now, because their input just landed.

    Each goes to its own queue with its interval as its expiry, like a
    scheduled send; a broker fault is a warning, because the daemon
    sends the tick again on the interval anyway. A no-op with the
    switch off, so the daemon's own run of a pass sends nothing.

    :param ticks: Tick tasks (``glue_runs``, ...).
    :returns: How many were sent.
    :rtype: int
    """
    if not settings.CELERY_ENABLED:
        return 0
    by_task = {tick.task: tick for tick in TICKS}
    return sum(1 for task in ticks if by_task[task].send())


def _nudge_after(action: str) -> None:
    """Send the passes that read what a scan action wrote.

    :param action: The ``QueuedAction`` that just ran.
    :return: None.
    """
    from scanning.models import QueuedAction

    following = {
        QueuedAction.FULL_PIPELINE: (
            enqueue_runs,
            submit_doctor,
            submit_runpod,
        ),
        QueuedAction.APPLY_PAGE_EDITS: (glue_runs,),
        QueuedAction.CREATE_OPINIONS: (
            prepare_opinions,
            build_opinion_pdfs,
            cut_figures,
        ),
    }
    nudge(*following.get(action, ()))


# -- the dispatch tasks: a person pressed a button ----------------------


@app.task(name="scanning.tasks.run_scan", queue=QUEUE_HEAVY)
def run_scan(scan_pk: int, action: str) -> bool:
    """Claim the scan a view queued, if it is still queued for the
    action the view wrote, and run it.

    :param scan_pk: The scan the view moved to QUEUED.
    :param action: The ``QueuedAction`` it wrote.
    :returns: Whether this task ran it. ``False`` when a tick, or a
        second delivery of this message, took it first, or when the
        scan was queued again for another action since.
    :rtype: bool
    """
    from scanning.management.commands import process_next_scan

    _fresh_connection()
    claimed = process_next_scan.claim_next(scan_pk, action)
    if claimed is None:
        logger.info(
            "scan %s was not QUEUED for %s when its task ran", scan_pk, action
        )
        return False
    scan, action = claimed
    process_next_scan.dispatch(scan, action)
    _nudge_after(action)
    return True


def dispatch_scan(scan_pk: int, action: str) -> bool:
    """Send the task that runs a scan a view just queued.

    Called after the view's compare-and-swap to QUEUED, never before:
    the task claims by the same swap, and a scan that is not QUEUED
    for ``action`` when it runs is left alone. The message expires
    after ``DAEMON_PROCESSING_TIMEOUT``, so one from before an outage
    never claims a scan queued much later: the tick claims that one.
    A no-op when Celery is off, and a logged warning when the broker
    is unreachable, because the tick finds the row either way.

    :param scan_pk: The scan that is now QUEUED.
    :param action: The ``QueuedAction`` the caller wrote.
    :returns: Whether a task was sent.
    :rtype: bool
    """
    if not settings.CELERY_ENABLED or not has_room(QUEUE_HEAVY):
        return False
    try:
        run_scan.apply_async(
            (scan_pk, action),
            queue=QUEUE_HEAVY,
            expires=float(settings.DAEMON_PROCESSING_TIMEOUT),
        )
    except Exception:
        _broker_failed()
        logger.warning(
            "could not send the task of scan %s; the tick will claim it",
            scan_pk,
            exc_info=True,
        )
        return False
    return True


_SUBMITTERS = {
    "runpod": submit_runpod,
    "doctor": submit_doctor,
    "mistral": submit_mistral,
}


def submit_soon(provider: str) -> bool:
    """Send the wave of a provider now, after a button created its rows.

    The read buttons (``start_dots_mocr``, ``start_mistral_ocr``,
    ``start_surya_ocr``, ``start_caselaw_tagger``) create PENDING rows
    the next submit tick would send; this sends that tick now. Same
    rules as :func:`dispatch_scan`.

    :param provider: A ``JobProvider`` value.
    :returns: Whether a task was sent.
    :rtype: bool
    """
    task = _SUBMITTERS.get(provider)
    if task is None:
        return False
    return bool(nudge(task))


# -- the worker's start --------------------------------------------------


@worker_ready.connect
def _release_mirrors_at_start(sender=None, **kwargs) -> None:
    """Free the opinion mirrors when a worker that serves ``opinions``
    starts, where the daemon freed them (``run_daemon`` skips it with
    the switch on). Best effort, like the daemon's.

    :param sender: The worker's consumer.
    :param kwargs: The signal's other arguments, unused.
    :return: None.
    """
    try:
        queues = {q.name for q in sender.task_consumer.queues}
    except Exception:
        queues = set()
    if QUEUE_OPINIONS not in queues:
        return
    from scanning import opinion_pdf

    try:
        _fresh_connection()
        opinion_pdf.release_mirrors()
    except Exception:
        logger.exception("Could not release the opinion PDF mirrors")
