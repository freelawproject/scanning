"""Tests for the human edits of the text of review 3 (issue #376).

A curator changes the text of one block, the section of one block and
the order of the blocks of one section of one page. Each change is an
``OpinionEdit`` row that names its block by the page address plus a
copy of the box, and the ensemble applies the standing rows at every
build. Four groups of tests here:

- the land rule and the build (``ensemble.land_edits``,
  ``ensemble.build_page``), over plain dicts;
- the write of the ensemble with edits: the rows, the key, the swap,
  the card of an edit the text does not hold;
- the endpoints, their gates and the Django message of every answer;
- the writers of ``opinion_edits``.
"""

from unittest.mock import patch

from django.contrib import messages
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from scanning import ensemble, opinion_edits, opinion_findings
from scanning.factories import ScanFactory
from scanning.models import (
    Opinion,
    OpinionCheck,
    OpinionEdit,
    OpinionFinding,
    OpinionReviewStatus,
    OpinionText,
)
from scanning.tests.test_ensemble import (
    BODY_A_PT,
    BODY_B_PT,
    EnsembleTestCase,
    em,
    engine_page,
    sup,
    unit,
)
from scanning.tests.test_views import ScanningTestCase

#: A third body box, below the two of the fixture, in points.
BODY_C_PT = [36.0, 540.0, 288.0, 700.0]

#: A footnote zone over the lower part of the page, in points.
FOOT_ZONE = [30.0, 530.0, 300.0, 710.0]


def entry(kind, box=None, **fields) -> dict:
    """One edit entry, in the shape of ``ensemble.edit_entries``."""
    base = {
        "id": fields.pop("id", 1),
        "kind": kind,
        "source_edit_id": None,
        "source_page": 2,
        "page_in_opinion": 0,
        "box_pt": box,
        "section": "",
        "base_text": "",
        "text": "",
        "order": [],
        "by": "curator",
        "at": "",
    }
    base.update(fields)
    return base


def page_with(dots, mistral, edits=(), zones=()) -> dict:
    """The ensemble of one page, read by two engines, with edits."""
    pages = {
        "dots_mocr": engine_page(dots, zones),
        "mistral_ocr": engine_page(mistral, zones),
    }
    return ensemble.build_page(pages, 0, list(edits))


def alike(*specs) -> tuple[list, list]:
    """The units of two engines that read each box alike."""
    dots, mistral = [], []
    for index, (box, text) in enumerate(specs):
        dots.append(unit("dots_mocr", index, box, text))
        mistral.append(unit("mistral_ocr", index, box, text))
    return dots, mistral


def group_at(page: dict, box) -> dict:
    """The group of a page whose box is ``box``."""
    return next(g for g in page["groups"] if g["box_pt"] == box)


class TestTheLandRule(TestCase):
    """``ensemble.land_edits``: IoU, one each way, the greatest first."""

    def test_a_box_lands_on_the_group_it_covers(self):
        groups = [{"box_pt": BODY_A_PT}, {"box_pt": BODY_B_PT}]
        moved = [BODY_B_PT[0] + 2, BODY_B_PT[1] + 2, *BODY_B_PT[2:]]

        self.assertEqual(ensemble.land_edits([moved], groups), {0: 1})

    def test_a_box_under_the_overlap_lands_nowhere(self):
        groups = [{"box_pt": BODY_A_PT}]
        half = [36.0, 108.0, 100.0, 150.0]

        self.assertEqual(ensemble.land_edits([half], groups), {})

    def test_each_group_takes_one_box_and_the_older_wins_a_tie(self):
        groups = [{"box_pt": BODY_A_PT}]

        landed = ensemble.land_edits([BODY_A_PT, BODY_A_PT], groups)

        self.assertEqual(landed, {0: 0})

    def test_no_box_lands_nowhere(self):
        self.assertEqual(
            ensemble.land_edits([None], [{"box_pt": BODY_A_PT}]), {}
        )


