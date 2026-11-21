"""The stop request a long step reads between its parts (#534).

A pod is sent SIGTERM and then SIGKILL once its grace period ends. A
step that takes minutes and never looks up (the Mistral render and
upload of a whole shard) is killed in the middle, and whatever it had
handed a third party stays there. So the process that receives the
signal sets this flag, and the step reads it between its parts and
hands its row back while it still can.

Two receivers, one per mode. The daemon's signal handler calls
:func:`request` in the process that runs the passes. A Celery worker
receives the signal in its main process while the task runs in a
forked child, so the main process makes the flag shared before the
pool forks (:func:`share`, from ``worker_init``) and sets it from
``worker_shutting_down``; every child inherits it, a recycled child
too, because each one is forked from that main process.

No Django import, so the signal hooks of ``celery_init`` can load it.
"""

from __future__ import annotations

import multiprocessing
import threading

_local = threading.Event()
_shared = None


def share() -> None:
    """Make the flag visible to the processes this one forks after.

    :return: None.
    """
    global _shared
    _shared = multiprocessing.get_context("fork").Event()


def request() -> None:
    """Ask every long step of this process, and of its children, to stop.

    :return: None.
    """
    _local.set()
    if _shared is not None:
        _shared.set()


def requested() -> bool:
    """Return whether a stop was asked for.

    :returns: Whether this process, or the worker that forked it, is
        shutting down.
    :rtype: bool
    """
    return _local.is_set() or (_shared is not None and _shared.is_set())


def reset() -> None:
    """Forget a stop request; for tests.

    :return: None.
    """
    global _shared
    _local.clear()
    _shared = None
