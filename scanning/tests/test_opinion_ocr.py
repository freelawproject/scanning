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

from scanning import opinion_ocr, opinions, yolo
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

    def make_run(self, scan, mistral=True, number=1) -> ApplyRun:
        """A complete apply run whose engine documents are in the bucket."""
        run = glued_run(scan, number=number)
        prefix = f"processing/{scan.pk}/a/{scan.volume}/1/"
        run.ocr_key = f"{prefix}jobs/apply/a{number}/ocr-volume.json"
        self.objects[run.ocr_key] = dots_document()
        if mistral:
            run.extract_key = (
                f"{prefix}jobs/apply/a{number}/extract-volume.json"
            )
            self.objects[run.extract_key] = mistral_document()
        else:
            run.extract_key = ""
        run.save(update_fields=["ocr_key", "extract_key"])
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
        # The header above the caption on the first page is the other
        # exclusion, a neighbour's text.
        self.assertEqual(document["counts"]["excluded"], 2)
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
        # A middle page has no neighbour.
        self.assertIsNone(self.unit(document, 1, "878")["exclusion"])
        self.assertEqual(first["page_index"], 1)

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
        # units under the page-wide box, plus the header of page 0.
        self.assertEqual(written["counts"]["excluded"], 4)

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
        # The other pages are judged as before.
        self.assertEqual(len(opinion_ocr.kept_units(written["pages"][2])), 3)


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
        self.assertEqual([u["text"] for u in kept], ["878 N. C.", "body B 2"])
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
            sorted([self.apply_run.ocr_key, self.apply_run.extract_key]),
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
        response = self.client.get(self.url("surya"))

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
