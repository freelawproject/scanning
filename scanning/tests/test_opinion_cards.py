"""The blocking cards as rows (``opinion_cards``, ``OpinionCard``)."""

import json
from unittest.mock import patch

from django.urls import reverse

from scanning import ensemble, opinion_cards
from scanning.factories import OpinionFactory, OpinionFindingFactory
from scanning.models import (
    Issue,
    OpinionCard,
    OpinionCheck,
    OpinionEdit,
    OpinionFinding,
    OpinionReviewStatus,
)
from scanning.tests.test_blocking_review import read
from scanning.tests.test_ensemble import document_of, page_of
from scanning.tests.test_views import ScanningTestCase


def blocking(group, gid=3, box=(36, 100, 288, 133)):
    group.update(
        {
            "id": gid,
            "level": ensemble.BLOCKING,
            "section": ensemble.BODY,
            "column": "L",
            "box_pt": list(box),
        }
    )
    return group


def document(*groups):
    return document_of(
        page_of(
            groups=list(groups),
            voted=len(groups),
            differing=len(groups),
            blocking=len(groups),
            no_majority=len(groups),
            low_confidence=len(groups),
        )
    )


class CardTestCase(ScanningTestCase):
    def setUp(self):
        self.opinion = OpinionFactory(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.finding = OpinionFindingFactory(
            opinion=self.opinion,
            check_name=OpinionCheck.NO_MAJORITY,
            severity=Issue.Severity.ERROR,
        )

    def held(self):
        """The court held, heId or hold: one open word, a slip."""
        return blocking(
            read(
                "the court held that",
                "the court heId that",
                "the court hold that",
            )
        )

    def row(self):
        return OpinionCard.objects.get(opinion=self.opinion)


class TestSync(CardTestCase):
    def test_a_card_of_an_open_page_is_a_row_with_its_reason(self):
        opinion_cards.sync(self.opinion, document(self.held()))

        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.OPEN)
        self.assertEqual(row.kind, "word")
        self.assertEqual(row.reason, "characters")
        self.assertEqual(row.check_name, OpinionCheck.NO_MAJORITY)
        self.assertEqual(row.card["token"], "held")

    def test_a_dismissed_page_has_no_row(self):
        self.finding.delete()

        opinion_cards.sync(self.opinion, document(self.held()))

        self.assertFalse(OpinionCard.objects.exists())

    def test_an_answer_outlives_a_rerun_with_the_same_readings(self):
        opinion_cards.sync(self.opinion, document(self.held()))
        opinion_cards.answer(self.row(), self.make_user(), "held")
        # A drop elsewhere renumbers the group: the key is the address.
        opinion_cards.sync(self.opinion, document(self.held() | {"id": 7}))

        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.ANSWERED)
        self.assertEqual(row.answer, "held")
        self.assertEqual(row.card["group_id"], 7)

    def test_an_answer_whose_readings_moved_is_stale(self):
        opinion_cards.sync(self.opinion, document(self.held()))
        opinion_cards.answer(self.row(), self.make_user(), "held")
        moved = blocking(
            read(
                "the court held that",
                "the court heId that",
                "the court hald that",
            )
        )

        opinion_cards.sync(self.opinion, document(moved))

        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.STALE)
        self.assertEqual(row.answer, "held")

    def test_a_settled_card_goes_and_an_applied_one_stays(self):
        """An applied row the build still shows opens again (its edit
        did not settle the place); one the build no longer shows is the
        record of what was written."""
        other = blocking(
            read("a dog sat", "a dig sat", "a dug sat"),
            gid=4,
            box=(36, 200, 288, 220),
        )
        opinion_cards.sync(self.opinion, document(self.held(), other))
        self.assertEqual(OpinionCard.objects.count(), 2)
        applied = OpinionCard.objects.get(card__token="held")
        OpinionCard.objects.filter(pk=applied.pk).update(
            status=OpinionCard.Status.APPLIED
        )

        # The engines now agree everywhere: no card.
        opinion_cards.sync(self.opinion, document())

        self.assertEqual(
            list(OpinionCard.objects.values_list("pk", flat=True)),
            [applied.pk],
        )


