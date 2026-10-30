"""Delete the glues of the old revisions of a volume's opinions (#452).

Every re-glue writes the opinion's glues under a new ``r{n}/`` folder:
the engine documents, the manifest, the redacted PDF and the ensemble
documents. Nothing reads an older folder once the live revision is
complete, and the promotion to the text review deletes them from then
on (``opinions.prune_glues``). This command deletes the folders that
were written before that, and it is the retry of a deletion that
failed.

Only a row whose live revision is complete is pruned
(``opinions.text_review_ready``), an approved row included: it keeps
its revision, so its live folder stays. The approved texts
(``approved/``), the tagger input (``tag/``) and every paid result
outside the opinion's folder are never deleted.

Examples:

    # Say what would go, and delete nothing.
    docker exec scanning-daemon python manage.py prune_opinion_glues \\
        2845 --dry-run

    # Delete the old glues of every volume.
    docker exec scanning-daemon python manage.py prune_opinion_glues --all
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import opinions
from scanning.models import Opinion, Scan


class Command(BaseCommand):
    help = (
        "Delete the glues of the revisions below the live one, for every "
        "opinion of the named volumes whose live revision is complete, or "
        "of every volume with --all."
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
            help="Scan numbers whose opinions are pruned.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Prune the opinions of every volume that has one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted, and delete nothing.",
        )

    def handle(self, *args, **options):
        """Prune, or report, the opinions of every named scan.

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
        if options["all"]:
            pks = list(
                Opinion.objects.order_by("scan_id")
                .values_list("scan_id", flat=True)
                .distinct()
            )
        total = failed = 0
        for pk in pks:
            scan = Scan.objects.filter(pk=pk).first()
            if scan is None:
                raise CommandError(f"scan {pk} does not exist")
            rows = (
                Opinion.objects.filter(scan=scan)
                .select_related("scan", "scan__reporter")
                .order_by("first_printed_page", "index_in_page")
            )
            count = 0
            for opinion in rows:
                if not opinions.text_review_ready(opinion):
                    continue
                keys = opinions.prune_glues(opinion, dry_run=dry_run)
                if keys is None:
                    failed += 1
                    continue
                count += len(keys)
            self.stdout.write(
                f"scan {pk}: {'would delete' if dry_run else 'deleted'} "
                f"{count} object(s)"
            )
            total += count
        self.stdout.write(
            f"{'Would delete' if dry_run else 'Deleted'} {total} object(s)"
        )
        if failed:
            self.stderr.write(
                f"{failed} opinion(s) could not be listed; run the command "
                "again"
            )
