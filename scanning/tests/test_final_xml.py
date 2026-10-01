"""Tests for the export of the final XML (``scanning/final_xml.py``, #408):
the object at ``final-xml/{scan}/{opinion}.xml``, its ledger on the
row, the pass of the collect tick that writes and deletes it, the
command, and the route and the file index entry that show it.

The S3 stub of ``test_tagger`` holds the approved text and the spans;
the XML objects go into a dict of their own.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from io import StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from scanning import casebody, final_xml, tagger
from scanning.models import Opinion, OpinionReviewStatus
from scanning.tests.test_tagger import _S3Case


class _ExportCase(_S3Case):
    """A tagged, approved opinion, and a bucket for the XML objects."""

    def setUp(self):
        super().setUp()
        self.xml: dict[str, bytes] = {}
        self.put = self.enterContext(
            patch(
                "scanning.s3_sync.upload_bytes_object",
                side_effect=self._put_xml,
            )
        )
        self.delete = self.enterContext(
            patch(
                "scanning.s3_sync.delete_object", side_effect=self._delete_xml
            )
        )

    def _put_xml(self, key, body, content_type):
        self.assertEqual(content_type, final_xml.CONTENT_TYPE)
        self.xml[key] = body
        return True

    def _delete_xml(self, key):
        self.xml.pop(key, None)
        return True

    def tagged(self, opinion=None):
        """Glue a finished run, so ``tagger.is_written`` holds."""
        self.finished_row()
        tagger.finish_ready_runs()
        self.opinion.refresh_from_db()
        self.assertTrue(tagger.is_written(self.opinion))
        return self.opinion

    def stored_root(self):
        return ET.fromstring(self.xml[final_xml.key(self.opinion)])

    def refresh(self):
        self.opinion.refresh_from_db()
        return self.opinion


class TestTheKey(_ExportCase):
    def test_the_key_is_built_from_the_two_ids_alone(self):
        self.assertEqual(
            final_xml.key(self.opinion),
            f"final-xml/{self.scan.pk}/{self.opinion.pk}.xml",
        )


class TestTheIds(_ExportCase):
    def test_the_casebody_carries_the_ids_and_the_schema(self):
        self.tagged()

        xml, _tags = final_xml.render(self.opinion)

        root = ET.fromstring(xml)
        self.assertEqual(root.get("scan-id"), str(self.scan.pk))
        self.assertEqual(root.get("opinion-id"), str(self.opinion.pk))
        self.assertEqual(root.get("schema"), str(casebody.SCHEMA))

    def test_the_page_shows_the_document_the_export_stores(self):
        self.tagged()
        final_xml.export_due()
        self.client.force_login(self.make_user())

        response = self.client.get(
            reverse(
                "serve_opinion_final_xml",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
            )
        )

        self.assertEqual(
            response.content, self.xml[final_xml.key(self.opinion)]
        )


class TestThePass(_ExportCase):
    def test_nothing_before_the_spans(self):
        self.assertEqual(final_xml.export_due(), 0)
        self.assertEqual(self.xml, {})

    def test_it_writes_the_object_and_stamps_the_row(self):
        self.tagged()

        self.assertEqual(final_xml.export_due(), 1)

        opinion = self.refresh()
        self.assertEqual(opinion.final_xml_tag_key, opinion.tag_key)
        self.assertEqual(opinion.final_xml_schema, casebody.SCHEMA)
        self.assertTrue(final_xml.is_written(opinion))
        self.assertEqual(
            self.stored_root().find("parties/party").text,
            "Jane ROE, Appellant,",
        )

    def test_a_second_tick_writes_nothing(self):
        self.tagged()
        final_xml.export_due()
        self.put.reset_mock()

        self.assertEqual(final_xml.export_due(), 0)
        self.put.assert_not_called()

    def test_a_text_in_review_is_not_exported(self):
        self.tagged()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

        self.assertEqual(final_xml.export_due(), 0)
        self.assertEqual(self.xml, {})

    def test_a_new_schema_writes_it_again(self):
        self.tagged()
        final_xml.export_due()

        with patch.object(casebody, "SCHEMA", casebody.SCHEMA + 1):
            self.assertFalse(final_xml.is_written(self.refresh()))
            self.assertEqual(final_xml.export_due(), 1)
            self.assertEqual(
                self.stored_root().get("schema"), str(casebody.SCHEMA)
            )
            self.assertTrue(final_xml.is_written(self.refresh()))

    def test_new_spans_write_it_again(self):
        self.tagged()
        final_xml.export_due()
        old = self.opinion.tag_key
        Opinion.objects.filter(pk=self.opinion.pk).update(tag_key=old + ".2")
        self.stored[old + ".2"] = self.stored[old]

        self.assertEqual(final_xml.export_due(), 1)
        self.assertEqual(self.refresh().final_xml_tag_key, old + ".2")

    def test_a_reopen_deletes_the_object_and_the_stamp(self):
        self.tagged()
        final_xml.export_due()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

        self.assertEqual(final_xml.export_due(), 1)

        opinion = self.refresh()
        self.assertEqual(self.xml, {})
        self.assertEqual(opinion.final_xml_tag_key, "")
        self.assertIsNone(opinion.final_xml_schema)
        self.assertFalse(final_xml.is_written(opinion))

    def test_a_new_approval_deletes_it_until_the_new_text_is_tagged(self):
        self.tagged()
        final_xml.export_due()
        self.approve_again("Another text.")

        final_xml.export_due()

        self.assertEqual(self.xml, {})

    def test_the_approval_after_a_reopen_writes_it_again(self):
        self.tagged()
        final_xml.export_due()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        final_xml.export_due()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        self.assertEqual(final_xml.export_due(), 1)
        self.assertIn(final_xml.key(self.opinion), self.xml)

    def test_a_failed_delete_keeps_the_stamp(self):
        self.tagged()
        final_xml.export_due()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.delete.side_effect = lambda key: False

        self.assertEqual(final_xml.export_due(), 0)
        self.assertEqual(
            self.refresh().final_xml_tag_key, self.opinion.tag_key
        )

    def test_the_cap_bounds_the_rows_of_one_tick(self):
        self.tagged()
        final_xml.export_due()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.assertEqual(final_xml.export_due(limit=0), 0)
        self.assertIn(final_xml.key(self.opinion), self.xml)

    def test_s3_off_writes_nothing(self):
        self.tagged()
        with patch("scanning.s3_sync.s3_active", return_value=False):
            self.assertEqual(final_xml.export_due(), 0)
        self.assertEqual(self.xml, {})


class TestTheFaults(_ExportCase):
    def test_a_failed_put_counts_nothing(self):
        self.tagged()
        self.put.side_effect = lambda key, body, content_type: False

        self.assertEqual(final_xml.export_due(), 0)

        opinion = self.refresh()
        self.assertEqual(opinion.final_xml_attempts, 0)
        self.assertEqual(opinion.final_xml_tag_key, "")

    def test_an_s3_fault_on_the_read_counts_nothing(self):
        self.tagged()
        self.download.side_effect = EndpointConnectionError(
            endpoint_url="https://s3"
        )

        final_xml.export_due()

        self.assertEqual(self.refresh().final_xml_attempts, 0)

    def test_a_missing_object_counts_and_stops_at_the_cap(self):
        self.tagged()
        self.download.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )

        for _ in range(final_xml.MAX_ATTEMPTS + 2):
            final_xml.export_due()

        self.assertEqual(
            self.refresh().final_xml_attempts, final_xml.MAX_ATTEMPTS
        )
        self.download.reset_mock()
        final_xml.export_due()
        self.download.assert_not_called()

    def test_spans_that_do_not_fit_count(self):
        self.tagged()
        self.stored[self.opinion.tag_key]["spans"] = [
            {"paragraph": 9, "start": 0, "end": 1, "label": "court"}
        ]

        final_xml.export_due()

        opinion = self.refresh()
        self.assertEqual(opinion.final_xml_attempts, 1)
        self.assertEqual(self.xml, {})
        self.assertEqual(opinion.status, OpinionReviewStatus.TEXT_REVIEW_DONE)

    def test_an_unexpected_fault_counts_and_raises_nothing(self):
        self.tagged()
        with patch.object(
            final_xml, "render", side_effect=RuntimeError("a bug")
        ):
            self.assertEqual(final_xml.export_due(), 0)

        self.assertEqual(self.refresh().final_xml_attempts, 1)

    def test_new_spans_reset_the_count(self):
        self.tagged()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            final_xml_attempts=final_xml.MAX_ATTEMPTS
        )
        self.approve_again("Jane ROE, Appellant, again.")
        self.opinion.refresh_from_db()
        self.finished_row()
        tagger.finish_ready_runs()

        self.assertEqual(self.refresh().final_xml_attempts, 0)


class TestTheSwap(_ExportCase):
    def test_a_row_that_moved_during_the_build_is_not_stamped(self):
        self.tagged()
        real = final_xml.render

        def reopen_then_render(opinion):
            Opinion.objects.filter(pk=opinion.pk).update(
                status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
            )
            return real(opinion)

        with patch.object(final_xml, "render", side_effect=reopen_then_render):
            self.assertEqual(final_xml.export_due(), 0)

        # No stamp names the object, so the export deletes it itself.
        self.assertEqual(self.refresh().final_xml_tag_key, "")
        self.assertEqual(self.xml, {})

    def test_new_spans_during_the_build_leave_it_for_the_next_tick(self):
        self.tagged()
        real = final_xml.render
        moved = self.opinion.tag_key + ".2"
        self.stored[moved] = self.stored[self.opinion.tag_key]

        def retag_then_render(opinion):
            Opinion.objects.filter(pk=opinion.pk).update(tag_key=moved)
            return real(opinion)

        with patch.object(final_xml, "render", side_effect=retag_then_render):
            self.assertEqual(final_xml.export_due(), 0)
        self.assertEqual(self.refresh().final_xml_tag_key, "")

        self.assertEqual(final_xml.export_due(), 1)
        self.assertEqual(self.refresh().final_xml_tag_key, moved)


class TestTheCommand(_ExportCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("export_final_xml", *args, stdout=out)
        return out.getvalue()

    def test_the_dry_run_changes_nothing(self):
        self.tagged()

        out = self.run_command("--all", "--dry-run")

        self.assertIn(f"write {final_xml.key(self.opinion)}", out)
        self.assertIn("Would write 1 and delete 0", out)
        self.assertEqual(self.xml, {})

    def test_it_exports_a_named_scan(self):
        self.tagged()

        out = self.run_command(str(self.scan.pk))

        self.assertIn("Wrote or deleted 1", out)
        self.assertTrue(final_xml.is_written(self.refresh()))

    def test_it_takes_the_rows_at_the_cap(self):
        self.tagged()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            final_xml_attempts=final_xml.MAX_ATTEMPTS
        )
        self.assertEqual(final_xml.export_due(), 0)

        self.run_command("--all")

        self.assertTrue(final_xml.is_written(self.refresh()))

    def test_it_refuses_both_or_neither(self):
        with self.assertRaises(CommandError):
            self.run_command("--all", str(self.scan.pk))
        with self.assertRaises(CommandError):
            self.run_command()

    def test_it_refuses_an_unknown_scan(self):
        with self.assertRaises(CommandError):
            self.run_command(str(self.scan.pk + 1000))


class TestTheRoute(_ExportCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def url(self, name):
        return reverse(
            name, kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk}
        )

    def test_404_before_the_export(self):
        self.tagged()

        response = self.client.get(self.url("serve_opinion_exported_xml"))

        self.assertEqual(response.status_code, 404)

    def test_a_redirect_to_the_object_once_exported(self):
        self.tagged()
        final_xml.export_due()

        with (
            patch("scanning.s3_sync.object_exists", return_value=True),
            patch(
                "scanning.s3_sync.presign_get",
                return_value="https://s3/signed",
            ) as presign,
        ):
            response = self.client.get(self.url("serve_opinion_exported_xml"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://s3/signed")
        self.assertEqual(
            presign.call_args.args[0], final_xml.key(self.opinion)
        )

    def test_the_file_index_names_the_object(self):
        def entry():
            return next(
                f
                for f in self.client.get(
                    self.url("opinion_file_index")
                ).json()["files"]
                if f["output"] == "opinion-exported-xml"
            )

        self.tagged()
        self.assertFalse(entry()["written"])
        self.assertNotIn("url", entry())

        final_xml.export_due()

        self.assertTrue(entry()["written"])
        self.assertEqual(entry()["key"], final_xml.key(self.opinion))
        self.assertEqual(
            entry()["url"], self.url("serve_opinion_exported_xml")
        )
