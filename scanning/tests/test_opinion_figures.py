"""Tests for the pictures of an opinion's text (issue #463).

The engines draw a box over a picture and read no text in it. The
ensemble writes it as a ``figure`` group, the daemon cuts it from the
original (``opinion_figures``), the review page draws it and moves it,
and the approval embeds it in the approved text, which the final XML
writes as a ``figure`` holding a data-URI ``img``.

Four groups of tests:

- the alignment and the build, over plain dicts;
- the approved text, the tagger's input and the XML, pure;
- the ledger and the cut, over the fixture of the OCR glue;
- the endpoints and the viewer's pins.
"""

import base64
import pathlib
from contextlib import contextmanager
from unittest.mock import patch

import fitz
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.db.models import F
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from scanning import (
    casebody,
    ensemble,
    markup,
    opinion_figures,
    opinion_pdf,
    opinion_review,
    opinions,
    paragraphs,
    views_api,
)
from scanning.models import (
    Opinion,
    OpinionCheck,
    OpinionFinding,
    OpinionReviewStatus,
)
from scanning.tests.test_ensemble import (
    EnsembleTestCase,
    engine_page,
    unit,
)
from scanning.tests.test_opinion_ocr import block, cell, to_pt

#: A picture between the two body blocks of the fixture, in points.
PICTURE_PT = [40.0, 420.0, 280.0, 560.0]
#: A body paragraph above it and one below it, in points.
ABOVE_PT = [40.0, 100.0, 280.0, 200.0]
BELOW_PT = [40.0, 600.0, 280.0, 700.0]


def picture(engine, index, box=PICTURE_PT, label="Picture") -> dict:
    """One engine's picture unit: no text, the ``figure`` kind."""
    return unit(engine, index, box, "", label=label, kind=markup.FIGURE)


def page_of(units_by_engine: dict[str, list[dict]]) -> dict:
    """The ensemble of one page read by the engines given."""
    return ensemble.build_page(
        {
            engine: engine_page(units)
            for engine, units in units_by_engine.items()
        },
        0,
    )


def figures(page: dict) -> list[dict]:
    """The picture groups of a page."""
    return [g for g in page["groups"] if g["kind"] == markup.FIGURE]


