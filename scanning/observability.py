"""Web-worker observability: in-flight-request and concurrency logging.

Closes the attribution gaps left by the worker-timeout instrumentation in
:mod:`scanning.workers` (see issue #115). All web views are sync ``def`` running
in the asgiref threadpool, so a handful of heavy requests (PDF render, S3 pull,
blackletter generation) can occupy every worker and starve the event loop — the
worker then misses its gunicorn heartbeat (WORKER TIMEOUT / SCANNING-1V) and the
kubelet's liveness probe times out and kills the pod. Neither the gunicorn access
log (which writes only on response *completion*, so a request that hangs until
the worker dies never logs) nor faulthandler (which names the view function but
not the request context) tells you *which* request saturated the workers.

This closes that:

- A middleware records each in-flight request in a shared registry, keyed by the
  serving thread id, with its URL, ``scan_pk`` and user (see
  :mod:`scanning.middleware`).
- A background thread periodically logs any request that has been in flight
  longer than a threshold — with its endpoint and params, *while it is still
  hung* — plus the in-flight count and ``threading.active_count()`` to surface
  threadpool/concurrency saturation.

Memory/RSS is intentionally not tracked here: ``container_memory_working_set_bytes``
from cAdvisor is already in Prometheus/Grafana.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-flight request registry (populated by InFlightRequestMiddleware)
# ---------------------------------------------------------------------------


class InFlightRequest(NamedTuple):
    """A snapshot of a request currently being served, keyed by thread id.

    Only plain primitives are stored — never the request object — so the monitor
    thread can read them without touching request/DB state from another thread.
    """

    path: str
    method: str
    start: float  # time.monotonic() at request entry
    scan_pk: str | None
    user: str | None


_registry: dict[int, InFlightRequest] = {}
_registry_lock = threading.Lock()


def register_request(ident: int, path: str, method: str) -> None:
    """Record a request as in-flight on entry. ``ident`` is the worker thread id."""
    with _registry_lock:
        _registry[ident] = InFlightRequest(
            path=path,
            method=method,
            start=time.monotonic(),
            scan_pk=None,
            user=None,
        )


def annotate_request(
    ident: int, scan_pk: str | None, user: str | None
) -> None:
    """Enrich an in-flight entry with view context resolved after dispatch."""
    with _registry_lock:
        entry = _registry.get(ident)
        if entry is not None:
            _registry[ident] = entry._replace(scan_pk=scan_pk, user=user)


def unregister_request(ident: int) -> None:
    """Clear a request from the registry on completion (called in a ``finally``)."""
    with _registry_lock:
        _registry.pop(ident, None)


def _snapshot_registry() -> list[tuple[int, InFlightRequest]]:
    with _registry_lock:
        return list(_registry.items())


def snapshot_for_report(now: float | None = None) -> list[dict]:
    """Return a JSON-serializable snapshot of in-flight requests for Sentry.

    Called from :meth:`scanning.workers.UvicornWorker._report_timeout_to_sentry`
    (which runs in the SIGABRT handler on the *main* thread) to attach the
    wedging request's endpoint, ``scan_pk`` and user to the WORKER TIMEOUT
    event. Sentry breadcrumbs are per-thread, so the monitor thread's warnings
    never reach that event; the registry is a lock-guarded module global, so
    reading it directly from the main thread is the one way this attribution
    lands in Sentry rather than only in the pod logs.

    Only reads the lock-guarded registry — never request/DB state — so it is
    safe to call from any thread. The main thread never holds ``_registry_lock``
    (only request/monitor threads do, briefly), so this cannot self-deadlock in
    the signal handler. ``now`` defaults to ``time.monotonic()``; the oldest
    (longest-running) request is listed first.
    """
    if now is None:
        now = time.monotonic()
    snapshot = [
        {
            "thread": ident,
            "method": entry.method,
            "path": entry.path,
            "scan_pk": entry.scan_pk,
            "user": entry.user,
            "elapsed_s": round(now - entry.start, 1),
        }
        for ident, entry in _snapshot_registry()
    ]
    snapshot.sort(key=lambda item: item["elapsed_s"], reverse=True)
    return snapshot


# ---------------------------------------------------------------------------
# The monitor
# ---------------------------------------------------------------------------


class WebMonitor:
    """One tick of concurrency/in-flight checks, plus the loop to run them.

    Split from thread management so ``run_checks`` can be exercised directly in
    tests with an injected ``now`` and a seeded registry, without real threads.
    """

    def __init__(self, *, interval: float, inflight_warn: float) -> None:
        self.interval = interval
        self.inflight_warn = inflight_warn

    def run_checks(self, now: float) -> None:
        """Perform one observability tick. ``now`` is a ``time.monotonic()`` value."""
        inflight = _snapshot_registry()

        # Concurrency/threadpool saturation signal.
        logger.info(
            "web-monitor inflight=%d threads=%d",
            len(inflight),
            threading.active_count(),
        )

        # Name each hung request, with params, while it is still in flight.
        for ident, entry in inflight:
            elapsed = now - entry.start
            if elapsed >= self.inflight_warn:
                logger.warning(
                    "web-monitor hung request thread=%d elapsed=%.1fs "
                    "%s %s scan_pk=%s user=%s",
                    ident,
                    elapsed,
                    entry.method,
                    entry.path,
                    entry.scan_pk,
                    entry.user,
                )

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self.run_checks(time.monotonic())
            except Exception:
                # A transient failure in one tick must never kill the monitor.
                logger.exception("web-monitor tick failed")


_monitor_lock = threading.Lock()
_monitor_started = False


def start_monitor() -> None:
    """Launch the background monitor thread once per worker process.

    Called post-fork from :meth:`scanning.workers.UvicornWorker.init_process`
    (the app is preloaded, so a thread started pre-fork would not survive). Reads
    its knobs from Django settings and is idempotent.
    """
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True

    from django.conf import settings

    monitor = WebMonitor(
        interval=settings.WEB_MONITOR_INTERVAL_SECONDS,
        inflight_warn=settings.WEB_INFLIGHT_WARN_SECONDS,
    )
    thread = threading.Thread(
        target=monitor._loop,
        name="web-monitor",
        daemon=True,
    )
    thread.start()
    logger.info(
        "web-monitor started (interval=%ss inflight_warn=%ss)",
        monitor.interval,
        monitor.inflight_warn,
    )
