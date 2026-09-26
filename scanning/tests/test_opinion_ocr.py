"""Tests for the OCR documents of one opinion (issue #350).

Seven groups:

- the cut (``opinion_ocr.build_document``): the pages, their order,
  a page nobody read;
- the frame (``opinion_ocr.page_size_pt``): one box in points from
  two engines' renders;
- the verdict (``opinion_ocr.verdict``): the three bands, the
  neighbour's mask, the maximum over the boxes;
- the document: what is copied and what is not, the manifest, the
  one reader of the verdict;
- the ledger (``opinion_ocr.is_written``): the stamp and the bump;
- the pass (``opinion_ocr.glue_due``): the cap, the order, the holds
  and the attempts;
- the route, the command and the prefix.
"""

import json
import shutil
from io import StringIO
from unittest.mock import patch

from blackletter.models import Label
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import F
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import markup, opinion_ocr, opinions, yolo
from scanning.factories import (
    ExternalJobFactory,
    OpinionBoundaryFactory,
    OpinionFactory,
    ScanFactory,
)
from scanning.models import (
    ApplyRun,
    Detection,
    ExternalJob,
    JobStage,
    JobStatus,
    Opinion,
    OpinionReviewStatus,
    Redaction,
    Scan,
    Status,
)
from scanning.tests.test_apply import MEDIA_ROOT
from scanning.tests.test_detections import model_row
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_redactions import computed
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import glued_run

#: The detections' render, 200 dpi over US letter: 612 by 792 points.
IMG_W, IMG_H = 1700, 2200
#: Points per render pixel, both axes.
SCALE = 612.0 / IMG_W

PAGES = 6


def cell(x0, y0, x1, y1, text="The court held.", category="Text") -> dict:
    """One dots.mocr cell, in render pixels."""
    return {"bbox": [x0, y0, x1, y1], "category": category, "text": text}


def block(x0, y0, x1, y1, text="The court held.", kind="text") -> dict:
    """One Mistral block, as the glue of #355 writes it."""
    return {"id": 0, "type": kind, "bbox": [x0, y0, x1, y1], "content": text}


#: The units every page carries, in the 1700 by 2200 space: a header
#: line in the left column, a body cell, and a second body cell.
HEADER = (100, 50, 800, 120)
BODY_A = (100, 300, 800, 900)
BODY_B = (100, 1000, 800, 1400)


def to_pt(box) -> list[float]:
    """A render box in points."""
    return [round(v * SCALE, 2) for v in box]


def dots_document(pages=PAGES, width=IMG_W, height=IMG_H, failed=()) -> dict:
    """A corrected volume's dots.mocr document over ``pages`` pages.

    :param pages: The page count.
    :param width: The render width of every page.
    :param height: The render height of every page.
    :param failed: Page indexes nobody read.
    :returns: The document.
    """
    factor = width / IMG_W
    entries = []
    for index in range(pages):
        entry = {
            "page_index": index,
            "pdf_page": index + 1,
            "source": {"kind": "original", "pdf_page": index + 1},
            "origin_width": width,
            "origin_height": height,
            "md": f"# page {index + 1}",
            "cells": [
                cell(*(v * factor for v in HEADER), text="878 N. C."),
                cell(*(v * factor for v in BODY_A), text=f"body A {index}"),
                cell(*(v * factor for v in BODY_B), text=f"body B {index}"),
            ],
        }
        if index in failed:
            entry["cells"] = []
            entry["error"] = "not read"
        entries.append(entry)
    return {
        "schema_version": 1,
        "engine": "dots_mocr",
        "run": 1,
        "pages": entries,
    }


#: The approved page number of every page of the fixture: the value
#: the ``HEADER`` cell carries. A fixture, not a sequence.
PRINTED = "878"


def printed_document(pages=PAGES, value=PRINTED) -> dict:
    """A corrected volume's printed-page map over ``pages`` pages.

    The document ``apply.printed_pages`` writes, with ``value`` on
    every page. ``None`` gives a map whose pages carry no number.
    """
    return {
        "schema_version": 1,
        "apply_run": "a1",
        "final_page_count": pages,
        "pages": [
            {
                "final_page": index + 1,
                "printed": value,
                "type": "single" if value else None,
                "by": "model" if value else None,
                "source": {"kind": "original", "pdf_page": index + 1},
            }
            for index in range(pages)
        ],
    }


def mistral_document(pages=PAGES) -> dict:
    """A corrected volume's Mistral document over ``pages`` pages."""
    return {
        "schema_version": 2,
        "engine": "mistral_ocr",
        "run": 1,
        "render": {"width": 1700, "height": 2200, "source": "original"},
        "pages": [
            {
                "page_index": index,
                "pdf_page": index + 1,
                "source": {"kind": "original", "pdf_page": index + 1},
                "md": f"# page {index + 1}",
                "blocks": [
                    block(*HEADER, text="878 N. C."),
                    block(*BODY_A, text=f"body A {index}"),
                    block(*BODY_B, text=f"body B {index}"),
                ],
            }
            for index in range(pages)
        ],
        "failed_pages": [],
    }


def surya_block(x0, y0, x1, y1, text="The court held.", label="Text") -> dict:
    """One Surya block, as the volume glue of #368 writes it."""
    return {
        "order": 0,
        "label": label,
        "raw_label": label.lower(),
        "bbox": [x0, y0, x1, y1],
        "confidence": 0.98,
        "html": f"<p>{text}</p>",
        "text": text,
        "skipped": False,
        "error": False,
    }


