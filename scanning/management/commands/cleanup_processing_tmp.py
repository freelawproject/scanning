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
import os
import shutil
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _tree_mtime(path: Path) -> float:
    """Return the newest mtime anywhere under ``path``, itself included.

    The sweep decides staleness on this, not on the top directory's own
    mtime: the processing layout is ``{pk}/{reporter}/{vol}/{start}/...``,
    so every real write lands levels below the directory the sweep
    iterates, and the top mtime is just the creation time (#215).
    Entries that vanish or refuse a stat mid-walk are skipped.

    :param path: Directory to examine.
    :returns: The newest mtime found, as an epoch timestamp.
    :rtype: float
    """
    try:
        newest = path.stat().st_mtime
    except OSError:
        newest = 0.0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                mtime = os.stat(os.path.join(root, name)).st_mtime
            except OSError:
                continue
            newest = max(newest, mtime)
    return newest


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

        removed = self._sweep_processing_dirs(cutoff)
        if removed:
            logger.info(
                "cleanup_processing_tmp: removed %d stale dir(s) under %s",
                removed,
                settings.PROCESSING_TMP_DIR,
            )

        leaked = self._sweep_leaked_stage_dirs(cutoff)
        if leaked:
            logger.info(
                "cleanup_processing_tmp: removed %d leaked stage temp dir(s)",
                leaked,
            )

    def _sweep_processing_dirs(self, cutoff: float) -> int:
        """Delete per-scan processing directories idle past the cutoff.

        Staleness is the newest mtime anywhere in the tree
        (:func:`_tree_mtime`), so a scan the daemon or a viewer touched
        deep down stays, however old its top directory is.

        :param cutoff: Epoch timestamp; older trees are removed.
        :returns: How many directories were removed.
        :rtype: int
        """
        tmp_root = Path(settings.PROCESSING_TMP_DIR)
        if not tmp_root.is_dir():
            return 0

        removed = 0
        for child in tmp_root.iterdir():
            if not child.is_dir():
                continue
            if _tree_mtime(child) < cutoff:
                try:
                    shutil.rmtree(child)
                    removed += 1
                except OSError:
                    logger.exception("Failed to remove stale dir %s", child)
        return removed

    def _sweep_leaked_stage_dirs(self, cutoff: float) -> int:
        """Delete leaked merge and glue scratch directories.

        The bitonal merge, the dots.mocr glue and the detection merge
        download shard results
        into ``TemporaryDirectory`` dirs in the system temp dir --
        outside ``PROCESSING_TMP_DIR``, so the sweep above never sees
        them. A normal exit removes them; a SIGKILL mid-stage leaks
        them, and nothing else ever reclaims the space (#215). A live
        stage is safe from this: its directory is minutes old and the
        TTL is hours.

        :param cutoff: Epoch timestamp; older directories are removed.
        :returns: How many directories were removed.
        :rtype: int
        """
        from scanning.bitonal import MERGE_TMP_PREFIX
        from scanning.dots_mocr import GLUE_TMP_PREFIX
        from scanning.yolo import MERGE_TMP_PREFIX as DETECT_TMP_PREFIX

        if getattr(settings, "TESTING", False):
            # A test run must never walk the developer's real temp dir.
            # The command's own tests lift this and point gettempdir at
            # a scratch root.
            return 0

        temp_root = Path(tempfile.gettempdir())
        if not temp_root.is_dir():
            return 0

        removed = 0
        for child in temp_root.iterdir():
            if not child.is_dir():
                continue
            if not child.name.startswith(
                (MERGE_TMP_PREFIX, GLUE_TMP_PREFIX, DETECT_TMP_PREFIX)
            ):
                continue
            if _tree_mtime(child) < cutoff:
                try:
                    shutil.rmtree(child)
                    removed += 1
                    logger.info("Removed leaked stage temp dir %s", child)
                except OSError:
                    logger.exception("Failed to remove leaked dir %s", child)
        return removed

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

        ttl_hours = settings.PENDING_UPLOAD_TTL_HOURS
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
