"""Measure the review-2 findings the dots.mocr text could raise (#303).

Step 1 of issue #303, and it changes nothing. Every review-2 finding
comes from a YOLO row, so a page YOLO read wrong raises no card. The
dots.mocr cells are a second witness the pipeline already paid for, and
this command measures what that witness would say before any check is
built on it.

Three probes, one per candidate check (``scanning.text_findings``):

- an arrow glyph in a cell, with no ``KEY_ICON`` row near it;
- a bracketed number at the start of a cell's text, with no
  ``HEADNOTE_BRACKET`` or ``HEADNOTE`` row near it;
- a ``Page-header`` cell with no white ``Redaction`` over it.

The report answers item 1 of the issue's work list: per glyph, how many
cells hold it and how many of those no key icon met; the same two
counts for the brackets and for the running heads; and the census of
every ``category`` value the corpus holds, which no code may assume.

**The uncovered share is the number to read, not the count.** A probe
whose cells are nearly all covered found a witness that agrees with
YOLO, so the check it stands for would be quiet and trustworthy. A
probe whose cells are nearly all uncovered found a pattern that does
not mean what the issue guessed, and the check would be noise. The
issue names no threshold: the survey reports, a person decides.

It reads the cells of the space the rows were measured in
(``detections.measured_run``), which is the rule
``text_fit.load_document`` owns, so the cells and the rows are always
in one page space. A volume whose document does not load is counted
under its reason and costs nothing else.

Examples:

    # The whole corpus, with five example pages per probe.
    docker exec scanning-daemon python manage.py survey_text_findings

    # A first look at thirty volumes.
    docker exec scanning-daemon python manage.py survey_text_findings \\
        --limit 30

    # Two named volumes, with every example page.
    docker exec scanning-daemon python manage.py survey_text_findings \\
        3374 3164 --examples 0
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import detections, s3_sync, text_findings
from scanning.models import Redaction, Scan


class Command(BaseCommand):
    help = (
        "Measure what the dots.mocr text would say about the redactions "
        "and the detections of every computed volume. Writes nothing."
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
            help="Scan numbers to survey; every computed volume when "
            "the list is empty.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Read at most this many volumes; 0 for all of them.",
        )
        parser.add_argument(
            "--examples",
            type=int,
            default=5,
            help="Example pages to print per probe; 0 for all of them.",
        )

    def handle(self, *args, **options):
        """Survey every eligible volume and report the totals.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If S3 is off, since the cells live in the
            bucket and there is nothing to read without it.
        """
        if not s3_sync.s3_active():
            raise CommandError(
                "S3 is not active here; the dots.mocr cells live in the "
                "bucket and nothing can be read without it."
            )

        wanted = options["scan_pks"]
        scans = self._candidates(wanted, options["limit"])
        total = text_findings.Survey()

        for scan in scans:
            run = detections.measured_run(scan)
            regions = text_findings.load_regions(scan, run)
            if not regions:
                total.skipped["the OCR volume holds no cell"] += 1
                self._say_skip(scan, wanted, "the OCR volume holds no cell")
                continue
            try:
                one = text_findings.survey_scan(scan, run, regions)
            except Exception as exc:
                # One volume's bad document must not end a corpus pass.
                total.skipped["the survey raised"] += 1
                self.stderr.write(f"scan {scan.pk}: {exc}")
                continue
            total.add(one)
            self._say_volume(scan, wanted, one)

        for pk in sorted(set(wanted) - {scan.pk for scan in scans}):
            self.stderr.write(
                f"scan {pk}: no computed redaction row; the compute has "
                "not reached it"
            )

        self.stdout.write(self._report(total, options["examples"]))
        self.stdout.write(
            self.style.SUCCESS(
                f"{total.volumes} volume(s) surveyed, "
                f"{sum(total.skipped.values())} skipped, nothing written"
            )
        )

    def _candidates(self, wanted: list[int], limit: int) -> list[Scan]:
        """Return the volumes the survey may read, in pk order.

        A computed ``Redaction`` row is the proof the compute ran, and
        the compute is what puts the rows and the cells in one page
        space. A status filter would add nothing: a volume with those
        rows has geometry to measure whatever review it sits in.

        :param wanted: The scan numbers the operator named, if any.
        :param limit: The most volumes to read, or 0 for all.
        :returns: The eligible scans.
        :rtype: list[Scan]
        """
        computed = (
            Redaction.objects.computed()
            .values_list("scan_id", flat=True)
            .distinct()
        )
        scans = Scan.objects.filter(pk__in=list(computed)).order_by("pk")
        if wanted:
            scans = scans.filter(pk__in=wanted)
        if limit > 0:
            scans = scans[:limit]
        return list(scans)

    def _say_skip(self, scan, wanted: list[int], reason: str) -> None:
        """Give the reason for a volume the operator named.

        The unnamed rest is a count, or a corpus pass would print a
        line for every volume with nothing to read.

        :param scan: The scan skipped.
        :param wanted: The scan numbers the operator named.
        :param reason: Why it was skipped.
        :return: None.
        """
        if scan.pk in wanted:
            self.stderr.write(f"scan {scan.pk}: {reason}")

    def _say_volume(self, scan, wanted: list[int], one) -> None:
        """Report one named volume's own counts.

        :param scan: The scan surveyed.
        :param wanted: The scan numbers the operator named.
        :param one: That volume's totals.
        :return: None.
        """
        if scan.pk not in wanted:
            return
        glyphs = sum(counts.cells for counts in one.glyphs.values())
        loose = sum(counts.uncovered for counts in one.glyphs.values())
        self.stdout.write(
            f"scan {scan.pk}: {one.pages} page(s), "
            f"{glyphs} glyph cell(s) ({loose} uncovered), "
            f"{one.brackets.cells} bracket cell(s) "
            f"({one.brackets.uncovered} uncovered), "
            f"{one.headers.cells} running head(s) "
            f"({one.headers.uncovered} uncovered)"
        )

    def _report(self, total, examples: int) -> str:
        """Return the lines that answer item 1 of the issue's work list.

        :param total: The corpus totals.
        :param examples: Example pages to print per probe, 0 for all.
        :returns: The report.
        :rtype: str
        """
        lines = [
            f"{total.volumes} volume(s), {total.pages} page(s) with cells",
            "",
            "the arrow glyphs, by code point:",
        ]
        if total.glyphs:
            lines.extend(self._glyph_lines(total))
        else:
            lines.append("  none found")
        lines.extend(
            [
                "",
                "the bracketed numbers: "
                + self._probe_line(total.brackets, "HEADNOTE row"),
                "the running heads: "
                + self._probe_line(total.headers, "white row"),
                "",
                "the cell categories:",
            ]
        )
        for category, count in total.categories.most_common():
            lines.append(f"  {category}: {count}")
        lines.extend(self._example_lines(total, examples))
        if total.skipped:
            lines.append("")
            lines.append("skipped:")
            for reason, count in total.skipped.most_common():
                lines.append(f"  {reason}: {count}")
        return "\n".join(lines)

    def _glyph_lines(self, total) -> list[str]:
        """Return one line per glyph, the named candidates first.

        :param total: The corpus totals.
        :returns: The lines.
        :rtype: list[str]
        """

        def order(item):
            glyph = item[0]
            named = text_findings.NAMED_GLYPHS.find(glyph)
            return (0, named) if named >= 0 else (1, -item[1].cells)

        lines = []
        for glyph, counts in sorted(total.glyphs.items(), key=order):
            named = (
                " (named in #303)"
                if glyph in (text_findings.NAMED_GLYPHS)
                else ""
            )
            lines.append(
                f"  U+{ord(glyph):04X} {glyph}{named}: "
                + self._probe_line(counts, "KEY_ICON row")
            )
        return lines

    def _probe_line(self, counts, what: str) -> str:
        """Return one probe's counts as a line.

        :param counts: The probe's counts.
        :param what: What a covered cell was met by.
        :returns: The line.
        :rtype: str
        """
        if not counts.cells:
            return "no cell matched"
        return (
            f"{counts.cells} cell(s), {counts.hits} match(es); "
            f"{counts.covered} met a {what}, {counts.uncovered} did not "
            f"({counts.share:.1%} uncovered)"
        )

    def _example_lines(self, total, examples: int) -> list[str]:
        """Return the example pages of each probe with an uncovered cell.

        A page address is ``scan/page``, the 1-based page of the space
        the rows are in, so a curator can open it.

        :param total: The corpus totals.
        :param examples: The most to print per probe, 0 for all.
        :returns: The lines.
        :rtype: list[str]
        """
        probes = [
            *(
                (f"U+{ord(glyph):04X} {glyph}", counts)
                for glyph, counts in total.glyphs.items()
            ),
            ("the bracketed numbers", total.brackets),
            ("the running heads", total.headers),
        ]
        lines = []
        for name, counts in probes:
            if not counts.pages:
                continue
            pages = sorted(counts.pages)
            shown = pages if examples <= 0 else pages[:examples]
            where = ", ".join(f"{pk}/{index + 1}" for pk, index in shown)
            more = (
                f", and {len(pages) - len(shown)} more"
                if len(shown) < len(pages)
                else ""
            )
            lines.append(f"  {name}: {where}{more}")
        if lines:
            lines.insert(0, "")
            lines.insert(1, "example pages with an uncovered cell:")
        return lines
