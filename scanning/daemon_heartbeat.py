"""The daemon's heartbeat (#503).

``run_daemon`` stamps one cache entry from its loop, at most every
:data:`INTERVAL_SECONDS`, and ``monitoring.daemon_heartbeat`` answers
500 when the stamp is older than the mode allows. The stamp is written
by the loop itself and never by a thread, so it says the clock turns,
not only that the process lives: a loop stuck in a send or a pass stops
beating.

The store is Django's default cache (the database), not the broker,
so the beat works in both modes. The two modes have two limits:

- ``celery``: the loop only sends ticks, each a second or two at most
  (the broker's socket timeouts), so a stamp older than
  :data:`CELERY_STALE_SECONDS` is a dead or stuck clock.
- ``serial``: the loop runs the passes, and one pass can hold it for
  minutes, so the limit is ``DAEMON_PROCESSING_TIMEOUT``, the age at
  which the stale sweep would call a scan's action dead too.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache

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

    :param mode: :data:`MODE_CELERY` or :data:`MODE_SERIAL`.
    :return: None.
    """
    try:
        cache.set(KEY, {"mode": mode, "at": time.time()}, TIMEOUT_SECONDS)
    except Exception:
        logger.warning("could not write the daemon heartbeat", exc_info=True)


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

    :returns: ``{"mode", "at"}``, or ``None``.
    :rtype: dict | None
    """
    return cache.get(KEY)
