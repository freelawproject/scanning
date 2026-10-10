"""The blocking review: the cards, the crop estimate, the two views."""

from types import SimpleNamespace

from django.test import TestCase
from django.urls import reverse

from scanning import blocking_review, ensemble
from scanning.factories import (
    OpinionFactory,
    OpinionFindingFactory,
    ScanFactory,
)
from scanning.models import Issue, OpinionCheck, OpinionReviewStatus
from scanning.tests.test_ensemble import BODY_A_PT, group_of, unit
from scanning.tests.test_opinion_edits import EditTestCase
from scanning.tests.test_views import ScanningTestCase

ENGINES = ("dots_mocr", "mistral_ocr", "surya")


def read(*texts) -> dict:
    """One aligned group of one reading per engine, resolved."""
    group = group_of(
        *[unit(ENGINES[i], 0, BODY_A_PT, text) for i, text in enumerate(texts)]
    )
    group.update(ensemble.resolve(group))
    return group


# ── the open words ───────────────────────────────────────────────────
class TestOpenWords(TestCase):
    def test_the_word_no_two_engines_agree_on_with_every_reading(self):
        words = blocking_review.open_words(
            read(
                "the court held that",
                "the court heId that",
                "the court hold that",
            )
        )

        self.assertEqual(len(words), 1)
        word = words[0]
        self.assertEqual(
            (word["token"], word["start"], word["length"]), ("held", 10, 4)
        )
        self.assertEqual(
            [r["engine"] for r in word["readings"]], list(ENGINES)
        )
        self.assertEqual(
            [r["word"] for r in word["readings"]], ["held", "heId", "hold"]
        )
        self.assertEqual(
            (word["before"], word["after"]), ("the court", "that")
        )

    def test_a_block_a_majority_settled_has_no_open_word(self):
        self.assertEqual(
            blocking_review.open_words(read("a b c", "a b c", "a x c")), []
        )

    def test_an_engine_that_read_nothing_at_the_word_says_so(self):
        words = blocking_review.open_words(read("a b c", "a c", "a d c"))

        self.assertEqual(len(words), 1)
        self.assertEqual(
            [r["word"] for r in words[0]["readings"]],
            ["b", blocking_review.READ_NOTHING, "d"],
        )

    def test_each_word_of_an_inserted_run_is_its_own_card(self):
        """Two engines read two words the base did not. The run goes in
        as two low-confidence tokens, and each card shows the engine's
        word at that place of the run, never the run whole: the answer
        goes into one word's place of the text."""
        words = blocking_review.open_words(
            # The third reading differs at the end too, or two readings
            # alike would be a majority and no vote.
            read(
                "the court held today",
                "the court plainly and held today",
                "the court plainly and held todey",
            )
        )

        self.assertEqual([w["token"] for w in words], ["plainly", "and"])
        self.assertEqual(
            [[r["word"] for r in w["readings"]] for w in words],
            [
                [blocking_review.READ_NOTHING, "plainly", "plainly"],
                [blocking_review.READ_NOTHING, "and", "and"],
            ],
        )
        self.assertEqual([w["start"] for w in words], [10, 18])

    def test_a_span_the_engines_joined_is_one_card_over_the_span(self):
        """dots.mocr writes ``Ill. Adm. Code`` as three words and the
        others as one, and Surya glues the section number on too. The
        vote aligns them at ``Ill.``, where no two engines agree. One
        card covers the four words, with each engine's reading of
        them, so the answer replaces the span and never one word."""
        words = blocking_review.open_words(
            read(
                "under 20 Ill. Adm. Code 501,30(a) which",
                "under 20 Ill.Adm.Code 501,30(a) which",
                "under 20 Ill.Adm.Code501,30(a) which",
            )
        )

        self.assertEqual(len(words), 1)
        word = words[0]
        self.assertEqual(word["token"], "Ill. Adm. Code 501,30(a)")
        self.assertEqual((word["start"], word["length"]), (9, 24))
        self.assertEqual(
            [r["word"] for r in word["readings"]],
            [
                "Ill. Adm. Code 501,30(a)",
                "Ill.Adm.Code 501,30(a)",
                "Ill.Adm.Code501,30(a)",
            ],
        )
        self.assertEqual(
            (word["before"], word["after"]), ("under 20", "which")
        )

    def test_a_block_with_two_open_words_has_two_cards_in_order(self):
        words = blocking_review.open_words(
            read(
                "one two three four",
                "one tvo three fuor",
                "one twq three foor",
            )
        )

        self.assertEqual([w["token"] for w in words], ["two", "four"])
        self.assertEqual([w["start"] for w in words], [4, 14])