# ── the alignment and the build ──────────────────────────────────────
class TestThePictureGroup(SimpleTestCase):
    def three_engines(self, *extra):
        return page_of(
            {
                name: [
                    unit(name, 0, ABOVE_PT, "The court held."),
                    picture(name, 1),
                    unit(name, 2, BELOW_PT, "So ordered."),
                    *[u for u in extra if u["engine"] == name],
                ]
                for name in ("dots_mocr", "mistral_ocr", "surya")
            }
        )

    def test_three_engines_draw_one_picture_in_its_place(self):
        page = self.three_engines()

        found = figures(page)
        self.assertEqual(len(found), 1)
        group = found[0]
        self.assertEqual(group["text"], "")
        self.assertEqual(group["section"], ensemble.BODY)
        self.assertIsNone(group["level"])
        self.assertEqual(
            group["figure"], {"width_pt": 240.0, "height_pt": 140.0}
        )
        self.assertEqual(
            [g["kind"] for g in page["groups"]],
            [markup.PARAGRAPH, markup.FIGURE, markup.PARAGRAPH],
        )
        # The body text takes no paragraph for the picture.
        self.assertEqual(page["text"], "The court held.\n\nSo ordered.")
        self.assertEqual(page["counts"]["figures"], 1)
        self.assertEqual(page["counts"]["figure_single"], 0)

    def test_a_picture_box_over_the_text_takes_none_of_it(self):
        """The guard of the alignment: a box that reads nothing never
        chains the text of a page into one group."""
        big = [30.0, 90.0, 290.0, 710.0]
        page = page_of(
            {
                "dots_mocr": [
                    unit("dots_mocr", 0, ABOVE_PT, "The court held."),
                    unit("dots_mocr", 1, BELOW_PT, "So ordered."),
                    picture("dots_mocr", 2, big),
                ],
                "mistral_ocr": [
                    unit("mistral_ocr", 0, ABOVE_PT, "The court held."),
                    unit("mistral_ocr", 1, BELOW_PT, "So ordered."),
                ],
            }
        )

        texts = [g for g in page["groups"] if g["kind"] != markup.FIGURE]
        self.assertEqual(
            [g["text"] for g in texts], ["The court held.", "So ordered."]
        )
        self.assertEqual(len(figures(page)), 1)

    def test_one_engine_alone_counts_for_the_card(self):
        page = page_of(
            {
                "dots_mocr": [picture("dots_mocr", 0)],
                "mistral_ocr": [
                    unit("mistral_ocr", 0, ABOVE_PT, "The court held.")
                ],
            }
        )

        self.assertEqual(page["counts"]["figure_single"], 1)
        self.assertEqual(figures(page)[0]["agreement"], ensemble.SINGLE)

    def test_a_picture_under_a_redaction_is_dropped(self):
        redacted = {"reason": "redaction", "rect_type": "manual"}
        page = page_of(
            {
                name: [
                    {**picture(name, 0), "exclusion": redacted, "share": 1.0}
                ]
                for name in ("dots_mocr", "mistral_ocr")
            }
        )

        self.assertEqual(figures(page), [])
        self.assertEqual(page["dropped"][0]["reason"], "redaction")

    def test_a_picture_is_never_a_footnote_or_a_quote(self):
        zone = [30.0, 400.0, 290.0, 580.0]
        pages = {
            name: engine_page([picture(name, 0)], zones=[zone], quotes=[zone])
            for name in ("dots_mocr", "mistral_ocr")
        }

        page = ensemble.build_page(pages, 0)

        group = figures(page)[0]
        self.assertEqual(group["section"], ensemble.BODY)
        self.assertFalse(group["blockquote"])
        self.assertEqual(page["blockquotes"], [])

    def test_a_picture_is_in_no_block_below(self):
        """A push to the footnotes takes the blocks below it, never a
        picture: its edit would land on nothing."""
        page = self.three_engines()

        above = page["groups"][0]
        self.assertNotIn(figures(page)[0]["id"], above["below"])
        self.assertNotIn("below", figures(page)[0])

    def test_a_picture_at_the_head_of_the_page_is_body_text(self):
        """The approved text reads the body band alone, so a picture in
        the head band must not fall out of it."""
        top = [100.0, 10.0, 500.0, 60.0]
        page = page_of(
            {
                name: [picture(name, 0, top), unit(name, 1, BELOW_PT, "Text.")]
                for name in ("dots_mocr", "mistral_ocr")
            }
        )

        self.assertEqual(figures(page)[0]["band"], "body")
        body = paragraphs.body({"pages": [{"page_in_opinion": 0, **page}]})
        self.assertEqual(
            [p["kind"] for p in body], [markup.FIGURE, markup.PARAGRAPH]
        )

    def test_a_document_glued_before_the_kind_reads_the_label(self):
        """A document of the glue before #463 has no ``figure`` kind;
        the label of the engine names the picture."""
        old = unit("dots_mocr", 0, PICTURE_PT, "", label="Picture")
        page = {"units": [{**old, "kind": None}]}

        placed, _ = ensemble._units_of(page, "dots_mocr")

        self.assertEqual(placed[0]["kind"], markup.FIGURE)


