"""Stop the RunPod GPU pod if the scan queue has been idle.

Scheduled by ``run_daemon`` every ``RUNPOD_POD_STOP_POLL_SECONDS``
(default 30s) when ``RUNPOD_ENABLED`` is true. Exits immediately if
any scan is QUEUED or PROCESSING. Otherwise, if the last recorded
activity is older than ``RUNPOD_POD_IDLE_GRACE_SECONDS`` and the pod
is currently running, issues ``POST /pods/{id}/stop``.

Examples:

    # Run once manually (useful for local debugging).
    docker exec scanning-daemon python manage.py stop_idle_gpu_pod
"""

import logging
from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from scanning import pod_manager
from scanning.models import Scan, Status

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Stop the RunPod GPU pod if no scans are queued or processing "
        "and the last activity was more than "
        "RUNPOD_POD_IDLE_GRACE_SECONDS ago."
    )

    def handle(self, *args, **options):
        """Run one idle-check tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        if not getattr(settings, "RUNPOD_ENABLED", False):
            return

        if Scan.objects.filter(
            status__in=[Status.QUEUED, Status.PROCESSING]
        ).exists():
            return

        last = pod_manager.get_last_activity()
        grace = int(getattr(settings, "RUNPOD_POD_IDLE_GRACE_SECONDS", 120))
        if last is not None:
            age = (datetime.now(UTC) - last).total_seconds()
            if age < grace:
                return

        try:
            pod = pod_manager.get_pod_status()
        except pod_manager.PodError as exc:
            logger.warning("cannot fetch pod status: %s", exc)
            return

        desired = (pod.get("desiredStatus") or "").upper()
        if desired != "RUNNING":
            # Already stopped / exiting -- nothing to do. Clear the
            # activity cache so the next boot starts fresh.
            pod_manager.clear_activity()
            return

        logger.info(
            "queue idle past grace (%s s); stopping pod %s",
            grace,
            pod.get("id"),
        )
        try:
            pod_manager.stop_pod()
        except pod_manager.PodError as exc:
            logger.warning("pod stop failed: %s", exc)
            return
        pod_manager.clear_activity()
