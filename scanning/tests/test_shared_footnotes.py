"""Tests for the footnotes of a shared first page (issue #457).

On a page where one opinion ends and the next starts, the earlier
opinion's footnotes are printed at the foot of the page. blackletter's
masks stop above them, so they went into the later opinion's text and
its redacted PDF. The later opinion's first page now masks them, a
card asks a person to look, and "Keep the footnotes" lifts the mask.
"""

from unittest.mock import patch

import fitz
from blackletter.models import Label
from django.urls import reverse

from scanning import (
    boundaries,
    ensemble,
    opinion_ocr,
    opinion_pdf,
    paragraphs,
    shared_footnotes,
)
from scanning.factories import (
    OpinionBoundaryFactory,
    OpinionFactory,
    ScanFactory,
)
from scanning.models import (
    Issue,
    KeptFootnotes,
    OpinionCheck,
    OpinionFindingDismissal,
    OpinionReviewStatus,
)
from scanning.tests.test_boundaries import IMG_H, IMG_W
from scanning.tests.test_detections import model_row
from scanning.tests.test_ensemble import engine_page, read_units, unit
from scanning.tests.test_views import ScanningTestCase


def detection(scan, page_index, label, x0, y0, x1, y1):
    """Store one live model detection, in pixels."""
    return model_row(
        scan,
        page_index=page_index,
        source_page=page_index + 1,
        label=label.name,
        label_id=int(label),
        confidence=0.95,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        img_width=IMG_W,
        img_height=IMG_H,
    )


def shared_page(scan, page_index=1):
    """Draw two columns and a footnote band on one page."""
    detection(scan, page_index, Label.TEXT_COLUMN, 100, 100, 800, 1700)
    detection(scan, page_index, Label.TEXT_COLUMN, 900, 100, 1600, 1700)
    return detection(scan, page_index, Label.FOOTNOTES, 100, 1720, 1600, 2050)


def two_opinions(scan):
    """Return two boundaries: the first ends on page 1, the second starts.

    The first ends low in the left column of page 1, and the second
    starts at the top of its right column.
    """
    before = OpinionBoundaryFactory(
        scan=scan,
        start_page_index=0,
        start_source_page=1,
        end_page_index=1,
        end_source_page=2,
        start_x=40.0,
        start_y=40.0,
        end_x=150.0,
        end_y=400.0,
    )
    after = OpinionBoundaryFactory(
        scan=scan,
        start_page_index=1,
        start_source_page=2,
        end_page_index=2,
        end_source_page=3,
        start_x=330.0,
        start_y=40.0,
        end_x=500.0,
        end_y=700.0,
    )
    return before, after


def footnote_masks(rects):
    """Return the footnote masks of one boundary's rects."""
    return [r for r in rects if r.get("kind") == boundaries.FOOTNOTES_MASK]


