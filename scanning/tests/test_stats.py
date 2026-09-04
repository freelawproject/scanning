"""Tests for the stats page (issue #260).

The page counts scans and volumes. This module covers the map of the
statuses (which must stay complete), the two tables, the rule that a
row with no file is not an upload, the repair pair, and the view.
"""

import tempfile

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import repairs, stats
from scanning.factories import (
    PageEditFactory,
    ReporterFactory,
    ScanFactory,
)
from scanning.models import (
    Detection,
    PageEdit,
    PageRepairRequest,
    Status,
)
from scanning.tests.test_views import ScanningTestCase

MEDIA_ROOT = tempfile.mkdtemp()


def _detection(scan):
    """Give the scan one detection row.

    :param scan: The scan the row belongs to.
    :returns: The row.
    """
    return Detection.objects.create(
        scan=scan,
        page_index=0,
        label="KEY",
        label_id=0,
        confidence=0.9,
        x0=0,
        y0=0,
        x1=10,
        y1=10,
        img_width=100,
        img_height=100,
    )


class StatsTestCase(ScanningTestCase):
    """Shared helpers for the counts."""

    def _row(self, rows, key):
        """Return the row of the table by its key.

        :param rows: A list from ``status_groups`` or ``funnel``.
        :param key: The key of the wanted row.
        :returns: The row.
        """
        for row in rows:
            if row["key"] == key:
                return row
        raise AssertionError(f"no row named {key}")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestStatusMap(StatsTestCase):
    """The map of the statuses must stay complete (#260)."""

    def test_every_status_is_in_one_group(self):
        """A status added later must not drop off the page."""
        seen = []
        for _key, _label, statuses in stats.STATUS_GROUPS:
            seen.extend(statuses)
        self.assertEqual(len(seen), len(set(seen)), "a status is in two rows")
        self.assertEqual(set(seen), set(Status.values))

    def test_the_groups_add_up_to_the_total(self):
        """The first table accounts for every scan."""
        ScanFactory(status=Status.UPLOADED)
        ScanFactory(status=Status.AWAITING)
        ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        ScanFactory(status=Status.ERROR_MAX_RETRIES)
        ScanFactory(status=Status.PENDING_REVIEW)

        rows = stats.status_groups()
        total = stats._totals(stats.uploaded_scans())
        self.assertEqual(sum(row["scans"] for row in rows), total["scans"])
        self.assertEqual(total["scans"], 6)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestWhatCounts(StatsTestCase):
    """Which rows the counts read, and how volumes are counted."""

    def test_a_scan_with_no_file_is_in_no_count(self):
        """A presign the browser never confirmed is not an upload."""
        ScanFactory(status=Status.UPLOADED)
        ScanFactory(status=Status.UPLOADED, original_pdf="")

        totals = stats._totals(stats.uploaded_scans())
        self.assertEqual(totals["scans"], 1)
        row = self._row(stats.status_groups(), "waiting_to_start")
        self.assertEqual(row["scans"], 1)

    def test_two_scans_of_one_volume_count_as_one_volume(self):
        """A multiscan volume raises the scan count only."""
        reporter = ReporterFactory()
        ScanFactory(
            reporter=reporter,
            volume=12,
            part_label="A",
            status=Status.UPLOADED,
        )
        ScanFactory(
            reporter=reporter,
            volume=12,
            part_label="B",
            status=Status.UPLOADED,
        )

        totals = stats._totals(stats.uploaded_scans())
        self.assertEqual(totals["scans"], 2)
        self.assertEqual(totals["volumes"], 1)

    def test_one_volume_number_of_two_reporters_is_two_volumes(self):
        """The volume is the pair, not the number alone."""
        # The factory reads ``short_name`` as the key of a
        # get-or-create, so two calls with the default give one
        # reporter.
        atlantic = ReporterFactory(short_name="a")
        pacific = ReporterFactory(short_name="p", full_name="Pacific Reporter")
        ScanFactory(reporter=atlantic, volume=12, status=Status.UPLOADED)
        ScanFactory(reporter=pacific, volume=12, status=Status.UPLOADED)

        totals = stats._totals(stats.uploaded_scans())
        self.assertEqual(totals["scans"], 2)
        self.assertEqual(totals["volumes"], 2)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestTheFunnel(StatsTestCase):
    """The funnel counts the new pipeline alone (#260, D8)."""

    def test_the_two_review_one_rows(self):
        """READY waits for the approval; DONE passed it."""
        ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        rows = stats.funnel()
        self.assertEqual(self._row(rows, "page_review_ready")["scans"], 1)
        self.assertEqual(self._row(rows, "page_review_passed")["scans"], 1)

    def test_the_redaction_row_needs_a_detection(self):
        """The same rule as the "Next: Detect" button."""
        with_rows = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        _detection(with_rows)

        row = self._row(stats.funnel(), "redaction_review_ready")
        self.assertEqual(row["scans"], 1)
        self.assertEqual(row["volumes"], 1)

    def test_a_detection_of_a_scan_in_review_one_does_not_count(self):
        """Review 2 follows review 1, and the count follows the status."""
        scan = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        _detection(scan)

        row = self._row(stats.funnel(), "redaction_review_ready")
        self.assertEqual(row["scans"], 0)

    def test_a_legacy_scan_is_in_the_legacy_row_only(self):
        """The retired pipeline has one counter, not a stage."""
        for status in stats.LEGACY_STATUSES:
            ScanFactory(status=status)

        legacy = self._row(stats.status_groups(), "legacy")
        self.assertEqual(legacy["scans"], len(stats.LEGACY_STATUSES))
        for row in stats.funnel():
            self.assertEqual(row["scans"], 0, row["key"])

    def test_the_two_rows_that_wait_for_a_stage_read_zero(self):
        """Step 3 (#206) and the text stage (#191) write no count yet."""
        ScanFactory(status=Status.APPROVED)
        ScanFactory(status=Status.EXTRACTED)

        rows = stats.funnel()
        for key in ("redaction_review_done", "text_review_ready"):
            row = self._row(rows, key)
            self.assertEqual(row["scans"], 0)
            self.assertTrue(row["note"])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRepairTotals(StatsTestCase):
    """How many pages wait, over how many scans (#249/#260)."""

    def setUp(self):
        """Give the test a user."""
        self.user = self.make_user()

    def _request(self, scan, page, **kwargs):
        """Ask for a page of the scan again.

        :param scan: The scan.
        :param page: The 1-based page of the original.
        :param kwargs: Overrides for the row.
        :returns: The row.
        """
        return PageRepairRequest.objects.create(
            scan=scan,
            requested_by=self.user,
            action=PageRepairRequest.Action.REPLACE,
            pdf_page=page,
            **kwargs,
        )

    def test_three_requests_over_two_scans(self):
        """The pair counts the rows and the scans they name."""
        first = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        second = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        self._request(first, 1)
        self._request(first, 2)
        self._request(second, 5)

        self.assertEqual(repairs.waiting_totals(), (3, 2))

    def test_a_dismissed_request_and_a_fulfilled_one_are_out(self):
        """Only the requests a scanner must still act on are counted."""
        scan = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        waiting = self._request(scan, 1)
        dismissed = self._request(scan, 2)
        dismissed.dismissed_at = timezone.now()
        dismissed.dismissed_by = self.user
        dismissed.save(update_fields=["dismissed_at", "dismissed_by"])
        answered = self._request(scan, 3)
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=answered.pdf_page,
            author=self.user,
        )

        self.assertEqual(repairs.waiting_totals(), (1, 1))
        self.assertEqual(
            list(repairs.queue("waiting").values_list("pk", flat=True)),
            [waiting.pk],
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestStatsView(StatsTestCase):
    """The page itself."""

    def setUp(self):
        """Give the test a user and a scan."""
        self.user = self.make_user()
        self.scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_user_who_is_not_logged_in_goes_to_the_login_page(self):
        """The page needs a login, as the repair queue does."""
        response = self.client.get(reverse("stats"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_the_page_shows_the_counts(self):
        """Every user sees the page and its numbers."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("stats"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["scans"], 1)
        self.assertEqual(response.context["totals"]["volumes"], 1)
        self.assertContains(response, "The review funnel")
        row = self._row(response.context["status_groups"], "page_review")
        self.assertEqual(row["scans"], 1)

    def test_the_page_costs_a_bounded_number_of_queries(self):
        """The shape is pinned: no query per row of a table."""
        ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        expected = (
            1  # the totals
            + len(stats.STATUS_GROUPS)
            + len([row for row in stats.FUNNEL_ROWS if row[2]])
            + 1  # the repair pair
        )
        with self.assertNumQueries(expected):
            stats.collect()