class TestAnswer(CardTestCase):
    def test_an_answer_is_stored_and_taken_back(self):
        opinion_cards.sync(self.opinion, document(self.held()))
        user = self.make_user()

        opinion_cards.answer(self.row(), user, "held")
        self.assertEqual(
            (self.row().status, self.row().answered_by),
            (OpinionCard.Status.ANSWERED, user),
        )
        opinion_cards.answer(self.row(), user, None)
        self.assertEqual(self.row().status, OpinionCard.Status.OPEN)
        self.assertIsNone(self.row().answer)

    def test_an_applied_card_takes_no_answer(self):
        opinion_cards.sync(self.opinion, document(self.held()))
        OpinionCard.objects.update(status=OpinionCard.Status.APPLIED)

        with self.assertRaises(opinion_cards.CardClosed):
            opinion_cards.answer(self.row(), self.make_user(), "held")

    def test_compose_is_the_review_pages_rule(self):
        word = {
            "kind": "word",
            "text": "the court heId that",
            "start": 10,
            "length": 4,
        }
        gap = {
            "kind": "word",
            "gap": True,
            "text": "a b",
            "start": 2,
            "length": 0,
        }
        block = {"kind": "block", "text": "x"}

        self.assertEqual(
            opinion_cards.compose(word, "held"), "the court held that"
        )
        self.assertEqual(opinion_cards.compose(gap, "z"), "a z b")
        self.assertEqual(opinion_cards.compose(gap, ""), "a b")
        self.assertEqual(opinion_cards.compose(block, " y  z "), "y z")


class TestApply(CardTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.doc = document(self.held())
        opinion_cards.sync(self.opinion, self.doc)
        for target, value in (
            ("scanning.ensemble.read_document", lambda o: self.doc),
            ("scanning.ensemble.is_written", lambda o: True),
            ("scanning.ensemble.rerun", self.rebuild),
        ):
            patcher = patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def rebuild(self, opinion):
        """The build after the edit: the word is settled, no card."""
        from django.db.models import F

        from scanning.models import Opinion

        Opinion.objects.filter(pk=opinion.pk).update(
            ensemble_edit_revision=F("edit_revision")
        )
        self.doc = document()
        opinion_cards.sync(opinion, self.doc)

    def test_a_changed_word_is_a_text_edit_of_its_block(self):
        opinion_cards.answer(self.row(), self.user, "holds")

        counts = opinion_cards.apply(self.opinion)

        edit = OpinionEdit.objects.get(opinion=self.opinion)
        self.assertEqual(edit.kind, OpinionEdit.Kind.TEXT)
        self.assertEqual(edit.text, "the court holds that")
        self.assertEqual(edit.base_text, "the court held that")
        # The only open word of the block: a whole-block judgement.
        self.assertIsNone(edit.span)
        self.assertEqual(edit.created_by, self.user)
        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.APPLIED)
        self.assertEqual(row.edit, edit)
        self.assertEqual((counts["edits"], counts["answers"]), (1, 1))

    def test_a_dry_run_writes_nothing(self):
        opinion_cards.answer(self.row(), self.user, "holds")

        counts = opinion_cards.apply(self.opinion, dry_run=True)

        self.assertEqual(counts["edits"], 1)
        self.assertFalse(OpinionEdit.objects.exists())
        self.assertEqual(self.row().status, OpinionCard.Status.ANSWERED)

    def test_a_page_kept_as_shown_is_dismissed(self):
        opinion_cards.answer(self.row(), self.user, "held")

        counts = opinion_cards.apply(self.opinion)

        self.assertFalse(OpinionEdit.objects.exists())
        self.assertEqual(counts["pages_dismissed"], 1)
        self.assertIsNotNone(
            OpinionFinding.objects.get(pk=self.finding.pk).dismissal
        )
        self.assertEqual(self.row().status, OpinionCard.Status.APPLIED)

    def test_a_block_the_page_would_refuse_is_stale(self):
        opinion_cards.answer(self.row(), self.user, "holds")
        self.doc["pages"][0]["groups"][0]["level"] = None

        counts = opinion_cards.apply(self.opinion)

        self.assertEqual(counts["stale"], 1)
        self.assertEqual(self.row().status, OpinionCard.Status.STALE)
        self.assertIn("alike", self.row().note)


