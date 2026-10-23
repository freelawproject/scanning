from http import HTTPStatus
from typing import NoReturn

from django.conf import settings
from django.db import OperationalError, connections
from django.http import HttpRequest, HttpResponse, JsonResponse

from scanning import tasks, worker_heartbeat
from scanning.celery_init import app


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
    ``null`` and the answer a 500. With ``CELERY_ENABLED`` off nothing
    is sent to a queue, so the answer is an empty 200.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: The depths (200), or a 500 when one is unknown.
    :rtype: JsonResponse
    """
    if not settings.CELERY_ENABLED:
        return JsonResponse({})
    depths = {queue: tasks.queue_depth(queue) for queue in tasks.QUEUES}
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
    read. With ``CELERY_ENABLED`` off no worker is expected: 200.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :returns: ``celery_enabled``, ``queues`` (queue to node names) and
        ``missing``; 200 when every queue has a worker, else 500.
    :rtype: JsonResponse
    """
    if not settings.CELERY_ENABLED:
        return JsonResponse({"celery_enabled": False})
    try:
        workers = worker_heartbeat.live_workers(app)
    except Exception:
        workers = None
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


def sentry_fail(request: HttpRequest) -> NoReturn:
    """Raise an intentional error to verify Sentry integration.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :raises ZeroDivisionError: Always.
    """
    raise ZeroDivisionError("Intentional error for Sentry")
