"""Tests for the redacted PDF of each opinion (issue #336, part 3).

Six groups:

- the ledger (``opinion_pdf.is_written``, ``opinion_pdf.due``): what a
  row owes, and in which order;
- the payload (``opinion_pdf.payload``): the rows in the small source's
  space;
- the write (``opinion_pdf.write_one``, ``opinion_pdf.run_tick``): the
  file, its ink, its pictures, the stamp, the fault;
- the local files: the inputs stay, the outputs go, the start release;
- the daemon: the schedule and the start step;
- the route (``serve_opinion_pdf``) and the logger entry.

The cut runs real blackletter over a synthetic bitonal volume built
from ``pdf_fixtures.write_two_column_page``; S3 is patched to the
local mirror, so nothing here needs a bucket.
"""

import logging
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fitz
from blackletter.models import Label
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import apply, boundaries, opinion_pdf, opinions, s3_sync
from scanning.factories import OpinionFactory, ScanFactory, UserFactory
from scanning.management.commands.run_daemon import Command as DaemonCommand
from scanning.models import (
    ApplyRun,
    Opinion,
    OpinionBoundary,
    OpinionReviewStatus,
    Redaction,
    Status,
)
from scanning.tests import pdf_fixtures
from scanning.tests.test_boundaries import (
    IMG_H,
    IMG_W,
    caption_row,
    document_of,
    key_row,
)
from scanning.tests.test_detections import model_row
from scanning.tests.test_opinions import make_run
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import identity_map

PAGES = 6


def write_volume(path: Path, pages: int = PAGES) -> None:
    """Write a bitonal volume of ``pages`` two-column pages.

    :param path: Where to write the PDF.
    :param pages: The page count.
    """
    one = path.parent / f"{path.stem}.one.pdf"
    pdf_fixtures.write_two_column_page(one, tmp_dir=path.parent)
    with fitz.open() as volume, fitz.open(str(one)) as page:
        for _ in range(pages):
            volume.insert_pdf(page)
        volume.save(str(path))


def write_colour_shard(path: Path, pages: int = PAGES) -> None:
    """Write a colour "original" shard with a red block on every page.

    The block sits where the tests put the ``IMAGE`` detection, so a
    picture rendered from it is red and a bitonal page is not.

    :param path: Where to write the PDF.
    :param pages: The page count.
    """
    with fitz.open() as doc:
        for _ in range(pages):
            page = doc.new_page(
                width=pdf_fixtures.PAGE_W, height=pdf_fixtures.PAGE_H
            )
            page.draw_rect(
                fitz.Rect(72, 300, 300, 500),
                color=None,
                fill=(1, 0, 0),
                width=0,
            )
        doc.save(str(path))


def column_rows(scan, page_index: int) -> None:
    """Store the two ``TEXT_COLUMN`` boxes of one page, in pixels.

    The bands of ``write_two_column_page`` (72 to 300 and 301.5 to 540
    points) at 200 dpi, a little inside the ink so the growth has
    something to grow over.

    :param scan: The scan.
    :param page_index: The 0-based page.
    """
    for x0, x1 in ((230, 820), (850, 1480)):
        model_row(
            scan,
            label="TEXT_COLUMN",
            label_id=int(Label.TEXT_COLUMN),
            page_index=page_index,
            source_page=page_index + 1,
            x0=x0,
            y0=280,
            x1=x1,
            y1=1940,
            img_width=IMG_W,
            img_height=IMG_H,
        )


