"""Scheduler daemon that invokes per-task management commands at intervals.

Each task's logic lives in its own management command. This process is
just a loop that calls each command when its interval has elapsed.
Adding a new periodic job means adding another entry to
``_build_schedule``.

Current schedule:

- ``process_next_scan`` every ``DAEMON_POLL_INTERVAL`` seconds (default 5s)
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
        "Run the scanning daemon. Invokes process_next_scan and "
        "cleanup_processing_tmp on their configured intervals."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shutdown = False

    def _build_schedule(self) -> list[ScheduledTask]:
        """Return the list of tasks this daemon should run.

        :return: Tasks with their intervals.
        :rtype: list[ScheduledTask]
        """
        tasks = [
            ScheduledTask(
                name="process_next_scan",
                interval_seconds=float(settings.DAEMON_POLL_INTERVAL),
            ),
            ScheduledTask(
                name="cleanup_processing_tmp",
                interval_seconds=float(
                    settings.PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS
                ),
            ),
        ]
        if getattr(settings, "RUNPOD_ENABLED", False):
            tasks.append(
                ScheduledTask(
                    name="stop_idle_gpu_pod",
                    interval_seconds=float(
                        getattr(settings, "RUNPOD_POD_STOP_POLL_SECONDS", 30)
                    ),
                )
            )
        return tasks

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
                except Exception:
                    logger.exception("Scheduled task %s failed", task.name)
                task.mark_ran(time.monotonic())
            time.sleep(1)

        self.stdout.write("Daemon shutting down.")

    def _handle_signal(self, signum, frame):
        """Set the shutdown flag on receipt of a termination signal.

        :param signum: The signal number received.
        :param frame: The interrupted stack frame.
        :return: None.
        """
        self.stdout.write(f"Received signal {signum}, shutting down...")
        self.shutdown = True
