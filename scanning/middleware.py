"""Request-path middleware for web-worker observability (issue #115)."""

from __future__ import annotations

import threading
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from . import observability


class InFlightRequestMiddleware:
    """Track each in-flight request in a shared registry for the monitor thread.

    gunicorn's access log only writes a line on response *completion*, so a
    request that hangs until the worker is SIGKILL'd never logs. This records the
    request on entry (keyed by the serving thread id) and clears it on
    completion; :class:`scanning.observability.WebMonitor` logs any entry that
    stays in flight too long, naming the frozen endpoint with its params before
    the worker dies.
    """

    def __init__(
        self, get_response: Callable[[HttpRequest], HttpResponse]
    ) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        ident = threading.get_ident()
        # get_full_path() (not request.path) so the query string is captured —
        # the params (e.g. ?page=15&dpi=300 on a crop render, ?step=) are exactly
        # what attributes a heavy request when it wedges a worker.
        observability.register_request(
            ident, request.get_full_path(), request.method
        )
        try:
            return self.get_response(request)
        finally:
            observability.unregister_request(ident)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., HttpResponse],
        view_args: tuple,
        view_kwargs: dict,
    ) -> None:
        """Enrich the entry with resolved view context (scan_pk, user).

        Runs after URL resolution and after auth middleware, so ``request.user``
        is available; the username is captured as a plain string here rather than
        being read from the request by the monitor thread.
        """
        scan_pk = view_kwargs.get("pk") or view_kwargs.get("scan_pk")
        user = getattr(request, "user", None)
        username = (
            getattr(user, "username", None) or "anonymous"
            if user is not None
            else None
        )
        observability.annotate_request(
            threading.get_ident(),
            str(scan_pk) if scan_pk is not None else None,
            username,
        )
        return None
