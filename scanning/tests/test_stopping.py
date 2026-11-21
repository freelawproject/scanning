"""Tests for ``scanning.stopping``, the stop request of a long step (#534)."""

import multiprocessing

from django.test import SimpleTestCase

from scanning import stopping


def _report(flag_seen, go):
    go.wait(10)
    flag_seen.value = stopping.requested()


class TestStopping(SimpleTestCase):
    """A stop asked for in one process reaches the steps that read it."""

    def setUp(self):
        stopping.reset()
        self.addCleanup(stopping.reset)

    def test_a_request_is_read_until_reset(self):
        self.assertFalse(stopping.requested())
        stopping.request()
        self.assertTrue(stopping.requested())
        stopping.reset()
        self.assertFalse(stopping.requested())

    def test_a_shared_flag_reaches_a_child_forked_before_the_request(self):
        # A Celery worker's main process receives the signal while the
        # task runs in a child it forked earlier.
        stopping.share()
        context = multiprocessing.get_context("fork")
        flag_seen = context.Value("b", 0)
        go = context.Event()
        child = context.Process(target=_report, args=(flag_seen, go))
        child.start()
        stopping.request()
        go.set()
        child.join(10)
        self.assertEqual(child.exitcode, 0)
        self.assertTrue(flag_seen.value)

    def test_the_worker_signals_share_and_set_the_flag(self):
        from scanning import celery_init

        celery_init._share_the_stop_flag()
        self.assertFalse(stopping.requested())
        celery_init._ask_the_tasks_to_stop()
        self.assertTrue(stopping.requested())
