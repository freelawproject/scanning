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
        """Recover or delete unconfirmed direct-to-S3 uploads.

        A ``PendingUpload`` older than ``PENDING_UPLOAD_TTL_HOURS`` never
        reached ``confirm_scan_upload``. Two outcomes:

        - The object actually landed in S3 (the POST finished, only confirm
          was lost). Recover it: attach it to the scan and replay the stored
          action, exactly as confirm would have. Never destroy a good file.
        - The object isn't there (truly abandoned before the upload
          finished). Delete the pending row, and if its scan never got a
          file attached (a fresh scan created just for this upload), delete
          the scan and any stray object too. Scans that already have a file
          (a re-upload of an existing scan) are left untouched.

        :return: None.
        """
        from datetime import timedelta

        from django.utils import timezone

        from scanning import s3_sync, services
        from scanning.models import PendingUpload

        ttl_hours = getattr(settings, "PENDING_UPLOAD_TTL_HOURS", 24.0)
        cutoff = timezone.now() - timedelta(hours=ttl_hours)
        stale = PendingUpload.objects.filter(
            date_created__lt=cutoff
        ).select_related("scan")

        recovered = 0
        pending_removed = 0
        scans_removed = 0
        for pending in stale:
            # Recover first: if the object is in S3 and valid, link it rather
            # than delete it. recover_pending_upload deletes the row on success.
            if services.recover_pending_upload(pending):
                recovered += 1
                continue
            scan = pending.scan
            if scan and not scan.original_pdf.name:
                # Truly abandoned fresh upload: reclaim any stray S3 object
                # (best-effort; a missing key is a no-op) and the fileless
                # scan. Guarded to the fileless case: a re-upload reuses the
                # confirmed original's s3_key, so deleting it here would
                # destroy the live original.
                s3_sync.delete_uploaded_object(pending.s3_key)
                scan.delete()
                scans_removed += 1
            pending.delete()
            pending_removed += 1

        if pending_removed or recovered:
            logger.info(
                "cleanup_processing_tmp: recovered %d unconfirmed upload(s), "
                "removed %d stale pending upload(s) and %d orphaned scan(s)",
                recovered,
                pending_removed,
                scans_removed,
            )