class TestThePage(CardTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())
        opinion_cards.sync(self.opinion, document(self.held()))

    def test_the_page_walks_a_reason(self):
        answer = self.client.get(
            reverse("opinion_card_review") + "?reason=characters"
        )

        self.assertEqual(answer.status_code, 200)
        self.assertContains(answer, f'id="opinion-{self.opinion.pk}"')
        self.assertContains(answer, 'data-mode="rows"')
        tabs = {t["slug"]: t["count"] for t in answer.context["tabs"]}
        self.assertEqual(tabs, {"": 1, "characters": 1})

        other = self.client.get(
            reverse("opinion_card_review") + "?reason=name"
        )
        self.assertNotContains(other, f'id="opinion-{self.opinion.pk}"')

    def test_the_rows_of_an_opinion(self):
        kwargs = {"pk": self.opinion.scan_id, "opinion_pk": self.opinion.pk}

        answer = self.client.get(
            reverse("opinion_card_rows", kwargs=kwargs) + "?reason=characters"
        ).json()

        (card,) = answer["cards"]
        self.assertEqual(card["token"], "held")
        self.assertEqual(card["reason_label"], "Characters differ in a word")
        self.assertIsNone(card["stored"])

    def test_an_answer_is_stored_on_the_row(self):
        row = self.row()
        url = reverse(
            "answer_opinion_card",
            kwargs={"pk": self.opinion.scan_id, "card_pk": row.pk},
        )

        answer = self.client.post(
            url,
            json.dumps({"answer": "held"}),
            content_type="application/json",
        )

        self.assertEqual(answer.json()["status"], "ok")
        self.assertEqual(self.row().answer, "held")
        self.assertFalse(OpinionEdit.objects.exists())

        OpinionCard.objects.update(status=OpinionCard.Status.APPLIED)
        refused = self.client.post(
            url, json.dumps({"answer": "x"}), content_type="application/json"
        )
        self.assertEqual(refused.status_code, 409)

    def test_a_bad_body_is_refused(self):
        url = reverse(
            "answer_opinion_card",
            kwargs={"pk": self.opinion.scan_id, "card_pk": self.row().pk},
        )

        answer = self.client.post(url, "[]", content_type="application/json")

        self.assertEqual(answer.status_code, 400)


