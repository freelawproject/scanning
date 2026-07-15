from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from scanning.factories import ScanFactory
from scanning.models import Scan, Status

# The bundled sample scan, reused as a viewable preview for dev data.
FIXTURE_PDF = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "a3d.332.1.1.pdf"
)


class Command(BaseCommand):
    help = "Seed dev data: staff user, scanner user, and sample scans."

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=3,
            help="Number of scans to create (default: 3).",
        )

    def handle(self, *args, **options):
        # Soft return (not sys.exit) so entrypoint/CI hooks that call
        # this command unconditionally don't blow up outside dev. No
        # data is written either way; the goal is just a quiet no-op.
        if not settings.DEVELOPMENT:
            self.stdout.write("Skipping dev seeding: DEVELOPMENT is not True.")
            return
        if settings.RUNPOD_ENABLED:
            # Placeholder PDF bytes (b"%PDF-1.4 test") would fail the
            # moment YOLO or PaddleOCR tries to read them on the worker,
            # polluting the DB and burning endpoint quota on errors.
            self.stdout.write("Skipping dev seeding: RUNPOD_ENABLED is True.")
            return

        staff, created = User.objects.get_or_create(
            username="staff",
            defaults={"is_staff": True, "email": "staff@example.com"},
        )
        if created:
            staff.set_password("password")
            staff.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS("Created staff user."))
        else:
            self.stdout.write("Staff user already exists.")

        scanner, created = User.objects.get_or_create(
            username="scanner",
            defaults={"is_staff": False, "email": "scanner@example.com"},
        )
        if created:
            scanner.set_password("password")
            scanner.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS("Created scanner user."))
        else:
            self.stdout.write("Scanner user already exists.")

        count = options["count"]
        existing = Scan.objects.count()
        if existing >= count:
            self.stdout.write(
                f"{existing} scan(s) already exist, skipping creation."
            )
            return

        to_create = count - existing
        scans = ScanFactory.create_batch(to_create, uploaded_by=scanner)

        seeded = 0
        for scan in scans:
            if self._seed_reviewable(scan):
                seeded += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {len(scans)} sample scan(s) "
                f"({seeded} with a viewable preview)."
            )
        )

    def _seed_reviewable(self, scan):
        """Give a scan a real, viewable preview so the process viewer works.

        ``ScanFactory`` attaches only a placeholder ``b"%PDF-1.4 test"`` that
        no PDF viewer can render, and nothing produces a ``bitonal.pdf``, so a
        freshly-seeded scan otherwise sits on "still processing" forever. Copy
        the bundled sample PDF into the scan's ``output_dir`` as both the
        bitonal preview (what ``serve_scan_pdf`` serves) and the original (used
        by crops), then mark it pending review.

        Deliberately does NOT run the pipeline: OCR/detection are slow and can
        hard-segfault under x86 emulation, which would break container startup
        (``make_dev_data`` runs from the web-dev entrypoint). This stays a
        fast, pure file copy.

        :param scan: The scan to seed.
        :returns: True if a preview was written, False if the fixture is
            missing or the copy failed (the scan is left as-is).
        :rtype: bool
        """
        if not FIXTURE_PDF.is_file():
            self.stdout.write(
                self.style.WARNING(
                    f"Sample PDF not found at {FIXTURE_PDF}; "
                    f"scan {scan.pk} left without a preview."
                )
            )
            return False
        try:
            data = FIXTURE_PDF.read_bytes()
            output_dir = Path(scan.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "bitonal.pdf").write_bytes(data)
            original_name = (
                f"{scan.reporter.short_name}.{scan.volume}"
                f".{scan.start_page}.{scan.end_page}.original.pdf"
            )
            (output_dir / original_name).write_bytes(data)
            scan.original_pdf.name = original_name
            scan.status = Status.PENDING_REVIEW
            scan.save(update_fields=["original_pdf", "status"])
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"Could not seed preview for scan {scan.pk}: {exc}"
                )
            )
            return False
        return True
