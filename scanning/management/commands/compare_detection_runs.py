"""Compare two merged detection runs of one volume (#338).

The re-read of #338 replaces every model detection of a volume, and a
curator's decisions land on the new rows by address and IoU
(``detections.resolve``). The checkpoint is the same, so the boxes of
the classes the first run already had should come back where they
were. This command measures that before the corpus is re-read, and
changes nothing: per label, how many boxes each run holds and how many
of the old ones the new run matches (same page, same label, IoU at
least ``detections.IOU_THRESHOLD``, each box taken once), and how many
standing decisions would find no box in the new run.

It reads the two merged volume documents, in the original's page
space, so a decision on a page a curator inserted or replaced is
counted apart and not checked.

Examples:

    # The live run against the run before it.
    docker exec scanning-daemon python manage.py compare_detection_runs 2845

    # Two named runs.
    docker exec scanning-daemon python manage.py compare_detection_runs \\
        2845 --old 1 --new 3
"""

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from scanning import detections, yolo
from scanning.models import ExternalJob, JobEngine, JobStage, JobStatus, Scan


class Command(BaseCommand):
    help = (
        "Compare two merged detection runs of a volume, label by label, "
        "and say how many standing decisions the newer one would lose."
    )

    def add_arguments(self, parser):
        """Register the CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument("scan_pk", type=int, help="The scan to compare.")
        parser.add_argument(
            "--old",
            type=int,
            default=None,
            help="The earlier run; the merged run before --new by default.",
        )
        parser.add_argument(
            "--new",
            type=int,
            default=None,
            help="The later run; the live run by default.",
        )

    def handle(self, *args, **options):
        """Print the comparison.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If the scan or a merged run is missing.
        """
        scan = Scan.objects.filter(pk=options["scan_pk"]).first()
        if scan is None:
            raise CommandError(f"scan {options['scan_pk']}: no such scan")
        merged = merged_runs(scan)
        new = options["new"] or (merged[-1] if merged else None)
        old = options["old"] or next(
            (run for run in reversed(merged) if new and run < new), None
        )
        for name, run in (("--new", new), ("--old", old)):
            if run is None or run not in merged:
                raise CommandError(
                    f"scan {scan.pk}: no merged detection run for {name} "
                    f"(merged runs: {merged or 'none'})"
                )

        try:
            old_entries = yolo.load_merged_document(scan, old)["detections"]
            new_entries = yolo.load_merged_document(scan, new)["detections"]
        except yolo.DetectMergeError as exc:
            raise CommandError(str(exc)) from exc

        report = compare(old_entries, new_entries)
        self.stdout.write(f"scan {scan.pk}: run {old} against run {new}")
        self.stdout.write(
            f"{'label':<22}{'old':>7}{'new':>7}{'matched':>9}"
            f"{'mean IoU':>10}{'lost':>7}{'added':>7}"
        )
        for label in sorted(report):
            row = report[label]
            mean = (
                f"{row['iou_sum'] / row['matched']:.3f}"
                if row["matched"]
                else "-"
            )
            self.stdout.write(
                f"{label:<22}{row['old']:>7}{row['new']:>7}"
                f"{row['matched']:>9}{mean:>10}"
                f"{row['old'] - row['matched']:>7}"
                f"{row['new'] - row['matched']:>7}"
            )

        landed, lost, unchecked = check_decisions(scan, new_entries)
        self.stdout.write(
            f"standing decisions: {landed} would land, {len(lost)} would "
            f"be stale, {unchecked} on edited pages not checked"
        )
        for decision in lost[:20]:
            self.stdout.write(
                f"  #{decision.pk} {decision.kind} {decision.label} "
                f"p.{decision.source_page}"
            )


def merged_runs(scan) -> list[int]:
    """Return the scan's volume detection runs whose every row is merged.

    :param scan: The scan.
    :returns: The run numbers, ascending.
    :rtype: list[int]
    """
    statuses: dict[int, set[str]] = defaultdict(set)
    for run, status in ExternalJob.objects.filter(
        scan=scan,
        stage=JobStage.DETECT,
        engine=JobEngine.BLACKLETTER,
        opinion=None,
        apply_run__isnull=True,
    ).values_list("run", "status"):
        statuses[run].add(status)
    return sorted(
        run for run, seen in statuses.items() if seen == {JobStatus.CONSUMED}
    )


def _bbox(entry: dict) -> list[float]:
    """Return an entry's box.

    :param entry: One merged-document detection.
    :returns: ``[x0, y0, x1, y1]``.
    :rtype: list[float]
    """
    return entry.get("bbox") or [0, 0, 0, 0]


def compare(old_entries: list[dict], new_entries: list[dict]) -> dict:
    """Match the old boxes to the new ones, label by label.

    The rule of ``detections.resolve``: same page, same label, the best
    IoU at least ``IOU_THRESHOLD``, each new box taken once.

    :param old_entries: The earlier run's detections.
    :param new_entries: The later run's detections.
    :returns: ``{label: {old, new, matched, iou_sum}}``.
    :rtype: dict
    """
    report: dict[str, dict] = defaultdict(
        lambda: {"old": 0, "new": 0, "matched": 0, "iou_sum": 0.0}
    )
    by_address: dict[tuple, list[dict]] = defaultdict(list)
    for entry in new_entries:
        report[entry["label"]]["new"] += 1
        by_address[(entry.get("pdf_page"), entry["label"])].append(entry)
    taken: set[int] = set()
    for entry in old_entries:
        row = report[entry["label"]]
        row["old"] += 1
        best, best_iou = None, 0.0
        for candidate in by_address.get(
            (entry.get("pdf_page"), entry["label"]), []
        ):
            if id(candidate) in taken:
                continue
            score = detections.iou(_bbox(entry), _bbox(candidate))
            if score > best_iou:
                best, best_iou = candidate, score
        if best is None or best_iou < detections.IOU_THRESHOLD:
            continue
        taken.add(id(best))
        row["matched"] += 1
        row["iou_sum"] += best_iou
    return dict(report)


def check_decisions(scan, new_entries: list[dict]):
    """Say which standing decisions would find a box in the new run.

    :param scan: The scan.
    :param new_entries: The later run's detections, in the original's
        page space.
    :returns: ``(landed, lost decisions, decisions on edited pages)``.
    :rtype: tuple[int, list, int]
    """
    by_address: dict[tuple, list[dict]] = defaultdict(list)
    for entry in new_entries:
        by_address[(entry.get("pdf_page"), entry["label_id"])].append(entry)
    landed, lost, unchecked = 0, [], 0
    taken: set[int] = set()
    for decision in detections.standing_decisions(scan).order_by("pk"):
        if decision.source_edit_id is not None:
            unchecked += 1
            continue
        best, best_iou = None, 0.0
        for candidate in by_address.get(
            (decision.source_page, decision.label_id), []
        ):
            if id(candidate) in taken:
                continue
            score = detections.iou(_bbox(candidate), decision.target_bbox)
            if score > best_iou:
                best, best_iou = candidate, score
        if best is None or best_iou < detections.IOU_THRESHOLD:
            lost.append(decision)
            continue
        taken.add(id(best))
        landed += 1
    return landed, lost, unchecked
