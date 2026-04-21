"""Manually re-upload a scan's processing files to S3.

Intended for recovery after a transient S3 failure that was caught and
logged (visible in Sentry). The retry must happen before the TTL sweep
removes the local /tmp/ copies; otherwise the files are no longer
available locally and have to be regenerated.

Examples:

    # Re-upload every file under the scan's output_dir.
    docker exec scanning-daemon python manage.py reupload_scan_files 1852

    # Re-upload only specific files (paths relative to output_dir).
    docker exec scanning-daemon python manage.py reupload_scan_files 1852 \\
        --files detections.json bitonal.pdf
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from scanning import s3_sync
from scanning.models import Scan


class Command(BaseCommand):
    help = (
        "Re-upload a scan's processing files to S3. Run from whichever "
        "container (web or daemon) holds the local /tmp/ copies."
    )

    def add_arguments(self, parser):
        """Register positional and optional CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "scan_id",
            type=int,
            help="Primary key of the scan to re-upload.",
        )
        parser.add_argument(
            "--files",
            nargs="+",
            metavar="REL_PATH",
            help=(
                "Optional list of relative paths (under the scan's "
                "output_dir) to upload. If omitted, every file under "
                "output_dir is uploaded."
            ),
        )

    def handle(self, *args, **options):
        """Retry the S3 upload for the given scan.

        :param args: Unused positional args.
        :param options: Parsed command options.
        :return: None.
        """
        scan_id = options["scan_id"]
        try:
            scan = Scan.objects.get(pk=scan_id)
        except Scan.DoesNotExist as exc:
            raise CommandError(f"Scan {scan_id} does not exist") from exc

        local_root = Path(scan.output_dir)
        if not local_root.is_dir():
            raise CommandError(
                f"Local output dir {local_root} does not exist. "
                "Files may have been swept by cleanup_processing_tmp; "
                "requeue the scan to regenerate."
            )

        files = options.get("files")
        if files:
            failures = []
            for rel in files:
                ok = s3_sync.upload_file_to_s3(scan, rel)
                if ok:
                    self.stdout.write(self.style.SUCCESS(f"  uploaded {rel}"))
                else:
                    failures.append(rel)
                    self.stdout.write(self.style.ERROR(f"  failed {rel}"))
            if failures:
                raise CommandError(
                    f"{len(failures)} file(s) failed to upload: {failures}"
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Re-uploaded {len(files)} file(s) for scan {scan_id}"
                )
            )
            return

        count = s3_sync.upload_processing_files(scan)
        self.stdout.write(
            self.style.SUCCESS(
                f"Re-uploaded {count} file(s) for scan {scan_id} to "
                f"s3://.../{s3_sync.s3_processing_prefix(scan)}"
            )
        )
