"""Tests for the OCR ensemble over one opinion (issue #365).

Eight groups:

- the alignment (``ensemble.align_page``): the N to M merge, the
  page-scale guard, the weak group;
- the reading order (``ensemble.place``): the bands, the column
  boundary, the full-width box;
- the vote (``ensemble.resolve``): the four ways a group resolves, and
  the word vote;
- the document (``ensemble.build_document``): the text, the offsets,
  the drop of an excluded group, a page nobody read;
- the rows (``ensemble.write_rows``): the address, the cache, the
  human text;
- the findings (``ensemble.rebuild_findings``): the three checks, the
  dismissal, the stale cards of the creation;
- the ledger and the pass: the stamp, the gate, the attempts;
- the button, the command and the two routes the review page reads.
"""

import json
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from scanning import ensemble, opinion_ocr, opinion_pdf, views_process
from scanning.factories import ScanFactory
from scanning.models import (
    Issue,
    Opinion,
    OpinionCheck,
    OpinionFinding,
    OpinionFindingDismissal,
    OpinionReviewStatus,
    OpinionText,
    PageEdit,
    Status,
)
from scanning.tests.test_opinion_ocr import (
    BODY_A,
    BODY_B,
    OpinionOcrTestCase,
    block,
    footnote_band,
    mistral_document,
    to_pt,
)
from scanning.tests.test_views import ScanningTestCase

#: The page of the fixture, in points: 1700 by 2200 at 200 dpi.
WIDTH, HEIGHT = 612.0, 792.0

#: The three units of every fixture page, in points.
HEADER_PT = [36.0, 18.0, 288.0, 43.2]
BODY_A_PT = [36.0, 108.0, 288.0, 324.0]
BODY_B_PT = [36.0, 360.0, 288.0, 504.0]


def unit(
    engine: str,
    index: int,
    box,
    text: str,
    exclusion=None,
    share: float = 0.0,
    label: str = "Text",
    marks=(),
    kind: str = "paragraph",
    table=None,
) -> dict:
    """One engine's unit of a page, in the shape the alignment reads.

    ``label`` is the engine's own label of the unit (``Text``,
    ``Footnote``); ``kind`` and ``marks`` are the parse of #404.
    """
    return {
        "engine": engine,
        "id": index,
        "box_pt": [float(v) for v in box],
        "text": text,
        "type": label,
        "exclusion": exclusion,
        "share": share,
        "marks": [dict(mark) for mark in marks],
        "kind": kind,
        "table": table,
    }


def em(start, end) -> dict:
    return {"start": start, "end": end, "kind": "em"}


def strong(start, end) -> dict:
    return {"start": start, "end": end, "kind": "strong"}


def sup(start, end) -> dict:
    return {"start": start, "end": end, "kind": "sup"}


def group_of(*units) -> dict:
    """One aligned group over the given units, for the vote alone."""
    engines = {}
    for member in units:
        engines[member["engine"]] = {
            "ids": [member["id"]],
            "types": [member["type"]],
            "box_pt": member["box_pt"],
            "text": member["text"],
            "marks": list(member.get("marks") or []),
            "kind": member.get("kind") or "paragraph",
            "table": member.get("table"),
            "excluded": False,
            "reason": "",
            "partial": False,
        }
    return {"engines": engines}


def counts(**values) -> dict:
    """The counts of one page of a document, with the rest at zero."""
    base = ensemble._counts()
    base.update(values)
    return base


def page_of(page_in_opinion=0, text="the text", groups=(), **values) -> dict:
    """One page of a document, for the readers that take one."""
    entry = {
        "page_in_opinion": page_in_opinion,
        "page_index": page_in_opinion + 1,
        "pdf_page": page_in_opinion + 2,
        "source": {"kind": "original", "pdf_page": page_in_opinion + 2},
        "frame": {"width_pt": WIDTH, "height_pt": HEIGHT},
        "text": text,
        "engines": ["dots_mocr", "mistral_ocr"],
        "missing": [],
        "groups": list(groups),
        "dropped": [],
        "counts": counts(**values),
    }
    return entry


def document_of(*pages) -> dict:
    """A document over the given pages, for the readers that take one."""
    return {
        "schema_version": ensemble.SCHEMA_VERSION,
        "engines": ["dots_mocr", "mistral_ocr"],
        "pages": list(pages),
        "counts": counts(),
    }


