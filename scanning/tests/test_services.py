"""Tests for scanning.services pipeline helpers and functions.

Uses the fixture PDF at scanning/tests/fixtures/a3d.332.1.1.pdf.
"""

import json
import pathlib
import shutil
import tempfile
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase, TestCase, override_settings

from scanning.factories import (
    PageEditFactory,
    ReporterFactory,
    ScanFactory,
    UserFactory,
    VolumeFactory,
)
from scanning.models import (
    CheckName,
    Detection,
    DetectionDecision,
    Issue,
    PageEdit,
    QueueStatus,
    Redaction,
    Scan,
    Stage,
    Status,
)
from scanning.services import refresh_volume_queue_status
from scanning.tests.pdf_fixtures import (
    BOTTOM_BAR,
    COLUMN_LEFT,
    COLUMN_RIGHT,
    CONTENT,
    PAGE_H,
    PAGE_W,
    write_bitonal_page,
    write_text_page,
    write_two_column_page,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
PDF_PATH = FIXTURE_DIR / "a3d.332.1.1.pdf"
#: One recorded bl-warm detection run over :data:`PDF_PATH` (#360). The
#: tests read it instead of running the models: a run cost the suite
#: about 19 seconds and loaded torch and ultralytics, which nothing in
#: the application needs. ``meta`` in the file says how to record it
#: again after a checkpoint change.
DETECTIONS_PATH = FIXTURE_DIR / "detections.a3d.332.1.1.json"

MEDIA_ROOT = tempfile.mkdtemp()


def _require_fixture(test_case):
    """Skip test if the fixture PDF is not present."""
    if not PDF_PATH.exists():
        test_case.skipTest(
            f"Test PDF not found at {PDF_PATH}. "
            "Copy it in to scanning/tests/fixtures/"
        )


def _make_scan_with_output(tmpdir=None, **kwargs):
    """Create a scan pointing at the fixture PDF.

    Uses the scan's computed output_dir property. If tmpdir is provided
    (legacy), creates a symlink from the computed path to it.
    """
    scan = ScanFactory(
        start_page=1,
        end_page=1,
        number_of_pages=1,
        **kwargs,
    )
    # Copy fixture PDF into MEDIA_ROOT so Django's storage resolves it
    media_dir = pathlib.Path(MEDIA_ROOT) / "test_pdfs"
    media_dir.mkdir(parents=True, exist_ok=True)
    pdf_dest = media_dir / f"scan_{scan.pk}.pdf"
    shutil.copy2(PDF_PATH, pdf_dest)
    scan.original_pdf.name = str(pdf_dest.relative_to(MEDIA_ROOT))
    scan.page_count = 1
    scan.save(update_fields=["original_pdf", "page_count"])

    # Create the computed output_dir
    output = pathlib.Path(scan.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # If a tmpdir was provided, copy fixture files there too
    if tmpdir:
        _write_bitonal_copy(pathlib.Path(tmpdir) / "bitonal.pdf")
        _write_bitonal_copy(output / "bitonal.pdf")
    return scan


def _write_bitonal_copy(dest):
    """Copy the fixture as ``bitonal.pdf``, distinguishable from the original.

    The processing PDF and the original are the same fixture, so a step that
    reads the multi-GB original where it should read the small processing
    copy produces byte-identical output and no test can tell. A trailing
    comment (ignored by every PDF reader, since it sits after ``%%EOF``)
    makes "which PDF did this read" assertable.
    """
    shutil.copy2(PDF_PATH, dest)
    with open(dest, "ab") as fh:
        fh.write(b"\n% bitonal\n")


def _import_detections(scan_pk):
    """Write the recorded detections of the fixture page as rows.

    Test-local copy of the pipeline importer that left with the legacy
    detect stage (issue #173). The geometry tests need DB rows that
    describe a real book page. They read :data:`DETECTIONS_PATH` for
    them, and run no model (#360).

    The file holds the label id and the label name of every box, and
    this asks the two to agree. A live detection run kept them in step
    by itself; a stored one cannot. A blackletter release that gives
    the labels new numbers would otherwise write a row whose box is a
    column and whose label is a heading, and the geometry tests would
    measure the wrong thing and still pass. A **deleted** number
    raises in :class:`~blackletter.models.Label` already; only a
    renumbering is silent, so the names are what this compares.

    :param scan_pk: The scan the rows belong to.
    :returns: The recorded detections, as the file holds them.
    :raises AssertionError: If a recorded label and its id disagree.
    """
    from blackletter.models import Label

    dets = json.loads(DETECTIONS_PATH.read_text())["detections"]
    for entry in dets:
        name = Label(entry["label_id"]).name
        if name != entry["label"]:
            raise AssertionError(
                f"{DETECTIONS_PATH.name}: label id {entry['label_id']} "
                f"reads {name} in this blackletter and {entry['label']} "
                "in the file. Record the file again -- its `meta` holds "
                "the command."
            )
    Detection.objects.filter(scan_id=scan_pk).delete()
    Detection.objects.bulk_create(
        Detection(
            scan_id=scan_pk,
            page_index=d["page_index"],
            label=Label(d["label_id"]).name,
            label_id=d["label_id"],
            confidence=d["confidence"],
            x0=d["bbox"][0],
            y0=d["bbox"][1],
            x1=d["bbox"][2],
            y1=d["bbox"][3],
            img_width=d.get("img_width", 0),
            img_height=d.get("img_height", 0),
            model_name=d.get("found_by", [{}])[0].get("model", ""),
            model_count=d.get("model_count", 1),
            found_by=d.get("found_by", []),
        )
        for d in dets
    )
    return dets


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestUpdateProgress(TestCase):
    """Test the _update_progress helper."""

    def test_updates_message(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        _update_progress(scan.pk, "Processing page 3...")
        scan.refresh_from_db()
        self.assertEqual(scan.progress_message, "Processing page 3...")

    def test_updates_current_and_total(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        _update_progress(scan.pk, "Page 5/10", current=5, total=10)
        scan.refresh_from_db()
        self.assertEqual(scan.progress_current, 5)
        self.assertEqual(scan.progress_total, 10)

    def test_truncates_long_message(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        long_msg = "x" * 500
        _update_progress(scan.pk, long_msg)
        scan.refresh_from_db()
        self.assertEqual(len(scan.progress_message), 255)

    def test_extra_kwargs_passed_through(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        _update_progress(scan.pk, "Done", progress_log="some log text")
        scan.refresh_from_db()
        self.assertEqual(scan.progress_log, "some log text")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestModelProvenanceSurvives(TestCase):
    """Which model family found a detection, end to end (issue #196).

    The confidence gates are per family since blackletter #73
    (``label_confidence(label, bl_warm)``), so the provenance has to
    reach every reader: the file ``blackletter.api.pair`` reads, and
    the ``Document`` the redaction geometry reads. Neither needs the
    detection stack, so these run without the ``local-ml`` extra.
    """

    def setUp(self):
        _require_fixture(self)
        self.scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d")
        )

    def _detection(self, **kwargs):
        """Store one bl-warm detection.

        :param kwargs: Fields to override.
        :returns: The row.
        """
        fields = {
            "scan": self.scan,
            "page_index": 0,
            "label": "PAGE_HEADER",
            "label_id": 2,
            "confidence": 0.92,
            "x0": 10,
            "y0": 20,
            "x1": 30,
            "y1": 40,
            "img_width": 1700,
            "img_height": 2200,
            "model_name": Detection.ModelName.BL_WARM,
            "found_by": [{"model": "bl_warm", "confidence": 0.92}],
        }
        fields.update(kwargs)
        return Detection.objects.create(**fields)

    def test_the_entries_carry_found_by(self):
        from scanning.services import detection_entries

        self._detection()

        det_data = detection_entries(self.scan.pk)

        self.assertEqual(
            det_data[0]["found_by"],
            [{"model": "bl_warm", "confidence": 0.92}],
        )
        # The rows are the only store since #240: no file is written.
        self.assertFalse(
            (pathlib.Path(self.scan.output_dir) / "detections.json").exists()
        )

    def test_a_hand_added_box_claims_no_model(self):
        """It would read as a second family and send the whole volume
        back to the legacy gates."""
        from blackletter.bl_warm import rows_are_bl_warm

        from scanning.services import detection_entries

        self._detection()
        self._detection(
            label="KEY_ICON",
            label_id=1,
            confidence=1.0,
            model_name=Detection.ModelName.MANUAL,
            found_by=[],
        )

        det_data = detection_entries(self.scan.pk)

        self.assertNotIn("found_by", det_data[1])
        self.assertTrue(rows_are_bl_warm(det_data))

    def test_a_hand_added_row_with_a_model_claim_is_silenced(self):
        """Rows written before #196 name ``manual`` as their model. The
        row kind is the guard, so a re-import that keeps them cannot
        send the volume back to the legacy gates."""
        from blackletter.bl_warm import rows_are_bl_warm

        from scanning.services import detection_entries

        self._detection()
        self._detection(
            label="KEY_ICON",
            label_id=1,
            confidence=1.0,
            model_name=Detection.ModelName.MANUAL,
            found_by=[{"model": "manual", "confidence": 1.0}],
        )

        det_data = detection_entries(self.scan.pk)
        geometry = detection_entries(self.scan.pk, page_numbers={})

        self.assertNotIn("found_by", det_data[1])
        self.assertNotIn("found_by", geometry[1])
        self.assertTrue(rows_are_bl_warm(det_data))
        self.assertTrue(rows_are_bl_warm(geometry))

    def test_the_add_endpoint_writes_no_provenance(self):
        """Through the view, not the model: the view is the writer that
        used to claim a ``manual`` family."""
        from django.urls import reverse

        self._detection()
        self.client.force_login(UserFactory())

        response = self.client.post(
            reverse("add_single_detection", kwargs={"pk": self.scan.pk}),
            data=json.dumps(
                {
                    "page_index": 0,
                    "label_id": 1,
                    "bbox": [100, 100, 140, 140],
                    "img_width": 1700,
                    "img_height": 2200,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["added"])
        added = Detection.objects.get(
            scan=self.scan, model_name=Detection.ModelName.MANUAL
        )
        self.assertEqual(added.found_by, [])
        self.assertEqual(added.pk, response.json()["detection_id"])
        # Addressed by its source page (#240): no run, so the original's.
        self.assertEqual(added.source_page, 1)
        self.assertIsNone(added.source_edit)

    def test_a_second_add_on_the_curator_s_own_box_is_a_no_op(self):
        """The proximity match includes hand-drawn rows, or a repeat
        click would draw a second box over the first (PR #288 review)."""
        from django.urls import reverse

        self.client.force_login(UserFactory())
        body = {
            "page_index": 0,
            "label_id": 1,
            "bbox": [100, 100, 140, 140],
            "img_width": 1700,
            "img_height": 2200,
        }
        first = self.client.post(
            reverse("add_single_detection", kwargs={"pk": self.scan.pk}),
            data=json.dumps(body),
            content_type="application/json",
        )

        second = self.client.post(
            reverse("add_single_detection", kwargs={"pk": self.scan.pk}),
            data=json.dumps({**body, "bbox": [104, 98, 144, 138]}),
            content_type="application/json",
        )

        self.assertTrue(first.json()["added"])
        self.assertFalse(second.json()["added"])
        self.assertEqual(
            second.json()["detection_id"], first.json()["detection_id"]
        )
        self.assertEqual(
            Detection.objects.filter(
                scan=self.scan, model_name=Detection.ModelName.MANUAL
            ).count(),
            1,
        )
        self.assertEqual(DetectionDecision.objects.count(), 0)

    def test_a_malformed_add_names_no_exception(self):
        from django.urls import reverse

        self.client.force_login(UserFactory())

        with self.assertLogs("scanning.views_api", level="WARNING"):
            response = self.client.post(
                reverse("add_single_detection", kwargs={"pk": self.scan.pk}),
                data=json.dumps({"page_index": 0, "label_id": 1, "bbox": [1]}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Traceback", response.json()["error"])
        self.assertNotIn("ValueError", response.json()["error"])

    def test_the_document_reads_the_bl_warm_gates(self):
        from scanning.services import (
            _build_document_with_ids,
            detection_entries,
        )

        self._detection()
        det_data = detection_entries(self.scan.pk)

        document, _ids = _build_document_with_ids(
            self.scan, det_data, str(PDF_PATH)
        )

        self.assertTrue(document.bl_warm)

    def test_a_legacy_volume_keeps_the_legacy_gates(self):
        from scanning.services import (
            _build_document_with_ids,
            detection_entries,
        )

        self._detection(model_name=Detection.ModelName.LARGE, found_by=[])
        det_data = detection_entries(self.scan.pk)

        document, _ids = _build_document_with_ids(
            self.scan, det_data, str(PDF_PATH)
        )

        self.assertFalse(document.bl_warm)

    def test_the_geometry_lookup_carries_it_too(self):
        from scanning.services import detection_entries

        self._detection()

        dets = detection_entries(self.scan.pk, page_numbers={})

        self.assertEqual(
            dets[0]["found_by"],
            [{"model": "bl_warm", "confidence": 0.92}],
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestDetectionEntries(TestCase):
    """``detection_entries`` is the in-memory list that replaced
    ``detections.json`` (#240)."""

    def setUp(self):
        _require_fixture(self)

    def test_lists_the_live_rows_and_writes_no_file(self):
        from scanning.services import detection_entries

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir)
            _import_detections(scan.pk)
            det_path = pathlib.Path(scan.output_dir) / "detections.json"
            det_path.unlink(missing_ok=True)

            det_data = detection_entries(scan.pk)

            self.assertEqual(
                len(det_data), Detection.objects.filter(scan=scan).count()
            )
            self.assertFalse(det_path.exists())

    def test_returns_an_empty_list_without_detections(self):
        from scanning.services import detection_entries

        scan = ScanFactory()
        self.assertEqual(detection_entries(scan.pk), [])


class TestMeasureRedactionRects(TestCase):
    """``_measure_redaction_rects`` measures and writes nothing (#240 PR B)."""

    def setUp(self):
        _require_fixture(self)

    def test_returns_the_rects_in_pixels(self):
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _import_detections(scan.pk)
            document, _ids, _entries = services._snapped_document(
                scan, str(PDF_PATH)
            )

            rects = services._measure_redaction_rects(document, None)

            self.assertGreater(len(rects), 0)
            self.assertEqual(Redaction.objects.filter(scan=scan).count(), 0)

    def test_nothing_without_pages(self):
        from types import SimpleNamespace

        from scanning import services

        self.assertEqual(
            services._measure_redaction_rects(SimpleNamespace(pages=[]), None),
            [],
        )

    def test_the_snapped_document_corrects_the_columns_in_memory(self):
        """The correction is applied to the document and never written
        back: ``_snap_text_columns_to_ink`` owns persistence, and a
        reviewer's hand-drawn column must reach the geometry corrected
        while its row stays as drawn."""
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir, reporter=ReporterFactory(short_name="a3d")
            )
            pdf = pathlib.Path(scan.output_dir) / "bitonal.pdf"
            write_two_column_page(pdf, tmp_dir=pathlib.Path(tmpdir))
            det = Detection.objects.create(
                scan=scan,
                page_index=0,
                label="TEXT_COLUMN",
                label_id=16,
                confidence=0.95,
                x0=COLUMN_LEFT.x0 + 6,
                y0=COLUMN_LEFT.y0,
                x1=COLUMN_LEFT.x1 - 6,
                y1=COLUMN_LEFT.y1,
                img_width=PAGE_W,
                img_height=PAGE_H,
            )

            document, _ids, _entries = services._snapped_document(
                scan, str(pdf)
            )

            box = document.pages[0].detections[0].bbox
            self.assertAlmostEqual(box.x1, COLUMN_LEFT.x0, delta=2.0)
            self.assertAlmostEqual(box.x2, COLUMN_LEFT.x1, delta=2.0)
            det.refresh_from_db()
            self.assertEqual(det.x0, COLUMN_LEFT.x0 + 6, "persisted the snap")

    def test_the_snapped_document_separates_the_columns(self):
        """Two boxes that share an edge leave ``clamp_to_gutters`` with
        no neighbour to measure, and a headnote box then grows across
        the gutter (#308). The cells give the gutter back, after the
        ink snap and in memory: ``columns.separate_rows`` owns the
        rows."""
        from scanning import services, text_fit

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir, reporter=ReporterFactory(short_name="a3d")
            )
            pdf = pathlib.Path(scan.output_dir) / "bitonal.pdf"
            write_two_column_page(pdf, tmp_dir=pathlib.Path(tmpdir))
            edge = COLUMN_LEFT.x1
            rows = [
                self._column(scan, COLUMN_LEFT.x0, edge),
                self._column(scan, edge, COLUMN_RIGHT.x1),
            ]
            cells = text_fit.page_cells(
                {
                    "pages": [
                        {
                            "page_index": 0,
                            "origin_width": PAGE_W,
                            "origin_height": PAGE_H,
                            "cells": [
                                {
                                    "bbox": [
                                        band.x0,
                                        band.y0 + 10,
                                        band.x1,
                                        band.y1 - 10,
                                    ],
                                    "category": "Text",
                                }
                                for band in (COLUMN_LEFT, COLUMN_RIGHT)
                            ],
                        }
                    ]
                }
            )

            document, _ids, _entries = services._snapped_document(
                scan, str(pdf), None, cells
            )

            boxes = sorted(
                (d.bbox for d in document.pages[0].detections),
                key=lambda b: b.x1,
            )
            self.assertAlmostEqual(boxes[0].x2, COLUMN_LEFT.x1, delta=0.1)
            self.assertAlmostEqual(boxes[1].x1, COLUMN_RIGHT.x0, delta=0.1)
            for row in rows:
                row.refresh_from_db()
            self.assertEqual(rows[0].x1, edge, "the rows are not written here")

    @staticmethod
    def _column(scan, x0, x1):
        """Write one full-height ``TEXT_COLUMN`` row of the fixture page.

        :param scan: The scan.
        :param x0: The left edge, in pixels, which are points here.
        :param x1: The right edge, the same way.
        :returns: The row.
        """
        return Detection.objects.create(
            scan=scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.95,
            x0=x0,
            y0=COLUMN_LEFT.y0,
            x1=x1,
            y1=COLUMN_LEFT.y1,
            img_width=PAGE_W,
            img_height=PAGE_H,
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestMeasureMarginRects(TestCase):
    """``_measure_margin_rects`` measures against the document's pages."""

    def setUp(self):
        _require_fixture(self)

    @staticmethod
    def _with_column(scan):
        """Give a scan the one detection the margin bounds need."""
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.95,
            x0=100,
            y0=200,
            x1=1600,
            y1=2000,
            img_width=1700,
            img_height=2200,
        )

    def _document(self, scan):
        """Build the document the redaction compute measures (#360).

        ``_build_document_with_ids`` is what the compute calls. The
        wrapper that dropped the ids went with the file generation.

        :param scan: The scan whose rows the document holds.
        :returns: The document.
        """
        from scanning.services import (
            _build_document_with_ids,
            detection_entries,
        )

        document, _ids = _build_document_with_ids(
            scan, detection_entries(scan.pk, page_numbers={}), str(PDF_PATH)
        )
        return document

    def test_measures_the_strips_in_points(self):
        from scanning.services import _measure_margin_rects

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            self._with_column(scan)

            margins = _measure_margin_rects(
                str(PDF_PATH), self._document(scan)
            )

            self.assertTrue(margins)
            self.assertEqual(margins[0]["page_index"], 0)
            self.assertEqual(Redaction.objects.filter(scan=scan).count(), 0)
            # Every page has its four strips (#370).
            from scanning import margin_fit

            for entry in margins:
                self.assertTrue(all(margin_fit._sides(entry).values()), entry)

    def test_measures_against_clipped_copies(self):
        """The document's own column box is not what blackletter reads (#370)."""
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            self._with_column(scan)
            document = self._document(scan)
            from dataclasses import replace

            page = document.pages[0]
            column = page.detections[0]
            page.detections[0] = replace(
                column, bbox=replace(column.bbox, x1=0.5)
            )
            page.text_box = (240.0, 200.0, 1450.0, 2000.0)

            with patch.object(
                services, "compute_margin_rects", return_value=[]
            ) as measure:
                services._measure_margin_rects(str(PDF_PATH), document)

            (read,) = measure.call_args.kwargs["pages"]
            self.assertGreater(read.detections[0].bbox.x1, 200.0)
            self.assertEqual(document.pages[0].detections[0].bbox.x1, 0.5)

    def test_every_page_gets_four_strips(self):
        """A page the measure left alone gets the curator's handles (#370)."""
        from types import SimpleNamespace

        from scanning import services

        entry = {
            "page_index": 0,
            "rects": [],
            "page_width": 612.0,
            "page_height": 792.0,
        }
        with patch.object(
            services, "compute_margin_rects", return_value=[entry]
        ):
            (measured,) = services._measure_margin_rects(
                str(PDF_PATH),
                SimpleNamespace(pages=[SimpleNamespace(text_box=None)]),
            )
        self.assertEqual(len(measured["rects"]), 4)

    def test_does_not_measure_anything_without_detections(self):
        """Without detections the bounds would come from the page's marks
        alone, so bleed-through at a page edge suppresses that page's top
        strip: a worse answer than none."""
        from types import SimpleNamespace

        from scanning import services

        with patch.object(services, "compute_margin_rects") as measure:
            self.assertEqual(
                services._measure_margin_rects(
                    str(PDF_PATH), SimpleNamespace(pages=[])
                ),
                [],
            )
        measure.assert_not_called()

    def test_reads_detections_from_the_db_not_the_file(self):
        """The DB is the source of truth, and is always reachable."""
        from scanning.services import detection_entries

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            self._with_column(scan)
            # No detections.json anywhere near this scan.
            self.assertFalse(
                (pathlib.Path(scan.output_dir) / "detections.json").exists()
            )
            dets = detection_entries(scan.pk, page_numbers={})
            self.assertEqual([d["label"] for d in dets], ["TEXT_COLUMN"])
            self.assertEqual(dets[0]["bbox"], [100, 200, 1600, 2000])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestComputeRedactionsApiView(TestCase):
    """The endpoint queues the measurement (issues #196, #305).

    It used to measure inside the request. The measurement renders
    every page of the volume -- 83 seconds for 1364 pages -- so it runs
    on the daemon now, and this view writes one status. A curator
    presses it from step 2 (#305); the status rules of
    ``REDACTION_COMPUTE_STATUSES`` are what refuse.
    """

    def setUp(self):
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            uploaded_by=self.user,
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
        )

    def _detection(self):
        return Detection.objects.create(
            scan=self.scan,
            page_index=0,
            label="PAGE_HEADER",
            label_id=2,
            confidence=0.9,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
        )

    def test_an_approved_review_is_refused(self):
        """A closed review 2 is not recomputed under the person who
        closed it (#305). The way back is the admin re-queue."""
        self._detection()
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.REDACTION_REVIEW_DONE
        )

        response = self.client.post(
            f"/scans/{self.scan.pk}/compute-redactions/"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.REDACTION_REVIEW_DONE)

    def test_a_volume_with_no_detections_is_refused(self):
        response = self.client.post(
            f"/scans/{self.scan.pk}/compute-redactions/"
        )
        self.assertEqual(response.status_code, 400)
        # The refusal shape of every other endpoint of step 2 (#305).
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("message", response.json())

    def test_a_volume_with_detections_is_queued(self):
        self._detection()

        response = self.client.post(
            f"/scans/{self.scan.pk}/compute-redactions/"
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.QUEUED)

    def test_the_request_opens_no_pdf(self):
        """It needs no local copy: the daemon pulls what it measures."""
        self._detection()

        with patch("fitz.open") as opened:
            self.client.post(f"/scans/{self.scan.pk}/compute-redactions/")

        opened.assert_not_called()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestServeOpinions(TestCase):
    """Test that serve_opinions reads from DB, not disk."""

    def test_returns_opinions_from_db(self):
        from scanning.factories import OpinionBoundaryFactory

        user = UserFactory()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user, page_count=2)
        row = OpinionBoundaryFactory(
            scan=scan, start_page_index=0, end_page_index=0
        )
        response = self.client.get(f"/scans/{scan.pk}/opinions-json/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["caption_page"], 0)
        self.assertEqual(data[0]["id"], row.pk)

    def test_returns_empty_list_without_opinions(self):
        user = UserFactory()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        response = self.client.get(f"/scans/{scan.pk}/opinions-json/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestStartDetectSkipsIfExists(TestCase):
    """Test that start_detect skips re-detection when detections exist."""

    def test_redirects_without_processing_when_detections_exist(self):
        user = UserFactory()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user, status=Status.APPROVED)
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="CASE_CAPTION",
            label_id=0,
            confidence=0.95,
            x0=10,
            y0=10,
            x1=100,
            y1=100,
        )

        response = self.client.post(f"/scans/{scan.pk}/start-detect/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("step=2", response.url)
        # Status should NOT have changed to PROCESSING
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.APPROVED)


# ---------------------------------------------------------------------------
# Large PDF path for end-to-end tests
# ---------------------------------------------------------------------------
PDF_23_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "assets"
    / "media"
    / "books"
    / "a3d"
    / "original"
    / "332_a3d_1-23_opinions.pdf"
)


class TestHandlePipelineExceptionRetryCap(TestCase):
    """Test the retry-cap logic in _handle_pipeline_exception."""

    def _make_processing_scan(self, retry_count=0):
        """Return a scan in PROCESSING status with the given retry_count."""
        scan = ScanFactory(status=Status.PROCESSING, retry_count=retry_count)
        return scan

    def test_transient_error_increments_retry_count_and_requeues(self):
        """A RunpodTransientError below the cap re-queues and increments retry_count."""
        from scanning.runpod_client import RunpodTransientError
        from scanning.services import _handle_pipeline_exception

        scan = self._make_processing_scan(retry_count=0)
        exc = RunpodTransientError("NO_GPU")

        with self.settings(RUNPOD_MAX_TRANSIENT_RETRIES=5):
            _handle_pipeline_exception(scan.pk, exc, context="test")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.retry_count, 1)
        self.assertIn("Retrying", scan.progress_message)

    def test_transient_error_at_cap_escalates_to_error(self):
        """When retry_count already equals the cap, the next failure marks ERROR."""
        from scanning.runpod_client import RunpodTransientError
        from scanning.services import _handle_pipeline_exception

        scan = self._make_processing_scan(retry_count=5)
        exc = RunpodTransientError("NO_GPU")

        with self.settings(RUNPOD_MAX_TRANSIENT_RETRIES=5):
            _handle_pipeline_exception(scan.pk, exc, context="test")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR_MAX_RETRIES)
        self.assertEqual(scan.retry_count, 6)
        self.assertIn("Max retries exceeded", scan.progress_message)


class TestHandlePipelineExceptionReleasesLocalFiles(TestCase):
    """Terminal failures free the local tree; a re-queue keeps it (#215)."""

    def _handle(self, scan, exc):
        from scanning.services import _handle_pipeline_exception

        with (
            self.settings(RUNPOD_MAX_TRANSIENT_RETRIES=5),
            patch("scanning.s3_sync.release_local_processing") as release,
        ):
            _handle_pipeline_exception(scan.pk, exc, context="test")
        return release

    def test_a_requeue_keeps_the_local_files(self):
        from scanning.runpod_client import RunpodTransientError

        scan = ScanFactory(status=Status.PROCESSING, retry_count=0)

        release = self._handle(scan, RunpodTransientError("NO_GPU"))

        release.assert_not_called()

    def test_max_retries_releases_the_local_files(self):
        from scanning.runpod_client import RunpodTransientError

        scan = ScanFactory(status=Status.PROCESSING, retry_count=5)

        release = self._handle(scan, RunpodTransientError("NO_GPU"))

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, scan.pk)

    def test_a_terminal_error_releases_the_local_files(self):
        scan = ScanFactory(status=Status.PROCESSING)

        release = self._handle(scan, ValueError("boom"))

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, scan.pk)

    def test_a_lost_guard_keeps_the_local_files(self):
        """The scan left PROCESSING first; someone else owns it now."""
        scan = ScanFactory(status=Status.CANCELLED)

        release = self._handle(scan, ValueError("boom"))

        release.assert_not_called()


