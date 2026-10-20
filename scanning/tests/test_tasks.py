"""Tests for the Celery layer (#503): the tasks, the tick table, the
locks, the nudges and the dispatch helpers.

No broker runs in a test. The tick tasks are called as functions and
their passes patched; the sends are checked against ``apply_async``
patched out.
"""

from unittest.mock import patch

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings

from scanning import locks, tasks
from scanning.factories import ScanFactory
from scanning.jobs import SubmitSummary
from scanning.models import JobProvider, QueuedAction, Scan, Status

TICK_TASKS = (
    tasks.claim_scans,
    tasks.enqueue_runs,
    tasks.submit_runpod,
    tasks.submit_doctor,
    tasks.submit_mistral,
    tasks.sweep_jobs,
    tasks.merge_conversions,
    tasks.glue_runs,
    tasks.prepare_opinions,
    tasks.score_pages,
    tasks.cut_figures,
    tasks.build_opinion_pdfs,
    tasks.cleanup_tmp,
)


def _no_connection_reset(test):
    """Keep the test transaction: the tasks open a fresh connection
    per run, as the tick commands do, and their tests patch it too."""
    patcher = patch("scanning.tasks.connections.close_all")
    patcher.start()
    test.addCleanup(patcher.stop)


class TestTicks(TestCase):
    """``TICKS`` is the daemon's schedule."""

    def setUp(self):
        patcher = patch("scanning.tasks.queue_depth", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_tick_task_is_in_the_table_on_a_known_queue(self):
        tasks_in_table = {tick.task for tick in tasks.TICKS}
        for task in TICK_TASKS:
            with self.subTest(task=task.name):
                self.assertIn(task, tasks_in_table)
        for tick in tasks.TICKS:
            with self.subTest(tick=tick.name):
                self.assertIn(tick.queue, tasks.QUEUES)
                self.assertIn(tick.task.queue, tasks.QUEUES)

    def test_the_tmp_sweep_is_sent_to_every_queue(self):
        """Every pod has its own /tmp."""
        queues = {t.queue for t in tasks.TICKS if t.task is tasks.cleanup_tmp}
        self.assertEqual(queues, set(tasks.QUEUES))

    @override_settings(DAEMON_COLLECT_INTERVAL=42)
    def test_the_interval_comes_from_the_settings_at_send_time(self):
        tick = next(t for t in tasks.TICKS if t.name == "sweep_jobs")
        self.assertEqual(tick.interval(), 42.0)
        with patch.object(tasks.sweep_jobs, "apply_async") as send:
            self.assertTrue(tick.send())
        # Its interval is its expiry: a stale tick is dropped, never
        # replayed behind a busy worker.
        send.assert_called_once_with(queue=tasks.QUEUE_FAST, expires=42.0)

    def test_a_broker_fault_on_a_send_is_a_warning(self):
        tick = next(t for t in tasks.TICKS if t.name == "sweep_jobs")
        with (
            patch.object(
                tasks.sweep_jobs, "apply_async", side_effect=OSError("down")
            ),
            self.assertLogs("scanning.tasks", level="WARNING"),
        ):
            self.assertFalse(tick.send())

    def test_every_task_is_registered_on_the_app(self):
        from scanning.celery_init import app

        for task in (*TICK_TASKS, tasks.run_scan):
            with self.subTest(task=task.name):
                self.assertEqual(app.tasks[task.name].name, task.name)


class TestThrottle(TestCase):
    """A send is skipped while its queue is at the depth cap."""

    def setUp(self):
        _no_connection_reset(self)

    @override_settings(CELERY_ENABLED=True, CELERY_QUEUE_MAX_DEPTH=10)
    def test_a_full_queue_skips_the_tick_and_the_dispatch(self):
        tick = next(t for t in tasks.TICKS if t.name == "glue_runs")
        with (
            patch("scanning.tasks.queue_depth", return_value=10),
            patch.object(tasks.glue_runs, "apply_async") as send_tick,
            patch.object(tasks.run_scan, "apply_async") as send_scan,
            self.assertLogs("scanning.tasks", level="WARNING"),
        ):
            self.assertFalse(tick.send())
            self.assertFalse(
                tasks.dispatch_scan(7, QueuedAction.FULL_PIPELINE)
            )
        send_tick.assert_not_called()
        send_scan.assert_not_called()

    @override_settings(CELERY_ENABLED=True, CELERY_QUEUE_MAX_DEPTH=10)
    def test_a_queue_under_the_cap_sends(self):
        tick = next(t for t in tasks.TICKS if t.name == "glue_runs")
        with (
            patch("scanning.tasks.queue_depth", return_value=9),
            patch.object(tasks.glue_runs, "apply_async") as send_tick,
        ):
            self.assertTrue(tick.send())
        send_tick.assert_called_once()

    @override_settings(CELERY_ENABLED=True, CELERY_QUEUE_MAX_DEPTH=10)
    def test_an_unreadable_depth_sends(self):
        """The throttle never holds work back on a broker it cannot ask."""
        tick = next(t for t in tasks.TICKS if t.name == "glue_runs")
        with (
            patch("scanning.tasks.queue_depth", return_value=None),
            patch.object(tasks.glue_runs, "apply_async") as send_tick,
        ):
            self.assertTrue(tick.send())
        send_tick.assert_called_once()

    def test_a_broker_fault_reads_as_unknown_depth(self):
        with (
            patch.object(
                tasks.app, "connection_for_read", side_effect=OSError("down")
            ),
            self.assertLogs("scanning.tasks", level="WARNING"),
        ):
            self.assertIsNone(tasks.queue_depth("fast"))


class TestLocks(TransactionTestCase):
    """A locked tick never overlaps itself."""

    def test_a_held_lock_is_seen_from_another_session(self):
        other = connections.create_connection("default")
        self.addCleanup(other.close)
        with locks.held("spec") as held:
            self.assertTrue(held)
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [locks.NAMESPACE, locks.key("spec")],
                )
                self.assertFalse(cursor.fetchone()[0])
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                [locks.NAMESPACE, locks.key("spec")],
            )
            self.assertTrue(cursor.fetchone()[0])
            cursor.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                [locks.NAMESPACE, locks.key("spec")],
            )

    def test_a_locked_tick_skips_its_turn_while_another_holds_the_lock(self):
        other = connections.create_connection("default")
        self.addCleanup(other.close)
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                [locks.NAMESPACE, locks.key("glue_runs")],
            )
        try:
            with (
                patch("scanning.ticks.glue_runs") as passes,
                patch("scanning.tasks.connections.close_all"),
            ):
                self.assertEqual(tasks.glue_runs(), 0)
            passes.assert_not_called()
        finally:
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [locks.NAMESPACE, locks.key("glue_runs")],
                )

    def test_a_locked_body_runs_under_its_lock(self):
        """The fresh connection is opened before the lock, never inside
        the body, or the session that holds the lock would be closed
        under the running pass."""
        other = connections.create_connection("default")
        self.addCleanup(other.close)
        seen = []

        @tasks.locked("probe")
        def body():
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [locks.NAMESPACE, locks.key("probe")],
                )
                seen.append(cursor.fetchone()[0])
            return 1

        self.assertEqual(body(), 1)
        self.assertEqual(seen, [False])
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                [locks.NAMESPACE, locks.key("probe")],
            )
            self.assertTrue(cursor.fetchone()[0])
            cursor.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                [locks.NAMESPACE, locks.key("probe")],
            )

    def test_the_three_opinion_passes_share_one_lock(self):
        """They share the local mirror, and one of them releases it."""
        other = connections.create_connection("default")
        self.addCleanup(other.close)
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                [locks.NAMESPACE, locks.key(tasks.OPINION_MIRROR_LOCK)],
            )
        try:
            with (
                patch("scanning.opinion_pdf.run_tick") as pdfs,
                patch("scanning.opinion_figures.run_tick") as figures,
                patch("scanning.ticks.prepare_opinions") as prepare,
                patch("scanning.tasks.connections.close_all"),
            ):
                self.assertEqual(tasks.build_opinion_pdfs(), 0)
                self.assertEqual(tasks.cut_figures(), 0)
                self.assertEqual(tasks.prepare_opinions(), 0)
            pdfs.assert_not_called()
            figures.assert_not_called()
            prepare.assert_not_called()
        finally:
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [locks.NAMESPACE, locks.key(tasks.OPINION_MIRROR_LOCK)],
                )


