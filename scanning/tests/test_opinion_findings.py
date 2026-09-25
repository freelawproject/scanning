"""Tests for the dismissal of a review-3 finding (issue #419).

A dismissal is its own row at the address of the card (the opinion,
the page and the check), and nothing deletes it. The endpoint refuses a
stale card, which is a fact about the row, and every opinion that is
not ready for the text review.
"""

from django.urls import reverse

from scanning import ensemble
from scanning.factories import (
    OpinionFactory,
    OpinionFindingFactory,
    ScanFactory,
)
from scanning.models import (
    Issue,
    OpinionCheck,
    OpinionFinding,
    OpinionFindingDismissal,
    OpinionReviewStatus,
)
from scanning.tests.test_ensemble import document_of, page_of
from scanning.tests.test_views import ScanningTestCase


class OpinionFindingTestCase(ScanningTestCase):
    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory()
        self.opinion = OpinionFactory(
            scan=self.scan,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
        self.finding = OpinionFindingFactory(
            opinion=self.opinion,
            check_name=OpinionCheck.SINGLE_ENGINE,
            severity=Issue.Severity.ERROR,
        )

    def address(self, name, finding=None, opinion=None, scan=None):
        finding = finding or self.finding
        opinion = opinion or self.opinion
        return reverse(
            name,
            kwargs={
                "pk": (scan or opinion.scan).pk,
                "opinion_pk": opinion.pk,
                "finding_pk": finding.pk,
            },
        )

    def dismiss(self, **names):
        return self.client.post(
            self.address("dismiss_opinion_finding", **names)
        )

    def restore(self, **names):
        return self.client.post(
            self.address("restore_opinion_finding", **names)
        )


class TestTheDismissal(OpinionFindingTestCase):
    def test_the_login_is_required(self):
        self.client.logout()

        response = self.dismiss()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OpinionFindingDismissal.objects.exists())

    def test_a_get_is_refused(self):
        response = self.client.get(self.address("dismiss_opinion_finding"))

        self.assertEqual(response.status_code, 405)

    def test_a_dismissal_is_a_row_and_mutes_the_card(self):
        response = self.dismiss()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["message"])
        row = OpinionFindingDismissal.objects.get()
        self.assertEqual(row.opinion, self.opinion)
        self.assertEqual(row.page_in_opinion, 0)
        self.assertEqual(row.check_name, OpinionCheck.SINGLE_ENGINE)
        self.assertEqual(row.dismissed_by, self.user)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.dismissal, row)

    def test_a_second_dismissal_writes_nothing(self):
        self.dismiss()

        response = self.dismiss()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpinionFindingDismissal.objects.count(), 1)

    def test_the_rebuild_keeps_the_dismissal(self):
        """The card is written again at every rebuild, and the row at
        its address mutes the new one."""
        self.dismiss()
        page = page_of(
            groups=[
                {
                    "id": 0,
                    "agreement": ensemble.SINGLE,
                    "level": ensemble.BLOCKING,
                    "source": "mistral_ocr",
                }
            ],
            single_engine=1,
        )

        ensemble.rebuild_findings(self.opinion, document_of(page))

        card = OpinionFinding.objects.get(opinion=self.opinion)
        self.assertEqual(card.check_name, OpinionCheck.SINGLE_ENGINE)
        self.assertIsNotNone(card.dismissal_id)

    def test_a_stale_card_is_refused(self):
        stale = OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.ORPHANED_OPINION,
        )

        response = self.dismiss(finding=stale)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.assertFalse(OpinionFindingDismissal.objects.exists())

    def test_an_opinion_not_ready_is_refused(self):
        for status in (
            OpinionReviewStatus.PROCESSING,
            OpinionReviewStatus.TEXT_REVIEW_DONE,
            OpinionReviewStatus.ERROR,
        ):
            with self.subTest(status=status):
                self.opinion.status = status
                self.opinion.save(update_fields=["status"])

                response = self.dismiss()

                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.json()["message"])
        self.assertFalse(OpinionFindingDismissal.objects.exists())

    def test_a_finding_of_another_opinion_is_a_404(self):
        other = OpinionFactory(
            scan=self.scan,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )

        response = self.dismiss(opinion=other)

        self.assertEqual(response.status_code, 404)

    def test_an_opinion_of_another_scan_is_a_404(self):
        response = self.dismiss(scan=ScanFactory())

        self.assertEqual(response.status_code, 404)


class TestTheRestore(OpinionFindingTestCase):
    def test_a_restore_withdraws_the_row_and_keeps_it(self):
        self.dismiss()

        response = self.restore()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        row = OpinionFindingDismissal.objects.get()
        self.assertIsNotNone(row.withdrawn_at)
        self.finding.refresh_from_db()
        self.assertIsNone(self.finding.dismissal_id)

    def test_a_restore_of_an_open_card_changes_nothing(self):
        response = self.restore()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["message"])
        self.assertFalse(OpinionFindingDismissal.objects.exists())

    def test_a_card_can_be_dismissed_again(self):
        self.dismiss()
        self.restore()

        self.dismiss()

        self.assertEqual(OpinionFindingDismissal.objects.count(), 2)
        self.assertEqual(
            OpinionFindingDismissal.objects.filter(
                withdrawn_at__isnull=True
            ).count(),
            1,
        )

    def test_an_opinion_not_ready_is_refused(self):
        self.dismiss()
        self.opinion.status = OpinionReviewStatus.TEXT_REVIEW_DONE
        self.opinion.save(update_fields=["status"])

        response = self.restore()

        self.assertEqual(response.status_code, 409)
        self.assertIsNone(OpinionFindingDismissal.objects.get().withdrawn_at)