def surya_document(pages=PAGES, width=IMG_W, height=IMG_H) -> dict:
    """A corrected volume's Surya document over ``pages`` pages.

    Surya measures in the page's own render, as dots.mocr does, so the
    boxes scale by ``width`` and the document carries no ``render``.
    """
    factor = width / IMG_W
    return {
        "schema_version": 1,
        "engine": "surya",
        "run": 1,
        "dpi": 200,
        "source": "original",
        "pages": [
            {
                "page_index": index,
                "pdf_page": index + 1,
                "source": {"kind": "original", "pdf_page": index + 1},
                "origin_width": width,
                "origin_height": height,
                "text": f"page {index + 1}",
                "blocks": [
                    surya_block(
                        *(v * factor for v in HEADER),
                        text="878 N. C.",
                        label="PageHeader",
                    ),
                    surya_block(
                        *(v * factor for v in BODY_A), text=f"body A {index}"
                    ),
                    surya_block(
                        *(v * factor for v in BODY_B), text=f"body B {index}"
                    ),
                ],
            }
            for index in range(pages)
        ],
        "failed_pages": [],
    }


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class OpinionOcrTestCase(TestCase):
    """A closed review 2, a corrected volume, two engine documents.

    The opinion covers final pages 1 to 3 (0-based), printed 502 to
    504. Its caption sits in the left column of page 1 at 90 points
    down, between the header line and the first body cell; its key
    icon in the right column of page 3.
    """

    def setUp(self):
        self.objects: dict[str, object] = {}
        self.uploads: dict[str, dict] = {}
        self.pulls: list[str] = []
        self.scan = ScanFactory(
            page_count=PAGES,
            status=Status.REDACTION_REVIEW_DONE,
            source_fingerprint="fp1",
        )
        self.prefix = f"processing/{self.scan.pk}/a/{self.scan.volume}/1/"
        self._patch_bucket()
        shutil.rmtree(self.scan.output_dir, ignore_errors=True)
        self.addCleanup(
            shutil.rmtree, self.scan.output_dir, ignore_errors=True
        )

        self.apply_run = self.make_run(self.scan)
        self.detect_rows = self.measure(self.scan, self.apply_run)
        for page in range(PAGES):
            self.columns(self.scan, page)
        self.boundary = self.make_boundary(self.scan, self.apply_run, 1, 3)
        self.opinion = self.make_opinion(
            self.scan, self.apply_run, self.boundary, 502
        )

    # -- the fixture ------------------------------------------------------

    def _patch_bucket(self):
        """A fake bucket: JSON in, bytes out, one prefix for every key."""

        def download_object(key, dest):
            self.pulls.append(key)
            if key not in self.objects:
                raise KeyError(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            value = self.objects[key]
            if isinstance(value, bytes):
                dest.write_bytes(value)
            else:
                dest.write_text(json.dumps(value))

        def upload_json(key, data):
            self.uploads[key] = json.loads(json.dumps(data))
            self.objects[key] = self.uploads[key]
            return True

        for target, side in (
            ("scanning.s3_sync.download_object", download_object),
            ("scanning.s3_sync.upload_json_object", upload_json),
            (
                "scanning.s3_sync.object_exists",
                lambda key: key in self.objects,
            ),
            ("scanning.s3_sync.s3_active", lambda: True),
        ):
            patcher = patch(target, side_effect=side)
            patcher.start()
            self.addCleanup(patcher.stop)
        prefix = patch(
            "scanning.s3_sync.s3_processing_prefix",
            side_effect=lambda scan: (
                f"processing/{scan.pk}/a/{scan.volume}/1/"
            ),
        )
        prefix.start()
        self.addCleanup(prefix.stop)

    def make_run(self, scan, mistral=True, surya=False, number=1) -> ApplyRun:
        """A complete apply run whose engine documents are in the bucket.

        Surya is off by default, because a volume is read with it by
        hand and most are not (#368).
        """
        run = glued_run(scan, number=number)
        prefix = f"processing/{scan.pk}/a/{scan.volume}/1/"
        run.ocr_key = f"{prefix}jobs/apply/a{number}/ocr-volume.json"
        self.objects[run.ocr_key] = dots_document()
        run.printed_pages_key = (
            f"{prefix}jobs/apply/a{number}/printed_pages.json"
        )
        self.objects[run.printed_pages_key] = printed_document()
        if mistral:
            run.extract_key = (
                f"{prefix}jobs/apply/a{number}/extract-volume.json"
            )
            self.objects[run.extract_key] = mistral_document()
        else:
            run.extract_key = ""
        if surya:
            run.surya_key = f"{prefix}jobs/apply/a{number}/surya-volume.json"
            self.objects[run.surya_key] = surya_document()
        else:
            run.surya_key = ""
        run.save(
            update_fields=[
                "ocr_key",
                "printed_pages_key",
                "extract_key",
                "surya_key",
            ]
        )
        return run

    def measure(self, scan, run) -> list[ExternalJob]:
        """A merged detection run whose redactions are measured in ``run``."""
        yolo.ensure_detect_jobs(scan, make_manifest(1, scan.page_count))
        ExternalJob.objects.filter(scan=scan, stage=JobStage.DETECT).update(
            status=JobStatus.CONSUMED
        )
        rows = yolo.live_detect_jobs(scan)
        yolo.write_apply_state(
            rows,
            {"applied_at": timezone.now().isoformat(), "apply_run": run.pk},
        )
        return rows

    def columns(self, scan, page_index, run=None):
        """The two text columns of one page, for the outside masks."""
        for x0, x1 in ((100, 800), (900, 1600)):
            model_row(
                scan,
                apply_run=run or self.apply_run,
                label="TEXT_COLUMN",
                label_id=int(Label.TEXT_COLUMN),
                page_index=page_index,
                source_page=page_index + 1,
                x0=x0,
                y0=100,
                x1=x1,
                y1=2100,
                img_width=IMG_W,
                img_height=IMG_H,
            )

    def make_boundary(self, scan, run, start, end):
        """A computed boundary over the final pages ``start`` to ``end``."""
        return OpinionBoundaryFactory(
            scan=scan,
            apply_run=run,
            start_page_index=start,
            start_source_page=start + 1,
            start_x=72.0,
            start_y=90.0,
            end_page_index=end,
            end_source_page=end + 1,
            end_x=540.0,
            end_y=700.0,
            source_fingerprint=scan.source_fingerprint,
        )

    def make_opinion(self, scan, run, boundary, first, index=0, **fields):
        """An opinion row over its boundary's pages."""
        values = {
            "scan": scan,
            "first_printed_page": first,
            "index_in_page": index,
            "last_printed_page": first
            + boundary.end_page_index
            - boundary.start_page_index,
            "page_count": boundary.end_page_index
            - boundary.start_page_index
            + 1,
            "start_page_index": boundary.start_page_index,
            "start_source_page": boundary.start_source_page,
            "end_page_index": boundary.end_page_index,
            "end_source_page": boundary.end_source_page,
            "apply_run": run,
            "boundary": boundary,
            "source_fingerprint": scan.source_fingerprint,
        }
        values.update(fields)
        return OpinionFactory(**values)

    def redact(self, page_index, box_pt, **fields):
        """A computed redaction in points, measured in the run."""
        values = {
            "page_index": page_index,
            "source_page": page_index + 1,
            "apply_run": self.apply_run,
            "x0": box_pt[0],
            "y0": box_pt[1],
            "x1": box_pt[2],
            "y1": box_pt[3],
        }
        values.update(fields)
        return computed(self.scan, **values)

    def inputs(self) -> opinion_ocr.ScanInputs:
        return opinion_ocr.load_inputs(self.scan)

    def write(self, opinion=None):
        """Write one opinion's documents, and return the dots.mocr one."""
        opinion = opinion or self.opinion
        opinion_ocr.write(opinion, self.inputs())
        return self.uploads[opinion_ocr.engine_key(opinion, "dots_mocr")]

    @staticmethod
    def unit(document, page_in_opinion, text_prefix):
        """The unit of one page whose text starts with ``text_prefix``."""
        page = document["pages"][page_in_opinion]
        return next(
            u for u in page["units"] if u["text"].startswith(text_prefix)
        )


# ── the cut ──────────────────────────────────────────────────────────
class TestTheCut(OpinionOcrTestCase):
    def test_the_document_holds_the_opinion_pages_in_order(self):
        document = self.write()

        pages = document["pages"]
        self.assertEqual([p["page_in_opinion"] for p in pages], [0, 1, 2])
        self.assertEqual([p["page_index"] for p in pages], [1, 2, 3])
        self.assertEqual([p["pdf_page"] for p in pages], [2, 3, 4])
        self.assertEqual(
            pages[0]["source"], {"kind": "original", "pdf_page": 2}
        )
        self.assertEqual(document["opinion"]["first_printed_page"], 502)
        self.assertEqual(document["opinion"]["page_count"], 3)
        self.assertEqual(document["apply_run"], "a1")
        self.assertEqual(document["source"]["key"], self.apply_run.ocr_key)

    def test_a_page_nobody_read_keeps_its_slot(self):
        self.objects[self.apply_run.ocr_key] = dots_document(failed=(2,))

        document = self.write()

        page = document["pages"][1]
        self.assertEqual(page["page_index"], 2)
        self.assertEqual(page["error"], "not read")
        self.assertEqual(page["units"], [])
        self.assertEqual(document["failed_pages"], [2])
        self.assertEqual(len(document["pages"][0]["units"]), 3)

    def test_a_page_the_document_lacks_fails_the_row(self):
        self.objects[self.apply_run.ocr_key] = dots_document(pages=3)

        with self.assertRaises(opinion_ocr.OpinionGlueError) as caught:
            opinion_ocr.write(self.opinion, self.inputs())

        self.assertIn("no page 4", str(caught.exception))
        self.opinion.refresh_from_db()
        self.assertFalse(opinion_ocr.is_written(self.opinion))


# ── the frame ────────────────────────────────────────────────────────
class TestTheFrame(OpinionOcrTestCase):
    def test_two_renders_give_one_box_in_points(self):
        """A dots.mocr page rendered at half the size and a Mistral block
        in the 1700 by 2200 space name the same ink."""
        self.objects[self.apply_run.ocr_key] = dots_document(
            width=850, height=1100
        )

        opinion_ocr.write(self.opinion, self.inputs())

        dots = self.uploads[opinion_ocr.engine_key(self.opinion, "dots_mocr")]
        mistral = self.uploads[
            opinion_ocr.engine_key(self.opinion, "mistral_ocr")
        ]
        a_dots = self.unit(dots, 1, "body A")
        a_mistral = self.unit(mistral, 1, "body A")
        self.assertEqual(a_dots["bbox"], [50, 150, 400, 450])
        self.assertEqual(a_mistral["bbox"], [100, 300, 800, 900])
        for got, want in zip(a_dots["box_pt"], to_pt(BODY_A)):
            self.assertAlmostEqual(got, want, delta=0.5)
        self.assertEqual(a_dots["box_pt"], a_mistral["box_pt"])
        self.assertEqual(
            dots["pages"][0]["frame"],
            {
                "width_pt": 612.0,
                "height_pt": 792.0,
                "render_width": 850.0,
                "render_height": 1100.0,
            },
        )
        self.assertEqual(mistral["pages"][0]["frame"]["render_width"], 1700.0)

    def test_a_detection_of_another_space_gives_no_size(self):
        """A human row the relocation could not place keeps its old run
        and its old index; it must not size a page of this run."""
        Detection.objects.filter(scan=self.scan, page_index=4).delete()
        model_row(
            self.scan,
            apply_run=None,
            page_index=4,
            img_width=850,
            img_height=1100,
        )

        inputs = self.inputs()

        self.assertNotIn(4, inputs.renders)
        self.assertEqual(inputs.renders[1], (IMG_W, IMG_H))

    def test_a_page_with_no_detection_takes_the_dots_render(self):
        inputs = opinion_ocr.ScanInputs(run=self.apply_run)
        page = {"origin_width": 1700, "origin_height": 2200}

        size = opinion_ocr.page_size_pt(inputs, 4, page)

        self.assertAlmostEqual(size[0], 612.0)
        self.assertAlmostEqual(size[1], 792.0)

    def test_a_fallback_render_is_at_72_dpi(self):
        inputs = opinion_ocr.ScanInputs(run=self.apply_run)
        page = {
            "origin_width": 612,
            "origin_height": 792,
            "render_fallback": True,
        }

        self.assertEqual(opinion_ocr.page_size_pt(inputs, 4, page), (612, 792))

    def test_the_detections_render_comes_first(self):
        inputs = opinion_ocr.ScanInputs(
            run=self.apply_run, renders={4: (1700, 2200)}
        )
        page = {"origin_width": 999, "origin_height": 999}

        size = opinion_ocr.page_size_pt(inputs, 4, page)

        self.assertAlmostEqual(size[0], 612.0)

    def test_no_render_at_all_gives_none(self):
        inputs = opinion_ocr.ScanInputs(run=self.apply_run)

        self.assertIsNone(opinion_ocr.page_size_pt(inputs, 4, None))
        self.assertIsNone(opinion_ocr.page_size_pt(inputs, 4, {"cells": []}))


# ── the verdict ──────────────────────────────────────────────────────
class TestTheVerdict(OpinionOcrTestCase):
    def test_a_cell_under_a_redaction_is_excluded_with_the_row(self):
        row = self.redact(2, to_pt(BODY_A), rect_type="headnote", fill="black")

        document = self.write()

        unit = self.unit(document, 1, "body A")
        self.assertEqual(
            unit["exclusion"],
            {
                "reason": "redaction",
                "rect_type": "headnote",
                "redaction_id": row.pk,
                "fill": "black",
            },
        )
        self.assertGreaterEqual(unit["share"], 0.99)
        self.assertIsNone(self.unit(document, 1, "body B")["exclusion"])
        # The header above the caption on the first page is a
        # neighbour's text, and the headers of the two other pages are
        # the page number (#396).
        self.assertEqual(document["counts"]["excluded"], 4)
        self.assertEqual(document["counts"]["partial"], 0)

    def test_a_touch_under_a_tenth_is_kept(self):
        """A margin strip that overlaps a cell's top by five percent."""
        x0, y0, x1, y1 = to_pt(BODY_B)
        self.redact(
            2,
            [0.0, y0, x1, y0 + (y1 - y0) * 0.05],
            rect_type=Redaction.MARGIN_TYPE,
            fill="white",
        )

        document = self.write()

        unit = self.unit(document, 1, "body B")
        self.assertIsNone(unit["exclusion"])
        self.assertAlmostEqual(unit["share"], 0.05, places=2)

    def test_a_cell_a_box_covers_by_a_third_is_partial(self):
        x0, y0, x1, y1 = to_pt(BODY_A)
        self.redact(2, [x0, y0, x1, y0 + (y1 - y0) * 0.4])

        document = self.write()

        unit = self.unit(document, 1, "body A")
        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertAlmostEqual(unit["share"], 0.4, places=2)
        self.assertLess(unit["share"], opinion_ocr.FULL_SHARE)
        self.assertEqual(document["counts"]["partial"], 1)

    def test_the_text_above_the_caption_on_the_first_page_is_outside(self):
        document = self.write()

        first = document["pages"][0]
        header = self.unit(document, 0, "878")
        self.assertEqual(header["exclusion"], {"reason": "outside"})
        self.assertGreaterEqual(header["share"], opinion_ocr.FULL_SHARE)
        # The body below the caption belongs to the opinion.
        self.assertIsNone(self.unit(document, 0, "body B")["exclusion"])
        # A middle page has no neighbour: its header is the page number
        # (#396), and the mask of the opinion before wins on the first.
        self.assertEqual(
            self.unit(document, 1, "878")["exclusion"]["reason"],
            opinion_ocr.PAGE_NUMBER,
        )
        self.assertEqual(first["page_index"], 1)

    def test_the_header_that_carries_the_page_number_is_excluded(self):
        """The approved number of the page is in the running head, and
        every engine reads the two as one unit (#396). The verdict is
        read in each engine's own text, so the Mistral block gets it
        too, off no dots.mocr box."""
        self.write()

        for engine in ("dots_mocr", "mistral_ocr"):
            document = self.uploads[
                opinion_ocr.engine_key(self.opinion, engine)
            ]
            header = self.unit(document, 1, "878 N. C.")
            self.assertEqual(
                header["exclusion"],
                {"reason": opinion_ocr.PAGE_NUMBER, "printed": PRINTED},
                engine,
            )
            self.assertEqual(header["share"], 1.0, engine)
            self.assertNotIn(
                "878 N. C.",
                [
                    u["text"]
                    for u in opinion_ocr.kept_units(document["pages"][1])
                ],
            )
            # Two of the three pages: the first page's header is the
            # mask of the opinion before.
            self.assertEqual(document["counts"]["page_number"], 2, engine)
            self.assertEqual(document["counts"]["partial"], 0, engine)

    def low_head_cell(self, category):
        """A two-line head cell that ends below the band, on page 2."""
        dots = dots_document()
        dots["pages"][2]["cells"][0] = cell(
            100,
            50,
            800,
            300,
            text="STATE v. SMITH\nCite as 218 A.3d 877 -- 878",
            category=category,
        )
        self.objects[self.apply_run.ocr_key] = dots

    def test_a_labelled_head_cell_below_the_band_is_the_page_number(self):
        """A two-line head cell with the ``Cite as`` line can end below
        the band. Review 1 read its number through the label, and the
        glue takes the label too."""
        self.low_head_cell("Page-header")

        document = self.write()

        header = self.unit(document, 1, "STATE v. SMITH")
        self.assertEqual(
            header["exclusion"]["reason"], opinion_ocr.PAGE_NUMBER
        )

    def test_a_body_cell_below_the_band_is_judged_by_the_band(self):
        """The same box and the same text under a body label: no zone,
        so the number at the end of its line keeps it in the text."""
        self.low_head_cell("Text")

        document = self.write()

        self.assertIsNone(
            self.unit(document, 1, "STATE v. SMITH")["exclusion"]
        )

    def test_a_labelled_cell_with_a_headnote_number_is_kept(self):
        """dots.mocr labels a headnote number ``Page-header`` too, in
        the body. The value is what keeps it."""
        dots = dots_document()
        dots["pages"][2]["cells"][1] = cell(
            *BODY_A, text="1", category="Page-header"
        )
        self.objects[self.apply_run.ocr_key] = dots

        document = self.write()

        self.assertIsNone(self.unit(document, 1, "1")["exclusion"])

    def test_a_body_cell_that_ends_in_the_number_is_kept(self):
        """The band is required: a citation at the end of a paragraph
        is opinion text."""
        document = dots_document()
        document["pages"][2]["cells"][1]["text"] = "the court said, at 878"
        self.objects[self.apply_run.ocr_key] = document

        written = self.write()

        unit = self.unit(written, 1, "the court said")
        self.assertIsNone(unit["exclusion"])

    def test_a_header_with_another_number_is_kept(self):
        """The value is required: a head cell that carries a number the
        page is not approved as is not the page number. The parallel
        citation page is the daily shape of it."""
        document = dots_document()
        document["pages"][2]["cells"][0]["text"] = "877 N. C."
        self.objects[self.apply_run.ocr_key] = document

        written = self.write()

        self.assertIsNone(self.unit(written, 1, "877")["exclusion"])
        self.assertEqual(written["counts"]["page_number"], 1)

    def test_a_number_in_the_middle_of_the_head_line_is_not_the_number(self):
        document = dots_document()
        document["pages"][2]["cells"][0]["text"] = "Cite as 878 A.3d 1"
        self.objects[self.apply_run.ocr_key] = document

        written = self.write()

        self.assertIsNone(self.unit(written, 1, "Cite as")["exclusion"])

    def test_a_redaction_over_the_header_wins_over_the_page_number(self):
        row = self.redact(2, to_pt(HEADER))

        document = self.write()

        header = self.unit(document, 1, "878")
        self.assertEqual(header["exclusion"]["reason"], "redaction")
        self.assertEqual(header["exclusion"]["redaction_id"], row.pk)
        self.assertEqual(document["counts"]["page_number"], 1)

    def test_a_page_with_no_approved_number_keeps_its_header(self):
        """No number, no verdict: the curator cleared it, or the page
        never printed one. A page the map lacks reads the same."""
        printed = printed_document()
        printed["pages"][2]["printed"] = None
        printed["pages"][2]["type"] = None
        printed["pages"][2]["by"] = None
        del printed["pages"][3]
        self.objects[self.apply_run.printed_pages_key] = printed

        document = self.write()

        self.assertIsNone(self.unit(document, 1, "878")["exclusion"])
        self.assertIsNone(self.unit(document, 2, "878")["exclusion"])
        self.assertEqual(document["counts"]["page_number"], 0)

    def test_a_curators_label_the_page_does_not_print_matches_nothing(self):
        printed = printed_document()
        printed["pages"][2]["printed"] = "1234"
        printed["pages"][2]["by"] = "curator"
        self.objects[self.apply_run.printed_pages_key] = printed

        document = self.write()

        self.assertIsNone(self.unit(document, 1, "878")["exclusion"])

    def test_the_share_is_the_maximum_and_not_the_sum(self):
        x0, y0, x1, y1 = to_pt(BODY_A)
        height = y1 - y0
        self.redact(2, [x0, y0, x1, y0 + height * 0.08])
        self.redact(2, [x0, y1 - height * 0.08, x1, y1])

        document = self.write()

        unit = self.unit(document, 1, "body A")
        self.assertIsNone(unit["exclusion"])
        self.assertAlmostEqual(unit["share"], 0.08, places=2)

    def test_a_redaction_wins_over_a_mask_it_matches(self):
        row = self.redact(1, to_pt(HEADER))

        document = self.write()

        header = self.unit(document, 0, "878")
        self.assertEqual(header["exclusion"]["reason"], "redaction")
        self.assertEqual(header["exclusion"]["redaction_id"], row.pk)

    def test_a_unit_with_no_box_is_unjudged_and_not_kept(self):
        """No box means nobody measured the unit against the redaction
        over it, so it is not clean text (#350 review)."""
        document = dots_document()
        document["pages"][2]["cells"].append(
            {"bbox": None, "category": "Text", "text": "no box"}
        )
        self.objects[self.apply_run.ocr_key] = document
        self.redact(2, [0, 0, 612, 792])

        written = self.write()

        unit = self.unit(written, 1, "no box")
        self.assertIsNone(unit["box_pt"])
        self.assertEqual(unit["exclusion"], {"reason": opinion_ocr.UNJUDGED})
        self.assertEqual(unit["share"], 0.0)
        self.assertEqual(self.unit(written, 1, "body A")["share"], 1.0)
        self.assertNotIn(
            "no box",
            [u["text"] for u in opinion_ocr.kept_units(written["pages"][1])],
        )
        self.assertEqual(written["counts"]["unjudged"], 1)
        # An unjudged unit is not an excluded one: the three judged
        # units under the page-wide box, plus the header of page 0 and
        # the page number of page 2.
        self.assertEqual(written["counts"]["excluded"], 5)

    def test_a_page_with_no_size_leaves_every_unit_unjudged(self):
        """No detection in the run's space and no dots.mocr render: the
        redaction over the body cannot be measured, so nothing on the
        page reads as clean."""
        Detection.objects.filter(scan=self.scan, page_index=2).delete()
        document = dots_document()
        del document["pages"][2]["origin_width"]
        self.objects[self.apply_run.ocr_key] = document
        self.redact(2, to_pt(BODY_A))

        written = self.write()

        page = written["pages"][1]
        self.assertIsNone(page["frame"])
        self.assertEqual(
            {u["exclusion"]["reason"] for u in page["units"]},
            {opinion_ocr.UNJUDGED},
        )
        self.assertEqual(opinion_ocr.kept_units(page), [])
        self.assertEqual(written["counts"]["unjudged"], 3)
        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        self.assertEqual(
            manifest["engines"]["dots_mocr"]["counts"]["unjudged"], 3
        )
        # The other pages are judged as before: the body is kept and
        # the page number is not.
        self.assertEqual(len(opinion_ocr.kept_units(written["pages"][2])), 2)


# ── the document ─────────────────────────────────────────────────────
class TestTheDocument(OpinionOcrTestCase):
    def test_every_unit_is_present_and_the_page_md_is_not(self):
        self.redact(2, to_pt(BODY_A))

        document = self.write()

        page = document["pages"][1]
        self.assertEqual(len(page["units"]), 3)
        self.assertNotIn("md", page)
        self.assertEqual([u["id"] for u in page["units"]], [0, 1, 2])
        self.assertEqual(page["units"][0]["type"], "Text")
        self.assertEqual(document["counts"]["units"], 9)

    def test_kept_units_applies_the_verdict(self):
        self.redact(2, to_pt(BODY_A))

        document = self.write()

        kept = opinion_ocr.kept_units(document["pages"][1])
        self.assertEqual([u["text"] for u in kept], ["body B 2"])
        self.assertEqual(
            [u["text"] for u in opinion_ocr.kept_units(document["pages"][0])],
            ["body A 1", "body B 1"],
        )

    def test_the_manifest_names_both_engines_and_their_sources(self):
        opinion_ocr.write(self.opinion, self.inputs())

        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        self.assertEqual(
            sorted(manifest["engines"]), ["dots_mocr", "mistral_ocr"]
        )
        self.assertEqual(
            manifest["engines"]["mistral_ocr"]["source_key"],
            self.apply_run.extract_key,
        )
        self.assertEqual(
            manifest["engines"]["dots_mocr"]["key"],
            opinion_ocr.engine_key(self.opinion, "dots_mocr"),
        )
        self.assertEqual(manifest["glue_revision"], 0)
        self.assertEqual(manifest["opinion"]["first_printed_page"], 502)

    def test_the_manifest_is_written_last(self):
        opinion_ocr.write(self.opinion, self.inputs())

        keys = list(self.uploads)
        self.assertTrue(keys[-1].endswith("/manifest.json"))
        self.assertTrue(keys[0].endswith("/dots_mocr.json"))

    def test_a_volume_nobody_read_with_mistral_glues_one_engine(self):
        self.apply_run.extract_key = ""
        self.apply_run.save(update_fields=["extract_key"])

        engines = opinion_ocr.write(self.opinion, self.inputs())

        self.assertEqual(engines, ["dots_mocr"])
        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        self.assertEqual(list(manifest["engines"]), ["dots_mocr"])
        self.assertNotIn(
            opinion_ocr.engine_key(self.opinion, "mistral_ocr"), self.uploads
        )

    def test_the_mistral_document_reads_the_block_shape(self):
        self.redact(2, to_pt(BODY_A))
        opinion_ocr.write(self.opinion, self.inputs())

        document = self.uploads[
            opinion_ocr.engine_key(self.opinion, "mistral_ocr")
        ]
        unit = self.unit(document, 1, "body A")
        self.assertEqual(unit["type"], "text")
        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertEqual(document["source"]["key"], self.apply_run.extract_key)


# ── the parsed unit (#404) ───────────────────────────────────────────
class TestTheParsedUnit(OpinionOcrTestCase):
    """Every engine's markup becomes marks over a plain text."""

    def set_body_a(self, page_index, dots=None, mistral=None, surya=None):
        """Give ``BODY_A`` of one page a text per engine."""
        if dots is not None:
            cell = self.objects[self.apply_run.ocr_key]["pages"][page_index][
                "cells"
            ][1]
            cell.update(dots)
        if mistral is not None:
            self.objects[self.apply_run.extract_key]["pages"][page_index][
                "blocks"
            ][1].update(mistral)
        if surya is not None:
            self.apply_run.surya_key = (
                f"{self.prefix}jobs/apply/a1/surya-volume.json"
            )
            document = surya_document()
            document["pages"][page_index]["blocks"][1].update(surya)
            self.objects[self.apply_run.surya_key] = document
            self.apply_run.save(update_fields=["surya_key"])

    def document(self, engine):
        return self.uploads[opinion_ocr.engine_key(self.opinion, engine)]

    def test_a_dots_italic_is_an_em_mark(self):
        self.set_body_a(2, dots={"text": "In *Castleman*, Justice Scalia"})

        document = self.write()

        unit = document["pages"][1]["units"][1]
        self.assertEqual(unit["text"], "In Castleman, Justice Scalia")
        self.assertEqual(
            unit["marks"], [{"start": 3, "end": 12, "kind": "em"}]
        )
        self.assertEqual(unit["kind"], "paragraph")
        self.assertNotIn("table", unit)
        self.assertEqual(document["counts"]["marks"], 1)
        self.assertEqual(
            document["schema_version"], opinion_ocr.SCHEMA_VERSION
        )

    def test_the_mistral_latex_superscript(self):
        self.set_body_a(
            2, mistral={"content": "(opinion of Scalia, J.).$^{4}$"}
        )

        opinion_ocr.write(self.opinion, self.inputs())

        unit = self.document("mistral_ocr")["pages"][1]["units"][1]
        self.assertEqual(unit["text"], "(opinion of Scalia, J.).4")
        self.assertEqual(
            unit["marks"], [{"start": 24, "end": 25, "kind": "sup"}]
        )

    def test_the_surya_marks_come_from_the_html(self):
        # The worker's flattened ``text`` lost the boundary already.
        self.set_body_a(
            2,
            surya={
                "html": "<p>See <i>Holt v. Hobbs</i>, x<sup>1</sup></p>",
                "text": "See Holt v. Hobbs, x1",
            },
        )

        opinion_ocr.write(self.opinion, self.inputs())

        unit = self.document("surya")["pages"][1]["units"][1]
        self.assertEqual(unit["text"], "See Holt v. Hobbs, x1")
        self.assertEqual(
            unit["marks"],
            [
                {"start": 4, "end": 17, "kind": "em"},
                {"start": 20, "end": 21, "kind": "sup"},
            ],
        )

    def test_the_engine_s_label_names_the_kind(self):
        self.set_body_a(
            2,
            dots={"text": "FACTS", "category": "Section-header"},
            mistral={"content": "FACTS", "type": "title"},
            surya={
                "html": "<p>FACTS</p>",
                "text": "FACTS",
                "label": "SectionHeader",
            },
        )

        opinion_ocr.write(self.opinion, self.inputs())

        for engine in ("dots_mocr", "mistral_ocr", "surya"):
            unit = self.document(engine)["pages"][1]["units"][1]
            self.assertEqual(unit["kind"], "heading", engine)
            self.assertEqual(unit["text"], "FACTS", engine)
            self.assertEqual(
                self.document(engine)["counts"]["headings"], 1, engine
            )

    def test_a_heading_mark_names_the_kind_and_leaves_the_text(self):
        self.set_body_a(2, dots={"text": "## DISCUSSION"})

        document = self.write()

        unit = document["pages"][1]["units"][1]
        self.assertEqual(
            (unit["kind"], unit["text"]), ("heading", "DISCUSSION")
        )

    def test_a_table_carries_its_rows(self):
        self.set_body_a(
            2,
            dots={
                "text": "<table><tr><td>Property Damage</td><td>$35,000.00</td></tr></table>",
                "category": "Table",
            },
        )

        document = self.write()

        unit = document["pages"][1]["units"][1]
        self.assertEqual(unit["kind"], "table")
        self.assertEqual(unit["table"], [["Property Damage", "$35,000.00"]])
        self.assertEqual(unit["text"], "Property Damage $35,000.00")
        self.assertEqual(document["counts"]["tables"], 1)

    def test_the_bracket_strip_moves_the_marks(self):
        self.set_body_a(
            2,
            dots={"text": "[3]\n*Held:* so"},
            surya={
                "html": "<p>[3] <i>Held:</i> so</p>",
                "text": "[3] Held: so",
            },
        )
        self.redact(2, to_pt(BRACKET), rect_type="HEADNOTE_BRACKET")

        opinion_ocr.write(self.opinion, self.inputs())

        for engine in ("dots_mocr", "surya"):
            unit = self.document(engine)["pages"][1]["units"][1]
            self.assertEqual(unit["text"], "Held: so", engine)
            self.assertEqual(unit["removed"], ["[3]"], engine)
            self.assertEqual(
                unit["marks"], [{"start": 0, "end": 5, "kind": "em"}], engine
            )

    def test_every_engine_has_a_dialect_and_a_kind_table(self):
        """A fourth engine is one entry of ``ENGINES`` (#368, #404)."""
        for name, spec in opinion_ocr.ENGINES.items():
            with self.subTest(engine=name):
                self.assertTrue(callable(spec.dialect))
                self.assertIsInstance(spec.kind_types, dict)
                self.assertTrue(
                    set(spec.kind_types.values()) <= set(markup.BLOCK_KINDS)
                )
                parsed = spec.parse({spec.markup_key: "", spec.type_key: ""})
                self.assertEqual(parsed, markup.Parsed(text=""))
        self.assertEqual(opinion_ocr.ENGINES["surya"].markup_key, "html")


# ── the third engine ─────────────────────────────────────────────────
class TestTheSuryaEngine(OpinionOcrTestCase):
    """Surya is one entry of ``ENGINES`` and no other code (#368)."""

    def add_surya(self, document=None):
        """Give the run a Surya document, as its own glue would."""
        self.apply_run.surya_key = (
            f"{self.prefix}jobs/apply/a1/surya-volume.json"
        )
        self.objects[self.apply_run.surya_key] = (
            document if document is not None else surya_document()
        )
        self.apply_run.save(update_fields=["surya_key"])

    def surya_doc(self):
        """The opinion's Surya document, as written."""
        return self.uploads[opinion_ocr.engine_key(self.opinion, "surya")]

    def test_a_marked_header_is_the_page_number(self):
        """Mistral writes the running head as a heading, dots.mocr sets
        bold marks, Surya a bullet. The parse takes the marks and the
        bullet off the text (#404, #428), or the group drops
        on the dots.mocr cell alone and the page gets a partial card
        for the clean Mistral block beside it."""
        mistral = mistral_document()
        mistral["pages"][2]["blocks"][0]["content"] = "# 878 N. C."
        self.objects[self.apply_run.extract_key] = mistral
        dots = dots_document()
        dots["pages"][2]["cells"][0]["text"] = "**878 N. C.**"
        self.objects[self.apply_run.ocr_key] = dots
        surya = surya_document()
        surya["pages"][2]["blocks"][0]["html"] = "<p>• 878 N. C.</p>"
        surya["pages"][2]["blocks"][0]["text"] = "• 878 N. C."
        self.add_surya(surya)

        opinion_ocr.write(self.opinion, self.inputs())

        for engine, prefix in (
            ("dots_mocr", "878"),
            ("mistral_ocr", "878"),
            ("surya", "878"),
        ):
            document = self.uploads[
                opinion_ocr.engine_key(self.opinion, engine)
            ]
            header = self.unit(document, 1, prefix)
            self.assertEqual(
                header["exclusion"]["reason"], opinion_ocr.PAGE_NUMBER, engine
            )
            self.assertEqual(document["counts"]["partial"], 0, engine)

    def test_surya_is_last_in_the_table(self):
        """The order of the table is the rank of the ensemble vote
        (#365), and the entry that arrives last takes the last rank."""
        self.assertEqual(
            list(opinion_ocr.ENGINES),
            ["dots_mocr", "mistral_ocr", "surya"],
        )

    def test_a_run_read_with_three_engines_writes_three_documents(self):
        self.add_surya()

        engines = opinion_ocr.write(self.opinion, self.inputs())

        self.assertEqual(engines, ["dots_mocr", "mistral_ocr", "surya"])
        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        self.assertEqual(
            list(manifest["engines"]),
            ["dots_mocr", "mistral_ocr", "surya"],
        )
        self.assertEqual(
            manifest["engines"]["surya"]["source_key"],
            self.apply_run.surya_key,
        )

    def test_the_document_reads_the_block_shape(self):
        self.add_surya()

        opinion_ocr.write(self.opinion, self.inputs())

        document = self.surya_doc()
        self.assertEqual(document["engine"], "surya")
        self.assertEqual(len(document["pages"]), 3)
        unit = self.unit(document, 1, "body A")
        self.assertEqual(unit["type"], "Text")
        self.assertEqual(unit["text"], "body A 2")
        self.assertIsNone(unit["exclusion"])
        header = self.unit(document, 1, "878 N. C.")
        self.assertEqual(header["type"], "PageHeader")
        # The third engine reads the page number too (#396).
        self.assertEqual(
            header["exclusion"]["reason"], opinion_ocr.PAGE_NUMBER
        )
        # The markup stays in the volume document: one unit shape for
        # every engine.
        self.assertNotIn("html", unit)

    def test_a_block_under_a_redaction_is_excluded(self):
        self.add_surya()
        self.redact(2, to_pt(BODY_A))

        opinion_ocr.write(self.opinion, self.inputs())

        unit = self.unit(self.surya_doc(), 1, "body A")
        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertGreaterEqual(unit["share"], opinion_ocr.FULL_SHARE)
        self.assertEqual(
            opinion_ocr.kept_units(self.surya_doc()["pages"][1]),
            [
                u
                for u in self.surya_doc()["pages"][1]["units"]
                if u["text"].startswith("body B")
            ],
        )

    def test_the_box_comes_from_the_page_and_not_the_document(self):
        """Surya reports the render of each page, as dots.mocr does, so
        a page rendered at another size still gives the same points."""
        self.add_surya(surya_document(width=3400, height=4400))

        opinion_ocr.write(self.opinion, self.inputs())

        document = self.surya_doc()
        unit = self.unit(document, 1, "body A")
        self.assertEqual(unit["bbox"], [200, 600, 1600, 1800])
        for got, want in zip(unit["box_pt"], to_pt(BODY_A)):
            self.assertAlmostEqual(got, want, delta=0.5)
        self.assertEqual(document["pages"][0]["frame"]["render_width"], 3400.0)

    def test_a_volume_nobody_read_with_surya_glues_two_engines(self):
        engines = opinion_ocr.write(self.opinion, self.inputs())

        self.assertEqual(engines, ["dots_mocr", "mistral_ocr"])
        self.assertNotIn(
            opinion_ocr.engine_key(self.opinion, "surya"), self.uploads
        )

    def test_a_live_surya_read_holds_the_scan(self):
        """The Mistral rule, engine for engine: a read on its way holds
        the glue, so the documents are written once with every engine
        the volume was read with."""
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="surya",
            status=JobStatus.SUBMITTED,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run), ["surya"]
        )
        with self.assertLogs("scanning.opinion_ocr", level="INFO"):
            self.assertEqual(opinion_ocr.glue_due(), 0)

    def test_a_live_read_of_the_edited_pages_holds_the_scan(self):
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="surya",
            status=JobStatus.CONSUMED,
        )
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="surya",
            apply_run=self.apply_run,
            run=2,
            status=JobStatus.PENDING,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run), ["surya"]
        )

    def test_a_dead_surya_run_holds_nothing(self):
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="surya",
            status=JobStatus.FAILED,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run), []
        )
        self.assertEqual(opinion_ocr.glue_due(), 1)

    def test_a_late_read_is_a_re_glue_and_not_a_watcher(self):
        """An opinion whose glue stands does not wait for a third
        engine; the operator runs ``reglue_opinion_ocr`` (#350)."""
        opinion_ocr.write(self.opinion, self.inputs())
        self.add_surya()

        self.assertEqual(opinion_ocr.glue_due(), 0)

        opinion_ocr.reglue(self.scan)

        self.assertEqual(opinion_ocr.glue_due(), 1)
        self.assertIn(
            opinion_ocr.engine_key(
                Opinion.objects.get(pk=self.opinion.pk), "surya"
            ),
            self.uploads,
        )