# ── the crop ─────────────────────────────────────────────────────────
class TestCropOf(TestCase):
    def test_the_line_of_a_word_from_its_place_in_the_block(self):
        """Three lines of thirty characters, 33 points tall: the word at
        40 is a third into the second line."""
        where = blocking_review.crop_of([100, 200, 300, 233], 90, 40, 4)

        self.assertEqual((where["line"], where["lines"]), (2, 3))
        self.assertEqual(where["crop"], [94, 200, 306, 233])
        self.assertEqual(where["highlight"][1], 211)
        self.assertEqual(where["highlight"][3], 222)
        self.assertAlmostEqual(
            where["highlight"][0],
            100 + 200 * ((40 - 30) / 30 - blocking_review.HIGHLIGHT_SLACK),
            places=1,
        )

    def test_a_long_block_crops_to_the_line_and_its_neighbours(self):
        where = blocking_review.crop_of([0, 0, 200, 110], 1000, 500, 5)

        self.assertEqual(where["line"], 6)
        self.assertEqual(where["crop"][1], 44)
        self.assertEqual(where["crop"][3], 77)

    def test_a_word_that_wraps_takes_the_rest_of_its_line(self):
        where = blocking_review.crop_of([0, 0, 200, 22], 100, 48, 6)

        self.assertEqual(where["highlight"][2], 200)
        self.assertEqual(where["highlight"][3], 22)

    def test_a_whole_block_has_no_highlight(self):
        where = blocking_review.block_crop([10, 20, 110, 53])

        self.assertIsNone(where["highlight"])
        self.assertEqual(where["crop"], [4, 20, 116, 53])
        self.assertEqual(where["lines"], 3)


# ── the cards ────────────────────────────────────────────────────────
def finding(page, check, pk):
    """The shape of one open finding the cards read."""
    return SimpleNamespace(
        page_in_opinion=page,
        check_name=check,
        pk=pk,
        message="the message",
        get_check_name_display=lambda: OpinionCheck(check).label,
    )


