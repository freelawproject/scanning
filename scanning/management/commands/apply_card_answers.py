"""Write the stored answers of the card rows into the text.

A volunteer's answer on the cards page (``/opinions/cards/``) is
stored on its ``OpinionCard`` row and changes nothing. This command
writes the answers of the named opinions as the review page's own text
edits and builds each opinion's text again (``opinion_cards.apply``):
one round per block a word at a time, a block whose every open card is
answered in one edit, and a page whose every card was kept as shown
dismissed. A row whose block the review page would refuse is marked
"needs a new answer" with the reason.

Examples:

    # Say what would be written, and change nothing.
    docker exec scanning-daemon python manage.py \\
        apply_card_answers 2845 --dry-run

    # Write the answers of two volumes.
    docker exec scanning-daemon python manage.py apply_card_answers 2845 2702

    # Write every stored answer.
    docker exec scanning-daemon python manage.py apply_card_answers --all

    # Write the answers of named opinions, by the pk of their review page.
    docker exec scanning-daemon python manage.py \\
        apply_card_answers --opinion 1234 1235
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from scanning import ensemble, opinion_cards
from scanning.models import ApplyRun, Opinion, OpinionCard, Scan


class Command(BaseCommand):
    help = (
        "Write the stored answers of the card rows of the named volumes, "
        "of every volume with --all, or of the opinions named with "
        "--opinion, into the text."
    )

    def add_arguments(self, parser):
        parser.add_argument("scan_pks", nargs="*", type=int)
        parser.add_argument("--all", action="store_true")
        parser.add_argument(
            "--opinion",
            nargs="+",
            action="extend",
            type=int,
            default=[],
            metavar="PK",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        scans = options["scan_pks"]
        named = options["opinion"]
        if bool(scans) + bool(named) + bool(options["all"]) != 1:
            raise CommandError(
                "Name scans, opinions (--opinion) or --all, one of the three."
            )
        answered = OpinionCard.objects.filter(
            status=OpinionCard.Status.ANSWERED
        )
        if scans:
            answered = answered.filter(opinion__scan_id__in=scans)
        if named:
            answered = answered.filter(opinion_id__in=named)
        ids = sorted(set(answered.values_list("opinion_id", flat=True)))
        opinions = (
            Opinion.objects.filter(pk__in=ids)
            .select_related("scan", "apply_run")
            .defer(
                *Scan.large_fields("scan__"),
                *(f"apply_run__{name}" for name in ApplyRun.LARGE_FIELDS),
            )
            .order_by("pk")
        )
        verb = "would write" if options["dry_run"] else "wrote"
        total: Counter = Counter()
        for opinion in opinions:
            try:
                counts = opinion_cards.apply(opinion, dry_run=options["dry_run"])
            except (ensemble.EnsembleError, ensemble.TransientFault) as exc:
                total["failed"] += 1
                self.stderr.write(f"{opinion} (#{opinion.pk}): {exc}")
                continue
            total.update(counts)
            total["opinions"] += 1
            line = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            self.stdout.write(f"{opinion} (#{opinion.pk}): {line or 'nothing'}")
        summary = ", ".join(f"{k} {v}" for k, v in sorted(total.items()))
        self.stdout.write(
            self.style.SUCCESS(f"{verb}: {summary or 'no answered card'}")
        )