# ── the ledger ───────────────────────────────────────────────────────
class TestTheLedger(OpinionOcrTestCase):
    def test_the_stamp_follows_the_write_and_the_bump(self):
        self.assertFalse(opinion_ocr.is_written(self.opinion))

        opinion_ocr.write(self.opinion, self.inputs())

        self.opinion.refresh_from_db()
        self.assertTrue(opinion_ocr.is_written(self.opinion))
        self.assertEqual(self.opinion.ocr_glue_revision, 0)
        Opinion.objects.filter(pk=self.opinion.pk).update(
            glue_revision=F("glue_revision") + 1
        )
        self.opinion.refresh_from_db()
        self.assertFalse(opinion_ocr.is_written(self.opinion))

    def test_a_bump_during_the_write_leaves_the_stamp_behind(self):
        real_upload = opinion_ocr.s3_sync.upload_json_object

        def bump_then_upload(key, data):
            Opinion.objects.filter(pk=self.opinion.pk).update(
                glue_revision=F("glue_revision") + 1
            )
            return real_upload(key, data)

        with patch(
            "scanning.s3_sync.upload_json_object", side_effect=bump_then_upload
        ):
            opinion_ocr.write(self.opinion, self.inputs())

        self.opinion.refresh_from_db()
        self.assertIsNone(self.opinion.ocr_glue_revision)
        self.assertFalse(opinion_ocr.is_written(self.opinion))

    def test_a_success_clears_this_module_s_message_alone(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(
            error_message="PDF: the cut failed"
        )

        opinion_ocr.write(self.opinion, self.inputs())

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.error_message, "PDF: the cut failed")
        Opinion.objects.filter(pk=self.opinion.pk).update(
            error_message=f"{opinion_ocr.MESSAGE_PREFIX}the boundary is gone",
            glue_revision=F("glue_revision") + 1,
        )
        self.opinion.refresh_from_db()

        opinion_ocr.write(self.opinion, self.inputs())

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.error_message, "")

    def test_the_prefix_is_the_invariant_key(self):
        self.assertEqual(self.opinion.glue_prefix, "jobs/opinions/502.0/r0/")
        self.assertEqual(
            opinion_ocr.engine_key(self.opinion, "mistral_ocr"),
            f"{self.prefix}jobs/opinions/502.0/r0/mistral_ocr.json",
        )
        self.assertEqual(
            opinion_ocr.engine_key(self.opinion, "manifest"),
            f"{self.prefix}jobs/opinions/502.0/r0/manifest.json",
        )

    def test_the_creation_raises_the_revision_of_a_matched_row(self):
        """The one line part 1 owes this module: a second approval
        moves the revision of every unapproved row (#350)."""
        Opinion.objects.filter(pk=self.opinion.pk).update(
            ocr_glue_revision=0, ocr_glue_attempts=2
        )
        approved = self.make_opinion(
            self.scan,
            self.apply_run,
            self.make_boundary(self.scan, self.apply_run, 4, 5),
            505,
            status=OpinionReviewStatus.TEXT_REVIEW_DONE,
            glue_revision=2,
            ocr_glue_revision=2,
        )
        printed = {
            "pages": [
                {
                    "final_page": i + 1,
                    "printed": str(501 + i),
                    "type": "single",
                }
                for i in range(PAGES)
            ]
        }

        opinions.create_rows(
            self.scan,
            self.apply_run,
            opinions.live_boundaries(self.scan),
            printed,
        )

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.glue_revision, 1)
        self.assertEqual(self.opinion.ocr_glue_attempts, 0)
        self.assertFalse(opinion_ocr.is_written(self.opinion))
        approved.refresh_from_db()
        self.assertEqual(approved.glue_revision, 2)
        self.assertTrue(opinion_ocr.is_written(approved))