def _make_scan_for_volume(volume, start=1, end=100, status=Status.UPLOADED):
    """Create a Scan attached to ``volume`` with sensible defaults."""
    return ScanFactory(
        volume_obj=volume,
        reporter=volume.reporter,
        volume=volume.volume_number,
        start_page=start,
        end_page=end,
        number_of_pages=end - start + 1,
        status=status,
    )


class TestRefreshVolumeQueueStatus(TestCase):
    """Test ``refresh_volume_queue_status`` and its derivation logic."""

    def test_no_scans_unassigned(self):
        volume = VolumeFactory()
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.NEEDS_SCANNING)

    def test_no_scans_assigned_user(self):
        user = UserFactory()
        volume = VolumeFactory(assigned_to=user)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.ASSIGNED)

    def test_partial_coverage_is_scanning(self):
        volume = VolumeFactory()
        _make_scan_for_volume(volume, start=1, end=50)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNING)

    def test_full_coverage_not_approved_is_scanned(self):
        volume = VolumeFactory()
        _make_scan_for_volume(
            volume, start=1, end=100, status=Status.PENDING_REVIEW
        )
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNED)

    def test_full_coverage_all_approved_is_complete(self):
        volume = VolumeFactory()
        _make_scan_for_volume(volume, start=1, end=100, status=Status.APPROVED)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.COMPLETE)

    def test_unavailable_status_preserved(self):
        volume = VolumeFactory(queue_status=QueueStatus.UNAVAILABLE)
        _make_scan_for_volume(volume, start=1, end=100, status=Status.APPROVED)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.UNAVAILABLE)

    def test_expected_parts_fallback_complete(self):
        """All approved + expected_parts met → COMPLETE even without page range."""
        volume = VolumeFactory(
            expected_start_page=None,
            expected_end_page=None,
            expected_parts=2,
        )
        _make_scan_for_volume(volume, start=1, end=50, status=Status.APPROVED)
        _make_scan_for_volume(
            volume, start=51, end=100, status=Status.APPROVED
        )
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.COMPLETE)

    def test_no_expectations_stays_scanning(self):
        """Volumes without expected_*_page or expected_parts can't be
        auto-completed: the helper has no way to know all work is in,
        so it leaves them at SCANNING until a curator marks otherwise.
        """
        volume = VolumeFactory(
            expected_start_page=None,
            expected_end_page=None,
        )
        _make_scan_for_volume(volume, start=1, end=100, status=Status.APPROVED)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNING)


