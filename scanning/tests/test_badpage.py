"""Tests for the bad-page score of review 1 (#436).

The score is a model's suspicion that a page is a bad scan. These
tests pin what the rest of review 1 relies on: the stamp on the scan,
the cards derived from it on every rebuild, the dismissal and the
deletion that answer them, the command that writes them, and the
step-1 page that shows them ranked.
"""

import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import fitz
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from scanning import apply, page_edits, services
from scanning.badpage import scoring
from scanning.factories import ScanFactory
from scanning.models import (
    CHECKS_A_DELETION_ANSWERS,
    PHYSICAL_PAGE_CHECKS,
    CheckName,
    Issue,
    PageEdit,
    PageRepairRequest,
    Status,
)
from scanning.tests.pdf_fixtures import write_bitonal_page
from scanning.tests.test_views import ScanningTestCase


def _entry(page: int, detected: str | None) -> dict:
    """One ``ocr_results`` entry, as the glue writes it."""
    return {
        "pdf_page": page,
        "detected": detected,
        "type": "single" if detected else None,
        "score": 0.9 if detected else None,
        "zone": "corner" if detected else None,
        "ocr": "dots_mocr" if detected else None,
        "img_width": 1700,
        "img_height": 2200,
    }


def _reviewable_scan(**kwargs):
    """A scan in page review with a numbered reading of every page."""
    kwargs.setdefault("status", Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
    kwargs.setdefault("page_count", 3)
    kwargs.setdefault("source_fingerprint", "100:3")
    kwargs.setdefault(
        "ocr_results",
        [_entry(p, str(p)) for p in range(1, kwargs["page_count"] + 1)],
    )
    return ScanFactory(**kwargs)


def _bitonal_volume(path: Path, pages: int) -> None:
    """Write a bitonal PDF of ``pages`` text-shaped pages to ``path``."""
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i in range(pages):
            part = Path(tmp) / f"p{i}.pdf"
            write_bitonal_page(
                part, header_line=True, corner_number=True, tmp_dir=Path(tmp)
            )
            parts.append(part)
        with fitz.open() as out:
            for part in parts:
                with fitz.open(str(part)) as src:
                    out.insert_pdf(src)
            out.save(str(path))


class TestScoring(ScanningTestCase):
    """The model scores every page of a PDF, and the stamp is read back."""

    def test_scores_every_page_of_a_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "bitonal.pdf"
            _bitonal_volume(pdf, 3)
            scores = scoring.score_pdf(pdf, jobs=2)
        self.assertEqual(sorted(scores), [1, 2, 3])
        for score in scores.values():
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_stamp_is_read_back_worst_first(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.9, 2: 0.05, 3: 0.4})
        scan.refresh_from_db()
        self.assertEqual(scan.page_scores["source_fingerprint"], "100:3")
        self.assertEqual(scoring.scores_of(scan), {1: 0.9, 2: 0.05, 3: 0.4})
        self.assertEqual(scoring.flagged(scan), [(1, 0.9), (3, 0.4)])

    def test_a_stamp_for_another_upload_reads_as_unscored(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.9})
        scan.source_fingerprint = "200:3"
        scan.save(update_fields=["source_fingerprint"])
        scan.refresh_from_db()
        self.assertEqual(scoring.scores_of(scan), {})
        self.assertEqual(scoring.issues(scan), [])

    def test_a_blank_fingerprint_matches_anything(self):
        scan = _reviewable_scan(source_fingerprint="")
        scoring.stamp(scan, {2: 0.5})
        scan.source_fingerprint = "300:3"
        scan.save(update_fields=["source_fingerprint"])
        scan.refresh_from_db()
        self.assertEqual(scoring.flagged(scan), [(2, 0.5)])