# ── the pass ─────────────────────────────────────────────────────────
class TestThePass(OpinionOcrTestCase):
    def more_opinions(self, count):
        """``count`` one-page opinions after the fixture's, in order."""
        rows = []
        for offset in range(count):
            page = 4 + offset % 2
            boundary = self.make_boundary(
                self.scan, self.apply_run, page, page
            )
            rows.append(
                self.make_opinion(
                    self.scan, self.apply_run, boundary, 505 + offset, index=0
                )
            )
        return rows

    def test_one_tick_writes_the_cap_of_one_scan_in_reading_order(self):
        second = self.make_opinion(
            self.scan,
            self.apply_run,
            self.make_boundary(self.scan, self.apply_run, 4, 5),
            505,
        )
        third = self.make_opinion(
            self.scan,
            self.apply_run,
            self.make_boundary(self.scan, self.apply_run, 5, 5),
            505,
            index=1,
        )

        self.assertEqual(opinion_ocr.glue_due(limit=2), 2)

        for row, written in (
            (self.opinion, True),
            (second, True),
            (third, False),
        ):
            row.refresh_from_db()
            self.assertEqual(opinion_ocr.is_written(row), written, row)
        self.assertEqual(opinion_ocr.glue_due(limit=2), 1)
        third.refresh_from_db()
        self.assertTrue(opinion_ocr.is_written(third))
        self.assertEqual(opinion_ocr.glue_due(), 0)

    def test_the_documents_are_pulled_once_per_tick(self):
        self.make_opinion(
            self.scan,
            self.apply_run,
            self.make_boundary(self.scan, self.apply_run, 4, 5),
            505,
        )

        self.assertEqual(opinion_ocr.glue_due(), 2)

        self.assertEqual(
            sorted(self.pulls),
            sorted(
                [
                    self.apply_run.ocr_key,
                    self.apply_run.extract_key,
                    self.apply_run.printed_pages_key,
                ]
            ),
        )
        # The mirror serves the next tick.
        self.pulls.clear()
        Opinion.objects.filter(scan=self.scan).update(
            glue_revision=F("glue_revision") + 1
        )
        self.assertEqual(opinion_ocr.glue_due(), 2)
        self.assertEqual(self.pulls, [])

    def test_the_newest_scan_goes_first(self):
        newer = ScanFactory(
            page_count=PAGES,
            status=Status.REDACTION_REVIEW_DONE,
            source_fingerprint="fp2",
        )
        self.addCleanup(shutil.rmtree, newer.output_dir, ignore_errors=True)
        run = self.make_run(newer)
        self.measure(newer, run)
        for page in range(PAGES):
            self.columns(newer, page, run)
        row = self.make_opinion(
            newer, run, self.make_boundary(newer, run, 1, 3), 900
        )

        self.assertEqual(opinion_ocr.glue_due(), 1)

        row.refresh_from_db()
        self.opinion.refresh_from_db()
        self.assertTrue(opinion_ocr.is_written(row))
        self.assertFalse(opinion_ocr.is_written(self.opinion))

    def test_a_held_newer_scan_does_not_stop_an_older_one(self):
        """A volume that waits for its Mistral batch must not hold the
        whole corpus (#350 review)."""
        newer = ScanFactory(
            page_count=PAGES,
            status=Status.REDACTION_REVIEW_DONE,
            source_fingerprint="fp2",
        )
        self.addCleanup(shutil.rmtree, newer.output_dir, ignore_errors=True)
        run = self.make_run(newer, mistral=False)
        self.measure(newer, run)
        for page in range(PAGES):
            self.columns(newer, page, run)
        row = self.make_opinion(
            newer, run, self.make_boundary(newer, run, 1, 3), 900
        )
        ExternalJobFactory(
            scan=newer,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            status=JobStatus.SUBMITTED,
        )

        with self.assertLogs("scanning.opinion_ocr", level="INFO") as logs:
            self.assertEqual(opinion_ocr.glue_due(), 1)

        self.assertIn(
            f"scan {newer.pk}: its opinions owe", "\n".join(logs.output)
        )
        row.refresh_from_db()
        self.opinion.refresh_from_db()
        self.assertFalse(opinion_ocr.is_written(row))
        self.assertTrue(opinion_ocr.is_written(self.opinion))

    def test_a_dead_engine_run_holds_nothing(self):
        """A FAILED Mistral run brings no read; only a person restarts
        it, so it must not hold the glue for good."""
        self.apply_run.extract_key = ""
        self.apply_run.save(update_fields=["extract_key"])
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            status=JobStatus.FAILED,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run), []
        )
        self.assertEqual(opinion_ocr.glue_due(), 1)

        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        self.assertEqual(list(manifest["engines"]), ["dots_mocr"])

    def test_a_dead_read_of_the_edited_pages_holds_nothing(self):
        """The apply rows are the last step of the read, so they decide:
        a consumed volume run beside a failed apply row is a read
        nothing will finish (#350 review)."""
        self.apply_run.extract_key = ""
        self.apply_run.save(update_fields=["extract_key"])
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            status=JobStatus.CONSUMED,
        )
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            apply_run=self.apply_run,
            # The rows of the edited pages take the next run number,
            # as ``ensure_extract_apply_jobs`` gives them.
            run=2,
            status=JobStatus.FAILED,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run), []
        )
        self.assertEqual(opinion_ocr.glue_due(), 1)

    def test_a_live_read_of_the_edited_pages_holds_the_scan(self):
        self.apply_run.extract_key = ""
        self.apply_run.save(update_fields=["extract_key"])
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            status=JobStatus.CONSUMED,
        )
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            apply_run=self.apply_run,
            run=2,
            status=JobStatus.SUBMITTED,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run),
            ["mistral_ocr"],
        )
        with self.assertLogs("scanning.opinion_ocr", level="INFO"):
            self.assertEqual(opinion_ocr.glue_due(), 0)

    def test_a_consumed_volume_run_with_no_apply_row_yet_holds_the_scan(self):
        """The window before the tick creates the rows of the edited
        pages: the volume rows are the fallback."""
        self.apply_run.extract_key = ""
        self.apply_run.save(update_fields=["extract_key"])
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            status=JobStatus.CONSUMED,
        )

        self.assertEqual(
            opinion_ocr.engines_owed(self.scan, self.apply_run),
            ["mistral_ocr"],
        )

    def test_a_scan_sent_back_has_no_due_row(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.READY_FOR_REDACTION_REVIEW
        )

        self.assertEqual(opinion_ocr.glue_due(), 0)
        self.assertEqual(self.pulls, [])

    def test_an_owed_engine_holds_the_scan_and_spends_nothing(self):
        self.apply_run.extract_key = ""
        self.apply_run.save(update_fields=["extract_key"])
        # A Mistral volume run in flight: the read is on its way.
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine="mistral_ocr",
            status=JobStatus.SUBMITTED,
        )

        with self.assertLogs("scanning.opinion_ocr", level="INFO") as logs:
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.assertIn("owes a read from mistral_ocr", "\n".join(logs.output))
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ocr_glue_attempts, 0)
        self.assertEqual(self.pulls, [])

    def test_a_stale_redaction_set_holds_the_scan(self):
        yolo.write_apply_state(
            self.detect_rows,
            {"applied_at": timezone.now().isoformat(), "apply_run": None},
        )

        with self.assertLogs("scanning.opinion_ocr", level="INFO") as logs:
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.assertIn("not measured against a1", "\n".join(logs.output))
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ocr_glue_attempts, 0)

    def test_no_corrected_volume_holds_the_scan(self):
        ApplyRun.objects.filter(pk=self.apply_run.pk).update(detections_key="")

        with self.assertLogs("scanning.opinion_ocr", level="INFO"):
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ocr_glue_attempts, 0)

    def test_a_document_that_does_not_pull_holds_the_scan(self):
        del self.objects[self.apply_run.extract_key]

        with self.assertLogs("scanning.opinion_ocr", level="INFO") as logs:
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.assertIn(
            "mistral_ocr document did not load", "\n".join(logs.output)
        )

    def test_a_printed_page_map_that_does_not_pull_holds_the_scan(self):
        """The page numbers are an input of the glue (#396), and a fact
        about the scan."""
        del self.objects[self.apply_run.printed_pages_key]

        with self.assertLogs("scanning.opinion_ocr", level="INFO") as logs:
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.assertIn("printed-page map did not load", "\n".join(logs.output))
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ocr_glue_attempts, 0)

    def test_a_lost_boundary_spends_an_attempt_and_three_end_the_row(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(boundary=None)

        for attempt in (1, 2):
            with self.assertLogs("scanning.opinion_ocr", level="WARNING"):
                self.assertEqual(opinion_ocr.glue_due(), 0)
            self.opinion.refresh_from_db()
            self.assertEqual(self.opinion.ocr_glue_attempts, attempt)
            self.assertEqual(
                self.opinion.status, OpinionReviewStatus.PROCESSING
            )
            self.assertIn("boundary", self.opinion.error_message)

        with self.assertLogs("scanning.opinion_ocr", level="ERROR"):
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ocr_glue_attempts, 3)
        self.assertEqual(self.opinion.status, OpinionReviewStatus.ERROR)
        self.assertFalse(opinion_ocr.due().filter(pk=self.opinion.pk).exists())
        # The next tick has nothing to do, and pulls nothing.
        self.pulls.clear()
        self.assertEqual(opinion_ocr.glue_due(), 0)
        self.assertEqual(self.pulls, [])

    def test_a_row_of_another_run_spends_an_attempt(self):
        other = ApplyRun.objects.create(
            scan=self.scan, number=2, superseded_at=timezone.now()
        )
        Opinion.objects.filter(pk=self.opinion.pk).update(apply_run=other)

        with self.assertLogs("scanning.opinion_ocr", level="WARNING"):
            self.assertEqual(opinion_ocr.glue_due(), 0)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.ocr_glue_attempts, 1)
        self.assertIn("another run", self.opinion.error_message)

    def test_a_row_fault_does_not_stop_the_other_rows(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(boundary=None)
        second = self.make_opinion(
            self.scan,
            self.apply_run,
            self.make_boundary(self.scan, self.apply_run, 4, 5),
            505,
        )

        with self.assertLogs("scanning.opinion_ocr", level="WARNING"):
            self.assertEqual(opinion_ocr.glue_due(), 1)

        second.refresh_from_db()
        self.assertTrue(opinion_ocr.is_written(second))

    def test_nothing_runs_without_s3(self):
        with patch("scanning.s3_sync.s3_active", return_value=False):
            self.assertEqual(opinion_ocr.glue_due(), 0)

    def test_the_pass_writes_no_scan_status(self):
        opinion_ocr.glue_due()

        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.REDACTION_REVIEW_DONE)

    def test_the_collect_tick_runs_the_pass_after_the_mistral_glues(self):
        calls = []
        with (
            patch(
                "scanning.mistral_ocr.finish_ready_applies",
                side_effect=lambda: calls.append("applies") or 0,
            ),
            patch(
                "scanning.opinion_ocr.glue_due",
                side_effect=lambda: calls.append("opinions") or 0,
            ),
            patch("django.db.connections.close_all"),
        ):
            call_command("collect_external_jobs")

        self.assertEqual(calls[-2:], ["applies", "opinions"])