class TestTheTextEdit(TestCase):
    """A ``TEXT`` edit replaces the text of the block it lands on."""

    def disagree(self):
        dots = [
            unit("dots_mocr", 0, BODY_A_PT, "The court held that"),
            unit("dots_mocr", 1, BODY_B_PT, "Affirmed."),
        ]
        mistral = [
            unit("mistral_ocr", 0, BODY_A_PT, "The cour held tbat"),
            unit("mistral_ocr", 1, BODY_B_PT, "Affirmed."),
        ]
        return dots, mistral

    def test_the_text_is_the_curators_and_the_block_no_disagreement(self):
        dots, mistral = self.disagree()
        base = page_with(dots, mistral)
        group = group_at(base, BODY_A_PT)
        self.assertEqual(group["level"], ensemble.BLOCKING)

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text=group["text"],
                    text="The court held that it",
                    section=ensemble.BODY,
                )
            ],
        )

        edited = group_at(page, BODY_A_PT)
        self.assertEqual(edited["text"], "The court held that it")
        self.assertEqual(edited["agreement"], ensemble.HUMAN)
        self.assertIsNone(edited["level"])
        self.assertEqual(edited["human"]["by"], "curator")
        self.assertEqual(edited["human"]["edit_id"], 1)
        self.assertTrue(page["text"].startswith("The court held that it"))
        self.assertEqual(page["counts"]["differing"], 0)
        self.assertEqual(page["counts"][ensemble.HUMAN], 1)
        self.assertEqual(page["unresolved_edits"], [])
        self.assertEqual(ensemble._disagreements(page), [])

    def test_the_readings_of_the_engines_stay_on_the_block(self):
        dots, mistral = self.disagree()
        group = group_at(page_with(dots, mistral), BODY_A_PT)

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text=group["text"],
                    text="The court held",
                )
            ],
        )

        engines = group_at(page, BODY_A_PT)["engines"]
        self.assertEqual(engines["mistral_ocr"]["text"], "The cour held tbat")

    def test_a_changed_base_holds_the_edit(self):
        """The curator judged a text that is not there now."""
        dots, mistral = self.disagree()

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text="Some other words",
                    text="The court held",
                )
            ],
        )

        group = group_at(page, BODY_A_PT)
        self.assertNotEqual(group["agreement"], ensemble.HUMAN)
        self.assertIsNone(group["human"])
        self.assertEqual(
            [e["reason"] for e in page["unresolved_edits"]],
            [ensemble.EDIT_BASE_CHANGED],
        )
        self.assertEqual(page["counts"]["unresolved_edits"], 1)

    def test_the_base_is_compared_by_the_key_of_the_vote(self):
        """A curly quote is no other reading."""
        dots = [unit("dots_mocr", 0, BODY_A_PT, "The court's rule")]
        mistral = [unit("mistral_ocr", 0, BODY_A_PT, "The courts rule")]

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text="The court’s rule",
                    text="The court's rule.",
                )
            ],
        )

        self.assertEqual(page["unresolved_edits"], [])

    def test_an_edit_on_no_block_is_unresolved(self):
        dots, mistral = self.disagree()

        page = page_with(
            dots,
            mistral,
            [entry(OpinionEdit.Kind.TEXT, BODY_C_PT, text="x")],
        )

        self.assertEqual(
            [e["reason"] for e in page["unresolved_edits"]],
            [ensemble.EDIT_NO_GROUP],
        )

    def test_an_edit_never_puts_back_a_redacted_block(self):
        excluded = {"reason": "redaction", "rect_type": "text"}
        dots = [
            unit("dots_mocr", 0, BODY_A_PT, "Name", exclusion=excluded),
        ]
        mistral = [unit("mistral_ocr", 0, BODY_A_PT, "Name")]

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text="Name",
                    text="Name",
                )
            ],
        )

        self.assertEqual(page["groups"], [])
        self.assertNotIn("Name", page["text"])
        self.assertEqual(
            [e["reason"] for e in page["unresolved_edits"]],
            [ensemble.EDIT_DROPPED],
        )

    def test_a_block_no_engine_reads_now_says_so(self):
        """No redaction took it, so the card must not name one."""
        dots = [unit("dots_mocr", 0, BODY_A_PT, "")]
        mistral = [unit("mistral_ocr", 0, BODY_A_PT, "")]

        page = page_with(
            dots,
            mistral,
            [entry(OpinionEdit.Kind.TEXT, BODY_A_PT, base_text="x", text="y")],
        )

        [unresolved] = page["unresolved_edits"]
        self.assertEqual(unresolved["reason"], ensemble.EDIT_EMPTY)
        self.assertIn("no engine reads", unresolved["said"])
        self.assertNotIn("redaction", unresolved["said"])

    def test_an_unresolved_edit_carries_its_line(self):
        dots, mistral = self.disagree()

        page = page_with(
            dots,
            mistral,
            [entry(OpinionEdit.Kind.SECTION, BODY_C_PT, section="footnotes")],
        )

        [unresolved] = page["unresolved_edits"]
        self.assertEqual(
            unresolved["said"],
            "The section of a block by curator: no block of the text is "
            "where it was",
        )

    def test_the_marks_carry_to_the_words_the_curator_kept(self):
        dots = [
            unit(
                "dots_mocr",
                0,
                BODY_A_PT,
                "See Lewis v. Marcotte here",
                marks=[em(4, 20)],
            )
        ]
        mistral = [
            unit("mistral_ocr", 0, BODY_A_PT, "See Lewis v. Marcotle here")
        ]
        group = group_at(page_with(dots, mistral), BODY_A_PT)

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text=group["text"],
                    text="See Lewis v. Marcotte, here",
                )
            ],
        )

        edited = group_at(page, BODY_A_PT)
        italic = [m for m in edited["marks"] if m["kind"] == "em"]
        self.assertTrue(italic)
        # The mark covers the words it touched in the engine's text,
        # and the comma the curator put on the last one is of that word.
        self.assertEqual(
            edited["text"][italic[0]["start"] : italic[0]["end"]],
            "Lewis v. Marcotte,",
        )
        self.assertEqual(edited["text"][italic[0]["end"] :], " here")

    def test_a_corrected_footnote_mark_keeps_its_superscript(self):
        """The curator corrects the character under the superscript
        (#423): no edit kind adds a mark, so the edit must keep it."""
        dots = [
            unit(
                "dots_mocr",
                0,
                BODY_A_PT,
                'the acts."l The court',
                marks=[sup(10, 11)],
            )
        ]
        mistral = [unit("mistral_ocr", 0, BODY_A_PT, 'the acts."l The court')]
        group = group_at(page_with(dots, mistral), BODY_A_PT)
        self.assertEqual(group["marks"], [sup(10, 11)])

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.TEXT,
                    BODY_A_PT,
                    base_text=group["text"],
                    text='the acts."1 The court',
                )
            ],
        )

        edited = group_at(page, BODY_A_PT)
        self.assertEqual(edited["text"], 'the acts."1 The court')
        self.assertEqual(edited["marks"], [sup(10, 11)])

    def test_a_page_nobody_read_holds_no_edit(self):
        pages = {
            "dots_mocr": {"page_in_opinion": 0, "error": "not read"},
            "mistral_ocr": {"page_in_opinion": 0, "error": "not read"},
        }

        page = ensemble.build_page(
            pages, 0, [entry(OpinionEdit.Kind.TEXT, BODY_A_PT)]
        )

        self.assertEqual(
            [e["reason"] for e in page["unresolved_edits"]],
            [ensemble.EDIT_NO_GROUP],
        )