class TestNudges(TestCase):
    """A finished step sends the pass that reads what it wrote."""

    def setUp(self):
        _no_connection_reset(self)
        patcher = patch("scanning.tasks.queue_depth", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_is_sent_while_celery_is_off(self):
        with patch.object(tasks.glue_runs, "apply_async") as send:
            self.assertEqual(tasks.nudge(tasks.glue_runs), 0)
        send.assert_not_called()

    @override_settings(CELERY_ENABLED=True)
    def test_a_completed_job_sends_the_merge_and_the_glues(self):
        from scanning.jobs import SweepSummary

        with (
            patch(
                "scanning.ticks.sweep", return_value=SweepSummary(completed=1)
            ),
            patch.object(tasks.merge_conversions, "apply_async") as merge,
            patch.object(tasks.glue_runs, "apply_async") as glue,
        ):
            self.assertEqual(tasks.sweep_jobs(), 1)
        merge.assert_called_once()
        glue.assert_called_once()
        self.assertEqual(merge.call_args.kwargs["queue"], tasks.QUEUE_HEAVY)

    @override_settings(CELERY_ENABLED=True)
    def test_a_sweep_that_completed_nothing_sends_nothing(self):
        from scanning.jobs import SweepSummary

        with (
            patch("scanning.ticks.sweep", return_value=SweepSummary()),
            patch.object(tasks.merge_conversions, "apply_async") as merge,
        ):
            self.assertEqual(tasks.sweep_jobs(), 0)
        merge.assert_not_called()

    @override_settings(CELERY_ENABLED=True)
    def test_the_opinion_creation_sends_the_opinion_passes(self):
        scan = ScanFactory(
            status=Status.QUEUED, queued_action=QueuedAction.CREATE_OPINIONS
        )
        with (
            patch("scanning.management.commands.process_next_scan.dispatch"),
            patch.object(tasks.prepare_opinions, "apply_async") as prepare,
            patch.object(tasks.build_opinion_pdfs, "apply_async") as pdfs,
            patch.object(tasks.cut_figures, "apply_async") as figures,
        ):
            self.assertTrue(
                tasks.run_scan(scan.pk, QueuedAction.CREATE_OPINIONS)
            )
        for send in (prepare, pdfs, figures):
            send.assert_called_once()


class TestDispatch(TestCase):
    """``dispatch_scan`` and ``submit_soon``."""

    def setUp(self):
        patcher = patch("scanning.tasks.queue_depth", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_is_sent_while_celery_is_off(self):
        with patch.object(tasks.run_scan, "apply_async") as send:
            self.assertFalse(
                tasks.dispatch_scan(1, QueuedAction.FULL_PIPELINE)
            )
            self.assertFalse(tasks.submit_soon(JobProvider.DOCTOR))
        send.assert_not_called()

    @override_settings(CELERY_ENABLED=True)
    def test_the_scan_task_is_sent_to_the_heavy_queue(self):
        with patch.object(tasks.run_scan, "apply_async") as send:
            self.assertTrue(
                tasks.dispatch_scan(7, QueuedAction.COMPUTE_REDACTIONS)
            )
        send.assert_called_once_with(
            (7, QueuedAction.COMPUTE_REDACTIONS),
            queue=tasks.QUEUE_HEAVY,
            expires=3600.0,
        )

    @override_settings(CELERY_ENABLED=True)
    def test_a_broker_fault_is_a_warning_and_not_an_error(self):
        """The tick claims the row anyway."""
        with (
            patch.object(
                tasks.run_scan, "apply_async", side_effect=OSError("down")
            ),
            self.assertLogs("scanning.tasks", level="WARNING"),
        ):
            self.assertFalse(
                tasks.dispatch_scan(7, QueuedAction.FULL_PIPELINE)
            )

    @override_settings(CELERY_ENABLED=True)
    def test_a_provider_wave_is_sent_to_its_own_queue(self):
        with patch.object(tasks.submit_doctor, "apply_async") as send:
            self.assertTrue(tasks.submit_soon(JobProvider.DOCTOR))
        self.assertEqual(send.call_args.kwargs["queue"], tasks.QUEUE_IO)

    @override_settings(CELERY_ENABLED=True)
    def test_an_unknown_provider_sends_nothing(self):
        self.assertFalse(tasks.submit_soon("nobody"))


class TestRunScan(TestCase):
    """``run_scan`` claims by compare-and-swap, like the tick."""

    def setUp(self):
        _no_connection_reset(self)

    def test_it_claims_and_runs_a_queued_scan(self):
        scan = ScanFactory(
            status=Status.QUEUED, queued_action=QueuedAction.CREATE_OPINIONS
        )
        with patch(
            "scanning.management.commands.process_next_scan.dispatch"
        ) as dispatch:
            self.assertTrue(
                tasks.run_scan(scan.pk, QueuedAction.CREATE_OPINIONS)
            )
        dispatch.assert_called_once()
        claimed, action = dispatch.call_args.args
        self.assertEqual(claimed.pk, scan.pk)
        self.assertEqual(action, QueuedAction.CREATE_OPINIONS)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)
        self.assertIsNotNone(scan.processed_at)

    def test_a_scan_a_tick_took_first_is_left_alone(self):
        scan = ScanFactory(status=Status.PROCESSING)
        with patch(
            "scanning.management.commands.process_next_scan.dispatch"
        ) as dispatch:
            self.assertFalse(
                tasks.run_scan(scan.pk, QueuedAction.CREATE_OPINIONS)
            )
        dispatch.assert_not_called()

    def test_a_scan_queued_again_for_another_action_is_left_alone(self):
        """A message from before an admin re-queue never runs the old
        action."""
        scan = ScanFactory(
            status=Status.QUEUED, queued_action=QueuedAction.FULL_PIPELINE
        )
        with patch(
            "scanning.management.commands.process_next_scan.dispatch"
        ) as dispatch:
            self.assertFalse(
                tasks.run_scan(scan.pk, QueuedAction.CREATE_OPINIONS)
            )
        dispatch.assert_not_called()
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)

    def test_a_blank_action_is_the_full_pipeline(self):
        scan = ScanFactory(status=Status.QUEUED, queued_action="")
        with patch(
            "scanning.management.commands.process_next_scan.dispatch"
        ) as dispatch:
            self.assertTrue(
                tasks.run_scan(scan.pk, QueuedAction.FULL_PIPELINE)
            )
        dispatch.assert_called_once()

    def test_the_tick_task_claims_the_next_by_rank(self):
        ScanFactory(
            status=Status.QUEUED, queued_action=QueuedAction.APPLY_PAGE_EDITS
        )
        upload = ScanFactory(status=Status.QUEUED, queued_action="")
        with patch(
            "scanning.management.commands.process_next_scan.dispatch"
        ) as dispatch:
            self.assertEqual(tasks.claim_scans(), 1)
        claimed, _ = dispatch.call_args.args
        self.assertEqual(claimed.pk, upload.pk)

    def test_the_tick_task_runs_nothing_when_the_queue_is_empty(self):
        self.assertEqual(tasks.claim_scans(), 0)


