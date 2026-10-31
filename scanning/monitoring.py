import logging
from http import HTTPStatus
from typing import NoReturn

from django.conf import settings
from django.db import DatabaseError, OperationalError, connections
from django.http import HttpRequest, HttpResponse, JsonResponse

from scanning import daemon_heartbeat, tasks, worker_heartbeat
from scanning.celery_init import app

logger = logging.getLogger(__name__)


def check_postgresql() -> bool:
    """Check if we can connect to PostgreSQL."""
    try:
        for alias in connections:
            with connections[alias].cursor() as c:
                c.execute("SELECT 1")
                c.fetchone()
    except OperationalError:
        return False
    return True


def heartbeat(request: HttpRequest) -> HttpResponse:
    """Return a plain-text OK response for uptime monitoring.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: A 200 response with body "OK".
    :rtype: HttpResponse
    """
    return HttpResponse("OK", content_type="text/plain")


def health_check(request: HttpRequest) -> JsonResponse:
    """Check connectivity to backing services and return their status.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: JSON with service statuses (200 if all healthy, 500 otherwise).
    :rtype: JsonResponse
    """
    is_postgresql_up = check_postgresql()

    status = HTTPStatus.OK
    if not is_postgresql_up:
        status = HTTPStatus.INTERNAL_SERVER_ERROR

    return JsonResponse(
        {"is_postgresql_up": is_postgresql_up},
        status=status,
    )


def celery_queues(request: HttpRequest) -> JsonResponse:
    """Return how many messages wait on each Celery queue.

    CourtListener's ``/monitoring/celery-queues/``: a flat
    ``{queue: depth}`` for an uptime monitor to alert on, one ``LLEN``
    per queue of ``tasks.QUEUES``. A depth the broker could not give is
    ``null`` and the answer a 500. The first unknown depth ends the
    reads, and inside ``tasks.BROKER_BACKOFF_SECONDS`` after a broker
    fault none is made, so a hung broker costs a probe one timeout at
    most, as it costs a view's send. With ``CELERY_ENABLED`` off
    nothing is sent to a queue, so the answer is an empty 200.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: The depths (200), or a 500 when one is unknown.
    :rtype: JsonResponse
    """
    if not settings.CELERY_ENABLED:
        return JsonResponse({})
    depths = dict.fromkeys(tasks.QUEUES)
    if tasks._broker_ok():
        for queue in tasks.QUEUES:
            depths[queue] = tasks.queue_depth(queue)
            if depths[queue] is None:
                break
    status = HTTPStatus.OK
    if any(depth is None for depth in depths.values()):
        status = HTTPStatus.INTERNAL_SERVER_ERROR
    return JsonResponse(depths, status=status)


def celery_workers(request: HttpRequest) -> JsonResponse:
    """Return the live workers of each Celery queue.

    Read off the heartbeat keys the workers write
    (``scanning.worker_heartbeat``), so a request broadcasts nothing.
    A queue of ``tasks.QUEUES`` that no live worker consumes is in
    ``missing`` and the answer a 500, as is a broker that cannot be
    read or faulted inside ``tasks.BROKER_BACKOFF_SECONDS``. With
    ``CELERY_ENABLED`` off no worker is expected: 200.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: ``celery_enabled``, ``queues`` (queue to node names) and
        ``missing``; 200 when every queue has a worker, else 500.
    :rtype: JsonResponse
    """
    if not settings.CELERY_ENABLED:
        return JsonResponse({"celery_enabled": False})
    workers = None
    if tasks._broker_ok():
        try:
            workers = worker_heartbeat.live_workers(app)
        except Exception:
            tasks._broker_failed()
            logger.warning(
                "could not read the worker heartbeats", exc_info=True
            )
    if workers is None:
        return JsonResponse(
            {"celery_enabled": True, "error": "the broker could not be read"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    queues: dict[str, list[str]] = {queue: [] for queue in tasks.QUEUES}
    for worker in workers:
        for queue in worker.get("queues", ()):
            queues.setdefault(queue, []).append(worker.get("hostname", "?"))
    for hostnames in queues.values():
        hostnames.sort()
    missing = [queue for queue in tasks.QUEUES if not queues[queue]]
    status = HTTPStatus.INTERNAL_SERVER_ERROR if missing else HTTPStatus.OK
    return JsonResponse(
        {"celery_enabled": True, "queues": queues, "missing": missing},
        status=status,
    )


def daemon_heartbeat_view(request: HttpRequest) -> JsonResponse:
    """Return whether the daemon's loop still turns.

    Read off the stamp ``run_daemon`` writes from its loop
    (``scanning.daemon_heartbeat``). With ``CELERY_ENABLED`` on the
    daemon is the clock, and a live worker with no clock runs nothing,
    so this is the third check beside the workers and the depths. A
    500 that names the database on every probe is the cache table
    ``createcachetable`` has not made.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: ``mode``, ``age_seconds`` and ``stale_after``; 200 when
        the stamp is fresh, 500 when it is stale or missing.
    :rtype: JsonResponse
    """
    try:
        stamp = daemon_heartbeat.read()
    except DatabaseError:
        logger.warning("could not read the daemon heartbeat", exc_info=True)
        return JsonResponse(
            {"error": "the database could not be read"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    if stamp is None:
        return JsonResponse(
            {"error": "the daemon never beat"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    mode = stamp["mode"]
    age = stamp["age_seconds"]
    limit = daemon_heartbeat.stale_after(mode)
    status = (
        HTTPStatus.OK if age <= limit else HTTPStatus.INTERNAL_SERVER_ERROR
    )
    return JsonResponse(
        {"mode": mode, "age_seconds": round(age, 1), "stale_after": limit},
        status=status,
    )


def sentry_fail(request: HttpRequest) -> NoReturn:
    """Raise an intentional error to verify Sentry integration.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :raises ZeroDivisionError: Always.
    """
    raise ZeroDivisionError("Intentional error for Sentry")
