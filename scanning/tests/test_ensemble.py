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
- the button and the command.
"""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from scanning import ensemble, opinion_ocr
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
    OpinionOcrTestCase,
    mistral_document,
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
    kind: str = "Text",
) -> dict:
    """One engine's unit of a page, in the shape the alignment reads."""
    return {
        "engine": engine,
        "id": index,
        "box_pt": [float(v) for v in box],
        "text": text,
        "type": kind,
        "exclusion": exclusion,
        "share": share,
    }


def group_of(*units) -> dict:
    """One aligned group over the given units, for the vote alone."""
    engines = {}
    for member in units:
        engines[member["engine"]] = {
            "ids": [member["id"]],
            "types": [member["type"]],
            "box_pt": member["box_pt"],
            "text": member["text"],
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
            unit("dots_mocr", 0, (40, 100, 570, 620), "", kind="Picture"),
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
            unit("dots_mocr", 0, (0, 0, 100, 60), "", kind="Picture"),
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
                {"text": "beta", "low_confidence": True},
                {"text": "gamma"},
            ],
        )
        self.assertEqual(disputed, 1)

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

    def stored(self, opinion=None) -> dict:
        """The ensemble document in the bucket."""
        opinion = opinion or self.opinion
        return self.uploads[ensemble.document_key(opinion)]


class TestTheDocument(EnsembleTestCase):
    def test_the_text_of_a_page_is_its_groups_in_order(self):
        document = self.run_ensemble()

        page = document["pages"][1]
        self.assertEqual(page["text"], "878 N. C.\n\nbody A 2\n\nbody B 2")
        self.assertEqual(
            [group["agreement"] for group in page["groups"]],
            [ensemble.UNANIMOUS] * 3,
        )
        self.assertEqual([g["band"] for g in page["groups"]][0], "head")

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
        self.assertEqual(page["text"], "878 N. C.\n\nbody B 2")
        self.assertEqual(len(page["dropped"]), 1)
        self.assertEqual(page["dropped"][0]["reason"], "redaction")
        self.assertEqual(page["counts"]["dropped"], 1)

    def test_a_part_of_a_block_under_a_box_is_a_partial_drop(self):
        self.redact(2, [36.0, 108.0, 288.0, 200.0])

        document = self.run_ensemble()

        page = document["pages"][1]
        self.assertTrue(page["dropped"][0]["partial"])
        self.assertEqual(page["counts"]["partial"], 1)

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
            for block in page["blocks"]:
                if block["content"].startswith("body A"):
                    block["content"] = ""
                    block["type"] = "picture"
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

    def test_the_document_names_the_opinion_and_the_engines(self):
        document = self.run_ensemble()

        self.assertEqual(document["engines"], ["dots_mocr", "mistral_ocr"])
        self.assertEqual(document["opinion"]["first_printed_page"], 502)
        self.assertEqual(document["apply_run"], "a1")
        self.assertEqual(len(document["pages"]), 3)
        self.assertEqual(document["counts"]["groups"], 8)

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
            for block in page["blocks"]:
                if block["content"].startswith("body A"):
                    block["content"] = text
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
        self.assertTrue(rows[1].text.startswith("878 N. C."))

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
                        "engines": {"dots_mocr": {"text": "the text"}},
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

    def test_the_pass_is_the_last_of_the_collect_tick(self):
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
            patch("django.db.connections.close_all"),
        ):
            call_command("collect_external_jobs")

        self.assertEqual(calls[-2:], ["glue", "ensemble"])


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
        self.assertIn("8 block(s)", body["message"])
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
        self.assertIn("is not in the bucket", response.json()["message"])

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

    def test_404_across_scans(self):
        other = ScanFactory()

        response = self.client.post(self.url(scan=other))

        self.assertEqual(response.status_code, 404)

    def test_the_button_refuses_a_get(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)


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