class TestCards(TestCase):
    def test_a_word_a_single_block_and_a_finding_of_another_check(self):
        split = read(
            "the court held that", "the court heId that", "the court hold that"
        )
        split.update(
            {
                "id": 3,
                "level": ensemble.BLOCKING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 133],
            }
        )
        alone = group_of(unit("mistral_ocr", 0, BODY_A_PT, "alone"))
        alone.update(ensemble.resolve(alone))
        alone.update(
            {
                "id": 4,
                "level": ensemble.BLOCKING,
                "section": ensemble.FOOTNOTES,
                "column": "R",
                "box_pt": [320, 600, 560, 622],
            }
        )
        settled = read("a b c", "a b c", "a x c")
        settled.update(
            {
                "id": 5,
                "level": ensemble.WARNING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 140, 288, 151],
            }
        )
        document = {
            "pages": [
                {"page_in_opinion": 0, "groups": [split, alone, settled]},
                {"page_in_opinion": 1, "groups": []},
            ]
        }
        findings = [
            finding(0, OpinionCheck.NO_MAJORITY, 11),
            finding(0, OpinionCheck.SINGLE_ENGINE, 12),
            finding(1, OpinionCheck.PAGE_NOT_READ, 13),
        ]

        cards = blocking_review.cards(document, findings)

        self.assertEqual(
            [c["kind"] for c in cards], ["word", "single", "link"]
        )
        word, single, link = cards
        self.assertEqual(word["where"], "page 1, body, left column")
        self.assertEqual((word["group_id"], word["finding_pk"]), (3, 11))
        self.assertEqual(word["token"], "held")
        # Nineteen characters over three lines: "held" at 10 is on
        # the second by the estimate.
        self.assertEqual(word["crop"]["line"], 2)
        self.assertEqual(single["where"], "page 1, footnote, right column")
        self.assertFalse(single["table"])
        self.assertEqual(
            (single["engine"], single["finding_pk"]), ("mistral_ocr", 12)
        )
        self.assertIsNone(single["crop"]["highlight"])
        self.assertEqual(
            (link["check"], link["finding_pk"]),
            (OpinionCheck.PAGE_NOT_READ, 13),
        )
        self.assertEqual(link["where"], "page 2")
        self.assertEqual(link["check_label"], OpinionCheck.PAGE_NOT_READ.label)

    def test_the_warnings_review_opens_the_words_a_majority_settled(self):
        """The same page in the warnings mode: the WARNING group's
        settled word is a card against the page's ENGINES_DISAGREE
        finding, every other open warning is a link, and the BLOCKING
        group is no card here."""
        split = read(
            "the court held that", "the court heId that", "the court hold that"
        )
        split.update(
            {
                "id": 3,
                "level": ensemble.BLOCKING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 133],
            }
        )
        settled = read("a b c", "a b c", "a x c")
        settled.update(
            {
                "id": 5,
                "level": ensemble.WARNING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 140, 288, 151],
            }
        )
        document = {
            "pages": [{"page_in_opinion": 0, "groups": [split, settled]}]
        }
        findings = [
            finding(0, OpinionCheck.ENGINES_DISAGREE, 14),
            finding(0, OpinionCheck.UNDETECTED_TEXT, 15),
        ]

        cards = blocking_review.cards(document, findings, ensemble.WARNING)

        self.assertEqual([c["kind"] for c in cards], ["word", "link"])
        word, link = cards
        self.assertEqual((word["group_id"], word["finding_pk"]), (5, 14))
        self.assertEqual(word["token"], "b")
        self.assertEqual(word["level"], ensemble.WARNING)
        self.assertEqual(
            [(r["engine"], r["word"]) for r in word["readings"]],
            [("dots_mocr", "b"), ("mistral_ocr", "b"), ("surya", "x")],
        )
        self.assertEqual(
            (link["check"], link["finding_pk"], link["level"]),
            (OpinionCheck.UNDETECTED_TEXT, 15, ensemble.WARNING),
        )

    def test_a_warning_group_with_no_settled_word_is_a_block_card(self):
        """A silent engine makes a WARNING group with every reading
        alike: one block card whose rows all read as shown."""
        quiet = read("a b c", "a b c")
        quiet.update(
            {
                "id": 6,
                "level": ensemble.WARNING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 140, 288, 151],
                "silent": ["surya"],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [quiet]}]}

        cards = blocking_review.cards(
            document,
            [finding(0, OpinionCheck.ENGINES_DISAGREE, 14)],
            ensemble.WARNING,
        )

        self.assertEqual([c["kind"] for c in cards], ["block"])
        self.assertEqual(cards[0]["silent"], ["surya"])
        self.assertEqual(cards[0]["open"], 0)
        self.assertEqual(
            [r["text"] for r in cards[0]["readings"]], ["a b c", "a b c"]
        )

    def test_a_table_is_one_block_card_with_every_reading(self):
        table = read("a b c", "x y z", "p q r")
        table.update(
            {
                "id": 7,
                "kind": "table",
                "level": ensemble.BLOCKING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 200],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [table]}]}

        cards = blocking_review.cards(
            document, [finding(0, OpinionCheck.NO_MAJORITY, 11)]
        )

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(
            (card["kind"], card["table"], card["open"]), ("block", True, 3)
        )
        self.assertEqual(
            [r["engine"] for r in card["readings"]], list(ENGINES)
        )
        self.assertEqual(card["readings"][1]["text"], "x y z")
        self.assertEqual(card["finding_pk"], 11)
        self.assertIsNone(card["crop"]["highlight"])

    def test_a_table_one_engine_read_says_so(self):
        alone = group_of(unit("mistral_ocr", 0, BODY_A_PT, "a | b"))
        alone.update(ensemble.resolve(alone))
        alone.update(
            {
                "id": 4,
                "kind": "table",
                "level": ensemble.BLOCKING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 133],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [alone]}]}

        cards = blocking_review.cards(
            document, [finding(0, OpinionCheck.SINGLE_ENGINE, 12)]
        )

        self.assertEqual(
            (cards[0]["kind"], cards[0]["table"]), ("single", True)
        )

    def test_a_block_whose_page_card_is_closed_is_no_card(self):
        """A dismissed ``NO_MAJORITY`` card leaves its blocks in the
        document as they were, and the approval no longer waits on
        them: no card, or it would offer an answer with nothing to
        close."""
        split = read(
            "the court held that", "the court heId that", "the court hold that"
        )
        split.update(
            {
                "id": 3,
                "level": ensemble.BLOCKING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 133],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [split]}]}

        self.assertEqual(blocking_review.cards(document, []), [])

    def test_a_block_with_many_open_words_is_one_card(self):
        many = read(
            "a b c d e f g h i j",
            "a1 b1 c1 d1 e1 f1 g1 h1 i1 j1",
            "a2 b2 c2 d2 e2 f2 g2 h2 i2 j2",
        )
        many.update(
            {
                "id": 8,
                "level": ensemble.BLOCKING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 200],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [many]}]}

        cards = blocking_review.cards(
            document, [finding(0, OpinionCheck.NO_MAJORITY, 11)]
        )

        self.assertEqual([c["kind"] for c in cards], ["block"])
        self.assertEqual((cards[0]["table"], cards[0]["open"]), (False, 10))

    def test_a_card_the_blocks_do_not_answer_is_a_link(self):
        """A ``NO_MAJORITY`` card of a page whose blocks hold no open
        word (a text written under another rule) is shown as a link:
        the approval waits on it, so the page must not hide it."""
        settled = read("a b c", "a b c", "a x c")
        settled.update(
            {
                "id": 5,
                "level": ensemble.WARNING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 140, 288, 151],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [settled]}]}

        cards = blocking_review.cards(
            document, [finding(0, OpinionCheck.NO_MAJORITY, 11)]
        )

        self.assertEqual([c["kind"] for c in cards], ["link"])
        self.assertEqual(cards[0]["finding_pk"], 11)

    def test_a_warning_block_and_a_dismissed_page_make_no_card(self):
        settled = read("a b c", "a b c", "a x c")
        settled.update(
            {
                "id": 5,
                "level": ensemble.WARNING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 140, 288, 151],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [settled]}]}

        self.assertEqual(blocking_review.cards(document, []), [])


