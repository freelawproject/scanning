"""Glue again the dots.mocr runs whose stored results hold a filtered page.

Issue #242. A ``filtered`` page is one whose answer was good layout
JSON with one character wrong: a lone quotation mark, a lone backslash,
a doubled closer. Upstream's parser discards the array and keeps the
words, so the page reaches the reader with no cell and no page number.
The glue now repairs such a page from the ``raw`` answer the result
object keeps (``scanning.layout_json``), and the worker does the same
before it spends a retry rung. Neither reaches a volume already glued:
the glue ran once, and ``applied_at`` closes the run.

This command hands those volumes back. For every scan in review 1
whose live run reports a filtered page, it runs the glue again over the
stored shard results -- the glue is idempotent and the results are kept
for good -- which rewrites the volume document with the repaired cells
and re-stamps the rows, and then clears the apply stamp
(``dots_mocr.reopen_apply``) so the next collect tick reads the page
numbers again. It starts no GPU job and creates no row.

**Run this before ``reread_failed_pages``.** That command reads
``filtered_pages`` off the row (``jobs.has_unread_pages``), and a run
glued before this deploy still carries them. So a re-read started
first pays RunPod for the very shards this command repairs from the
stored results for nothing. Re-glue first; whatever still reports a
filtered page afterwards is a shape no arm reaches, and only then is
a paid re-read worth considering.

``--dry-run`` downloads the shard results, runs the repair in memory
and writes nothing. Its report is the survey the issue asks for: one
line per filtered page saying which arm repairs it, or the parser
message with an excerpt of the answer when none does, or that the
result was written before the worker kept ``raw``. Then the rate, the
share each arm answers, and the count of filtered answers the
threshold rung of #238 recovered -- the measurement that says whether
that rung is worth paying for this class of fault.

The dry run reads **every** glued volume in review 1, including the
ones whose rows report no filtered page: a volume whose rung recovered
every filtered answer holds no filtered page to be found by, and it is
exactly the evidence the rung works. It is also the page total the
rate divides by. So the survey costs one download per shard of the
corpus, and it changes nothing.

Safe by the two properties of ``reapply_page_numbers``: a volume in
``READY_FOR_PAGE_COMPLETENESS_REVIEW`` is a recompute and keeps its
status, the numbers a curator typed are ``PageEdit`` rows and survive
every apply, and an approved volume is not in ``APPLY_STATUSES``.

Examples:

    # Say which pages the repair reaches, and change nothing.
    docker exec scanning-daemon python manage.py reglue_dots_mocr \\
        --dry-run

    # Glue every eligible volume again.
    docker exec scanning-daemon python manage.py reglue_dots_mocr

    # Two named volumes only.
    docker exec scanning-daemon python manage.py reglue_dots_mocr \\
        2726 2702
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import dots_mocr, jobs, s3_sync
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
)


class Command(BaseCommand):
    help = (
        "Glue again every dots.mocr run in review 1 whose stored results "
        "hold a filtered page, so the repaired cells reach the reader."
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
            help="Scan numbers to glue again; every eligible scan when "
            "the list is empty.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what the repair would reach, and change nothing.",
        )

    def handle(self, *args, **options):
        """Glue again, or survey, every eligible scan.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If S3 is off, since every shard result
            lives in the bucket and there is nothing to read without it.
        """
        dry_run = options["dry_run"]
        wanted = options["scan_pks"]

        if not s3_sync.s3_active():
            raise CommandError(
                "S3 is not active here; the shard results live in the "
                "bucket and nothing can be read without it."
            )

        scans = self._candidates(wanted)
        survey = _Survey()
        reglued = skipped = 0

        for scan in scans:
            rows = dots_mocr.live_analyze_jobs(scan)
            if not rows or any(
                row.status != JobStatus.CONSUMED for row in rows
            ):
                skipped += 1
                self._say_skip(
                    scan, wanted, "the live dots.mocr run is not glued yet"
                )
                continue
            if not dry_run and not any(
                jobs.page_lists(row)["filtered_pages"] for row in rows
            ):
                # Only the writing pass may skip on the rows. A survey
                # that skipped here would never look at a volume whose
                # retry rung recovered every filtered answer -- exactly
                # the population that says the rung works -- and its
                # page total, the denominator of the rate, would count
                # only the volumes that still hold a hole.
                skipped += 1
                self._say_skip(
                    scan,
                    wanted,
                    "no shard of the live run reports a filtered page",
                )
                continue

            try:
                if dry_run:
                    self._survey_scan(scan, rows, survey)
                else:
                    self._reglue_scan(scan, rows)
            except Exception as exc:
                # One volume's bad result must not end the pass: the
                # command is run over the whole corpus.
                skipped += 1
                self.stderr.write(f"scan {scan.pk}: {exc}")
                continue
            reglued += 1

        for pk in sorted(set(wanted) - {scan.pk for scan in scans}):
            self.stderr.write(
                f"scan {pk}: no glued run in review 1; nothing to do"
            )

        if dry_run:
            self.stdout.write(survey.summary())
        self.stdout.write(
            self.style.SUCCESS(
                f"{reglued} scan(s) "
                f"{'surveyed' if dry_run else 'glued again'}, "
                f"{skipped} skipped{' (dry run)' if dry_run else ''}"
            )
        )

    def _candidates(self, wanted: list[int]) -> list[Scan]:
        """Return the scans the pass may act on, in pk order.

        The same set as ``reapply_page_numbers``: a glued run, and a
        status the apply pass reads. An approved volume is not among
        them.

        :param wanted: The scan numbers the operator named, if any.
        :returns: The eligible scans.
        :rtype: list[Scan]
        """
        glued = (
            ExternalJob.objects.filter(
                stage=JobStage.ANALYZE,
                engine=JobEngine.DOTS_MOCR,
                provider=JobProvider.RUNPOD,
                status=JobStatus.CONSUMED,
            )
            .values_list("scan_id", flat=True)
            .distinct()
        )
        scans = Scan.objects.filter(
            pk__in=list(glued), status__in=dots_mocr.APPLY_STATUSES
        ).order_by("pk")
        if wanted:
            scans = scans.filter(pk__in=wanted)
        return list(scans)

    def _say_skip(self, scan, wanted: list[int], reason: str) -> None:
        """Give the reason for a scan the operator named.

        The unnamed rest is a count: a corpus-wide pass would otherwise
        print a line for every volume with nothing to do.

        :param scan: The scan skipped.
        :param wanted: The scan numbers the operator named.
        :param reason: Why it was skipped.
        :return: None.
        """
        if scan.pk in wanted:
            self.stderr.write(f"scan {scan.pk}: {reason}")

    def _survey_scan(self, scan, rows, survey: "_Survey") -> None:
        """Report one scan's filtered pages, and write nothing.

        :param scan: The scan to survey.
        :param rows: Its live run's rows.
        :param survey: The running totals.
        :return: None.
        """
        result = dots_mocr.survey_repairs(scan, rows)
        survey.rung_recoveries += result["rung_recoveries"]
        survey.pages += result["pages"]
        survey.volumes += 1
        for report in result["reports"]:
            where = (
                f"scan {scan.pk} volume page {report['pdf_page']} "
                f"(shard {report['shard_index'] + 1} page "
                f"{report['page_no']})"
            )
            survey.filtered += 1
            if report["edits"] is not None:
                survey.count_repair(report["edits"])
                self.stdout.write(
                    f"{where}: would repair by {', '.join(report['edits'])}"
                )
            elif report["no_raw"]:
                survey.no_raw += 1
                self.stdout.write(
                    f"{where}: no answer stored (read before the worker "
                    "kept it)"
                )
            else:
                survey.unrepaired += 1
                self.stdout.write(f"{where}: cannot repair: {report['fault']}")

    def _reglue_scan(self, scan, rows) -> None:
        """Glue one scan again and hand it back to the apply.

        :param scan: The scan to glue again.
        :param rows: Its live run's rows.
        :return: None.
        """
        dots_mocr.merge_dotsmocr_results(scan, rows)
        reopened = dots_mocr.reopen_apply(scan)
        left = sum(
            len(jobs.page_lists(row)["filtered_pages"])
            for row in dots_mocr.live_analyze_jobs(scan)
        )
        self.stdout.write(
            f"scan {scan.pk}: glued again, {left} filtered page(s) left, "
            + (
                "handed back to the apply"
                if reopened
                else "the apply had not run yet"
            )
        )


class _Survey:
    """The dry run's running totals, and the line that reports them."""

    def __init__(self):
        self.filtered = 0
        self.repaired = 0
        self.unrepaired = 0
        self.no_raw = 0
        self.rung_recoveries = 0
        self.pages = 0
        self.volumes = 0
        self.arms: dict[str, int] = {}

    def count_repair(self, edits: list[str]) -> None:
        """Count one repaired page and the arms it took.

        :param edits: The edit names, each ``<arm>@<offset>``.
        :return: None.
        """
        self.repaired += 1
        for edit in edits:
            arm = edit.split("@", 1)[0]
            self.arms[arm] = self.arms.get(arm, 0) + 1

    def summary(self) -> str:
        """Return the lines that answer items 1 to 3 of issue #242.

        The rate is the point of the first line, so it names both
        numbers and the one-in-N form the issue reports: "17 filtered
        page(s) in 13159, about one in 774".

        :returns: The rate of the fault, the share each arm answers,
            and what the threshold rung of #238 recovered.
        :rtype: str
        """
        by_arm = ", ".join(
            f"{arm} {n}" for arm, n in sorted(self.arms.items())
        )
        rate = (
            f", about one in {round(self.pages / self.filtered)}"
            if self.filtered
            else ""
        )
        return "\n".join(
            [
                f"{self.filtered} filtered page(s) in {self.pages} page(s) "
                f"of {self.volumes} volume(s){rate}",
                f"  {self.repaired} repairable"
                f"{f' ({by_arm})' if by_arm else ''}, "
                f"{self.unrepaired} not, "
                f"{self.no_raw} with no stored answer",
                "  the threshold rung of #238 recovered "
                f"{self.rung_recoveries} filtered answer(s)",
            ]
        )
