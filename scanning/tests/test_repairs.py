"""Tests for the page repair requests of review 1 (issue #249).

A reviewer with no book records a page a scanner must scan again, or
a gap a scanner must fill. This module covers the row, the three
endpoints, the derived fulfilled state, what step 1 shows, and the
queue view.
"""

import json
import tempfile

from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import page_edits, repairs, views_process
from scanning.factories import (
    PageEditFactory,
    ReporterFactory,
    ScanFactory,
)
from scanning.models import PageEdit, PageRepairRequest, Status
from scanning.tests.test_views import ScanningTestCase

MEDIA_ROOT = tempfile.mkdtemp()


def _ocr_results(pages):
    """Return one clean OCR entry per page.

    :param pages: The printed numbers, in PDF page order.
    :returns: The ``Scan.ocr_results`` list.
    """
    return [
        {
            "pdf_page": i + 1,
            "detected": str(n),
            "type": "single",
            "score": 1.0,
            "zone": "header",
        }
        for i, n in enumerate(pages)
    ]


class RepairTestCase(ScanningTestCase):
    """A logged-in user and a three-page scan with a gap after page 1."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            page_count=3,
            source_fingerprint="100:3",
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            ocr_results=_ocr_results([1, 3, 4]),
            page_map=[
                {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
                {"type": "missing", "logical_number": 2},
                {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
                {"type": "pdf_page", "pdf_index": 2, "logical_number": 4},
            ],
        )

    def _request(self, **body):
        """POST one repair request.

        :param body: The JSON body.
        :returns: The response.
        """
        return self.client.post(
            reverse("request_page_repair", kwargs={"pk": self.scan.pk}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def _replace(self, pdf_page=2, note="blurry"):
        """Ask for a rescan of one page.

        :param pdf_page: The page.
        :param note: What the reviewer saw.
        :returns: The response.
        """
        return self._request(action="replace", pdf_page=pdf_page, note=note)

    def _insert(self, anchor=1, label="2"):
        """Ask for a missing page.

        :param anchor: The page the gap follows.
        :param label: The printed number the placeholder shows.
        :returns: The response.
        """
        return self._request(
            action="insert", anchor_pdf_page=anchor, logical_page=label
        )

    def _dismiss(self, request_id):
        """Dismiss one request.

        :param request_id: The row's primary key.
        :returns: The response.
        """
        return self.client.post(
            reverse("dismiss_page_repair", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"request_id": request_id}),
            content_type="application/json",
        )

    def _step_one(self):
        """Return the rendered step-1 page.

        :returns: The response.
        """
        return self.client.get(
            reverse("scan_process", kwargs={"pk": self.scan.pk}) + "?step=1"
        )


class TestPageRepairRequestConstraints(RepairTestCase):
    """The address matches the action, and one open row per address."""

    def _row(self, **fields):
        """Build one unsaved row with the test's scan and user.

        :param fields: The other fields.
        :returns: The row.
        """
        return PageRepairRequest(
            scan=self.scan, requested_by=self.user, **fields
        )

    def test_a_replace_needs_a_page_and_no_anchor(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(
                action=PageRepairRequest.Action.REPLACE, anchor_pdf_page=1
            ).save()

    def test_an_insert_needs_an_anchor_and_no_page(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(
                action=PageRepairRequest.Action.INSERT, pdf_page=1
            ).save()

    def test_one_open_request_per_address(self):
        self._row(action=PageRepairRequest.Action.REPLACE, pdf_page=2).save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(
                action=PageRepairRequest.Action.REPLACE, pdf_page=2
            ).save()

    def test_a_dismissed_row_frees_the_address(self):
        first = self._row(action=PageRepairRequest.Action.REPLACE, pdf_page=2)
        first.save()
        repairs.dismiss(
            PageRepairRequest.objects.filter(pk=first.pk), self.user
        )

        self._row(action=PageRepairRequest.Action.REPLACE, pdf_page=2).save()

        self.assertEqual(self.scan.repair_requests.count(), 2)

    def test_a_gap_before_page_one_is_an_address(self):
        self._row(
            action=PageRepairRequest.Action.INSERT, anchor_pdf_page=0
        ).save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(
                action=PageRepairRequest.Action.INSERT, anchor_pdf_page=0
            ).save()


class TestRequestEndpoint(RepairTestCase):
    """``request_page_repair`` writes one row per address."""

    def test_a_rescan_request_becomes_one_row(self):
        response = self._replace(pdf_page=2, note="  the lower third is torn ")

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["created"])
        row = self.scan.repair_requests.get()
        self.assertEqual(row.action, PageRepairRequest.Action.REPLACE)
        self.assertEqual(row.pdf_page, 2)
        self.assertIsNone(row.anchor_pdf_page)
        self.assertEqual(row.note, "the lower third is torn")
        self.assertEqual(row.logical_page, "3")
        self.assertEqual(row.requested_by, self.user)
        self.assertEqual(row.source_fingerprint, "100:3")
        self.assertEqual(data["request"]["id"], row.pk)
        self.assertFalse(data["request"]["fulfilled"])
        self.assertEqual(data["request"]["nav_pdf_index"], 1)

    def test_a_missing_page_request_is_addressed_by_its_gap(self):
        response = self._insert(anchor=1, label="2")

        self.assertEqual(response.status_code, 200)
        row = self.scan.repair_requests.get()
        self.assertEqual(row.action, PageRepairRequest.Action.INSERT)
        self.assertIsNone(row.pdf_page)
        self.assertEqual(row.anchor_pdf_page, 1)
        self.assertEqual(row.logical_page, "2")
        self.assertEqual(
            json.loads(response.content)["request"]["nav_pdf_index"], 0
        )

    def test_an_older_viewer_sends_the_label_alone(self):
        response = self._request(action="insert", logical_page="2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scan.repair_requests.get().anchor_pdf_page, 1)

    def test_a_second_request_answers_the_first_row(self):
        first = json.loads(self._replace(note="first").content)
        second = json.loads(self._replace(note="second").content)

        self.assertFalse(second["created"])
        self.assertEqual(second["request"]["id"], first["request"]["id"])
        self.assertEqual(self.scan.repair_requests.get().note, "first")

    def test_a_page_the_volume_does_not_have_is_refused(self):
        self.assertEqual(self._replace(pdf_page=9).status_code, 404)
        self.assertEqual(self._replace(pdf_page=0).status_code, 404)
        self.assertFalse(self.scan.repair_requests.exists())

    def test_a_gap_past_the_last_page_is_refused(self):
        self.assertEqual(self._insert(anchor=7).status_code, 404)
        self.assertFalse(self.scan.repair_requests.exists())

    def test_an_unknown_action_is_refused(self):
        response = self._request(action="rotate", pdf_page=1)
        self.assertEqual(response.status_code, 400)

    def test_a_label_that_is_not_a_page_number_is_refused(self):
        response = self._request(
            action="insert", anchor_pdf_page=1, logical_page="<b>2</b>"
        )
        self.assertEqual(response.status_code, 400)

    def test_the_note_is_cut(self):
        self._replace(note="x" * 1000)
        self.assertEqual(
            len(self.scan.repair_requests.get().note), repairs.NOTE_MAX_CHARS
        )

    def test_a_reading_the_narrowing_refuses_is_dropped(self):
        results = self.scan.ocr_results
        results[1]["detected"] = "<img src=x onerror=alert(1)>"
        self.scan.ocr_results = results
        self.scan.save(update_fields=["ocr_results"])

        # With and without a label from the viewer: the label of a
        # rescan is the server's to read, and a junk reading must not
        # make the button fail on the one page that needs it.
        sent = self._request(action="replace", pdf_page=2, logical_page="<b>x")
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(self.scan.repair_requests.get().logical_page, "")

        self._dismiss(json.loads(sent.content)["request"]["id"])
        self.assertEqual(self._replace(pdf_page=2).status_code, 200)

    def test_the_label_of_a_rescan_is_read_by_the_server(self):
        response = self._request(
            action="replace", pdf_page=2, logical_page="999"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scan.repair_requests.get().logical_page, "3")

    def test_a_request_is_taken_in_any_status(self):
        self.scan.status = Status.PAGE_COMPLETENESS_REVIEW_DONE
        self.scan.save(update_fields=["status"])
        self.assertEqual(self._replace().status_code, 200)

    def test_login_is_required(self):
        self.client.logout()
        response = self._replace()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.scan.repair_requests.exists())


class TestARangeMissingAtTheEnd(RepairTestCase):
    """The placeholder of issue #256 asks like any other gap.

    A range missing at the end of the volume is **one** gap: its
    address is the last physical page, so the range is one row and the
    printed range rides along as the label.
    """

    def setUp(self):
        super().setUp()
        self.scan.start_page = 1
        self.scan.end_page = 13
        self.scan.ocr_results = _ocr_results([1, 2, 3])
        self.scan.page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 2},
            {"type": "pdf_page", "pdf_index": 2, "logical_number": 3},
            {
                "type": "missing",
                "logical_number": "4-13",
                "missing_range": [4, 13],
            },
        ]
        self.scan.save()

    def test_the_range_becomes_one_row_after_the_last_page(self):
        response = self._insert(anchor=3, label="4-13")

        self.assertEqual(response.status_code, 200)
        row = self.scan.repair_requests.get()
        self.assertEqual(row.action, PageRepairRequest.Action.INSERT)
        self.assertEqual(row.anchor_pdf_page, 3)
        self.assertEqual(row.logical_page, "4-13")
        self.assertEqual(
            json.loads(response.content)["request"]["nav_pdf_index"], 2
        )

    def test_a_second_ask_answers_the_same_row(self):
        first = json.loads(self._insert(anchor=3, label="4-13").content)
        second = json.loads(self._insert(anchor=3, label="4-13").content)

        self.assertFalse(second["created"])
        self.assertEqual(second["request"]["id"], first["request"]["id"])

    def test_an_older_viewer_places_the_range_by_its_label(self):
        response = self._request(action="insert", logical_page="4-13")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scan.repair_requests.get().anchor_pdf_page, 3)

    def test_the_viewer_draws_the_placeholder_and_the_request(self):
        self._insert(anchor=3, label="4-13")

        page = self._step_one().content.decode()

        self.assertIn("missing_range", page)
        self.assertIn("4-13", page)


class TestDismissEndpoint(RepairTestCase):
    """``dismiss_page_repair`` stamps, and never deletes."""

    def test_a_dismissal_stamps_the_row(self):
        row_id = json.loads(self._replace().content)["request"]["id"]

        self.assertEqual(self._dismiss(row_id).status_code, 200)

        row = self.scan.repair_requests.get()
        self.assertIsNotNone(row.dismissed_at)
        self.assertEqual(row.dismissed_by, self.user)
        # ``update`` skips ``auto_now``; the audit reads the last touch.
        self.assertEqual(row.date_modified, row.dismissed_at)
        self.assertEqual(repairs.waiting_requests(self.scan), [])

    def test_a_second_dismissal_is_a_no_op(self):
        row_id = json.loads(self._replace().content)["request"]["id"]
        self._dismiss(row_id)
        first = self.scan.repair_requests.get()

        other = self.make_user()
        self.client.force_login(other)
        self.assertEqual(self._dismiss(row_id).status_code, 200)

        row = self.scan.repair_requests.get()
        self.assertEqual(row.dismissed_at, first.dismissed_at)
        self.assertEqual(row.dismissed_by, self.user)

    def test_any_user_may_dismiss(self):
        row_id = json.loads(self._replace().content)["request"]["id"]
        other = self.make_user()
        self.client.force_login(other)

        self.assertEqual(self._dismiss(row_id).status_code, 200)
        self.assertEqual(self.scan.repair_requests.get().dismissed_by, other)

    def test_an_unknown_request_is_404(self):
        self.assertEqual(self._dismiss(999).status_code, 404)
        self.assertEqual(self._dismiss("abc").status_code, 404)
        self.assertEqual(self._dismiss(None).status_code, 404)

    def test_a_request_of_another_scan_is_404(self):
        other = ScanFactory(page_count=2)
        row = PageRepairRequest.objects.create(
            scan=other,
            requested_by=self.user,
            action=PageRepairRequest.Action.REPLACE,
            pdf_page=1,
        )
        self.assertEqual(self._dismiss(row.pk).status_code, 404)
        row.refresh_from_db()
        self.assertIsNone(row.dismissed_at)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestFulfilledIsDerived(RepairTestCase):
    """An upload at the address fulfils the request; an undo reopens it."""

    def test_a_replacement_fulfils_a_rescan_request(self):
        self._replace(pdf_page=2)

        self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), "pdf_page": 2},
        )

        row = repairs.open_requests(self.scan).get()
        self.assertTrue(row.fulfilled)
        self.assertEqual(repairs.waiting_requests(self.scan), [])

    def test_an_undo_of_the_replacement_reopens_it(self):
        self._replace(pdf_page=2)
        self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), "pdf_page": 2},
        )

        self.client.post(
            reverse("undo_replace_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2}),
            content_type="application/json",
        )

        self.assertEqual(len(repairs.waiting_requests(self.scan)), 1)

    def test_an_insert_in_the_gap_fulfils_a_missing_page_request(self):
        self._insert(anchor=1, label="2")
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
            source_fingerprint="100:3",
        )

        self.assertTrue(repairs.open_requests(self.scan).get().fulfilled)

    def test_an_insert_in_another_gap_fulfils_nothing(self):
        self._insert(anchor=1, label="2")
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=2,
            value="",
        )

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_replacement_of_another_page_fulfils_nothing(self):
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=3,
            value="",
        )

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    # --- A one-page missing-page request answered beside its gap (#393) ---

    def _replacement(self, pdf_page, **fields):
        """Save a standing replacement of ``pdf_page`` under this original.

        :param pdf_page: The page replaced.
        :param fields: Overrides of the factory.
        :returns: The edit.
        """
        fields.setdefault("source_fingerprint", "100:3")
        return PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=pdf_page,
            value="",
            **fields,
        )

    def test_a_replacement_of_the_page_before_the_gap_fulfils_a_one_page_request(
        self,
    ):
        # The blurry page was the page asked for: the scanner scanned
        # it again, and the request is answered.
        self._insert(anchor=1, label="2")
        self._replacement(1)

        row = repairs.open_requests(self.scan).get()

        self.assertTrue(row.fulfilled)
        self.assertFalse(row.fulfilled_at_address)
        self.assertEqual(repairs.fulfilled_by(row), "replace")
        self.assertEqual(
            repairs.as_dict(row, self.scan)["fulfilled_by"], "replace"
        )
        self.assertEqual(repairs.waiting_requests(self.scan), [])
        self.assertFalse(repairs.has_waiting(self.scan))

    def test_a_replacement_of_the_page_after_the_gap_fulfils_it_too(self):
        self._insert(anchor=1, label="2")
        self._replacement(2)

        self.assertTrue(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_replacement_two_pages_from_the_gap_fulfils_nothing(self):
        self._insert(anchor=1, label="2")
        self._replacement(3)

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_replacement_beside_a_range_request_fulfils_nothing(self):
        # A replacement is one page; a request for a range of pages is
        # never answered by one.
        self._insert(anchor=3, label="4-13")
        self._replacement(3)

        row = repairs.open_requests(self.scan).get()

        self.assertTrue(row.fulfilled_beside)
        self.assertFalse(row.fulfilled)
        self.assertIsNone(repairs.fulfilled_by(row))
        self.assertTrue(repairs.has_waiting(self.scan))

    def test_a_replacement_beside_the_gap_before_the_request_fulfils_nothing(
        self,
    ):
        self._replacement(1)
        self._insert(anchor=1, label="2")

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_withdrawn_replacement_beside_the_gap_fulfils_nothing(self):
        self._insert(anchor=1, label="2")
        self._replacement(1, withdrawn_at=timezone.now())

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_stale_replacement_beside_the_gap_fulfils_nothing(self):
        self._insert(anchor=1, label="2")
        self._replacement(1, source_fingerprint="99:3")

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_fulfilled_by_names_the_shape_that_answered(self):
        self._insert(anchor=1, label="2")
        self._replace(pdf_page=3)
        insert_row, replace_row = list(repairs.open_requests(self.scan))
        self.assertIsNone(repairs.fulfilled_by(insert_row))
        self.assertIsNone(repairs.fulfilled_by(replace_row))

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
            source_fingerprint="100:3",
        )
        self._replacement(3)

        insert_row, replace_row = list(repairs.open_requests(self.scan))
        self.assertEqual(repairs.fulfilled_by(insert_row), "insert")
        self.assertEqual(repairs.fulfilled_by(replace_row), "replace")

    def test_a_stale_edit_fulfils_nothing(self):
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="999:3",
        )

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_scan_with_no_fingerprint_takes_any_edit(self):
        # The other blank of the rule: the volume was never sharded,
        # so nothing stamped the scan. The edit carries a fingerprint
        # of its own and must still fulfil. The test reads the scan
        # through the outer row, which is where the query reads it.
        self.scan.source_fingerprint = ""
        self.scan.save(update_fields=["source_fingerprint"])
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )

        self.assertTrue(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_legacy_edit_with_no_fingerprint_fulfils(self):
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="",
        )

        self.assertTrue(repairs.open_requests(self.scan).get().fulfilled)

    def test_an_edit_already_there_answers_nothing(self):
        # A curator replaced the page; the reviewer finds the
        # replacement blurry too and asks again. That request waits.
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )

        data = json.loads(self._replace(pdf_page=2).content)

        self.assertTrue(data["created"])
        self.assertFalse(data["request"]["fulfilled"])
        self.assertEqual(len(repairs.waiting_requests(self.scan)), 1)
        self.assertEqual(repairs.waiting_count(), 1)

    def test_a_second_upload_after_the_request_fulfils_it(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )
        self._replace(pdf_page=2)

        self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), "pdf_page": 2},
        )

        self.assertEqual(repairs.waiting_requests(self.scan), [])

    def test_an_applied_edit_against_an_earlier_upload_fulfils_nothing(self):
        # The original never changes: the apply (#206) writes another
        # file. So the fingerprint moves only on a re-upload, and an
        # edit applied against the earlier upload names a leaf of
        # another book, whatever its stamp says.
        # Withdrawn, because one row only may stand per address
        # (#224): the second write supersedes the first, which is
        # what ``page_edits.supersede`` does to an applied row.
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
            applied_at=timezone.now(),
            withdrawn_at=timezone.now(),
        )
        self.scan.source_fingerprint = "777:3"
        self.scan.save(update_fields=["source_fingerprint"])
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
            applied_at=timezone.now(),
        )

        self.assertFalse(repairs.open_requests(self.scan).get().fulfilled)

    def test_asking_again_over_an_answered_request_is_told_so(self):
        # The mirror of ``test_an_edit_already_there_answers_nothing``:
        # here the request came first. The key matches the fulfilled
        # row, so nothing new is created, and the answer must say so.
        first = json.loads(self._replace(pdf_page=2).content)["request"]
        self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), "pdf_page": 2},
        )

        data = json.loads(self._replace(pdf_page=2).content)

        self.assertFalse(data["created"])
        self.assertTrue(data["already_fulfilled"])
        self.assertEqual(data["request"]["id"], first["id"])
        self.assertTrue(data["request"]["fulfilled"])
        self.assertEqual(
            data["message"], views_process.REPAIR_ALREADY_FULFILLED_MESSAGE
        )
        self.assertEqual(self.scan.repair_requests.count(), 1)

    def test_a_dismissal_of_the_answered_request_frees_the_ask(self):
        first = json.loads(self._replace(pdf_page=2).content)["request"]
        self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), "pdf_page": 2},
        )
        self._dismiss(first["id"])

        data = json.loads(self._replace(pdf_page=2).content)

        self.assertTrue(data["created"])
        self.assertFalse(data["already_fulfilled"])
        self.assertFalse(data["request"]["fulfilled"])
        self.assertEqual(len(repairs.waiting_requests(self.scan)), 1)
        self.assertEqual(self.scan.repair_requests.count(), 2)

    def test_a_first_request_is_not_already_fulfilled(self):
        data = json.loads(self._replace(pdf_page=2).content)
        self.assertFalse(data["already_fulfilled"])
        self.assertNotIn("message", data)

    def test_an_applied_edit_against_this_upload_fulfils(self):
        # ``applied_at`` answers "is it built into an output?", not
        # "which upload is it counted against?". Done work fulfils.
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
            applied_at=timezone.now(),
        )

        self.assertTrue(repairs.open_requests(self.scan).get().fulfilled)

    def test_a_request_is_not_a_page_edit(self):
        self._replace(pdf_page=2)
        self._insert(anchor=1)

        self.assertFalse(self.scan.page_edits.exists())
        self.assertFalse(page_edits.has_pending_changes(self.scan))
        self.assertEqual(
            page_edits.pending_edit_flags(self.scan),
            {"has_pending_changes": False, "has_pending_inserts": False},
        )


class TestStaleRequests(RepairTestCase):
    """A request made against an earlier upload is marked, not dropped."""

    def test_a_request_against_another_original_is_marked(self):
        self._replace(pdf_page=2)
        self.scan.source_fingerprint = "200:3"
        self.scan.save(update_fields=["source_fingerprint"])

        payload = repairs.viewer_payload(self.scan)

        self.assertEqual(len(payload), 1)
        self.assertTrue(payload[0]["stale"])
        self.assertEqual(len(repairs.waiting_requests(self.scan)), 1)

    def test_a_blank_fingerprint_matches_anything(self):
        self.scan.source_fingerprint = ""
        self.scan.save(update_fields=["source_fingerprint"])
        self._replace(pdf_page=2)

        self.assertFalse(repairs.viewer_payload(self.scan)[0]["stale"])


class TestStepOneShowsTheRequests(RepairTestCase):
    """What the review page carries for the viewer and the sidebar."""

    def test_the_viewer_reads_every_open_request(self):
        self._replace(pdf_page=2, note="blurry <b>bold</b>")
        self._insert(anchor=1, label="2")

        response = self._step_one()

        # Volume order: the gap after page 1 sits before page 2.
        rows = response.context["repair_requests"]
        self.assertEqual(
            [(r["action"], r["pdf_page"], r["anchor_pdf_page"]) for r in rows],
            [("insert", None, 1), ("replace", 2, None)],
        )
        # The note is a person's typing, escaped where it is drawn: in
        # the sidebar by the auto-escape, in the script block by
        # json_script. The raw text reaches the browser nowhere.
        self.assertContains(response, "blurry &lt;b&gt;bold&lt;/b&gt;")
        self.assertContains(response, "repair-requests-data")
        self.assertNotContains(response, "blurry <b>bold</b>")
        self.assertNotContains(response, "</b>")

    def test_the_sidebar_row_of_a_requested_page_says_so(self):
        self._replace(pdf_page=2)

        response = self._step_one()

        rows = response.context["ocr_results"]
        self.assertEqual(
            [r["needs_repair"] for r in rows], [False, True, False]
        )
        self.assertContains(response, "NEED")
        self.assertContains(response, "Repairs requested")

    def test_a_fulfilled_request_raises_no_badge(self):
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )

        response = self._step_one()

        self.assertEqual(response.context["waiting_repairs"], [])
        self.assertTrue(response.context["repair_requests"][0]["fulfilled"])
        self.assertNotContains(response, "NEED")

    def test_a_gap_sorts_after_the_page_it_follows(self):
        self._insert(anchor=2, label="3a")
        self._replace(pdf_page=2)
        self._replace(pdf_page=3)

        rows = repairs.viewer_payload(self.scan)

        self.assertEqual(
            [(r["action"], r["pdf_page"], r["anchor_pdf_page"]) for r in rows],
            [("replace", 2, None), ("insert", None, 2), ("replace", 3, None)],
        )

    def test_a_dismissed_request_is_absent(self):
        row_id = json.loads(self._replace(pdf_page=2).content)["request"]["id"]
        self._dismiss(row_id)

        response = self._step_one()

        self.assertEqual(response.context["repair_requests"], [])

    def test_the_repairs_come_before_the_issues(self):
        """The block is a reason the review cannot close (#266).

        Under the issue cards the reviewer had to scroll to find it,
        and they found it after the work.
        """
        self._replace(pdf_page=2)

        html = self._step_one().content.decode()

        self.assertLess(
            html.index('id="repairs-section"'),
            html.index('id="issues-section"'),
        )


class TestRepairQueue(RepairTestCase):
    """The queue of repairs, over every scan."""

    def setUp(self):
        super().setUp()
        # A newer scan of another reporter, so the order and the
        # reporter filter both have something to tell apart.
        self.other = ScanFactory(
            page_count=2,
            source_fingerprint="50:2",
            reporter=ReporterFactory(short_name="zz", full_name="Zed"),
        )
        self.other_row = PageRepairRequest.objects.create(
            scan=self.other,
            requested_by=self.user,
            action=PageRepairRequest.Action.INSERT,
            anchor_pdf_page=0,
            note="the title page",
        )

    def _queue(self, **params):
        """GET the queue page.

        :param params: Query parameters.
        :returns: The response.
        """
        return self.client.get(reverse("repair_queue"), params)

    def test_the_queue_groups_the_waiting_requests_by_scan(self):
        self._replace(pdf_page=2, note="blurry")
        self._insert(anchor=1, label="2")

        response = self._queue()

        self.assertEqual(response.status_code, 200)
        # Newest scan first, so a scanner sees the volume last worked on.
        groups = response.context["groups"]
        self.assertEqual(
            [g["scan"].pk for g in groups], [self.other.pk, self.scan.pk]
        )
        self.assertEqual(len(groups[1]["requests"]), 2)
        self.assertContains(response, "blurry")
        self.assertContains(response, "the title page")
        self.assertContains(response, "Waiting")
        self.assertContains(response, "?step=1&amp;goto=1")

    def test_the_queue_shows_who_has_the_book(self):
        response = self._queue()
        self.assertContains(response, self.other.uploaded_by.username)

    def test_a_dismissed_request_leaves_the_waiting_list(self):
        row_id = json.loads(self._replace(pdf_page=2).content)["request"]["id"]
        self._dismiss(row_id)

        waiting = self._queue()
        dismissed = self._queue(state="dismissed")

        self.assertEqual(
            [g["scan"].pk for g in waiting.context["groups"]],
            [self.other.pk],
        )
        self.assertEqual(
            [g["scan"].pk for g in dismissed.context["groups"]],
            [self.scan.pk],
        )
        self.assertContains(dismissed, "Dismissed")

    def test_a_fulfilled_request_leaves_the_waiting_list(self):
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )

        waiting = self._queue()
        fulfilled = self._queue(state="fulfilled")

        self.assertEqual(
            [g["scan"].pk for g in waiting.context["groups"]],
            [self.other.pk],
        )
        self.assertEqual(
            [g["scan"].pk for g in fulfilled.context["groups"]],
            [self.scan.pk],
        )
        self.assertContains(fulfilled, "Fulfilled")

    def test_a_request_answered_beside_its_gap_leaves_the_waiting_list(self):
        self._insert(anchor=1, label="2")
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=1,
            value="",
            source_fingerprint="100:3",
        )

        waiting = self._queue()
        fulfilled = self._queue(state="fulfilled")

        self.assertEqual(
            [g["scan"].pk for g in waiting.context["groups"]],
            [self.other.pk],
        )
        self.assertEqual(
            [g["scan"].pk for g in fulfilled.context["groups"]],
            [self.scan.pk],
        )

    def test_the_reporter_filter(self):
        self._replace(pdf_page=2)

        response = self._queue(reporter=self.scan.reporter.short_name)

        self.assertEqual(
            [g["scan"].pk for g in response.context["groups"]],
            [self.scan.pk],
        )

    def test_a_stale_request_is_marked_on_the_queue_page(self):
        self._replace(pdf_page=2)
        self.scan.source_fingerprint = "200:3"
        self.scan.save(update_fields=["source_fingerprint"])

        response = self._queue()

        self.assertContains(response, "EARLIER UPLOAD", count=1)
        self.assertTrue(
            [r for r in response.context["groups"][1]["requests"]][0].is_stale
        )

    def test_the_queue_paginates_scans_and_reads_one_page_of_rows(self):
        # Fifty-one scans with one waiting request each, plus the two
        # of setUp: the second page holds the oldest three.
        for _ in range(51):
            scan = ScanFactory(page_count=1)
            PageRepairRequest.objects.create(
                scan=scan,
                requested_by=self.user,
                action=PageRepairRequest.Action.REPLACE,
                pdf_page=1,
            )
        self._replace(pdf_page=2)

        first = self._queue()
        second = self._queue(page=2)

        self.assertEqual(len(first.context["groups"]), 50)
        self.assertEqual(
            [g["scan"].pk for g in second.context["groups"]][-2:],
            [self.other.pk, self.scan.pk],
        )
        self.assertEqual(second.context["page_obj"].paginator.num_pages, 2)

    def test_an_unknown_state_reads_as_waiting(self):
        response = self._queue(state="bogus")
        self.assertEqual(response.context["state"], "waiting")

    def test_the_header_counts_the_waiting_requests(self):
        self._replace(pdf_page=2)

        response = self._queue()

        self.assertEqual(response.context["waiting_repairs_count"], 2)
        self.assertEqual(repairs.waiting_count(), 2)

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(self._queue().status_code, 302)

    def test_an_empty_queue_says_so(self):
        repairs.dismiss(PageRepairRequest.objects.all(), self.user)
        response = self._queue()
        self.assertContains(response, "No page waits for a scanner.")


class TestDismissFromTheQueue(RepairTestCase):
    """The Dismiss button of the queue page (#249).

    A missing-page request draws its card on the placeholder of its
    gap, and the placeholder goes when the printed sequence stops
    showing the gap. The request then waits with no button on the
    page and holds the review-1 approval (#266). The queue offers the
    same button, under the same rule as ``dismiss_page_repair``.
    """

    def _dismiss_from_queue(self, request_id, query=""):
        """POST the queue's Dismiss form.

        :param request_id: The request to dismiss.
        :param query: The query string of the list the button was on.
        :returns: The response.
        """
        return self.client.post(
            reverse("dismiss_repair_from_queue", kwargs={"pk": request_id})
            + query
        )

    def test_an_open_request_offers_the_button(self):
        self._insert(anchor=1, label="2")
        response = self.client.get(reverse("repair_queue"))
        row = self.scan.repair_requests.get()

        self.assertContains(
            response,
            reverse("dismiss_repair_from_queue", kwargs={"pk": row.pk}),
        )
        self.assertContains(response, ">Dismiss</button>")

    def test_a_dismissed_request_offers_none(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()
        repairs.dismiss(self.scan.repair_requests.all(), self.user)

        response = self.client.get(
            reverse("repair_queue"), {"state": "dismissed"}
        )

        self.assertNotContains(
            response,
            reverse("dismiss_repair_from_queue", kwargs={"pk": row.pk}),
        )
        self.assertNotContains(response, ">Dismiss</button>")

    def test_the_form_carries_the_list_query(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()

        response = self.client.get(
            reverse("repair_queue"), {"state": "all", "page": "1"}
        )

        self.assertContains(
            response,
            reverse("dismiss_repair_from_queue", kwargs={"pk": row.pk})
            + "?state=all&amp;page=1",
        )

    def test_a_dismissal_stamps_the_row_and_returns_to_the_list(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()
        other = self.make_user()
        self.client.force_login(other)

        response = self._dismiss_from_queue(row.pk, "?state=all&page=1")

        self.assertRedirects(
            response,
            reverse("repair_queue") + "?state=all&page=1",
            fetch_redirect_response=False,
        )
        row.refresh_from_db()
        self.assertIsNotNone(row.dismissed_at)
        # Any logged-in user, the rule of the page card's button.
        self.assertEqual(row.dismissed_by, other)
        self.assertEqual(row.date_modified, row.dismissed_at)
        self.assertFalse(repairs.has_waiting(self.scan))
        flashed = [m.message for m in get_messages(response.wsgi_request)]
        self.assertIn("Request dismissed.", flashed)

    def test_a_stray_query_key_is_dropped(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()

        response = self._dismiss_from_queue(
            row.pk, "?state=waiting&next=https://evil.example/"
        )

        self.assertRedirects(
            response,
            reverse("repair_queue") + "?state=waiting",
            fetch_redirect_response=False,
        )

    def test_a_second_dismissal_is_a_no_op(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()
        repairs.dismiss(self.scan.repair_requests.all(), self.user)
        first = self.scan.repair_requests.get()
        other = self.make_user()
        self.client.force_login(other)

        response = self._dismiss_from_queue(row.pk)

        self.assertRedirects(
            response, reverse("repair_queue"), fetch_redirect_response=False
        )
        row.refresh_from_db()
        self.assertEqual(row.dismissed_at, first.dismissed_at)
        self.assertEqual(row.dismissed_by, self.user)
        flashed = [m.message for m in get_messages(response.wsgi_request)]
        self.assertIn("This request was already dismissed.", flashed)

    def test_an_unknown_request_is_404(self):
        self.assertEqual(self._dismiss_from_queue(999).status_code, 404)

    def test_a_get_is_refused(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()

        response = self.client.get(
            reverse("dismiss_repair_from_queue", kwargs={"pk": row.pk})
        )

        self.assertEqual(response.status_code, 405)
        row.refresh_from_db()
        self.assertIsNone(row.dismissed_at)

    def test_login_is_required(self):
        self._insert(anchor=1, label="2")
        row = self.scan.repair_requests.get()
        self.client.logout()

        response = self._dismiss_from_queue(row.pk)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        row.refresh_from_db()
        self.assertIsNone(row.dismissed_at)


class TestProjectRequests(RepairTestCase):
    """Every open missing-page request keeps a placeholder (#393).

    The sequence analysis alone writes a ``missing`` entry, and the
    placeholder goes when the sequence stops showing the gap. The
    request's note, Dismiss button and insert form live on that
    placeholder, so the viewer gets one for the request itself.
    """

    #: A three-page map with the anchors stamped, as
    #: ``page_edits.project_inserts`` hands it on, and no gap.
    FLAT_MAP = [
        {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
        {"type": "pdf_page", "pdf_index": 1, "logical_number": 2},
        {"type": "pdf_page", "pdf_index": 2, "logical_number": 3},
    ]

    def _row(self, anchor, label="2", **fields):
        """Return the viewer's dict of an INSERT request.

        :param anchor: The gap.
        :param label: The printed label.
        :returns: The shape :func:`repairs.as_dict` writes.
        """
        return {
            "id": 1,
            "action": "insert",
            "anchor_pdf_page": anchor,
            "pdf_page": None,
            "logical_page": label,
            "fulfilled": False,
            **fields,
        }

    def test_a_request_at_a_shown_gap_adds_nothing(self):
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "missing", "logical_number": 2, "anchor_pdf_page": 1},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
        ]

        out = repairs.project_requests(page_map, [self._row(1)])

        self.assertEqual(out, page_map)

    def test_a_replace_request_adds_nothing(self):
        row = {"action": "replace", "pdf_page": 2, "anchor_pdf_page": None}

        self.assertEqual(
            repairs.project_requests(self.FLAT_MAP, [row]), self.FLAT_MAP
        )

    def test_a_request_with_no_gap_gets_a_placeholder_after_its_anchor(self):
        out = repairs.project_requests(self.FLAT_MAP, [self._row(1)])

        self.assertEqual(len(out), 4)
        self.assertEqual(
            out[1],
            {
                "type": "missing",
                "logical_number": "2",
                "anchor_pdf_page": 1,
                "from_request": True,
            },
        )
        self.assertEqual([e.get("pdf_index") for e in out], [0, None, 1, 2])
        # Not modified in place.
        self.assertEqual(len(self.FLAT_MAP), 3)

    def test_the_placeholder_follows_the_images_of_its_gap(self):
        page_map = [
            self.FLAT_MAP[0],
            {"type": "inserted", "logical_number": "1a", "insert_edit_id": 7},
            self.FLAT_MAP[1],
            self.FLAT_MAP[2],
        ]

        out = repairs.project_requests(page_map, [self._row(1)])

        self.assertEqual(
            [e["type"] for e in out],
            ["pdf_page", "inserted", "missing", "pdf_page", "pdf_page"],
        )

    def test_a_gap_before_page_one_goes_first(self):
        out = repairs.project_requests(
            self.FLAT_MAP, [self._row(0, label="i")]
        )

        self.assertTrue(out[0].get("from_request"))
        self.assertEqual(out[0]["anchor_pdf_page"], 0)

    def test_a_range_label_marks_a_range(self):
        out = repairs.project_requests(
            self.FLAT_MAP, [self._row(3, label="4-13")]
        )

        self.assertEqual(out[-1]["missing_range"], [4, 13])
        self.assertEqual(out[-1]["logical_number"], "4-13")

    def test_a_request_whose_anchor_is_not_in_the_map_goes_last(self):
        out = repairs.project_requests(self.FLAT_MAP, [self._row(9)])

        self.assertEqual(len(out), 4)
        self.assertTrue(out[-1].get("from_request"))

    def test_two_requests_keep_the_order_of_the_volume(self):
        out = repairs.project_requests(
            self.FLAT_MAP,
            [self._row(2, label="3"), self._row(1, label="2")],
        )

        self.assertEqual(
            [e.get("anchor_pdf_page") for e in out if e["type"] == "missing"],
            [1, 2],
        )
        self.assertEqual(
            [e.get("pdf_index") for e in out], [0, None, 1, None, 2]
        )

    def test_a_fulfilled_request_keeps_its_placeholder(self):
        # The row is open until a person judges the new page and
        # dismisses it, so its Dismiss button must stay on the page.
        out = repairs.project_requests(
            self.FLAT_MAP, [self._row(1, fulfilled=True)]
        )

        self.assertEqual(len(out), 4)

    def test_step_one_draws_the_placeholder_of_a_request_with_no_gap(self):
        # The scan's map shows a gap after page 1 and none after page
        # 2. The request after page 2 gets a card; the one after page
        # 1 already has the gap's.
        self._insert(anchor=1, label="2")
        self._insert(anchor=2, label="3")

        page_map = json.loads(self._step_one().context["page_map_json"])

        missing = [e for e in page_map if e["type"] == "missing"]
        self.assertEqual(
            [
                (e["anchor_pdf_page"], bool(e.get("from_request")))
                for e in missing
            ],
            [(1, False), (2, True)],
        )
        self.assertEqual(
            [e.get("pdf_index") for e in page_map], [0, None, 1, None, 2]
        )

    def test_a_dismissed_request_draws_no_placeholder(self):
        self._insert(anchor=2, label="3")
        repairs.dismiss(self.scan.repair_requests.all(), self.user)

        page_map = json.loads(self._step_one().context["page_map_json"])

        self.assertEqual([e for e in page_map if e.get("from_request")], [])


class TestWaitingCounts(RepairTestCase):
    """The count the badge of the scan list reads (issue #266)."""

    def test_one_query_counts_every_scan_of_a_page(self):
        other = ScanFactory(page_count=2, source_fingerprint="50:2")
        PageRepairRequest.objects.create(
            scan=other,
            action=PageRepairRequest.Action.REPLACE,
            requested_by=self.user,
            pdf_page=1,
            source_fingerprint="50:2",
        )
        self._replace(pdf_page=2)

        with self.assertNumQueries(1):
            counts = repairs.waiting_counts([self.scan.pk, other.pk])

        self.assertEqual(counts, {self.scan.pk: 1, other.pk: 1})

    def test_two_requests_of_one_scan_read_two(self):
        """The grouping is by scan, never by address.

        ``annotate_fulfilled`` orders by ``sort_address``, and Django
        puts the ordering columns into ``GROUP BY``. Without the
        cleared ordering this scan would read 1 twice.
        """
        self._replace(pdf_page=2)
        self._insert(anchor=1, label="2")

        self.assertEqual(
            repairs.waiting_counts([self.scan.pk]), {self.scan.pk: 2}
        )

    def test_a_scan_with_no_request_is_absent(self):
        other = ScanFactory(page_count=2)

        self.assertEqual(repairs.waiting_counts([other.pk]), {})

    def test_a_dismissed_request_is_not_counted(self):
        row_id = json.loads(self._replace(pdf_page=2).content)["request"]["id"]
        self._dismiss(row_id)

        self.assertEqual(repairs.waiting_counts([self.scan.pk]), {})

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_a_fulfilled_request_is_not_counted(self):
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )

        self.assertEqual(repairs.waiting_counts([self.scan.pk]), {})

    def test_has_waiting_reads_the_same_term(self):
        self.assertFalse(repairs.has_waiting(self.scan))

        row_id = json.loads(self._replace(pdf_page=2).content)["request"]["id"]
        self.assertTrue(repairs.has_waiting(self.scan))

        self._dismiss(row_id)
        self.assertFalse(repairs.has_waiting(self.scan))

    def test_a_stale_request_waits_too(self):
        """A request against an earlier upload holds the review open.

        This is not the ``PageEdit`` rule: an apply cannot place a
        stale edit, but a person judges a stale request and dismisses
        it with one click.
        """
        self._replace(pdf_page=2)
        self.scan.source_fingerprint = "200:3"
        self.scan.save(update_fields=["source_fingerprint"])

        self.assertTrue(repairs.has_waiting(self.scan))
        self.assertEqual(
            repairs.waiting_counts([self.scan.pk]), {self.scan.pk: 1}
        )


class TestTheApprovalWaitsForTheScanner(RepairTestCase):
    """A waiting request refuses the review-1 approval (issue #266)."""

    def _approve(self):
        """POST the approve button of review 1.

        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("approve_page_completeness", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_a_waiting_request_refuses_the_approval(self):
        self._replace(pdf_page=2)

        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertIn(views_process.REPAIRS_WAITING_MESSAGE, flashed)

    def test_the_bar_shows_a_note_and_no_approve_button(self):
        self._replace(pdf_page=2)

        response = self._step_one()

        self.assertTrue(response.context["repairs_waiting"])
        self.assertContains(response, "Waiting for a scanner")
        self.assertNotContains(
            response, "I reviewed this scan and it is complete"
        )

    def test_the_fragment_agrees_with_the_page(self):
        """One flag serves both, or the bar would offer a refused button."""
        self._replace(pdf_page=2)

        fragment = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk}) + "?step=1"
        )

        self.assertIn(
            "Waiting for a scanner", json.loads(fragment.content)["html"]
        )

    def test_the_approval_passes_after_a_dismissal(self):
        row_id = json.loads(self._replace(pdf_page=2).content)["request"]["id"]
        self._dismiss(row_id)

        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn(views_process.PAGE_REVIEW_APPROVED_MESSAGE, flashed)

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_a_fulfilled_request_does_not_refuse(self):
        """The scanner did the work; the row waits for nobody."""
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
        )

        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn(views_process.PAGE_REVIEW_APPROVED_MESSAGE, flashed)

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_a_request_answered_beside_its_gap_does_not_refuse(self):
        """The scanner scanned the blurry page again (#393)."""
        self._insert(anchor=1, label="2")
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=1,
            value="",
            source_fingerprint="100:3",
        )

        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn(views_process.PAGE_REVIEW_APPROVED_MESSAGE, flashed)

    def test_the_bar_offers_the_button_with_no_request(self):
        response = self._step_one()

        self.assertFalse(response.context["repairs_waiting"])
        self.assertContains(
            response, "I reviewed this scan and it is complete"
        )