class TestTheSectionEdit(TestCase):
    """A ``SECTION`` edit puts a block in the body or the footnotes."""

    def test_a_body_block_goes_to_the_footnotes(self):
        dots, mistral = alike(
            (BODY_A_PT, "The court held."), (BODY_B_PT, "1 See the note.")
        )

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.SECTION,
                    BODY_B_PT,
                    id=7,
                    section=ensemble.FOOTNOTES,
                )
            ],
        )

        group = group_at(page, BODY_B_PT)
        self.assertEqual(group["section"], ensemble.FOOTNOTES)
        self.assertEqual(group["section_edit"], 7)
        self.assertEqual(page["footnotes"], "1 See the note.")
        self.assertEqual(page["text"], "The court held.")
        self.assertEqual(page["counts"]["footnote_groups"], 1)

    def test_a_footnote_goes_to_the_body_and_raises_no_doubt(self):
        dots = [
            unit("dots_mocr", 0, BODY_A_PT, "The court held."),
            unit("dots_mocr", 1, BODY_C_PT, "It is so.", label="Footnote"),
        ]
        mistral = [
            unit("mistral_ocr", 0, BODY_A_PT, "The court held."),
            unit("mistral_ocr", 1, BODY_C_PT, "It is so.", label="footnote"),
        ]
        zoned = page_with(dots, mistral, zones=[FOOT_ZONE])
        self.assertEqual(
            group_at(zoned, BODY_C_PT)["section"], ensemble.FOOTNOTES
        )

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.SECTION, BODY_C_PT, section=ensemble.BODY
                )
            ],
            zones=[FOOT_ZONE],
        )

        group = group_at(page, BODY_C_PT)
        self.assertEqual(group["section"], ensemble.BODY)
        self.assertFalse(group["footnote_doubt"])
        self.assertEqual(page["footnotes"], "")
        self.assertEqual(page["counts"]["footnote_doubt"], 0)


