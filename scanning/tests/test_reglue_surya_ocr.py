"""Tests for the ``reglue_surya_ocr`` command (issue #368).

The command is the Surya entry of ``reglue_extract``, whose walk the
Mistral twin's tests cover in full. What is tested here is that this
entry is wired to this engine: the two objects it writes, the fields it
stamps, and the one thing it must never do -- write a document for a
run whose edited pages nobody has read.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from scanning import apply, surya
from scanning.models import ExternalJob, JobStatus
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_surya_apply_glue import SURYA, SuryaApplyTestCase


@override_settings(**SURYA)
class TestReglueSuryaOcr(SuryaApplyTestCase):
    """Run over the stored results, with no GPU call and no new row."""

    def run_command(self, *args):
        """Call the command and return what it printed.

        :param args: Command-line arguments.
        :returns: The standard output.
        :rtype: str
        """
        out = StringIO()
        call_command("reglue_surya_ocr", *args, stdout=out, stderr=out)
        return out.getvalue()

    def build_glued(self):
        """Read a volume, glue it, and glue its corrected volume.

        :returns: The standing apply run.
        """
        run, _edits = self.read_run()
        surya.finish_ready_applies()
        run.refresh_from_db()
        return run

    def test_a_read_volume_is_glued_again(self):
        self.build_glued()
        key = surya.glued_result_key(self.scan, 1)
        del self.objects[key]

        output = self.run_command()

        self.assertIn(key, self.objects)
        self.assertIn("Glued 1 volume(s)", output)
        document = self.objects[key]
        self.assertEqual(document["schema_version"], surya.GLUE_SCHEMA_VERSION)
        self.assertEqual(document["engine"], "surya")
        self.assertEqual(len(document["pages"]), self.PAGES)

    def test_the_corrected_volume_is_written_again(self):
        run = self.build_glued()
        written = run.surya_key
        del self.objects[written]

        output = self.run_command()

        self.assertIn(written, self.objects)
        self.assertIn("corrected volume(s)", output)
        run.refresh_from_db()
        self.assertEqual(run.surya_key, written)
        self.assertEqual(run.surya_run, 1)
        self.assertEqual(
            self.objects[written]["apply_run"],
            run.label,
        )

    def test_the_dry_run_writes_nothing(self):
        run = self.build_glued()
        keys = set(self.objects)

        output = self.run_command("--dry-run")

        self.assertIn("Would glue 1 volume(s)", output)
        self.assertEqual(set(self.objects), keys)
        self.assertEqual(self.objects[run.surya_key]["apply_run"], run.label)

    def test_a_run_whose_pages_are_unread_is_left_as_it_is(self):
        """The normal case after a deploy: a person runs the command
        before the tick has created the one-page rows."""
        run, _edits = self.built_run()
        self.volume_surya_run()

        output = self.run_command()

        run.refresh_from_db()
        self.assertEqual(run.surya_key, "")
        self.assertIsNone(run.surya_run)
        self.assertIn("not read yet", output)

    def test_no_row_is_created(self):
        run, _edits = self.built_run()
        self.volume_surya_run()
        before = ExternalJob.objects.count()

        self.run_command()

        self.assertEqual(ExternalJob.objects.count(), before)
        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 0)

    def test_an_open_run_is_skipped(self):
        self.built_run()
        rows = surya.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.SUBMITTED
        )

        self.assertIn("skipped 1 open run(s)", self.run_command())

    def test_a_named_scan_with_no_run_is_an_error(self):
        with self.assertRaises(CommandError) as caught:
            self.run_command(str(self.scan.pk))

        self.assertIn("no Surya run", str(caught.exception))

    def test_a_mistral_run_is_no_candidate_here(self):
        """Both engines are ``EXTRACT``: each command reads its own
        rows and never the other's."""
        from scanning import mistral_ocr

        self.built_run()
        mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )

        with self.assertRaises(CommandError):
            self.run_command(str(self.scan.pk))

    def test_the_tick_still_reads_the_pages_afterwards(self):
        """The run is left alone, not lost: the next tick creates its
        rows and glues it once they answer."""
        run, _edits = self.built_run()
        self.volume_surya_run()
        self.run_command()

        surya.finish_ready_applies()
        run.refresh_from_db()
        self.complete_surya_rows(run)

        self.assertEqual(surya.finish_ready_applies(), 1)
        run.refresh_from_db()
        self.assertEqual(
            run.surya_key,
            f"{apply.run_prefix(self.scan, run)}surya-volume.json",
        )
        self.assertEqual(
            [
                page
                for page in self.objects[run.surya_key]["pages"]
                if "error" in page
            ],
            [],
        )