class TestRedactionGeometryFromInk(SimpleTestCase):
    """The library contract this app now depends on for headnote rects.

    The app used to patch ``blackletter.process``'s text-bound helpers from
    here, because ``_text_bottom`` returned ``clip.y0`` on a page with no
    words, which collapsed every headnote rect and dropped it: a text-less
    ``bitonal.pdf`` produced *no* headnote rects while every other rect type
    came out unchanged. blackletter measures ink itself now (#68), keyed off
    ``Document.ocr_applied``, which ``_build_document_with_ids``
    always sets. These tests pin that contract, so a library release that
    regressed it would fail here rather than silently ship empty redactions.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    def test_bottom_comes_from_the_ink_without_a_text_layer(self):
        from blackletter.scanner import _text_bottom

        pdf = self.tmp / "bitonal.pdf"
        write_bitonal_page(pdf)
        clip = fitz.Rect(CONTENT.x0, CONTENT.y0, CONTENT.x1, PAGE_H)
        with fitz.open(str(pdf)) as doc:
            bottom = _text_bottom(doc[0], clip)
        self.assertGreater(bottom, clip.y0, "headnote rects would collapse")
        self.assertAlmostEqual(bottom, CONTENT.y1, delta=8.0)

    def test_ocr_applied_prefers_ink_over_our_own_word_boxes(self):
        """Documents are built with ``ocr_applied=True``, which selects ink."""
        from blackletter.scanner import _tighten_to_text

        pdf = self.tmp / "text.pdf"
        write_text_page(pdf, bottom_bar=True)
        rect = fitz.Rect(0, 0, PAGE_W, PAGE_H)
        with fitz.open(str(pdf)) as doc:
            tight = _tighten_to_text(doc[0], rect, ocr_applied=True)
        self.assertIsNotNone(tight)
        # The platen band at the page foot is outside the content box, so
        # ink measurement stops at the last line of text instead.
        self.assertLess(tight.y1, BOTTOM_BAR.y0)

    def test_side_bounds_are_not_narrowed_from_ink(self):
        """Shrinking a headnote rect horizontally is the unsafe direction."""
        from blackletter.scanner import _text_x_bounds

        pdf = self.tmp / "bitonal.pdf"
        write_bitonal_page(pdf)
        clip = fitz.Rect(CONTENT.x0 - 20, 200, CONTENT.x1 + 20, 400)
        with fitz.open(str(pdf)) as doc:
            left, right = _text_x_bounds(doc[0], clip)
        self.assertEqual((left, right), (clip.x0, clip.x1))


class TestSnapTextColumnsToInk(TestCase):
    """``_snap_text_columns_to_ink`` corrects the column boxes at the source.

    YOLO's ``TEXT_COLUMN`` boxes land slightly inside the printed text, and
    three consumers depend on them: headnote rects snap their x-bounds to
    these boxes, margin strips take them as the text band, and the
    outside-opinion masks white out whole columns with them. Widening the
    boxes once means none of those has to compensate.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf = pathlib.Path(self._tmp.name) / "two_col.pdf"
        write_two_column_page(self.pdf)
        self.scan = ScanFactory(reporter=ReporterFactory(short_name="a3d"))

    def _column(self, x0, x1, y0=100.0, y1=700.0):
        """A TEXT_COLUMN detection, in image pixels == PDF points."""
        return Detection.objects.create(
            scan=self.scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.97,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            img_width=PAGE_W,
            img_height=PAGE_H,
        )

    def test_widens_a_narrow_box_to_the_text(self):
        from scanning.services import _snap_text_columns_to_ink

        det = self._column(COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)
        changed = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        det.refresh_from_db()
        self.assertEqual(changed, 1)
        self.assertAlmostEqual(det.x0, COLUMN_LEFT.x0, delta=2.0)
        self.assertAlmostEqual(det.x1, COLUMN_LEFT.x1, delta=2.0)

    def test_leaves_the_vertical_bounds_alone(self):
        """Only x is consumed; y feeds nothing and must not move."""
        from scanning.services import _snap_text_columns_to_ink

        det = self._column(COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6, 150, 650)
        _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        det.refresh_from_db()
        self.assertEqual((det.y0, det.y1), (150, 650))

    def test_does_not_cross_the_gutter(self):
        """A box may not grow into its neighbour's column."""
        from scanning.services import _snap_text_columns_to_ink

        left = self._column(COLUMN_LEFT.x0, COLUMN_LEFT.x1)
        right = self._column(COLUMN_RIGHT.x0, COLUMN_RIGHT.x1)
        _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        left.refresh_from_db()
        right.refresh_from_db()
        self.assertLessEqual(
            left.x1, right.x0, f"columns overlap: {left.x1} > {right.x0}"
        )

    def test_is_idempotent(self):
        from scanning.services import _snap_text_columns_to_ink

        self._column(COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)
        self._column(COLUMN_RIGHT.x0 + 6, COLUMN_RIGHT.x1 - 6)
        first = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        second = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        self.assertEqual(first, 2)
        self.assertEqual(second, 0, "snapping moved the boxes twice")

    def test_leaves_an_inconclusive_edge_alone(self):
        """An edge that never finds the end of the ink is not moved.

        A one-column detection over a full-width table would otherwise
        slide out by the growth limit on every run, never settling.
        """
        from scanning.services import _snap_text_columns_to_ink

        # Inset far enough on both sides that growth runs to its limit.
        det = self._column(COLUMN_RIGHT.x0 + 40, COLUMN_RIGHT.x1 - 40)
        before = (det.x0, det.x1)
        changed = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        det.refresh_from_db()
        self.assertEqual(changed, 0)
        self.assertEqual((det.x0, det.x1), before)

    def test_no_columns_is_a_no_op(self):
        from scanning.services import _snap_text_columns_to_ink

        self.assertEqual(
            _snap_text_columns_to_ink(self.scan.pk, str(self.pdf)), 0
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestMarginRectsUseTheDetections(TestCase):
    """The detected pages must reach the measurement.

    They are what intersects the ink content box with the text band and the
    header row: without them one bleed-through blob in a corner drags the
    box out to the page edge and that page's top strip collapses.
    blackletter owns that behaviour and tests it; what this app has to get
    right is handing the pages over, corrected, rather than measuring bare.
    """

    def setUp(self):
        _require_fixture(self)

    def test_passes_the_detected_pages_to_the_measurement(self):
        from blackletter.models import Label

        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            Detection.objects.create(
                scan=scan,
                page_index=0,
                label="TEXT_COLUMN",
                label_id=16,
                confidence=0.95,
                x0=100,
                y0=200,
                x1=1600,
                y1=2000,
                img_width=1700,
                img_height=2200,
            )
            document, _ids = services._build_document_with_ids(
                scan,
                services.detection_entries(scan.pk, page_numbers={}),
                str(PDF_PATH),
            )
            with patch.object(
                services, "compute_margin_rects", return_value=[]
            ) as measure:
                services._measure_margin_rects(str(PDF_PATH), document)
            pages = measure.call_args.kwargs["pages"]
            self.assertTrue(pages, "measured with no detected pages")
            self.assertIn(
                Label.TEXT_COLUMN,
                [d.label for d in pages[0].detections],
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSnapIgnoresDetectionsPastTheEnd(TestCase):
    """A detection can outlive the page it was found on.

    Deleting a page shifts the rows after it down, and a stale row can end
    up pointing past the end of the PDF. Indexing that page raises, and it
    would take the whole pipeline step down with it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.pdf = self.tmp / "two_col.pdf"
        write_two_column_page(self.pdf, tmp_dir=self.tmp)
        self.scan = ScanFactory(reporter=ReporterFactory(short_name="a3d"))

    def _column(self, page_index, x0, x1):
        return Detection.objects.create(
            scan=self.scan,
            page_index=page_index,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.97,
            x0=x0,
            y0=100.0,
            x1=x1,
            y1=700.0,
            img_width=PAGE_W,
            img_height=PAGE_H,
        )

    def test_a_detection_past_the_last_page_is_skipped(self):
        from scanning.services import _snap_text_columns_to_ink

        self._column(0, COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)
        ghost = self._column(5, COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)

        self.assertEqual(
            _snap_text_columns_to_ink(self.scan.pk, str(self.pdf)), 1
        )
        ghost.refresh_from_db()
        self.assertEqual(ghost.x0, COLUMN_LEFT.x0 + 6, "moved a ghost box")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRecalculateIssues(TestCase):
    """Recheck rebuilds issues from stored data.

    It must not need a local copy of the PDF (production web pods never
    download one, SCANNING-1S), and the expected page range comes from
    the scan's start_page/end_page, the same source the validate stage
    uses, rather than from the uploaded filename.
    """

    def _make_scan(self, **kwargs):
        """Create a scan whose original PDF is absent locally, as in prod."""
        scan = ScanFactory(status=Status.PENDING_REVIEW, **kwargs)
        pathlib.Path(scan.original_pdf.path).unlink()
        return scan

    def test_no_local_pdf(self):
        """Rechecking a scan with no local PDF rebuilds issues instead of
        raising FileNotFoundError."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=3,
            page_count=3,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": None, "type": None},
                {"pdf_page": 3, "detected": "3", "type": "single"},
            ],
        )
        with self.assertRaises(FileNotFoundError):
            scan.pdf_path

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)
        self.assertEqual(scan.page_count, 3)
        self.assertEqual(
            [e["pdf_index"] for e in scan.page_map if "pdf_index" in e],
            [0, 1, 2],
        )
        self.assertTrue(
            scan.issues.filter(check_name="no_page_number").exists()
        )

    def test_recheck_keeps_the_review_1_statuses(self):
        """A recheck must not move a scan between review states (#154):
        a scan in a page-completeness review state keeps it, and only
        the legacy PENDING_REVIEW rows keep getting PENDING_REVIEW."""
        from scanning import services

        for status in (
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
        ):
            with self.subTest(status=status):
                scan = self._make_scan(
                    start_page=1,
                    end_page=2,
                    page_count=2,
                    ocr_results=[
                        {"pdf_page": 1, "detected": "1", "type": "single"},
                        {"pdf_page": 2, "detected": "2", "type": "single"},
                    ],
                )
                scan.status = status
                scan.save(update_fields=["status"])

                services.recalculate_issues(scan)

                scan.refresh_from_db()
                self.assertEqual(scan.status, status)
                self.assertTrue(scan.page_map)

    def test_missing_pages_use_scan_page_range(self):
        """Pages the volume should contain but OCR never saw are reported,
        even when they fall past the last detected number."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=5,
            page_count=3,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
                {"pdf_page": 3, "detected": "3", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [4, 5])

    def test_out_of_range_reading_flagged(self):
        """A number far outside the volume's range is flagged as a stray
        reading rather than accepted as a real page number."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=5,
            page_count=1,
            ocr_results=[
                {"pdf_page": 1, "detected": "999", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertTrue(
            scan.issues.filter(check_name="suspicious_reading").exists()
        )

    def test_no_end_page_falls_back_to_detected_numbers(self):
        """Without an end_page there is no expected range, so missing
        pages are derived from the detected numbers alone."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=None,
            number_of_pages=None,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "4", "type": "single"},
                {"pdf_page": 2, "detected": "6", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [5])

    def test_page_count_refreshed_when_pdf_available(self):
        """When the PDF is on disk, page_count is re-read from it."""
        from scanning import services

        _require_fixture(self)
        scan = _make_scan_with_output(
            status=Status.PENDING_REVIEW,
            ocr_results=[{"pdf_page": 1, "detected": "1", "type": "single"}],
        )
        scan.page_count = 99
        scan.save(update_fields=["page_count"])

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.page_count, 1)

    def test_auto_corrects_stray_ocr_reading(self):
        """A stray OCR reading that sits at a consistent offset from its
        neighbours is corrected to the number the sequence implies."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=4,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
                {"pdf_page": 3, "detected": "3", "type": "single"},
                {"pdf_page": 4, "detected": "999", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[3]["detected"], "4")
        self.assertTrue(
            scan.issues.filter(check_name="auto_corrected").exists()
        )

    def test_manual_page_number_not_auto_corrected(self):
        """A page number a curator typed is left alone even when it falls
        outside the volume's range: it is flagged, not overwritten."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=4,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
                {"pdf_page": 3, "detected": "3", "type": "single"},
                {
                    "pdf_page": 4,
                    "detected": "999",
                    "type": "single",
                    "zone": "manual",
                    "ocr": "manual",
                },
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[3]["detected"], "999")
        self.assertFalse(
            scan.issues.filter(check_name="auto_corrected").exists()
        )
        self.assertTrue(
            scan.issues.filter(check_name="suspicious_reading").exists()
        )

    def test_keeps_the_review_2_findings(self):
        """Recheck rebuilds page-number issues but leaves the findings
        of review 2 (#240 PR D), which are derived from other rows and
        have one writer of their own, in place."""
        from scanning import services
        from scanning.models import CheckName, Issue

        scan = self._make_scan(
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": None, "type": None},
            ],
        )
        Issue.objects.create(
            scan=scan,
            check_name=CheckName.UNMATCHED_KEY_ICON,
            target=Issue.Target.DETECTION,
            severity="warning",
            message="a key icon no opinion names",
        )
        stale = Issue.objects.create(
            scan=scan,
            check_name=CheckName.NO_PAGE_NUMBER,
            page_number=1,
            severity="info",
            message="stale",
        )

        services.recalculate_issues(scan)

        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.UNMATCHED_KEY_ICON
            ).exists()
        )
        self.assertFalse(Issue.objects.filter(pk=stale.pk).exists())

    def _make_range_scan(self, **range_entry):
        """Create the volume of issue #233.

        One physical page carries the book pages 913 to 925, between
        page 911 and page 926.

        :param range_entry: Fields to add to the range page's entry.
        :returns: The scan.
        """
        entry = {"pdf_page": 3, "detected": "913-925", "type": "range"}
        entry.update(range_entry)
        return self._make_scan(
            start_page=910,
            end_page=927,
            page_count=5,
            ocr_results=[
                {"pdf_page": 1, "detected": "910", "type": "single"},
                {"pdf_page": 2, "detected": "911", "type": "single"},
                entry,
                {"pdf_page": 4, "detected": "926", "type": "single"},
                {"pdf_page": 5, "detected": "927", "type": "single"},
            ],
        )

    def test_a_range_page_answers_the_large_gap(self):
        """The pages a range covers are present, not missing (#233)."""
        from scanning import services

        scan = self._make_range_scan()

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertFalse(
            scan.issues.filter(check_name=CheckName.LARGE_GAP).exists()
        )
        self.assertEqual(scan.missing_pages, [912])
        self.assertTrue(
            scan.issues.filter(check_name=CheckName.PAGE_RANGE).exists()
        )

    def test_a_machine_range_keeps_its_warning(self):
        """A reading of the model is a question for the curator."""
        from scanning import services

        scan = self._make_range_scan(zone="dots-header", ocr="913-925")

        services.recalculate_issues(scan)

        card = scan.issues.get(check_name=CheckName.PAGE_RANGE)
        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertIn("Verify", card.message)

    def test_a_curators_range_is_a_note(self):
        """The curator had the page in front of them, so the card
        records the range instead of asking about it (#233)."""
        from scanning import services

        scan = self._make_range_scan(zone="manual", ocr="manual")

        services.recalculate_issues(scan)

        card = scan.issues.get(check_name=CheckName.PAGE_RANGE)
        self.assertEqual(card.severity, Issue.Severity.INFO)
        self.assertEqual(card.page_number, 913)
        self.assertIn("913-925", card.message)
        self.assertNotIn("Verify", card.message)

    def test_a_trailing_letter_breaks_no_sequence(self):
        """The book adds 2094a and 2094b between 2094 and 2095, so the
        volume holds four pages and two numbers (#319).

        This pins ``blackletter.validate``: it skips a reading it
        cannot parse and keeps the page before and the page after as
        neighbours, which is the whole rule for this shape.
        """
        from scanning import services

        scan = self._make_scan(
            start_page=2094,
            end_page=2095,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "2094", "type": "single"},
                {"pdf_page": 2, "detected": "2094a", "type": "suffixed"},
                {"pdf_page": 3, "detected": "2094b", "type": "suffixed"},
                {"pdf_page": 4, "detected": "2095", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [])
        for check in (
            CheckName.MISSING_PAGE,
            CheckName.DUPLICATE_PAGE,
            CheckName.BACKWARD_PAGE,
            CheckName.LARGE_GAP,
            CheckName.NO_PAGE_NUMBER,
        ):
            with self.subTest(check=check):
                self.assertFalse(scan.issues.filter(check_name=check).exists())

    def _make_suffixed_scan(self, **entry):
        """Four pages around a trailing letter the model read at page
        2, unless ``entry`` says who read it."""
        return self._make_scan(
            start_page=2094,
            end_page=2095,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "2094", "type": "single"},
                {
                    "pdf_page": 2,
                    "detected": "209B",
                    "type": "suffixed",
                    **entry,
                },
                {"pdf_page": 3, "detected": "2094b", "type": "suffixed"},
                {"pdf_page": 4, "detected": "2095", "type": "single"},
            ],
        )

    def test_a_trailing_letter_the_model_read_is_a_question(self):
        """The sequence asks nothing about the page, so the card does
        (#319). It is addressed by the physical page."""
        from scanning import services

        scan = self._make_suffixed_scan(zone="dots-header")

        services.recalculate_issues(scan)

        cards = scan.issues.filter(check_name=CheckName.SUSPICIOUS_READING)
        self.assertEqual(
            sorted(cards.values_list("page_number", flat=True)), [2, 3]
        )
        card = cards.get(page_number=2)
        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertEqual(
            card.message,
            "PDF page 2 reads as '209B', a page number with a trailing "
            "letter. Verify this is expected.",
        )

    def test_a_curators_trailing_letter_raises_nothing(self):
        """A person read that page (#319)."""
        from scanning import services

        scan = self._make_suffixed_scan(zone="manual", ocr="manual")

        services.recalculate_issues(scan)

        self.assertEqual(
            list(
                scan.issues.filter(
                    check_name=CheckName.SUSPICIOUS_READING
                ).values_list("page_number", flat=True)
            ),
            [3],
        )

    def test_a_dismissed_trailing_letter_stays_dismissed(self):
        """The card is a ``suspicious_reading``, so the dismissal a
        curator already has answers it on every recompute (#214)."""
        from scanning import page_edits, services

        scan = self._make_suffixed_scan(zone="dots-header")
        services.recalculate_issues(scan)
        card = scan.issues.get(
            check_name=CheckName.SUSPICIOUS_READING, page_number=2
        )
        page_edits.supersede(
            scan,
            PageEdit.Kind.DISMISS_ISSUE,
            {
                "pdf_page": card.page_number,
                "logical_page": "",
                "value": card.check_name,
            },
            {"source_fingerprint": scan.source_fingerprint},
            UserFactory(),
        )

        services.recalculate_issues(scan)

        self.assertFalse(
            scan.issues.filter(
                check_name=CheckName.SUSPICIOUS_READING, page_number=2
            ).exists()
        )

    def test_rebuild_page_map_without_local_pdf(self):
        """rebuild_page_map (manual page edits) also runs off stored data
        and applies the scan's page range."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=4,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
            ],
        )

        services.rebuild_page_map(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [3, 4])
        self.assertFalse(scan.issues.exists())


class TestTrailingGapPlaceholder(TestCase):
    """A range missing at the end of the volume (issue #256).

    ``build_issues`` collapses a run of more than 6 missing pages into
    one ``large_gap`` card and drops its pages, so the reviewer got a
    warning and no placeholder: nothing to upload to, and no gap to ask
    a scanner for. One placeholder now stands for the whole run, and
    only when the run reaches the scan's recorded last page.
    """

    def _make_scan(self, **kwargs):
        """Create a scan whose original PDF is absent locally, as in prod.

        :param kwargs: Fields for the factory.
        :returns: The scan.
        """
        scan = ScanFactory(status=Status.PENDING_REVIEW, **kwargs)
        pathlib.Path(scan.original_pdf.path).unlink()
        return scan

    def _read(self, pages, **kwargs):
        """Create a scan that read one number per page.

        :param pages: The printed numbers, in PDF page order.
        :param kwargs: Fields for the factory.
        :returns: The scan.
        """
        return self._make_scan(
            page_count=len(pages),
            ocr_results=[
                {"pdf_page": i + 1, "detected": str(n), "type": "single"}
                for i, n in enumerate(pages)
            ],
            **kwargs,
        )

    def _placeholders(self, scan):
        """Return the missing entries of the scan's page map.

        :param scan: The scan to read.
        :returns: The ``missing`` entries, in order.
        :rtype: list[dict]
        """
        scan.refresh_from_db()
        return [e for e in scan.page_map if e.get("type") == "missing"]

    def test_a_range_missing_at_the_end_gets_one_placeholder(self):
        """Ten pages the volume stops before are one gap, so one
        placeholder, labelled with the range."""
        from scanning import services

        scan = self._read(range(1, 11), start_page=1, end_page=20)

        services.recalculate_issues(scan)

        self.assertEqual(
            self._placeholders(scan),
            [
                {
                    "type": "missing",
                    "logical_number": "11-20",
                    "missing_range": [11, 20],
                }
            ],
        )

    def test_the_card_of_the_run_says_what_to_do(self):
        """The card read "likely an OCR misread" and named no action."""
        from scanning import services

        scan = self._read(range(1, 11), start_page=1, end_page=20)

        services.recalculate_issues(scan)

        card = scan.issues.get(check_name=CheckName.LARGE_GAP)
        self.assertEqual(card.page_number, 11)
        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertIn("Pages 11\u201320 (10 pages)", card.message)
        self.assertIn("The last page number read is 10", card.message)
        self.assertIn("ask a scanner", card.message)

    def test_the_placeholder_follows_the_last_page_of_the_volume(self):
        """The gap's address is the physical page it follows, which the
        viewer's projection stamps (#214)."""
        from scanning import page_edits, services

        scan = self._read(range(1, 11), start_page=1, end_page=20)

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        entry = page_edits.project_inserts(scan, scan.page_map)[-1]
        self.assertEqual(entry["type"], "missing")
        self.assertEqual(entry["anchor_pdf_page"], 10)

    def test_a_short_run_keeps_the_placeholder_of_each_page(self):
        """Three missing pages are not collapsed, so blackletter draws
        one placeholder each and this pass adds none."""
        from scanning import services

        scan = self._read(range(1, 11), start_page=1, end_page=13)

        services.recalculate_issues(scan)

        self.assertEqual(
            [e["logical_number"] for e in self._placeholders(scan)],
            [11, 12, 13],
        )

    def test_a_gap_inside_the_volume_gets_no_placeholder(self):
        """Those pages are almost always in the book with a number
        nobody read, so the card stands alone."""
        from scanning import services

        scan = self._read(
            list(range(1, 6)) + list(range(20, 26)), start_page=1, end_page=25
        )

        services.recalculate_issues(scan)

        self.assertEqual(self._placeholders(scan), [])
        self.assertTrue(
            scan.issues.filter(check_name=CheckName.LARGE_GAP).exists()
        )

    def test_a_scan_with_no_recorded_last_page_gets_no_placeholder(self):
        """Without an end page there is no trailing gap to find (#209)."""
        from scanning import services

        scan = self._read(range(1, 11), start_page=None, end_page=None)

        services.recalculate_issues(scan)

        self.assertEqual(self._placeholders(scan), [])

    def test_a_printed_range_over_the_end_gets_no_placeholder(self):
        """A compressed page carries the pages it prints (#233), so they
        are not missing at all."""
        from scanning import services

        scan = self._read(range(1, 11), start_page=1, end_page=20)
        scan.ocr_results = scan.ocr_results + [
            {"pdf_page": 11, "detected": "11-20", "type": "range"}
        ]
        scan.page_count = 11
        scan.save(update_fields=["ocr_results", "page_count"])

        services.recalculate_issues(scan)

        self.assertEqual(self._placeholders(scan), [])

    def test_a_page_number_edit_keeps_the_placeholder(self):
        """``rebuild_page_map`` is the other builder of the page map, and
        the two must agree."""
        from scanning import services

        scan = self._read(range(1, 11), start_page=1, end_page=20)

        services.rebuild_page_map(scan)

        self.assertEqual(
            self._placeholders(scan)[0]["missing_range"], [11, 20]
        )


class TestRunComputeIssues(TestCase):
    """The apply of the glued dots.mocr output (#149/#204, #212).

    Called by ``dots_mocr.apply_ready_runs`` with the scan and the
    glued document's key; the download is patched in. The scan never
    transits QUEUED/PROCESSING, and like the recheck the apply must
    not need a local copy of the PDF.
    """

    def _make_scan(self, **kwargs):
        """Create a scan with no local PDF, parked for the apply.

        :param kwargs: ScanFactory overrides.
        :returns: The scan.
        """
        kwargs.setdefault("status", Status.AWAITING_VALIDATION)
        scan = ScanFactory(start_page=1, end_page=2, page_count=2, **kwargs)
        pathlib.Path(scan.original_pdf.path).unlink()
        return scan

    def _document(self, texts):
        """Build a glued volume document with one header cell per page.

        :param texts: One header text per page; None makes the page a
            filtered one.
        :returns: The document dict.
        """
        from scanning.tests.test_page_numbers import cell, make_page

        return {
            "pages": [
                make_page(index + 1, None if text is None else [cell(text)])
                for index, text in enumerate(texts)
            ]
        }

    def _run(self, scan, document):
        from scanning import services

        with patch(
            "scanning.s3_sync.download_json_object", return_value=document
        ) as download:
            done = services.run_compute_issues(scan, "jobs/x/r1-volume.json")
        return done, download

    def test_the_apply_reads_pages_and_readies_the_scan(self):
        scan = self._make_scan()

        done, download = self._run(scan, self._document(["1", None]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(scan.progress_message, "Done")
        self.assertEqual(
            [(r["pdf_page"], r["detected"]) for r in scan.ocr_results],
            [(1, "1"), (2, None)],
        )
        self.assertTrue(scan.page_map)
        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.NO_PAGE_NUMBER, page_number=2
            ).exists()
        )
        download.assert_called_once_with("jobs/x/r1-volume.json")

    def test_a_legacy_pending_review_scan_takes_the_edge(self):
        scan = self._make_scan(status=Status.PENDING_REVIEW)

        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_ready_scan_recomputes_without_a_status_write(self):
        scan = self._make_scan(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(scan.ocr_results[1]["detected"], "2")

    def test_a_curators_number_survives_the_apply(self):
        # The row outranks the run, and the blob is rebuilt from both
        # (#214). It used to be carried over from the previous blob by
        # a "manual" stamp on two of its fields.
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="9",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "9")
        self.assertEqual(scan.ocr_results[0]["zone"], "manual")
        self.assertEqual(scan.ocr_results[1]["detected"], "2")

    def test_a_curators_range_survives_the_apply(self):
        # One PDF page can carry several book pages, which is what
        # CheckName.PAGE_RANGE is raised for.
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="678-686",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "678-686")
        self.assertEqual(scan.ocr_results[0]["type"], "range")

    def test_a_curators_trailing_letter_survives_the_apply(self):
        # The page the book adds between two numbered pages (#319).
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="2094a",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "2094a")
        self.assertEqual(scan.ocr_results[0]["type"], "suffixed")

    def test_a_cleared_number_survives_the_apply(self):
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertIsNone(scan.ocr_results[0]["detected"])
        self.assertEqual(scan.ocr_results[0]["zone"], "manual")

    def test_an_edit_against_another_original_is_reported(self):
        # An address names a page of the original it was made on. An
        # edit from another one is dropped and said out loud, never
        # placed on whatever page now holds that number.
        scan = self._make_scan()
        Scan.objects.filter(pk=scan.pk).update(source_fingerprint="200:2")
        scan.refresh_from_db()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="9",
            source_fingerprint="100:2",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "1")
        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.STALE_PAGE_EDIT, page_number=1
            ).exists()
        )

    def test_an_edit_naming_an_absent_page_is_reported(self):
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=7,
            value="9",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(len(scan.ocr_results), 2)
        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.STALE_PAGE_EDIT, page_number=7
            ).exists()
        )

    def test_review_2_findings_are_kept(self):
        """The apply of the page numbers says nothing about the
        redactions (#240 PR D)."""
        scan = self._make_scan()
        Issue.objects.create(
            scan=scan,
            check_name=CheckName.UNCOVERED_HEADNOTE,
            target=Issue.Target.REDACTION,
            severity=Issue.Severity.WARNING,
            message="a headnote no redaction covers",
        )

        self._run(scan, self._document(["1", "2"]))

        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.UNCOVERED_HEADNOTE
            ).exists()
        )

    def test_a_second_apply_recomputes_idempotently(self):
        scan = self._make_scan()

        self._run(scan, self._document(["1", "2"]))
        scan.refresh_from_db()
        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(len(scan.ocr_results), 2)
        self.assertEqual(
            scan.issues.filter(check_name=CheckName.NO_PAGE_NUMBER).count(),
            0,
        )

    def test_a_lost_edge_leaves_the_scan_alone(self):
        """The #210 review race, closed: a scan cancelled between the
        pass's read and the edge write keeps its status, and its
        Issues are not rebuilt over a decision somebody just made."""
        scan = self._make_scan()
        Scan.objects.filter(pk=scan.pk).update(status=Status.CANCELLED)

        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertFalse(done)
        self.assertEqual(scan.status, Status.CANCELLED)
        self.assertFalse(scan.issues.exists())

    def test_a_download_failure_raises_to_the_caller(self):
        from scanning import services

        scan = self._make_scan()

        with patch(
            "scanning.s3_sync.download_json_object",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                services.run_compute_issues(scan, "jobs/x/r1-volume.json")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertFalse(scan.issues.exists())


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineConvertBranches(TestCase):
    """Where ``run_full_pipeline`` leaves a scan (issue #176).

    Four outcomes, and which one a volume gets is the whole decision
    this stage makes: hand the shards to doctor, or park because there
    is nothing to convert with, nothing worth converting, or no shards.
    """

    def _scan(self, pages=2):
        """Build a QUEUED scan whose original PDF exists on disk."""
        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=176,
            start_page=1,
            end_page=pages,
            status=Status.PROCESSING,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()
        return scan

    def _run(self, scan, manifest, is_bitonal=False, doctor=True):
        """Run the pipeline with sharding and the skip check stubbed."""
        from scanning import services

        with (
            patch("scanning.services._ensure_shards", return_value=manifest),
            patch(
                "scanning.bitonal.source_is_bitonal",
                return_value=is_bitonal,
            ),
            patch("scanning.doctor_client.enabled", return_value=doctor),
            # S3 is inert under TESTING, and the pipeline refuses to
            # create jobs whose shards doctor could not fetch.
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    @staticmethod
    def _manifest(shard_count=3, pages_per_shard=1):
        from scanning.tests.test_jobs import make_manifest

        return make_manifest(shard_count, pages_per_shard)

    def test_a_convertible_volume_waits_on_its_jobs(self):
        from scanning.models import ExternalJob, JobStage, JobStatus

        scan = self._scan(pages=3)

        scan = self._run(scan, self._manifest(shard_count=3))

        self.assertEqual(scan.status, Status.AWAITING)
        self.assertEqual(scan.page_count, 3)
        self.assertEqual(scan.progress_total, 3)
        self.assertIn("Converting 3 part", scan.progress_message)
        rows = ExternalJob.objects.filter(scan=scan, stage=JobStage.CONVERT)
        self.assertEqual(rows.count(), 3)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})

    def test_an_already_bitonal_volume_skips_the_stage(self):
        """Converting it would cost a full raster pass to save ~11%."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), is_bitonal=True)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(scan.page_count, 2)
        self.assertIn("already bitonal", scan.progress_message)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_without_doctor_the_scan_parks_as_before(self):
        """No in-process fallback exists, so this is the #173 behaviour."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), doctor=False)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertIn("temporarily disabled", scan.progress_message)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_without_s3_no_jobs_are_created(self):
        """Doctor fetches shards from S3, so no S3 means no conversion.

        This is what makes DOCTOR_ENABLED=True safe as a default:
        TESTING and a dev environment without credentials never uploaded
        the shards, so a job created there could never be submitted and
        would park its scan in AWAITING until its queue deadline expired
        hours later. Parking it unconverted is the honest outcome.
        """
        from scanning import services
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        with (
            patch(
                "scanning.services._ensure_shards",
                return_value=self._manifest(),
            ),
            patch("scanning.doctor_client.enabled", return_value=True),
            patch("scanning.s3_sync.s3_active", return_value=False),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_without_shards_no_jobs_are_created(self):
        """Sharding disabled means there is nothing for a job to read."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        scan = self._run(scan, None)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_a_second_run_reuses_the_jobs(self):
        """The re-queue path must not pay for the conversion twice."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=3)
        manifest = self._manifest(shard_count=3)

        self._run(scan, manifest)
        first = set(
            ExternalJob.objects.filter(scan=scan).values_list("pk", flat=True)
        )
        Scan = type(scan)
        Scan.objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        self._run(scan, manifest)

        self.assertEqual(
            set(
                ExternalJob.objects.filter(scan=scan).values_list(
                    "pk", flat=True
                )
            ),
            first,
        )

    def test_local_files_release_after_the_push(self):
        """S3 holds every byte after the push; the tree is a cache (#215)."""
        scan = self._scan(pages=3)

        with patch("scanning.s3_sync.release_local_processing") as release:
            self._run(scan, self._manifest(shard_count=3))

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, scan.pk)

    def test_a_parked_unconverted_scan_also_releases(self):
        scan = self._scan(pages=2)

        with patch("scanning.s3_sync.release_local_processing") as release:
            self._run(scan, self._manifest(), doctor=False)

        release.assert_called_once()

    def test_a_failed_push_keeps_the_local_files(self):
        """The local tree may hold bytes S3 never received."""
        scan = self._scan(pages=2)

        with (
            patch(
                "scanning.services._push_processing_files_to_s3",
                return_value=False,
            ),
            patch("scanning.s3_sync.release_local_processing") as release,
        ):
            self._run(scan, self._manifest(), doctor=False)

        release.assert_not_called()


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineOcrEnqueue(TestCase):
    """The pipeline enqueues the dots.mocr read (issue #207).

    The OCR rows are independent of the bitonal branch: the stage
    reads the original shards, so a volume that parks unconverted, or
    skips the conversion, still gets its read. ``_can_analyze`` is the
    gate, mirror of ``_can_convert``.
    """

    def _scan(self, pages=2, status=Status.PROCESSING):
        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=207,
            start_page=1,
            end_page=pages,
            status=status,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()
        return scan

    @staticmethod
    def _manifest(shard_count=2):
        from scanning.tests.test_jobs import make_manifest

        return make_manifest(shard_count, 1)

    def _run(
        self,
        scan,
        manifest,
        is_bitonal=False,
        doctor=True,
        dots=True,
        s3=True,
        cancel_midway=False,
    ):
        from scanning import services
        from scanning.models import Scan as ScanModel

        def _shard(inner_scan):
            if cancel_midway:
                ScanModel.objects.filter(pk=inner_scan.pk).update(
                    status=Status.CANCELLED
                )
            return manifest

        with (
            patch("scanning.services._ensure_shards", side_effect=_shard),
            patch(
                "scanning.bitonal.source_is_bitonal",
                return_value=is_bitonal,
            ),
            patch("scanning.doctor_client.enabled", return_value=doctor),
            patch("scanning.dots_mocr.enabled", return_value=dots),
            patch("scanning.s3_sync.s3_active", return_value=s3),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    @staticmethod
    def _analyze_rows(scan):
        from scanning.models import ExternalJob, JobStage

        return list(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.ANALYZE
            ).order_by("shard_index")
        )

    def test_a_new_upload_gets_ocr_rows_beside_the_convert_rows(self):
        from scanning.models import ExternalJob, JobStage, JobStatus

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(shard_count=2))

        self.assertEqual(scan.status, Status.AWAITING)
        rows = self._analyze_rows(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})
        self.assertEqual(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.CONVERT
            ).count(),
            2,
        )

    def test_a_volume_doctor_cannot_serve_still_gets_ocr(self):
        from scanning.models import ExternalJob, JobStage, JobStatus

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), doctor=False)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        rows = self._analyze_rows(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})
        self.assertFalse(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.CONVERT
            ).exists()
        )

    def test_an_already_bitonal_volume_still_gets_ocr(self):
        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), is_bitonal=True)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(len(self._analyze_rows(scan)), 2)

    def test_dots_mocr_off_creates_no_ocr_rows(self):
        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), dots=False)

        self.assertEqual(scan.status, Status.AWAITING)
        self.assertEqual(self._analyze_rows(scan), [])

    def test_without_s3_no_ocr_rows_are_created(self):
        """The worker fetches its shard through a presigned GET."""
        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), s3=False)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(self._analyze_rows(scan), [])

    def test_without_shards_no_ocr_rows_are_created(self):
        scan = self._scan(pages=2)

        scan = self._run(scan, None)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(self._analyze_rows(scan), [])

    def test_a_scan_cancelled_mid_shard_hands_back_its_ocr_rows(self):
        from scanning.models import JobStatus

        scan = self._scan(pages=2)

        with self.assertLogs("scanning.services", level="WARNING"):
            scan = self._run(scan, self._manifest(), cancel_midway=True)

        self.assertEqual(scan.status, Status.CANCELLED)
        rows = self._analyze_rows(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {JobStatus.CANCELLED})

    def test_a_lost_claim_keeps_carried_results_carryable(self):
        """The claim is lost most often to the daemon's own shutdown,
        which re-queues the scan and returns -- a retry, not an end. A
        carried COMPLETED row is a paid result the retry re-reads, so
        the hand-back cancels only the unstarted rows."""
        from scanning import dots_mocr
        from scanning.models import ExternalJob, JobStatus

        scan = self._scan(pages=2)
        manifest = self._manifest(shard_count=2)
        old = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=old[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/analyze/dots_mocr/r1-s0-a1.json",
        )
        ExternalJob.objects.filter(pk=old[1].pk).update(
            status=JobStatus.FAILED
        )

        with (
            patch("scanning.s3_sync.object_exists", return_value=True),
            self.assertLogs("scanning.services", level="WARNING"),
        ):
            scan = self._run(scan, manifest, cancel_midway=True)

        self.assertEqual(scan.status, Status.CANCELLED)
        carried, pending = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual(carried.status, JobStatus.COMPLETED)
        self.assertEqual(pending.status, JobStatus.CANCELLED)

        # The retry (an admin re-queue) carries the kept result again.
        type(scan).objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        with patch("scanning.s3_sync.object_exists", return_value=True):
            scan = self._run(scan, manifest)
        fresh = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].result_key, "jobs/analyze/dots_mocr/r1-s0-a1.json"
        )
        self.assertEqual(fresh[1].status, JobStatus.PENDING)

    def test_a_requeue_reuses_a_consumed_ocr_run(self):
        """An applied run is history nobody pays for twice."""
        from scanning import dots_mocr
        from scanning.models import ExternalJob, JobStatus

        scan = self._scan(pages=2)
        manifest = self._manifest(shard_count=2)
        first = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk__in=[row.pk for row in first]).update(
            status=JobStatus.CONSUMED
        )

        scan = self._run(scan, manifest)

        rows = self._analyze_rows(scan)
        self.assertEqual({row.pk for row in rows}, {row.pk for row in first})
        self.assertEqual({row.status for row in rows}, {JobStatus.CONSUMED})


