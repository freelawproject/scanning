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
        from django.core.cache import cache

        # The corpus counts of the cards page are cached for two
        # minutes, and one test's counts would be another's.
        cache.clear()
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


class TestTheFixesOfTheReview(CardTestCase):
    def test_a_word_and_a_gap_at_one_offset_both_land(self):
        """The word is replaced first, then the gap's words go in
        before it: never a cut into the inserted words."""
        gap = OpinionCard(
            pk=1,
            answer="very",
            card={"kind": "word", "gap": True, "start": 2, "length": 0},
        )
        word = OpinionCard(
            pk=2,
            answer="big",
            card={"kind": "word", "start": 2, "length": 3},
        )

        self.assertEqual(
            opinion_cards._all_answers("a dog sat", [gap, word]),
            "a very big sat",
        )
        self.assertEqual(
            opinion_cards._all_answers("a dog sat", [word, gap]),
            "a very big sat",
        )

    def test_send_back_keeps_the_answer_and_who_gave_it(self):
        opinion_cards.sync(self.opinion, document(self.held()))
        volunteer = self.make_user()
        opinion_cards.answer(self.row(), volunteer, "holds")
        staff = self.make_staff_user()
        self.client.force_login(staff)
        url = reverse(
            "send_back_opinion_card",
            kwargs={"pk": self.opinion.scan_id, "card_pk": self.row().pk},
        )

        answer = self.client.post(url)

        self.assertEqual(answer.json()["status"], "ok")
        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.STALE)
        self.assertEqual((row.answer, row.answered_by), ("holds", volunteer))
        self.assertIn(staff.username, row.note)
        listed = self.client.get(
            reverse("opinion_card_answers") + "?status=stale"
        )
        self.assertContains(listed, "holds")
        # A second press finds nothing waiting for the apply.
        self.assertEqual(self.client.post(url).status_code, 409)

    def test_send_back_is_for_staff(self):
        opinion_cards.sync(self.opinion, document(self.held()))
        opinion_cards.answer(self.row(), self.make_user(), "holds")
        self.client.force_login(self.make_user())

        answer = self.client.post(
            reverse(
                "send_back_opinion_card",
                kwargs={"pk": self.opinion.scan_id, "card_pk": self.row().pk},
            )
        )

        self.assertEqual(answer.status_code, 403)

    def test_the_open_filter_keeps_the_viewers_own_answers(self):
        other = blocking(
            read("a dog sat", "a dig sat", "a dug sat"),
            gid=4,
            box=(36, 200, 288, 220),
        )
        opinion_cards.sync(self.opinion, document(self.held(), other))
        viewer = self.make_user()
        rows = {r.card["token"]: r for r in OpinionCard.objects.all()}
        opinion_cards.answer(rows["held"], viewer, "holds")
        opinion_cards.answer(rows["dog"], self.make_user(), "dug")
        self.client.force_login(viewer)
        kwargs = {"pk": self.opinion.scan_id, "opinion_pk": self.opinion.pk}

        cards = self.client.get(
            reverse("opinion_card_rows", kwargs=kwargs)
        ).json()["cards"]

        # Their own answer, with its Undo; another volunteer's is no
        # longer open and is not theirs to undo.
        self.assertEqual(
            [(c["token"], c["stored"]) for c in cards], [("held", "holds")]
        )

    def test_the_sync_command_skips_an_opinion_a_build_stamped_since(self):
        from django.core.management import call_command

        from scanning.models import Opinion

        doc = document(self.held())

        def read_and_race(opinion):
            # A build stamps a newer document between the read and the
            # write of the rows.
            Opinion.objects.filter(pk=opinion.pk).update(
                ensemble_edit_revision=opinion.ensemble_edit_revision + 1
            )
            return doc

        with (
            patch("scanning.ensemble.is_written", return_value=True),
            patch(
                "scanning.ensemble.read_document", side_effect=read_and_race
            ),
        ):
            call_command("sync_opinion_cards", str(self.opinion.scan_id))

        self.assertFalse(OpinionCard.objects.exists())


