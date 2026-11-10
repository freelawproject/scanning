"""The Celery workers' heartbeat (#503).

Every worker's main process writes one key on the broker,
``scanning:celery:worker:{hostname}``, every
:data:`INTERVAL_SECONDS`, holding the queues it consumes, and the key
lives :data:`TTL_SECONDS`. So a live worker is a key, a worker that
died or lost the broker is a key that expired, and the monitoring
view (``monitoring.celery_workers``) reads which queues have a live
consumer with one ``SCAN`` and no broadcast: a request costs no
round-trip to every worker, and a busy pool still beats, because the
timer runs in the main process and never behind a task.

The beat says a worker is up and listening. It does not say the
queue moves: a pool whose every child is stuck still beats, and that
shows as a depth that grows (``monitoring.celery_queues``).
"""

from __future__ import annotations

import json
import logging
import time

from celery import bootsteps

logger = logging.getLogger(__name__)

KEY_PREFIX = "scanning:celery:worker:"
INTERVAL_SECONDS = 15
#: Three missed beats, so one slow write never reads as a dead worker.
TTL_SECONDS = 3 * INTERVAL_SECONDS


def _redis_client(connection):
    """Return the Redis client under a broker connection, or ``None``
    on another transport.

    :param connection: A ``kombu.Connection``.
    :returns: A ``redis.Redis``, or ``None``.
    """
    client = getattr(connection.default_channel, "client", None)
    if client is None or not hasattr(client, "set"):
        return None
    return client


def beat(app, hostname: str, queues: list[str]) -> None:
    """Write one beat. Never raises: a missed beat is the alarm.

    :param app: The Celery app, for its connection pool.
    :param hostname: The worker's node name (``celery@pod``).
    :param queues: The queue names the worker consumes.
    :return: None.
    """
    payload = json.dumps(
        {"hostname": hostname, "queues": sorted(queues), "at": time.time()}
    )
    try:
        with app.pool.acquire(block=True) as connection:
            client = _redis_client(connection)
            if client is None:
                return
            client.set(KEY_PREFIX + hostname, payload, ex=TTL_SECONDS)
    except Exception:
        logger.warning(
            "could not write the heartbeat of %s", hostname, exc_info=True
        )


def forget(app, hostname: str) -> None:
    """Delete a worker's key on a clean shutdown, so the view does not
    count it for :data:`TTL_SECONDS` after it left. Best effort.

    :param app: The Celery app.
    :param hostname: The worker's node name.
    :return: None.
    """
    try:
        with app.pool.acquire(block=True) as connection:
            client = _redis_client(connection)
            if client is not None:
                client.delete(KEY_PREFIX + hostname)
    except Exception:
        logger.warning(
            "could not delete the heartbeat of %s", hostname, exc_info=True
        )


def live_workers(app) -> list[dict] | None:
    """Return the beats of the live workers, or ``None`` when the
    broker cannot say.

    :param app: The Celery app.
    :returns: One ``{"hostname", "queues", "at"}`` per live worker.
    :rtype: list[dict] | None
    """
    with app.pool.acquire(block=True) as connection:
        client = _redis_client(connection)
        if client is None:
            return None
        workers = []
        for key in client.scan_iter(match=KEY_PREFIX + "*", count=100):
            raw = client.get(key)
            if raw is None:
                # Expired between the scan and the read.
                continue
            try:
                workers.append(json.loads(raw))
            except ValueError:
                logger.warning("unreadable heartbeat under %r", key)
        return workers


class Heartbeat(bootsteps.StartStopStep):
    """A consumer bootstep that writes :func:`beat` on the worker's
    timer. It needs the task consumer, which knows the queues; a
    reconnect stops and starts the consumer's steps, so the beat
    follows the broker connection.
    """

    requires = {"celery.worker.consumer.tasks:Tasks"}

    def __init__(self, c, **kwargs):
        self.tref = None
        super().__init__(c, **kwargs)

    def start(self, c) -> None:
        """Beat now and every :data:`INTERVAL_SECONDS`.

        :param c: The worker's consumer.
        :return: None.
        """
        queues = [q.name for q in c.task_consumer.queues]

        def _beat() -> None:
            beat(c.app, c.hostname, queues)

        _beat()
        self.tref = c.timer.call_repeatedly(INTERVAL_SECONDS, _beat)

    def stop(self, c) -> None:
        """Stop the timer, keeping the key: a reconnect beats again.

        :param c: The worker's consumer.
        :return: None.
        """
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None

    def shutdown(self, c) -> None:
        """Stop the timer and delete the key.

        :param c: The worker's consumer.
        :return: None.
        """
        self.stop(c)
        forget(c.app, c.hostname)