# ── the route ────────────────────────────────────────────────────────
class TestTheRoute(OpinionOcrTestCase, ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def url(self, engine, opinion=None, scan=None):
        opinion = opinion or self.opinion
        scan = scan or self.scan
        return reverse(
            "serve_opinion_ocr",
            kwargs={"pk": scan.pk, "opinion_pk": opinion.pk, "engine": engine},
        )

    def test_a_written_engine_redirects_to_a_presigned_get(self):
        opinion_ocr.write(self.opinion, self.inputs())
        self.opinion.refresh_from_db()

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/x"
        ) as presign:
            response = self.client.get(self.url("mistral_ocr"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://s3/x")
        key, _ttl = presign.call_args.args
        self.assertEqual(
            key, opinion_ocr.engine_key(self.opinion, "mistral_ocr")
        )
        self.assertIn(
            "opinion-502.0-r0-mistral_ocr.json",
            presign.call_args.kwargs["content_disposition"],
        )

    def test_the_manifest_is_served_too(self):
        opinion_ocr.write(self.opinion, self.inputs())

        with patch(
            "scanning.s3_sync.presign_get", return_value="https://s3/m"
        ):
            response = self.client.get(self.url("manifest"))

        self.assertEqual(response.status_code, 302)

    def test_404_before_the_write(self):
        response = self.client.get(self.url("dots_mocr"))

        self.assertEqual(response.status_code, 404)
        self.assertIn("not written at r0", response.json()["error"])

    def test_404_for_an_unknown_engine(self):
        # An engine of no entry. Surya is one since #368, so the name
        # here is an engine nobody reads with.
        response = self.client.get(self.url("lighton"))

        self.assertEqual(response.status_code, 404)
        self.assertIn("Unknown engine", response.json()["error"])

    def test_404_across_scans(self):
        other = ScanFactory()

        response = self.client.get(self.url("dots_mocr", scan=other))

        self.assertEqual(response.status_code, 404)

    def test_login_is_required(self):
        self.client.logout()

        response = self.client.get(self.url("dots_mocr"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


# ── the command ──────────────────────────────────────────────────────
class TestReglueCommand(OpinionOcrTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("reglue_opinion_ocr", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_every_unapproved_row_moves_and_its_counters_reset(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(
            ocr_glue_revision=0, ocr_glue_attempts=2
        )
        approved = self.make_opinion(
            self.scan,
            self.apply_run,
            self.make_boundary(self.scan, self.apply_run, 4, 5),
            505,
            status=OpinionReviewStatus.TEXT_REVIEW_DONE,
            glue_revision=1,
            ocr_glue_revision=1,
        )

        output = self.run_command(str(self.scan.pk))

        self.assertIn("1 opinion(s) due again", output)
        self.assertIn("Moved 1 opinion(s)", output)
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.glue_revision, 1)
        self.assertEqual(self.opinion.ocr_glue_attempts, 0)
        self.assertFalse(opinion_ocr.is_written(self.opinion))
        approved.refresh_from_db()
        self.assertEqual(approved.glue_revision, 1)
        self.assertTrue(opinion_ocr.is_written(approved))
        self.assertTrue(opinion_ocr.due().filter(pk=self.opinion.pk).exists())

    def test_a_dry_run_writes_nothing(self):
        output = self.run_command(str(self.scan.pk), "--dry-run")

        self.assertIn("would glue 1 opinion(s) again", output)
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.glue_revision, 0)

    def test_an_unknown_scan_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command("999999")

    def test_all_moves_every_volume(self):
        other = ScanFactory(
            page_count=PAGES,
            status=Status.REDACTION_REVIEW_DONE,
            source_fingerprint="fp2",
        )
        run = self.make_run(other)
        second = self.make_opinion(
            other, run, self.make_boundary(other, run, 1, 2), 700
        )

        output = self.run_command("--all")

        self.assertIn("Moved 2 opinion(s)", output)
        for row in (self.opinion, second):
            row.refresh_from_db()
            self.assertEqual(row.glue_revision, 1)

    def test_all_and_a_scan_are_refused_and_so_is_neither(self):
        for args in (("--all", str(self.scan.pk)), ()):
            with self.assertRaises(CommandError):
                self.run_command(*args)


# ── the footnote zone (#399) ─────────────────────────────────────────
#: A footnote band across both columns, in render pixels.
BAND = (100, 1600, 1600, 2100)

#: The zones of a page no detection drew on (#399, #411).
NO_ZONES = {"footnotes": [], "blockquotes": []}


def footnote_band(scan, run, page_index, box=BAND) -> Detection:
    """One ``FOOTNOTES`` detection over final page ``page_index``.

    ``run=None`` leaves the row outside the run's space, the shape of a
    human row ``detections.relocate_rows`` could not place.
    """
    return model_row(
        scan,
        apply_run=run,
        label=opinion_ocr.FOOTNOTE_LABEL,
        label_id=int(Label.FOOTNOTES),
        page_index=page_index,
        source_page=page_index + 1,
        x0=box[0],
        y0=box[1],
        x1=box[2],
        y1=box[3],
        img_width=IMG_W,
        img_height=IMG_H,
    )


class TestTheFootnoteZone(OpinionOcrTestCase):
    """The ``FOOTNOTES`` detections of the run, frozen on every page."""

    def band(self, page_index, in_run=True, box=BAND) -> Detection:
        return footnote_band(
            self.scan, self.apply_run if in_run else None, page_index, box
        )

    def test_a_band_is_a_zone_in_points_on_every_engine_page(self):
        self.band(2)

        opinion_ocr.write(self.opinion, self.inputs())

        for engine in ("dots_mocr", "mistral_ocr"):
            document = self.uploads[
                opinion_ocr.engine_key(self.opinion, engine)
            ]
            self.assertEqual(
                document["pages"][1]["zones"],
                {"footnotes": [to_pt(BAND)], "blockquotes": []},
            )
            self.assertEqual(document["pages"][0]["zones"], NO_ZONES)
        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        self.assertEqual(
            manifest["engines"]["dots_mocr"]["counts"]["footnote_zones"], 1
        )

    def test_a_band_outside_the_run_s_space_is_no_zone(self):
        """The run's space alone, the rule of ``inputs.renders``."""
        self.band(2, in_run=False)

        written = self.write()

        self.assertEqual(written["pages"][1]["zones"], NO_ZONES)

    def test_a_withdrawn_band_is_no_zone(self):
        row = self.band(2)
        row.active = False
        row.save(update_fields=["active"])

        written = self.write()

        self.assertEqual(written["pages"][1]["zones"], NO_ZONES)

    def test_a_page_with_no_size_carries_no_zone(self):
        """No size puts nothing in points. The band alone is a size, so
        the page must lose its dots.mocr render too."""
        self.band(2)
        Detection.objects.filter(scan=self.scan, page_index=2).delete()
        document = dots_document()
        del document["pages"][2]["origin_width"]
        self.objects[self.apply_run.ocr_key] = document

        written = self.write()

        self.assertIsNone(written["pages"][1]["frame"])
        self.assertEqual(written["pages"][1]["zones"], NO_ZONES)

    def test_every_engine_names_its_footnote_labels(self):
        """The spellings each engine writes, measured on the corpus
        (#399): lowercase for Mistral, CamelCase for Surya."""
        for spec in opinion_ocr.ENGINES.values():
            self.assertIsInstance(spec.footnote_types, frozenset)
        self.assertEqual(
            opinion_ocr.ENGINES["dots_mocr"].footnote_types, {"Footnote"}
        )
        self.assertEqual(
            opinion_ocr.ENGINES["mistral_ocr"].footnote_types,
            {"references", "footer", "aside_text"},
        )
        self.assertEqual(
            opinion_ocr.ENGINES["surya"].footnote_types,
            {"Footnote", "Bibliography"},
        )


# ── the blockquote zone (#411) ───────────────────────────────────────
#: A quote inside the left column, in render pixels.
QUOTE = (260, 700, 800, 900)


class TestTheBlockquoteZone(OpinionOcrTestCase):
    """The ``BLOCKQUOTE`` detections of the run, frozen on every page
    at the confidence floor or above it."""

    def quote(self, page_index, **fields) -> Detection:
        values = {
            "apply_run": self.apply_run,
            "label": opinion_ocr.BLOCKQUOTE_LABEL,
            "label_id": int(Label.BLOCKQUOTE),
            "page_index": page_index,
            "source_page": page_index + 1,
            "confidence": 0.9,
            "x0": QUOTE[0],
            "y0": QUOTE[1],
            "x1": QUOTE[2],
            "y1": QUOTE[3],
            "img_width": IMG_W,
            "img_height": IMG_H,
        }
        values.update(fields)
        return model_row(self.scan, **values)

    def test_a_quote_is_a_zone_in_points_on_every_engine_page(self):
        self.quote(2)
        footnote_band(self.scan, self.apply_run, 2)

        opinion_ocr.write(self.opinion, self.inputs())

        for engine in ("dots_mocr", "mistral_ocr"):
            document = self.uploads[
                opinion_ocr.engine_key(self.opinion, engine)
            ]
            self.assertEqual(
                document["pages"][1]["zones"],
                {"footnotes": [to_pt(BAND)], "blockquotes": [to_pt(QUOTE)]},
            )
            self.assertEqual(document["pages"][0]["zones"], NO_ZONES)
        manifest = self.uploads[
            opinion_ocr.engine_key(self.opinion, "manifest")
        ]
        counts = manifest["engines"]["dots_mocr"]["counts"]
        self.assertEqual(counts["blockquote_zones"], 1)
        self.assertEqual(counts["footnote_zones"], 1)

    def test_a_model_box_under_the_floor_is_no_zone(self):
        self.quote(2, confidence=0.79)

        written = self.write()

        self.assertEqual(written["pages"][1]["zones"], NO_ZONES)

    def test_a_model_box_at_the_floor_is_a_zone(self):
        self.quote(2, confidence=0.8)

        written = self.write()

        self.assertEqual(
            written["pages"][1]["zones"]["blockquotes"], [to_pt(QUOTE)]
        )

    @override_settings(BLOCKQUOTE_MIN_CONFIDENCE=0.5)
    def test_the_setting_moves_the_floor(self):
        self.quote(2, confidence=0.6)

        written = self.write()

        self.assertEqual(
            written["pages"][1]["zones"]["blockquotes"], [to_pt(QUOTE)]
        )

    def test_a_hand_drawn_box_is_a_zone(self):
        """A person's box has confidence 1.0, the rule of
        ``detections.add_manual``, so the floor never drops it."""
        self.quote(2, confidence=1.0, model_name=Detection.ModelName.MANUAL)

        written = self.write()

        self.assertEqual(
            written["pages"][1]["zones"]["blockquotes"], [to_pt(QUOTE)]
        )

    def test_a_withdrawn_box_is_no_zone(self):
        row = self.quote(2)
        row.active = False
        row.save(update_fields=["active"])

        written = self.write()

        self.assertEqual(written["pages"][1]["zones"], NO_ZONES)

    def test_the_floor_does_not_touch_the_footnote_zone(self):
        row = footnote_band(self.scan, self.apply_run, 2)
        row.confidence = 0.3
        row.save(update_fields=["confidence"])

        written = self.write()

        self.assertEqual(
            written["pages"][1]["zones"]["footnotes"], [to_pt(BAND)]
        )


# ── the headnote bracket (#373) ──────────────────────────────────────
#: A bracket glyph at the start of ``BODY_A``, in render pixels.
BRACKET = (105, 305, 140, 330)


class TestTheBracketToken(OpinionOcrTestCase):
    """A bracket box deletes the bracket of the unit it touches."""

    def body_a(self, page_index, text, category=None, bbox=BODY_A):
        """Give ``BODY_A`` of one page ``text`` in both engines."""
        cells = self.objects[self.apply_run.ocr_key]["pages"][page_index][
            "cells"
        ]
        cells[1]["text"] = text
        cells[1]["bbox"] = list(bbox) if bbox else None
        if category:
            cells[1]["category"] = category
        blocks = self.objects[self.apply_run.extract_key]["pages"][page_index][
            "blocks"
        ]
        blocks[1]["content"] = text
        blocks[1]["bbox"] = list(bbox) if bbox else None

    def bracket(self, page_index, box=BRACKET, rect_type="HEADNOTE_BRACKET"):
        return self.redact(page_index, to_pt(box), rect_type=rect_type)

    def mistral(self):
        return self.uploads[
            opinion_ocr.engine_key(self.opinion, "mistral_ocr")
        ]

    def test_the_bracket_leaves_the_paragraph_in_every_engine(self):
        self.body_a(2, "[1] The court held.")
        self.bracket(2)

        document = self.write()

        for doc in (document, self.mistral()):
            unit = doc["pages"][1]["units"][1]
            self.assertEqual(unit["text"], "The court held.")
            self.assertIsNone(unit["exclusion"])
            self.assertEqual(unit["removed"], ["[1]"])
        self.assertEqual(document["counts"]["brackets_removed"], 1)
        self.assertEqual(
            document["schema_version"], opinion_ocr.SCHEMA_VERSION
        )

    def test_a_large_bracket_box_no_longer_drops_the_words(self):
        """A share of 0.3 excluded the unit before #373."""
        self.body_a(2, "[14, 15] It is well settled that")
        self.bracket(2, box=(100, 300, 800, 480))

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "It is well settled that")
        self.assertIsNone(unit["exclusion"])

    def test_a_bracket_no_box_touches_stays(self):
        """The PDF shows it too; #328's card asks for the box."""
        self.body_a(2, "[1] The court held.")

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "[1] The court held.")
        self.assertNotIn("removed", unit)

    def test_a_bracket_box_elsewhere_on_the_page_leaves_it(self):
        self.body_a(2, "[1] The court held.")
        self.bracket(2, box=(105, 1005, 140, 1030))

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "[1] The court held.")

    def test_a_curator_s_box_deletes_the_bracket_too(self):
        self.body_a(2, "[1] The court held.")
        self.bracket(2, rect_type=Redaction.MANUAL_TYPE)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "The court held.")
        self.assertIsNone(unit["exclusion"])

    def test_a_curator_s_bracket_box_leaves_a_short_line_in_the_text(self):
        """The box covers an eighth of a one-line unit, which excluded
        the whole line before #419. The deleted token is the proof that
        the box is the bracket."""
        self.body_a(2, "[1] Negligence.", bbox=(100, 300, 300, 335))
        self.bracket(2, rect_type=Redaction.MANUAL_TYPE)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "Negligence.")
        self.assertEqual(unit["removed"], ["[1]"])
        self.assertIsNone(unit["exclusion"])

    def test_a_curator_s_small_box_that_deleted_nothing_excludes(self):
        """The same box over a name at the start of a short line: no
        token went, so the box is a redaction and takes the unit."""
        self.body_a(2, "Smith v. Jones.", bbox=(100, 300, 300, 335))
        self.bracket(2, rect_type=Redaction.MANUAL_TYPE)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "Smith v. Jones.")
        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertEqual(unit["exclusion"]["rect_type"], "manual")

    def test_a_name_box_beside_the_bracket_still_excludes(self):
        """Two curator boxes on one short line, one over "[1]" and one
        over the name after it. Both have the size and the place of a
        bracket; only the leftmost is the token (#419)."""
        self.body_a(2, "[1] Smith.", bbox=(100, 300, 300, 335))
        self.bracket(2, rect_type=Redaction.MANUAL_TYPE)
        self.bracket(
            2, box=(150, 305, 230, 330), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["removed"], ["[1]"])
        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertEqual(unit["exclusion"]["rect_type"], "manual")

    def test_one_box_over_the_bracket_and_a_name_still_excludes(self):
        """The box deleted the bracket, and it is wider than the token:
        it is the redaction of the name after it (#419)."""
        self.body_a(2, "[1] Smith.", bbox=(100, 300, 300, 335))
        self.bracket(
            2, box=(105, 305, 245, 330), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["removed"], ["[1]"])
        self.assertEqual(unit["exclusion"]["reason"], "redaction")

    def test_a_curator_s_box_beside_a_model_bracket_still_excludes(self):
        """The model's box is the token, so a curator's small box on the
        same line deleted nothing and stays in the verdict (#419)."""
        self.body_a(2, "[1] Smith.", bbox=(100, 300, 300, 335))
        self.bracket(2)
        self.bracket(
            2, box=(150, 305, 230, 330), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "Smith.")
        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertEqual(unit["exclusion"]["rect_type"], "manual")

    def test_a_curator_s_large_box_still_excludes(self):
        self.body_a(2, "[1] The court held.")
        self.bracket(
            2, box=(100, 300, 800, 480), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["exclusion"]["reason"], "redaction")
        self.assertEqual(unit["exclusion"]["rect_type"], "manual")
        self.assertEqual(unit["text"], "[1] The court held.")

    def test_a_curator_s_box_inside_the_line_keeps_the_bracket(self):
        """A box over a word, 108 pt from the left edge of the unit."""
        self.body_a(2, "[1] The court held.")
        self.bracket(
            2, box=(400, 305, 435, 330), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "[1] The court held.")
        self.assertIsNone(unit["exclusion"])

    def test_a_curator_s_box_wider_than_a_bracket_keeps_it(self):
        """61.2 pt wide, a tenth of nothing: no deletion, no exclusion."""
        self.body_a(2, "[1] The court held.")
        self.bracket(
            2, box=(105, 305, 275, 330), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "[1] The court held.")
        self.assertIsNone(unit["exclusion"])

    def test_a_curator_s_box_taller_than_a_bracket_keeps_it(self):
        """21.2 pt high."""
        self.body_a(2, "[1] The court held.")
        self.bracket(
            2, box=(105, 305, 140, 364), rect_type=Redaction.MANUAL_TYPE
        )

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "[1] The court held.")

    def test_the_label_of_the_unit_does_not_matter(self):
        self.body_a(2, "[2] An accused is entitled", category="List-item")
        self.bracket(2)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "An accused is entitled")

    def test_a_joined_block_loses_the_bracket_of_every_line(self):
        self.body_a(2, "[2] First.\n[3-5] Second.")
        self.bracket(2)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "First.\nSecond.")
        self.assertEqual(unit["removed"], ["[2]", "[3-5]"])

    def test_a_unit_with_no_box_keeps_its_text(self):
        """Unjudged: ``kept_units`` leaves it out already."""
        self.body_a(2, "[1] The court held.", bbox=None)
        self.bracket(2)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "[1] The court held.")
        self.assertEqual(unit["exclusion"]["reason"], opinion_ocr.UNJUDGED)

    def test_the_bracket_alone_leaves_an_empty_clean_unit(self):
        self.body_a(2, "[3]", bbox=(100, 300, 150, 330))
        self.bracket(2)

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["text"], "")
        self.assertIsNone(unit["exclusion"])

    def test_a_headnote_box_still_excludes_its_unit(self):
        self.body_a(2, "[1] The court held.")
        self.bracket(2)
        self.redact(2, to_pt(BODY_A), rect_type="headnote")

        unit = self.write()["pages"][1]["units"][1]

        self.assertEqual(unit["exclusion"]["rect_type"], "headnote")
        self.assertEqual(unit["text"], "The court held.")


