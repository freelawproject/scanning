"""Tests for the blockquote edits of review 3 (issue #419).

A curator says whether a block is a blockquote: the whole block, over
the zone of the detections, or one span of its text. The edit is an
``OpinionEdit`` of kind ``BLOCKQUOTE``, and the ensemble applies it at
every build. Three groups of tests here:

- the build (``ensemble.build_page``, ``ensemble.blockquote_runs``)
  and the tagged text (``markup.serialize``), over plain dicts;
- the snap of a selection (``opinion_edits.snap_span``);
- the endpoint, its refusals and the Undo.
"""

from django.contrib import messages
from django.test import TestCase

from scanning import ensemble, markup, opinion_edits, views_api
from scanning.models import OpinionEdit
from scanning.tests import test_opinion_edits
from scanning.tests.test_ensemble import (
    BODY_A_PT,
    BODY_B_PT,
    engine_page,
)
from scanning.tests.test_opinion_edits import alike, entry, group_at

QUOTE_TEXT = (
    '"(1) Dangerous weapon means any weapon, device, instrument, material '
    'or substance."'
)
BODY_TEXT = "Defendant points to several statutes."
BLOCK_TEXT = f"{QUOTE_TEXT} {BODY_TEXT}"

#: A blockquote zone over body B, in points.
QUOTE_ZONE = [30.0, 350.0, 300.0, 510.0]


def build(specs, edits=(), quotes=()) -> dict:
    """The ensemble of one page, read alike by two engines."""
    dots, mistral = alike(*specs)
    pages = {
        "dots_mocr": engine_page(dots, quotes=quotes),
        "mistral_ocr": engine_page(mistral, quotes=quotes),
    }
    return ensemble.build_page(pages, 0, list(edits))


def quote_edit(box, **fields) -> dict:
    return entry(OpinionEdit.Kind.BLOCKQUOTE, box, **fields)


class TestTheWholeBlock(TestCase):
    def test_a_block_with_no_zone_becomes_a_quote(self):
        page = build(
            [(BODY_A_PT, "The court held."), (BODY_B_PT, QUOTE_TEXT)],
            [quote_edit(BODY_B_PT, id=5, quoted=True)],
        )

        group = group_at(page, BODY_B_PT)
        self.assertTrue(group["blockquote"])
        self.assertEqual(group["quote_edit"], 5)
        self.assertIsNone(group["quote_span"])
        self.assertEqual(
            page["blockquotes"],
            [
                {
                    "start": group["start"],
                    "end": group["end"],
                    "groups": [group["id"]],
                    "list_groups": [],
                }
            ],
        )

    def test_a_block_in_the_zone_is_no_quote_when_a_person_says_so(self):
        specs = [(BODY_A_PT, "The court held."), (BODY_B_PT, QUOTE_TEXT)]
        self.assertTrue(
            group_at(build(specs, quotes=[QUOTE_ZONE]), BODY_B_PT)[
                "blockquote"
            ]
        )

        page = build(
            specs, [quote_edit(BODY_B_PT, quoted=False)], quotes=[QUOTE_ZONE]
        )

        self.assertFalse(group_at(page, BODY_B_PT)["blockquote"])
        self.assertEqual(page["blockquotes"], [])

    def test_an_edit_of_a_block_now_in_the_footnotes_is_unresolved(self):
        page = build(
            [(BODY_A_PT, "The court held."), (BODY_B_PT, "1 See it.")],
            [
                entry(
                    OpinionEdit.Kind.SECTION,
                    BODY_B_PT,
                    id=3,
                    section=ensemble.FOOTNOTES,
                ),
                quote_edit(BODY_B_PT, id=4, quoted=True),
            ],
        )

        group = group_at(page, BODY_B_PT)
        self.assertFalse(group["blockquote"])
        self.assertIsNone(group["quote_edit"])
        self.assertEqual(
            [(e["edit_id"], e["reason"]) for e in page["unresolved_edits"]],
            [(4, ensemble.EDIT_IN_FOOTNOTES)],
        )