# ── the alignment ────────────────────────────────────────────────────
class TestTheAlignment(TestCase):
    def test_one_box_and_two_boxes_make_one_group(self):
        """Containment, not IoU: two blocks inside one block are the
        same content, and the merge reads them in order."""
        units = [
            unit("dots_mocr", 0, (36, 108, 288, 324), "alpha beta"),
            unit("mistral_ocr", 0, (36, 108, 288, 200), "alpha"),
            unit("mistral_ocr", 1, (36, 210, 288, 324), "beta"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 1)
        merged = groups[0]["engines"]
        self.assertEqual(merged["mistral_ocr"]["text"], "alpha beta")
        self.assertEqual(merged["mistral_ocr"]["ids"], [0, 1])
        self.assertEqual(groups[0]["present"], ["dots_mocr", "mistral_ocr"])

    def test_a_box_that_reads_nothing_swallows_no_paragraph(self):
        """A picture box over the body would chain the page into one
        group and read it across the gutter, so it never links. It is
        still reported: one engine read nothing where the other read
        the text."""
        units = [
            unit("dots_mocr", 0, (40, 100, 570, 620), "", label="Picture"),
            unit("mistral_ocr", 0, (50, 110, 290, 300), "left one"),
            unit("mistral_ocr", 1, (50, 320, 290, 600), "left two"),
            unit("mistral_ocr", 2, (330, 110, 560, 300), "right one"),
            unit("mistral_ocr", 3, (330, 320, 560, 600), "right two"),
        ]

        placed = ensemble.place(
            ensemble.align_page(units, WIDTH, HEIGHT), WIDTH, HEIGHT
        )

        self.assertEqual(
            [ensemble.resolve(g)["text"] for g in placed],
            ["left one", "left two", "right one", "right two"],
        )
        silent = [g for g in placed if ensemble.resolve(g)["silent"]]
        self.assertEqual(len(silent), 1)

    def test_a_whole_page_block_with_text_links(self):
        """One engine reads the page as one block and the other as
        paragraphs. Held apart, the page would hold its text twice and
        no card would say so."""
        units = [
            unit("dots_mocr", 0, (0, 0, WIDTH, HEIGHT), "alpha beta"),
            unit("mistral_ocr", 0, (50, 110, 560, 300), "alpha"),
            unit("mistral_ocr", 1, (50, 320, 560, 600), "beta"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            ensemble.resolve(groups[0])["agreement"], ensemble.UNANIMOUS
        )
        self.assertTrue(groups[0]["page_scale"])

    def test_a_box_that_reads_nothing_and_covers_nothing_is_dropped(self):
        units = [
            unit("dots_mocr", 0, (0, 0, 100, 60), "", label="Picture"),
            unit("mistral_ocr", 0, (300, 400, 560, 600), "alpha"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            sorted(ensemble.resolve(g)["text"] for g in groups),
            ["", "alpha"],
        )

    def test_two_whole_page_boxes_make_one_group(self):
        """Two engines' reading of the same whole-page box is one
        group: held apart, it would write the page twice."""
        units = [
            unit("dots_mocr", 0, (0, 0, WIDTH, HEIGHT), "alpha"),
            unit("mistral_ocr", 0, (0, 0, WIDTH, HEIGHT), "alpha"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["page_scale"])

    def test_a_big_body_block_links_to_the_paragraphs_inside_it(self):
        """One block over the body of a page is 0.68 of it, and it is
        text. Held out of the graph it would write the body a second
        time beside the other engine's paragraphs."""
        units = [
            unit("dots_mocr", 0, (50, 80, 562, 720), "alpha beta gamma"),
            unit("mistral_ocr", 0, (50, 80, 562, 280), "alpha"),
            unit("mistral_ocr", 1, (50, 300, 562, 500), "beta"),
            unit("mistral_ocr", 2, (50, 520, 562, 720), "gamma"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["page_scale"])
        self.assertEqual(
            ensemble.resolve(groups[0])["agreement"], ensemble.UNANIMOUS
        )

    def test_two_engines_of_one_page_never_link_to_themselves(self):
        units = [
            unit("dots_mocr", 0, (36, 108, 288, 200), "alpha"),
            unit("dots_mocr", 1, (36, 110, 288, 202), "beta"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 2)

    def test_a_block_over_two_columns_reads_by_column(self):
        """One engine reads the page as one block; the other reads the
        paragraphs of both columns. The members of that group must not
        read across the gutter."""
        cells = [
            unit("dots_mocr", 0, (50, 100, 290, 300), "left one"),
            unit("dots_mocr", 1, (50, 320, 290, 700), "left two"),
            unit("dots_mocr", 2, (320, 100, 560, 300), "right one"),
            unit("dots_mocr", 3, (320, 320, 560, 700), "right two"),
        ]
        whole = unit("mistral_ocr", 4, (50, 100, 560, 700), "one block")

        groups = ensemble.align_page([*cells, whole], WIDTH, HEIGHT)

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0]["engines"]["dots_mocr"]["text"],
            "left one left two right one right two",
        )

    def test_an_image_placeholder_links_nothing(self):
        """Mistral writes a picture box as an image placeholder, so a
        box that reads nothing is not an empty string."""
        cells = [
            unit("dots_mocr", 0, (50, 100, 290, 300), "left one"),
            unit("dots_mocr", 1, (50, 320, 290, 700), "left two"),
            unit("dots_mocr", 2, (320, 100, 560, 300), "right one"),
            unit("dots_mocr", 3, (320, 320, 560, 700), "right two"),
        ]
        picture = unit(
            "mistral_ocr", 4, (50, 100, 560, 700), "![img-0.jpeg](img-0.jpeg)"
        )

        groups = ensemble.align_page([*cells, picture], WIDTH, HEIGHT)

        self.assertEqual(len(groups), 4)
        silent = [g for g in groups if "mistral_ocr" in g["engines"]]
        self.assertEqual(len(silent), 1)
        self.assertEqual(
            ensemble.resolve(silent[0])["silent"], ["mistral_ocr"]
        )

    def test_a_line_break_tag_links_nothing(self):
        cells = [unit("dots_mocr", 0, (50, 100, 290, 300), "left one")]
        tag = unit("mistral_ocr", 1, (50, 100, 560, 700), "<br>")

        groups = ensemble.align_page([*cells, tag], WIDTH, HEIGHT)

        self.assertEqual(groups[0]["engines"]["mistral_ocr"]["text"], "")

    def test_a_unit_of_marks_alone_reads_nothing(self):
        """A lone ``###`` is a heading mark with no heading."""
        groups = ensemble.align_page(
            [unit("dots_mocr", 0, (50, 100, 300, 200), "###")], WIDTH, HEIGHT
        )

        self.assertEqual(ensemble.resolve(groups[0])["text"], "")

    def test_a_group_whose_boxes_barely_match_is_weak(self):
        units = [
            unit("dots_mocr", 0, (0, 0, 300, 300), "alpha"),
            unit("mistral_ocr", 0, (0, 0, 100, 100), "alpha"),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["weak"])
        self.assertLess(groups[0]["alignment_iou"], ensemble.WEAK_IOU)

    def test_a_group_carries_the_exclusion_of_its_members(self):
        units = [
            unit("dots_mocr", 0, (36, 108, 288, 324), "alpha"),
            unit(
                "mistral_ocr",
                0,
                (36, 108, 288, 324),
                "alpha",
                exclusion={"reason": "redaction"},
                share=0.5,
            ),
        ]

        groups = ensemble.align_page(units, WIDTH, HEIGHT)

        self.assertTrue(groups[0]["excluded"])
        self.assertEqual(groups[0]["reason"], "redaction")
        self.assertTrue(groups[0]["partial"])


# ── the reading order ────────────────────────────────────────────────
class TestTheReadingOrder(TestCase):
    @staticmethod
    def box(index, box) -> dict:
        return {"box_pt": [float(v) for v in box], "id": index}

    def order(self, *boxes) -> list[int]:
        placed = ensemble.place(list(boxes), WIDTH, HEIGHT)
        return [group["id"] for group in placed]

    def test_a_two_column_page_reads_the_left_column_first(self):
        left_top = self.box(0, (50, 100, 300, 200))
        left_foot = self.box(1, (50, 300, 300, 400))
        right_top = self.box(2, (350, 100, 580, 200))
        right_foot = self.box(3, (350, 300, 580, 400))

        self.assertEqual(
            self.order(right_top, left_foot, right_foot, left_top),
            [0, 1, 2, 3],
        )

    def test_a_full_width_box_restarts_the_order_below_it(self):
        left_top = self.box(0, (50, 100, 300, 200))
        right_top = self.box(1, (350, 100, 580, 200))
        rule = self.box(2, (40, 250, 580, 280))
        left_foot = self.box(3, (50, 300, 300, 400))
        right_foot = self.box(4, (350, 300, 580, 400))

        self.assertEqual(
            self.order(left_top, right_top, rule, left_foot, right_foot),
            [0, 1, 2, 3, 4],
        )

    def test_the_head_and_the_foot_read_across(self):
        left_head = self.box(0, (50, 20, 200, 40))
        right_head = self.box(1, (400, 22, 560, 42))
        body = self.box(2, (50, 100, 560, 400))
        foot = self.box(3, (50, 760, 560, 780))

        self.assertEqual(
            self.order(foot, body, right_head, left_head), [0, 1, 2, 3]
        )

    def test_a_page_with_few_boxes_reads_as_one_column(self):
        first = self.box(0, (50, 100, 300, 200))
        second = self.box(1, (350, 300, 580, 400))

        self.assertIsNone(
            ensemble.column_boundary([first, second], WIDTH, HEIGHT)
        )
        self.assertEqual(self.order(second, first), [0, 1])

    def test_the_boundary_is_the_right_column_edge(self):
        boxes = [
            self.box(0, (50, 100, 300, 200)),
            self.box(1, (50, 300, 300, 400)),
            self.box(2, (350, 100, 580, 200)),
            self.box(3, (350, 300, 580, 400)),
        ]

        boundary = ensemble.column_boundary(boxes, WIDTH, HEIGHT)

        self.assertAlmostEqual(
            boundary, 350 - ensemble.EDGE_PAD * WIDTH, places=2
        )


# ── the vote ─────────────────────────────────────────────────────────
class TestTheVote(TestCase):
    @staticmethod
    def read(*texts) -> dict:
        names = ("dots_mocr", "mistral_ocr", "surya")
        return group_of(
            *[
                unit(names[index], 0, BODY_A_PT, text)
                for index, text in enumerate(texts)
            ]
        )

    def test_one_engine_alone_is_single(self):
        answer = ensemble.resolve(self.read("the court held"))

        self.assertEqual(answer["agreement"], ensemble.SINGLE)
        self.assertEqual(answer["source"], "dots_mocr")
        self.assertEqual(answer["text"], "the court held")

    def test_every_engine_agreeing_is_unanimous(self):
        answer = ensemble.resolve(
            self.read("the court held", "the court held", "the court held")
        )

        self.assertEqual(answer["agreement"], ensemble.UNANIMOUS)
        self.assertEqual(len(answer["agreeing"]), 3)

    def test_two_of_three_agreeing_is_a_majority(self):
        answer = ensemble.resolve(
            self.read("the court held", "the couit held", "the court held")
        )

        self.assertEqual(answer["agreement"], ensemble.MAJORITY)
        self.assertEqual(answer["text"], "the court held")
        self.assertEqual(answer["agreeing"], ["dots_mocr", "surya"])
        self.assertEqual(answer["tokens"], [])

    def test_three_readings_are_voted_word_by_word(self):
        """Each engine errs in its own place, so the region has no
        majority and the words do."""
        answer = ensemble.resolve(
            self.read(
                "alpha beta gamma delta",
                "alpha xeta gamma delta",
                "alpha beta gamma zelta",
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        self.assertEqual(answer["text"], "alpha beta gamma delta")
        self.assertEqual(answer["n_low_confidence"], 0)
        self.assertEqual(len(answer["tokens"]), 4)

    def test_a_word_no_majority_settles_is_marked(self):
        answer = ensemble.resolve(
            self.read(
                "alpha beta gamma", "alpha xeta gamma", "alpha zeta gamma"
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        self.assertEqual(answer["n_low_confidence"], 1)
        marked = [t for t in answer["tokens"] if t.get("low_confidence")]
        self.assertEqual([t["text"] for t in marked], ["beta"])

    def test_a_word_a_majority_settles_says_so(self):
        """#380: the reader must tell a word every engine read from a
        word two of three read, and the vote is the only rule for
        that."""
        answer = ensemble.resolve(
            self.read(
                "alpha beta gamma delta",
                "alpha xeta gamma delta",
                "alpha beta gamma zelta",
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        marked = [t["text"] for t in answer["tokens"] if t.get("majority")]
        self.assertEqual(marked, ["beta", "delta"])

    def test_a_word_the_base_never_read_says_so(self):
        """#380: a run the engines put in and a word no majority
        settled are both marked, and they read differently. The viewer
        names one of them, so the token must say which it is."""
        answer = ensemble.resolve(
            self.read(
                "the court held today",
                "the court plainly held today",
                "the court plainly held todya",
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        put_in = [t["text"] for t in answer["tokens"] if t.get("inserted")]
        self.assertEqual(put_in, ["plainly"])
        for token in answer["tokens"]:
            if token.get("inserted"):
                self.assertTrue(token.get("low_confidence"), token)

    def test_a_word_no_majority_settles_is_not_an_inserted_word(self):
        answer = ensemble.resolve(
            self.read(
                "alpha beta gamma", "alpha xeta gamma", "alpha zeta gamma"
            )
        )

        marked = [t for t in answer["tokens"] if t.get("low_confidence")]
        self.assertEqual([t["text"] for t in marked], ["beta"])
        self.assertIsNone(marked[0].get("inserted"))

    def test_a_word_every_engine_read_carries_no_flag(self):
        answer = ensemble.resolve(
            self.read(
                "alpha beta gamma delta",
                "alpha xeta gamma delta",
                "alpha beta gamma zelta",
            )
        )

        plain = [
            token["text"]
            for token in answer["tokens"]
            if not token.get("majority") and not token.get("low_confidence")
        ]
        self.assertEqual(plain, ["alpha", "gamma"])

    def test_a_word_no_majority_settles_is_not_a_majority_word(self):
        """The two flags never meet on one token."""
        answer = ensemble.resolve(
            self.read(
                "alpha beta gamma", "alpha xeta gamma", "alpha zeta gamma"
            )
        )

        for token in answer["tokens"]:
            self.assertFalse(
                token.get("majority") and token.get("low_confidence"),
                token,
            )
        marked = [t["text"] for t in answer["tokens"] if t.get("majority")]
        self.assertEqual(marked, [])

    def test_a_word_a_folded_difference_leaves_alone_is_unanimous(self):
        """#378, #380: the vote folds the typography, so a curly quote
        never makes a word a majority word. The panel's own marks do
        show it, and they are not this flag."""
        answer = ensemble.resolve(
            self.read(
                "the court's order stands",
                "the court’s order stnads",
                "the court's order stadns",
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        marked = [t["text"] for t in answer["tokens"] if t.get("majority")]
        self.assertEqual(marked, [])

    def test_no_token_carries_markup(self):
        """The prototype stored ``<mark>``; this module stores a flag,
        and the viewer builds the nodes."""
        answer = ensemble.resolve(
            self.read("alpha beta", "alpha xeta", "alpha zeta")
        )

        for token in answer["tokens"]:
            self.assertNotIn("<", token["text"])
            self.assertIn("text", token)
            self.assertLessEqual(set(token), {"text", "low_confidence"})

    def test_typography_is_not_a_disagreement(self):
        """The engines differ about the quotes on almost every page of
        a real volume, and about none of the words."""
        answer = ensemble.resolve(
            self.read(
                'the "court" said . . . so',
                "the \u201ccourt\u201d said ... so",
                'the "*court*" said ... so',
            )
        )

        self.assertEqual(answer["agreement"], ensemble.UNANIMOUS)
        self.assertEqual(answer["text"], 'the "court" said . . . so')
        self.assertEqual(answer["n_low_confidence"], 0)

    def test_a_markdown_heading_of_one_engine_is_no_disagreement(self):
        answer = ensemble.resolve(self.read("## FACTS", "### FACTS", "FACTS"))

        self.assertEqual(answer["agreement"], ensemble.UNANIMOUS)
        self.assertEqual(answer["text"], "## FACTS")

    def test_a_word_the_engines_read_alike_survives_a_voted_group(self):
        """Inside a voted group the words vote over the key too, so a
        curly quote is not one of the disputes."""
        answer = ensemble.resolve(
            self.read(
                "the court's word alpha",
                "the court\u2019s word xeta",
                "the court's word zeta",
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        self.assertEqual(answer["n_low_confidence"], 1)
        self.assertEqual(answer["text"], "the court's word alpha")

    def test_the_key_leaves_the_words_alone(self):
        self.assertEqual(
            ensemble.compare_text("the \u201ccourt\u2019s\u201d *word*"),
            'the "court\'s" word',
        )
        self.assertEqual(ensemble.compare_text("appeal. . . ."), "appeal...")
        self.assertEqual(ensemble.compare_word("##"), "")

    def test_a_word_only_the_other_engine_read_is_marked_and_counted(self):
        """Two engines make every difference a tie, and the same tie on
        a word the base does have is marked. So the word goes in,
        marked, and the count and the mark say the same thing."""
        tokens, disputed = ensemble.vote_words(
            ensemble._pairs("alpha gamma"),
            [ensemble._pairs("alpha beta gamma")],
        )

        self.assertEqual(
            tokens,
            [
                {"text": "alpha"},
                {
                    "text": "beta",
                    "low_confidence": True,
                    "inserted": True,
                },
                {"text": "gamma"},
            ],
        )
        self.assertEqual(disputed, 1)

    def test_a_join_of_two_words_never_deletes_the_second(self):
        """#391: two engines read the pilcrow and the number as one
        word, and the base read them as two. The span is recorded at
        the first position, so the second holds no reading of theirs.
        That is not a vote to drop the word, and it deleted a citation
        number of a real opinion."""
        tokens, disputed = ensemble.vote_words(
            ensemble._pairs("La.), \u00b6 235-236. No one"),
            [
                ensemble._pairs("La.), \u00b6\u00b6235\u2013236. No one"),
                ensemble._pairs("La.), \u00b61235-236. No one"),
            ],
        )

        self.assertEqual(
            " ".join(token["text"] for token in tokens),
            "La.), \u00b6 235-236. No one",
        )
        marked = [t["text"] for t in tokens if t.get("low_confidence")]
        self.assertEqual(marked, ["\u00b6", "235-236."])
        self.assertEqual(disputed, 2)

    def test_a_join_two_engines_agree_on_writes_the_tail_once(self):
        """The other side of #391: both engines joined the two base
        words the same way, and their reading won at the head. It
        holds the tail already, so the base's own tail word must not
        go in after it."""
        tokens, _ = ensemble.vote_words(
            ensemble._pairs("the wordone wordtwo end"),
            [
                ensemble._pairs("the wordonewordtwo end"),
                ensemble._pairs("the wordonewordtwo end"),
            ],
        )

        self.assertEqual(
            " ".join(token["text"] for token in tokens),
            "the wordonewordtwo end",
        )

    def test_a_join_one_engine_alone_makes_leaves_the_split(self):
        """One engine cannot carry the head, so the base's two words
        both stand."""
        tokens, _ = ensemble.vote_words(
            ensemble._pairs("the wordone wordtwo end"),
            [
                ensemble._pairs("the wordonewordtwo end"),
                ensemble._pairs("the wordone wordtwo end"),
            ],
        )

        self.assertEqual(
            " ".join(token["text"] for token in tokens),
            "the wordone wordtwo end",
        )

    def test_an_engine_that_abstains_confirms_nothing(self):
        """A word the other engines did not answer for is not a word
        every engine read, so it carries the majority flag (#380)."""
        tokens, _ = ensemble.vote_words(
            ensemble._pairs("alpha beta gamma delta"),
            [
                ensemble._pairs("alpha betagamma delta"),
                ensemble._pairs("alpha beta gamma delta"),
            ],
        )

        by_word = {token["text"]: token for token in tokens}
        self.assertEqual(by_word["gamma"].get("majority"), True)

    def test_an_engine_that_read_nothing_still_votes_to_drop(self):
        """A deletion is a reading, and a majority of them drops the
        word. #391 changed the join alone."""
        tokens, _ = ensemble.vote_words(
            ensemble._pairs("alpha beta gamma"),
            [ensemble._pairs("alpha gamma"), ensemble._pairs("alpha gamma")],
        )

        self.assertEqual(
            [token["text"] for token in tokens], ["alpha", "gamma"]
        )

    def test_a_word_only_a_minority_read_is_dropped(self):
        tokens, disputed = ensemble.vote_words(
            ensemble._pairs("alpha gamma"),
            [
                ensemble._pairs("alpha beta gamma"),
                ensemble._pairs("alpha gamma"),
            ],
        )

        self.assertEqual([t["text"] for t in tokens], ["alpha", "gamma"])
        self.assertEqual(disputed, 0)

    def test_a_word_a_majority_read_goes_in_marked_and_counted(self):
        tokens, disputed = ensemble.vote_words(
            ensemble._pairs("the quick fox"),
            [
                ensemble._pairs("the quick brown fox"),
                ensemble._pairs("the quick brown fox"),
            ],
        )

        self.assertEqual(
            [t["text"] for t in tokens], ["the", "quick", "brown", "fox"]
        )
        self.assertEqual(disputed, 1)

    def test_an_engine_that_read_nothing_does_not_vote(self):
        """One engine calls a region a picture and reads no word of it
        while the other reads the paragraph. An empty read is not a
        reading of the text, and it must not take it away."""
        answer = ensemble.resolve(self.read("", "the paragraph"))

        self.assertEqual(answer["text"], "the paragraph")
        self.assertEqual(answer["agreement"], ensemble.SINGLE)
        self.assertEqual(answer["source"], "mistral_ocr")
        self.assertEqual(answer["silent"], ["dots_mocr"])
        self.assertEqual(answer["n_low_confidence"], 0)

    def test_a_group_no_engine_read_stays_empty(self):
        answer = ensemble.resolve(self.read("", ""))

        self.assertEqual(answer["text"], "")
        self.assertEqual(answer["silent"], [])

    def test_the_engines_that_read_decide_among_themselves(self):
        answer = ensemble.resolve(
            self.read("", "the court held", "the court held")
        )

        self.assertEqual(answer["agreement"], ensemble.UNANIMOUS)
        self.assertEqual(answer["agreeing"], ["mistral_ocr", "surya"])
        self.assertEqual(answer["silent"], ["dots_mocr"])

    def test_the_first_engine_of_the_table_is_the_base(self):
        answer = ensemble.resolve(
            self.read("alpha beta", "alpha xeta", "alpha zeta")
        )

        self.assertEqual(answer["source"], "dots_mocr")
        self.assertEqual(list(opinion_ocr.ENGINES)[0], "dots_mocr")


# ── the document ─────────────────────────────────────────────────────
class EnsembleTestCase(OpinionOcrTestCase):
    """The fixture of the OCR glue, plus a reader of the bucket."""

    def setUp(self):
        super().setUp()
        patcher = patch(
            "scanning.s3_sync.download_json_object",
            side_effect=self.read_object,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def read_object(self, key):
        """The bucket's answer to one JSON read."""
        if key not in self.objects:
            raise KeyError(key)
        return json.loads(json.dumps(self.objects[key]))

    def glue(self, opinion=None):
        """Write the OCR documents of one opinion, and reload the row."""
        opinion = opinion or self.opinion
        opinion_ocr.write(opinion, self.inputs())
        opinion.refresh_from_db()
        return opinion

    def run_ensemble(self, opinion=None):
        """Glue the OCR documents, then write the text."""
        opinion = self.glue(opinion)
        return ensemble.rerun(opinion)

    def edit(self):
        """One page edit of the scan, for an address of kind ``edit``."""
        if not hasattr(self, "_edit"):
            self._edit = PageEdit.objects.create(
                scan=self.scan,
                kind=PageEdit.Kind.ROTATE_PAGE,
                pdf_page=2,
                value="90",
                source_fingerprint=self.scan.source_fingerprint,
            )
        return self._edit

    def one_body_block(self):
        """Let Mistral read the body of every page as one block."""
        document = mistral_document()
        for page in document["pages"]:
            page["blocks"] = [
                block(
                    BODY_A[0],
                    BODY_A[1],
                    BODY_B[2],
                    BODY_B[3],
                    text="body A and B",
                )
            ]
        self.objects[self.apply_run.extract_key] = document
        return document

    def stored(self, opinion=None) -> dict:
        """The ensemble document in the bucket."""
        opinion = opinion or self.opinion
        return self.uploads[ensemble.document_key(opinion)]


class TestTheDocument(EnsembleTestCase):
    def test_the_text_of_a_page_is_its_groups_in_order(self):
        document = self.run_ensemble()

        page = document["pages"][1]
        self.assertEqual(page["text"], "body A 2\n\nbody B 2")
        self.assertEqual(
            [group["agreement"] for group in page["groups"]],
            [ensemble.UNANIMOUS] * 2,
        )
        self.assertEqual([g["band"] for g in page["groups"]], ["body", "body"])

    def test_the_page_number_is_left_out(self):
        """The running head carries the approved number of the page,
        and the glue marks it in every engine (#396). The group goes
        whole, with no partial: every engine read the head alike."""
        document = self.run_ensemble()

        page = document["pages"][1]
        self.assertEqual(len(page["dropped"]), 1)
        drop = page["dropped"][0]
        self.assertEqual(drop["reason"], opinion_ocr.PAGE_NUMBER)
        self.assertFalse(drop["partial"])
        self.assertEqual(sorted(drop["engines"]), ["dots_mocr", "mistral_ocr"])
        self.assertEqual(page["counts"]["partial"], 0)

    def test_the_text_of_the_opinion_before_is_left_out(self):
        """The first page is shared, and the running head above the
        caption belongs to the opinion before this one (#293). The
        mask of ``boundaries.outside_rects`` takes it out."""
        document = self.run_ensemble()

        page = document["pages"][0]
        self.assertEqual(page["text"], "body A 1\n\nbody B 1")
        self.assertEqual(page["dropped"][0]["reason"], "outside")

    def test_every_group_names_its_place_in_the_text(self):
        document = self.run_ensemble()

        page = document["pages"][1]
        for group in page["groups"]:
            self.assertEqual(
                page["text"][group["start"] : group["end"]], group["text"]
            )

    def test_a_group_under_a_redaction_is_left_out(self):
        self.redact(2, BODY_A_PT)

        document = self.run_ensemble()

        page = document["pages"][1]
        self.assertEqual(page["text"], "body B 2")
        self.assertEqual(
            sorted(d["reason"] for d in page["dropped"]),
            [opinion_ocr.PAGE_NUMBER, "redaction"],
        )
        self.assertEqual(page["counts"]["dropped"], 2)

    def test_a_part_of_a_block_under_a_box_is_a_partial_drop(self):
        self.redact(2, [36.0, 108.0, 288.0, 200.0])

        document = self.run_ensemble()

        page = document["pages"][1]
        self.assertTrue(self.redaction_drop(page)["partial"])
        self.assertEqual(page["counts"]["partial"], 1)

    @staticmethod
    def redaction_drop(page: dict) -> dict:
        """The one drop of a page a redaction made; the page number
        makes the other (#396)."""
        return next(d for d in page["dropped"] if d["reason"] == "redaction")

    def test_a_box_over_one_cell_of_a_block_is_a_partial_drop(self):
        """The daily shape of it: one engine reads the body as one
        block, a redaction covers one cell of the other engine whole,
        and the group is dropped whole. The reader loses a clean
        reading, so the page says ``partial``."""
        self.one_body_block()
        self.redact(2, BODY_A_PT)

        built = self.run_ensemble()

        page = built["pages"][1]
        self.assertNotIn("body", page["text"])
        self.assertTrue(self.redaction_drop(page)["partial"])
        self.assertEqual(page["counts"]["partial"], 1)

    def test_a_block_only_one_engine_saw_is_a_disagreement(self):
        """The other engine drew no box there. Nothing votes, and the
        page must still say that they did not read it alike."""
        document = mistral_document()
        for page in document["pages"]:
            page["blocks"] = [
                entry
                for entry in page["blocks"]
                if not entry["content"].startswith("body A")
            ]
        self.objects[self.apply_run.extract_key] = document

        built = self.run_ensemble()

        page = built["pages"][1]
        group = next(g for g in page["groups"] if "body A 2" in g["text"])
        self.assertEqual(group["agreement"], ensemble.SINGLE)
        self.assertEqual(group["silent"], [])
        self.assertEqual(page["counts"]["differing"], 1)

    def test_a_page_one_engine_did_not_read_names_it(self):
        document = mistral_document()
        document["pages"][2]["blocks"] = []
        document["pages"][2]["error"] = "not read"
        self.objects[self.apply_run.extract_key] = document

        built = self.run_ensemble()

        page = built["pages"][1]
        self.assertEqual(page["engines"], ["dots_mocr", "mistral_ocr"])
        self.assertEqual(page["missing"], ["mistral_ocr"])
        self.assertEqual(page["counts"]["differing"], len(page["groups"]))

    def test_the_engines_that_differ_make_one_voted_group(self):
        self.disagree()

        document = self.run_ensemble()

        page = document["pages"][1]
        voted = [g for g in page["groups"] if g["agreement"] == ensemble.VOTED]
        self.assertEqual(len(voted), 1)
        self.assertGreater(voted[0]["n_low_confidence"], 0)
        self.assertEqual(
            sorted(voted[0]["engines"]), ["dots_mocr", "mistral_ocr"]
        )

    def test_a_picture_box_of_one_engine_keeps_the_other_s_text(self):
        """The engines align a paragraph with a box one of them read
        nothing in. The text stays, and the page says they differ."""
        document = mistral_document()
        for page in document["pages"]:
            for entry in page["blocks"]:
                if entry["content"].startswith("body A"):
                    entry["content"] = ""
                    entry["type"] = "picture"
        self.objects[self.apply_run.extract_key] = document

        built = self.run_ensemble()

        page = built["pages"][1]
        self.assertIn("body A 2", page["text"])
        group = next(g for g in page["groups"] if "body A 2" in g["text"])
        self.assertEqual(group["silent"], ["mistral_ocr"])
        self.assertEqual(page["counts"]["differing"], 1)

    def test_a_page_nobody_read_carries_its_reason(self):
        self.objects[self.apply_run.extract_key] = mistral_document()
        from scanning.tests.test_opinion_ocr import dots_document

        self.objects[self.apply_run.ocr_key] = dots_document(failed=(1, 2, 3))
        for page in self.objects[self.apply_run.extract_key]["pages"]:
            page["blocks"] = []
            page["error"] = "not read"

        document = self.run_ensemble()

        page = document["pages"][0]
        self.assertEqual(page["error"], "not read")
        self.assertEqual(page["groups"], [])
        self.assertEqual(page["text"], "")

    def test_a_page_nobody_measured_carries_its_reason(self):
        """No detection and no render measured the page, so no box of
        it is in points. The page has no text, and it says why."""
        page = {
            "page_in_opinion": 0,
            "page_index": 0,
            "pdf_page": 1,
            "source": None,
            "frame": None,
            "units": [],
        }

        entry = ensemble.build_page(
            {"dots_mocr": page, "mistral_ocr": dict(page)}, 0
        )

        self.assertEqual(entry["error"], ensemble.UNMEASURED)
        self.assertEqual(entry["text"], "")
        self.assertEqual(entry["groups"], [])

    def test_the_document_names_the_opinion_and_the_engines(self):
        document = self.run_ensemble()

        self.assertEqual(document["engines"], ["dots_mocr", "mistral_ocr"])
        self.assertEqual(document["opinion"]["first_printed_page"], 502)
        self.assertEqual(document["apply_run"], "a1")
        self.assertEqual(len(document["pages"]), 3)
        # Two body groups a page: every header is out, the first as
        # the opinion before and the others as the page number.
        self.assertEqual(document["counts"]["groups"], 6)

    def test_the_document_lands_beside_the_engine_files(self):
        self.run_ensemble()

        self.assertEqual(
            ensemble.document_key(self.opinion),
            f"{self.prefix}jobs/opinions/502.0/r0/ensemble.json",
        )
        self.assertIn(ensemble.document_key(self.opinion), self.uploads)

    def test_an_engine_document_short_of_a_page_fails_the_row(self):
        self.glue()
        key = opinion_ocr.engine_key(self.opinion, "mistral_ocr")
        self.objects[key]["pages"] = self.objects[key]["pages"][:1]

        with self.assertRaises(ensemble.EnsembleError) as caught:
            ensemble.rerun(self.opinion)

        self.assertIn("has no page 2", str(caught.exception))

    def disagree(self, text="body A one"):
        """Make the Mistral read of one block differ from the dots one."""
        document = mistral_document()
        for page in document["pages"]:
            for entry in page["blocks"]:
                if entry["content"].startswith("body A"):
                    entry["content"] = text
        self.objects[self.apply_run.extract_key] = document


# ── the rows ─────────────────────────────────────────────────────────
class TestTheRows(EnsembleTestCase):
    def test_one_row_per_page_with_its_address(self):
        self.run_ensemble()

        rows = list(OpinionText.objects.filter(opinion=self.opinion))
        self.assertEqual([row.page_in_opinion for row in rows], [0, 1, 2])
        self.assertEqual([row.page_index for row in rows], [1, 2, 3])
        self.assertEqual([row.source_page for row in rows], [2, 3, 4])
        self.assertIsNone(rows[0].source_edit_id)
        self.assertEqual(rows[0].apply_run_id, self.apply_run.pk)
        self.assertTrue(rows[0].text.startswith("body A 1"))
        self.assertTrue(rows[1].text.startswith("body A 2"))

    def test_the_text_is_a_cache_and_the_human_text_is_not(self):
        self.run_ensemble()
        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        row.human_text = "what the curator typed"
        row.save(update_fields=["human_text"])

        self.run_ensemble()

        row.refresh_from_db()
        self.assertEqual(row.human_text, "what the curator typed")
        self.assertEqual(row.current_text, "what the curator typed")
        self.assertTrue(row.text.startswith("body A 1"))

    def test_a_row_of_a_page_the_opinion_lost_is_deleted(self):
        self.run_ensemble()
        OpinionText.objects.create(
            opinion=self.opinion, page_in_opinion=7, page_index=9
        )

        self.run_ensemble()

        self.assertEqual(
            OpinionText.objects.filter(opinion=self.opinion).count(), 3
        )

    def test_the_address_of_an_edited_page_is_one_based(self):
        """``detections.source_of_entry`` is the one rule: the apply
        writes the page of an edit 0-based inside its shard, and a
        rotation writes 0."""
        page = page_of()
        page["source"] = {"kind": "edit", "edit_id": self.edit().pk, "page": 0}

        ensemble.write_rows(self.opinion, document_of(page))

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertEqual(row.source_page, 1)
        self.assertEqual(row.source_edit_id, self.edit().pk)

    def test_a_page_the_opinion_lost_keeps_a_curator_s_text(self):
        """An opinion does grow shorter, and ``human_text`` is never
        discarded (#335)."""
        self.run_ensemble()
        last = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=2)
        last.human_text = "what the curator typed"
        last.save(update_fields=["human_text"])
        empty = OpinionText.objects.create(
            opinion=self.opinion, page_in_opinion=3, page_index=4
        )

        ensemble.write_rows(self.opinion, document_of(page_of(0), page_of(1)))

        last.refresh_from_db()
        self.assertEqual(last.human_text, "what the curator typed")
        self.assertFalse(OpinionText.objects.filter(pk=empty.pk).exists())

    def test_a_disagreement_names_its_place_and_every_reading(self):
        document = document_of(
            page_of(
                text="alpha beta",
                groups=[
                    {
                        "start": 0,
                        "end": 10,
                        "agreement": ensemble.VOTED,
                        "engines": {
                            "dots_mocr": {"text": "alpha beta"},
                            "mistral_ocr": {"text": "alpha peta"},
                        },
                    }
                ],
            )
        )

        ensemble.write_rows(self.opinion, document)

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertEqual(
            row.disagreements,
            [
                {
                    "start": 0,
                    "end": 10,
                    "section": ensemble.BODY,
                    "agreement": ensemble.VOTED,
                    "variants": {
                        "dots_mocr": "alpha beta",
                        "mistral_ocr": "alpha peta",
                    },
                }
            ],
        )

    def test_a_group_the_engines_agree_on_is_no_disagreement(self):
        document = document_of(
            page_of(
                groups=[
                    {
                        "start": 0,
                        "end": 8,
                        "agreement": ensemble.UNANIMOUS,
                        "engines": {
                            "dots_mocr": {"text": "the text"},
                            "mistral_ocr": {"text": "the text"},
                        },
                    }
                ]
            )
        )

        ensemble.write_rows(self.opinion, document)

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertEqual(row.disagreements, [])


# ── the findings ─────────────────────────────────────────────────────
class TestTheFindings(EnsembleTestCase):
    def rebuild(self, **values) -> list[OpinionFinding]:
        ensemble.rebuild_findings(self.opinion, document_of(page_of(**values)))
        return list(
            OpinionFinding.objects.filter(opinion=self.opinion).order_by(
                "check_name"
            )
        )

    def test_a_majority_group_is_an_engines_disagree_card(self):
        cards = self.rebuild(majority=2, differing=2)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].check_name, OpinionCheck.ENGINES_DISAGREE)
        self.assertEqual(cards[0].page_in_opinion, 0)
        self.assertEqual(cards[0].severity, Issue.Severity.WARNING)
        self.assertIn("2 place(s)", cards[0].message)

    def test_a_voted_group_is_an_engines_disagree_card(self):
        """With two engines no group can hold a majority, and a card
        that read the majority alone would never be written."""
        cards = self.rebuild(voted=2, differing=2)

        self.assertEqual(cards[0].check_name, OpinionCheck.ENGINES_DISAGREE)
        self.assertIn("2 place(s)", cards[0].message)

    def test_the_card_counts_what_the_row_calls_a_disagreement(self):
        cards = self.rebuild(majority=1, voted=1, differing=2)

        self.assertIn("2 place(s)", cards[0].message)

    def test_a_silent_engine_is_a_place_the_engines_differ(self):
        cards = self.rebuild(differing=1, silent=1)

        self.assertEqual(cards[0].check_name, OpinionCheck.ENGINES_DISAGREE)
        self.assertIn("1 place(s)", cards[0].message)

    def test_a_word_with_no_majority_is_its_own_card(self):
        cards = self.rebuild(low_confidence=3)

        self.assertEqual(cards[0].check_name, OpinionCheck.NO_MAJORITY)
        self.assertEqual(cards[0].severity, Issue.Severity.ERROR)
        self.assertIn("3 word(s)", cards[0].message)

    def test_a_partial_redaction_is_its_own_card(self):
        cards = self.rebuild(partial=1)

        self.assertEqual(cards[0].check_name, OpinionCheck.PARTIAL_REDACTION)

    def test_a_page_the_engines_agree_on_makes_no_card(self):
        self.assertEqual(self.rebuild(unanimous=4), [])

    def test_the_card_names_the_engine_that_did_not_read(self):
        document = mistral_document()
        document["pages"][2]["blocks"] = []
        document["pages"][2]["error"] = "not read"
        self.objects[self.apply_run.extract_key] = document

        self.run_ensemble()

        card = OpinionFinding.objects.get(
            opinion=self.opinion,
            page_in_opinion=1,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        self.assertIn("mistral_ocr did not read this page", card.message)

    def test_a_page_with_no_text_is_one_card_of_its_own(self):
        page = page_of(text="")
        page["error"] = ensemble.UNMEASURED

        ensemble.rebuild_findings(self.opinion, document_of(page))

        card = OpinionFinding.objects.get(opinion=self.opinion)
        self.assertEqual(card.check_name, OpinionCheck.PAGE_NOT_READ)
        self.assertEqual(card.severity, Issue.Severity.ERROR)
        self.assertIn("No engine measured", card.message)

    def test_a_page_no_engine_read_says_so(self):
        page = page_of(text="")
        page["error"] = "not read"

        ensemble.rebuild_findings(self.opinion, document_of(page))

        card = OpinionFinding.objects.get(opinion=self.opinion)
        self.assertEqual(card.check_name, OpinionCheck.PAGE_NOT_READ)
        self.assertIn("No engine read this page", card.message)

    def test_the_partial_card_names_what_covered_the_block(self):
        """``opinion_ocr`` masks the opinion before this one, and that
        is not a redaction."""
        page = page_of(partial=1)
        page["dropped"] = [
            {
                "engines": {},
                "box_pt": None,
                "reason": "outside",
                "partial": True,
            }
        ]

        ensemble.rebuild_findings(self.opinion, document_of(page))

        card = OpinionFinding.objects.get(
            opinion=self.opinion, check_name=OpinionCheck.PARTIAL_REDACTION
        )
        self.assertIn("The mask of the opinion before", card.message)
        self.assertNotIn("redaction covers", card.message)

    def test_the_partial_card_names_the_page_number(self):
        """One engine glued the head to the first paragraph, and the
        group went whole (#396). The card must not call that a
        redaction."""
        page = page_of(partial=1)
        page["dropped"] = [
            {
                "engines": {},
                "box_pt": None,
                "reason": opinion_ocr.PAGE_NUMBER,
                "partial": True,
            }
        ]

        ensemble.rebuild_findings(self.opinion, document_of(page))

        card = OpinionFinding.objects.get(
            opinion=self.opinion, check_name=OpinionCheck.PARTIAL_REDACTION
        )
        self.assertIn("The page number covers part of", card.message)

    def test_a_standing_dismissal_mutes_the_new_card(self):
        dismissal = OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )

        cards = self.rebuild(majority=1, differing=1)

        self.assertEqual(cards[0].dismissal_id, dismissal.pk)

    def test_a_withdrawn_dismissal_mutes_nothing(self):
        from django.utils import timezone

        OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
            withdrawn_at=timezone.now(),
        )

        cards = self.rebuild(majority=1, differing=1)

        self.assertIsNone(cards[0].dismissal_id)

    def test_the_rebuild_keeps_the_cards_of_the_creation(self):
        """``opinions.create_rows`` owns the two stale checks, and one
        rebuild writes the three of the ensemble."""
        stale = OpinionFinding.objects.create(
            opinion=self.opinion,
            check_name=OpinionCheck.ORPHANED_OPINION,
            message="no boundary",
        )

        self.rebuild(majority=1, differing=1)

        self.assertTrue(OpinionFinding.objects.filter(pk=stale.pk).exists())

    def test_a_second_rebuild_writes_one_card(self):
        self.rebuild(majority=1, differing=1)
        cards = self.rebuild(majority=1, differing=1)

        self.assertEqual(len(cards), 1)

    def test_the_write_leaves_the_cards_of_the_document(self):
        self.redact(2, [36.0, 108.0, 288.0, 200.0])

        self.run_ensemble()

        cards = OpinionFinding.objects.filter(
            opinion=self.opinion, check_name=OpinionCheck.PARTIAL_REDACTION
        )
        self.assertEqual([card.page_in_opinion for card in cards], [1])


# ── the ledger and the pass ──────────────────────────────────────────
class TestTheLedger(EnsembleTestCase):
    def test_the_stamp_says_the_text_is_current(self):
        self.glue()
        self.assertFalse(ensemble.is_written(self.opinion))

        ensemble.rerun(self.opinion)

        self.opinion.refresh_from_db()
        self.assertTrue(ensemble.is_written(self.opinion))
        self.assertEqual(self.opinion.ensemble_revision, 0)
        self.assertEqual(self.opinion.ensemble_attempts, 0)

    def test_a_new_glue_revision_makes_the_text_stale(self):
        self.run_ensemble()
        opinion_ocr.reglue(self.scan)

        self.opinion.refresh_from_db()
        self.assertFalse(ensemble.is_written(self.opinion))

    def test_the_glue_stamps_how_many_engines_it_wrote(self):
        self.glue()

        self.assertEqual(self.opinion.ocr_engine_count, 2)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=3)
    def test_a_two_engine_row_is_not_due(self):
        self.glue()

        self.assertEqual(list(ensemble.due()), [])
        self.assertEqual(ensemble.run_tick(), 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_the_pass_writes_a_row_that_holds_enough_engines(self):
        self.glue()

        self.assertEqual(ensemble.run_tick(), 1)

        self.opinion.refresh_from_db()
        self.assertTrue(ensemble.is_written(self.opinion))
        self.assertTrue(
            OpinionText.objects.filter(opinion=self.opinion).exists()
        )

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_the_pass_writes_nothing_twice(self):
        self.glue()
        ensemble.run_tick()

        self.assertEqual(ensemble.run_tick(), 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_scan_that_left_the_done_status_is_not_due(self):
        self.glue()
        Scan_ = type(self.scan)
        Scan_.objects.filter(pk=self.scan.pk).update(
            status=Status.READY_FOR_REDACTION_REVIEW
        )

        self.assertEqual(ensemble.run_tick(), 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_row_without_its_ocr_glue_is_not_due(self):
        self.assertEqual(ensemble.run_tick(), 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_failing_row_spends_its_attempts_and_ends(self):
        self.glue()
        del self.objects[opinion_ocr.engine_key(self.opinion, "manifest")]

        for _ in range(ensemble.MAX_ATTEMPTS):
            ensemble.run_tick()

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ensemble_attempts, ensemble.MAX_ATTEMPTS)
        self.assertEqual(self.opinion.status, OpinionReviewStatus.ERROR)
        self.assertTrue(
            self.opinion.error_message.startswith(ensemble.MESSAGE_PREFIX)
        )
        self.assertEqual(ensemble.run_tick(), 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_bucket_that_is_away_spends_no_attempt(self):
        """The tick runs every fifteen seconds, so a fault that passes
        would otherwise end every due row in a minute."""
        self.glue()
        with patch(
            "scanning.s3_sync.download_json_object",
            side_effect=OSError("connection reset"),
        ):
            self.assertEqual(ensemble.run_tick(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ensemble_attempts, 0)
        self.assertEqual(self.opinion.status, OpinionReviewStatus.PROCESSING)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_an_upload_that_fails_spends_no_attempt(self):
        self.glue()
        with patch("scanning.s3_sync.upload_json_object", return_value=False):
            self.assertEqual(ensemble.run_tick(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ensemble_attempts, 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_document_that_is_not_json_spends_an_attempt(self):
        """It fails the same way at every retry, so it is a fact about
        the row and not a fault that passes."""
        self.glue()
        with patch(
            "scanning.s3_sync.download_json_object",
            side_effect=ValueError("Expecting value: line 1 column 1"),
        ):
            self.assertEqual(ensemble.run_tick(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ensemble_attempts, 1)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_missing_document_spends_an_attempt(self):
        """The row says its OCR documents are written, so an object
        that is not in the bucket is a fact about the row."""
        self.glue()
        del self.objects[opinion_ocr.engine_key(self.opinion, "dots_mocr")]

        self.assertEqual(ensemble.run_tick(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ensemble_attempts, 1)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_a_glue_that_moved_takes_the_rows_back(self):
        """The OCR glue wrote again while the text was written, so the
        rows and the cards describe documents that are gone."""
        opinion = self.glue()
        documents = ensemble.load_documents(opinion)

        def move(*args, **kwargs):
            Opinion.objects.filter(pk=opinion.pk).update(
                ocr_glue_revision=opinion.ocr_glue_revision + 1
            )
            return 0

        with patch("scanning.ensemble.rebuild_findings", side_effect=move):
            with self.assertRaises(ensemble.RevisionMoved):
                ensemble.write(opinion, documents)

        self.assertFalse(OpinionText.objects.filter(opinion=opinion).exists())
        opinion.refresh_from_db()
        self.assertIsNone(opinion.ensemble_revision)
        self.assertEqual(opinion.ensemble_attempts, 0)

    def test_an_approved_row_is_not_due(self):
        """A person read its text and said it is right, the rule
        ``opinion_ocr.reglue`` and ``opinions.create_rows`` follow."""
        self.glue()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        self.assertEqual(list(ensemble.due()), [])
        self.assertEqual(ensemble.run_tick(), 0)

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
    def test_another_work_s_error_message_survives_a_failure(self):
        """``error_message`` is shared, and a row the PDF pass ended
        must keep the reason it ended."""
        self.glue()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.ERROR,
            error_message="The PDF source did not pull.",
        )
        self.opinion.refresh_from_db()

        ensemble.record_failure(self.opinion, "the document failed")

        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.error_message, "The PDF source did not pull."
        )
        self.assertEqual(self.opinion.ensemble_attempts, 1)

    def test_a_run_that_works_takes_back_this_module_s_error(self):
        self.glue()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.ERROR,
            error_message=f"{ensemble.MESSAGE_PREFIX}the document failed",
        )
        self.opinion.refresh_from_db()

        ensemble.rerun(self.opinion)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.status, OpinionReviewStatus.PROCESSING)
        self.assertEqual(self.opinion.error_message, "")

    def test_another_work_s_error_is_left_alone(self):
        self.glue()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.ERROR,
            error_message="OCR glue: the document did not load",
        )
        self.opinion.refresh_from_db()

        ensemble.rerun(self.opinion)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.status, OpinionReviewStatus.ERROR)
        self.assertEqual(
            self.opinion.error_message,
            "OCR glue: the document did not load",
        )

    def test_the_pass_runs_after_the_glue_and_before_the_promotion(self):
        """The three opinion passes end the tick, in this order.

        The glue writes the documents, the ensemble reads them, and the
        promotion opens the review of a row that now has both of its
        objects (#365).
        """
        calls = []
        with (
            patch(
                "scanning.opinion_ocr.glue_due",
                side_effect=lambda: calls.append("glue") or 0,
            ),
            patch(
                "scanning.ensemble.run_tick",
                side_effect=lambda: calls.append("ensemble") or 0,
            ),
            patch(
                "scanning.opinions.promote_ready_opinions",
                side_effect=lambda: calls.append("promote") or 0,
            ),
            patch("django.db.connections.close_all"),
        ):
            call_command("collect_external_jobs")

        self.assertEqual(calls[-3:], ["glue", "ensemble", "promote"])


# ── the button and the command ───────────────────────────────────────
class TestTheButton(EnsembleTestCase, ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def url(self, opinion=None, scan=None) -> str:
        opinion = opinion or self.opinion
        scan = scan or self.scan
        return reverse(
            "rerun_opinion_ensemble",
            kwargs={"pk": scan.pk, "opinion_pk": opinion.pk},
        )

    @override_settings(OPINION_ENSEMBLE_MIN_ENGINES=3)
    def test_the_button_waives_the_engine_gate(self):
        self.glue()

        response = self.client.post(self.url())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("dots_mocr, mistral_ocr", body["message"])
        self.assertIn("6 block(s)", body["message"])
        self.opinion.refresh_from_db()
        self.assertTrue(ensemble.is_written(self.opinion))

    def test_409_before_the_ocr_documents_exist(self):
        response = self.client.post(self.url())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("not written", response.json()["message"])

    def test_409_when_a_document_does_not_load(self):
        self.glue()
        del self.objects[opinion_ocr.engine_key(self.opinion, "dots_mocr")]

        response = self.client.post(self.url())

        self.assertEqual(response.status_code, 409)
        message = response.json()["message"]
        self.assertIn("must be written again", message)
        self.assertNotIn("jobs/opinions", message)

    def test_409_for_an_approved_opinion(self):
        self.glue()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        response = self.client.post(self.url())

        self.assertEqual(response.status_code, 409)
        self.assertIn("Reopen it first", response.json()["message"])
        self.assertFalse(
            OpinionText.objects.filter(opinion=self.opinion).exists()
        )

    def test_a_bucket_fault_answers_our_own_line(self):
        """The words of a library never reach the answer."""
        self.glue()

        with patch(
            "scanning.s3_sync.download_json_object",
            side_effect=OSError("connection reset by 10.0.0.1"),
        ):
            response = self.client.post(self.url())

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("10.0.0.1", response.json()["message"])
        self.assertIn("Press the button again", response.json()["message"])

    def test_409_when_the_glue_moved_under_the_write(self):
        """Nothing was kept, so the answer must not say it was."""
        self.glue()

        def move(*args, **kwargs):
            Opinion.objects.filter(pk=self.opinion.pk).update(
                ocr_glue_revision=self.opinion.ocr_glue_revision + 1
            )
            return 0

        with patch("scanning.ensemble.rebuild_findings", side_effect=move):
            response = self.client.post(self.url())

        self.assertEqual(response.status_code, 409)
        self.assertIn("nothing was kept", response.json()["message"])
        self.assertFalse(
            OpinionText.objects.filter(opinion=self.opinion).exists()
        )

    def test_404_across_scans(self):
        other = ScanFactory()

        response = self.client.post(self.url(scan=other))

        self.assertEqual(response.status_code, 404)

    def test_the_button_refuses_a_get(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)


class TestTheReaderRoutes(EnsembleTestCase, ScanningTestCase):
    """The two routes the review page reads, and the staff redirect.

    The answer is JSON that holds a presigned GET, not a 302: pdf.js
    and ``fetch`` read a direct URL, and a browser judges the CORS
    rules of a redirected request differently (#365).
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def url(self, name, opinion=None, scan=None, **extra) -> str:
        opinion = opinion or self.opinion
        scan = scan or self.scan
        return reverse(
            name,
            kwargs={"pk": scan.pk, "opinion_pk": opinion.pk, **extra},
        )

    def write_the_pdf(self):
        """Stamp the PDF ledger and put the object in the bucket."""
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=self.opinion.glue_revision
        )
        self.objects[opinion_pdf.key(self.opinion)] = b"%PDF-1.7"
        self.opinion.refresh_from_db()

    # -- the PDF ----------------------------------------------------------

    def test_the_pdf_route_answers_a_presigned_get(self):
        self.write_the_pdf()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/pdf"
        ) as presign:
            response = self.client.get(self.url("opinion_pdf_url"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://s3/pdf")
        self.assertEqual(response.json()["revision"], 0)
        key, _ttl = presign.call_args.args
        self.assertEqual(key, opinion_pdf.key(self.opinion))

    def test_the_pdf_signature_outlives_the_reading(self):
        """pdf.js asks for another range whenever the reviewer scrolls.

        Ten minutes is the size of one download (#243). A range after
        the signature dies is a 403 on a page that shows no reason, so
        the PDF takes the lifetime of the original's own URL.
        """
        self.write_the_pdf()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/pdf"
        ) as presign:
            self.client.get(self.url("opinion_pdf_url"))

        _key, ttl = presign.call_args.args
        self.assertEqual(ttl, settings.ORIGINAL_VIEW_PRESIGN_TTL)
        self.assertGreater(ttl, views_process.GLUED_OUTPUT_PRESIGN_TTL)

    def test_the_document_signature_is_one_read_long(self):
        """One ``fetch`` at load, so the short lifetime is the right one."""
        self.run_ensemble()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/e"
        ) as presign:
            self.client.get(self.url("opinion_ensemble_url"))

        _key, ttl = presign.call_args.args
        self.assertEqual(ttl, views_process.GLUED_OUTPUT_PRESIGN_TTL)

    def test_the_pdf_route_names_no_download(self):
        """A ``Content-Disposition`` makes a browser save the file.

        That header belongs to the routes of #243, and it is the
        opposite of what a reader wants.
        """
        self.write_the_pdf()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/pdf"
        ) as presign:
            self.client.get(self.url("opinion_pdf_url"))

        self.assertEqual(presign.call_args.kwargs, {})

    def test_404_before_the_pdf_is_written(self):
        response = self.client.get(self.url("opinion_pdf_url"))

        self.assertEqual(response.status_code, 404)
        self.assertIn("not written yet", response.json()["error"])

    def test_404_when_the_pdf_is_stamped_and_gone(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=self.opinion.glue_revision
        )

        response = self.client.get(self.url("opinion_pdf_url"))

        self.assertEqual(response.status_code, 404)
        self.assertIn("not in the", response.json()["error"])

    # -- the ensemble document --------------------------------------------

    def test_the_ensemble_route_answers_a_presigned_get(self):
        self.run_ensemble()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/e"
        ) as presign:
            response = self.client.get(self.url("opinion_ensemble_url"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://s3/e")
        key, _ttl = presign.call_args.args
        self.assertEqual(key, ensemble.document_key(self.opinion))

    def test_404_before_the_ensemble_is_written(self):
        self.glue()

        response = self.client.get(self.url("opinion_ensemble_url"))

        self.assertEqual(response.status_code, 404)
        self.assertIn("not written yet", response.json()["error"])

    def test_404_when_the_ocr_glue_moved_on(self):
        """The stamp must name the live revision, the one rule."""
        self.run_ensemble()
        Opinion.objects.filter(pk=self.opinion.pk).update(glue_revision=1)

        response = self.client.get(self.url("opinion_ensemble_url"))

        self.assertEqual(response.status_code, 404)

    def test_404_for_an_opinion_of_another_scan(self):
        other = ScanFactory()

        response = self.client.get(
            self.url("opinion_ensemble_url", scan=other)
        )

        self.assertEqual(response.status_code, 404)

    def test_the_login_is_required(self):
        self.client.logout()

        for name in ("opinion_pdf_url", "opinion_ensemble_url"):
            response = self.client.get(self.url(name))

            self.assertEqual(response.status_code, 302)
            self.assertIn("login", response["Location"])

    # -- the staff redirect ------------------------------------------------

    def test_the_staff_route_serves_the_ensemble(self):
        self.run_ensemble()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/e"
        ):
            response = self.client.get(
                self.url("serve_opinion_ocr", engine="ensemble")
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://s3/e")

    def test_the_staff_route_404s_before_the_ensemble_is_written(self):
        self.glue()

        response = self.client.get(
            self.url("serve_opinion_ocr", engine="ensemble")
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("not written yet", response.json()["error"])

    def test_the_file_index_lists_the_ensemble(self):
        self.run_ensemble()

        response = self.client.get(self.url("opinion_file_index"))

        files = {entry["name"]: entry for entry in response.json()["files"]}
        self.assertTrue(files["ensemble.json"]["written"])
        self.assertEqual(
            files["ensemble.json"]["url"],
            self.url("serve_opinion_ocr", engine="ensemble"),
        )

    def test_the_file_index_leaves_an_unwritten_ensemble_without_a_url(self):
        self.glue()

        response = self.client.get(self.url("opinion_file_index"))

        files = {entry["name"]: entry for entry in response.json()["files"]}
        self.assertFalse(files["ensemble.json"]["written"])
        self.assertNotIn("url", files["ensemble.json"])


class TestTheCommand(EnsembleTestCase):
    def run_command(self, *args, **options) -> str:
        out = StringIO()
        call_command(
            "rerun_opinion_ensemble", *args, stdout=out, stderr=out, **options
        )
        return out.getvalue()

    def test_the_dry_run_changes_nothing(self):
        self.glue()

        output = self.run_command(self.scan.pk, "--dry-run")

        self.assertIn("would read 1 opinion(s)", output)
        self.assertFalse(
            OpinionText.objects.filter(opinion=self.opinion).exists()
        )

    def test_the_command_writes_the_text(self):
        self.glue()

        output = self.run_command(self.scan.pk)

        self.assertIn("Wrote 1 opinion(s)", output)
        self.assertEqual(
            OpinionText.objects.filter(opinion=self.opinion).count(), 3
        )

    def test_an_opinion_without_its_ocr_documents_is_passed_over(self):
        output = self.run_command(self.scan.pk)

        self.assertIn("Wrote 0 opinion(s)", output)

    def test_an_approved_opinion_is_left_alone(self):
        self.glue()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        self.run_command(self.scan.pk)

        self.assertFalse(
            OpinionText.objects.filter(opinion=self.opinion).exists()
        )

    def test_an_unknown_scan_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command(99999)


# ── the section (#399) ───────────────────────────────────────────────
#: The left and the right column of a test page, and a footnote band
#: across both, in points.
LEFT_X = (50, 300)
RIGHT_X = (350, 580)
FOOT_ZONE = [40.0, 590.0, 580.0, 710.0]


def read_units(*specs) -> list[dict]:
    """Units of two engines that read the same boxes alike.

    Each spec is ``(box, text)`` or ``(box, text, kinds)``, with
    ``kinds`` the ``(dots category, mistral type)`` pair.
    """
    units = []
    for index, spec in enumerate(specs):
        box, text = spec[0], spec[1]
        kinds = spec[2] if len(spec) > 2 else ("Text", "text")
        units.append(unit("dots_mocr", index, box, text, label=kinds[0]))
        units.append(unit("mistral_ocr", index, box, text, label=kinds[1]))
    return units


def engine_page(units_of_engine: list[dict], zones=()) -> dict:
    """One engine's page of an opinion document, for ``build_page``."""
    return {
        "page_in_opinion": 0,
        "page_index": 1,
        "pdf_page": 2,
        "source": {"kind": "original", "pdf_page": 2},
        "frame": {"width_pt": WIDTH, "height_pt": HEIGHT},
        "zones": {"footnotes": list(zones)},
        "units": [
            {
                "id": u["id"],
                "type": u["type"],
                "text": u["text"],
                "box_pt": u["box_pt"],
                "exclusion": u["exclusion"],
                "share": u["share"],
                "marks": list(u.get("marks") or []),
                "kind": u.get("kind") or "paragraph",
                "table": u.get("table"),
            }
            for u in units_of_engine
        ],
    }


def build(units, zones=()) -> dict:
    """The ensemble of one page read by the two engines of ``units``."""
    pages = {
        engine: engine_page([u for u in units if u["engine"] == engine], zones)
        for engine in ("dots_mocr", "mistral_ocr")
    }
    return ensemble.build_page(pages, 0)


class TestTheSection(TestCase):
    """``ensemble.section``: the zone decides, the labels doubt."""

    def group(self, *units) -> dict:
        groups = ensemble.align_page(list(units), WIDTH, HEIGHT)
        self.assertEqual(len(groups), 1)
        return groups[0]

    def test_a_group_under_the_zone_is_a_footnote(self):
        group = self.group(*read_units(((50, 600, 300, 700), "1. note")))

        self.assertEqual(
            ensemble.section(group, [FOOT_ZONE]), (ensemble.FOOTNOTES, False)
        )

    def test_a_group_outside_the_zone_is_body_text(self):
        group = self.group(*read_units(((50, 100, 300, 200), "the body")))

        self.assertEqual(
            ensemble.section(group, [FOOT_ZONE]), (ensemble.BODY, False)
        )

    def test_a_group_half_under_the_zone_is_judged_by_its_share(self):
        group = self.group(*read_units(((50, 500, 300, 700), "astride")))

        half = [40.0, 600.0, 580.0, 710.0]
        less = [40.0, 620.0, 580.0, 710.0]
        self.assertEqual(
            ensemble.section(group, [half])[0], ensemble.FOOTNOTES
        )
        self.assertEqual(ensemble.section(group, [less])[0], ensemble.BODY)

    def test_a_page_scale_group_over_a_small_zone_stays_body_text(self):
        """The share is of the group's own box, never of the zone."""
        group = self.group(
            *read_units(((20, 20, WIDTH - 20, HEIGHT - 20), "the whole page"))
        )

        self.assertEqual(
            ensemble.section(group, [FOOT_ZONE]), (ensemble.BODY, False)
        )

    def test_the_share_is_the_maximum_over_the_zones(self):
        group = self.group(*read_units(((50, 500, 300, 700), "astride")))
        strips = [[40.0, 600.0, 580.0, 650.0], [40.0, 650.0, 580.0, 710.0]]

        self.assertEqual(ensemble.section(group, strips)[0], ensemble.BODY)

    def test_a_footnote_label_never_moves_a_group(self):
        """Exact and rare (#399): a label outside every zone is a doubt
        and no footnote, whatever every engine says."""
        group = self.group(
            *read_units(
                ((50, 600, 300, 700), "1. note", ("Footnote", "references"))
            )
        )

        self.assertEqual(ensemble.section(group, []), (ensemble.BODY, True))
        self.assertEqual(
            ensemble.section(group, [[40.0, 20.0, 580.0, 60.0]]),
            (ensemble.BODY, True),
        )

    def test_a_body_label_under_the_zone_raises_no_doubt(self):
        """Three zone pages in four carry no footnote label at all."""
        group = self.group(*read_units(((50, 600, 300, 700), "1. note")))

        self.assertEqual(
            ensemble.section(group, [FOOT_ZONE]), (ensemble.FOOTNOTES, False)
        )

    def test_a_footnote_label_under_the_zone_raises_no_doubt(self):
        group = self.group(
            *read_units(
                ((50, 600, 300, 700), "1. note", ("Footnote", "references"))
            )
        )

        self.assertEqual(
            ensemble.section(group, [FOOT_ZONE]), (ensemble.FOOTNOTES, False)
        )

    def test_one_engine_s_label_is_a_doubt(self):
        group = self.group(
            *read_units(((50, 600, 300, 700), "1. note", ("Footnote", "text")))
        )

        self.assertEqual(ensemble.section(group, []), (ensemble.BODY, True))
        self.assertEqual(ensemble._footnote_labellers(group), ["dots_mocr"])

    def test_a_silent_engine_s_label_counts_for_nothing(self):
        group = self.group(
            unit("dots_mocr", 0, (50, 600, 300, 700), "1. note"),
            unit(
                "mistral_ocr", 0, (50, 600, 300, 700), "", label="references"
            ),
        )

        self.assertEqual(ensemble.section(group, []), (ensemble.BODY, False))

    def test_a_merged_unit_of_mixed_labels_is_not_labelled(self):
        """A footnote cell glued to a body cell says nothing."""
        group = self.group(
            unit(
                "dots_mocr",
                0,
                (50, 600, 300, 650),
                "1. note",
                label="Footnote",
            ),
            unit("dots_mocr", 1, (50, 650, 300, 700), "more", label="Text"),
            unit("mistral_ocr", 0, (50, 600, 300, 700), "1. note more"),
        )

        self.assertEqual(ensemble.section(group, []), (ensemble.BODY, False))

    def test_every_engine_s_spelling_is_read(self):
        for engine, spec in opinion_ocr.ENGINES.items():
            for kind in spec.footnote_types:
                group = self.group(
                    unit(engine, 0, (50, 600, 300, 700), "1. note", label=kind)
                )
                self.assertEqual(
                    ensemble.section(group, []),
                    (ensemble.BODY, True),
                    (engine, kind),
                )

    def test_an_unknown_engine_is_not_labelled(self):
        group = self.group(
            unit("other", 0, (50, 600, 300, 700), "1. note", label="Footnote")
        )

        self.assertEqual(ensemble.section(group, []), (ensemble.BODY, False))


class TestTheTwoTexts(TestCase):
    """``build_page`` over a hand-built page: the order and the offsets."""

    #: Two columns of two paragraphs, and one footnote at the foot of
    #: the left column, under the band.
    TWO_COLUMNS = (
        ((LEFT_X[0], 100, LEFT_X[1], 200), "left one"),
        ((LEFT_X[0], 300, LEFT_X[1], 400), "left two"),
        ((RIGHT_X[0], 100, RIGHT_X[1], 200), "right one"),
        ((RIGHT_X[0], 300, RIGHT_X[1], 400), "right two"),
    )
    LEFT_NOTE = ((LEFT_X[0], 600, LEFT_X[1], 700), "1. left note")
    RIGHT_NOTE = ((RIGHT_X[0], 600, RIGHT_X[1], 700), "2. right note")

    def test_the_body_joins_across_a_footnote(self):
        """The order of the whole page put the footnote between the
        two columns (#317). The body is ordered without it."""
        page = build(
            read_units(*self.TWO_COLUMNS, self.LEFT_NOTE), zones=[FOOT_ZONE]
        )

        self.assertEqual(
            page["text"], "left one\n\nleft two\n\nright one\n\nright two"
        )
        self.assertEqual(page["footnotes"], "1. left note")
        self.assertEqual(page["zones"], {"footnotes": [FOOT_ZONE]})

    def test_without_a_zone_the_footnote_stays_in_the_body(self):
        page = build(read_units(*self.TWO_COLUMNS, self.LEFT_NOTE))

        self.assertEqual(
            page["text"],
            "left one\n\nleft two\n\n1. left note\n\nright one\n\nright two",
        )
        self.assertEqual(page["footnotes"], "")

    def test_two_footnotes_read_left_then_right(self):
        """The footnotes take the boundary of the whole page: two boxes
        of their own could not find a gutter."""
        page = build(
            read_units(*self.TWO_COLUMNS, self.RIGHT_NOTE, self.LEFT_NOTE),
            zones=[FOOT_ZONE],
        )

        self.assertEqual(page["footnotes"], "1. left note\n\n2. right note")
        notes = [
            g for g in page["groups"] if g["section"] == ensemble.FOOTNOTES
        ]
        self.assertEqual([g["column"] for g in notes], ["L", "R"])

    def test_a_footnote_in_the_foot_band_keeps_its_column(self):
        """A short last footnote sits in the foot band of ``place``,
        which reads with no column (review of #401). The footnotes
        split no band."""
        low_zone = [40.0, 690.0, 580.0, 785.0]
        right = ((RIGHT_X[0], 700, RIGHT_X[1], 780), "2. right note")
        left = ((LEFT_X[0], 760, LEFT_X[1], 780), "1. left note")
        page = build(
            read_units(*self.TWO_COLUMNS, right, left), zones=[low_zone]
        )

        self.assertEqual(page["footnotes"], "1. left note\n\n2. right note")
        notes = [
            g for g in page["groups"] if g["section"] == ensemble.FOOTNOTES
        ]
        self.assertEqual([g["column"] for g in notes], ["L", "R"])
        self.assertEqual({g["band"] for g in notes}, {ensemble.FOOTNOTES})

    def test_every_group_names_its_section_and_its_own_offsets(self):
        page = build(
            read_units(*self.TWO_COLUMNS, self.RIGHT_NOTE, self.LEFT_NOTE),
            zones=[FOOT_ZONE],
        )

        for group in page["groups"]:
            text = page[group["section"]]
            self.assertEqual(
                text[group["start"] : group["end"]], group["text"]
            )
        notes = [
            g for g in page["groups"] if g["section"] == ensemble.FOOTNOTES
        ]
        self.assertEqual(notes[0]["start"], 0)
        self.assertEqual(page["counts"]["footnote_groups"], 2)
        self.assertEqual(page["counts"]["footnote_doubt"], 0)

    def test_a_labelled_footnote_outside_the_zone_is_counted(self):
        note = (self.LEFT_NOTE[0], self.LEFT_NOTE[1], ("Footnote", "text"))
        page = build(read_units(*self.TWO_COLUMNS, note))

        group = next(g for g in page["groups"] if g["footnote_doubt"])
        self.assertEqual(group["section"], ensemble.BODY)
        self.assertEqual(group["footnote_by"], ["dots_mocr"])
        self.assertEqual(page["counts"]["footnote_doubt"], 1)

    def test_a_dropped_group_counts_no_doubt(self):
        units = read_units(*self.TWO_COLUMNS)
        excluded = {"reason": "redaction", "rect_type": "", "fill": ""}
        for engine in ("dots_mocr", "mistral_ocr"):
            units.append(
                unit(
                    engine,
                    9,
                    self.LEFT_NOTE[0],
                    "1. note",
                    exclusion=excluded,
                    share=1.0,
                    label="Footnote" if engine == "dots_mocr" else "footer",
                )
            )
        page = build(units)

        self.assertEqual(page["counts"]["footnote_doubt"], 0)
        self.assertEqual(len(page["dropped"]), 1)

    def test_the_zones_come_off_the_first_engine_page_that_has_them(self):
        units = read_units(*self.TWO_COLUMNS, self.LEFT_NOTE)
        pages = {
            "dots_mocr": engine_page(
                [u for u in units if u["engine"] == "dots_mocr"]
            ),
            "mistral_ocr": engine_page(
                [u for u in units if u["engine"] == "mistral_ocr"], [FOOT_ZONE]
            ),
        }

        page = ensemble.build_page(pages, 0)

        self.assertEqual(page["footnotes"], "1. left note")


class TestTheFootnotesOfAnOpinion(EnsembleTestCase):
    """The fixture of the glue with a footnote band over body B."""

    #: The band over the second body cell of every page, so body A
    #: stays in the text and body B goes to the footnotes.
    BAND_B = (BODY_B[0], BODY_B[1] - 20, BODY_B[2] + 800, BODY_B[3] + 20)

    def band(self, page_index):
        footnote_band(self.scan, self.apply_run, page_index, self.BAND_B)

    def test_the_footnotes_of_a_page_are_their_own_text(self):
        self.band(2)

        document = self.run_ensemble()

        page = document["pages"][1]
        # The header is the printed page number, excluded since #396.
        self.assertEqual(page["text"], "body A 2")
        self.assertEqual(page["footnotes"], "body B 2")
        self.assertEqual(page["zones"], {"footnotes": [to_pt(self.BAND_B)]})
        # The first page masks its header, above the caption.
        self.assertEqual(document["pages"][0]["text"], "body A 1\n\nbody B 1")
        self.assertEqual(document["pages"][0]["footnotes"], "")
        self.assertEqual(document["counts"]["footnote_groups"], 1)

    def test_the_row_carries_the_footnotes_apart(self):
        self.band(2)
        self.run_ensemble()

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=1)
        self.assertEqual(row.text, "body A 2")
        self.assertEqual(row.footnotes, "body B 2")
        other = OpinionText.objects.get(
            opinion=self.opinion, page_in_opinion=0
        )
        self.assertEqual(other.footnotes, "")

    def test_a_disagreement_in_the_footnotes_names_its_section(self):
        self.band(2)
        document = mistral_document()
        for entry in document["pages"][2]["blocks"]:
            if entry["content"] == "body B 2":
                entry["content"] = "body B two"
        self.objects[self.apply_run.extract_key] = document

        self.run_ensemble()

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=1)
        self.assertEqual(len(row.disagreements), 1)
        entry = row.disagreements[0]
        self.assertEqual(entry["section"], ensemble.FOOTNOTES)
        self.assertEqual(
            row.footnotes[entry["start"] : entry["end"]], "body B 2"
        )

    def test_a_second_run_leaves_the_curator_s_text_alone(self):
        self.band(2)
        self.run_ensemble()
        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=1)
        row.human_text = "typed"
        row.save(update_fields=["human_text"])

        self.run_ensemble()

        row.refresh_from_db()
        self.assertEqual(row.human_text, "typed")
        self.assertEqual(row.footnotes, "body B 2")

    def test_a_footnote_label_outside_the_zone_is_a_card(self):
        document = mistral_document()
        for entry in document["pages"][2]["blocks"]:
            if entry["content"] == "body B 2":
                entry["type"] = "references"
        self.objects[self.apply_run.extract_key] = document

        self.run_ensemble()

        card = OpinionFinding.objects.get(
            opinion=self.opinion, check_name=OpinionCheck.FOOTNOTE_UNSURE
        )
        self.assertEqual(card.page_in_opinion, 1)
        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertIn("mistral_ocr read 1 block(s)", card.message)
        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=1)
        self.assertIn("body B 2", row.text)

    def test_a_body_label_under_the_zone_is_no_card(self):
        self.band(2)

        self.run_ensemble()

        self.assertFalse(
            OpinionFinding.objects.filter(
                opinion=self.opinion, check_name=OpinionCheck.FOOTNOTE_UNSURE
            ).exists()
        )


class TestTheFootnoteCard(TestTheFindings):
    def test_a_doubt_is_its_own_card(self):
        cards = self.rebuild(footnote_doubt=1)

        self.assertEqual([c.check_name for c in cards], ["footnote_unsure"])
        self.assertIn("An engine read 1 block(s)", cards[0].message)

    def test_the_card_names_the_engines(self):
        ensemble.rebuild_findings(
            self.opinion,
            document_of(
                page_of(
                    groups=[
                        {
                            "footnote_doubt": True,
                            "footnote_by": ["mistral_ocr", "dots_mocr"],
                        }
                    ],
                    footnote_doubt=1,
                )
            ),
        )

        card = OpinionFinding.objects.get(opinion=self.opinion)
        self.assertIn("dots_mocr, mistral_ocr read 1 block(s)", card.message)

    def test_a_standing_dismissal_mutes_the_card(self):
        dismissal = OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.FOOTNOTE_UNSURE,
        )

        cards = self.rebuild(footnote_doubt=1)

        self.assertEqual(cards[0].dismissal_id, dismissal.pk)

    def test_the_check_is_the_rebuild_s_own(self):
        self.assertIn(OpinionCheck.FOOTNOTE_UNSURE, ensemble.ENSEMBLE_CHECKS)


class TestTheBracketToken(EnsembleTestCase):
    """The bracket a box redacts never reaches the text (#373)."""

    def test_the_text_of_the_page_holds_no_bracket(self):
        for key, units, field in (
            (self.apply_run.ocr_key, "cells", "text"),
            (self.apply_run.extract_key, "blocks", "content"),
        ):
            self.objects[key]["pages"][2][units][1][field] = "[1] body A 2"
        self.redact(
            2, to_pt((105, 305, 140, 330)), rect_type="HEADNOTE_BRACKET"
        )

        document = self.run_ensemble()

        self.assertEqual(document["pages"][1]["text"], "body A 2\n\nbody B 2")
        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=1)
        self.assertNotIn("[1]", row.text)


# ── the marks and the kind (#404) ────────────────────────────────────
def three(*specs) -> dict:
    """A group of up to three engines, each ``(text, marks, kind)``."""
    names = ("dots_mocr", "mistral_ocr", "surya")
    units = []
    for index, spec in enumerate(specs):
        text, marks = spec[0], spec[1] if len(spec) > 1 else ()
        kind = spec[2] if len(spec) > 2 else "paragraph"
        units.append(
            unit(names[index], 0, BODY_A_PT, text, marks=marks, kind=kind)
        )
    return group_of(*units)


class TestTheMarks(TestCase):
    """The marks of a group are the union of its readings."""

    def test_a_word_one_engine_marks_is_marked(self):
        answer = ensemble.resolve(
            three(
                ("In Castleman, Justice", [em(3, 12)]),
                ("In Castleman, Justice",),
            )
        )

        self.assertEqual(answer["agreement"], ensemble.UNANIMOUS)
        # The word carries the mark, comma included.
        self.assertEqual(answer["marks"], [em(3, 13)])

    def test_adjacent_marked_words_are_one_span(self):
        answer = ensemble.resolve(
            three(
                ("see Lewis v. Marcotte, at 1", [em(4, 21)]),
                ("see Lewis v. Marcotte, at 1",),
            )
        )

        self.assertEqual(answer["marks"], [em(4, 22)])

    def test_a_superscript_is_the_chars_and_not_the_word(self):
        answer = ensemble.resolve(
            three(('acts."1 The', [sup(6, 7)]), ('acts."1 The',))
        )

        self.assertEqual(answer["marks"], [sup(6, 7)])

    def test_the_marks_of_every_engine_join(self):
        answer = ensemble.resolve(
            three(
                ("Held: see Id. there", [strong(0, 5)]),
                ("Held: see Id. there", [em(10, 13)]),
            )
        )

        self.assertEqual(answer["marks"], [strong(0, 5), em(10, 13)])

    def test_an_ellipsis_before_the_italic_moves_no_mark(self):
        """Equal keys, different word counts: the alignment is by key."""
        answer = ensemble.resolve(
            three(
                ("said . . . so Held", []), ("said ... so Held", [em(12, 16)])
            )
        )

        self.assertEqual(answer["agreement"], ensemble.UNANIMOUS)
        self.assertEqual(answer["text"], "said . . . so Held")
        self.assertEqual(answer["marks"], [em(14, 18)])

    def test_a_voted_group_keeps_the_marks_of_the_words_that_won(self):
        answer = ensemble.resolve(
            three(
                ("the court held", [em(4, 9)]),
                ("the court hold",),
                ("the court helt",),
            )
        )

        self.assertEqual(answer["agreement"], ensemble.VOTED)
        self.assertEqual(answer["marks"], [em(4, 9)])

    def test_a_word_the_source_did_not_read_carries_its_engine_s_mark(self):
        answer = ensemble.resolve(
            three(
                ("the court", []),
                ("the court held", [em(10, 14)]),
                ("the court held", []),
            )
        )

        self.assertEqual(answer["agreement"], ensemble.MAJORITY)
        self.assertEqual(answer["text"], "the court held")
        self.assertEqual(answer["marks"], [em(10, 14)])

    def test_a_silent_engine_marks_nothing(self):
        answer = ensemble.resolve(three(("the court held", []), ("", [])))

        self.assertEqual(answer["marks"], [])
        self.assertEqual(answer["kind"], "paragraph")

    def test_a_unit_of_the_glue_before_the_marks_reads_plain(self):
        page = engine_page([unit("dots_mocr", 0, BODY_A_PT, "the text")])
        for u in page["units"]:
            del u["marks"], u["kind"], u["table"]
        other = engine_page([unit("mistral_ocr", 0, BODY_A_PT, "the text")])

        entry = ensemble.build_page(
            {"dots_mocr": page, "mistral_ocr": other}, 0
        )

        group = entry["groups"][0]
        self.assertEqual((group["kind"], group["marks"]), ("paragraph", []))
        self.assertNotIn("table", group)

    def test_the_marks_of_two_members_shift_by_the_join(self):
        groups = ensemble.align_page(
            [
                unit(
                    "dots_mocr",
                    0,
                    (50, 600, 300, 650),
                    "left one",
                    marks=[em(0, 4)],
                ),
                unit(
                    "dots_mocr",
                    1,
                    (50, 650, 300, 700),
                    "left two",
                    marks=[em(5, 8)],
                ),
                unit(
                    "mistral_ocr", 0, (50, 600, 300, 700), "left one left two"
                ),
            ],
            WIDTH,
            HEIGHT,
        )

        self.assertEqual(len(groups), 1)
        merged = groups[0]["engines"]["dots_mocr"]
        self.assertEqual(merged["text"], "left one left two")
        self.assertEqual(merged["marks"], [em(0, 4), em(14, 17)])
        self.assertEqual(
            ensemble.resolve(groups[0])["marks"], [em(0, 4), em(14, 17)]
        )

    def test_a_text_plain_shortens_keeps_its_words_and_loses_its_marks(self):
        groups = ensemble.align_page(
            [
                unit("dots_mocr", 0, BODY_A_PT, "a  b", marks=[em(0, 1)]),
                unit("mistral_ocr", 0, BODY_A_PT, "a b"),
            ],
            WIDTH,
            HEIGHT,
        )

        merged = groups[0]["engines"]["dots_mocr"]
        self.assertEqual((merged["text"], merged["marks"]), ("a b", []))


class TestTheKind(TestCase):
    """The kind of a group is a majority, the rank breaking a tie."""

    def test_the_majority_of_the_engines_names_the_kind(self):
        answer = ensemble.resolve(
            three(
                ("FACTS", [], "heading"),
                ("FACTS", [], "heading"),
                ("FACTS", [], "paragraph"),
            )
        )

        self.assertEqual(answer["kind"], "heading")

    def test_a_tie_goes_to_the_first_engine(self):
        answer = ensemble.resolve(
            three(
                ("Amanda JONES", [], "paragraph"),
                ("Amanda JONES", [], "heading"),
            )
        )

        # Mistral labels the caption ``title``; dots says text, and wins.
        self.assertEqual(answer["kind"], "paragraph")

    def test_a_silent_engine_has_no_say(self):
        # Two paragraphs against one heading, were the silent engine
        # counted; a tie the first engine breaks, since it is not.
        answer = ensemble.resolve(
            three(
                ("FACTS", [], "heading"),
                ("", [], "paragraph"),
                ("FACTS", [], "paragraph"),
            )
        )

        self.assertEqual(answer["kind"], "heading")

    def test_mixed_members_of_one_engine_are_a_paragraph(self):
        groups = ensemble.align_page(
            [
                unit(
                    "dots_mocr",
                    0,
                    (50, 600, 300, 650),
                    "FACTS",
                    kind="heading",
                ),
                unit(
                    "dots_mocr",
                    1,
                    (50, 650, 300, 700),
                    "the body",
                    kind="paragraph",
                ),
                unit(
                    "mistral_ocr",
                    0,
                    (50, 600, 300, 700),
                    "FACTS the body",
                    kind="heading",
                ),
            ],
            WIDTH,
            HEIGHT,
        )

        self.assertEqual(
            groups[0]["engines"]["dots_mocr"]["kind"], "paragraph"
        )
        self.assertEqual(ensemble.resolve(groups[0])["kind"], "paragraph")

    def test_the_kind_is_on_the_group_of_the_page(self):
        entry = build(
            [
                unit("dots_mocr", 0, BODY_A_PT, "FACTS", kind="heading"),
                unit("mistral_ocr", 0, BODY_A_PT, "FACTS", kind="heading"),
            ]
        )

        self.assertEqual(entry["groups"][0]["kind"], "heading")


class TestTheTable(TestCase):
    def test_the_rows_are_the_source_s(self):
        rows = [["Property Damage", "$35,000.00"]]
        answer = ensemble.resolve(
            three(
                ("Property Damage $35,000.00", [], "table"),
                ("Property Damage $35,000.00", [], "table"),
            )
        )
        self.assertEqual(answer["table"], None)

        group = three(
            ("Property Damage $35,000.00", [], "table"),
            ("Property Damage $35,000.00", [], "table"),
        )
        group["engines"]["dots_mocr"]["table"] = rows
        group["engines"]["mistral_ocr"]["table"] = [["other"]]
        answer = ensemble.resolve(group)

        self.assertEqual((answer["kind"], answer["table"]), ("table", rows))

    def test_two_table_members_concatenate_their_rows(self):
        groups = ensemble.align_page(
            [
                unit(
                    "dots_mocr",
                    0,
                    (50, 600, 300, 650),
                    "a b",
                    kind="table",
                    table=[["a", "b"]],
                ),
                unit(
                    "dots_mocr",
                    1,
                    (50, 650, 300, 700),
                    "c d",
                    kind="table",
                    table=[["c", "d"]],
                ),
                unit(
                    "mistral_ocr",
                    0,
                    (50, 600, 300, 700),
                    "a b c d",
                    kind="table",
                    table=[["a", "b"], ["c", "d"]],
                ),
            ],
            WIDTH,
            HEIGHT,
        )

        self.assertEqual(
            groups[0]["engines"]["dots_mocr"]["table"],
            [["a", "b"], ["c", "d"]],
        )

    def test_a_paragraph_group_carries_no_rows(self):
        entry = build(
            [
                unit("dots_mocr", 0, BODY_A_PT, "the body"),
                unit("mistral_ocr", 0, BODY_A_PT, "the body"),
            ]
        )

        self.assertNotIn("table", entry["groups"][0])

    def test_a_table_group_carries_its_rows(self):
        rows = [["a", "b"]]
        entry = build(
            [
                unit(
                    "dots_mocr", 0, BODY_A_PT, "a b", kind="table", table=rows
                ),
                unit(
                    "mistral_ocr",
                    0,
                    BODY_A_PT,
                    "a b",
                    kind="table",
                    table=rows,
                ),
            ]
        )

        self.assertEqual(entry["groups"][0]["table"], rows)


class TestTheMarksOfAPage(TestCase):
    """The marks of a row point into the field their section names."""

    def test_the_marks_add_the_group_s_start_in_their_section(self):
        entry = build(
            [
                unit(
                    "dots_mocr",
                    0,
                    (LEFT_X[0], 100, LEFT_X[1], 200),
                    "left one",
                    marks=[em(5, 8)],
                ),
                unit(
                    "mistral_ocr",
                    0,
                    (LEFT_X[0], 100, LEFT_X[1], 200),
                    "left one",
                ),
                unit(
                    "dots_mocr",
                    1,
                    (LEFT_X[0], 300, LEFT_X[1], 400),
                    "left two",
                    marks=[strong(0, 4)],
                ),
                unit(
                    "mistral_ocr",
                    1,
                    (LEFT_X[0], 300, LEFT_X[1], 400),
                    "left two",
                ),
                unit(
                    "dots_mocr",
                    2,
                    (LEFT_X[0], 600, LEFT_X[1], 700),
                    "1. left note",
                    marks=[em(3, 7)],
                ),
                unit(
                    "mistral_ocr",
                    2,
                    (LEFT_X[0], 600, LEFT_X[1], 700),
                    "1. left note",
                ),
            ],
            zones=[FOOT_ZONE],
        )

        self.assertEqual(entry["text"], "left one\n\nleft two")
        self.assertEqual(entry["footnotes"], "1. left note")
        marks = ensemble._marks(entry)
        self.assertEqual(
            marks,
            [
                {"start": 5, "end": 8, "kind": "em", "section": "text"},
                {"start": 10, "end": 14, "kind": "strong", "section": "text"},
                {"start": 3, "end": 7, "kind": "em", "section": "footnotes"},
            ],
        )
        self.assertEqual(entry["text"][10:14], "left")
        self.assertEqual(entry["footnotes"][3:7], "left")


class TestTheMarksOfARow(EnsembleTestCase):
    def test_the_row_holds_the_marks_of_its_text(self):
        cells = self.objects[self.apply_run.ocr_key]["pages"][1]["cells"]
        cells[1]["text"] = "In *Castleman*, Justice"
        blocks = self.objects[self.apply_run.extract_key]["pages"][1]["blocks"]
        blocks[1]["content"] = "In Castleman, Justice"

        self.run_ensemble()

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertEqual(len(row.marks), 1)
        mark = row.marks[0]
        self.assertEqual((mark["kind"], mark["section"]), ("em", "text"))
        self.assertEqual(row.text[mark["start"] : mark["end"]], "Castleman,")

    def test_a_row_without_marks_is_empty(self):
        self.run_ensemble()

        row = OpinionText.objects.get(opinion=self.opinion, page_in_opinion=0)
        self.assertEqual(row.marks, [])
