"""Tag the approved opinions again after a change of the weights (#573).

The tagger's weights are part of every row's identity (``tagger.MODEL``),
so a new checkpoint leaves the spans of the old one in place, and they
still export. This command tags the approved opinions again with the
current weights: every ``TEXT_REVIEW_DONE`` opinion that holds spans
the current weights did not place (``tagger.has_current_spans``),
through ``tagger.ensure_tag_jobs``, the call the button makes. That
includes an opinion approved again after its old run, whose spans are
over an earlier text: the run reads the text approved now. A second
call starts nothing for an opinion the first one tagged. An opinion
that was never tagged is left to the button.

Creating a row is what costs money, so run it with ``--dry-run`` first:
it lists what it would start and starts nothing. An opinion whose run
of the current weights is in flight is skipped; one whose result is paid
for already is placed in this call, with no job. The placement moves
``tag_key``, and the export pass writes the final XML again by itself.

Examples:

    # List what would be tagged again, and start nothing.
    docker exec scanning-daemon python manage.py retag_opinions --all --dry-run

    # Tag the approved opinions of two volumes again.
    docker exec scanning-daemon python manage.py retag_opinions 3593 2845
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import s3_sync, tagger
from scanning.models import (
    JobProvider,
    JobStatus,
    Opinion,
    OpinionReviewStatus,
    Scan,
)


def candidates(scan_pks: list[int] | None):
    """Return the approved opinions whose spans other weights wrote.

    :param scan_pks: The volumes to walk, or None for every volume.
    :returns: The opinions, with ``scan``, by scan and key.
    :rtype: list[Opinion]
    """
    rows = (
        Opinion.objects.filter(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .exclude(approved_text_key="")
        .exclude(tag_key="")
        .select_related("scan")
        .defer(*Scan.large_fields("scan__"))
        .order_by("scan_id", "first_printed_page", "index_in_page")
    )
    if scan_pks is not None:
        rows = rows.filter(scan_id__in=scan_pks)
    return [o for o in rows if not tagger.has_current_spans(o)]


class Command(BaseCommand):
    help = (
        "Start a tagger run of the current weights for every approved "
        "opinion of the named volumes, or of every volume with --all, "
        "whose spans older weights wrote."
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
            help="Scan numbers whose opinions are tagged again.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Tag the opinions of every volume again.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be started, and start nothing.",
        )

    def handle(self, *args, **options):
        """Start the runs, or report them.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If a named scan does not exist, if the call
            names scans and passes ``--all`` or does neither, or, outside
            a dry run, if the stage is off or S3 is.
        """
        pks = options["scan_pks"]
        if options["all"] and pks:
            raise CommandError("name the scans or pass --all, not both")
        if not options["all"] and not pks:
            raise CommandError("name the scans, or pass --all")
        for pk in pks:
            if not Scan.objects.filter(pk=pk).exists():
                raise CommandError(f"scan {pk} does not exist")
        dry_run = options["dry_run"]
        if not dry_run:
            if not tagger.enabled():
                raise CommandError(
                    "the tagger is off (TAGGER_ENABLED or its endpoint id): "
                    "nothing can be started"
                )
            if not s3_sync.s3_active():
                raise CommandError(
                    "S3 is off: the worker reads its input there"
                )

        found = candidates(pks or None)
        running = [o for o in found if tagger.has_current_run(o)]
        due = [o for o in found if o not in running]
        for opinion in running:
            self.stdout.write(
                f"skip {opinion} of scan {opinion.scan_id}: a tagger run is "
                "in flight"
            )
        if dry_run:
            for opinion in due:
                self.stdout.write(
                    f"would tag {opinion} of scan {opinion.scan_id} again"
                )
            self.stdout.write(
                f"{len(due)} opinion(s) would be tagged again with "
                f"{tagger.MODEL}; {len(running)} in flight. Dry run: "
                "nothing was started."
            )
            return

        started = placed = failed = 0
        for opinion in due:
            try:
                rows = tagger.ensure_tag_jobs(opinion)
            except tagger.TaggerInputError as exc:
                failed += 1
                self.stderr.write(
                    f"{opinion} of scan {opinion.scan_id}: not started: {exc}"
                )
                continue
            row = rows[0] if rows else None
            if row is not None and row.status in (
                JobStatus.COMPLETED,
                JobStatus.CONSUMED,
            ):
                # A result of these weights is paid for already: placed
                # now, the rule of the button.
                try:
                    tagger.place(opinion, row)
                except tagger.TaggerGlueError as exc:
                    failed += 1
                    self.stderr.write(
                        f"{opinion} of scan {opinion.scan_id}: not placed: "
                        f"{exc}"
                    )
                    continue
                placed += 1
                self.stdout.write(
                    f"placed {opinion} of scan {opinion.scan_id} from run "
                    f"{row.run}"
                )
                continue
            started += 1
            self.stdout.write(
                f"started {opinion} of scan {opinion.scan_id} "
                f"(run {row.run if row else '?'})"
            )
        if started:
            from scanning import tasks

            tasks.submit_soon(JobProvider.RUNPOD)
        self.stdout.write(
            f"{started} run(s) started, {placed} placed from a paid result, "
            f"{failed} failed, {len(running)} in flight, with {tagger.MODEL}."
        )
