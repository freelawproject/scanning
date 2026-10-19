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
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection

logger = logging.getLogger(__name__)

#: The ``classid`` every lock of this module uses; ``final_xml`` keeps
#: its one-argument lock apart from these.
NAMESPACE = 503


def key(name: str) -> int:
    """Return the ``objid`` of a named lock.

    :param name: The lock's name.
    :returns: A signed 32-bit key.
    :rtype: int
    """
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


@contextmanager
def held(name: str, wait: bool = False) -> Iterator[bool]:
    """Hold the named lock for the block, if it can be had.

    :param name: The lock's name.
    :param wait: Block until the lock is free, instead of answering
        ``False`` at once. For a short pass that must run after the
        one holding the lock, never for a tick.
    :returns: (as the context value) Whether this call holds the lock.
        With ``wait`` it is always ``True``.
    """
    fn = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {fn}(%s, %s)", [NAMESPACE, key(name)])
        got = cursor.fetchone()[0] if not wait else True
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", [NAMESPACE, key(name)]
                )
