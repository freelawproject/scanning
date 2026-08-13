"""Scheduler daemon that invokes per-task management commands at intervals.

Each task's logic lives in its own management command. This process is
just a loop that calls each command when its interval has elapsed.
Adding a new periodic job means adding another entry to
``_build_schedule``.

Current schedule:

- ``process_next_scan`` every ``DAEMON_POLL_INTERVAL`` seconds (default 5s)
- ``collect_external_jobs`` every ``DAEMON_COLLECT_INTERVAL`` seconds
  (default 15s)
- ``cleanup_processing_tmp`` every ``PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS``
  seconds (default 900s)

Examples:

    # Start the daemon in the foreground (docker-compose runs this
    # automatically in the scanning-daemon service).
    docker exec scanning-daemon python manage.py run_daemon

    # Ad-hoc run inside the web container, e.g. when debugging locally.
    docker exec scanning-django python manage.py run_daemon
"""

import logging
import signal
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import OperationalError

logger = logging.getLogger(__name__)


@dataclass
class ScheduledTask:
    """A management command to invoke at a fixed interval.

    :ivar name: Name of the management command (as passed to call_command).
    :ivar interval_seconds: Minimum time between consecutive runs.
    :ivar last_ran: Monotonic timestamp of the most recent run. 0.0 means
        "never ran"; the task fires on the first scheduler tick.
    """

    name: str
    interval_seconds: float
    last_ran: float = field(default=0.0)

    def due(self, now: float) -> bool:
        """Return True if the task should fire at ``now``.

        :param now: Current monotonic timestamp.
        :return: Whether the task's interval has elapsed.
        :rtype: bool
        """
        return (now - self.last_ran) >= self.interval_seconds

    def mark_ran(self, now: float) -> None:
        """Record that the task ran at ``now``.

        :param now: Current monotonic timestamp.
        :return: None.
        """
        self.last_ran = now


class Command(BaseCommand):
    help = (
        "Run the scanning daemon. Invokes process_next_scan, "
        "collect_external_jobs and cleanup_processing_tmp on their "
        "configured intervals."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shutdown = False

    def _build_schedule(self) -> list[ScheduledTask]:
        """Return the list of tasks this daemon should run.

        :return: Tasks with their intervals.
        :rtype: list[ScheduledTask]
        """
        return [
            ScheduledTask(
                name="process_next_scan",
                interval_seconds=float(settings.DAEMON_POLL_INTERVAL),
            ),
            ScheduledTask(
                name="collect_external_jobs",
                interval_seconds=float(settings.DAEMON_COLLECT_INTERVAL),
            ),
            ScheduledTask(
                name="cleanup_processing_tmp",
                interval_seconds=float(
                    settings.PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS
                ),
            ),
        ]

    def handle(self, *args, **options):
        """Tick the schedule until a termination signal is received.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        schedule = self._build_schedule()
        intervals = ", ".join(
            f"{t.name}={t.interval_seconds:g}s" for t in schedule
        )
        self.stdout.write(f"Daemon started. Schedule: {intervals}")

        while not self.shutdown:
            now = time.monotonic()
            for task in schedule:
                if not task.due(now):
                    continue
                try:
                    call_command(task.name)
                except OperationalError as exc:
                    # Transient DB blip (e.g. a lost TLS handshake on a
                    # fresh connection). Self-recovering; log at WARNING so
                    # it stays a breadcrumb and doesn't create a Sentry
                    # error event. See issue #116.
                    logger.warning(
                        "Scheduled task %s hit a transient DB error: %s",
                        task.name,
                        exc,
                    )
                except Exception:
                    logger.exception("Scheduled task %s failed", task.name)
                task.mark_ran(time.monotonic())
            time.sleep(1)

        self.stdout.write("Daemon shutting down.")

    def _handle_signal(self, signum, frame):
        """Set the shutdown flag on receipt of a termination signal.

        Also re-queues any PROCESSING scans so they retry on the next
        daemon tick instead of waiting out ``DAEMON_PROCESSING_TIMEOUT``,
        and reports the shutdown to Sentry to help diagnose unexpected
        kills (deploys, OOM, container restarts).

        :param signum: The signal number received.
        :param frame: The interrupted stack frame.
        :return: None.
        """
        self.stdout.write(f"Received signal {signum}, shutting down...")
        self.shutdown = True

        try:
            from scanning.models import Scan, Status

            # Don't bump retry_count: the scan didn't fail, the daemon
            # was killed. Only RunpodTransientError-driven re-queues
            # (in _handle_pipeline_exception) consume the retry budget,
            # so a stream of deploys can't push a scan to ERROR_MAX_RETRIES
            # without a single real GPU/runpod failure.
            #
            # We do bump interruption_count so a scan can't be re-queued
            # forever by a churning daemon pod: past DAEMON_MAX_INTERRUPTIONS
            # it is flagged ERROR_INTERRUPTED for review instead (issue #124).
            # The helper logs the re-queue (INFO breadcrumb) and flag (ERROR
            # event) itself, so both this path and stale-recovery report to
            # Sentry consistently.
            Scan.requeue_or_flag_interrupted(
                Scan.objects.filter(status=Status.PROCESSING),
                requeue_message=(
                    f"Daemon received signal {signum} mid-pipeline, "
                    "re-queued for next tick."
                ),
            )
        except Exception:
            logger.exception("Failed to re-queue in-flight scans on shutdown")

        try:
            import sentry_sdk

            sentry_sdk.capture_message(
                f"scanning daemon received signal {signum}, shutting down",
                level="warning",
            )
        except Exception:
            logger.exception("Failed to report daemon shutdown to Sentry")
