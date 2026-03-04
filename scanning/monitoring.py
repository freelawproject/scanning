from http import HTTPStatus
from typing import NoReturn

from django.db import OperationalError, connections
from django.http import HttpRequest, HttpResponse, JsonResponse


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


def sentry_fail(request: HttpRequest) -> NoReturn:
    """Raise an intentional error to verify Sentry integration.

    :param request: The incoming HTTP request.
    :type request: HttpRequest
    :raises ZeroDivisionError: Always.
    """
    raise ZeroDivisionError("Intentional error for Sentry")