def redaction(scan, page_index: int, **fields) -> Redaction:
    """Store one visible computed box, in points.

    :param scan: The scan.
    :param page_index: The 0-based page.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "origin": Redaction.Origin.COMPUTED,
        "rect_type": "headnote",
        "fill": "black",
        "x0": 80.0,
        "y0": 120.0,
        "x1": 280.0,
        "y1": 200.0,
        "source_page": page_index + 1,
        "source_fingerprint": scan.source_fingerprint,
        "page_index": page_index,
    }
    values.update(fields)
    return Redaction.objects.create(**values)


def pixel(path: Path, page: int, x: float, y: float) -> tuple:
    """Return the colour under one point of one page of a PDF.

    :param path: The PDF.
    :param page: The 0-based page.
    :param x: The x, in points.
    :param y: The y, in points.
    :returns: ``(r, g, b)``.
    """
    with fitz.open(str(path)) as doc:
        pix = doc[page].get_pixmap(dpi=72, colorspace=fitz.csRGB, alpha=False)
        return pix.pixel(int(x), int(y))


class OpinionPdfCase(ScanningTestCase):
    """A scan with a corrected volume in its mirror, and one opinion.

    The volume is the identity run's bitonal copy, placed where
    ``apply.local_copy`` looks first, so no pull happens. Uploads are
    captured into ``self.uploaded`` as ``{key: local copy}``.
    """

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="opinion-pdf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.scan = ScanFactory(
            page_count=PAGES,
            source_fingerprint="10:6",
            status=Status.REDACTION_REVIEW_DONE,
        )
        self.run = self.make_run(self.scan)
        self.volume_path = self.mirror(self.run.bitonal_key)
        write_volume(self.volume_path)
        self.uploaded: dict[str, Path] = {}

        def upload(key, path, content_type):
            dest = self.tmp / "uploaded" / Path(key).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dest)
            self.uploaded[key] = dest
            return True

        patcher = patch(
            "scanning.s3_sync.upload_file_object", side_effect=upload
        )
        self.upload = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("scanning.s3_sync.s3_active", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_run(self, scan) -> ApplyRun:
        run = make_run(scan)
        prefix = s3_sync.s3_processing_prefix(scan)
        run.page_map = identity_map(PAGES)
        run.final_pdf_key = f"{prefix}jobs/apply/a1/final.pdf"
        run.bitonal_key = f"{prefix}jobs/apply/a1/bitonal.pdf"
        run.ocr_key = f"{prefix}jobs/apply/a1/ocr.json"
        run.printed_pages_key = f"{prefix}jobs/apply/a1/printed_pages.json"
        run.detections_key = f"{prefix}jobs/apply/a1/detections.json"
        run.built_at = run.date_created
        run.save()
        return run

    def mirror(self, key: str) -> Path:
        """Return the local mirror path of ``key``, parents created."""
        prefix = s3_sync.s3_processing_prefix(self.scan)
        path = Path(self.scan.output_dir) / key[len(prefix) :]
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def opinion(self, start=1, end=3, **fields) -> Opinion:
        """One opinion over the 0-based pages ``start`` to ``end``.

        With its boundary (a caption on ``start``, a key on ``end``)
        and the column boxes of every page, so the masks can be drawn.
        The caption sits at 600 px (216 points), below the first text
        lines of the fixture, so the mask above it has ink to hide and
        ink to grow over.
        """
        for page in range(PAGES):
            column_rows(self.scan, page)
        caption = caption_row(self.scan, start, y0=600.0)
        key = key_row(self.scan, end)
        document, row_ids = document_of([caption, key])
        boundaries.write_computed(self.scan, document, row_ids, None, 1)
        boundary = OpinionBoundary.objects.computed().get(scan=self.scan)
        values = {
            "scan": self.scan,
            "first_printed_page": 100 + start,
            "last_printed_page": 100 + end,
            "page_count": end - start + 1,
            "start_source_page": start + 1,
            "start_page_index": start,
            "end_source_page": end + 1,
            "end_page_index": end,
            "apply_run": self.run,
            "boundary": boundary,
            "source_fingerprint": self.scan.source_fingerprint,
        }
        values.update(fields)
        return OpinionFactory(**values)


class TestLedger(TestCase):
    def test_a_new_row_is_not_written_and_is_due(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        row = OpinionFactory(scan=scan)

        self.assertFalse(opinion_pdf.is_written(row))
        self.assertEqual(list(opinion_pdf.due()), [row])

    def test_a_stamp_at_the_live_revision_is_written(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        row = OpinionFactory(
            scan=scan, glue_revision=2, redacted_pdf_revision=2
        )

        self.assertTrue(opinion_pdf.is_written(row))
        self.assertEqual(list(opinion_pdf.due()), [])

    def test_a_bump_makes_the_pdf_due_again(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        row = OpinionFactory(
            scan=scan, glue_revision=1, redacted_pdf_revision=1
        )
        Opinion.objects.filter(pk=row.pk).update(glue_revision=2)

        self.assertEqual(list(opinion_pdf.due()), [row])

    def test_error_rows_spent_rows_and_other_statuses_are_not_due(self):
        done = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        OpinionFactory(scan=done, status=OpinionReviewStatus.ERROR)
        OpinionFactory(scan=done, pdf_attempts=opinion_pdf.MAX_ATTEMPTS)
        back = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        OpinionFactory(scan=back)

        self.assertEqual(list(opinion_pdf.due()), [])

    def test_the_statuses_the_pass_reads_are_one_set(self):
        """#334 extends the set; a row of a moved scan must stay due."""
        self.assertIn(
            Status.REDACTION_REVIEW_DONE, opinion_pdf.OPINION_PDF_STATUSES
        )
        for status in opinion_pdf.OPINION_PDF_STATUSES:
            row = OpinionFactory(scan=ScanFactory(status=status))
            self.assertIn(row, list(opinion_pdf.due()))

    def test_a_failed_row_waits_the_cooldown(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        recent = OpinionFactory(
            scan=scan, first_printed_page=1, pdf_attempted_at=timezone.now()
        )
        old = OpinionFactory(
            scan=scan,
            first_printed_page=2,
            pdf_attempted_at=timezone.now()
            - opinion_pdf.RETRY_AFTER
            - timedelta(seconds=1),
        )

        self.assertEqual(list(opinion_pdf.due()), [old])
        self.assertNotIn(recent, list(opinion_pdf.due()))

    def test_newest_scan_first_then_reading_order(self):
        older = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        newer = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        old_row = OpinionFactory(scan=older, first_printed_page=1)
        second = OpinionFactory(
            scan=newer, first_printed_page=7, index_in_page=1
        )
        first = OpinionFactory(
            scan=newer, first_printed_page=7, index_in_page=0
        )
        earlier = OpinionFactory(scan=newer, first_printed_page=3)

        self.assertEqual(
            list(opinion_pdf.due()), [earlier, first, second, old_row]
        )
        self.assertEqual(opinion_pdf.next_due(), earlier)

    def test_the_key_and_the_download_name(self):
        scan = ScanFactory(volume=214)
        scan.reporter.short_name = "a3d"
        scan.reporter.save()
        row = OpinionFactory(
            scan=scan,
            first_printed_page=12,
            last_printed_page=15,
            glue_revision=3,
        )

        self.assertEqual(
            opinion_pdf.key(row),
            f"{s3_sync.s3_processing_prefix(scan)}jobs/opinions/o{row.pk}/"
            "r3/redacted.pdf",
        )
        self.assertEqual(
            opinion_pdf.download_name(row), "a3d.214.0012-0015.pdf"
        )
        row.index_in_page = 1
        self.assertEqual(
            opinion_pdf.download_name(row), "a3d.214.0012-0015.1.pdf"
        )


class TestPartOneBumpsTheRevision(TestCase):
    """The one line part 1 carries for part 3 (plan section 5.2)."""

    def _write(self, scan, run):
        from scanning.tests.test_opinions import (
            make_boundary,
            printed_document,
        )

        row = make_boundary(scan, run, 0, 1)
        printed = printed_document((500, "number"), (501, "number"))
        opinions.create_rows(scan, run, [row], printed)
        return Opinion.objects.get(scan=scan)

    def test_a_re_derived_unapproved_row_is_bumped_and_reset(self):
        scan = ScanFactory(page_count=2)
        run = make_run(scan)
        first = self._write(scan, run)
        self.assertEqual(first.glue_revision, 0)
        Opinion.objects.filter(pk=first.pk).update(
            pdf_attempts=2,
            pdf_attempted_at=timezone.now(),
            status=OpinionReviewStatus.ERROR,
        )

        second = self._write(scan, run)

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.glue_revision, 1)
        self.assertEqual(second.pdf_attempts, 0)
        self.assertIsNone(second.pdf_attempted_at)
        self.assertEqual(second.status, OpinionReviewStatus.PROCESSING)

    def test_an_approved_row_keeps_its_revision(self):
        scan = ScanFactory(page_count=2)
        run = make_run(scan)
        first = self._write(scan, run)
        Opinion.objects.filter(pk=first.pk).update(
            status=OpinionReviewStatus.TEXT_REVIEW_DONE, pdf_attempts=2
        )

        second = self._write(scan, run)

        self.assertEqual(second.glue_revision, 0)
        self.assertEqual(second.pdf_attempts, 2)
        self.assertEqual(second.status, OpinionReviewStatus.TEXT_REVIEW_DONE)


