"""Tests for the creation of the ``Opinion`` rows (issue #336, part 1).

Four groups:

- the printed span and the key (``opinions.printed_span``,
  ``opinions.keyed``);
- the write (``opinions.create_rows``): the first run, the second run,
  and the two stale cards;
- the worker (``opinions.run``): where it parks the scan;
- the approval and the daemon: the queue and the dispatch.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from scanning import apply, opinions, review_states
from scanning.factories import (
    OpinionBoundaryFactory,
    OpinionFactory,
    OpinionFindingFactory,
    ScanFactory,
)
from scanning.management.commands.process_next_scan import Command
from scanning.models import (
    ApplyRun,
    Opinion,
    OpinionBoundary,
    OpinionCheck,
    OpinionFindingDismissal,
    OpinionReviewStatus,
    QueuedAction,
    Scan,
    Status,
)
from scanning.tests.test_views import ScanningTestCase


def printed_document(*pages) -> dict:
    """Build a printed-page map from ``(printed, type)`` pairs.

    One entry per physical page, in order; ``None`` for a page with no
    number.

    :param pages: The pairs, or None.
    :returns: The document ``apply.load_printed_pages`` returns.
    """
    entries = []
    for index, page in enumerate(pages):
        printed, kind = page if page else (None, None)
        entries.append(
            {"final_page": index + 1, "printed": printed, "type": kind}
        )
    return {"pages": entries}


def make_run(scan, number: int = 1) -> ApplyRun:
    """Create a bare apply run for the rows to point at.

    :param scan: The scan.
    :param number: The run's number.
    :returns: The run.
    """
    return ApplyRun.objects.create(
        scan=scan,
        number=number,
        source_fingerprint=scan.source_fingerprint or "",
    )


def make_boundary(scan, run, start: int, end: int, y: float = 100.0, **kw):
    """Create a computed boundary over the pages ``start`` to ``end``.

    :param scan: The scan.
    :param run: The apply run the boundary is measured in.
    :param start: The 0-based start page.
    :param end: The 0-based end page.
    :param y: The start anchor's y, for the reading order.
    :param kw: Extra factory fields.
    :returns: The row.
    """
    fields = {
        "scan": scan,
        "apply_run": run,
        "start_page_index": start,
        "start_source_page": start + 1,
        "start_y": y,
        "end_page_index": end,
        "end_source_page": end + 1,
        "source_fingerprint": scan.source_fingerprint or "",
    }
    fields.update(kw)
    return OpinionBoundaryFactory(**fields)


class TestPrintedSpan(TestCase):
    """The span the key and the data are read from."""

    def _row(self, start, end):
        return OpinionBoundaryFactory.build(
            start_page_index=start, end_page_index=end
        )

    def test_a_numbered_start_and_end(self):
        lookup = {0: (500, None), 1: (501, None), 2: (502, None)}

        self.assertEqual(
            opinions.printed_span(self._row(0, 2), lookup), (500, 502)
        )

    def test_a_start_page_with_no_number_raises(self):
        lookup = {1: (501, None)}

        with self.assertRaises(opinions.PrintedNumberError) as ctx:
            opinions.printed_span(self._row(0, 1), lookup)
        self.assertIn("Page 1 of the corrected volume", str(ctx.exception))

    def test_a_blank_last_leaf_is_legal(self):
        """The last printed page is read off the last numbered page."""
        lookup = {0: (500, None), 1: (501, None)}

        self.assertEqual(
            opinions.printed_span(self._row(0, 3), lookup), (500, 501)
        )

    def test_a_range_start_page_gives_its_end(self):
        lookup = {0: (677, 685), 1: (686, None)}

        self.assertEqual(
            opinions.printed_span(self._row(0, 1), lookup), (685, 686)
        )

    def test_a_range_end_page_gives_its_start(self):
        lookup = {0: (676, None), 1: (677, 685)}

        self.assertEqual(
            opinions.printed_span(self._row(0, 1), lookup), (676, 677)
        )

    def test_an_opinion_inside_one_range_page_covers_the_range(self):
        lookup = {0: (677, 685)}

        self.assertEqual(
            opinions.printed_span(self._row(0, 0), lookup), (677, 685)
        )

    def test_numbers_that_run_backwards_raise(self):
        lookup = {0: (500, None), 1: (5, None)}

        with self.assertRaises(opinions.PrintedNumberError):
            opinions.printed_span(self._row(0, 1), lookup)


class TestPrintedLookup(TestCase):
    """The lookup, with the one arm the sequence check does not have."""

    def test_a_suffixed_page_maps_to_its_number(self):
        document = printed_document(
            ("2094", "single"), ("2094a", "suffixed"), ("2095", "single")
        )

        self.assertEqual(
            opinions.printed_lookup(document),
            {0: (2094, None), 1: (2094, None), 2: (2095, None)},
        )

    def test_a_page_with_no_number_is_absent(self):
        document = printed_document(("1", "single"), None, ("3", "single"))

        self.assertEqual(
            opinions.printed_lookup(document), {0: (1, None), 2: (3, None)}
        )

    def test_a_range_page_keeps_both_ends(self):
        document = printed_document(("677-685", "range"))

        self.assertEqual(opinions.printed_lookup(document), {0: (677, 685)})


class TestTheKey(TestCase):
    """The rank inside one printed page follows the reading order."""

    def setUp(self):
        self.scan = ScanFactory(page_count=4)
        self.apply_run = make_run(self.scan)

    def test_two_opinions_on_one_printed_page_get_0_and_1(self):
        lower = make_boundary(self.scan, self.apply_run, 0, 1, y=400.0)
        upper = make_boundary(self.scan, self.apply_run, 0, 0, y=100.0)
        lookup = {0: (500, None), 1: (501, None)}

        keys = [
            (row.pk, first, index)
            for row, first, index, _ in opinions.keyed(
                opinions.live_boundaries(self.scan), lookup
            )
        ]

        self.assertEqual(keys, [(upper.pk, 500, 0), (lower.pk, 500, 1)])

    def test_a_suffixed_page_shares_the_bucket(self):
        on_number = make_boundary(self.scan, self.apply_run, 0, 0)
        on_suffix = make_boundary(self.scan, self.apply_run, 1, 1)
        lookup = opinions.printed_lookup(
            printed_document(("2094", "single"), ("2094a", "suffixed"))
        )

        keys = [
            (row.pk, first, index)
            for row, first, index, _ in opinions.keyed(
                opinions.live_boundaries(self.scan), lookup
            )
        ]

        self.assertEqual(
            keys, [(on_number.pk, 2094, 0), (on_suffix.pk, 2094, 1)]
        )

    def test_a_dismissed_boundary_is_not_live(self):
        kept = make_boundary(self.scan, self.apply_run, 0, 0)
        dismissed = make_boundary(self.scan, self.apply_run, 1, 1)
        decision = OpinionBoundaryFactory(
            scan=self.scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.DISMISS,
        )
        OpinionBoundary.objects.filter(pk=dismissed.pk).update(
            decision=decision
        )

        rows = opinions.live_boundaries(self.scan)

        self.assertEqual([r.pk for r in rows], [kept.pk])


class TestCheckSpace(TestCase):
    """Every boundary must be measured against the corrected volume."""

    def setUp(self):
        self.scan = ScanFactory(page_count=4, source_fingerprint="fp1")
        self.apply_run = make_run(self.scan)

    def test_rows_of_the_run_pass(self):
        rows = [make_boundary(self.scan, self.apply_run, 0, 1)]

        opinions.check_space(self.scan, self.apply_run, rows)

    def test_a_row_of_another_run_is_refused(self):
        other = make_run(self.scan, number=2)
        rows = [make_boundary(self.scan, other, 0, 1)]

        with self.assertRaises(opinions.BoundaryOutOfSpace):
            opinions.check_space(self.scan, self.apply_run, rows)

    def test_a_row_of_another_original_is_refused(self):
        rows = [make_boundary(self.scan, self.apply_run, 0, 1)]
        OpinionBoundary.objects.filter(pk=rows[0].pk).update(
            source_fingerprint="fp0"
        )
        rows = opinions.live_boundaries(self.scan)

        with self.assertRaises(opinions.BoundaryOutOfSpace):
            opinions.check_space(self.scan, self.apply_run, rows)


class TestCreateRows(TestCase):
    """The one writer of an ``Opinion`` row."""

    def setUp(self):
        self.scan = ScanFactory(page_count=6, source_fingerprint="fp1")
        self.apply_run = make_run(self.scan)
        self.printed = printed_document(
            *[(str(500 + i), "single") for i in range(6)]
        )

    def _create(self):
        rows = opinions.live_boundaries(self.scan)
        return opinions.create_rows(
            self.scan, self.apply_run, rows, self.printed
        )

    def test_the_first_run_writes_one_row_per_boundary(self):
        first = make_boundary(self.scan, self.apply_run, 0, 2)
        second = make_boundary(self.scan, self.apply_run, 3, 5)

        summary = self._create()

        self.assertEqual((summary.created, summary.updated), (2, 0))
        rows = list(Opinion.objects.filter(scan=self.scan))
        self.assertEqual(
            [
                (o.first_printed_page, o.index_in_page, o.last_printed_page)
                for o in rows
            ],
            [(500, 0, 502), (503, 0, 505)],
        )
        one = rows[0]
        self.assertEqual(one.page_count, 3)
        self.assertEqual((one.start_page_index, one.end_page_index), (0, 2))
        self.assertEqual((one.start_source_page, one.end_source_page), (1, 3))
        self.assertIsNone(one.start_source_edit_id)
        self.assertEqual(one.apply_run_id, self.apply_run.pk)
        self.assertEqual(one.source_fingerprint, "fp1")
        self.assertEqual(one.boundary_id, first.pk)
        self.assertEqual(one.status, OpinionReviewStatus.PROCESSING)
        self.assertEqual(rows[1].boundary_id, second.pk)

    def test_the_addresses_of_an_edit_page_are_copied(self):
        from scanning.factories import PageEditFactory

        edit = PageEditFactory(scan=self.scan)
        make_boundary(
            self.scan,
            self.apply_run,
            0,
            1,
            start_source_edit=edit,
            start_source_page=1,
        )

        self._create()

        one = Opinion.objects.get(scan=self.scan)
        self.assertEqual(one.start_source_edit_id, edit.pk)
        self.assertEqual(one.start_source_page, 1)

    def test_a_start_page_with_no_number_writes_nothing(self):
        make_boundary(self.scan, self.apply_run, 0, 1)
        make_boundary(self.scan, self.apply_run, 2, 3)
        self.printed = printed_document(
            ("500", "single"), ("501", "single"), None, ("503", "single")
        )

        with self.assertRaises(opinions.PrintedNumberError):
            self._create()

        self.assertFalse(Opinion.objects.filter(scan=self.scan).exists())

    def test_a_second_run_updates_in_place_and_keeps_the_human_fields(self):
        boundary = make_boundary(self.scan, self.apply_run, 0, 2)
        self._create()
        one = Opinion.objects.get(scan=self.scan)
        user = one.scan.uploaded_by
        Opinion.objects.filter(pk=one.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE,
            approved_by=user,
            notes="kept",
            glue_revision=3,
            approved_text_key="jobs/opinions/o1/approved.txt",
        )
        # The compute wrote the boundary again, one page longer.
        boundary.delete()
        longer = make_boundary(self.scan, self.apply_run, 0, 3)

        summary = self._create()

        self.assertEqual((summary.created, summary.updated), (0, 1))
        one.refresh_from_db()
        self.assertEqual(one.last_printed_page, 503)
        self.assertEqual(one.page_count, 4)
        self.assertEqual(one.end_page_index, 3)
        self.assertEqual(one.boundary_id, longer.pk)
        self.assertEqual(one.status, OpinionReviewStatus.TEXT_REVIEW_DONE)
        self.assertEqual(one.approved_by_id, user.pk)
        self.assertEqual(one.notes, "kept")
        self.assertEqual(one.glue_revision, 3)
        self.assertEqual(
            one.approved_text_key, "jobs/opinions/o1/approved.txt"
        )

    def test_a_second_run_under_a_new_apply_run_moves_the_indexes(self):
        make_boundary(self.scan, self.apply_run, 2, 3)
        self._create()
        one = Opinion.objects.get(scan=self.scan)
        OpinionBoundary.objects.filter(scan=self.scan).delete()
        # A page inserted before the opinion: same address, index +1.
        later = make_run(self.scan, number=2)
        make_boundary(
            self.scan, later, 3, 4, start_source_page=3, end_source_page=4
        )
        self.apply_run = later
        self.printed = printed_document(
            ("500", "single"),
            ("501", "single"),
            ("501a", "suffixed"),
            ("502", "single"),
            ("503", "single"),
        )

        summary = self._create()

        self.assertEqual((summary.created, summary.updated), (0, 1))
        one.refresh_from_db()
        self.assertEqual((one.start_page_index, one.end_page_index), (3, 4))
        self.assertEqual(one.apply_run_id, later.pk)

    def test_an_error_row_goes_back_to_processing(self):
        make_boundary(self.scan, self.apply_run, 0, 2)
        self._create()
        one = Opinion.objects.get(scan=self.scan)
        Opinion.objects.filter(pk=one.pk).update(
            status=OpinionReviewStatus.ERROR, error_message="boom"
        )

        self._create()

        one.refresh_from_db()
        self.assertEqual(one.status, OpinionReviewStatus.PROCESSING)

    def test_a_ready_row_is_not_reset(self):
        make_boundary(self.scan, self.apply_run, 0, 2)
        self._create()
        one = Opinion.objects.get(scan=self.scan)
        Opinion.objects.filter(pk=one.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

        self._create()

        one.refresh_from_db()
        self.assertEqual(one.status, OpinionReviewStatus.READY_FOR_TEXT_REVIEW)

    def test_a_boundary_that_is_gone_leaves_an_orphan_card(self):
        make_boundary(self.scan, self.apply_run, 0, 2)
        gone = make_boundary(self.scan, self.apply_run, 3, 5)
        self._create()
        gone.delete()

        summary = self._create()

        self.assertEqual(
            (summary.updated, summary.orphaned, summary.stale), (1, 1, 0)
        )
        orphan = Opinion.objects.get(scan=self.scan, first_printed_page=503)
        cards = list(orphan.findings.all())
        self.assertEqual(
            [(c.check_name, c.page_in_opinion) for c in cards],
            [(OpinionCheck.ORPHANED_OPINION, None)],
        )
        # The row keeps its data.
        self.assertEqual(orphan.last_printed_page, 505)
        self.assertEqual(Opinion.objects.filter(scan=self.scan).count(), 2)

    def test_a_number_that_changed_leaves_a_stale_card_and_a_new_row(self):
        make_boundary(self.scan, self.apply_run, 3, 5)
        self._create()
        old = Opinion.objects.get(scan=self.scan)
        # The reading of page 4 changed from 503 to 530.
        self.printed = printed_document(
            *[(str(500 + i), "single") for i in range(3)],
            ("530", "single"),
            ("531", "single"),
            ("532", "single"),
        )

        summary = self._create()

        self.assertEqual(
            (summary.created, summary.stale, summary.orphaned), (1, 1, 0)
        )
        old.refresh_from_db()
        card = old.findings.get()
        self.assertEqual(card.check_name, OpinionCheck.STALE_PAGE_NUMBER)
        self.assertIn("503", card.message)
        self.assertIn("530", card.message)
        self.assertTrue(
            Opinion.objects.filter(
                scan=self.scan, first_printed_page=530
            ).exists()
        )

    def test_a_row_that_matches_again_loses_its_cards(self):
        boundary = make_boundary(self.scan, self.apply_run, 0, 2)
        self._create()
        one = Opinion.objects.get(scan=self.scan)
        boundary.delete()
        self._create()
        self.assertEqual(one.findings.count(), 1)
        make_boundary(self.scan, self.apply_run, 0, 2)

        summary = self._create()

        self.assertEqual(summary.updated, 1)
        self.assertEqual(one.findings.count(), 0)

    def test_the_page_checks_of_another_rebuild_are_left_alone(self):
        make_boundary(self.scan, self.apply_run, 0, 2)
        self._create()
        one = Opinion.objects.get(scan=self.scan)
        OpinionFindingFactory(
            opinion=one,
            check_name=OpinionCheck.ENGINES_DISAGREE,
            page_in_opinion=1,
        )

        self._create()

        self.assertEqual(
            list(one.findings.values_list("check_name", flat=True)),
            [OpinionCheck.ENGINES_DISAGREE],
        )

    def test_a_row_of_another_scan_is_not_touched(self):
        other = OpinionFactory()
        make_boundary(self.scan, self.apply_run, 0, 2)

        self._create()

        other.refresh_from_db()
        self.assertEqual(other.findings.count(), 0)

    def test_the_summary_message(self):
        summary = opinions.Summary(created=2, updated=1, orphaned=1)

        self.assertEqual(
            summary.message,
            "3 opinion(s): 2 new, 1 updated, 1 without a boundary.",
        )
        self.assertEqual(
            opinions.Summary(created=1).message,
            "1 opinion(s): 1 new, 0 updated.",
        )


class TestTheWorker(TestCase):
    """Where ``opinions.run`` parks the scan.

    The daemon claims the scan before the work, so every test puts it
    in PROCESSING first: the park writes over the busy statuses alone.
    """

    def setUp(self):
        self.scan = ScanFactory(page_count=3, status=Status.PROCESSING)
        self.apply_run = make_run(self.scan)
        self.printed = printed_document(
            ("500", "single"), ("501", "single"), ("502", "single")
        )

    def _run(self, run="default", printed=None):
        final = self.apply_run if run == "default" else run
        with (
            patch.object(review_states, "final_run", return_value=final),
            patch.object(
                apply,
                "load_printed_pages",
                return_value=printed or self.printed,
            ),
        ):
            opinions.run(self.scan.pk)
        self.scan.refresh_from_db()

    def test_a_success_parks_in_review_two_done(self):
        make_boundary(self.scan, self.apply_run, 0, 2)

        self._run()

        self.assertEqual(self.scan.status, Status.REDACTION_REVIEW_DONE)
        self.assertIn("1 opinion(s): 1 new", self.scan.progress_message)
        self.assertEqual(Opinion.objects.filter(scan=self.scan).count(), 1)

    def test_no_corrected_volume_parks_back_in_review_two(self):
        make_boundary(self.scan, self.apply_run, 0, 2)

        self._run(run=None)

        self.assertEqual(self.scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn("not built", self.scan.progress_message)
        self.assertFalse(Opinion.objects.filter(scan=self.scan).exists())

    def test_a_missing_number_parks_back_with_the_page_named(self):
        make_boundary(self.scan, self.apply_run, 1, 2)

        self._run(
            printed=printed_document(
                ("500", "single"), None, ("502", "single")
            )
        )

        self.assertEqual(self.scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn(
            "Page 2 of the corrected volume", self.scan.progress_message
        )

    def test_a_boundary_of_another_run_parks_back(self):
        other = make_run(self.scan, number=2)
        make_boundary(self.scan, other, 0, 2)

        self._run()

        self.assertEqual(self.scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn("Recompute the redactions", self.scan.progress_message)

    def test_an_unreadable_document_parks_back(self):
        make_boundary(self.scan, self.apply_run, 0, 2)
        with (
            patch.object(
                review_states, "final_run", return_value=self.apply_run
            ),
            patch.object(
                apply,
                "load_printed_pages",
                side_effect=apply.ApplyError("the printed pages did not load"),
            ),
        ):
            opinions.run(self.scan.pk)

        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn("did not load", self.scan.progress_message)

    def test_an_unexpected_failure_parks_back_and_raises_nothing(self):
        make_boundary(self.scan, self.apply_run, 0, 2)
        with (
            patch.object(
                review_states, "final_run", return_value=self.apply_run
            ),
            patch.object(
                apply, "load_printed_pages", return_value=self.printed
            ),
            patch.object(
                opinions, "create_rows", side_effect=RuntimeError("boom")
            ),
            self.assertLogs("scanning.opinions", level="ERROR"),
        ):
            opinions.run(self.scan.pk)

        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn("Approve again", self.scan.progress_message)

    def test_an_admin_who_moved_the_scan_keeps_their_decision(self):
        """The park is guarded on the busy statuses."""
        make_boundary(self.scan, self.apply_run, 0, 2)
        Scan.objects.filter(pk=self.scan.pk).update(status=Status.ERROR)

        self._run()

        self.assertEqual(self.scan.status, Status.ERROR)

    def test_a_volume_with_no_boundary_writes_no_row_and_closes(self):
        self._run()

        self.assertEqual(self.scan.status, Status.REDACTION_REVIEW_DONE)
        self.assertFalse(Opinion.objects.filter(scan=self.scan).exists())


class TestTheQueue(TestCase):
    """The approval's write, and the daemon's claim."""

    def test_a_ready_scan_is_queued(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        self.assertTrue(opinions.queue_create_opinions(scan))

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.CREATE_OPINIONS)

    def test_any_other_status_is_refused(self):
        for status in (
            Status.REDACTION_REVIEW_DONE,
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
            Status.PENDING_REVIEW,
            Status.QUEUED,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(status=status)

                self.assertFalse(opinions.queue_create_opinions(scan))

                scan.refresh_from_db()
                self.assertEqual(scan.status, status)

    def test_the_daemon_dispatches_the_worker(self):
        scan = ScanFactory(
            status=Status.QUEUED, queued_action=QueuedAction.CREATE_OPINIONS
        )

        with (
            patch("django.db.connections.close_all"),
            patch("scanning.services.run_create_opinions") as worker,
        ):
            Command().handle()

        worker.assert_called_once_with(scan.pk)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)

    def test_the_service_calls_the_body(self):
        from scanning import services

        with (
            patch("django.db.connections.close_all"),
            patch.object(opinions, "run") as body,
        ):
            services.run_create_opinions(7)

        body.assert_called_once_with(7)


class TestTheStepTwoNote(ScanningTestCase):
    """The reason of a failed run reaches the reviewer.

    The worker parks the scan in READY_FOR_REDACTION_REVIEW with the
    reason in ``progress_message``, and the poll reloads the page at
    once, so the step-2 bar must print that field there.
    """

    def setUp(self):
        self.client.force_login(self.make_user(username="reviewer"))

    def _bar(self, scan):
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": scan.pk}),
            {"step": 2},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["html"]

    def test_the_reason_shows_in_review_two(self):
        scan = ScanFactory(
            status=Status.READY_FOR_REDACTION_REVIEW,
            progress_message="Page 2 of the corrected volume has no printed number.",
        )

        bar = self._bar(scan)

        self.assertIn("review2-note", bar)
        self.assertIn("Page 2 of the corrected volume", bar)

    def test_the_note_is_escaped(self):
        scan = ScanFactory(
            status=Status.READY_FOR_REDACTION_REVIEW,
            progress_message="<b>boom</b>",
        )

        bar = self._bar(scan)

        self.assertIn("&lt;b&gt;boom", bar)
        self.assertNotIn("<b>boom", bar)

    def test_no_note_without_a_message(self):
        scan = ScanFactory(
            status=Status.READY_FOR_REDACTION_REVIEW, progress_message=""
        )

        self.assertNotIn("review2-note", self._bar(scan))

    def test_no_note_after_the_approval(self):
        scan = ScanFactory(
            status=Status.REDACTION_REVIEW_DONE,
            progress_message="1 opinion(s): 1 new, 0 updated.",
        )

        self.assertNotIn("review2-note", self._bar(scan))


class TestSuffixedNumber(TestCase):
    """The bucket of a page with a trailing letter."""

    def test_the_letter_is_dropped(self):
        from scanning import page_numbers

        self.assertEqual(page_numbers.suffixed_number("2094a"), 2094)
        self.assertEqual(page_numbers.suffixed_number("12B"), 12)

    def test_another_shape_is_none(self):
        from scanning import page_numbers

        for value in (None, "", "2094", "677-685", "abc"):
            with self.subTest(value=value):
                self.assertIsNone(page_numbers.suffixed_number(value))


class TestFindingCounts(TestCase):
    """The warning badge of the opinions list (#334)."""

    def setUp(self):
        self.scan = ScanFactory()
        self.opinion = OpinionFactory(scan=self.scan)

    def test_an_opinion_with_no_finding_is_absent(self):
        counts = opinions.finding_counts([self.opinion.pk])

        self.assertEqual(counts, {})

    def test_the_findings_of_two_pages_are_one_count(self):
        OpinionFindingFactory(opinion=self.opinion, page_in_opinion=0)
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=1,
            check_name=OpinionCheck.COLUMN_EDGE,
        )

        counts = opinions.finding_counts([self.opinion.pk])

        self.assertEqual(counts, {self.opinion.pk: (2, 0)})

    def test_a_stale_finding_counts_twice(self):
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.ORPHANED_OPINION,
        )

        counts = opinions.finding_counts([self.opinion.pk])

        self.assertEqual(counts, {self.opinion.pk: (1, 1)})

    def test_a_dismissed_finding_does_not_count(self):
        dismissal = OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        OpinionFindingFactory(opinion=self.opinion, dismissal=dismissal)
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=1,
            check_name=OpinionCheck.COLUMN_EDGE,
        )

        counts = opinions.finding_counts([self.opinion.pk])

        self.assertEqual(counts, {self.opinion.pk: (1, 0)})

    def test_two_opinions_take_one_query(self):
        other = OpinionFactory(scan=self.scan)
        OpinionFindingFactory(opinion=self.opinion)
        OpinionFindingFactory(opinion=other)

        with self.assertNumQueries(1):
            counts = opinions.finding_counts([self.opinion.pk, other.pk])

        self.assertEqual(counts[self.opinion.pk], (1, 0))
        self.assertEqual(counts[other.pk], (1, 0))
