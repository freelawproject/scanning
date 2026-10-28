"""Tests for the Celery monitoring views and the workers' heartbeat
(#503). No broker runs: the pool's connection is a fake whose Redis
client is a dict, answering ``bytes`` as the real client does."""

import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from celery.worker.consumer import Consumer
from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from scanning import daemon_heartbeat, tasks, worker_heartbeat
from scanning.celery_init import app


class FakeRedis:
    """The few Redis calls the heartbeat makes, over a dict."""

    def __init__(self):
        self.data = {}
        self.ttls = {}

    def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttls[key] = ex

    def get(self, key):
        if isinstance(key, bytes):
            key = key.decode()
        value = self.data.get(key)
        return None if value is None else value.encode()

    def delete(self, key):
        self.data.pop(key, None)

    def scan_iter(self, match, count=None):
        prefix = match.rstrip("*")
        return [k.encode() for k in list(self.data) if k.startswith(prefix)]


def _fake_pool(client):
    connection = MagicMock()
    connection.default_channel.client = client

    @contextmanager
    def acquire(block=True):
        yield connection

    return patch.object(app.pool, "acquire", side_effect=acquire)


class TestHeartbeat(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = _fake_pool(self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_beat_is_a_key_that_expires(self):
        worker_heartbeat.beat(app, "celery@a", ["heavy", "fast"])
        key = worker_heartbeat.KEY_PREFIX + "celery@a"
        self.assertEqual(
            json.loads(self.redis.data[key])["queues"], ["fast", "heavy"]
        )
        self.assertEqual(self.redis.ttls[key], worker_heartbeat.TTL_SECONDS)

    def test_a_beat_never_raises(self):
        with (
            patch.object(app.pool, "acquire", side_effect=OSError("down")),
            self.assertLogs("scanning.worker_heartbeat", level="WARNING"),
        ):
            worker_heartbeat.beat(app, "celery@a", ["fast"])

    def test_the_step_beats_on_start_and_forgets_on_shutdown(self):
        consumer = MagicMock()
        consumer.app = app
        consumer.hostname = "celery@a"
        queue = MagicMock()
        queue.name = "opinions"
        consumer.task_consumer.queues = [queue]
        step = worker_heartbeat.Heartbeat(consumer)
        step.start(consumer)
        key = worker_heartbeat.KEY_PREFIX + "celery@a"
        self.assertIn(key, self.redis.data)
        interval, scheduled = consumer.timer.call_repeatedly.call_args.args
        self.assertEqual(interval, worker_heartbeat.INTERVAL_SECONDS)
        # The timer's own call writes the key again.
        self.redis.delete(key)
        scheduled()
        self.assertEqual(
            json.loads(self.redis.data[key])["queues"], ["opinions"]
        )
        step.shutdown(consumer)
        consumer.timer.call_repeatedly.return_value.cancel.assert_called_once()
        self.assertNotIn(key, self.redis.data)

    def test_the_step_is_in_the_consumer_blueprint_after_the_tasks(self):
        """A ``requires`` that does not resolve fails only at worker
        boot, so the real blueprint is built here."""
        blueprint = Consumer.Blueprint(steps=app.steps["consumer"])
        blueprint.apply(MagicMock(app=app, steps=[], hostname="celery@a"))
        order = [type(step).__name__ for step in blueprint.order]
        self.assertIn("Heartbeat", order)
        self.assertLess(order.index("Tasks"), order.index("Heartbeat"))

    def test_a_stop_keeps_the_key_for_a_reconnect(self):
        consumer = MagicMock()
        consumer.app = app
        consumer.hostname = "celery@a"
        consumer.task_consumer.queues = []
        step = worker_heartbeat.Heartbeat(consumer)
        step.start(consumer)
        step.stop(consumer)
        self.assertIn(
            worker_heartbeat.KEY_PREFIX + "celery@a", self.redis.data
        )


@override_settings(CELERY_ENABLED=True)
class TestCeleryWorkersView(TestCase):
    def setUp(self):
        tasks.reset_broker_backoff()
        self.addCleanup(tasks.reset_broker_backoff)
        self.redis = FakeRedis()
        patcher = _fake_pool(self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self):
        return self.client.get(reverse("celery_workers"))

    def test_every_queue_served_is_a_200(self):
        worker_heartbeat.beat(app, "celery@a", ["fast", "io"])
        worker_heartbeat.beat(app, "celery@b", ["heavy", "opinions", "fast"])
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["queues"]["fast"], ["celery@a", "celery@b"])
        self.assertEqual(body["missing"], [])

    def test_a_queue_with_no_live_worker_is_a_500(self):
        worker_heartbeat.beat(app, "celery@a", ["fast", "io", "opinions"])
        response = self._get()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["missing"], ["heavy"])

    def test_no_worker_at_all_is_a_500(self):
        response = self._get()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["missing"], list(tasks.QUEUES))

    def test_a_broker_fault_is_a_500(self):
        with patch.object(app.pool, "acquire", side_effect=OSError("down")):
            response = self._get()
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.json())

    def test_a_fault_holds_the_next_probes_off_the_broker(self):
        with (
            patch.object(app.pool, "acquire", side_effect=OSError("down")),
            self.assertLogs("scanning.monitoring", level="WARNING"),
        ):
            self._get()
        with patch.object(app.pool, "acquire") as acquire:
            response = self._get()
        self.assertEqual(response.status_code, 500)
        acquire.assert_not_called()

    @override_settings(CELERY_ENABLED=False)
    def test_the_switch_off_expects_no_worker(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"celery_enabled": False})


