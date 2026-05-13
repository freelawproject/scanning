from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from scanning.factories import ScanFactory
from scanning.models import Scan


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
        if not settings.DEVELOPMENT or settings.RUNPOD_ENABLED:
            self.stdout.write("Skipping dev seeding.")
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
        self.stdout.write(
            self.style.SUCCESS(f"Created {len(scans)} sample scan(s).")
        )