class TestTheRowsScript(ScanningTestCase):
    def test_answered_rows_are_drawn_and_an_applied_one_has_no_undo(self):
        import pathlib
        import re

        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "static"
            / "scanning"
            / "blocking_review.js"
        ).read_text()
        render = re.search(
            r"Section\.prototype\.render = function(.*?)\n    \};",
            source,
            re.S,
        ).group(1)
        self.assertIn(
            "rowsMode && !this.cards.length && !this.answers.length", render
        )
        self.assertIn("!rowsMode && !this.cards.length", render)
        self.assertIn("undo: card.applied ? null", source)
        self.assertIn("if (answer.undo)", source)


class TestNotTextAndSetAside(CardTestCase):
    def setUp(self):
        super().setUp()
        opinion_cards.sync(self.opinion, document(self.held()))
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.url = reverse(
            "answer_opinion_card",
            kwargs={"pk": self.opinion.scan_id, "card_pk": self.row().pk},
        )

    def post(self, body):
        return self.client.post(
            self.url, json.dumps(body), content_type="application/json"
        )

    def test_not_text_is_a_drop_answer(self):
        answer = self.post({"drop": True})

        self.assertEqual(answer.json()["status"], "ok")
        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.ANSWERED)
        self.assertEqual(row.action, OpinionCard.Action.DROP)
        self.assertFalse(opinion_cards.is_keep(row))

    def test_the_apply_writes_a_drop_of_the_block(self):
        self.post({"drop": True})
        doc = document(self.held())
        with (
            patch("scanning.ensemble.read_document", return_value=doc),
            patch("scanning.ensemble.is_written", return_value=True),
            patch("scanning.ensemble.rerun", side_effect=self.settle),
        ):
            counts = opinion_cards.apply(self.opinion)

        edit = OpinionEdit.objects.get(opinion=self.opinion)
        self.assertEqual(edit.kind, OpinionEdit.Kind.DROP)
        self.assertEqual(edit.box_pt, doc["pages"][0]["groups"][0]["box_pt"])
        self.assertEqual(counts["drops"], 1)
        self.assertEqual(self.row().status, OpinionCard.Status.APPLIED)

    def settle(self, opinion):
        from django.db.models import F

        from scanning.models import Opinion

        Opinion.objects.filter(pk=opinion.pk).update(
            ensemble_edit_revision=F("edit_revision")
        )
        opinion_cards.sync(opinion, document())

    def test_a_rescan_files_a_repair_request_and_sets_the_card_aside(self):
        from scanning.models import PageRepairRequest

        answer = self.post({"flag": "rescan"})

        self.assertEqual(answer.json()["status"], "ok")
        repair = PageRepairRequest.objects.get(scan_id=self.opinion.scan_id)
        self.assertEqual(repair.action, PageRepairRequest.Action.REPLACE)
        # page_of's page sits on page 2 of the original.
        self.assertEqual(repair.pdf_page, 2)
        row = self.row()
        self.assertEqual(row.status, OpinionCard.Status.FLAGGED)
        self.assertEqual((row.flag, row.repair), ("rescan", repair))
        # A second card of the page answers the same request.
        self.post({"flag": "rescan"})
        self.assertEqual(PageRepairRequest.objects.count(), 1)
        # It waits in the repair queue like a review-1 request: the queue
        # lists it, and the header counts it.
        from scanning import repairs

        queue = self.client.get(reverse("repair_queue"))
        self.assertEqual(queue.status_code, 200)
        self.assertIn(
            repair.pk,
            [r.pk for g in queue.context["groups"] for r in g["requests"]],
        )
        self.assertEqual(repairs.waiting_count(), 1)

    def test_a_set_aside_card_holds_its_page(self):
        """No kept page around it is dismissed, the build keeps the
        status, and Undo opens it again."""
        self.post({"flag": "redaction"})
        opinion_cards.sync(self.opinion, document(self.held()))
        self.assertEqual(self.row().status, OpinionCard.Status.FLAGGED)

        self.post({"answer": None})

        row = self.row()
        self.assertEqual((row.status, row.flag), (OpinionCard.Status.OPEN, ""))

    def test_a_closed_card_files_no_rescan(self):
        """The card is applied between the page's load and the press:
        the flag is refused, and no repair work is left in the queue."""
        from scanning.models import PageRepairRequest

        row = self.row()
        with patch(
            "scanning.opinion_cards.set_aside",
            side_effect=opinion_cards.CardClosed(row.pk),
        ):
            answer = self.post({"flag": "rescan"})

        self.assertEqual(answer.status_code, 409)
        self.assertFalse(PageRepairRequest.objects.exists())

    def test_an_inserted_page_has_no_page_to_rescan(self):
        OpinionCard.objects.filter(pk=self.row().pk).update(
            card={**self.row().card, "address": [7, 1]}
        )

        answer = self.post({"flag": "rescan"})

        self.assertEqual(answer.status_code, 409)
        self.assertEqual(self.row().status, OpinionCard.Status.OPEN)

    def test_staff_see_a_set_aside_card(self):
        self.post({"flag": "redaction"})
        self.client.force_login(self.make_staff_user())

        answer = self.client.get(
            reverse("opinion_card_answers") + "?status=flagged"
        )

        self.assertContains(answer, "Redaction wrong")


