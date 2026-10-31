"""The daemon's heartbeat (#503).

``run_daemon`` stamps one cache entry from its loop, at most every
:data:`INTERVAL_SECONDS` and after every pass it runs, and ``monitoring.daemon_heartbeat_view`` answers 500 when the
stamp is older than the mode allows. The loop writes the stamp between
its passes, so it says the clock turns, not only that the process
lives: a loop stuck in a send or a pass stops beating.

The store is Django's default cache (the database), not the broker,
so the beat works in both modes. Its table is ``createcachetable``,
run once by hand in the deploy's temp pod (the README's production
checklist): the deploy never runs it, and this is the one reader of
the cache, so a missing table shows here alone, as a warning on every
beat and a 500 on the check. The two modes have two limits:

- ``celery``: the loop only sends ticks, each a second or two at most
  (the broker's socket timeouts), so a stamp older than
  :data:`CELERY_STALE_SECONDS` is a dead or stuck clock.
- ``serial``: the loop runs the passes, and a scan action can hold it
  for longer than any limit, so the action's own heartbeat thread
  (``process_next_scan.heartbeat``) stamps here too, with the switch
  off alone: with it on that thread runs in a worker, and a worker's
  beat must never hide a dead clock, and only for
  ``DAEMON_MAX_ACTION_SECONDS``, so a hung action still goes stale. The
  limit is then ``DAEMON_PROCESSING_TIMEOUT``, the rule the stale sweep
  reads off the same thread; the other passes hold the loop for
  minutes at most.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import connections

logger = logging.getLogger(__name__)

KEY = "scanning:daemon:heartbeat"
INTERVAL_SECONDS = 15
CELERY_STALE_SECONDS = 4 * INTERVAL_SECONDS
#: The entry outlives any limit, so a stale beat reads as stale and not
#: as missing; a day is long past both.
TIMEOUT_SECONDS = 24 * 60 * 60

MODE_CELERY = "celery"
MODE_SERIAL = "serial"


def beat(mode: str) -> None:
    """Write the stamp. Never raises: a missed beat is the alarm.

    A beat leaves no session open: with ``CELERY_ENABLED`` on nothing
    else in the loop touches the database, and a session held idle
    between beats is the one a failover or an idle kill drops, so the
    next beat fails too. A fresh connection every fifteen seconds is
    nothing, and the passes open one of their own anyway.

    :param mode: :data:`MODE_CELERY` or :data:`MODE_SERIAL`.
    :return: None.
    """
    try:
        cache.set(KEY, {"mode": mode, "at": time.time()}, TIMEOUT_SECONDS)
    except Exception:
        logger.warning("could not write the daemon heartbeat", exc_info=True)
    finally:
        connections.close_all()


def stale_after(mode: str) -> int:
    """Return how old a stamp of the mode may be.

    :param mode: The mode the daemon wrote.
    :returns: Seconds.
    :rtype: int
    """
    if mode == MODE_CELERY:
        return CELERY_STALE_SECONDS
    return int(settings.DAEMON_PROCESSING_TIMEOUT)


def read() -> dict | None:
    """Return the last stamp, or ``None`` when the daemon never beat.

    :returns: ``{"mode", "age_seconds"}``, or ``None``.
    :rtype: dict | None
    :raises DatabaseError: When the cache table cannot be read.
    """
    stamp = cache.get(KEY)
    if stamp is None:
        return None
    age = time.time() - float(stamp.get("at", 0))
    return {
        "mode": stamp.get("mode", MODE_SERIAL),
        "age_seconds": max(0.0, age),
    }