class TestTheOrderEdit(TestCase):
    """An ``ORDER`` edit moves the listed blocks inside their slots."""

    def three(self):
        return alike(
            (BODY_A_PT, "First."),
            (BODY_B_PT, "Second."),
            (BODY_C_PT, "Third."),
        )

    def test_a_listed_pair_swaps(self):
        dots, mistral = self.three()

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.ORDER,
                    id=4,
                    section=ensemble.BODY,
                    order=[BODY_B_PT, BODY_A_PT, BODY_C_PT],
                )
            ],
        )

        self.assertEqual(page["text"], "Second.\n\nFirst.\n\nThird.")
        self.assertEqual(page["order_edits"], {ensemble.BODY: 4})
        self.assertEqual(page["unresolved_edits"], [])

    def test_a_block_the_list_does_not_name_keeps_its_slot(self):
        dots, mistral = self.three()

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.ORDER,
                    section=ensemble.BODY,
                    order=[BODY_C_PT, BODY_A_PT],
                )
            ],
        )

        self.assertEqual(page["text"], "Third.\n\nSecond.\n\nFirst.")

    def test_a_listed_box_that_is_gone_is_unresolved_and_the_rest_moves(self):
        dots, mistral = alike((BODY_A_PT, "First."), (BODY_B_PT, "Second."))

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.ORDER,
                    section=ensemble.BODY,
                    order=[BODY_B_PT, BODY_C_PT, BODY_A_PT],
                )
            ],
        )

        self.assertEqual(page["text"], "Second.\n\nFirst.")
        self.assertEqual(
            [e["reason"] for e in page["unresolved_edits"]],
            [ensemble.EDIT_NO_GROUP],
        )

    def test_a_block_of_the_other_section_is_not_counted(self):
        """A section edit took it out of this list."""
        dots, mistral = self.three()

        page = page_with(
            dots,
            mistral,
            [
                entry(
                    OpinionEdit.Kind.SECTION,
                    BODY_C_PT,
                    id=2,
                    section=ensemble.FOOTNOTES,
                ),
                entry(
                    OpinionEdit.Kind.ORDER,
                    id=3,
                    section=ensemble.BODY,
                    order=[BODY_B_PT, BODY_A_PT, BODY_C_PT],
                ),
            ],
        )

        self.assertEqual(page["text"], "Second.\n\nFirst.")
        self.assertEqual(page["footnotes"], "Third.")
        self.assertEqual(page["unresolved_edits"], [])


class TestTheSwappedOrder(TestCase):
    """``opinion_edits.swapped_order``: one place, inside the section."""

    def groups(self):
        return [
            {"id": 0, "box_pt": BODY_A_PT},
            {"id": 1, "box_pt": BODY_B_PT},
        ]

    def test_down_swaps_with_the_next(self):
        self.assertEqual(
            opinion_edits.swapped_order(self.groups(), 0, 1),
            [BODY_B_PT, BODY_A_PT],
        )

    def test_the_edges_do_not_move(self):
        self.assertIsNone(opinion_edits.swapped_order(self.groups(), 0, -1))
        self.assertIsNone(opinion_edits.swapped_order(self.groups(), 1, 1))
        self.assertIsNone(opinion_edits.swapped_order(self.groups(), 9, 1))


class TestTheFold(TestCase):
    def test_the_fold_is_the_whitespace_rule_of_an_engine(self):
        self.assertEqual(
            opinion_edits.fold("  The  court\r\n\r\n held.\t "),
            "The court\nheld.",
        )


# ── the write ─────────────────────────────────────────────────────────
class EditTestCase(EnsembleTestCase):
    """An opinion whose first page holds one block the engines split.

    The Mistral reading of body A of page 1 of the volume (the first
    page of the opinion) says other words, so that block is a vote of
    two engines, a blocking level. Body B reads alike.
    """

    def setUp(self):
        super().setUp()
        mistral = self.objects[self.apply_run.extract_key]
        mistral["pages"][1]["blocks"][1]["content"] = "bodv A l"
        self.run_ensemble()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.opinion.refresh_from_db()

    def page(self, index=0) -> dict:
        self.opinion.refresh_from_db()
        return self.stored()["pages"][index]

    def split_group(self) -> dict:
        return next(g for g in self.page()["groups"] if g["level"])

    def alike_group(self) -> dict:
        return next(g for g in self.page()["groups"] if not g["level"])

    def write_edit(self, **fields) -> OpinionEdit:
        page = self.page()
        address = ensemble._address(page)
        values = {
            "source_edit_id": address[0],
            "source_page": address[1],
            "page_in_opinion": 0,
            "glue_revision": self.opinion.glue_revision,
        }
        values.update(fields)
        return opinion_edits.supersede(self.opinion, None, **values)