class TestTheBracketBox(TestCase):
    """``opinion_ocr.is_bracket_box`` on its limits (#373)."""

    #: A unit, in points.
    UNIT = [36.0, 108.0, 288.0, 324.0]

    @staticmethod
    def rect(x0, y0, x1, y1, rect_type=Redaction.MANUAL_TYPE):
        return {"rect_type": rect_type, "x0": x0, "y0": y0, "x1": x1, "y1": y1}

    def check(self, *args, **kwargs):
        return opinion_ocr.is_bracket_box(
            self.rect(*args, **kwargs), self.UNIT
        )

    def test_a_manual_box_on_each_limit_counts(self):
        self.assertTrue(self.check(36.0, 110.0, 96.0, 130.0))
        self.assertTrue(self.check(56.0, 110.0, 70.0, 120.0))
        self.assertTrue(self.check(16.0, 110.0, 40.0, 120.0))

    def test_a_manual_box_past_a_limit_does_not(self):
        self.assertFalse(self.check(36.0, 110.0, 96.1, 120.0))
        self.assertFalse(self.check(36.0, 110.0, 50.0, 130.1))
        self.assertFalse(self.check(56.1, 110.0, 70.0, 120.0))

    def test_a_bracket_box_counts_at_any_size(self):
        self.assertTrue(
            self.check(36.0, 108.0, 288.0, 324.0, rect_type="HEADNOTE_BRACKET")
        )

    def test_a_box_that_does_not_touch_the_unit_does_not(self):
        self.assertFalse(
            self.check(36.0, 400.0, 50.0, 410.0, rect_type="HEADNOTE_BRACKET")
        )
        self.assertFalse(self.check(36.0, 400.0, 50.0, 410.0))

    def test_another_type_never_counts(self):
        self.assertFalse(
            self.check(36.0, 110.0, 50.0, 120.0, rect_type="headnote")
        )
