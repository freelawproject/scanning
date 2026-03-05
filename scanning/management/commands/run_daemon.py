import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the scanning daemon that polls for unprocessed scans."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shutdown = False

    def handle(self, *args, **options):
        """Start the daemon loop.

        Installs signal handlers for graceful shutdown, then iterates
        a schedule of tasks, running each when its interval has elapsed.

        :param args: Positional arguments (unused).
        :param options: Command options (unused).
        """
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        schedule = self._build_schedule()
        self.stdout.write(
            "Daemon started with schedule: "
            + ", ".join(
                f"{name} every {interval}s" for _, interval, name in schedule
            )
        )

        last_run: dict[str, float] = {}
        while not self.shutdown:
            now = time.monotonic()
            for task_fn, interval, name in schedule:
                if now - last_run.get(name, 0) >= interval:
                    self._run_task(task_fn, name)
                    last_run[name] = time.monotonic()
            time.sleep(1)

        self.stdout.write("Scanning daemon shutting down.")

    def _build_schedule(self):
        """Build the list of scheduled tasks.

        :returns: List of (task_fn, interval_seconds, name) tuples.
        :rtype: list[tuple[callable, int, str]]
        """

        def process_scans():
            from scanning.management.commands.process_scans import (
                Command as ProcessScans,
            )

            ProcessScans().handle()

        return [
            (
                process_scans,
                settings.DAEMON_PROCESS_SCANS_INTERVAL,
                "process_scans",
            ),
        ]

    def _run_task(self, task_fn, name):
        """Execute a scheduled task with error handling.

        :param task_fn: The callable to execute.
        :type task_fn: callable
        :param name: Human-readable task name for logging.
        :type name: str
        """
        try:
            self.stdout.write(f"Running {name}...")
            task_fn()
            self.stdout.write(f"Finished {name}.")
        except Exception:
            logger.exception("Error running task %s", name)

    def _handle_signal(self, signum, frame):
        """Set the shutdown flag on receiving a termination signal.

        :param signum: Signal number.
        :type signum: int
        :param frame: Current stack frame.
        :type frame: frame
        """
        self.stdout.write(f"Received signal {signum}, shutting down...")
        self.shutdown = True