@override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
class TestTheWriteWithEdits(EditTestCase):
    def test_a_standing_text_edit_is_the_text_of_the_row(self):
        group = self.split_group()
        self.write_edit(
            kind=OpinionEdit.Kind.TEXT,
            box_pt=group["box_pt"],
            base_text=group["text"],
            text="body A 1, as printed",
        )
        self.opinion.refresh_from_db()

        ensemble.rerun(self.opinion)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.edit_revision, 1)
        self.assertEqual(self.opinion.ensemble_edit_revision, 1)
        self.assertTrue(
            ensemble.document_key(self.opinion).endswith("ensemble.e1.json")
        )
        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertIn("body A 1, as printed", row.text)
        self.assertEqual(self.stored()["edit_revision"], 1)
        self.assertFalse(
            OpinionFinding.objects.filter(
                opinion=self.opinion,
                page_in_opinion=0,
                check_name=OpinionCheck.NO_MAJORITY,
            ).exists()
        )

    def test_an_edit_the_text_does_not_hold_is_an_error_card(self):
        self.write_edit(
            kind=OpinionEdit.Kind.TEXT,
            box_pt=self.split_group()["box_pt"],
            base_text="words nobody read",
            text="x",
        )
        self.opinion.refresh_from_db()

        ensemble.rerun(self.opinion)

        card = OpinionFinding.objects.get(
            opinion=self.opinion, check_name=OpinionCheck.UNRESOLVED_EDIT
        )
        self.assertEqual(card.severity, "error")
        self.assertEqual(card.page_in_opinion, 0)
        self.assertIn("other words", card.message)
        with self.assertRaises(opinion_findings.UndismissableOpinionFinding):
            opinion_findings.dismiss(self.opinion, card, None)

    def test_an_edit_of_a_page_the_opinion_lost_is_a_card_of_the_opinion(self):
        opinion_edits.supersede(
            self.opinion,
            None,
            kind=OpinionEdit.Kind.SECTION,
            source_edit_id=None,
            source_page=99,
            page_in_opinion=0,
            box_pt=BODY_A_PT,
            section=ensemble.FOOTNOTES,
            glue_revision=self.opinion.glue_revision,
        )
        self.opinion.refresh_from_db()

        ensemble.rerun(self.opinion)

        card = OpinionFinding.objects.get(
            opinion=self.opinion, check_name=OpinionCheck.UNRESOLVED_EDIT
        )
        self.assertIsNone(card.page_in_opinion)
        self.assertIn("no longer in this opinion", card.message)

    def test_an_edit_written_during_a_build_takes_the_build_back(self):
        def edit_now(*args, **kwargs):
            Opinion.objects.filter(pk=self.opinion.pk).update(edit_revision=5)
            return 0

        with (
            patch("scanning.ensemble.rebuild_findings", side_effect=edit_now),
            self.assertRaises(ensemble.RevisionMoved),
        ):
            ensemble.rerun(self.opinion)

    def test_a_new_stamp_deletes_the_document_it_replaced(self):
        old_key = ensemble.document_key(self.opinion)
        self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=self.alike_group()["box_pt"],
            section=ensemble.FOOTNOTES,
        )
        self.opinion.refresh_from_db()

        with patch("scanning.s3_sync.delete_objects") as delete:
            ensemble.rerun(self.opinion)

        delete.assert_called_once_with([old_key])
        self.opinion.refresh_from_db()
        self.assertNotEqual(ensemble.document_key(self.opinion), old_key)

    def test_a_build_of_the_same_revisions_deletes_nothing(self):
        with patch("scanning.s3_sync.delete_objects") as delete:
            ensemble.rerun(self.opinion)

        delete.assert_not_called()

    def test_a_build_that_lost_the_swap_deletes_its_own_document(self):
        self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=self.alike_group()["box_pt"],
            section=ensemble.FOOTNOTES,
        )
        self.opinion.refresh_from_db()
        lost = ensemble.document_key(self.opinion, 1)

        def edit_now(*args, **kwargs):
            Opinion.objects.filter(pk=self.opinion.pk).update(edit_revision=5)
            return 0

        with (
            patch("scanning.ensemble.rebuild_findings", side_effect=edit_now),
            patch("scanning.s3_sync.delete_objects") as delete,
            self.assertRaises(ensemble.RevisionMoved),
        ):
            ensemble.rerun(self.opinion)

        delete.assert_called_once_with([lost])

    def test_a_lost_build_spares_the_key_a_winner_stamped(self):
        """Two builds of the same revisions write one key.

        The winner committed its stamp before this build reached its
        swap, and an edit then raised the revision under this build.
        """
        self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=self.alike_group()["box_pt"],
            section=ensemble.FOOTNOTES,
        )
        Opinion.objects.filter(pk=self.opinion.pk).update(
            ensemble_edit_revision=1
        )
        self.opinion.refresh_from_db()

        def moved(*args, **kwargs):
            Opinion.objects.filter(pk=self.opinion.pk).update(edit_revision=2)
            return 0

        with (
            patch("scanning.ensemble.rebuild_findings", side_effect=moved),
            patch("scanning.s3_sync.delete_objects") as delete,
            self.assertRaises(ensemble.RevisionMoved),
        ):
            ensemble.rerun(self.opinion)

        delete.assert_not_called()

    def test_a_row_whose_edit_the_text_does_not_hold_is_due(self):
        self.assertNotIn(self.opinion, ensemble.due())
        Opinion.objects.filter(pk=self.opinion.pk).update(edit_revision=1)

        self.assertIn(self.opinion, ensemble.due())