# ── the cards endpoint ───────────────────────────────────────────────
class TestTheCardsEndpoint(EditTestCase, ScanningTestCase):
    """Over the fixture of the edits: one block of page 1 the two
    engines split, in ``READY_FOR_TEXT_REVIEW``."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def url(self, opinion=None, scan=None):
        opinion = opinion or self.opinion
        return reverse(
            "opinion_blocking_cards",
            kwargs={"pk": (scan or opinion.scan).pk, "opinion_pk": opinion.pk},
        )

    def test_the_cards_of_the_split_block(self):
        answer = self.client.get(self.url())

        self.assertEqual(answer.status_code, 200)
        data = answer.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(
            data["opinion"]["glue_revision"], self.opinion.glue_revision
        )
        self.assertEqual(
            data["opinion"]["edit_revision"],
            self.opinion.ensemble_edit_revision,
        )
        words = [c for c in data["cards"] if c["kind"] == "word"]
        self.assertTrue(words)
        self.assertEqual(words[0]["page_in_opinion"], 0)
        self.assertEqual(words[0]["group_id"], self.split_group()["id"])
        self.assertEqual(
            [r["engine"] for r in words[0]["readings"]],
            ["dots_mocr", "mistral_ocr"],
        )
        self.assertTrue(words[0]["dismiss_url"].endswith("/dismiss/"))
        self.assertIn("crop", words[0]["crop"])

    def test_a_card_carries_the_way_back_from_its_dismissal(self):
        data = self.client.get(self.url()).json()

        word = next(c for c in data["cards"] if c["kind"] == "word")
        self.assertTrue(word["restore_url"].endswith("/restore/"))
        self.assertEqual(
            word["restore_url"],
            reverse(
                "restore_opinion_finding",
                kwargs={
                    "pk": self.scan.pk,
                    "opinion_pk": self.opinion.pk,
                    "finding_pk": word["finding_pk"],
                },
            ),
        )

    def test_a_written_word_answers_its_edit_id_for_the_undo(self):
        """The page keeps the id to withdraw the edit with, the review
        page's own Undo, so a wrong pick is taken back the same way."""
        import json

        data = self.client.get(self.url()).json()
        word = next(c for c in data["cards"] if c["kind"] == "word")
        text = word["text"]
        body = {
            "page_in_opinion": word["page_in_opinion"],
            "group_id": word["group_id"],
            "text": text[: word["start"]]
            + "typed"
            + text[word["start"] + word["length"] :],
            "glue_revision": data["opinion"]["glue_revision"],
            "edit_revision": data["opinion"]["edit_revision"],
        }
        kwargs = {"pk": self.scan.pk, "opinion_pk": self.opinion.pk}

        written = self.client.post(
            reverse("edit_opinion_text", kwargs=kwargs),
            json.dumps(body),
            content_type="application/json",
        ).json()

        self.assertEqual(written["status"], "ok")
        self.assertIsInstance(written["edit_id"], int)
        taken_back = self.client.post(
            reverse("withdraw_opinion_edit", kwargs=kwargs),
            json.dumps({"edit_id": written["edit_id"]}),
            content_type="application/json",
        ).json()
        self.assertEqual(taken_back["status"], "ok")

    def test_a_block_taken_out_answers_its_edit_id_and_loses_its_cards(self):
        """The "Not text" button of a card: the whole block goes, every
        card of it with it, and the id withdraws the edit."""
        import json

        data = self.client.get(self.url()).json()
        card = next(c for c in data["cards"] if c["kind"] != "link")
        kwargs = {"pk": self.scan.pk, "opinion_pk": self.opinion.pk}
        written = self.client.post(
            reverse("edit_opinion_drop", kwargs=kwargs),
            json.dumps(
                {
                    "page_in_opinion": card["page_in_opinion"],
                    "group_id": card["group_id"],
                    "glue_revision": data["opinion"]["glue_revision"],
                    "edit_revision": data["opinion"]["edit_revision"],
                }
            ),
            content_type="application/json",
        ).json()
        self.assertEqual(written["status"], "ok")
        self.assertIsInstance(written["edit_id"], int)
        after = self.client.get(self.url()).json()
        self.assertEqual(
            [c for c in after["cards"] if c.get("box_pt") == card["box_pt"]],
            [],
        )
        taken_back = self.client.post(
            reverse("withdraw_opinion_edit", kwargs=kwargs),
            json.dumps({"edit_id": written["edit_id"]}),
            content_type="application/json",
        ).json()
        self.assertEqual(taken_back["status"], "ok")
        back = self.client.get(self.url()).json()
        self.assertTrue(
            any(c.get("box_pt") == card["box_pt"] for c in back["cards"])
        )

    def test_the_level_of_the_cards_is_read_off_the_query(self):
        data = self.client.get(self.url() + "?level=warning").json()

        self.assertEqual(data["level"], ensemble.WARNING)
        self.assertIsInstance(data["blocking_open"], int)
        self.assertTrue(
            all(c["level"] == ensemble.WARNING for c in data["cards"])
        )
        self.assertEqual(
            self.client.get(self.url()).json()["level"], ensemble.BLOCKING
        )

    def test_an_opinion_of_another_scan_is_404(self):
        answer = self.client.get(self.url(scan=ScanFactory()))

        self.assertEqual(answer.status_code, 404)

    def test_an_opinion_with_no_text_is_refused(self):
        bare = OpinionFactory(scan=self.scan, first_printed_page=900)

        answer = self.client.get(self.url(bare))

        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.json()["status"], "error")