class TestTheLevelFilter(CardTestCase):
    def setUp(self):
        super().setUp()
        comma = blocking(
            read(
                "the court held, that x",
                "the court held. that x",
                "the court held; that x",
            ),
            gid=4,
            box=(36, 200, 288, 220),
        )
        opinion_cards.sync(self.opinion, document(self.held(), comma))
        self.client.force_login(self.make_user())

    def test_the_rows_say_which_cards_are_minor(self):
        rows = {r.card["token"]: r for r in OpinionCard.objects.all()}

        self.assertFalse(rows["held"].minor)
        self.assertTrue(rows["held,"].minor)

    def test_the_page_counts_and_filters_by_level(self):
        url = reverse("opinion_card_review")

        everything = self.client.get(url)
        blocking = self.client.get(url + "?level=blocking")
        minor = self.client.get(url + "?level=minor")

        # Before the rebuild both block; one of them is minor and becomes
        # a minor warning at the next build.
        self.assertEqual(
            everything.context["levels"],
            {"blocking": 2, "pending": 1, "minor": 0},
        )
        self.assertEqual(everything.context["card_total"], 2)
        self.assertEqual(blocking.context["card_total"], 2)
        self.assertEqual(
            {t["slug"]: t["count"] for t in minor.context["tabs"]},
            {"": 1, "punctuation": 1},
        )