class TestPayload(OpinionPdfCase):
    def test_the_rows_land_in_the_small_source_space(self):
        row = self.opinion(start=1, end=3)
        redaction(self.scan, 2)
        redaction(self.scan, 0)  # outside the opinion
        model_row(
            self.scan,
            label="IMAGE",
            label_id=int(Label.IMAGE),
            page_index=2,
            source_page=3,
            x0=200,
            y0=850,
            x1=800,
            y1=1400,
            img_width=IMG_W,
            img_height=IMG_H,
        )

        with fitz.open(str(self.volume_path)) as volume:
            small = fitz.open()
            small.insert_pdf(volume, from_page=1, to_page=3)
            data = opinion_pdf.payload(row, volume, small)
            small.close()

        self.assertEqual(set(data["pages"]), {"1"})
        rect = data["pages"]["1"][0]
        self.assertEqual(
            (
                rect["x0"],
                rect["y0"],
                rect["x1"],
                rect["y1"],
                rect["fill"],
                rect["type"],
            ),
            (80.0, 120.0, 280.0, 200.0, "black", "headnote"),
        )
        (dict_,) = data["opinions"]
        self.assertEqual(dict_["caption_page"], 0)
        self.assertEqual(dict_["end_page"], 2)
        self.assertEqual(dict_["filename"], "redacted.pdf")
        self.assertNotIn("first_page_number", dict_)
        self.assertEqual(
            {m["page_index"] for m in dict_["outside_rects"]}, {0, 2}
        )
        self.assertEqual(set(data["images"]), {"1"})
        image = data["images"]["1"][0]
        self.assertAlmostEqual(image["x0"], 72.0, places=0)
        self.assertAlmostEqual(image["y0"], 306.0, places=0)

    def test_a_zero_render_size_falls_back_to_the_render_density(self):
        """A ``1`` in place of the zero would make the scale the page width."""
        row = self.opinion(start=1, end=3)
        model_row(
            self.scan,
            label="IMAGE",
            label_id=int(Label.IMAGE),
            page_index=2,
            source_page=3,
            x0=200,
            y0=850,
            x1=800,
            y1=1400,
            img_width=0,
            img_height=0,
        )
        with fitz.open(str(self.volume_path)) as volume:
            small = fitz.open()
            small.insert_pdf(volume, from_page=1, to_page=3)
            data = opinion_pdf.payload(row, volume, small)
            small.close()
        image = data["images"]["1"][0]
        self.assertAlmostEqual(image["x0"], 72.0, places=1)
        self.assertAlmostEqual(image["x1"], 288.0, places=1)
        self.assertLess(image["x1"], pdf_fixtures.PAGE_W)

    def test_a_reversed_range_refuses(self):
        row = self.opinion(start=1, end=3)
        Opinion.objects.filter(pk=row.pk).update(
            start_page_index=3, end_page_index=1
        )
        row.refresh_from_db()
        with fitz.open(str(self.volume_path)) as volume:
            with self.assertRaises(opinion_pdf.OpinionPdfError) as ctx:
                opinion_pdf._small_source(volume, row, self.tmp / "s.pdf")
        self.assertIn("before it", str(ctx.exception))

    def test_no_picture_gives_no_images_key(self):
        row = self.opinion(start=1, end=3)
        with fitz.open(str(self.volume_path)) as volume:
            small = fitz.open()
            small.insert_pdf(volume, from_page=1, to_page=3)
            data = opinion_pdf.payload(row, volume, small)
            small.close()
        self.assertNotIn("images", data)

    def test_a_null_boundary_refuses(self):
        row = self.opinion(start=1, end=3, boundary=None)
        with fitz.open(str(self.volume_path)) as volume:
            small = fitz.open()
            small.insert_pdf(volume, from_page=1, to_page=3)
            with self.assertRaises(opinion_pdf.OpinionPdfError) as ctx:
                opinion_pdf.payload(row, volume, small)
            small.close()
        self.assertIn("boundary of this opinion is gone", str(ctx.exception))

    def test_the_masks_grow_to_the_ink_with_the_document(self):
        self.opinion(start=1, end=3)
        boundary = self.scan.opinion_boundaries.get()

        bare = boundaries.outside_rects(self.scan, [boundary])[boundary.pk]
        with fitz.open(str(self.volume_path)) as volume:
            grown = boundaries.outside_rects(
                self.scan, [boundary], document=volume
            )[boundary.pk]

        first_bare = [r for r in bare if r["page_index"] == 1][0]
        first_grown = [r for r in grown if r["page_index"] == 1][0]
        # The column box starts at 230 px (82.8 pt); the ink at 72 pt.
        self.assertAlmostEqual(first_bare["x0"], 82.8, places=1)
        self.assertLess(first_grown["x0"], first_bare["x0"])
        self.assertAlmostEqual(first_grown["x0"], 72.0, delta=1.5)