class TestApplyWithTheEnsemble(ScanningTestCase):
    """The apply over the real build: each round's edit lands in
    ``ensemble.build_page``, the findings and the rows are written from
    the page it builds, and the next round reads that page."""

    def setUp(self):
        from scanning.tests.test_ensemble import BODY_A_PT, engine_page, unit

        self.opinion = OpinionFactory(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.user = self.make_user()
        self.texts = {
            "dots_mocr": "a dog sat on a mat",
            "mistral_ocr": "a dig sat on a mot",
            "surya": "a dug sat on a met",
        }
        self.pages = {
            name: engine_page([unit(name, 0, BODY_A_PT, text)])
            for name, text in self.texts.items()
        }
        for target, value in (
            ("scanning.ensemble.read_document", lambda o: self.doc),
            ("scanning.ensemble.is_written", lambda o: True),
            ("scanning.ensemble.rerun", self.build),
        ):
            patcher = patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.build(self.opinion)

    def build(self, opinion):
        """``ensemble.write`` without the bucket: the page, the findings,
        the rows, the stamp."""
        from django.db.models import F

        from scanning.models import Opinion

        page = ensemble.build_page(
            self.pages, 0, edits=ensemble.edit_entries(opinion)
        )
        self.doc = {
            "schema_version": ensemble.SCHEMA_VERSION,
            "engines": list(self.texts),
            "pages": [page],
        }
        ensemble.rebuild_findings(opinion, self.doc)
        opinion_cards.sync(opinion, self.doc)
        Opinion.objects.filter(pk=opinion.pk).update(
            ensemble_edit_revision=F("edit_revision")
        )

    def open_rows(self):
        return {
            r.card["token"]: r
            for r in OpinionCard.objects.filter(
                opinion=self.opinion, status__in=opinion_cards.LIVE
            )
        }

    def text(self):
        return self.doc["pages"][0]["groups"][0]["text"]

    def test_the_answers_of_a_whole_block_are_one_edit(self):
        rows = self.open_rows()
        self.assertEqual(set(rows), {"dog", "mat"})
        opinion_cards.answer(rows["dog"], self.user, "dug")
        opinion_cards.answer(rows["mat"], self.user, "mat")

        counts = opinion_cards.apply(self.opinion)

        self.assertEqual(counts["edits"], 1)
        self.assertEqual(self.text(), "a dug sat on a mat")
        self.assertEqual(self.open_rows(), {})
        self.assertFalse(
            OpinionFinding.objects.filter(
                opinion=self.opinion,
                check_name=OpinionCheck.NO_MAJORITY,
            ).exists()
        )

    def test_a_block_answered_in_part_takes_its_word_and_waits(self):
        """One word answered: an edit of its span; the other word is
        still an open card, placed in the person's text, and its answer
        later is a second edit over the first."""
        opinion_cards.answer(self.open_rows()["dog"], self.user, "dug")

        opinion_cards.apply(self.opinion)

        self.assertEqual(self.text(), "a dug sat on a mat")
        left = self.open_rows()
        self.assertEqual(set(left), {"mat"})
        card = left["mat"].card
        self.assertEqual(
            self.text()[card["start"] : card["start"] + card["length"]], "mat"
        )

        opinion_cards.answer(left["mat"], self.user, "met")
        opinion_cards.apply(self.opinion)

        self.assertEqual(self.text(), "a dug sat on a met")
        self.assertEqual(self.open_rows(), {})
        self.assertEqual(
            OpinionEdit.objects.filter(
                opinion=self.opinion, withdrawn_at__isnull=True
            ).count(),
            1,
        )

    def test_a_page_kept_whole_is_dismissed_and_its_text_stays(self):
        for token, row in self.open_rows().items():
            opinion_cards.answer(row, self.user, token)

        counts = opinion_cards.apply(self.opinion)

        self.assertEqual(counts["pages_dismissed"], 1)
        self.assertEqual(self.text(), "a dog sat on a mat")
        self.assertFalse(OpinionEdit.objects.exists())
        self.assertIsNotNone(
            OpinionFinding.objects.get(
                opinion=self.opinion, check_name=OpinionCheck.NO_MAJORITY
            ).dismissal
        )


class TestTheAnswersPage(CardTestCase):
    def setUp(self):
        super().setUp()
        opinion_cards.sync(self.opinion, document(self.held()))
        self.volunteer = self.make_user()
        opinion_cards.answer(self.row(), self.volunteer, "holds")

    def test_staff_see_who_answered_what(self):
        self.client.force_login(self.make_staff_user())

        answer = self.client.get(reverse("opinion_card_answers"))

        self.assertEqual(answer.status_code, 200)
        self.assertContains(answer, self.volunteer.username)
        self.assertContains(answer, "holds")
        self.assertContains(answer, 'data-role="send-back"')

    def test_a_filter_by_volunteer(self):
        self.client.force_login(self.make_staff_user())

        answer = self.client.get(
            reverse("opinion_card_answers") + "?user=nobody"
        )

        self.assertNotContains(answer, "holds")

    def test_a_volunteer_cannot_open_it(self):
        self.client.force_login(self.make_user())

        answer = self.client.get(reverse("opinion_card_answers"))

        self.assertEqual(answer.status_code, 403)