class TestCards(ScanningTestCase):
    """The cards are derived on every rebuild and answered like any other."""

    def test_bad_page_is_a_physical_check_a_deletion_answers(self):
        self.assertIn(CheckName.BAD_PAGE, PHYSICAL_PAGE_CHECKS)
        self.assertIn(CheckName.BAD_PAGE, CHECKS_A_DELETION_ANSWERS)

    def test_rebuild_writes_one_card_per_flagged_page(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.9, 2: 0.05, 3: 0.4})
        scan.refresh_from_db()
        services.recalculate_issues(scan)
        cards = Issue.objects.filter(
            scan=scan, check_name=CheckName.BAD_PAGE
        ).order_by("page_number")
        self.assertEqual(
            [(c.page_number, c.metadata["score"]) for c in cards],
            [(1, 0.9), (3, 0.4)],
        )
        self.assertEqual(cards[0].severity, "warning")
        self.assertIn("0.90", cards[0].message)

    def test_a_dismissal_survives_the_rebuild(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.9, 3: 0.4})
        scan.refresh_from_db()
        services.recalculate_issues(scan)
        card = Issue.objects.get(
            scan=scan, check_name=CheckName.BAD_PAGE, page_number=1
        )
        self.client.force_login(self.make_user())
        response = self.client.post(
            reverse("dismiss_issue", kwargs={"pk": scan.pk}),
            data={"issue_id": card.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        scan.refresh_from_db()
        services.recalculate_issues(scan)
        pages = list(
            Issue.objects.filter(
                scan=scan, check_name=CheckName.BAD_PAGE
            ).values_list("page_number", flat=True)
        )
        self.assertEqual(pages, [3])

    def test_a_deleted_page_answers_its_card(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.9, 3: 0.4})
        scan.refresh_from_db()
        page_edits.supersede(
            scan,
            PageEdit.Kind.DELETE_PAGE,
            {"pdf_page": 3},
            {"source_fingerprint": scan.source_fingerprint},
            self.make_user(),
        )
        services.recalculate_issues(scan)
        pages = list(
            Issue.objects.filter(
                scan=scan, check_name=CheckName.BAD_PAGE
            ).values_list("page_number", flat=True)
        )
        self.assertEqual(pages, [1])


class TestCommand(ScanningTestCase):
    """``score_bad_pages`` stamps a volume and writes its cards."""

    def _run(self, *args):
        out = StringIO()
        call_command("score_bad_pages", *args, stdout=out)
        return out.getvalue()

    def test_scores_a_file_and_writes_the_cards(self):
        scan = _reviewable_scan(page_count=3)
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "bitonal.pdf"
            _bitonal_volume(pdf, 3)
            out = self._run(str(scan.pk), "--pdf", str(pdf))
        scan.refresh_from_db()
        self.assertEqual(sorted(scan.page_scores["scores"]), ["1", "2", "3"])
        self.assertIn("scores stamped", out)
        flagged = scoring.flagged(scan)
        cards = Issue.objects.filter(scan=scan, check_name=CheckName.BAD_PAGE)
        self.assertEqual(cards.count(), len(flagged))

    def test_refuses_a_file_of_another_page_count(self):
        scan = _reviewable_scan(page_count=5)
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "bitonal.pdf"
            _bitonal_volume(pdf, 2)
            with self.assertRaises(CommandError):
                self._run(str(scan.pk), "--pdf", str(pdf))
        scan.refresh_from_db()
        self.assertIsNone(scan.page_scores)


class TestStep1Page(ScanningTestCase):
    """The page shows the cards ranked and badges the pages in the list."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def _page(self, scan):
        response = self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_cards_are_ranked_and_pages_badged(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.4, 2: 0.05, 3: 0.9})
        scan.refresh_from_db()
        services.recalculate_issues(scan)
        html = self._page(scan)
        self.assertIn('Possible bad scans (<span id="bad-pages-count">2', html)
        self.assertLess(html.index("Score 0.90"), html.index("Score 0.40"))
        self.assertIn(
            'title="Bad-page score 0.90: worth checking">0.90<', html
        )
        self.assertIn(
            'title="Bad-page score 0.40: worth checking">0.40<', html
        )
        self.assertNotIn("Bad-page score 0.05", html)
        # The cards are not repeated in the issue list.
        issues = html[html.index('id="issues-section"') :]
        self.assertNotIn("bad_page", issues[: issues.index("pages-list")])

    def test_a_requested_page_has_no_card(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.4, 3: 0.9})
        scan.refresh_from_db()
        PageRepairRequest.objects.create(
            scan=scan,
            action=PageRepairRequest.Action.REPLACE,
            pdf_page=3,
            requested_by=self.make_user(),
            source_fingerprint=scan.source_fingerprint,
        )
        services.recalculate_issues(scan)
        html = self._page(scan)
        self.assertIn('Possible bad scans (<span id="bad-pages-count">1', html)
        self.assertIn("Score 0.40", html)
        self.assertNotIn("Score 0.90", html)
        # The row of the page list still shows the score: the request
        # answers the card, not the measurement.
        self.assertIn(
            'title="Bad-page score 0.90: worth checking">0.90<', html
        )

    def test_an_unscored_volume_hides_the_section_and_says_pending(self):
        scan = _reviewable_scan()
        services.recalculate_issues(scan)
        html = self._page(scan)
        self.assertIn('id="bad-pages-section" hidden', html)
        self.assertNotIn("Bad-page score", html)
        self.assertIn("Bad-scan check not yet run", html)

    def test_a_scored_volume_says_nothing(self):
        scan = _reviewable_scan()
        scoring.stamp(scan, {1: 0.01, 2: 0.02, 3: 0.0})
        scan.refresh_from_db()
        services.recalculate_issues(scan)
        html = self._page(scan)
        self.assertNotIn("Bad-scan check", html)

    def test_a_failed_volume_is_not_pending(self):
        scan = _reviewable_scan()
        for _ in range(scoring.MAX_ATTEMPTS):
            scoring.record_failure(scan, "no bitonal copy")
        scan.refresh_from_db()
        self.assertEqual(scoring.state(scan), scoring.FAILED)
        html = self._page(scan)
        self.assertNotIn("Bad-scan check", html)

    def test_state_rule(self):
        scan = _reviewable_scan()
        self.assertEqual(scoring.state(scan), scoring.PENDING)
        scoring.record_failure(scan, "once")
        self.assertEqual(scoring.state(scan), scoring.PENDING)
        scoring.stamp(scan, {})
        self.assertEqual(scoring.state(scan), scoring.SCORED)
        scoring.stamp(scan, {1: 0.9})
        scan.source_fingerprint = "999:3"
        self.assertEqual(scoring.state(scan), scoring.PENDING)


class TestPass(ScanningTestCase):
    """The daemon pass scores the newest owed volume and counts its faults."""

    def test_owed_scans_are_past_the_merge_and_unscored(self):
        waiting = _reviewable_scan(status=Status.AWAITING)
        uploaded = _reviewable_scan(status=Status.UPLOADED)
        unsharded = _reviewable_scan(source_fingerprint="")
        older = _reviewable_scan(status=Status.AWAITING_VALIDATION)
        newer = _reviewable_scan()
        scored = _reviewable_scan()
        scoring.stamp(scored, {1: 0.1})
        self.assertEqual(
            [s.pk for s in scoring.owed_scans()], [newer.pk, older.pk]
        )
        for scan in (waiting, uploaded, unsharded, scored):
            self.assertNotIn(scan.pk, [s.pk for s in scoring.owed_scans()])

    def test_tick_scores_the_newest_owed_scan_and_writes_its_cards(self):
        _reviewable_scan()
        newest = _reviewable_scan(page_count=2)
        output_dir = Path(newest.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        _bitonal_volume(output_dir / "bitonal.pdf", 2)
        self.assertEqual(scoring.run_tick(jobs=2), 1)
        newest.refresh_from_db()
        self.assertEqual(sorted(newest.page_scores["scores"]), ["1", "2"])
        self.assertEqual(
            Issue.objects.filter(
                scan=newest, check_name=CheckName.BAD_PAGE
            ).count(),
            len(scoring.flagged(newest)),
        )
        # The next tick takes the next scan; the scored one is done.
        self.assertNotIn(newest.pk, [s.pk for s in scoring.owed_scans()])

    def test_a_fault_counts_on_the_stamp_and_the_cap_retires_the_row(self):
        scan = _reviewable_scan()
        with mock.patch.object(
            apply,
            "volume_bitonal_key",
            return_value="processing/x/bitonal.pdf",
        ):
            for attempt in range(1, scoring.MAX_ATTEMPTS + 1):
                self.assertEqual(scoring.run_tick(jobs=2), 0)
                scan.refresh_from_db()
                self.assertEqual(scan.page_scores["attempts"], attempt)
                self.assertIn("error", scan.page_scores)
        self.assertEqual(scoring.scores_of(scan), {})
        self.assertNotIn(scan.pk, [s.pk for s in scoring.owed_scans()])

    def test_the_command_without_a_scan_runs_one_tick(self):
        scan = _reviewable_scan(page_count=2)
        output_dir = Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        _bitonal_volume(output_dir / "bitonal.pdf", 2)
        out = StringIO()
        # The tick closes the daemon's connections first, which would
        # close this test's transaction too.
        with (
            mock.patch.object(scoring, "jobs_setting", return_value=2),
            mock.patch("django.db.connections.close_all"),
        ):
            call_command("score_bad_pages", stdout=out)
        self.assertIn("Scored 1 volume(s)", out.getvalue())
        scan.refresh_from_db()
        self.assertIsNotNone(scan.page_scores)
