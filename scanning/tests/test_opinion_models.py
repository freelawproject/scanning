"""Tests for the opinion models of the third review (issue #335).

The models alone: the identity, the constraints, the derived keys and
the cascades. Nothing writes these rows yet, so there is no behaviour to
test here. The creation is #336 and the review is #334.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from scanning.factories import (
    ExternalJobFactory,
    OpinionFactory,
    OpinionFindingFactory,
    OpinionTextFactory,
    ScanFactory,
    UserFactory,
    WithdrawnOpinionFactory,
)
from scanning.models import (
    DISMISSABLE_OPINION_CHECKS,
    STALE_OPINION_CHECKS,
    ExternalJob,
    JobEngine,
    JobStage,
    Opinion,
    OpinionCheck,
    OpinionFinding,
    OpinionFindingDismissal,
    OpinionReviewStatus,
    OpinionText,
)


class TestOpinionIdentity(TestCase):
    """The key is the printed page plus the index inside that page."""

    def test_two_opinions_may_start_on_one_printed_page(self):
        scan = ScanFactory()
        first = OpinionFactory(
            scan=scan, first_printed_page=42, index_in_page=0
        )
        second = OpinionFactory(
            scan=scan, first_printed_page=42, index_in_page=1
        )
        self.assertNotEqual(first.pk, second.pk)

    def test_the_same_key_twice_is_refused(self):
        scan = ScanFactory()
        OpinionFactory(scan=scan, first_printed_page=42, index_in_page=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpinionFactory(
                    scan=scan, first_printed_page=42, index_in_page=0
                )

    def test_two_scans_may_hold_the_same_key(self):
        OpinionFactory(scan=ScanFactory(), first_printed_page=42)
        OpinionFactory(scan=ScanFactory(), first_printed_page=42)
        self.assertEqual(
            Opinion.objects.filter(first_printed_page=42).count(), 2
        )

    def test_the_last_printed_page_may_not_precede_the_first(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpinionFactory(first_printed_page=50, last_printed_page=49)

    def test_one_printed_page_is_a_legal_span(self):
        row = OpinionFactory(first_printed_page=50, last_printed_page=50)
        self.assertEqual(row.last_printed_page, 50)


class TestOpinionFields(TestCase):
    """The status, the glue prefix and the two addresses."""

    def test_a_new_row_is_processing(self):
        self.assertEqual(
            OpinionFactory().status, OpinionReviewStatus.PROCESSING
        )

    def test_the_glue_prefix_follows_the_revision(self):
        row = OpinionFactory()
        self.assertEqual(row.glue_prefix, f"jobs/opinions/o{row.pk}/r0/")
        row.glue_revision = 3
        self.assertEqual(row.glue_prefix, f"jobs/opinions/o{row.pk}/r3/")

    def test_the_approved_text_key_is_not_under_the_glue_prefix(self):
        """A re-glue raises the revision and must not reach the text."""
        row = OpinionFactory(approved_text_key="opinions/o1/approved.txt")
        row.glue_revision = 9
        self.assertNotIn(row.glue_prefix, row.approved_text_key)

    def test_the_address_survives_with_no_apply_run(self):
        row = OpinionFactory(apply_run=None)
        self.assertIsNone(row.apply_run)
        self.assertEqual(row.start_source_page, 1)
        self.assertEqual(row.end_source_page, 2)

    def test_deleting_the_scan_takes_the_opinion(self):
        scan = ScanFactory()
        OpinionFactory(scan=scan)
        scan.delete()
        self.assertFalse(Opinion.objects.exists())


class TestOpinionText(TestCase):
    """One row per page, and the human text is what a reader shows."""

    def test_the_same_page_twice_is_refused(self):
        opinion = OpinionFactory()
        OpinionTextFactory(opinion=opinion, page_in_opinion=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpinionTextFactory(opinion=opinion, page_in_opinion=0)

    def test_the_agreed_page_carries_no_disagreement(self):
        self.assertEqual(OpinionTextFactory().disagreements, [])

    def test_a_disagreement_names_its_offsets_and_variants(self):
        row = OpinionTextFactory(
            disagreements=[
                {
                    "start": 4,
                    "end": 11,
                    "variants": {"dots_mocr": "opinion", "surya": "opimon"},
                }
            ]
        )
        row.refresh_from_db()
        self.assertEqual(row.disagreements[0]["variants"]["surya"], "opimon")

    def test_the_human_text_wins(self):
        row = OpinionTextFactory(text="The opimon of the court.")
        self.assertEqual(row.current_text, "The opimon of the court.")
        row.human_text = "The opinion of the court."
        self.assertEqual(row.current_text, "The opinion of the court.")

    def test_deleting_the_opinion_takes_its_pages(self):
        opinion = OpinionFactory()
        OpinionTextFactory(opinion=opinion)
        opinion.delete()
        self.assertFalse(OpinionText.objects.exists())


class TestWithdrawnOpinion(TestCase):
    """A range the book says holds no opinion."""

    def test_a_row_is_withdrawn_not_deleted(self):
        row = WithdrawnOpinionFactory()
        row.withdrawn_at = timezone.now()
        row.save(update_fields=["withdrawn_at"])
        row.refresh_from_db()
        self.assertIsNotNone(row.withdrawn_at)

    def test_the_last_printed_page_may_not_precede_the_first(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WithdrawnOpinionFactory(
                    first_printed_page=50, last_printed_page=49
                )

    def test_two_rows_may_name_one_range(self):
        """No unique key: a curator answers a gap card, not a model."""
        scan = ScanFactory()
        WithdrawnOpinionFactory(scan=scan, first_printed_page=7)
        WithdrawnOpinionFactory(scan=scan, first_printed_page=7)
        self.assertEqual(scan.withdrawn_opinions.count(), 2)


class TestOpinionFinding(TestCase):
    """The warnings, and the dismissals that mute them."""

    def test_a_stale_check_is_not_dismissable(self):
        for check in STALE_OPINION_CHECKS:
            self.assertNotIn(check, DISMISSABLE_OPINION_CHECKS)

    def test_every_other_check_is_dismissable(self):
        self.assertEqual(
            DISMISSABLE_OPINION_CHECKS,
            frozenset(OpinionCheck) - STALE_OPINION_CHECKS,
        )

    def test_is_stale_reads_the_check(self):
        self.assertFalse(OpinionFindingFactory().is_stale)
        self.assertTrue(
            OpinionFindingFactory(
                check_name=OpinionCheck.STALE_PAGE_NUMBER
            ).is_stale
        )

    def test_a_finding_starts_undismissed(self):
        self.assertIsNone(OpinionFindingFactory().dismissal)

    def test_one_standing_dismissal_per_page_and_check(self):
        opinion = OpinionFactory()
        OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpinionFindingDismissal.objects.create(
                    opinion=opinion,
                    page_in_opinion=0,
                    check_name=OpinionCheck.ENGINES_DISAGREE,
                )

    def test_a_withdrawn_dismissal_lets_a_second_in(self):
        opinion = OpinionFactory()
        first = OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        first.withdrawn_at = timezone.now()
        first.save(update_fields=["withdrawn_at"])
        second = OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        self.assertNotEqual(first.pk, second.pk)

    def test_one_standing_dismissal_for_the_whole_opinion(self):
        """The second key: Postgres counts two NULL pages as different."""
        opinion = OpinionFactory()
        OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.PAGE_GAP,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpinionFindingDismissal.objects.create(
                    opinion=opinion,
                    page_in_opinion=None,
                    check_name=OpinionCheck.PAGE_GAP,
                )

    def test_a_page_dismissal_and_an_opinion_dismissal_coexist(self):
        opinion = OpinionFactory()
        OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.PAGE_GAP,
        )
        OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.PAGE_GAP,
        )
        self.assertEqual(opinion.finding_dismissals.count(), 2)

    def test_withdrawing_a_dismissal_leaves_the_finding(self):
        """SET_NULL: nothing deletes a finding when a dismissal goes."""
        opinion = OpinionFactory()
        dismissal = OpinionFindingDismissal.objects.create(
            opinion=opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        finding = OpinionFindingFactory(opinion=opinion, dismissal=dismissal)
        dismissal.delete()
        finding.refresh_from_db()
        self.assertIsNone(finding.dismissal)

    def test_deleting_the_opinion_takes_its_findings(self):
        opinion = OpinionFactory()
        OpinionFindingFactory(opinion=opinion)
        opinion.delete()
        self.assertFalse(OpinionFinding.objects.exists())


class TestJobRowsPointAtTheNewModel(TestCase):
    """``ExternalJob.opinion`` is an ``Opinion`` since #335."""

    def test_the_field_names_the_new_model(self):
        self.assertIs(
            ExternalJob._meta.get_field("opinion").related_model, Opinion
        )

    def test_an_opinion_level_row_takes_an_opinion(self):
        scan = ScanFactory()
        opinion = OpinionFactory(scan=scan)
        job = ExternalJobFactory(
            scan=scan,
            opinion=opinion,
            stage=JobStage.TIEBREAK,
            engine=JobEngine.DOTS_MOCR,
        )
        self.assertEqual(job.opinion_id, opinion.pk)

    def test_an_opinion_level_row_without_one_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(
                    opinion=None,
                    stage=JobStage.TIEBREAK,
                    engine=JobEngine.DOTS_MOCR,
                )

    def test_deleting_the_opinion_takes_its_jobs(self):
        scan = ScanFactory()
        opinion = OpinionFactory(scan=scan)
        ExternalJobFactory(
            scan=scan,
            opinion=opinion,
            stage=JobStage.TIEBREAK,
            engine=JobEngine.DOTS_MOCR,
        )
        opinion.delete()
        self.assertFalse(
            ExternalJob.objects.filter(stage=JobStage.TIEBREAK).exists()
        )


class TestTheLegacyRowsAreSeparate(TestCase):
    """``OpinionScan`` keeps its rows under its own accessor."""

    def test_the_two_accessors_are_different(self):
        scan = ScanFactory()
        OpinionFactory(scan=scan)
        self.assertEqual(scan.opinions.count(), 1)
        self.assertEqual(scan.legacy_opinions.count(), 0)

    def test_a_user_who_approves_is_recorded(self):
        user = UserFactory()
        row = OpinionFactory(approved_by=user, approved_at=timezone.now())
        self.assertEqual(row.approved_by, user)
        self.assertIn(row, user.approved_opinions.all())