class TestTheMask(ScanningTestCase):
    """``boundaries.outside_rects`` masks the earlier opinion's notes."""

    def setUp(self):
        self.scan = ScanFactory(page_count=3)
        shared_page(self.scan)
        self.before, self.after = two_opinions(self.scan)

    def test_the_later_opinion_masks_the_footnotes_of_its_first_page(self):
        masks = boundaries.outside_rects(self.scan, [self.before, self.after])

        notes = footnote_masks(masks[self.after.pk])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["page_index"], 1)
        # The band of the detection, in points (200 dpi to 72).
        self.assertAlmostEqual(notes[0]["y0"], 1720 * 72 / 200, places=0)
        self.assertAlmostEqual(notes[0]["y1"], 2050 * 72 / 200, places=0)

    def test_the_earlier_opinion_keeps_the_footnotes_of_its_last_page(self):
        masks = boundaries.outside_rects(self.scan, [self.before, self.after])

        self.assertEqual(footnote_masks(masks[self.before.pk]), [])

    def test_a_kept_row_lifts_the_mask(self):
        masks = boundaries.outside_rects(
            self.scan, [self.after], kept_footnotes={self.after.pk}
        )

        self.assertEqual(footnote_masks(masks[self.after.pk]), [])
        # The mask over the earlier opinion's text stays.
        self.assertTrue(masks[self.after.pk])

    def test_a_first_page_nobody_shares_has_no_footnote_mask(self):
        scan = ScanFactory(page_count=3)
        shared_page(scan, page_index=0)
        row = OpinionBoundaryFactory(scan=scan, start_x=330.0, start_y=40.0)

        masks = boundaries.outside_rects(scan, [row])

        self.assertEqual(footnote_masks(masks[row.pk]), [])

    def test_a_dismissed_opinion_before_shares_no_page(self):
        boundaries.dismiss(self.scan, self.before, self.make_user())

        self.assertEqual(
            boundaries.shared_first_pages(self.scan, [self.after]), {}
        )

    def test_the_opinion_must_come_before_in_reading_order(self):
        """Only the later start shares its first page with the other."""
        self.assertEqual(
            boundaries.shared_first_pages(
                self.scan, [self.before, self.after]
            ),
            {self.after.pk: 400.0},
        )

    def test_a_footnote_box_above_the_end_of_the_opinion_before_stays(self):
        """A stray box over the text is not the foot of the page."""
        detection(self.scan, 1, Label.FOOTNOTES, 900, 300, 1600, 600)

        masks = boundaries.outside_rects(self.scan, [self.after])

        notes = footnote_masks(masks[self.after.pk])
        self.assertEqual(len(notes), 1)
        self.assertGreater(notes[0]["y0"], 400.0)


class TestTheVerdict(ScanningTestCase):
    """The OCR glue names a unit under the footnote mask."""

    BOX = [100.0, 620.0, 500.0, 700.0]

    def test_a_unit_under_the_footnote_mask_is_the_neighbours_note(self):
        mask = {
            "x0": 90.0,
            "y0": 600.0,
            "x1": 520.0,
            "y1": 740.0,
            "kind": boundaries.FOOTNOTES_MASK,
        }

        exclusion, share = opinion_ocr.verdict(self.BOX, [], [mask])

        self.assertEqual(
            exclusion, {"reason": opinion_ocr.NEIGHBOUR_FOOTNOTES}
        )
        self.assertEqual(share, 1.0)

    def test_a_unit_under_the_text_mask_stays_outside(self):
        mask = {"x0": 90.0, "y0": 600.0, "x1": 520.0, "y1": 740.0}

        exclusion, _ = opinion_ocr.verdict(self.BOX, [], [mask])

        self.assertEqual(exclusion, {"reason": "outside"})


def page_of(dropped=(), groups=()):
    """Return one page of an ensemble document, the fields the card reads."""
    return {
        "page_in_opinion": 0,
        "dropped": list(dropped),
        "groups": list(groups),
    }


NOTE_DROP = {
    "reason": opinion_ocr.NEIGHBOUR_FOOTNOTES,
    "section": ensemble.FOOTNOTES,
}


class TestTheCard(ScanningTestCase):
    """``ensemble._shared_footnotes_card`` over one page."""

    def setUp(self):
        self.opinion = OpinionFactory()

    def card(self, page, kept=False):
        return ensemble._shared_footnotes_card(self.opinion, page, kept, {})

    def test_a_page_whose_notes_nobody_took_has_no_card(self):
        drop = {"reason": "outside", "section": ensemble.BODY}

        self.assertIsNone(self.card(page_of([drop])))

    def test_notes_taken_under_a_text_with_no_mark_are_a_warning(self):
        card = self.card(page_of([NOTE_DROP, NOTE_DROP]))

        self.assertEqual(card.check_name, OpinionCheck.SHARED_FOOTNOTES)
        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertIn("2 footnote block(s)", card.message)

    def test_a_footnote_mark_in_the_text_makes_it_an_error(self):
        group = {
            "section": ensemble.BODY,
            "text": "The court held 1 that",
            "marks": [{"kind": "sup", "start": 15, "end": 16}],
        }

        card = self.card(page_of([NOTE_DROP], [group]))

        self.assertEqual(card.severity, Issue.Severity.ERROR)
        self.assertIn("mark(s) 1", card.message)

    def test_a_mark_of_a_footnote_group_does_not_count(self):
        group = {
            "section": ensemble.FOOTNOTES,
            "text": "1 See",
            "marks": [{"kind": "sup", "start": 0, "end": 1}],
        }

        card = self.card(page_of([NOTE_DROP], [group]))

        self.assertEqual(card.severity, Issue.Severity.WARNING)

    def test_an_opinion_of_one_page_makes_it_an_error(self):
        self.opinion.page_count = 1

        card = self.card(page_of([NOTE_DROP]))

        self.assertEqual(card.severity, Issue.Severity.ERROR)
        self.assertIn("starts and ends on this page", card.message)

    def test_a_kept_page_says_so(self):
        card = self.card(page_of(), kept=True)

        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertIn("kept the footnotes", card.message)


