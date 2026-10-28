"""The blocking review: the cards, the crop estimate, the two views."""

import pathlib
import re
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
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


SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static"
    / "scanning"
    / "blocking_review.js"
)


class TestTheScriptKeysAKeptCardByItsPlace(SimpleTestCase):
    """A kept card is browser state until the page's dismissal is
    sent, and an answer elsewhere rebuilds the opinion in between:
    the finding rows are written again with new pks and an edit before
    the word moves its offset. The key reads the position the server
    writes for that, and never the finding pk."""

    def test_the_key_reads_the_position_and_never_the_finding(self):
        source = SCRIPT.read_text()
        body = re.search(
            r"function keyOf\(card\) \{(.*?)\n    \}", source, re.S
        ).group(1)
        self.assertIn("card.position", body)
        self.assertNotIn("finding_pk", body)
        self.assertNotIn("card.start", body.split("else")[0])

    def test_a_reload_settles_the_findings_whose_cards_are_all_kept(self):
        source = SCRIPT.read_text()
        self.assertIn("return self.settle();", source)
        self.assertIn("Section.prototype.settle = function", source)


# ── the open words ───────────────────────────────────────────────────
class TestOpenWords(TestCase):
    def test_a_run_one_engine_alone_read_is_a_warning_card_on_the_gap(self):
        """The star-page number: the card sits on the gap before
        ``included``, with a zero-length span there, and its rows are
        nothing, nothing and ``1490``. The answer goes into the gap."""
        voted = read("a included b", "a included c", "a 1490 included b")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        gap = cards[0]
        self.assertTrue(gap["gap"])
        self.assertEqual(
            (gap["start"], gap["length"], gap["token"]), (2, 0, "")
        )
        self.assertEqual((gap["before"], gap["after"]), ("a", "included b"))
        self.assertEqual(
            [(r["engine"], r["word"]) for r in gap["readings"]],
            [("dots_mocr", ""), ("mistral_ocr", ""), ("surya", "1490")],
        )
        self.assertEqual(gap["crop_span"], [2, 8])
        # The word a majority settled over one engine is its own card.
        self.assertEqual([c["token"] for c in cards[1:]], ["b"])
        self.assertEqual(blocking_review.open_words(voted), [])

    def test_a_card_keeps_its_position_when_a_word_before_it_is_rewritten(
        self,
    ):
        """The browser keys a kept card by ``position``: the finding pk
        is new on every rebuild and the offset moves with an edit
        before the word, so a word kept as shown must still match its
        card after an answer elsewhere in the block rebuilt the
        opinion."""
        voted = read(
            "one two three four", "uno two tres four", "ein two drei four"
        )

        before = blocking_review.open_words(voted)
        kept = ensemble._human_read(
            voted,
            {"text": "unoxx two three four", "span": [0, 3]},
            voted["engines"],
        )
        after = blocking_review.open_words(kept)

        self.assertEqual(
            [(c["token"], c["start"], c["position"]) for c in before],
            [("one", 0, 0), ("three", 8, 2)],
        )
        # The rewritten word is judged; the kept one moved by two
        # characters and kept its position.
        self.assertEqual(
            [(c["token"], c["start"], c["position"]) for c in after],
            [("three", 10, 2)],
        )

    def test_a_gap_card_carries_the_position_of_the_word_after_it(self):
        voted = read("a included b", "a included c", "a 1490 included b")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        self.assertEqual(
            [(c.get("gap"), c["position"]) for c in cards],
            [(True, 1), (None, 2)],
        )

    def test_a_gap_no_two_engines_read_alike_is_a_blocking_card(self):
        voted = read(
            "two or more", "two [c]ontract or more", "two I contract or more"
        )

        cards = blocking_review.open_words(voted)

        self.assertEqual(len(cards), 1)
        gap = cards[0]
        self.assertTrue(gap["gap"])
        self.assertEqual((gap["start"], gap["length"]), (4, 0))
        self.assertEqual(
            [r["word"] for r in gap["readings"]],
            ["", "[c]ontract", "I contract"],
        )
        self.assertEqual(
            blocking_review.open_words(voted, blocking_review.MAJORITY), []
        )

    def test_a_word_a_majority_dropped_is_a_card_on_its_gap(self):
        voted = read(
            "two 1490 or more alpha", "two or more beta", "two or more gamma"
        )
        self.assertEqual(voted["text"], "two or more alpha")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        self.assertEqual([(c["token"], c["start"]) for c in cards], [("", 4)])
        self.assertEqual(
            [(r["engine"], r["word"]) for r in cards[0]["readings"]],
            [("dots_mocr", "1490"), ("mistral_ocr", ""), ("surya", "")],
        )

    def test_a_gap_beside_a_changed_word_keeps_its_card(self):
        voted = read("a b c d", "a z c d", "a b W c d")
        kept = ensemble._human_read(
            voted, {"text": "a z c d", "span": [2, 3]}, voted["engines"]
        )

        cards = blocking_review.open_words(kept, blocking_review.MAJORITY)

        gap = next(c for c in cards if c.get("gap"))
        self.assertEqual((gap["start"], gap["after"]), (4, "c d"))
        self.assertEqual([r["word"] for r in gap["readings"]], ["", "", "W"])

    def test_a_gap_before_a_word_a_person_rewrote_keeps_its_card(self):
        voted = read(
            "two or more", "two [c]ontract or more", "two I contract or more"
        )
        kept = ensemble._human_read(
            voted, {"text": "two of more", "span": [4, 6]}, voted["engines"]
        )

        cards = blocking_review.open_words(kept)

        self.assertEqual(
            [(c.get("gap"), c["start"]) for c in cards], [(True, 4)]
        )
        self.assertEqual(
            [r["word"] for r in cards[0]["readings"]],
            ["", "[c]ontract", "I contract"],
        )

    def test_the_stored_block_of_a_person_names_the_engine_of_its_positions(
        self,
    ):
        """Through the build, not ``_human_read`` alone: the first
        engine dissents from the agreeing pair, the shown text is the
        pair's, and after a word answer the stored block carries the
        engine its tokens count against, so the cards of the words
        left open read the right rows at the right places."""
        from scanning.tests.test_ensemble import engine_page

        texts = {
            "dots_mocr": "a sat x",
            "mistral_ocr": "a dog sat y",
            "surya": "a dog sat y",
        }
        pages = {
            name: engine_page([unit(name, 0, BODY_A_PT, text)])
            for name, text in texts.items()
        }
        edit = {
            "id": 1,
            "kind": "text",
            "source_edit_id": None,
            "source_page": 2,
            "page_in_opinion": 0,
            "box_pt": BODY_A_PT,
            "section": ensemble.BODY,
            "base_text": "a dog sat y",
            "text": "a dog sat z",
            "order": [],
            "quoted": None,
            "span": [10, 11],
            "by": "curator",
            "at": "",
        }

        page = ensemble.build_page(pages, 0, edits=[edit])

        group = page["groups"][0]
        self.assertEqual(group["agreement"], ensemble.HUMAN)
        self.assertEqual(group["base"], "mistral_ocr")
        cards = blocking_review.open_words(group, blocking_review.MAJORITY)
        self.assertEqual(
            [(c["token"], c["start"]) for c in cards], [("dog", 2)]
        )
        self.assertEqual(
            [(r["engine"], r["word"]) for r in cards[0]["readings"]],
            [("mistral_ocr", "dog"), ("dots_mocr", ""), ("surya", "dog")],
        )

    def test_a_bare_mark_the_base_read_is_no_row_of_a_gap_card(self):
        """``* * *`` has no key and no vote token, so it is not a
        dropped word: the base's row of the gap after it is empty, and
        choosing a row never writes the mark a second time."""
        voted = read("a b * * * c x", "a b * * * 1490 c y", "a b * * * c z")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        gap = next(c for c in cards if c.get("gap"))
        self.assertEqual(
            [r["word"] for r in gap["readings"]], ["", "1490", ""]
        )

    def test_a_run_before_a_bare_mark_is_on_the_gap_card_after_it(self):
        """The vote carries the gap past the marks to ``c``, so the
        card's rows read the runs over the whole gap."""
        voted = read("a b * * * c x", "a b 1490 * * * c y", "a b * * * c z")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        gap = next(c for c in cards if c.get("gap"))
        self.assertEqual(
            [r["word"] for r in gap["readings"]], ["", "1490", ""]
        )

    def test_a_gap_card_after_a_rewritten_word_shows_its_own_runs(self):
        """Two gaps, before ``b`` and before ``c``, and the person
        rewrote ``b``: the card before ``c`` is bounded by the vote's
        own positions, so its rows hold ``Y`` and never the other gap's
        ``X``, and the card on the rewritten word holds ``X``."""
        voted = read("a b c d", "a X b Y c d", "a b c d")
        kept = ensemble._human_read(
            voted, {"text": "a z c d", "span": [2, 3]}, voted["engines"]
        )

        cards = blocking_review.open_words(kept, blocking_review.MAJORITY)

        gaps = [c for c in cards if c.get("gap")]
        self.assertEqual(
            [(g["start"], [r["word"] for r in g["readings"]]) for g in gaps],
            [(2, ["", "X", ""]), (4, ["", "Y", ""])],
        )

    def test_a_gap_card_never_shows_a_run_the_vote_put_in(self):
        """``1490`` was read by two engines and went in; ``W`` by one
        alone and was dropped, past the marks. The card before ``c``
        shows ``W`` and not ``1490``, which is in the text already."""
        voted = read(
            "a b * * * c x", "a b 1490 * * * c y", "a b 1490 * * * W c z"
        )
        self.assertIn("1490", voted["text"])

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        gap = next(c for c in cards if c.get("gap"))
        self.assertEqual([r["word"] for r in gap["readings"]], ["", "", "W"])

    def test_a_run_after_the_last_word_is_a_card_after_it(self):
        voted = read("a b", "a c", "a b 1490")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        self.assertEqual(
            [(c["token"], c["start"], c["length"]) for c in cards],
            [("b", 2, 1), ("", 3, 0)],
        )
        gap = cards[1]
        self.assertTrue(gap["gap"])
        self.assertEqual((gap["before"], gap["after"]), ("a b", ""))
        self.assertEqual(
            [r["word"] for r in gap["readings"]], ["", "", "1490"]
        )

    def test_the_cards_are_placed_in_the_shown_text(self):
        """Two engines read ``the dog sat`` and the first read ``the
        sat``: the shown text is theirs, and the card of ``dog`` is a
        word a majority settled over the first engine, at its offset
        in the shown text, never a run the first engine lacks."""
        voted = read("the sat", "the dog sat", "the dog sat")
        self.assertEqual(voted["text"], "the dog sat")

        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)

        self.assertEqual(
            [(c["token"], c["start"]) for c in cards], [("dog", 4)]
        )
        self.assertEqual(
            [(r["engine"], r["word"]) for r in cards[0]["readings"]],
            [("mistral_ocr", "dog"), ("dots_mocr", ""), ("surya", "dog")],
        )

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

    def test_a_block_a_person_wrote_keeps_the_open_words_they_left(self):
        """One card answered (the edit's ``span``), the other stays:
        the open word the person left as it was is placed in the
        person's text, with the same readings; a word they changed is
        no card, and a whole-block answer leaves none."""
        voted = read(
            "the court held that",
            "the court heId that",
            "the court hold that",
        )
        engines = voted["engines"]
        kept = ensemble._human_read(
            voted, {"text": "a court held that day", "span": [0, 3]}, engines
        )
        words = blocking_review.open_words(kept)

        self.assertEqual(len(words), 1)
        word = words[0]
        self.assertEqual(
            (word["token"], word["start"], word["length"]), ("held", 8, 4)
        )
        self.assertEqual(
            [r["word"] for r in word["readings"]], ["held", "heId", "hold"]
        )
        self.assertEqual(
            (word["before"], word["after"]), ("a court", "that day")
        )

        answered = ensemble._human_read(
            voted, {"text": "the court ruled that", "span": [10, 14]}, engines
        )
        self.assertEqual(blocking_review.open_words(answered), [])

        whole = ensemble._human_read(
            voted, {"text": "a court held that", "span": None}, engines
        )
        self.assertEqual(whole["tokens"], [])
        self.assertEqual(blocking_review.open_words(whole), [])

    def test_a_run_the_base_did_not_read_keeps_its_place_after_an_answer(
        self,
    ):
        """Two engines read ``plainly and`` where the base read nothing
        (and differ at the end, so no reading holds a majority): two
        cards. The person answers the first; the card of the second
        still shows the engines' second word of the run, not their
        first, because the place in the run is carried on the token."""
        voted = read(
            "the court held today",
            "the court plainly and held today",
            "the court plainly and held todey",
        )
        self.assertEqual(voted["agreement"], ensemble.VOTED)
        cards = blocking_review.open_words(voted)
        self.assertEqual([c["token"] for c in cards], ["plainly", "and"])
        self.assertEqual(
            [r["word"] for r in cards[1]["readings"]],
            [blocking_review.READ_NOTHING, "and", "and"],
        )
        first = cards[0]
        text = voted["text"]
        answered = (
            text[: first["start"]]
            + "clearly"
            + text[first["start"] + first["length"] :]
        )

        kept = ensemble._human_read(
            voted,
            {
                "text": answered,
                "span": [first["start"], first["start"] + first["length"]],
            },
            voted["engines"],
        )
        cards = blocking_review.open_words(kept)

        self.assertEqual(kept["text"], "the court clearly and held today")
        self.assertEqual([c["token"] for c in cards], ["and"])
        self.assertEqual(
            [r["word"] for r in cards[0]["readings"]],
            [blocking_review.READ_NOTHING, "and", "and"],
        )

    def test_a_block_a_majority_read_alike_never_becomes_blocking(self):
        """The re-vote for the carry is from the shown engine's side,
        the agreeing pair's, so a word the pair read and the first
        engine missed (``x``) is a word a majority settled over one
        engine, a warning like ``e`` and ``h``; nothing of a block two
        engines read alike is ever blocking, before or after an answer."""
        voted = read("a b c d g", "a b x c e h", "a b x c e h")
        self.assertEqual(voted["agreement"], ensemble.MAJORITY)
        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)
        self.assertEqual([c["token"] for c in cards], ["x", "e", "h"])
        first = cards[1]

        kept = ensemble._human_read(
            voted,
            {
                "text": "a b x c f h",
                "span": [first["start"], first["start"] + first["length"]],
            },
            voted["engines"],
        )

        self.assertEqual(kept["n_low_confidence"], 0)
        self.assertEqual(
            ensemble.disagreement_level(kept, 3), ensemble.WARNING
        )
        self.assertEqual(
            blocking_review.open_words(kept, blocking_review.LOW_CONFIDENCE),
            [],
        )
        self.assertEqual(
            [
                c["token"]
                for c in blocking_review.open_words(
                    kept, blocking_review.MAJORITY
                )
            ],
            ["x", "h"],
        )

    def test_a_block_a_majority_read_alike_keeps_its_majority_words(self):
        """The warnings review: a block two engines read alike has no
        tokens of its own, so its majority words are voted for the
        carry, and the block is a warning until each is answered."""
        voted = read("a b c", "a b c", "a x c")
        self.assertEqual(voted["agreement"], ensemble.MAJORITY)
        cards = blocking_review.open_words(voted, blocking_review.MAJORITY)
        self.assertEqual([c["token"] for c in cards], ["b"])

        kept = ensemble._human_read(
            voted, {"text": "a b d", "span": [4, 5]}, voted["engines"]
        )

        self.assertTrue(ensemble._differs(kept, 3))
        self.assertEqual(
            ensemble.disagreement_level(kept, 3), ensemble.WARNING
        )
        cards = blocking_review.open_words(kept, blocking_review.MAJORITY)
        self.assertEqual([(c["token"], c["start"]) for c in cards], [("b", 2)])
        self.assertEqual(
            blocking_review.open_words(kept, blocking_review.LOW_CONFIDENCE),
            [],
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
    def test_a_gap_card_is_cropped_at_the_word_beside_it(self):
        group = read("a included b", "a included c", "a 1490 included b")
        group.update(
            {
                "id": 3,
                "level": ensemble.WARNING,
                "section": ensemble.BODY,
                "column": "L",
                "box_pt": [36, 100, 288, 133],
            }
        )
        document = {"pages": [{"page_in_opinion": 0, "groups": [group]}]}

        cards = blocking_review.cards(
            document,
            [finding(0, OpinionCheck.ENGINES_DISAGREE, 9)],
            ensemble.WARNING,
        )

        self.assertEqual([c["kind"] for c in cards], ["word", "word"])
        self.assertTrue(cards[0]["gap"])
        self.assertIsNotNone(cards[0]["crop"]["highlight"])
        self.assertEqual(cards[0]["finding_pk"], 9)

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

    def test_a_partial_redaction_is_a_cover_card_of_its_blocks(self):
        """The page's partial drops, each outlined: the crop holds them
        all, with the page around a short one, and a bracket drop is
        not the card's."""
        document = {
            "pages": [
                {
                    "page_in_opinion": 0,
                    "groups": [],
                    "dropped": [
                        {"partial": True, "box_pt": [100, 200, 140, 210]},
                        {"partial": True, "box_pt": [100, 400, 300, 430]},
                        {
                            "partial": True,
                            "bracket": True,
                            "box_pt": [50, 50, 60, 60],
                        },
                        {"partial": False, "box_pt": [0, 0, 10, 10]},
                    ],
                }
            ]
        }

        cards = blocking_review.cards(
            document, [finding(0, OpinionCheck.PARTIAL_REDACTION, 21)]
        )

        self.assertEqual([c["kind"] for c in cards], [blocking_review.COVER])
        card = cards[0]
        self.assertEqual(card["finding_pk"], 21)
        self.assertEqual(
            card["boxes"], [[100, 200, 140, 210], [100, 400, 300, 430]]
        )
        x0, y0, x1, y1 = card["crop"]["crop"]
        self.assertLessEqual(
            (x0, y0),
            (100 - blocking_review.CROP_PAD_PT, 200 - blocking_review.LINE_PT),
        )
        self.assertGreaterEqual(
            (x1, y1),
            (300 + blocking_review.CROP_PAD_PT, 430 + blocking_review.LINE_PT),
        )
        self.assertGreaterEqual(x1 - x0, blocking_review.COVER_MIN_WIDTH_PT)

    def test_a_short_block_is_shown_with_the_page_around_it(self):
        document = {
            "pages": [
                {
                    "page_in_opinion": 0,
                    "groups": [],
                    "dropped": [
                        {"partial": True, "box_pt": [200, 300, 210, 308]}
                    ],
                }
            ]
        }

        [card] = blocking_review.cards(
            document, [finding(0, OpinionCheck.PARTIAL_REDACTION, 21)]
        )

        x0, y0, x1, y1 = card["crop"]["crop"]
        self.assertAlmostEqual(x1 - x0, blocking_review.COVER_MIN_WIDTH_PT)
        self.assertAlmostEqual(y1 - y0, blocking_review.COVER_MIN_HEIGHT_PT)
        self.assertAlmostEqual((x0 + x1) / 2, 205)

    def test_a_partial_redaction_with_no_box_is_a_link(self):
        """A document written before the drops carried a box."""
        document = {
            "pages": [
                {
                    "page_in_opinion": 0,
                    "groups": [],
                    "dropped": [{"partial": True}],
                }
            ]
        }

        cards = blocking_review.cards(
            document, [finding(0, OpinionCheck.PARTIAL_REDACTION, 21)]
        )

        self.assertEqual([c["kind"] for c in cards], [blocking_review.LINK])

    def test_a_warning_partial_redaction_is_a_cover_card_too(self):
        """A text built before the card blocked keeps it as a warning."""
        document = {
            "pages": [
                {
                    "page_in_opinion": 0,
                    "groups": [],
                    "dropped": [
                        {"partial": True, "box_pt": [100, 200, 300, 230]}
                    ],
                }
            ]
        }

        cards = blocking_review.cards(
            document,
            [finding(0, OpinionCheck.PARTIAL_REDACTION, 21)],
            ensemble.WARNING,
        )

        self.assertEqual([c["kind"] for c in cards], [blocking_review.COVER])
        self.assertEqual(cards[0]["level"], ensemble.WARNING)

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

    def test_the_counts_mode_answers_both_lengths_from_one_read(self):
        data = self.client.get(self.url() + "?counts=1").json()

        self.assertEqual(data["status"], "ok")
        self.assertNotIn("cards", data)
        self.assertEqual(
            data["counts"][ensemble.BLOCKING],
            len(self.client.get(self.url()).json()["cards"]),
        )
        self.assertEqual(
            data["counts"][ensemble.WARNING],
            len(
                self.client.get(self.url() + "?level=warning").json()["cards"]
            ),
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
            # Past the factory's own sequence, whatever ran before.
            first_printed_page=self.waiting.first_printed_page + 1000,
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

    def walk(self, route, opinion):
        return reverse(route) + f"?scan={self.scan.pk}&opinion={opinion.pk}"

    def test_an_opinion_filter_walks_that_opinion_alone(self):
        other = OpinionFactory(
            scan=self.scan,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
            first_printed_page=70,
        )
        OpinionFindingFactory(
            opinion=other,
            check_name=OpinionCheck.NO_MAJORITY,
            severity=Issue.Severity.ERROR,
        )

        answer = self.client.get(
            self.walk("opinion_blocking_review", self.waiting)
        )

        self.assertContains(answer, f'id="opinion-{self.waiting.pk}"')
        self.assertNotContains(answer, f'id="opinion-{other.pk}"')
        # Nor the opinion of the volume that waits on its approval.
        self.assertNotContains(answer, f'id="ready-{self.clean.pk}"')

    def test_the_walk_of_one_opinion_goes_back_to_the_whole_list(self):
        """Its own pages keep the opinion; the list it goes back to is
        the volume's."""
        answer = self.client.get(
            self.walk("opinion_blocking_review", self.waiting)
        )

        self.assertEqual(
            answer.context["list_url"],
            reverse("opinion_list") + f"?scan={self.scan.pk}",
        )
        self.assertIn(f"opinion={self.waiting.pk}", answer.context["query"])

    def test_an_opinion_alone_is_a_scope(self):
        answer = self.client.get(
            reverse("opinion_blocking_review") + f"?opinion={self.waiting.pk}"
        )

        self.assertEqual(answer.status_code, 200)
        self.assertContains(answer, f'id="opinion-{self.waiting.pk}"')

    def test_the_review_page_offers_the_walks_of_its_opinion(self):
        OpinionFindingFactory(opinion=self.waiting)

        answer = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.waiting.pk})
        )

        self.assertContains(
            answer,
            self.walk("opinion_blocking_review", self.waiting).replace(
                "&", "&amp;"
            ),
        )
        self.assertContains(
            answer,
            self.walk("opinion_warning_review", self.waiting).replace(
                "&", "&amp;"
            ),
        )

    def test_the_review_page_offers_no_walk_with_nothing_open(self):
        """The clean opinion has a warning and no blocking card, so it
        offers the warnings walk alone."""
        answer = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.clean.pk})
        )

        self.assertNotContains(answer, reverse("opinion_blocking_review"))
        self.assertContains(answer, reverse("opinion_warning_review"))

    def test_an_approved_opinion_offers_no_walk(self):
        self.waiting.status = OpinionReviewStatus.TEXT_REVIEW_DONE
        self.waiting.save(update_fields=["status"])

        answer = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.waiting.pk})
        )

        self.assertNotContains(answer, reverse("opinion_blocking_review"))
        self.assertNotContains(answer, reverse("opinion_warning_review"))

    def test_the_button_waits_for_a_volume(self):
        answer = self.client.get(reverse("opinion_list"))

        self.assertNotContains(
            answer, 'href="' + reverse("opinion_blocking_review")
        )
        self.assertContains(answer, "Review all blocking")