class TestWrite(OpinionPdfCase):
    def test_the_file_is_cut_painted_uploaded_and_stamped(self):
        row = self.opinion(start=1, end=3)
        redaction(self.scan, 2, x0=80.0, y0=120.0, x1=280.0, y1=200.0)
        redaction(
            self.scan,
            3,
            rect_type="margin",
            fill="white",
            x0=72.0,
            y0=100.0,
            x1=300.0,
            y1=160.0,
        )

        summary = opinion_pdf.write_one(row)

        key = opinion_pdf.key(row)
        self.assertEqual(list(self.uploaded), [key])
        written = self.uploaded[key]
        with fitz.open(str(written)) as out:
            self.assertEqual(out.page_count, 3)
        # The black box on the second page of the opinion.
        self.assertEqual(pixel(written, 1, 180, 160), (0, 0, 0))
        # The white margin box on the third page covers the ink lines.
        self.assertEqual(pixel(written, 2, 150, 130), (255, 255, 255))
        row.refresh_from_db()
        self.assertTrue(opinion_pdf.is_written(row))
        self.assertEqual(row.pdf_attempts, 0)
        self.assertIsNone(row.pdf_attempted_at)
        self.assertEqual(summary["pages"], 3)
        # The scratch directory is gone; the inputs stay.
        self.assertFalse(opinion_pdf._scratch_dir(row).exists())
        self.assertTrue(self.volume_path.exists())

    def test_the_first_page_is_masked_above_the_caption(self):
        row = self.opinion(start=1, end=3)
        opinion_pdf.write_one(row)
        written = self.uploaded[opinion_pdf.key(row)]
        # Left column, above the caption (its top is at 216 pt): the
        # fixture's line at y=100..104 is masked.
        self.assertEqual(pixel(written, 0, 150, 102), (255, 255, 255))
        # The right column keeps that line: the caption is in the left.
        self.assertEqual(pixel(written, 0, 400, 102), (0, 0, 0))
        # A line below the caption keeps its ink.
        self.assertEqual(pixel(written, 0, 150, 402), (0, 0, 0))

    def test_the_picture_comes_from_the_shard(self):
        row = self.opinion(start=1, end=3)
        shard_key = f"{s3_sync.shards_prefix(self.scan)}shard-000.pdf"
        write_colour_shard(self.mirror(shard_key))
        manifest = {
            "shards": [
                {
                    "name": "shard-000.pdf",
                    "index": 0,
                    "from_page": 0,
                    "to_page": 5,
                }
            ]
        }
        model_row(
            self.scan,
            label="IMAGE",
            label_id=int(Label.IMAGE),
            page_index=2,
            source_page=3,
            x0=200,
            y0=850,
            x1=800,
            y1=1400,
            img_width=IMG_W,
            img_height=IMG_H,
        )

        with patch(
            "scanning.sharding.committed_manifest", return_value=(manifest, "")
        ):
            summary = opinion_pdf.write_one(row)

        written = self.uploaded[opinion_pdf.key(row)]
        with fitz.open(str(written)) as out:
            # The bitonal page is one image; the picture is the second.
            self.assertEqual(len(out[1].get_images()), 2)
            self.assertEqual(len(out[0].get_images()), 1)
        r, g, b = pixel(written, 1, 150, 400)
        self.assertGreater(r, 200)
        self.assertLess(g, 80)
        self.assertEqual(summary["images"], 1)
        # The shard stays in the mirror for the next tick.
        self.assertTrue(self.mirror(shard_key).exists())

    def test_the_rect_is_scaled_to_the_shard_page(self):
        """A shard page twice the size gives a clip twice the size."""
        row = self.opinion(start=1, end=3)
        shard_key = f"{s3_sync.shards_prefix(self.scan)}shard-000.pdf"
        path = self.mirror(shard_key)
        with fitz.open() as doc:
            for _ in range(PAGES):
                page = doc.new_page(
                    width=pdf_fixtures.PAGE_W * 2,
                    height=pdf_fixtures.PAGE_H * 2,
                )
                page.draw_rect(
                    fitz.Rect(144, 600, 600, 1000),
                    color=None,
                    fill=(0, 0, 1),
                    width=0,
                )
            doc.save(str(path))
        manifest = {
            "shards": [
                {
                    "name": "shard-000.pdf",
                    "index": 0,
                    "from_page": 0,
                    "to_page": 5,
                }
            ]
        }
        model_row(
            self.scan,
            label="IMAGE",
            label_id=int(Label.IMAGE),
            page_index=2,
            source_page=3,
            x0=200,
            y0=850,
            x1=800,
            y1=1400,
            img_width=IMG_W,
            img_height=IMG_H,
        )

        with patch(
            "scanning.sharding.committed_manifest", return_value=(manifest, "")
        ):
            opinion_pdf.write_one(row)

        written = self.uploaded[opinion_pdf.key(row)]
        r, g, b = pixel(written, 1, 150, 400)
        self.assertGreater(b, 200)
        self.assertLess(r, 80)

    def test_a_shard_that_does_not_open_leaves_the_page_bitonal(self):
        row = self.opinion(start=1, end=3)
        manifest = {
            "shards": [
                {
                    "name": "missing.pdf",
                    "index": 0,
                    "from_page": 0,
                    "to_page": 5,
                }
            ]
        }
        model_row(
            self.scan,
            label="IMAGE",
            label_id=int(Label.IMAGE),
            page_index=2,
            source_page=3,
            x0=200,
            y0=850,
            x1=800,
            y1=1400,
            img_width=IMG_W,
            img_height=IMG_H,
        )

        with (
            patch(
                "scanning.sharding.committed_manifest",
                return_value=(manifest, ""),
            ),
            patch(
                "scanning.s3_sync.download_object", side_effect=OSError("no")
            ),
            self.assertLogs("scanning.opinion_pdf", level="WARNING") as logs,
        ):
            opinion_pdf.write_one(row)

        self.assertTrue(
            any("could not open the shard" in m for m in logs.output)
        )
        written = self.uploaded[opinion_pdf.key(row)]
        with fitz.open(str(written)) as out:
            # The bitonal page alone: no picture was inserted.
            self.assertEqual(len(out[1].get_images()), 1)
        r, g, b = pixel(written, 1, 150, 400)
        self.assertEqual(r, g)
        self.assertEqual(g, b)
        row.refresh_from_db()
        self.assertTrue(opinion_pdf.is_written(row))

    def test_a_bump_during_the_write_leaves_the_stamp_behind(self):
        row = self.opinion(start=1, end=3)
        real = opinion_pdf._stamp

        def bump_then_stamp(opinion):
            Opinion.objects.filter(pk=opinion.pk).update(glue_revision=5)
            return real(opinion)

        with patch("scanning.opinion_pdf._stamp", side_effect=bump_then_stamp):
            opinion_pdf.write_one(row)

        row.refresh_from_db()
        self.assertEqual(row.glue_revision, 5)
        self.assertIsNone(row.redacted_pdf_revision)
        self.assertFalse(opinion_pdf.is_written(row))

    def test_another_run_refuses(self):
        row = self.opinion(start=1, end=3)
        older = make_run(self.scan, number=0)
        ApplyRun.objects.filter(pk=older.pk).update(
            superseded_at=older.date_created
        )
        Opinion.objects.filter(pk=row.pk).update(apply_run=older)
        row.refresh_from_db()

        with self.assertRaises(opinion_pdf.OpinionPdfError) as ctx:
            opinion_pdf.write_one(row)
        self.assertIn("corrected volume changed", str(ctx.exception))
        self.assertEqual(self.uploaded, {})

    def test_a_pull_that_fails_is_transient(self):
        row = self.opinion(start=1, end=3)
        with (
            patch(
                "scanning.apply.local_copy",
                side_effect=apply.ApplyError("could not pull"),
            ),
            self.assertRaises(opinion_pdf.TransientFault),
        ):
            opinion_pdf.write_one(row)

    def test_a_put_that_fails_is_transient(self):
        row = self.opinion(start=1, end=3)
        self.upload.side_effect = None
        self.upload.return_value = False
        with self.assertRaises(opinion_pdf.TransientFault):
            opinion_pdf.write_one(row)
        row.refresh_from_db()
        self.assertFalse(opinion_pdf.is_written(row))

    def test_a_failed_result_is_the_opinions_fault(self):
        row = self.opinion(start=1, end=3)
        result = {
            "files": [None],
            "failed": [{"index": 0, "error": "insert_pdf past the end"}],
            "full_redacted": None,
        }
        with (
            patch("blackletter.api.generate", return_value=result),
            self.assertRaises(opinion_pdf.OpinionPdfError) as ctx,
        ):
            opinion_pdf.write_one(row)
        self.assertIn("insert_pdf past the end", str(ctx.exception))
        self.assertEqual(self.uploaded, {})
        self.assertFalse(opinion_pdf._scratch_dir(row).exists())