class TestAPartOfTheBlock(TestCase):
    """The example of the issue: one box over a quoted paragraph and a
    paragraph of the body (opinion 1674 of scan 3541)."""

    def span_edit(self, **fields):
        fields.setdefault("span", [0, len(QUOTE_TEXT)])
        fields.setdefault("base_text", BLOCK_TEXT)
        return quote_edit(BODY_B_PT, id=6, quoted=True, **fields)

    def test_the_run_is_the_span(self):
        page = build(
            [(BODY_A_PT, "The court held."), (BODY_B_PT, BLOCK_TEXT)],
            [self.span_edit()],
        )

        group = group_at(page, BODY_B_PT)
        self.assertFalse(group["blockquote"])
        self.assertEqual(group["quote_span"], [0, len(QUOTE_TEXT)])
        self.assertEqual(group["quote_edit"], 6)
        [run] = page["blockquotes"]
        self.assertEqual(page["text"][run["start"] : run["end"]], QUOTE_TEXT)
        self.assertEqual(run["groups"], [group["id"]])

    def test_the_tagged_text_quotes_the_span_alone(self):
        page = build(
            [(BODY_A_PT, "The court held."), (BODY_B_PT, BLOCK_TEXT)],
            [self.span_edit()],
        )

        marks = [
            markup.Mark(m["start"], m["end"], m["kind"])
            for m in ensemble._marks(page)
            if m["section"] == ensemble.BODY
        ]
        tagged = markup.serialize(markup.Parsed(page["text"], marks))

        self.assertIn(
            f"<blockquote>{QUOTE_TEXT}</blockquote> {BODY_TEXT}",
            tagged,
        )

    def test_a_span_that_ends_the_text_goes_on_into_the_next_quote(self):
        tail = "The court held that the words are the words."
        page = build(
            [(BODY_A_PT, BLOCK_TEXT), (BODY_B_PT, tail)],
            [
                quote_edit(
                    BODY_A_PT,
                    id=7,
                    quoted=True,
                    span=[len(QUOTE_TEXT) + 1, len(BLOCK_TEXT)],
                    base_text=BLOCK_TEXT,
                ),
                quote_edit(BODY_B_PT, id=8, quoted=True),
            ],
        )

        [run] = page["blockquotes"]
        self.assertEqual(
            page["text"][run["start"] : run["end"]], f"{BODY_TEXT}\n\n{tail}"
        )
        self.assertEqual(len(run["groups"]), 2)

    def test_a_span_in_the_middle_starts_a_quote_of_its_own(self):
        before = "The rule reads."
        page = build(
            [(BODY_A_PT, before), (BODY_B_PT, BLOCK_TEXT)],
            [
                quote_edit(BODY_A_PT, id=7, quoted=True),
                quote_edit(
                    BODY_B_PT,
                    id=8,
                    quoted=True,
                    span=[len(QUOTE_TEXT) + 1, len(BLOCK_TEXT)],
                    base_text=BLOCK_TEXT,
                ),
            ],
        )

        self.assertEqual(
            [page["text"][r["start"] : r["end"]] for r in page["blockquotes"]],
            [before, BODY_TEXT],
        )

    def test_a_span_that_starts_the_text_goes_on_from_the_quote_before(self):
        before = "The rule reads."
        page = build(
            [(BODY_A_PT, before), (BODY_B_PT, BLOCK_TEXT)],
            [quote_edit(BODY_A_PT, id=7, quoted=True), self.span_edit()],
        )

        [run] = page["blockquotes"]
        self.assertEqual(
            page["text"][run["start"] : run["end"]],
            f"{before}\n\n{QUOTE_TEXT}",
        )

    def test_other_words_hold_the_edit(self):
        """The span is offsets into the text the curator saw, so a
        build over another text, even one the vote reads alike, holds
        it."""
        page = build(
            [(BODY_A_PT, "The court held."), (BODY_B_PT, BLOCK_TEXT)],
            [self.span_edit(base_text=BLOCK_TEXT.replace('"', "“", 1))],
        )

        group = group_at(page, BODY_B_PT)
        self.assertIsNone(group["quote_span"])
        self.assertIsNone(group["quote_edit"])
        self.assertEqual(
            [(e["edit_id"], e["reason"]) for e in page["unresolved_edits"]],
            [(6, ensemble.EDIT_BASE_CHANGED)],
        )
        self.assertEqual(page["blockquotes"], [])


class TestTheSnap(TestCase):
    TEXT = "The rule reads plainly."

    def test_a_selection_inside_words_takes_the_whole_words(self):
        self.assertEqual(opinion_edits.snap_span(self.TEXT, 5, 11), (4, 14))

    def test_the_spaces_at_the_edges_are_left_out(self):
        self.assertEqual(opinion_edits.snap_span(self.TEXT, 3, 9), (4, 8))

    def test_no_word_is_none(self):
        self.assertIsNone(opinion_edits.snap_span(self.TEXT, 3, 4))
        self.assertIsNone(opinion_edits.snap_span(self.TEXT, 5, 5))

    def test_a_selection_past_the_text_is_held_inside_it(self):
        self.assertEqual(
            opinion_edits.snap_span(self.TEXT, -4, 99), (0, len(self.TEXT))
        )