@override_settings(CELERY_ENABLED=True)
class TestCeleryQueuesView(TestCase):
    def setUp(self):
        tasks.reset_broker_backoff()
        self.addCleanup(tasks.reset_broker_backoff)

    def test_the_depths_are_a_flat_map(self):
        depths = dict(zip(tasks.QUEUES, range(len(tasks.QUEUES)), strict=True))
        with patch("scanning.tasks.queue_depth", side_effect=depths.get):
            response = self.client.get(reverse("celery_queues"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), depths)

    def test_an_unknown_depth_is_a_500(self):
        with patch("scanning.tasks.queue_depth", return_value=None):
            response = self.client.get(reverse("celery_queues"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), dict.fromkeys(tasks.QUEUES, None))

    def test_the_first_unknown_depth_ends_the_reads(self):
        with patch("scanning.tasks.queue_depth", return_value=None) as depth:
            self.client.get(reverse("celery_queues"))
        depth.assert_called_once_with(tasks.QUEUES[0])

    def test_inside_the_backoff_no_depth_is_read(self):
        tasks._broker_failed()
        with patch("scanning.tasks.queue_depth") as depth:
            response = self.client.get(reverse("celery_queues"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), dict.fromkeys(tasks.QUEUES, None))
        depth.assert_not_called()

    @override_settings(CELERY_ENABLED=False)
    def test_the_switch_off_reads_no_broker(self):
        with patch("scanning.tasks.queue_depth") as depth:
            response = self.client.get(reverse("celery_queues"))
        self.assertEqual(response.json(), {})
        depth.assert_not_called()


class TestDaemonHeartbeatView(TestCase):
    def setUp(self):
        # A beat closes its session, which a test transaction cannot
        # survive.
        patcher = patch("scanning.daemon_heartbeat.connections.close_all")
        self.close_all = patcher.start()
        self.addCleanup(patcher.stop)
        cache.delete(daemon_heartbeat.KEY)
        self.addCleanup(cache.delete, daemon_heartbeat.KEY)

    def _get(self):
        return self.client.get(reverse("daemon_heartbeat"))

    def _stamp(self, mode, age):
        cache.set(
            daemon_heartbeat.KEY, {"mode": mode, "at": time.time() - age}
        )

    def test_a_daemon_that_never_beat_is_a_500(self):
        response = self._get()
        self.assertEqual(response.status_code, 500)
        self.assertIn("never beat", response.json()["error"])

    def test_a_fresh_clock_is_a_200(self):
        daemon_heartbeat.beat(daemon_heartbeat.MODE_CELERY)
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "celery")
        self.assertEqual(
            body["stale_after"], daemon_heartbeat.CELERY_STALE_SECONDS
        )
        self.assertLess(body["age_seconds"], 5)

    def test_a_beat_writes_the_one_entry_again(self):
        daemon_heartbeat.beat(daemon_heartbeat.MODE_CELERY)
        daemon_heartbeat.beat(daemon_heartbeat.MODE_SERIAL)
        self.assertEqual(daemon_heartbeat.read()["mode"], "serial")

    def test_a_clock_past_its_limit_is_a_500(self):
        self._stamp(
            daemon_heartbeat.MODE_CELERY,
            daemon_heartbeat.CELERY_STALE_SECONDS + 1,
        )
        self.assertEqual(self._get().status_code, 500)

    @override_settings(DAEMON_PROCESSING_TIMEOUT=600)
    def test_a_serial_daemon_may_sit_in_a_long_pass(self):
        self._stamp(daemon_heartbeat.MODE_SERIAL, 300)
        self.assertEqual(self._get().status_code, 200)
        self._stamp(daemon_heartbeat.MODE_SERIAL, 601)
        self.assertEqual(self._get().status_code, 500)

    def test_a_database_fault_never_raises_from_the_beat(self):
        with (
            patch("scanning.daemon_heartbeat.cache.set", side_effect=OSError),
            self.assertLogs("scanning.daemon_heartbeat", level="WARNING"),
        ):
            daemon_heartbeat.beat(daemon_heartbeat.MODE_SERIAL)
        self.close_all.assert_called_once_with()

    def test_every_beat_closes_its_session(self):
        """A session held idle between beats is the one a failover
        drops, and with Celery on nothing else in the loop opens one."""
        daemon_heartbeat.beat(daemon_heartbeat.MODE_CELERY)
        self.close_all.assert_called_once_with()

    def test_a_database_fault_is_a_json_500(self):
        """A cache table ``createcachetable`` has not made, or a dead
        database, must not raise an HTML 500 and a Sentry event per
        probe."""
        with (
            patch(
                "scanning.daemon_heartbeat.cache.get",
                side_effect=DatabaseError("no table"),
            ),
            self.assertLogs("scanning.monitoring", level="WARNING"),
        ):
            response = self._get()
        self.assertEqual(response.status_code, 500)
        self.assertIn("database", response.json()["error"])