class TestMinorWarningCards(CardTestCase):
    """A block the minor rule made a warning keeps its open words as
    minor warning cards, apart from the warnings a majority settled."""

    def setUp(self):
        super().setUp()
        OpinionFindingFactory(
            opinion=self.opinion,
            check_name=OpinionCheck.ENGINES_DISAGREE,
            severity=Issue.Severity.WARNING,
        )
        self.user = self.make_user()

    def comma(self, level):
        group = read(
            "the court held, that x",
            "the court held. that x",
            "the court held; that x",
        )
        blocking(group, gid=4, box=(36, 200, 288, 220))
        group["level"] = level
        return group

    def settled(self):
        """Two engines read dug alike: a warning a majority settled."""
        group = read("a dug sat", "a dug sat", "a dig sat")
        blocking(group, gid=5, box=(36, 300, 288, 320))
        group["level"] = ensemble.WARNING
        return group

    def test_a_demoted_word_is_a_minor_warning_card(self):
        opinion_cards.sync(
            self.opinion,
            document(self.comma(ensemble.WARNING), self.settled()),
        )

        (row,) = OpinionCard.objects.all()
        self.assertEqual(row.card["token"], "held,")
        self.assertEqual(row.check_name, OpinionCheck.ENGINES_DISAGREE)
        self.assertTrue(row.minor)
        self.assertEqual(row.reason, "punctuation")

    def test_an_answer_outlives_the_rebuild_that_demotes_its_card(self):
        opinion_cards.sync(
            self.opinion, document(self.comma(ensemble.BLOCKING))
        )
        row = OpinionCard.objects.get()
        self.assertEqual(row.check_name, OpinionCheck.NO_MAJORITY)
        opinion_cards.answer(row, self.user, "held.")

        opinion_cards.sync(
            self.opinion, document(self.comma(ensemble.WARNING))
        )

        row = OpinionCard.objects.get()
        self.assertEqual(row.check_name, OpinionCheck.ENGINES_DISAGREE)
        self.assertEqual(
            (row.status, row.answer), (OpinionCard.Status.ANSWERED, "held.")
        )

    def test_a_kept_minor_card_closes_with_no_edit(self):
        opinion_cards.sync(
            self.opinion, document(self.comma(ensemble.WARNING))
        )
        opinion_cards.answer(OpinionCard.objects.get(), self.user, "held,")
        doc = document(self.comma(ensemble.WARNING))

        with (
            patch("scanning.ensemble.read_document", return_value=doc),
            patch("scanning.ensemble.is_written", return_value=True),
        ):
            counts = opinion_cards.apply(self.opinion)

        self.assertEqual(counts["minor_kept"], 1)
        self.assertFalse(OpinionEdit.objects.exists())
        self.assertEqual(
            OpinionCard.objects.get().status, OpinionCard.Status.APPLIED
        )

    def test_the_page_counts_minor_warnings_apart(self):
        opinion_cards.sync(
            self.opinion, document(self.held(), self.comma(ensemble.WARNING))
        )
        self.client.force_login(self.user)

        answer = self.client.get(reverse("opinion_card_review"))
        minor = self.client.get(
            reverse("opinion_card_review") + "?level=minor"
        )
        blocking = self.client.get(
            reverse("opinion_card_review") + "?level=blocking"
        )

        self.assertEqual(
            answer.context["levels"], {"blocking": 1, "pending": 0, "minor": 1}
        )
        self.assertEqual(minor.context["card_total"], 1)
        self.assertEqual(blocking.context["card_total"], 1)

    def test_a_minor_block_of_many_open_words_has_a_card_per_word(self):
        """Nine open words: a blocking block would be one whole-block
        card; a minor block keeps a card per word, so none is lost."""
        texts = [
            " ".join(f"w{i}{mark}" for i in range(9))
            for mark in (",", ".", ";")
        ]
        group = read(*texts)
        blocking(group, gid=6, box=(36, 400, 288, 460))
        group["level"] = ensemble.WARNING

        opinion_cards.sync(self.opinion, document(group))

        rows = OpinionCard.objects.all()
        self.assertEqual(rows.count(), 9)
        self.assertTrue(all(r.kind == "word" and r.minor for r in rows))

    def test_a_kept_minor_card_stays_closed_while_its_readings_hold(self):
        opinion_cards.sync(self.opinion, document(self.comma(ensemble.WARNING)))
        opinion_cards.answer(OpinionCard.objects.get(), self.user, "held,")
        doc = document(self.comma(ensemble.WARNING))
        with (
            patch("scanning.ensemble.read_document", return_value=doc),
            patch("scanning.ensemble.is_written", return_value=True),
        ):
            opinion_cards.apply(self.opinion)

        opinion_cards.sync(self.opinion, document(self.comma(ensemble.WARNING)))
        self.assertEqual(
            OpinionCard.objects.get().status, OpinionCard.Status.APPLIED
        )

        moved = read(
            "the court held, that x",
            "the court held. that x",
            "the court held: that x",
        )
        blocking(moved, gid=4, box=(36, 200, 288, 220))
        moved["level"] = ensemble.WARNING
        opinion_cards.sync(self.opinion, document(moved))
        row = OpinionCard.objects.get()
        self.assertEqual((row.status, row.answer), (OpinionCard.Status.OPEN, None))