class TestBodyMarks(ScanningTestCase):
    def test_the_labels_of_the_sup_marks_in_order(self):
        group = {
            "text": "a* b 12 c x",
            "marks": [
                {"kind": "sup", "start": 5, "end": 7},
                {"kind": "sup", "start": 1, "end": 2},
                {"kind": "em", "start": 8, "end": 9},
                {"kind": "sup", "start": 10, "end": 11},
            ],
        }

        self.assertEqual(paragraphs.body_marks(group), ["*", "12"])


class TestKeep(ScanningTestCase):
    """``shared_footnotes.keep`` and ``give_back``."""

    def setUp(self):
        self.user = self.make_user()
        self.opinion = OpinionFactory(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

    def test_a_keep_writes_one_row_and_raises_the_revision(self):
        self.assertTrue(shared_footnotes.keep(self.opinion, self.user))
        self.assertFalse(shared_footnotes.keep(self.opinion, self.user))

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.glue_revision, 1)
        self.assertTrue(shared_footnotes.kept(self.opinion))
        self.assertEqual(KeptFootnotes.objects.count(), 1)

    def test_a_give_back_withdraws_the_row_and_raises_the_revision(self):
        shared_footnotes.keep(self.opinion, self.user)

        self.assertTrue(shared_footnotes.give_back(self.opinion, self.user))
        self.assertFalse(shared_footnotes.give_back(self.opinion, self.user))

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.glue_revision, 2)
        self.assertFalse(shared_footnotes.kept(self.opinion))
        # Withdrawn, never deleted.
        self.assertEqual(KeptFootnotes.objects.count(), 1)

    def test_a_row_of_another_first_page_does_not_apply(self):
        shared_footnotes.keep(self.opinion, self.user)
        self.opinion.start_source_page = 5
        self.opinion.save(update_fields=["start_source_page"])

        self.assertFalse(shared_footnotes.kept(self.opinion))
        # A new keep withdraws the stale row and writes one for the page.
        self.assertTrue(shared_footnotes.keep(self.opinion, self.user))
        self.assertTrue(shared_footnotes.kept(self.opinion))
        self.assertEqual(
            KeptFootnotes.objects.filter(withdrawn_at__isnull=True).count(),
            1,
        )

    def test_the_status_is_read_again_under_the_lock(self):
        """An approval between the view's check and the write refuses."""
        stale = type(self.opinion).objects.get(pk=self.opinion.pk)
        self.opinion.status = OpinionReviewStatus.TEXT_REVIEW_DONE
        self.opinion.save(update_fields=["status"])

        with self.assertRaises(shared_footnotes.FootnotesClosed):
            shared_footnotes.keep(stale, self.user)
        with self.assertRaises(shared_footnotes.FootnotesClosed):
            shared_footnotes.give_back(stale, self.user)

        self.assertFalse(KeptFootnotes.objects.exists())
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.glue_revision, 0)

    def test_a_keep_reopens_the_dismissal_of_the_card(self):
        """A dismissal said the notes are the opinion before's; a keep
        and a give back change that answer, so the new card is open."""
        dismissal = OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.SHARED_FOOTNOTES,
        )
        card = self.opinion.findings.create(
            page_in_opinion=0,
            check_name=OpinionCheck.SHARED_FOOTNOTES,
            severity=Issue.Severity.ERROR,
            dismissal=dismissal,
        )
        other = OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )

        shared_footnotes.keep(self.opinion, self.user)

        dismissal.refresh_from_db()
        card.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(dismissal.withdrawn_at)
        self.assertIsNone(card.dismissal_id)
        self.assertIsNone(other.withdrawn_at)

    def test_the_viewer_lifts_the_mask_of_a_kept_opinion(self):
        scan = self.opinion.scan
        shared_page(scan)
        _, after = two_opinions(scan)
        self.opinion.boundary = after
        self.opinion.start_source_page = 2
        self.opinion.save(update_fields=["boundary", "start_source_page"])

        def notes():
            payload = boundaries.viewer_payload(scan, {})
            row = next(p for p in payload if p["id"] == after.pk)
            return footnote_masks(row["outside_rects"])

        self.assertTrue(notes())
        shared_footnotes.keep(self.opinion, self.user)
        self.assertEqual(notes(), [])
        self.assertEqual(shared_footnotes.kept_boundaries(scan), {after.pk})