class TestApplyUploadAction(TestCase):
    """Both upload actions queue the pipeline (issue #176)."""

    def test_upload_validate_queues_for_processing(self):
        from scanning.models import QueuedAction, UploadAction
        from scanning.services import apply_upload_action

        scan = ScanFactory(status=Status.UPLOADED)

        apply_upload_action(scan, UploadAction.UPLOAD_VALIDATE)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.stage, Stage.VALIDATE)
        self.assertEqual(scan.queued_action, QueuedAction.FULL_PIPELINE)
        self.assertIn("processing", scan.progress_message)

    def test_upload_only_is_queued_too(self):
        """It used to stay UPLOADED, so it never got a preview at all."""
        from scanning.models import QueuedAction, UploadAction
        from scanning.services import apply_upload_action

        scan = ScanFactory(status=Status.UPLOADED)

        apply_upload_action(scan, UploadAction.UPLOAD_ONLY)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.FULL_PIPELINE)
        self.assertIn("conversion", scan.progress_message)


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineRequeue(TestCase):
    """Re-queueing a scan that has already been through the stage."""

    def _scan_with_jobs(self, statuses):
        """Build a PROCESSING scan whose convert run has these statuses."""
        from scanning import jobs
        from scanning.models import ExternalJob
        from scanning.tests.test_jobs import make_manifest

        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=176,
            start_page=1,
            end_page=len(statuses),
            status=Status.PROCESSING,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        for _ in statuses:
            doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()

        manifest = make_manifest(len(statuses), 1)
        rows = jobs.ensure_convert_jobs(scan, manifest)
        for job, status in zip(rows, statuses, strict=True):
            ExternalJob.objects.filter(pk=job.pk).update(status=status)
        return scan, manifest

    def _run(self, scan, manifest):
        from scanning import services

        with (
            patch("scanning.services._ensure_shards", return_value=manifest),
            patch("scanning.bitonal.source_is_bitonal", return_value=False),
            patch("scanning.doctor_client.enabled", return_value=True),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    def test_an_already_converted_volume_is_not_converted_again(self):
        """Its shard results are deleted, so a re-merge would fail too."""
        from scanning.models import ExternalJob, JobStatus

        scan, manifest = self._scan_with_jobs(
            [JobStatus.CONSUMED, JobStatus.CONSUMED]
        )

        scan = self._run(scan, manifest)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(
            ExternalJob.objects.filter(scan=scan).count(),
            2,
            "no second run should have been created",
        )

    def test_a_cancelled_run_is_started_over(self):
        """The admin re-queue path: abandoned rows must not park a scan."""
        from scanning.models import ExternalJob, JobStatus

        scan, manifest = self._scan_with_jobs(
            [JobStatus.CANCELLED, JobStatus.CANCELLED]
        )

        scan = self._run(scan, manifest)

        self.assertEqual(scan.status, Status.AWAITING)
        pending = ExternalJob.objects.filter(
            scan=scan, status=JobStatus.PENDING
        )
        self.assertEqual(pending.count(), 2)
        self.assertEqual({job.run for job in pending}, {2})


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineStatusGuard(TestCase):
    """The pipeline only moves a scan it still owns (issue #176).

    The daemon claims a scan by moving it to PROCESSING, so anything
    else means somebody took it away -- and writing AWAITING anyway
    would start real external work on a volume that was stopped.
    """

    def _scan(self, status):
        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=176,
            start_page=1,
            end_page=2,
            status=status,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()
        return scan

    def _run(self, scan, cancel_midway=False):
        from scanning import services
        from scanning.models import Scan as ScanModel
        from scanning.tests.test_jobs import make_manifest

        def _shard(inner_scan):
            if cancel_midway:
                ScanModel.objects.filter(pk=inner_scan.pk).update(
                    status=Status.CANCELLED
                )
            return make_manifest(shard_count=2, pages_per_shard=1)

        with (
            patch("scanning.services._ensure_shards", side_effect=_shard),
            patch("scanning.bitonal.source_is_bitonal", return_value=False),
            patch("scanning.doctor_client.enabled", return_value=True),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    def test_a_scan_cancelled_mid_shard_is_not_resurrected(self):
        from scanning.models import ExternalJob, JobStatus

        scan = self._scan(Status.PROCESSING)

        with self.assertLogs("scanning.services", level="WARNING"):
            scan = self._run(scan, cancel_midway=True)

        self.assertEqual(scan.status, Status.CANCELLED)
        # Its rows were created before the status write failed, so they
        # are handed back rather than left for a wave to convert.
        self.assertEqual(
            set(
                ExternalJob.objects.filter(scan=scan).values_list(
                    "status", flat=True
                )
            ),
            {JobStatus.CANCELLED},
        )

    def test_the_park_paths_are_guarded_too(self):
        from scanning import services

        scan = self._scan(Status.CANCELLED)

        with self.assertLogs("scanning.services", level="WARNING"):
            services._park_unconverted(scan.pk, 2, "parked")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.CANCELLED)
        self.assertNotEqual(scan.progress_message, "parked")
