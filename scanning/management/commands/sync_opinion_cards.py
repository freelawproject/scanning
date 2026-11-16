"""Write the card rows of opinions from their stamped ensemble documents.

The ensemble build writes the card rows (``opinion_cards.sync``) each
time it writes a document. This command writes them from the document
already stamped, with no build: it reads one S3 object per opinion and
writes nothing to the bucket, only the ``OpinionCard`` rows. Run it
once after the deploy of the card rows, and after a change of the
reason rule (``blocking_types``), which moves no document.

Examples:

    docker exec scanning-daemon python manage.py sync_opinion_cards 2845
    docker exec scanning-daemon python manage.py sync_opinion_cards --all
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scanning import ensemble, opinion_cards
from scanning.models import Opinion, OpinionReviewStatus


class Command(BaseCommand):
    help = (
        "Write the card rows of the opinions in text review of the named "
        "volumes, or of every volume with --all, from their ensemble "
        "documents. Reads the bucket; writes only the rows."
    )

    def add_arguments(self, parser):
        parser.add_argument("scan_pks", nargs="*", type=int)
        parser.add_argument("--all", action="store_true")

    def handle(self, *args, **options):
        if bool(options["scan_pks"]) == bool(options["all"]):
            raise CommandError("Name scans, or --all.")
        opinions = Opinion.objects.filter(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        ).order_by("scan_id", "pk")
        if options["scan_pks"]:
            opinions = opinions.filter(scan_id__in=options["scan_pks"])
        total: Counter = Counter()
        for opinion in opinions.iterator():
            if not ensemble.is_written(opinion):
                total["not written"] += 1
                continue
            read_at = (opinion.ensemble_revision, opinion.ensemble_edit_revision)
            try:
                document = ensemble.read_document(opinion)
            except (ensemble.EnsembleError, ensemble.TransientFault) as exc:
                total["failed"] += 1
                self.stderr.write(f"{opinion} (#{opinion.pk}): {exc}")
                continue
            with transaction.atomic():
                # The lock ``ensemble.write`` takes: a build that stamped
                # a newer document since the read wrote the rows from it,
                # and these would be the older document's.
                now = (
                    Opinion.objects.select_for_update()
                    .filter(pk=opinion.pk)
                    .values_list("ensemble_revision", "ensemble_edit_revision")
                    .first()
                )
                if now != read_at:
                    total["moved, skipped"] += 1
                    continue
                total.update(opinion_cards.sync(opinion, document))
            total["opinions"] += 1
        summary = ", ".join(f"{k} {v}" for k, v in sorted(total.items()))
        self.stdout.write(self.style.SUCCESS(summary or "nothing to sync"))
