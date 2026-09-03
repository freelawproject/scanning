"""Tests for the page repair requests of review 1 (issue #249).

A reviewer with no book records a page a scanner must scan again, or
a gap a scanner must fill. This module covers the row, the three
endpoints, the derived fulfilled state, what step 1 shows, and the
queue view.
"""

import json
import tempfile

from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import page_edits, repairs
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

        self._replace(pdf_page=2)

        self.assertEqual(self.scan.repair_requests.get().logical_page, "")

    def test_a_request_is_taken_in_any_status(self):
        self.scan.status = Status.PAGE_COMPLETENESS_REVIEW_DONE
        self.scan.save(update_fields=["status"])
        self.assertEqual(self._replace().status_code, 200)

    def test_login_is_required(self):
        self.client.logout()
        response = self._replace()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.scan.repair_requests.exists())


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

    def test_an_applied_edit_fulfils_whatever_the_fingerprint(self):
        # The apply (#206) rewrites the original, so its own edits
        # never match the new fingerprint. They are done work.
        self._replace(pdf_page=2)
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:3",
            applied_at=timezone.now(),
        )
        self.scan.source_fingerprint = "777:3"
        self.scan.save(update_fields=["source_fingerprint"])

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