class TestTick(OpinionPdfCase):
    def test_one_tick_writes_one_pdf_and_keeps_the_mirror(self):
        first = self.opinion(start=1, end=2)
        second = OpinionFactory(
            scan=self.scan,
            first_printed_page=104,
            last_printed_page=105,
            page_count=2,
            start_source_page=4,
            start_page_index=3,
            end_source_page=5,
            end_page_index=4,
            apply_run=self.run,
            boundary=first.boundary,
        )

        with patch("scanning.s3_sync.release_local_processing") as release:
            written = opinion_pdf.run_tick()

        self.assertEqual(written, 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(opinion_pdf.is_written(first))
        self.assertFalse(opinion_pdf.is_written(second))
        release.assert_not_called()

    def test_the_tick_that_leaves_nothing_due_releases_the_mirror(self):
        self.opinion(start=1, end=2)

        with patch("scanning.s3_sync.release_local_processing") as release:
            opinion_pdf.run_tick()

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, self.scan.pk)

    def test_no_due_row_is_a_no_op(self):
        with patch("scanning.apply.local_copy") as pull:
            self.assertEqual(opinion_pdf.run_tick(), 0)
        pull.assert_not_called()

    def test_a_fault_counts_on_the_row_and_the_next_tick_moves_on(self):
        broken = self.opinion(start=1, end=2, boundary=None)
        good = OpinionFactory(
            scan=self.scan,
            first_printed_page=104,
            last_printed_page=105,
            page_count=2,
            start_source_page=4,
            start_page_index=3,
            end_source_page=5,
            end_page_index=4,
            apply_run=self.run,
            boundary=self.scan.opinion_boundaries.get(),
        )

        with self.assertLogs("scanning.opinion_pdf", level="WARNING"):
            self.assertEqual(opinion_pdf.run_tick(), 0)
        broken.refresh_from_db()
        self.assertEqual(broken.pdf_attempts, 1)
        self.assertIsNotNone(broken.pdf_attempted_at)
        self.assertIn("boundary of this opinion is gone", broken.error_message)
        self.assertEqual(broken.status, OpinionReviewStatus.PROCESSING)

        # The broken row is under its cooldown, so it no longer holds
        # the head of the queue: the next tick writes the good one.
        self.assertEqual(opinion_pdf.run_tick(), 1)
        good.refresh_from_db()
        self.assertTrue(opinion_pdf.is_written(good))

        # Two more faults, each after its cooldown, close the row.
        expired = (
            timezone.now() - opinion_pdf.RETRY_AFTER - timedelta(seconds=1)
        )
        for _ in range(opinion_pdf.MAX_ATTEMPTS - 1):
            Opinion.objects.filter(pk=broken.pk).update(
                pdf_attempted_at=expired
            )
            with self.assertLogs("scanning.opinion_pdf", level="WARNING"):
                opinion_pdf.run_tick()
        broken.refresh_from_db()
        self.assertEqual(broken.pdf_attempts, opinion_pdf.MAX_ATTEMPTS)
        self.assertEqual(broken.status, OpinionReviewStatus.ERROR)

    def test_a_raise_inside_the_write_is_transient(self):
        row = self.opinion(start=1, end=2)
        with (
            patch(
                "blackletter.api.generate",
                side_effect=ValueError("bad payload"),
            ),
            self.assertLogs("scanning.opinion_pdf", level="ERROR"),
        ):
            self.assertEqual(opinion_pdf.run_tick(), 0)
        row.refresh_from_db()
        self.assertEqual(row.pdf_attempts, 0)
        self.assertIsNotNone(row.pdf_attempted_at)
        self.assertIn("ValueError: bad payload", row.error_message)
        self.assertEqual(list(opinion_pdf.due()), [])

    def test_a_transient_fault_spends_no_attempt(self):
        row = self.opinion(start=1, end=2)
        self.upload.side_effect = None
        self.upload.return_value = False
        with self.assertLogs("scanning.opinion_pdf", level="WARNING") as logs:
            self.assertEqual(opinion_pdf.run_tick(), 0)
        self.assertTrue(any("transient fault" in m for m in logs.output))
        row.refresh_from_db()
        self.assertEqual(row.pdf_attempts, 0)
        self.assertEqual(row.status, OpinionReviewStatus.PROCESSING)
        self.assertIsNotNone(row.pdf_attempted_at)
        # Past the cooldown it is due again, with its attempts intact.
        Opinion.objects.filter(pk=row.pk).update(
            pdf_attempted_at=timezone.now()
            - opinion_pdf.RETRY_AFTER
            - timedelta(seconds=1)
        )
        self.assertEqual(list(opinion_pdf.due()), [row])

    def test_the_volume_on_disk_is_finished_first(self):
        on_disk = self.opinion(start=1, end=2)
        newer_scan = ScanFactory(
            page_count=PAGES,
            source_fingerprint="10:6",
            status=Status.REDACTION_REVIEW_DONE,
        )
        newer = OpinionFactory(scan=newer_scan)
        self.assertGreater(newer_scan.pk, self.scan.pk)
        self.assertEqual(opinion_pdf.due().first(), newer)
        self.assertFalse(Path(newer_scan.output_dir).is_dir())

        self.assertEqual(opinion_pdf.next_due(), on_disk)

        # With no mirror anywhere, the newest scan goes first.
        shutil.rmtree(self.scan.output_dir)
        self.assertEqual(opinion_pdf.next_due(), newer)

    def test_a_reviewed_row_at_the_cap_keeps_its_status(self):
        row = self.opinion(
            start=1,
            end=2,
            boundary=None,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
            pdf_attempts=opinion_pdf.MAX_ATTEMPTS - 1,
        )
        with self.assertLogs("scanning.opinion_pdf", level="ERROR"):
            opinion_pdf.run_tick()
        row.refresh_from_db()
        self.assertEqual(row.status, OpinionReviewStatus.READY_FOR_TEXT_REVIEW)
        self.assertEqual(row.pdf_attempts, opinion_pdf.MAX_ATTEMPTS)
        self.assertEqual(list(opinion_pdf.due()), [])

    def test_the_command_runs_one_tick(self):
        from django.core.management import call_command

        self.opinion(start=1, end=2)
        # The tick closes every connection, which would tear down the
        # test transaction (the ``test_process_next_scan`` rule).
        with (
            patch("scanning.s3_sync.release_local_processing"),
            patch("django.db.connections.close_all"),
        ):
            call_command("build_opinion_pdfs")
        self.assertEqual(len(self.uploaded), 1)