class TestTheWriters(EditTestCase):
    def test_a_second_move_supersedes_the_first(self):
        first = self.write_edit(
            kind=OpinionEdit.Kind.ORDER,
            section=ensemble.BODY,
            order=[BODY_B_PT, BODY_A_PT],
        )
        second = self.write_edit(
            kind=OpinionEdit.Kind.ORDER,
            section=ensemble.BODY,
            order=[BODY_A_PT, BODY_B_PT],
        )

        first.refresh_from_db()
        self.assertIsNotNone(first.withdrawn_at)
        self.assertEqual(second.replaces, first)
        self.assertEqual(
            OpinionEdit.objects.filter(withdrawn_at__isnull=True).count(), 1
        )
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.edit_revision, 2)

    def test_a_block_edit_supersedes_the_edit_of_the_same_box(self):
        box = self.split_group()["box_pt"]
        first = self.write_edit(
            kind=OpinionEdit.Kind.SECTION, box_pt=box, section="footnotes"
        )
        other = self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=self.alike_group()["box_pt"],
            section="footnotes",
        )
        second = self.write_edit(
            kind=OpinionEdit.Kind.SECTION, box_pt=box, section="text"
        )

        first.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(second.replaces, first)
        self.assertIsNotNone(first.withdrawn_at)
        self.assertIsNone(other.withdrawn_at)

    def test_withdraw_stamps_and_deletes_nothing(self):
        edit = self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=BODY_A_PT,
            section="footnotes",
        )

        self.assertTrue(opinion_edits.withdraw(self.opinion, edit, None))
        self.assertFalse(opinion_edits.withdraw(self.opinion, edit, None))

        edit.refresh_from_db()
        self.assertIsNotNone(edit.withdrawn_at)
        self.assertEqual(OpinionEdit.objects.count(), 1)
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.edit_revision, 2)