class TestTheColumnBoundary(SimpleTestCase):
    """A box across the middle of the page votes for no column (#463).

    The boundary is read off the left edges of the body boxes, at the
    widest gap between them. A picture as wide as both columns, or a
    centered line, has its left edge inside the left column, and that
    edge split the left column from its own text: every left box then
    read as a full-width band, and the two columns were interleaved.
    """

    WIDTH, HEIGHT = 612.0, 792.0

    def boxes(self, *extra) -> list[dict]:
        columns = [
            [96.0, 100.0, 306.0, 200.0],
            [96.0, 210.0, 306.0, 300.0],
            [96.0, 480.0, 306.0, 600.0],
            [313.0, 100.0, 527.0, 300.0],
            [315.0, 480.0, 526.0, 600.0],
        ]
        return [{"box_pt": box} for box in [*columns, *extra]]

    def test_a_picture_and_a_centered_line_leave_the_gutter(self):
        plain = ensemble.column_boundary(self.boxes(), self.WIDTH, self.HEIGHT)
        crossed = ensemble.column_boundary(
            self.boxes(
                [120.0, 310.0, 505.0, 470.0],
                [245.0, 80.0, 374.0, 92.0],
            ),
            self.WIDTH,
            self.HEIGHT,
        )

        self.assertEqual(crossed, plain)
        self.assertGreater(crossed, 300.0)

    def test_a_two_column_page_keeps_its_gutter(self):
        """No box crosses the middle: the right column's first edge less
        the pad, the boundary the rule gave before #463."""
        boundary = ensemble.column_boundary(
            self.boxes(), self.WIDTH, self.HEIGHT
        )

        self.assertAlmostEqual(
            boundary, 313.0 - ensemble.EDGE_PAD * self.WIDTH
        )

    def test_a_page_set_off_the_middle_keeps_its_gutter(self):
        """A scan of a bound book sets the text off the page's middle.
        The columns of a page shifted either way keep their gutter, the
        one the rule gave before #463, with no picture on the page."""
        for shift in (-40.0, -25.0, 25.0, 40.0):
            boxes = [
                {"box_pt": [b[0] + shift, b[1], b[2] + shift, b[3]]}
                for b in (box["box_pt"] for box in self.boxes())
            ]

            boundary = ensemble.column_boundary(boxes, self.WIDTH, self.HEIGHT)

            self.assertAlmostEqual(
                boundary,
                313.0 + shift - ensemble.EDGE_PAD * self.WIDTH,
                msg=f"shift {shift}",
            )

    def test_a_heading_inside_a_column_still_votes(self):
        """A centered heading of one column ("A. Empaneling Jurors")
        stays on its side of the middle, so it is a box of its column
        and the gutter does not move."""
        heading = [129.0, 310.0, 250.0, 325.0]

        boundary = ensemble.column_boundary(
            self.boxes(heading), self.WIDTH, self.HEIGHT
        )

        self.assertAlmostEqual(
            boundary, 313.0 - ensemble.EDGE_PAD * self.WIDTH
        )

    def test_a_one_column_page_has_no_boundary(self):
        """Every block of a one-column page crosses the middle, so none
        votes, and the page reads as one column, as before."""
        wide = [
            {"box_pt": [72.0, top, 540.0, top + 80.0]}
            for top in (100.0, 200.0, 300.0, 400.0, 500.0)
        ]

        self.assertIsNone(
            ensemble.column_boundary(wide, self.WIDTH, self.HEIGHT)
        )

    def test_a_two_column_page_with_a_full_width_title_keeps_its_order(self):
        """A full-width line across both columns, with no picture: the
        line reads where it sits and each column reads whole after it,
        the order the rule gave before #463."""
        boxes = [
            {"box_pt": box, "name": name}
            for name, box in (
                ("title", [150.0, 60.0, 460.0, 80.0]),
                ("left 1", [96.0, 100.0, 306.0, 200.0]),
                ("right 1", [313.0, 100.0, 527.0, 300.0]),
                ("left 2", [96.0, 210.0, 306.0, 300.0]),
                ("right 2", [315.0, 310.0, 526.0, 400.0]),
                ("left 3", [96.0, 310.0, 306.0, 400.0]),
            )
        ]
        boundary = ensemble.column_boundary(boxes, self.WIDTH, self.HEIGHT)

        ordered = ensemble.reading_order(
            boxes, ensemble.LINE_BAND * self.HEIGHT, boundary, self.WIDTH
        )

        self.assertEqual(
            [box["name"] for box in ordered],
            ["title", "left 1", "left 2", "left 3", "right 1", "right 2"],
        )

    def test_the_columns_read_around_the_picture(self):
        """Left then right above the picture, the picture, then left
        then right below it. The centered line at the top is the page
        that showed the fault: with the picture, its left edge took the
        widest gap, and the left column read as full-width bands."""
        boxes = [
            {"box_pt": box, "name": name}
            for name, box in (
                ("centered", [245.0, 80.0, 374.0, 92.0]),
                ("above left 1", [96.0, 100.0, 306.0, 200.0]),
                ("above right", [313.0, 100.0, 527.0, 300.0]),
                ("above left 2", [96.0, 210.0, 306.0, 300.0]),
                ("picture", [120.0, 310.0, 505.0, 470.0]),
                ("below right", [315.0, 480.0, 526.0, 600.0]),
                ("below left", [96.0, 480.0, 306.0, 600.0]),
            )
        ]
        boundary = ensemble.column_boundary(boxes, self.WIDTH, self.HEIGHT)

        ordered = ensemble.reading_order(
            boxes, ensemble.LINE_BAND * self.HEIGHT, boundary, self.WIDTH
        )

        self.assertEqual(
            [box["name"] for box in ordered],
            [
                "centered",
                "above left 1",
                "above left 2",
                "above right",
                "picture",
                "below left",
                "below right",
            ],
        )