class TestReleaseMirrors(TestCase):
    def test_releases_the_trees_of_done_scans_alone(self):
        tmp = Path(tempfile.mkdtemp(prefix="mirrors-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        done = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        busy = ScanFactory(status=Status.PROCESSING)
        for scan in (done, busy):
            (tmp / str(scan.pk)).mkdir()
        (tmp / "not-a-scan").mkdir()

        with (
            override_settings(DEVELOPMENT=False, PROCESSING_TMP_DIR=str(tmp)),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.s3_sync.release_local_processing", return_value=True
            ) as release,
        ):
            self.assertEqual(opinion_pdf.release_mirrors(), 1)

        self.assertEqual(
            [c.args[0].pk for c in release.call_args_list], [done.pk]
        )

    def test_does_nothing_under_development(self):
        with (
            override_settings(DEVELOPMENT=True),
            patch("scanning.s3_sync.release_local_processing") as release,
        ):
            self.assertEqual(opinion_pdf.release_mirrors(), 0)
        release.assert_not_called()


class TestDaemon(TestCase):
    @override_settings(DAEMON_OPINION_PDF_INTERVAL=7)
    def test_the_pass_is_the_last_task_of_the_schedule(self):
        schedule = DaemonCommand()._build_schedule()
        self.assertEqual(schedule[-1].name, "build_opinion_pdfs")
        self.assertEqual(schedule[-1].interval_seconds, 7.0)

    def test_the_start_releases_the_mirrors_once_before_the_loop(self):
        cmd = DaemonCommand()
        cmd.shutdown = False
        order = []

        def fake_call_command(name, *a, **kw):
            order.append(name)
            cmd.shutdown = True

        with (
            patch(
                "scanning.management.commands.run_daemon.call_command",
                side_effect=fake_call_command,
            ),
            patch("scanning.management.commands.run_daemon.signal.signal"),
            patch("scanning.management.commands.run_daemon.time.sleep"),
            patch(
                "scanning.opinion_pdf.release_mirrors",
                side_effect=lambda: order.append("release") or 0,
            ) as release,
        ):
            cmd.handle()

        release.assert_called_once()
        self.assertEqual(order[0], "release")


class TestRoute(ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.scan = ScanFactory(volume=214)
        self.scan.reporter.short_name = "a3d"
        self.scan.reporter.save()
        self.row = OpinionFactory(
            scan=self.scan, first_printed_page=12, last_printed_page=15
        )
        self.url = reverse(
            "serve_opinion_pdf",
            kwargs={"pk": self.scan.pk, "opinion_pk": self.row.pk},
        )

    def test_needs_a_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_404_before_the_write(self):
        self.client.force_login(UserFactory())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertIn("not written yet", body["error"])
        self.assertEqual(body["opinion"], self.row.pk)
        self.assertEqual(body["revision"], 0)
        self.assertNotIn("run", body)

    def test_404_for_an_opinion_of_another_scan(self):
        other = OpinionFactory(redacted_pdf_revision=0)
        self.client.force_login(UserFactory())
        response = self.client.get(
            reverse(
                "serve_opinion_pdf",
                kwargs={"pk": self.scan.pk, "opinion_pk": other.pk},
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_redirects_to_a_presigned_get_with_the_download_name(self):
        Opinion.objects.filter(pk=self.row.pk).update(redacted_pdf_revision=0)
        self.client.force_login(UserFactory())
        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
            patch(
                "scanning.s3_sync.presign_get",
                return_value="https://bucket/signed",
            ) as presign,
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://bucket/signed")
        self.assertEqual(presign.call_args.args[0], opinion_pdf.key(self.row))
        self.assertEqual(
            presign.call_args.kwargs["content_disposition"],
            'attachment; filename="a3d.214.0012-0015.pdf"',
        )


class TestLogging(TestCase):
    def test_the_blackletter_logger_has_the_console_handler(self):
        entry = settings.LOGGING["loggers"]["blackletter"]
        self.assertEqual(entry["handlers"], ["console"])
        self.assertTrue(entry["propagate"])
        self.assertEqual(entry["level"], settings.BLACKLETTER_LOG_LEVEL)

    def test_generate_logs_its_start_line_under_the_small_source_name(self):
        """The prefix names the opinion, so interleaved ticks read apart."""
        logger = logging.getLogger("blackletter.api")
        with fitz.open() as doc:
            doc.new_page()
            tmp = Path(tempfile.mkdtemp(prefix="bl-log-"))
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            source = tmp / "o7.r0.pdf"
            doc.save(str(source))
        from blackletter.api import generate

        with self.assertLogs(logger, level="INFO") as logs:
            generate(
                pdf_path=source,
                redactions={
                    "opinions": [
                        {
                            "caption_page": 0,
                            "end_page": 0,
                            "filename": "redacted.pdf",
                        }
                    ],
                    "pages": {},
                },
                output_dir=tmp / "out",
                full_redacted=False,
            )
        self.assertTrue(any("o7.r0.pdf" in line for line in logs.output))
