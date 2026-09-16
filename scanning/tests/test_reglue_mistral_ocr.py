"""Tests for the ``reglue_mistral_ocr`` command (issue #245).

The glue is the one transform of a stored Mistral result, so a changed
transform must reach the volumes already read. The command is how, and
what it must never do is start paid work: it reads rows and writes two
objects.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from scanning import apply, mistral_ocr
from scanning.models import ApplyRun, ExternalJob, JobStage, JobStatus
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_mistral_apply_glue import (
    MISTRAL,
    MistralApplyTestCase,
)


@override_settings(**MISTRAL)
class TestReglueMistralOcr(MistralApplyTestCase):
    """Run over the stored results, with no API call and no new row."""

    def run_command(self, *args):
        """Call the command and return what it printed.

        :param args: Command-line arguments.
        :returns: The standard output.
        :rtype: str
        """
        out = StringIO()
        call_command("reglue_mistral_ocr", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_a_read_volume_is_glued_again(self):
        self.build_glued()
        key = mistral_ocr.glued_result_key(self.scan, 1)
        del self.objects[key]

        output = self.run_command()

        self.assertIn(key, self.objects)
        self.assertIn("Glued 1 volume(s)", output)

    def test_the_corrected_volume_is_written_again(self):
        run = self.build_glued()
        ApplyRun.objects.filter(pk=run.pk).update(
            extract_key="", extract_run=None
        )

        self.run_command()

        run.refresh_from_db()
        self.assertEqual(
            run.extract_key,
            f"{apply.run_prefix(self.scan, run)}extract-volume.json",
        )
        self.assertEqual(run.extract_run, 1)

    def test_a_dry_run_writes_nothing(self):
        self.build_glued()
        key = mistral_ocr.glued_result_key(self.scan, 1)
        del self.objects[key]

        output = self.run_command("--dry-run")

        self.assertNotIn(key, self.objects)
        self.assertIn("Would glue 1 volume(s)", output)

    def test_no_row_is_created(self):
        run = self.build_glued()
        before = ExternalJob.objects.count()
        ExternalJob.objects.filter(
            apply_run=run, stage=JobStage.EXTRACT
        ).delete()

        self.run_command()

        self.assertLess(ExternalJob.objects.count(), before)
        self.assertEqual(
            ExternalJob.objects.filter(
                apply_run=run, stage=JobStage.EXTRACT
            ).count(),
            0,
        )

    def test_an_open_run_is_skipped(self):
        self.built_run()
        rows = mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.SUBMITTED
        )

        output = self.run_command()

        self.assertIn("skipped 1 open run(s)", output)

    def test_a_named_scan_with_no_run_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command(str(self.scan.pk))

    def build_glued(self):
        """Read a volume, glue it, and glue its corrected volume.

        :returns: The standing apply run.
        """
        run, _edits = self.read_run()
        mistral_ocr.finish_ready_applies()
        run.refresh_from_db()
        return run
