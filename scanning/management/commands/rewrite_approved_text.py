"""Write the approved text of the approved opinions again (issue #375).

The approved text is a flow of paragraphs and a list of footnotes,
derived from the ensemble document the curator approved by the join
rule of ``paragraphs``. When that rule changes, ``paragraphs.JOIN_RULE``
goes up and this command writes the approved text of every approved
opinion again, under a new key. The approval stands: the curator
approved the blocks of the text, and the rule only joins them
otherwise. The object keeps who approved and when, and the old object
stays in the bucket.

The ensemble document of an approved opinion stays in the bucket,
because nothing builds an approved row again, so no engine is asked to
read and no page is rendered.

An opinion whose approved text holds the rule of this code already is
skipped: its key would be the same.

Examples:

    # Say what would be written, and change nothing.
    docker exec scanning-daemon python manage.py \\
        rewrite_approved_text --all --dry-run

    # Write the approved text of two volumes again.
    docker exec scanning-daemon python manage.py \\
        rewrite_approved_text 2845 2702
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import opinion_review, paragraphs
from scanning.models import Opinion, OpinionReviewStatus, Scan


class Command(BaseCommand):
    help = (
        "Write the approved text of every approved opinion of the named "
        "volumes, or of every volume with --all, again under the join "
        "rule of this code."
    )

    def add_arguments(self, parser):
        """Register the CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "scan_pks",
            nargs="*",
            type=int,
            help="Scan numbers whose approved opinions are written again.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Write the approved opinions of every volume again.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written, and change nothing.",
        )

    def handle(self, *args, **options):
        """Write every named scan's approved texts again, or report them.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If a named scan does not exist, or if the
            call names scans and passes ``--all``, or does neither.
        """
        dry_run = options["dry_run"]
        pks = options["scan_pks"]
        if options["all"] == bool(pks):
            raise CommandError("name the scans or pass --all, not both")
        for pk in pks:
            if not Scan.objects.filter(pk=pk).exists():
                raise CommandError(f"scan {pk} does not exist")
        rows = Opinion.objects.filter(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        ).select_related("scan", "apply_run", "approved_by")
        if pks:
            rows = rows.filter(scan_id__in=pks)
        written = current = failed = 0
        for opinion in rows.order_by(
            "scan_id", "first_printed_page", "index_in_page"
        ):
            key = opinion_review.approved_key(
                opinion, opinion.ensemble_edit_revision
            )
            if key == opinion.approved_text_key:
                current += 1
                continue
            if dry_run:
                written += 1
                self.stdout.write(
                    f"{opinion} of scan {opinion.scan_id}: {key}"
                )
                continue
            try:
                opinion_review.rewrite_text(opinion)
            except opinion_review.ApprovalRefused as exc:
                failed += 1
                self.stderr.write(
                    f"{opinion} of scan {opinion.scan_id}: {exc}"
                )
                continue
            written += 1
            self.stdout.write(f"{opinion} of scan {opinion.scan_id}: {key}")
        self.stdout.write(
            f"{'Would write' if dry_run else 'Wrote'} {written} approved "
            f"text(s) under join rule {paragraphs.JOIN_RULE}, {current} "
            f"current, {failed} failed"
        )
