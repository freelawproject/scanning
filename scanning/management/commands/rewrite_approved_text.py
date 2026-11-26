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

A rule that changes the footnotes alone (#649) changes no text of an
opinion with no footnote, so ``--with-footnotes`` leaves those opinions
out: it walks only the approved opinions whose text holds a footnote,
read off the ``OpinionText.footnotes`` cache of their pages, with no
read of the bucket. An opinion it leaves out keeps the object of the
older rule, whose text the new rule would write the same, and a later
run without the flag writes it under the new key.

Examples:

    # Say what would be written, and change nothing.
    docker exec scanning-daemon python manage.py \\
        rewrite_approved_text --all --dry-run

    # Write the approved text of two volumes again.
    docker exec scanning-daemon python manage.py \\
        rewrite_approved_text 2845 2702

    # After a change of the footnote rule: the opinions with footnotes
    # alone, first what would be written, then the write.
    docker exec scanning-daemon python manage.py \\
        rewrite_approved_text --all --with-footnotes --dry-run
    docker exec scanning-daemon python manage.py \\
        rewrite_approved_text --all --with-footnotes
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef, QuerySet

from scanning import opinion_review, paragraphs
from scanning.models import (
    ApplyRun,
    Opinion,
    OpinionReviewStatus,
    OpinionText,
    Scan,
)

#: How many opinions the walk holds at once. The command runs in the
#: daemon's pod, so its memory is the daemon's.
CHUNK_SIZE = 100


def approved_rows() -> QuerySet:
    """Return the approved opinions, with the caches of the joins left.

    The rewrite reads the scan's pk and the run's keys alone, never a
    cache of the scan and not the run's page map (#582), and the join
    repeats them on every opinion: megabytes a row, for every approved
    opinion at once, which ended the daemon's pod.

    :returns: The queryset.
    :rtype: QuerySet
    """
    return (
        Opinion.objects.filter(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .select_related("scan", "apply_run", "approved_by")
        .defer(
            *Scan.large_fields("scan__"),
            *(f"apply_run__{name}" for name in ApplyRun.LARGE_FIELDS),
        )
    )


def with_footnotes(rows: QuerySet) -> QuerySet:
    """Return the opinions of ``rows`` whose text holds a footnote.

    The ensemble writes the footnotes of every page into
    ``OpinionText.footnotes`` (#399), and nothing builds an approved
    opinion again, so the cache of an approved opinion is the footnote
    section of the document its approved text was written from.

    :param rows: A queryset of ``Opinion``.
    :returns: The rows with a page whose footnote cache is not blank.
    :rtype: QuerySet
    """
    return rows.filter(
        Exists(
            OpinionText.objects.filter(opinion=OuterRef("pk")).exclude(
                footnotes=""
            )
        )
    )


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
            "--with-footnotes",
            action="store_true",
            help=(
                "Walk only the approved opinions whose text holds a "
                "footnote: for a rule that changes the footnotes alone."
            ),
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
        if options["all"] and pks:
            raise CommandError("name the scans or pass --all, not both")
        if not options["all"] and not pks:
            raise CommandError("name the scans, or pass --all")
        for pk in pks:
            if not Scan.objects.filter(pk=pk).exists():
                raise CommandError(f"scan {pk} does not exist")
        rows = approved_rows()
        if pks:
            rows = rows.filter(scan_id__in=pks)
        left_out = 0
        if options["with_footnotes"]:
            approved = rows.count()
            rows = with_footnotes(rows)
            left_out = approved - rows.count()
        written = current = failed = 0
        ordered = rows.order_by(
            "scan_id", "first_printed_page", "index_in_page"
        )
        for opinion in ordered.iterator(chunk_size=CHUNK_SIZE):
            if opinion.approved_at is None:
                # An approval that names no time names no key.
                failed += 1
                self.stderr.write(
                    f"{opinion} of scan {opinion.scan_id}: no approval time"
                )
                continue
            key = opinion_review.approved_key(
                opinion, opinion.ensemble_edit_revision, opinion.approved_at
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
        if options["with_footnotes"]:
            self.stdout.write(
                f"Left out {left_out} approved opinion(s) with no footnote"
            )
