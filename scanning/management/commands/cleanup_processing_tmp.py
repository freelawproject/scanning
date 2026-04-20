"""Delete stale /tmp/ processing directories whose last access is
older than the configured TTL.

In DEVELOPMENT the command exits without deleting anything so local
work isn't disrupted. Flip ``DEVELOPMENT=False`` in the environment to
exercise the real deletion flow.

Examples:

    # Normal invocation (uses settings.PROCESSING_TMP_TTL_HOURS).
    docker exec scanning-daemon python manage.py cleanup_processing_tmp

    # Force the TTL to 1 hour for a one-off cleanup.
    docker exec scanning-daemon python manage.py cleanup_processing_tmp \\
        --ttl-hours 1

    # Also runs automatically from run_daemon every
    # PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS (default 900s).
"""

import logging
import shutil
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Delete /tmp/scanning/* directories that haven't been touched "
        "in PROCESSING_TMP_TTL_HOURS hours. No-op in DEVELOPMENT."
    )

    def add_arguments(self, parser):
        """Register optional CLI flags for this command.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "--ttl-hours",
            type=float,
            default=None,
            help=(
                "Override the TTL in hours. Defaults to "
                "settings.PROCESSING_TMP_TTL_HOURS."
            ),
        )

    def handle(self, *args, **options):
        """Sweep stale processing directories.

        :param args: Unused positional args.
        :param options: Parsed command options.
        :return: None.
        """
        if settings.DEVELOPMENT:
            self.stdout.write(
                "DEVELOPMENT=True: skipping /tmp/ processing cleanup."
            )
            return

        ttl_hours = options.get("ttl_hours")
        if ttl_hours is None:
            ttl_hours = settings.PROCESSING_TMP_TTL_HOURS
        cutoff = time.time() - (ttl_hours * 3600)

        tmp_root = Path(settings.PROCESSING_TMP_DIR)
        if not tmp_root.is_dir():
            return

        removed = 0
        for child in tmp_root.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                logger.exception("Failed to stat %s; skipping", child)
                continue
            if mtime < cutoff:
                try:
                    shutil.rmtree(child)
                    removed += 1
                except OSError:
                    logger.exception("Failed to remove stale dir %s", child)

        if removed:
            logger.info(
                "cleanup_processing_tmp: removed %d stale dir(s) under %s",
                removed,
                tmp_root,
            )