class TestTheDigest(SimpleTestCase):
    def document(self, box=PICTURE_PT, run="a1") -> dict:
        return {
            "apply_run": run,
            "pages": [
                {
                    "page_in_opinion": 0,
                    "page_index": 4,
                    "groups": [
                        {"id": 0, "kind": markup.PARAGRAPH, "box_pt": [0] * 4},
                        {
                            "id": 1,
                            "kind": markup.FIGURE,
                            "box_pt": box,
                            "figure": {"width_pt": 1.0, "height_pt": 2.0},
                        },
                    ],
                }
            ],
        }

    def test_the_pictures_of_a_document(self):
        self.assertEqual(
            ensemble.figures_of(self.document()),
            [
                {
                    "page_in_opinion": 0,
                    "page_index": 4,
                    "group": 1,
                    "box_pt": PICTURE_PT,
                    "width_pt": 1.0,
                    "height_pt": 2.0,
                }
            ],
        )

    def test_the_digest_follows_the_run_and_the_place(self):
        digest = ensemble.figure_digest(self.document())

        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, ensemble.figure_digest(self.document()))
        self.assertNotEqual(
            digest, ensemble.figure_digest(self.document(run="a2"))
        )
        moved = [PICTURE_PT[0] + 5, *PICTURE_PT[1:]]
        self.assertNotEqual(
            digest, ensemble.figure_digest(self.document(box=moved))
        )

    def test_a_text_with_no_picture_has_no_digest(self):
        document = self.document()
        document["pages"][0]["groups"].pop()

        self.assertEqual(ensemble.figure_digest(document), "")


# ── the approved text, the tagger's input and the XML ────────────────
def body_with_picture(data="QUJD") -> list[dict]:
    """An approved body: a paragraph, a picture, a paragraph."""
    figure = {"width_pt": 240.0, "height_pt": 140.0}
    if data is not None:
        figure.update(content_type="image/jpeg", data=data)
    return [
        {"kind": "paragraph", "text": "The court held.", "marks": [],
         "pages": [0], "page_breaks": [], "joins": []},
        {"kind": "figure", "text": "", "marks": [], "pages": [0],
         "page_breaks": [], "joins": [], "figure": figure},
        {"kind": "paragraph", "text": "So ordered.", "marks": [],
         "pages": [0], "page_breaks": [], "joins": []},
    ]  # fmt: skip


class TestTheApprovedText(SimpleTestCase):
    def test_the_constant_is_the_kind_of_markup(self):
        self.assertEqual(paragraphs.FIGURE, markup.FIGURE)

    def test_a_picture_is_a_paragraph_of_its_own(self):
        """Nothing joins across a picture, and it keeps its size."""
        group = {
            "id": 1,
            "kind": markup.FIGURE,
            "section": paragraphs.BODY_SECTION,
            "band": paragraphs.BODY_BAND,
            "column": None,
            "text": "",
            "box_pt": PICTURE_PT,
            "figure": {"width_pt": 240.0, "height_pt": 140.0},
        }
        text = {
            "id": 0,
            "kind": "paragraph",
            "section": paragraphs.BODY_SECTION,
            "band": paragraphs.BODY_BAND,
            "column": None,
            "text": "The court held that",
        }
        after = {**text, "id": 2, "text": "the appeal fails."}
        document = {
            "pages": [{"page_in_opinion": 0, "groups": [text, group, after]}]
        }

        body = paragraphs.body(document)

        self.assertEqual(
            [p["kind"] for p in body], ["paragraph", "figure"] + ["paragraph"]
        )
        self.assertEqual(
            body[1]["figure"],
            {"width_pt": 240.0, "height_pt": 140.0, "box_pt": PICTURE_PT},
        )

    def test_the_tagger_never_reads_the_picture(self):
        projection = markup.project(body_with_picture())

        self.assertEqual(projection.paragraphs, [0, 2])
        self.assertNotIn("QUJD", projection.text)