# ── the endpoints ─────────────────────────────────────────────────────
@override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
class TestTheEndpoints(EditTestCase, ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def url(self, name, scan=None) -> str:
        return reverse(
            name,
            kwargs={
                "pk": (scan or self.scan).pk,
                "opinion_pk": self.opinion.pk,
            },
        )

    def post(self, name, scan=None, **body):
        document = self.stored()
        body.setdefault("glue_revision", document["opinion"]["glue_revision"])
        body.setdefault("edit_revision", document.get("edit_revision", 0))
        self.forget()
        return self.client.post(
            self.url(name, scan), body, content_type="application/json"
        )

    def forget(self):
        """Drop the messages a page would have shown after a reload.

        The viewer reloads after every answer, and the page shows the
        messages and spends them. A test renders no page, so each
        request starts with none.
        """
        self.client.cookies.pop("messages", None)
        session = self.client.session
        session.pop("_messages", None)
        session.save()

    def undo(self, edit_id):
        self.forget()
        return self.client.post(
            self.url("withdraw_opinion_edit"),
            {"edit_id": edit_id},
            content_type="application/json",
        )

    def assertAnswered(self, response, status, level):
        """One Django message of the level, and the same line in JSON."""
        self.assertEqual(response.status_code, status)
        said = [
            (message.level, message.message)
            for message in get_messages(response.wsgi_request)
        ]
        self.assertEqual(len(said), 1, said)
        self.assertEqual(said[0][0], level)
        self.assertEqual(said[0][1], response.json()["message"])

    def test_a_text_edit_is_saved_and_written(self):
        group = self.split_group()

        response = self.post(
            "edit_opinion_text",
            page_in_opinion=0,
            group_id=group["id"],
            text="  body A 1,\r\n as printed ",
        )

        self.assertAnswered(response, 200, messages.SUCCESS)
        edit = OpinionEdit.objects.get()
        self.assertEqual(edit.text, "body A 1,\nas printed")
        self.assertEqual(edit.base_text, group["text"])
        self.assertEqual(edit.box_pt, group["box_pt"])
        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertIn("body A 1,\nas printed", row.text)
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ensemble_edit_revision, 1)

    def test_a_unanimous_block_takes_no_text_edit(self):
        response = self.post(
            "edit_opinion_text",
            page_in_opinion=0,
            group_id=self.alike_group()["id"],
            text="anything",
        )

        self.assertAnswered(response, 409, messages.ERROR)
        self.assertIn("alike", response.json()["message"])
        self.assertFalse(OpinionEdit.objects.exists())

    def test_an_empty_or_unchanged_text_is_refused(self):
        group = self.split_group()
        for text in ("   ", group["text"]):
            response = self.post(
                "edit_opinion_text",
                page_in_opinion=0,
                group_id=group["id"],
                text=text,
            )
            self.assertAnswered(response, 409, messages.ERROR)
        self.assertFalse(OpinionEdit.objects.exists())

    def test_an_edited_block_asks_for_the_undo_first(self):
        group = self.split_group()
        self.post(
            "edit_opinion_text",
            page_in_opinion=0,
            group_id=group["id"],
            text="body A one",
        )
        edited = next(g for g in self.page()["groups"] if g.get("human"))

        response = self.post(
            "edit_opinion_text",
            page_in_opinion=0,
            group_id=edited["id"],
            text="body A 1.",
        )

        self.assertAnswered(response, 409, messages.ERROR)
        self.assertIn("Undo it first", response.json()["message"])

    def test_a_page_drawn_over_another_text_is_refused(self):
        response = self.post(
            "edit_opinion_text",
            page_in_opinion=0,
            group_id=self.split_group()["id"],
            text="body A 1",
            edit_revision=3,
        )

        self.assertAnswered(response, 409, messages.ERROR)
        self.assertIn("changed since", response.json()["message"])

    def test_a_closed_opinion_takes_no_edit(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        response = self.post(
            "edit_opinion_section",
            page_in_opinion=0,
            group_id=self.alike_group()["id"],
            section="footnotes",
        )

        self.assertAnswered(response, 409, messages.ERROR)

    def test_an_opinion_of_another_scan_is_a_404_with_a_message(self):
        response = self.post(
            "edit_opinion_section",
            scan=ScanFactory(),
            page_in_opinion=0,
            group_id=0,
            section="footnotes",
        )

        self.assertAnswered(response, 404, messages.ERROR)

    def test_a_body_the_page_does_not_send_is_a_400(self):
        response = self.client.post(
            self.url("edit_opinion_text"),
            "not json",
            content_type="application/json",
        )

        self.assertAnswered(response, 400, messages.ERROR)

    def test_a_block_goes_to_the_footnotes_and_back_with_the_undo(self):
        group = self.alike_group()

        response = self.post(
            "edit_opinion_section",
            page_in_opinion=0,
            group_id=group["id"],
            section="footnotes",
        )

        self.assertAnswered(response, 200, messages.SUCCESS)
        moved = group_at(self.page(), group["box_pt"])
        self.assertEqual(moved["section"], "footnotes")
        self.assertTrue(moved["section_edit"])

        response = self.undo(moved["section_edit"])

        self.assertAnswered(response, 200, messages.SUCCESS)
        self.assertEqual(
            group_at(self.page(), group["box_pt"])["section"], "text"
        )
        self.assertEqual(
            OpinionEdit.objects.filter(withdrawn_at__isnull=False).count(), 1
        )

    def test_an_edit_whose_block_is_gone_is_undone_from_its_card(self):
        """The one way out of an ``UNRESOLVED_EDIT`` card (#376)."""
        edit = self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=[36.0, 600.0, 100.0, 700.0],
            section="footnotes",
        )
        self.opinion.refresh_from_db()
        ensemble.rerun(self.opinion)
        self.assertEqual(
            self.page()["unresolved_edits"][0]["edit_id"], edit.pk
        )
        self.assertTrue(
            OpinionFinding.objects.filter(
                check_name=OpinionCheck.UNRESOLVED_EDIT
            ).exists()
        )

        response = self.undo(edit.pk)

        self.assertAnswered(response, 200, messages.SUCCESS)
        edit.refresh_from_db()
        self.assertIsNotNone(edit.withdrawn_at)
        self.assertEqual(self.page()["unresolved_edits"], [])
        self.assertFalse(
            OpinionFinding.objects.filter(
                check_name=OpinionCheck.UNRESOLVED_EDIT
            ).exists()
        )

    def test_a_second_undo_is_refused(self):
        self.post(
            "edit_opinion_section",
            page_in_opinion=0,
            group_id=self.alike_group()["id"],
            section="footnotes",
        )
        edit = OpinionEdit.objects.get()
        opinion_edits.withdraw(self.opinion, edit, None)

        response = self.undo(edit.pk)

        self.assertAnswered(response, 409, messages.ERROR)

    def test_a_block_moves_down_and_the_top_block_does_not_move_up(self):
        groups = [g for g in self.page()["groups"] if g["section"] == "text"]
        first, second = groups[0], groups[1]

        response = self.post(
            "move_opinion_block",
            page_in_opinion=0,
            group_id=first["id"],
            direction="down",
        )

        self.assertAnswered(response, 200, messages.SUCCESS)
        after = [g["box_pt"] for g in self.page()["groups"]]
        self.assertLess(
            after.index(second["box_pt"]), after.index(first["box_pt"])
        )
        top = self.page()["groups"][0]

        response = self.post(
            "move_opinion_block",
            page_in_opinion=0,
            group_id=top["id"],
            direction="up",
        )

        self.assertAnswered(response, 409, messages.ERROR)
        self.assertIn("edge", response.json()["message"])

    def test_a_direction_that_is_no_string_is_a_400(self):
        for direction in ([], {"up": 1}, 1):
            response = self.post(
                "move_opinion_block",
                page_in_opinion=0,
                group_id=self.alike_group()["id"],
                direction=direction,
            )

            self.assertAnswered(response, 400, messages.ERROR)
        self.assertFalse(OpinionEdit.objects.exists())

    def test_a_build_that_fails_keeps_the_edit_and_warns(self):
        with patch(
            "scanning.ensemble.rerun",
            side_effect=ensemble.TransientFault("the bucket is away"),
        ):
            response = self.post(
                "edit_opinion_section",
                page_in_opinion=0,
                group_id=self.alike_group()["id"],
                section="footnotes",
            )

        self.assertAnswered(response, 200, messages.WARNING)
        self.assertNotIn("bucket is away", response.json()["message"])
        self.assertTrue(OpinionEdit.objects.exists())
        self.assertIn(self.opinion, ensemble.due())

    def test_an_edit_the_text_does_not_hold_yet_holds_the_next(self):
        """A build that failed: the button writes it, a reload does not."""
        Opinion.objects.filter(pk=self.opinion.pk).update(edit_revision=1)

        response = self.post(
            "edit_opinion_section",
            page_in_opinion=0,
            group_id=self.alike_group()["id"],
            section="footnotes",
        )

        self.assertAnswered(response, 409, messages.ERROR)
        self.assertIn(
            "Read the OCR documents again", response.json()["message"]
        )
        self.assertFalse(OpinionEdit.objects.exists())

    def test_the_edits_refuse_a_get(self):
        for name in (
            "edit_opinion_text",
            "edit_opinion_section",
            "move_opinion_block",
            "withdraw_opinion_edit",
        ):
            self.assertEqual(self.client.get(self.url(name)).status_code, 405)

    def test_the_review_page_offers_the_edits_while_ready(self):
        response = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

        self.assertContains(response, 'data-can-edit="1"')
        self.assertContains(response, self.url("edit_opinion_text"))

        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )
        response = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

        self.assertContains(response, 'data-can-edit=""')
