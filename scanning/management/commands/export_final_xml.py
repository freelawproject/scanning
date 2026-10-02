"""Store the final XML of the approved opinions for CourtListener (#408).

The collect tick does this work by itself (pass fifteen,
``final_xml.export_due``), a few rows per tick. This command does it
for the named volumes, or for every volume with ``--all``, at once:
after the deploy that adds the export, and after a change of
``casebody.SCHEMA``, which makes every stored document old.

It also takes the rows the tick stopped trying at
``final_xml.MAX_ATTEMPTS``: their count starts again, so a fault fixed
in the code is one run of this command.

Last, it deletes the objects under ``final-xml/`` that no stamp names
(``final_xml.orphan_keys``): the files of a deleted scan whose admin
sweep failed. The tick does not list the prefix; this command does.

It holds the exporter's lock, the one the tick takes, and refuses when
the tick holds it: run it again a moment later.

Examples:

    # Say what would be written and deleted, and change nothing.
    docker exec scanning-daemon python manage.py \\
        export_final_xml --all --dry-run

    # Store the final XML of two volumes.
    docker exec scanning-daemon python manage.py export_final_xml 3593 2845
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import casebody, final_xml, s3_sync
from scanning.models import Opinion, Scan


class Command(BaseCommand):
    help = (
        "Store the final XML of every approved and tagged opinion of the "
        "named volumes, or of every volume with --all, and delete the "
        "stored XML of an opinion that is no longer approved."
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
            help="Scan numbers whose opinions are exported.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Export the opinions of every volume.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written and deleted, and change "
            "nothing.",
        )

    def handle(self, *args, **options):
        """Export every named scan's owed final XML, or report it.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If a named scan does not exist, if the call
            names scans and passes ``--all`` or does neither, or if S3
            is off.
        """
        pks = options["scan_pks"]
        if options["all"] and pks:
            raise CommandError("name the scans or pass --all, not both")
        if not options["all"] and not pks:
            raise CommandError("name the scans, or pass --all")
        for pk in pks:
            if not Scan.objects.filter(pk=pk).exists():
                raise CommandError(f"scan {pk} does not exist")
        rows = Opinion.objects.all()
        prefixes = [s3_sync.FINAL_XML_PREFIX]
        if pks:
            rows = rows.filter(scan_id__in=pks)
            prefixes = [s3_sync.final_xml_prefix(pk) for pk in pks]
        dry_run = options["dry_run"]
        if not dry_run and not s3_sync.s3_active():
            raise CommandError("S3 is off: nothing can be exported")
        with final_xml.exporter_lock() as held:
            if not held:
                raise CommandError(
                    "the collect tick is exporting now; run this again in "
                    "a moment"
                )
            owed = list(final_xml.owed_rows(rows))
            withdrawn = list(final_xml.withdrawn_rows(rows))
            orphans = [
                key
                for prefix in prefixes
                for key in final_xml.orphan_keys(prefix)
            ]
            if dry_run:
                self._report(owed, withdrawn, orphans)
                return
            Opinion.objects.filter(pk__in=[o.pk for o in owed]).update(
                final_xml_attempts=0
            )
            done = final_xml.export_rows(None, rows)
            deleted = 0
            for key in orphans:
                if s3_sync.delete_object(key):
                    deleted += 1
                    self.stdout.write(f"deleted the orphan {key}")
                else:
                    self.stderr.write(f"could not delete the orphan {key}")
        left = final_xml.owed_rows(rows).count()
        self.stdout.write(
            f"Wrote or deleted {done} final XML document(s) under schema "
            f"{casebody.SCHEMA}, deleted {deleted} orphan(s); {left} still "
            "owed (the log has the reasons)"
        )

    def _report(self, owed, withdrawn, orphans):
        for opinion in owed:
            self.stdout.write(
                f"write {final_xml.key(opinion)} ({opinion} of scan "
                f"{opinion.scan_id})"
            )
        for opinion in withdrawn:
            self.stdout.write(
                f"delete {final_xml.key(opinion)} ({opinion} of scan "
                f"{opinion.scan_id}, {opinion.status})"
            )
        for key in orphans:
            self.stdout.write(f"delete the orphan {key}")
        self.stdout.write(
            f"Would write {len(owed)} and delete {len(withdrawn)} final "
            f"XML document(s) under schema {casebody.SCHEMA}, and delete "
            f"{len(orphans)} orphan(s)"
        )