class TestWaves(TestCase):
    """One wave task per provider."""

    def setUp(self):
        _no_connection_reset(self)

    def test_each_wave_task_runs_its_provider_alone(self):
        for task, provider in (
            (tasks.submit_runpod, JobProvider.RUNPOD),
            (tasks.submit_doctor, JobProvider.DOCTOR),
            (tasks.submit_mistral, JobProvider.MISTRAL),
        ):
            with (
                self.subTest(provider=provider),
                patch(
                    "scanning.jobs.submit_pending",
                    return_value=SubmitSummary(submitted=2),
                ) as submit,
            ):
                self.assertEqual(task(), 2)
            submit.assert_called_once_with(providers=(provider,))


class TestQueuedWritersDispatch(TestCase):
    """Every writer of QUEUED sends the task after its swap."""

    @override_settings(CELERY_ENABLED=True)
    def test_the_redaction_compute_button_dispatches(self):
        from scanning import services

        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        with patch("scanning.tasks.dispatch_scan") as dispatch:
            queued, _ = services.queue_redaction_compute(scan)
        self.assertTrue(queued)
        dispatch.assert_called_once_with(
            scan.pk, QueuedAction.COMPUTE_REDACTIONS
        )

    @override_settings(CELERY_ENABLED=True)
    def test_a_lost_swap_dispatches_nothing(self):
        from scanning import opinions

        scan = ScanFactory(status=Status.PROCESSING)
        with patch("scanning.tasks.dispatch_scan") as dispatch:
            self.assertEqual(opinions.queue_create_opinions(scan), "")
        dispatch.assert_not_called()

    @override_settings(CELERY_ENABLED=True)
    def test_the_approval_dispatches(self):
        from scanning import opinions

        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        with (
            patch(
                "scanning.detections.changed_since_compute", return_value=False
            ),
            patch("scanning.tasks.dispatch_scan") as dispatch,
        ):
            self.assertEqual(
                opinions.queue_create_opinions(scan),
                QueuedAction.CREATE_OPINIONS,
            )
        dispatch.assert_called_once_with(scan.pk, QueuedAction.CREATE_OPINIONS)
        self.assertEqual(Scan.objects.get(pk=scan.pk).status, Status.QUEUED)