class TestTheEndpoints(ScanningTestCase):
    """The two writes of the card, and the button on the review page."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(page_count=3)
        shared_page(self.scan)
        self.before, self.after = two_opinions(self.scan)
        self.opinion = OpinionFactory(
            scan=self.scan,
            boundary=self.after,
            start_page_index=1,
            start_source_page=2,
            end_page_index=2,
            end_source_page=3,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
        kwargs = {"pk": self.scan.pk, "opinion_pk": self.opinion.pk}
        self.keep_url = reverse("keep_opinion_footnotes", kwargs=kwargs)
        self.give_back_url = reverse(
            "give_back_opinion_footnotes", kwargs=kwargs
        )

    def test_the_keep_and_the_give_back(self):
        response = self.client.post(self.keep_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(shared_footnotes.kept(self.opinion))

        response = self.client.post(self.give_back_url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(shared_footnotes.kept(self.opinion))

    def test_an_approval_under_the_lock_refuses(self):
        with patch.object(
            shared_footnotes,
            "keep",
            side_effect=shared_footnotes.FootnotesClosed("done"),
        ):
            response = self.client.post(self.keep_url)

        self.assertEqual(response.status_code, 409)

    def test_a_kept_row_on_a_page_nobody_shares_offers_no_give_back(self):
        shared_footnotes.keep(self.opinion, self.user)
        self.opinion.findings.create(
            page_in_opinion=0,
            check_name=OpinionCheck.SHARED_FOOTNOTES,
            severity=Issue.Severity.WARNING,
        )
        boundaries.dismiss(self.scan, self.before, self.user)

        response = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

        self.assertNotContains(response, self.give_back_url)

    def test_an_opinion_not_ready_refuses(self):
        self.opinion.status = OpinionReviewStatus.TEXT_REVIEW_DONE
        self.opinion.save(update_fields=["status"])

        response = self.client.post(self.keep_url)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(KeptFootnotes.objects.exists())

    def test_a_first_page_nobody_shares_refuses(self):
        self.opinion.boundary = self.before
        self.opinion.save(update_fields=["boundary"])

        response = self.client.post(self.keep_url)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(KeptFootnotes.objects.exists())

    def test_an_opinion_of_another_scan_is_a_404(self):
        other = ScanFactory()
        response = self.client.post(
            reverse(
                "keep_opinion_footnotes",
                kwargs={"pk": other.pk, "opinion_pk": self.opinion.pk},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_the_card_carries_the_button(self):
        self.opinion.findings.create(
            page_in_opinion=0,
            check_name=OpinionCheck.SHARED_FOOTNOTES,
            severity=Issue.Severity.WARNING,
            message="Taken.",
        )
        review = reverse("opinion_review", kwargs={"pk": self.opinion.pk})

        response = self.client.get(review)

        self.assertContains(response, f'data-footnotes-url="{self.keep_url}"')
        self.assertContains(response, "Keep the footnotes")

        shared_footnotes.keep(self.opinion, self.user)
        response = self.client.get(review)

        self.assertContains(
            response, f'data-footnotes-url="{self.give_back_url}"'
        )
        self.assertContains(response, "Give back")


class TestTheTakenZone(ScanningTestCase):
    """A footnote zone the mask took is not drawn as one (#457)."""

    ZONE = [94.9, 604.7, 514.6, 743.5]

    def test_a_zone_the_footnote_mask_covers_is_taken(self):
        mask = {
            "x0": 94.9,
            "y0": 604.7,
            "x1": 514.6,
            "y1": 743.5,
            "kind": boundaries.FOOTNOTES_MASK,
        }

        self.assertTrue(opinion_ocr._taken(self.ZONE, [mask]))

    def test_a_text_mask_takes_no_zone(self):
        mask = {"x0": 94.9, "y0": 604.7, "x1": 514.6, "y1": 743.5}

        self.assertFalse(opinion_ocr._taken(self.ZONE, [mask]))


class TestThePdfMasks(ScanningTestCase):
    """``opinion_pdf._masks`` carries the footnote mask into the PDF."""

    def setUp(self):
        self.user = self.make_user()
        self.scan = ScanFactory(page_count=3)
        shared_page(self.scan)
        _, after = two_opinions(self.scan)
        self.opinion = OpinionFactory(
            scan=self.scan,
            boundary=after,
            start_page_index=1,
            start_source_page=2,
            end_page_index=2,
            end_source_page=3,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
        self.volume = fitz.open()
        for _ in range(3):
            self.volume.new_page(width=612, height=792)

    def tearDown(self):
        self.volume.close()

    def test_the_mask_is_on_the_first_page_of_the_small_source(self):
        notes = footnote_masks(opinion_pdf._masks(self.opinion, self.volume))

        self.assertEqual([n["page_index"] for n in notes], [0])

    def test_a_kept_opinion_has_no_footnote_mask(self):
        shared_footnotes.keep(self.opinion, self.user)

        notes = footnote_masks(opinion_pdf._masks(self.opinion, self.volume))

        self.assertEqual(notes, [])


class TestTheEnsembleZones(ScanningTestCase):
    """The ensemble hides a taken zone and still sections by it."""

    ZONE = [40.0, 590.0, 580.0, 710.0]

    def test_a_taken_zone_is_not_drawn_and_its_notes_are_dropped(self):
        units = read_units(((50, 100, 300, 200), "own text")) + [
            unit(
                engine,
                1,
                (50, 600, 300, 700),
                "23. note of the opinion before",
                exclusion={"reason": opinion_ocr.NEIGHBOUR_FOOTNOTES},
                share=1.0,
            )
            for engine in ("dots_mocr", "mistral_ocr")
        ]
        pages = {}
        for engine in ("dots_mocr", "mistral_ocr"):
            page = engine_page(
                [u for u in units if u["engine"] == engine], [self.ZONE]
            )
            page["zones"][opinion_ocr.TAKEN_FOOTNOTES] = [self.ZONE]
            pages[engine] = page

        entry = ensemble.build_page(pages, 0)

        self.assertEqual(entry["zones"]["footnotes"], [])
        self.assertEqual(
            entry["zones"][opinion_ocr.TAKEN_FOOTNOTES], [self.ZONE]
        )
        self.assertEqual(entry["footnotes"], "")
        drop = next(
            d
            for d in entry["dropped"]
            if d["reason"] == opinion_ocr.NEIGHBOUR_FOOTNOTES
        )
        # The zone still sections the drop, so a body join is not cut.
        self.assertEqual(drop["section"], ensemble.FOOTNOTES)