# ── the page and the list ────────────────────────────────────────────
class TestTheBlockingPage(ScanningTestCase):
    def setUp(self):
        self.client.force_login(self.make_user())
        self.scan = ScanFactory()
        self.waiting = OpinionFactory(
            scan=self.scan, status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        OpinionFindingFactory(
            opinion=self.waiting,
            check_name=OpinionCheck.NO_MAJORITY,
            severity=Issue.Severity.ERROR,
        )
        self.clean = OpinionFactory(
            scan=self.scan,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
            first_printed_page=50,
        )
        # A warning holds nothing.
        OpinionFindingFactory(opinion=self.clean)

    def test_the_page_lists_the_opinions_that_wait(self):
        answer = self.client.get(
            reverse("opinion_blocking_review") + f"?scan={self.scan.pk}"
        )

        self.assertEqual(answer.status_code, 200)
        self.assertContains(answer, f'id="opinion-{self.waiting.pk}"')
        self.assertNotContains(answer, f'id="opinion-{self.clean.pk}"')
        # The one with nothing open is listed as ready to approve, with
        # the revisions the approval names.
        self.assertContains(answer, f'id="ready-{self.clean.pk}"')
        self.assertNotContains(answer, f'id="ready-{self.waiting.pk}"')
        self.assertContains(
            answer,
            reverse(
                "approve_opinion_text",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.clean.pk},
            ),
        )
        self.assertContains(
            answer,
            reverse(
                "opinion_blocking_cards",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.waiting.pk},
            ),
        )
        self.assertContains(
            answer,
            reverse(
                "edit_opinion_drop",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.waiting.pk},
            ),
        )
        self.assertContains(
            answer,
            reverse(
                "withdraw_opinion_edit",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.waiting.pk},
            ),
        )

    def test_the_warnings_page_lists_the_opinions_with_a_warning(self):
        """The one with a warning alone is listed, the one with a
        blocking card alone is not, nothing is ready, and the cards
        are asked for at the warning level."""
        answer = self.client.get(
            reverse("opinion_warning_review") + f"?scan={self.scan.pk}"
        )

        self.assertEqual(answer.status_code, 200)
        self.assertContains(answer, f'id="opinion-{self.clean.pk}"')
        self.assertNotContains(answer, f'id="opinion-{self.waiting.pk}"')
        self.assertNotContains(answer, 'id="ready-')
        self.assertContains(answer, 'data-level="warning"')
        self.assertContains(answer, "?level=warning")
        self.assertContains(answer, "Warnings review")

    def test_the_list_offers_the_warnings_review_of_a_volume(self):
        answer = self.client.get(
            reverse("opinion_list") + f"?scan={self.scan.pk}"
        )

        self.assertContains(
            answer, reverse("opinion_warning_review") + f"?scan={self.scan.pk}"
        )

    def test_an_approved_opinion_is_not_listed(self):
        self.waiting.status = OpinionReviewStatus.TEXT_REVIEW_DONE
        self.waiting.save(update_fields=["status"])

        answer = self.client.get(
            reverse("opinion_blocking_review") + f"?scan={self.scan.pk}"
        )

        self.assertNotContains(answer, f'id="opinion-{self.waiting.pk}"')

    def test_no_scope_sends_back_to_the_list(self):
        answer = self.client.get(reverse("opinion_blocking_review"))

        self.assertRedirects(answer, reverse("opinion_list"))

    def test_the_list_filters_to_the_blocking_ones_and_offers_the_button(self):
        answer = self.client.get(
            reverse("opinion_list") + f"?scan={self.scan.pk}&blocking=1"
        )

        self.assertContains(
            answer, reverse("opinion_review", kwargs={"pk": self.waiting.pk})
        )
        self.assertNotContains(
            answer, reverse("opinion_review", kwargs={"pk": self.clean.pk})
        )
        self.assertContains(
            answer,
            reverse("opinion_blocking_review") + f"?scan={self.scan.pk}",
        )

    def test_the_button_waits_for_a_volume(self):
        answer = self.client.get(reverse("opinion_list"))

        self.assertNotContains(
            answer, 'href="' + reverse("opinion_blocking_review")
        )
        self.assertContains(answer, "Review all blocking")
