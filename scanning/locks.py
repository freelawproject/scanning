"""Advisory locks for the passes that must not overlap (#503).

A pass that reads a count and then writes was safe under the serial
daemon because nothing else ran. With the passes on several workers,
each such pass takes a named session lock here before it runs, and a
second copy of it finds the lock held and does nothing: the next tick
runs it. The lock is Postgres's own (``pg_try_advisory_lock``), so it
holds across pods, and a session lock and not a transaction lock, the
shape of ``final_xml.exporter_lock``, because a pass makes S3 calls a
transaction must not span.

A lock is keyed by its name's CRC under one namespace, so a new pass
needs no registry: two names collide only on a CRC collision, which
costs a skipped tick and never a wrong write.

A session lock outlives a client that vanished without closing its
socket (a node failure, a spot reclaim, a partition) for as long as
the server keeps the dead session, which on default TCP keepalives is
about two hours, and every tick under that name would skip meanwhile.
So the locking session sets the server's keepalives first
(:data:`KEEPALIVE`), and an orphaned backend is reaped, and its lock
freed, in about a minute and a half. The blocking path waits at most
:data:`WAIT_TIMEOUT_SECONDS` for the same reason.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import OperationalError, connection

logger = logging.getLogger(__name__)

#: The ``classid`` every lock of this module uses; ``final_xml`` keeps
#: its one-argument lock apart from these.
NAMESPACE = 503

#: The server-side TCP keepalives of a locking session, in seconds:
#: idle before the first probe, between probes, and how many may go
#: unanswered. A dead client's backend ends after idle plus interval
#: times count, about ninety seconds, and its lock goes with it.
KEEPALIVE = {
    "tcp_keepalives_idle": 60,
    "tcp_keepalives_interval": 10,
    "tcp_keepalives_count": 3,
}

#: How long the blocking path waits before it answers ``False``, so a
#: holder's dead session never blocks a pass for longer than the
#: keepalives need to reap it.
WAIT_TIMEOUT_SECONDS = 120


def key(name: str) -> int:
    """Return the ``objid`` of a named lock.

    :param name: The lock's name.
    :returns: A signed 32-bit key.
    :rtype: int
    """
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


def _bound_session(cursor) -> None:
    """Set the keepalives of this session, so a vanished client's lock
    is freed by the server in about a minute and a half.

    Best effort: a server that refuses the setting (a platform with no
    such socket options) keeps the lock semantics and loses the bound,
    which is logged once per connection.

    :param cursor: A cursor of the locking connection.
    :return: None.
    """
    try:
        for name, value in KEEPALIVE.items():
            cursor.execute(f"SET {name} = %s", [value])
    except Exception:
        logger.warning("could not bound the locking session", exc_info=True)


@contextmanager
def held(name: str, wait: bool = False) -> Iterator[bool]:
    """Hold the named lock for the block, if it can be had.

    :param name: The lock's name.
    :param wait: Block until the lock is free, up to
        :data:`WAIT_TIMEOUT_SECONDS`, instead of answering ``False``
        at once. For a short pass that must run after the one holding
        the lock, never for a tick.
    :returns: (as the context value) Whether this call holds the lock.
        With ``wait``, ``False`` only when the wait timed out.
    """
    got = False
    with connection.cursor() as cursor:
        _bound_session(cursor)
        if wait:
            cursor.execute(
                "SET lock_timeout = %s", [f"{WAIT_TIMEOUT_SECONDS}s"]
            )
            try:
                cursor.execute(
                    "SELECT pg_advisory_lock(%s, %s)", [NAMESPACE, key(name)]
                )
                got = True
            except OperationalError:
                logger.warning(
                    "lock %s not had in %ss; skipped",
                    name,
                    WAIT_TIMEOUT_SECONDS,
                )
            finally:
                cursor.execute("SET lock_timeout = 0")
        else:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [NAMESPACE, key(name)]
            )
            got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", [NAMESPACE, key(name)]
                )