class TestTheXml(SimpleTestCase):
    def build(self, body) -> str:
        return casebody.build(
            {"opinion": {}, "pages": [], "footnotes": [], "body": body},
            {"spans": []},
        )

    def test_the_picture_is_an_img_at_its_printed_size(self):
        xml = self.build(body_with_picture())

        self.assertIn(
            '<figure><img src="data:image/jpeg;base64,QUJD" alt="" '
            'width="320" height="187"/></figure>',
            xml,
        )
        self.assertLess(xml.index("The court held."), xml.index("<figure>"))
        self.assertLess(xml.index("</figure>"), xml.index("So ordered."))

    def test_a_picture_with_no_image_writes_no_img(self):
        for data in (None, 'x" onerror="alert(1)'):
            xml = self.build(body_with_picture(data=data))

            self.assertIn("<figure></figure>", xml)
            self.assertNotIn("<img", xml)

    def test_the_display_draws_the_picture(self):
        html = casebody.display_html(self.build(body_with_picture()))

        self.assertIn(
            '<figure class="cb-figure"><img src="data:image/jpeg;base64,'
            'QUJD" alt="" width="320" height="187"></figure>',
            html,
        )


def white_jpeg(width: int, height: int) -> bytes:
    """A white JPEG of the size given."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pix.clear_with(255)
    return pix.tobytes("jpeg")


class TestThePaint(SimpleTestCase):
    """``opinion_figures.paint``: the rects of a page over a cut."""

    BOX = [100.0, 200.0, 300.0, 300.0]

    def test_a_black_rect_lands_at_its_place_in_the_cut(self):
        data = white_jpeg(400, 200)
        rect = {"x0": 150.0, "y0": 250.0, "x1": 200.0, "y1": 275.0}

        painted = fitz.Pixmap(
            opinion_figures.paint(data, self.BOX, [{**rect, "fill": "black"}])
        )

        # Points 150-200 x 250-275 are pixels 100-200 x 100-150.
        self.assertLess(sum(painted.pixel(150, 125)) / 3, 60)
        self.assertGreater(sum(painted.pixel(50, 50)) / 3, 200)
        self.assertGreater(sum(painted.pixel(250, 125)) / 3, 200)

    def test_a_rect_off_the_picture_changes_nothing(self):
        data = white_jpeg(400, 200)
        rect = {
            "x0": 10.0,
            "y0": 10.0,
            "x1": 50.0,
            "y1": 50.0,
            "fill": "black",
        }

        self.assertEqual(opinion_figures.paint(data, self.BOX, [rect]), data)

    def test_a_rect_over_the_edge_is_clipped_to_the_picture(self):
        data = white_jpeg(400, 200)
        rect = {
            "x0": 0.0,
            "y0": 0.0,
            "x1": 120.0,
            "y1": 600.0,
            "fill": "black",
        }

        painted = fitz.Pixmap(opinion_figures.paint(data, self.BOX, [rect]))

        self.assertLess(sum(painted.pixel(20, 100)) / 3, 60)
        self.assertGreater(sum(painted.pixel(60, 100)) / 3, 200)


# ── the ledger and the cut ───────────────────────────────────────────
class FigureTestCase(EnsembleTestCase):
    """The fixture of the OCR glue, with a picture on page 2 of the
    volume (the second page of the opinion) that both engines draw."""

    #: The picture in render pixels: below the two body cells.
    PICTURE = (100, 1500, 800, 2000)

    def setUp(self):
        super().setUp()
        self.objects[self.apply_run.ocr_key]["pages"][2]["cells"].append(
            cell(*self.PICTURE, text="", category="Picture")
        )
        self.objects[self.apply_run.extract_key]["pages"][2]["blocks"].append(
            block(
                *self.PICTURE, text="![img-0.jpeg](img-0.jpeg)", kind="image"
            )
        )
        self.cuts: dict[str, bytes] = {}
        upload = patch(
            "scanning.s3_sync.upload_bytes_object",
            side_effect=self.upload_bytes,
        )
        upload.start()
        self.addCleanup(upload.stop)

    def upload_bytes(self, key, data, content_type):
        self.cuts[key] = data
        self.objects[key] = data
        return True

    @contextmanager
    def fake_source(self, answer=b"jpeg bytes"):
        """``opinion_pdf.image_source`` with no shard: one answer."""
        asked = []

        @contextmanager
        def source(opinion, run, page_rect, images):
            def image_for(page_index, rect):
                asked.append((page_index, page_rect(page_index), rect))
                return answer

            yield image_for

        with patch("scanning.opinion_pdf.image_source", side_effect=source):
            yield asked

    def ready(self):
        """Run the ensemble, and open the text review."""
        self.run_ensemble()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.opinion.refresh_from_db()


class TestTheLedger(FigureTestCase):
    def test_the_ensemble_stamps_the_digest_and_the_review_waits(self):
        self.run_ensemble()
        self.opinion.refresh_from_db()

        figure = ensemble.figures_of(self.stored())[0]
        self.assertEqual(figure["page_in_opinion"], 1)
        self.assertEqual(figure["box_pt"], to_pt(self.PICTURE))
        self.assertEqual(
            self.opinion.figure_digest, ensemble.figure_digest(self.stored())
        )
        self.assertFalse(opinion_figures.is_written(self.opinion))
        self.assertTrue(
            opinion_figures.owed().filter(pk=self.opinion.pk).exists()
        )
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=self.opinion.glue_revision
        )
        self.opinion.refresh_from_db()
        self.assertFalse(opinions.text_review_ready(self.opinion))

    def test_a_text_with_no_picture_owes_no_cut(self):
        self.objects[self.apply_run.ocr_key]["pages"][2]["cells"].pop()
        self.objects[self.apply_run.extract_key]["pages"][2]["blocks"].pop()

        self.run_ensemble()
        self.opinion.refresh_from_db()

        self.assertEqual(self.opinion.figure_digest, "")
        self.assertTrue(opinion_figures.is_written(self.opinion))
        self.assertFalse(
            opinion_figures.owed().filter(pk=self.opinion.pk).exists()
        )

    def test_the_cut_stores_every_picture_and_stamps_the_row(self):
        self.run_ensemble()
        self.opinion.refresh_from_db()

        with self.fake_source() as asked:
            self.assertEqual(opinion_figures.cut_one(self.opinion), 1)

        page_index, page_rect, rect = asked[0]
        self.assertEqual(page_index, 1)
        self.assertEqual(
            [rect.x0, rect.y0, rect.x1, rect.y1], to_pt(self.PICTURE)
        )
        self.assertEqual((page_rect.width, page_rect.height), (612.0, 792.0))
        figure = ensemble.figures_of(self.stored())[0]
        key = opinion_figures.key(self.opinion, "a1", figure)
        self.assertEqual(self.cuts, {key: b"jpeg bytes"})
        self.assertIn("/jobs/opinions/502.0/figures/a1/p2_", key)
        self.opinion.refresh_from_db()
        self.assertTrue(opinion_figures.is_written(self.opinion))
        self.assertFalse(
            opinion_figures.owed().filter(pk=self.opinion.pk).exists()
        )

    def test_a_text_that_moved_during_the_cut_stamps_nothing(self):
        self.run_ensemble()
        self.opinion.refresh_from_db()
        Opinion.objects.filter(pk=self.opinion.pk).update(figure_digest="x")
        self.opinion.refresh_from_db()

        with self.fake_source():
            self.assertEqual(opinion_figures.cut_one(self.opinion), 0)

        self.assertEqual(self.cuts, {})
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.figures_cut_digest, "")

    @override_settings(OPINION_PDF_RETRY_AFTER_SECONDS=0)
    def test_a_picture_that_does_not_render_spends_attempts_then_ends(self):
        self.run_ensemble()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
            error_message="another pass said this",
        )

        with self.fake_source(answer=None):
            for tick in range(opinion_figures.MAX_ATTEMPTS):
                self.assertEqual(opinion_figures.run_tick(), 0)
                if tick == 0:
                    # Below the cap the message is another pass's.
                    self.opinion.refresh_from_db()
                    self.assertEqual(
                        self.opinion.error_message, "another pass said this"
                    )

        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.figure_attempts, opinion_figures.MAX_ATTEMPTS
        )
        self.assertEqual(self.opinion.status, OpinionReviewStatus.ERROR)
        self.assertIn("Pictures:", self.opinion.error_message)
        self.assertFalse(opinion_figures.owed().exists())

    def test_a_bucket_fault_spends_nothing(self):
        self.run_ensemble()
        self.cuts = None

        def refuse(key, data, content_type):
            return False

        with (
            self.fake_source(),
            patch("scanning.s3_sync.upload_bytes_object", side_effect=refuse),
        ):
            self.assertEqual(opinion_figures.run_tick(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.figure_attempts, 0)
        self.assertIsNotNone(self.opinion.figures_attempted_at)
        self.assertTrue(
            opinion_figures.owed().filter(pk=self.opinion.pk).exists()
        )
        # The row waits its cooldown, so the next tick takes another.
        self.assertFalse(
            opinion_figures.due().filter(pk=self.opinion.pk).exists()
        )

    def test_a_fault_waits_the_cooldown_of_the_pdf_pass(self):
        self.run_ensemble()

        with self.fake_source(answer=None):
            opinion_figures.run_tick()
            self.assertEqual(opinion_figures.run_tick(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.figure_attempts, 1)
        self.assertFalse(
            opinion_figures.due().filter(pk=self.opinion.pk).exists()
        )
        with override_settings(OPINION_PDF_RETRY_AFTER_SECONDS=0):
            self.assertTrue(
                opinion_figures.due().filter(pk=self.opinion.pk).exists()
            )

    def test_a_page_with_no_source_is_a_fact_of_the_run(self):
        self.run_ensemble()
        self.opinion.refresh_from_db()

        with (
            self.fake_source(),
            patch("scanning.opinion_pdf._source_of", return_value=None),
            self.assertRaises(opinion_figures.FigureError),
        ):
            opinion_figures.cut_one(self.opinion)

        self.assertEqual(self.cuts, {})

    def test_the_cut_hides_a_redaction_over_part_of_the_picture(self):
        """A box too small to drop the picture (a name inside a map) is
        painted on the cut: the original under it never leaves."""
        box = to_pt(self.PICTURE)
        name = [box[0] + 10, box[1] + 10, box[0] + 40, box[1] + 20]
        self.redact(2, name, fill="black")
        self.run_ensemble()
        self.opinion.refresh_from_db()
        self.assertEqual(len(ensemble.figures_of(self.stored())), 1)
        width = round((box[2] - box[0]) * 2)
        height = round((box[3] - box[1]) * 2)

        with self.fake_source(answer=white_jpeg(width, height)):
            opinion_figures.cut_one(self.opinion)

        cut = fitz.Pixmap(next(iter(self.cuts.values())))
        self.assertLess(sum(cut.pixel(40, 30)) / 3, 60)
        self.assertGreater(sum(cut.pixel(width - 10, height - 10)) / 3, 200)

    def test_the_mirror_waits_for_the_cut(self):
        """The PDF pass frees the local tree only when the cut of the
        pictures owes nothing of the scan either."""
        self.run_ensemble()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=F("glue_revision")
        )

        with patch(
            "scanning.s3_sync.release_local_processing", return_value=True
        ) as release:
            self.assertFalse(opinion_pdf._release_if_done(self.scan))

            self.opinion.refresh_from_db()
            with self.fake_source():
                opinion_figures.cut_one(self.opinion)
            self.assertTrue(opinion_pdf._release_if_done(self.scan))

        release.assert_called_once_with(self.scan)

    def test_an_approved_row_is_never_cut(self):
        self.run_ensemble()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        self.assertFalse(opinion_figures.owed().exists())


class TestTheCard(FigureTestCase):
    def test_a_picture_one_engine_drew_is_a_warning(self):
        self.objects[self.apply_run.extract_key]["pages"][2]["blocks"].pop()

        self.run_ensemble()

        card = OpinionFinding.objects.get(
            opinion=self.opinion, check_name=OpinionCheck.FIGURE_ONE_ENGINE
        )
        self.assertEqual(card.page_in_opinion, 1)
        self.assertIn("dots_mocr alone drew 1 picture(s)", card.message)

    def test_a_picture_every_engine_drew_writes_no_card(self):
        self.run_ensemble()

        self.assertFalse(
            OpinionFinding.objects.filter(
                check_name=OpinionCheck.FIGURE_ONE_ENGINE
            ).exists()
        )


class TestTheApproval(FigureTestCase):
    def test_the_approval_embeds_the_cut(self):
        self.run_ensemble()
        document = self.stored()
        text = paragraphs.approved_document(document, {}, "curator", "now")
        figure = ensemble.figures_of(document)[0]
        key = opinion_figures.key(self.opinion, "a1", figure)

        with patch(
            "scanning.s3_sync.download_bytes_object",
            side_effect=lambda asked: {key: b"jpeg"}[asked],
        ):
            opinion_review._embed_figures(self.opinion, document, text)

        found = [p for p in text["body"] if p["kind"] == markup.FIGURE]
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0]["figure"],
            {
                "width_pt": figure["width_pt"],
                "height_pt": figure["height_pt"],
                "content_type": "image/jpeg",
                "data": base64.b64encode(b"jpeg").decode("ascii"),
            },
        )

    def test_a_missing_cut_refuses_the_approval(self):
        self.run_ensemble()
        document = self.stored()
        text = paragraphs.approved_document(document, {}, "curator", "now")
        missing = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        with (
            patch(
                "scanning.s3_sync.download_bytes_object", side_effect=missing
            ),
            self.assertRaises(opinion_review.ApprovalRefused) as refused,
        ):
            opinion_review._embed_figures(self.opinion, document, text)

        self.assertEqual(refused.exception.code, opinion_review.FIGURES)
        self.assertIn(
            opinion_review.FIGURES, views_api.APPROVE_REFUSED_MESSAGES
        )


@override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
class TestTheEndpoints(FigureTestCase):
    def setUp(self):
        super().setUp()
        self.ready()
        user = get_user_model().objects.create_user("curator", password="x")
        self.client.force_login(user)

    def url(self, name) -> str:
        return reverse(
            name, kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk}
        )

    def figure_group(self) -> dict:
        page = self.stored()["pages"][1]
        return next(g for g in page["groups"] if g["kind"] == markup.FIGURE)

    def test_only_a_move_edits_a_picture(self):
        group = self.figure_group()
        document = self.stored()
        revisions = {
            "glue_revision": document["opinion"]["glue_revision"],
            "edit_revision": document.get("edit_revision", 0),
        }
        for name, body in (
            ("edit_opinion_text", {"text": "words"}),
            ("edit_opinion_section", {"section": ensemble.FOOTNOTES}),
            ("edit_opinion_blockquote", {"quoted": True}),
        ):
            response = self.client.post(
                self.url(name),
                {
                    "page_in_opinion": 1,
                    "group_id": group["id"],
                    **body,
                    **revisions,
                },
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 409, name)
            self.assertEqual(
                response.json()["message"], views_api.EDIT_FIGURE_MESSAGE
            )

    def test_the_url_of_a_picture(self):
        group = self.figure_group()
        query = {
            "page": 2,
            "box": ",".join(str(v) for v in group["box_pt"]),
        }

        response = self.client.get(self.url("opinion_figure_url"), query)
        self.assertEqual(response.status_code, 404)

        with self.fake_source():
            opinion_figures.cut_one(self.opinion)
        with patch(
            "scanning.s3_sync.presign_get",
            side_effect=lambda key, *a, **kw: key,
        ):
            response = self.client.get(self.url("opinion_figure_url"), query)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], next(iter(self.cuts)))
        for bad in (
            {"page": "x", "box": "1,2,3,4"},
            {"page": 2, "box": "1,2"},
        ):
            self.assertEqual(
                self.client.get(
                    self.url("opinion_figure_url"), bad
                ).status_code,
                400,
            )


# ── the viewer ───────────────────────────────────────────────────────
VIEWER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static"
    / "scanning"
    / "viewer_step3.js"
)


class TestTheViewer(TestCase):
    def test_a_picture_takes_a_move_alone(self):
        """The toolbar of a picture stops after the two moves and the
        Undo of the order: the three other edits refuse it."""
        source = VIEWER.read_text()
        bar = source[source.index("function fillBar(") :]
        bar = bar[: bar.index("\n    }\n")]
        stop = bar.index("if (figure) { return; }")
        self.assertLess(bar.index("'Move down'"), stop)
        self.assertLess(bar.index("'Undo the order'"), stop)
        self.assertGreater(bar.index("'Put in the footnotes'"), stop)
        self.assertIn("!figure", bar[: bar.index("'Edit text'") + 200])

    def test_the_picture_is_read_from_its_endpoint(self):
        source = VIEWER.read_text()

        self.assertIn("endpoint('figureUrlEndpoint')", source)
        self.assertNotIn("figures/", source)