class TestTheEndpoint(test_opinion_edits.TestTheEndpoints):
    """``edit_opinion_blockquote``, over the fixture of the other edit
    endpoints. The module is imported and not the class, so the runner
    does not find the class here and run its tests a second time."""

    # Nor through this subclass: a name bound to None is no test.
    for _name in dir(test_opinion_edits.TestTheEndpoints):
        if _name.startswith("test_"):
            locals()[_name] = None
    del _name

    def quote(self, group, **body):
        return self.post(
            "edit_opinion_blockquote",
            page_in_opinion=0,
            group_id=group["id"],
            **body,
        )

    def test_a_block_becomes_a_quote_and_back_with_the_undo(self):
        group = self.alike_group()

        response = self.quote(group, quoted=True)

        self.assertAnswered(response, 200, messages.SUCCESS)
        self.assertEqual(
            response.json()["message"],
            views_api.EDIT_QUOTE_SAVED_MESSAGE["whole"],
        )
        quoted = group_at(self.page(), group["box_pt"])
        self.assertTrue(quoted["blockquote"])
        self.assertTrue(quoted["quote_edit"])

        response = self.undo(quoted["quote_edit"])

        self.assertAnswered(response, 200, messages.SUCCESS)
        self.assertFalse(group_at(self.page(), group["box_pt"])["blockquote"])

    def test_a_selection_is_snapped_and_saved_with_the_text(self):
        group = self.alike_group()
        text = group["text"]
        cut = text.index(" ")

        response = self.quote(group, quoted=True, span=[1, cut])

        self.assertAnswered(response, 200, messages.SUCCESS)
        self.assertEqual(
            response.json()["message"],
            views_api.EDIT_QUOTE_SAVED_MESSAGE["span"],
        )
        row = OpinionEdit.objects.get(kind=OpinionEdit.Kind.BLOCKQUOTE)
        self.assertEqual(row.span, [0, cut])
        self.assertEqual(row.base_text, text)
        self.assertEqual(
            group_at(self.page(), group["box_pt"])["quote_span"], [0, cut]
        )

    def test_a_selection_of_the_whole_text_is_the_whole_block(self):
        group = self.alike_group()

        response = self.quote(group, quoted=True, span=[0, len(group["text"])])

        self.assertAnswered(response, 200, messages.SUCCESS)
        row = OpinionEdit.objects.get(kind=OpinionEdit.Kind.BLOCKQUOTE)
        self.assertIsNone(row.span)
        self.assertEqual(row.base_text, "")
        self.assertTrue(group_at(self.page(), group["box_pt"])["blockquote"])

    def test_a_second_edit_supersedes_the_first(self):
        group = self.alike_group()
        self.quote(group, quoted=True)

        response = self.quote(
            group_at(self.page(), group["box_pt"]), quoted=False
        )

        self.assertAnswered(response, 200, messages.SUCCESS)
        self.assertEqual(
            OpinionEdit.objects.filter(
                kind=OpinionEdit.Kind.BLOCKQUOTE, withdrawn_at__isnull=True
            ).count(),
            1,
        )
        self.assertFalse(group_at(self.page(), group["box_pt"])["blockquote"])

    def test_the_refusals(self):
        group = self.alike_group()
        for body, status, said in (
            ({"quoted": False}, 409, views_api.EDIT_QUOTE_UNCHANGED_MESSAGE),
            ({"quoted": "yes"}, 400, views_api.EDIT_BAD_REQUEST_MESSAGE),
            (
                {"quoted": False, "span": [0, 2]},
                400,
                views_api.EDIT_BAD_REQUEST_MESSAGE,
            ),
            (
                {"quoted": True, "span": [0, True]},
                400,
                views_api.EDIT_BAD_REQUEST_MESSAGE,
            ),
            (
                {"quoted": True, "span": [3, 3]},
                409,
                views_api.EDIT_QUOTE_NO_WORD_MESSAGE,
            ),
        ):
            with self.subTest(body=body):
                response = self.quote(group, **body)
                self.assertAnswered(response, status, messages.ERROR)
                self.assertEqual(response.json()["message"], said)
        self.assertFalse(OpinionEdit.objects.exists())

    def test_a_footnote_takes_no_quote(self):
        group = self.alike_group()
        self.write_edit(
            kind=OpinionEdit.Kind.SECTION,
            box_pt=group["box_pt"],
            section=ensemble.FOOTNOTES,
        )
        self.run_ensemble()

        response = self.quote(
            group_at(self.page(), group["box_pt"]), quoted=True
        )

        self.assertAnswered(response, 409, messages.ERROR)
        self.assertEqual(
            response.json()["message"], views_api.EDIT_QUOTE_FOOTNOTE_MESSAGE
        )
