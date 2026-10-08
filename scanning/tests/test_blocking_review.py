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

        cards = blocking_review.cards(document, [])

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
