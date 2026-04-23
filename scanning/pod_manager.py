"""RunPod pod lifecycle helpers.

The scanning daemon uses these to start a dedicated GPU pod when
work is queued and stop it when the queue drains, saving money
compared to leaving the pod running 24/7 or paying the Serverless
per-second premium.

Three public entry points:

- :func:`ensure_pod_ready` -- idempotent; starts the pod if stopped,
  waits for its public endpoint to come up, returns the base URL the
  daemon should POST jobs to. Called from inside
  ``scanning/runpod_client.py`` just before each dispatch.
- :func:`stop_pod` -- idempotent; issues ``POST /pods/{id}/stop``.
- :func:`record_activity` / :func:`get_last_activity` -- timestamp
  bookkeeping so :func:`stop_idle_gpu_pod` knows when the pod has
  been genuinely idle long enough to stop.

Activity timestamps live in Django's cache. ``LocMemCache`` is fine
for a single-replica daemon; if the daemon is run as multiple
replicas behind a shared Postgres, swap to a shared cache backend
(or persist the timestamp on a tiny single-row DB model).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class PodError(RuntimeError):
    """Raised on terminal RunPod REST failure."""


class PodBootTimeout(PodError):
    """Pod failed to reach a serving state within the boot timeout.

    Caller is expected to translate this into a
    :class:`~scanning.runpod_client.RunpodTransientError` so the
    affected scan goes back to ``QUEUED`` for the next daemon tick.
    """


_LAST_ACTIVITY_CACHE_KEY = "scanning:runpod_pod:last_activity"
_LAST_ACTIVITY_TTL_SECONDS = 24 * 60 * 60

_RUNPOD_REST = "https://rest.runpod.io/v1"


# ── REST helpers ────────────────────────────────────────────────────
def _headers() -> dict[str, str]:
    """Return the Authorization header for RunPod REST calls.

    :returns: Header dict with a Bearer token from ``RUNPOD_API_KEY``.
    :rtype: dict[str, str]
    :raises PodError: If ``RUNPOD_API_KEY`` is not configured.
    """
    api_key = getattr(settings, "RUNPOD_API_KEY", "")
    if not api_key:
        raise PodError("RUNPOD_API_KEY is not configured")
    return {"Authorization": f"Bearer {api_key}"}


def _pod_id() -> str:
    """Return the configured RunPod pod id.

    :returns: Pod id string.
    :rtype: str
    :raises PodError: If ``RUNPOD_POD_ID`` is not configured.
    """
    pod_id = getattr(settings, "RUNPOD_POD_ID", "")
    if not pod_id:
        raise PodError("RUNPOD_POD_ID is not configured")
    return pod_id


def get_pod_status() -> dict:
    """GET ``/v1/pods/{id}`` and return the parsed JSON.

    :returns: Pod resource dict. Notable fields: ``desiredStatus``
        (``"RUNNING"`` / ``"EXITED"``), ``publicIp``, ``portMappings``.
    :rtype: dict
    :raises PodError: On non-2xx response.
    """
    pod_id = _pod_id()
    r = requests.get(
        f"{_RUNPOD_REST}/pods/{pod_id}", headers=_headers(), timeout=30
    )
    if r.status_code != 200:
        raise PodError(
            f"GET /pods/{pod_id} returned {r.status_code}: {r.text[:200]}"
        )
    return r.json()


def _start_pod() -> None:
    """POST ``/v1/pods/{id}/start``. No-op if already running.

    :raises PodError: On non-2xx response (except 409 "already
        running", which is treated as success).
    """
    pod_id = _pod_id()
    r = requests.post(
        f"{_RUNPOD_REST}/pods/{pod_id}/start",
        headers=_headers(),
        timeout=30,
    )
    # RunPod returns 200 on success; 409 on "already running" on
    # some API versions. Either is fine.
    if r.status_code in (200, 409):
        return
    raise PodError(
        f"POST /pods/{pod_id}/start returned {r.status_code}: {r.text[:200]}"
    )


def stop_pod() -> None:
    """POST ``/v1/pods/{id}/stop``. Idempotent.

    :raises PodError: On non-2xx response (except 409 / 404, treated
        as already stopped).
    """
    pod_id = _pod_id()
    try:
        r = requests.post(
            f"{_RUNPOD_REST}/pods/{pod_id}/stop",
            headers=_headers(),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PodError(f"POST /pods/{pod_id}/stop failed: {exc}") from exc
    if r.status_code in (200, 404, 409):
        logger.info("pod %s stop requested (HTTP %s)", pod_id, r.status_code)
        return
    raise PodError(
        f"POST /pods/{pod_id}/stop returned {r.status_code}: {r.text[:200]}"
    )


# ── Readiness ───────────────────────────────────────────────────────
def _base_url_from_pod(pod: dict) -> str | None:
    """Derive the HTTP base URL for the pod's exposed port.

    Prefers RunPod's proxy domain
    (``https://{podId}-{port}.proxy.runpod.net``) so TLS terminates
    on RunPod's side; falls back to direct IP over plain HTTP when
    the proxy isn't populated yet.

    :param pod: Pod resource dict from :func:`get_pod_status`.
    :returns: Base URL such as ``https://abc-8000.proxy.runpod.net``
        or ``http://1.2.3.4:10023``; ``None`` if the pod hasn't
        exposed its port yet.
    :rtype: str | None
    """
    port = int(getattr(settings, "RUNPOD_POD_PORT", 8000))
    pod_id = pod.get("id") or _pod_id()
    proxy = f"https://{pod_id}-{port}.proxy.runpod.net"

    # If the portMappings show that port is bound, the proxy URL is
    # reachable. RunPod's proxy doesn't advertise itself in the REST
    # response, but in practice it's available as soon as the port
    # mapping is populated.
    mappings = pod.get("portMappings") or {}
    if str(port) in mappings or port in mappings:
        return proxy

    # Direct-IP fallback for when the proxy isn't populated. Plain
    # HTTP; callers should ensure the token survives that (bearer on
    # plain HTTP is acceptable over a short-lived link but not ideal).
    public_ip = pod.get("publicIp")
    if public_ip and mappings:
        mapped = mappings.get(str(port)) or mappings.get(port)
        if mapped:
            return f"http://{public_ip}:{mapped}"
    return None


def _probe_health(base_url: str, timeout: int = 10) -> bool:
    """Return True if ``GET {base_url}/health`` returns 200.

    :param base_url: Pod HTTP base URL.
    :param timeout: Request timeout in seconds.
    :returns: Whether the health probe succeeded.
    :rtype: bool
    """
    api_key = getattr(settings, "RUNPOD_POD_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = requests.get(
            f"{base_url}/health", headers=headers, timeout=timeout
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def ensure_pod_ready(timeout: int | None = None) -> tuple[str, int]:
    """Start the pod if needed and wait until the FastAPI server is up.

    :param timeout: Maximum wall-clock seconds to wait for the pod to
        become reachable. Defaults to
        ``settings.RUNPOD_POD_BOOT_TIMEOUT``.
    :returns: ``(base_url, boot_ms)`` where ``base_url`` is the HTTP
        prefix for ``/detect`` / ``/analyze`` / ``/health`` / ``/warmup``,
        and ``boot_ms`` is the elapsed time this call spent waiting for
        the pod to come up (``0`` if it was already serving).
    :rtype: tuple[str, int]
    :raises PodBootTimeout: If the pod never reaches a serving state.
    :raises PodError: On REST-level failures.
    """
    if timeout is None:
        timeout = int(getattr(settings, "RUNPOD_POD_BOOT_TIMEOUT", 600))
    start = time.monotonic()

    # Fast path: pod may already be up. Read status once before
    # kicking any state transition.
    pod = get_pod_status()
    desired = (pod.get("desiredStatus") or "").upper()
    if desired == "RUNNING":
        base_url = _base_url_from_pod(pod)
        if base_url and _probe_health(base_url):
            return base_url, 0

    # Not running (or not serving yet). Issue /start and poll.
    if desired != "RUNNING":
        logger.info("pod %s is %s; requesting start", _pod_id(), desired)
        _start_pod()

    deadline = start + timeout
    sleep_s = 2.0
    while time.monotonic() < deadline:
        time.sleep(sleep_s)
        sleep_s = min(sleep_s * 1.5, 10.0)

        try:
            pod = get_pod_status()
        except PodError as exc:
            logger.warning("pod status probe failed: %s", exc)
            continue

        desired = (pod.get("desiredStatus") or "").upper()
        if desired != "RUNNING":
            continue

        base_url = _base_url_from_pod(pod)
        if not base_url:
            continue

        if _probe_health(base_url):
            boot_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "pod %s ready at %s after %d ms", _pod_id(), base_url, boot_ms
            )
            return base_url, boot_ms

    raise PodBootTimeout(
        f"pod {_pod_id()} did not become ready within {timeout}s"
    )


# ── Activity tracking for stop_idle_gpu_pod ─────────────────────────
def record_activity() -> None:
    """Mark "there was pod work just now" so the idle stopper waits.

    Called by :func:`runpod_client._invoke` right after a successful
    dispatch. Idempotent; overwrites any existing timestamp.

    :returns: None.
    """
    cache.set(
        _LAST_ACTIVITY_CACHE_KEY,
        datetime.now(UTC).isoformat(),
        timeout=_LAST_ACTIVITY_TTL_SECONDS,
    )


def get_last_activity() -> datetime | None:
    """Return the last recorded pod activity timestamp, if any.

    :returns: UTC datetime, or ``None`` if no activity has been
        recorded (or the cache entry expired).
    :rtype: datetime | None
    """
    raw = cache.get(_LAST_ACTIVITY_CACHE_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def clear_activity() -> None:
    """Remove the last-activity timestamp. Used after stopping the pod.

    :returns: None.
    """
    cache.delete(_LAST_ACTIVITY_CACHE_KEY)
