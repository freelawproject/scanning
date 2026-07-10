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
        # DB cleanup of abandoned direct-to-S3 uploads runs in every
        # environment (it's independent of the /tmp/ sweep below, which
        # is a DEVELOPMENT no-op).
        self._sweep_pending_uploads()

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

    def _sweep_pending_uploads(self):
        """Delete unconfirmed direct-to-S3 uploads (and orphaned scans).

        A ``PendingUpload`` older than ``PENDING_UPLOAD_TTL_HOURS`` means
        the browser requested a presigned POST but never confirmed (tab
        closed, upload abandoned). Delete the row; if its scan never got
        an original PDF attached (a fresh scan created just for this
        upload), delete the scan too. Scans that already have a file
        (e.g. a re-upload of an existing scan) are left untouched.

        :return: None.
        """
        from datetime import timedelta

        from django.utils import timezone

        from scanning import s3_sync
        from scanning.models import PendingUpload

        ttl_hours = getattr(settings, "PENDING_UPLOAD_TTL_HOURS", 24.0)
        cutoff = timezone.now() - timedelta(hours=ttl_hours)
        stale = PendingUpload.objects.filter(
            date_created__lt=cutoff
        ).select_related("scan")

        pending_removed = 0
        scans_removed = 0
        for pending in stale:
            scan = pending.scan
            if scan and not scan.original_pdf.name:
                # Genuinely abandoned fresh upload: reclaim the stranded S3
                # object (browser completed the up-to-2 GB POST but never
                # confirmed) along with the fileless scan. Best-effort; a
                # missing key is a no-op.
                #
                # Only do this when the scan has no file. A re-upload of an
                # existing scan reuses the SAME deterministic s3_key as the
                # confirmed original, so deleting it here would destroy the
                # live original while scan.original_pdf.name still points at
                # it -- the "re-upload scans left untouched" contract above.
                s3_sync.delete_uploaded_object(pending.s3_key)
                scan.delete()
                scans_removed += 1
            pending.delete()
            pending_removed += 1

        if pending_removed:
            logger.info(
                "cleanup_processing_tmp: removed %d stale pending upload(s) "
                "and %d orphaned scan(s)",
                pending_removed,
                scans_removed,
            )
