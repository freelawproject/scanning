"""Tests for ``scanning.bands`` (issue #620).

A picture across both columns on a page two opinions share: the trim
of the model's box off the next opinion, and the masks that read the
page as the columns above the picture, the picture, the columns under
it. The pages are the three of the volume the issue names, in the
pixels of a 1700 by 2200 render.
"""

from blackletter.models import Label
from django.test import TestCase

from scanning import bands
from scanning.factories import ScanFactory
from scanning.models import Detection
from scanning.tests.test_detections import model_row
from scanning.text_fit import PageCells

IMG_W, IMG_H = 1700, 2200
#: Points per pixel of the render.
PT = 72 / 200

LEFT = (84.0, 304.0)
RIGHT = (305.0, 524.0)


def row(scan, label, page_index, box, **fields):
    """Store one model detection of ``label`` at ``box``, in pixels."""
    return model_row(
        scan,
        label=label.name,
        label_id=int(label),
        page_index=page_index,
        source_page=page_index + 1,
        x0=box[0],
        y0=box[1],
        x1=box[2],
        y1=box[3],
        confidence=fields.pop("confidence", 0.95),
        img_width=IMG_W,
        img_height=IMG_H,
        **fields,
    )


def columns(scan, page_index, top=180.0, bottom=2000.0):
    """Store the two column boxes of a page."""
    row(scan, Label.TEXT_COLUMN, page_index, (235, top, 845, bottom))
    row(scan, Label.TEXT_COLUMN, page_index, (855, top, 1455, bottom))


def cells(*boxes):
    """Return the cells of one page, in the render's own pixels."""
    return PageCells(float(IMG_W), float(IMG_H), tuple(boxes))


def layout(*pictures):
    """Return a page of two columns with ``pictures`` across them."""
    return bands.Layout(
        left=LEFT, right=RIGHT, top=60.0, bottom=720.0, bands=pictures
    )


class TestIsFullWidth(TestCase):
    def test_a_box_over_both_columns_is(self):
        self.assertTrue(bands.is_full_width((100, 0, 510, 9), LEFT, RIGHT))

    def test_a_box_in_one_column_is_not(self):
        self.assertFalse(bands.is_full_width((90, 0, 300, 9), LEFT, RIGHT))

    def test_a_sliver_into_the_facing_column_is_not(self):
        self.assertFalse(bands.is_full_width((90, 0, 340, 9), LEFT, RIGHT))


class TestOutside(TestCase):
    """The masks of a page read as columns, picture, columns."""

    PICTURE = (100.0, 62.0, 510.0, 415.0)

    def test_the_key_in_the_left_column_keeps_the_picture_whole(self):
        # The opinion ends at a key under the picture, left column: the
        # columns under the picture are masked from the key on, and no
        # mask reaches above the picture's foot.
        masks = bands.outside(
            layout(self.PICTURE), 0, 0, False, True, end=(238.0, 476.0)
        )

        self.assertEqual(
            [(m.column, m.box) for m in masks],
            [
                ("left", (84.0, 476.0, 304.0, 720.0)),
                ("right", (305.0, 415.0, 524.0, 720.0)),
            ],
        )

    def test_the_next_opinion_masks_the_picture_whatever_its_column(self):
        # A caption in the left column under the picture: the old rule
        # masked the left column above it and left the right half of
        # the picture.
        masks = bands.outside(layout(self.PICTURE), 87.0, 523.0, True, False)

        self.assertEqual(
            [(m.column, m.box) for m in masks],
            [
                ("", (84.0, 60.0, 524.0, 415.0)),
                ("left", (84.0, 415.0, 304.0, 523.0)),
            ],
        )

    def test_a_caption_in_the_right_column_masks_the_left_one_under_it(self):
        masks = bands.outside(layout(self.PICTURE), 308.0, 594.0, True, False)

        self.assertEqual(
            [(m.column, m.box) for m in masks],
            [
                ("", (84.0, 60.0, 524.0, 415.0)),
                ("left", (84.0, 415.0, 304.0, 720.0)),
                ("right", (305.0, 415.0, 524.0, 594.0)),
            ],
        )

    def test_a_caption_at_the_gutter_is_read_inside_its_box(self):
        # The volume's gutter is a few points off this page's: a
        # caption whose left edge is at the divide is the right
        # column's.
        divide = layout().divide
        masks = bands.outside(
            layout(self.PICTURE), divide - 2, 594.0, True, False
        )

        self.assertEqual([m.column for m in masks], ["", "left", "right"])

    def test_an_opinion_that_ends_above_the_picture_masks_it(self):
        picture = (100.0, 400.0, 510.0, 700.0)
        masks = bands.outside(
            layout(picture), 0, 0, False, True, end=(500.0, 300.0)
        )

        self.assertEqual(
            [(m.column, m.box) for m in masks],
            [
                ("right", (305.0, 300.0, 524.0, 400.0)),
                # Three points above the picture: blackletter paints a
                # mask that far under its top.
                ("", (84.0, 397.0, 524.0, 720.0)),
            ],
        )

    def test_an_anchor_inside_the_picture_answers_nothing(self):
        # The box is drawn over the caption: nothing says where the
        # picture ends, and the page keeps blackletter's masks.
        picture = (100.0, 62.0, 510.0, 715.0)

        self.assertIsNone(
            bands.outside(layout(picture), 308.0, 594.0, True, False)
        )
        self.assertIsNone(
            bands.outside(
                layout(picture), 0, 0, False, True, end=(238.0, 629.0)
            )
        )

    def test_a_caption_at_the_foot_of_a_trimmed_picture_is_under_it(self):
        picture = (100.0, 62.0, 510.0, 595.0)

        masks = bands.outside(layout(picture), 308.0, 594.0, True, False)

        self.assertIsNotNone(masks)
        self.assertEqual(masks[0].box, (84.0, 60.0, 524.0, 595.0))


class TestHeldAnchors(TestCase):
    """The anchors a picture box is drawn over, by their own point."""

    BOX = (232.0, 172.0, 1450.0, 1700.0)

    def test_a_caption_whose_top_is_under_the_box_is_held(self):
        # Most of the caption is below the box: the boundary's anchor
        # is its top, which the masks read, so the box is over it.
        caption = ((857.0, 1650.0, 1424.0, 1900.0), bands.CAPTION)

        self.assertEqual(bands.held_anchors(self.BOX, [caption]), [caption])

    def test_a_caption_at_the_foot_of_the_box_is_not(self):
        caption = ((857.0, 1696.0, 1424.0, 1900.0), bands.CAPTION)

        self.assertEqual(bands.held_anchors(self.BOX, [caption]), [])

    def test_a_key_is_read_at_its_foot(self):
        # A key whose foot is under the top edge of a picture ends an
        # opinion above the picture.
        above = ((411.0, 100.0, 661.0, 176.0), bands.KEY)
        inside = ((411.0, 1233.0, 661.0, 1322.0), bands.KEY)

        self.assertEqual(
            bands.held_anchors(self.BOX, [above, inside]), [inside]
        )

    def test_an_anchor_beside_the_box_is_not_held(self):
        key = ((100.0, 1233.0, 200.0, 1322.0), bands.KEY)

        self.assertEqual(bands.held_anchors(self.BOX, [key]), [])


class TestTrimmedFoot(TestCase):
    """Where a box the model ran to the foot of the page should end."""

    BOX = (232.0, 172.0, 1450.0, 2009.0)
    KEY = ((411.0, 1233.0, 661.0, 1322.0), bands.KEY)
    DIVIDE = 850.0

    def test_the_facing_column_starts_above_the_key(self):
        # The picture's last line ends at 1134, the right column starts
        # at 1172, above the key icon at 1233: the box ends between.
        page = [
            (305, 198, 1345, 315),
            (757, 1098, 936, 1134),
            (864, 1172, 1434, 1243),
            (894, 1277, 1102, 1308),
            (253, 1453, 830, 1562),
        ]

        foot = bands.trimmed_foot(self.BOX, [self.KEY], page, self.DIVIDE)

        self.assertEqual(foot, (1134 + 1172) / 2)

    def test_a_block_of_the_picture_beside_the_gutter_stays_in_it(self):
        # A signature block set in the right half of a form, a blank
        # above the caption: the column starts at the caption.
        key = ((439.0, 1658.0, 661.0, 1747.0), bands.KEY)
        caption = ((857.0, 1650.0, 1424.0, 1900.0), bands.CAPTION)
        page = [
            (336, 1091, 1349, 1199),
            (879, 1314, 1140, 1350),
            (879, 1466, 1225, 1499),
            (857, 1650, 1424, 1721),
        ]

        foot = bands.trimmed_foot(self.BOX, [key, caption], page, self.DIVIDE)

        self.assertEqual(foot, (1499 + 1650) / 2)

    def test_the_anchor_s_own_column_is_not_walked(self):
        # A typed block of a form runs down the left half to just above
        # the key, a line every 70 px: it is the picture's, and the
        # walk reads the facing column alone.
        block = [(300, y, 800, y + 40) for y in range(500, 1200, 70)]

        foot = bands.trimmed_foot(self.BOX, [self.KEY], block, self.DIVIDE)

        self.assertEqual(foot, (1170 + 1233) / 2)

    def test_a_page_nobody_read_ends_the_box_at_the_anchor(self):
        foot = bands.trimmed_foot(self.BOX, [self.KEY], [], self.DIVIDE)

        self.assertEqual(foot, 1233 - bands.FOOT_GAP_PX)

    def test_a_box_that_holds_no_anchor_is_left_alone(self):
        key = ((411.0, 2050.0, 661.0, 2100.0), bands.KEY)

        self.assertIsNone(bands.trimmed_foot(self.BOX, [key], [], self.DIVIDE))

    def test_a_caption_with_no_key_trims_nothing(self):
        # A form the picture reproduces has a caption of its own: no
        # key icon ends an opinion here, so the box is the card's.
        caption = ((500.0, 600.0, 1200.0, 760.0), bands.CAPTION)

        self.assertIsNone(
            bands.trimmed_foot(self.BOX, [caption], [], self.DIVIDE)
        )

    def test_a_trim_that_leaves_no_picture_is_refused(self):
        key = ((411.0, 190.0, 661.0, 215.0), bands.KEY)

        self.assertIsNone(bands.trimmed_foot(self.BOX, [key], [], self.DIVIDE))


class TestTrimRows(TestCase):
    def _page(self, scan, page_index=3):
        picture = row(
            scan,
            Label.IMAGE,
            page_index,
            (232, 172, 1450, 2009),
            confidence=0.7,
        )
        row(scan, Label.KEY_ICON, page_index, (411, 1233, 661, 1322))
        return picture

    def test_the_model_box_ends_above_the_columns_under_it(self):
        scan = ScanFactory(page_count=6)
        columns(scan, 3)
        picture = self._page(scan)
        page = cells((757, 1098, 936, 1134), (864, 1172, 1434, 1243))

        written = bands.trim_rows(scan, {3: page})

        picture.refresh_from_db()
        self.assertEqual(written, 1)
        self.assertEqual(picture.y1, 1153.0)
        self.assertEqual(picture.y0, 172.0)

    def test_a_page_with_no_column_box_reads_the_volume_columns(self):
        # The right column starts at 1172, left of the picture's own
        # middle (841) by nothing but right of the volume's gutter
        # (850): only the neighbour's columns make it a column cell.
        scan = ScanFactory(page_count=6)
        columns(scan, 1)
        picture = self._page(scan)
        page = cells((757, 1098, 936, 1134), (864, 1172, 1434, 1243))

        bands.trim_rows(scan, {3: page})

        picture.refresh_from_db()
        self.assertEqual(picture.y1, 1153.0)

    def test_a_one_column_page_is_not_written(self):
        # One full-measure column box: the page has no two columns, and
        # nothing here reads it.
        scan = ScanFactory(page_count=6)
        columns(scan, 1)
        row(scan, Label.TEXT_COLUMN, 3, (235, 180, 1455, 2000))
        picture = self._page(scan)

        self.assertEqual(bands.trim_rows(scan, {}), 0)
        picture.refresh_from_db()
        self.assertEqual(picture.y1, 2009.0)

    def test_a_key_of_the_wrong_size_cuts_no_picture(self):
        # A key-shaped mark in an exhibit, four times the size of the
        # volume's keys: the pairing drops it, so the trim does, and
        # the card still asks about the box.
        scan = ScanFactory(page_count=9)
        columns(scan, 3)
        picture = row(scan, Label.IMAGE, 3, (232, 172, 1450, 2009))
        # Under 0.90: blackletter trusts a surer key whatever its size.
        row(scan, Label.KEY_ICON, 3, (411, 1233, 1211, 1633), confidence=0.6)
        for page in (5, 6, 7, 8):
            row(scan, Label.KEY_ICON, page, (411, 1233, 661, 1322))

        self.assertEqual(bands.trim_rows(scan, {}), 0)
        picture.refresh_from_db()
        self.assertEqual(picture.y1, 2009.0)

    def test_a_caption_alone_is_left_for_the_card(self):
        scan = ScanFactory(page_count=6)
        columns(scan, 3)
        picture = row(scan, Label.IMAGE, 3, (232, 172, 1450, 1500))
        row(scan, Label.CASE_CAPTION, 3, (500, 600, 1200, 760))

        self.assertEqual(bands.trim_rows(scan, {}), 0)
        picture.refresh_from_db()
        self.assertEqual(picture.y1, 1500.0)

    def test_a_box_a_curator_drew_is_not_written(self):
        scan = ScanFactory(page_count=6)
        columns(scan, 3)
        picture = self._page(scan)
        Detection.objects.filter(pk=picture.pk).update(
            model_name=Detection.ModelName.MANUAL
        )

        self.assertEqual(bands.trim_rows(scan, {}), 0)

    def test_a_picture_in_one_column_is_not_written(self):
        scan = ScanFactory(page_count=6)
        columns(scan, 3)
        picture = row(scan, Label.IMAGE, 3, (240, 172, 840, 2009))
        row(scan, Label.KEY_ICON, 3, (411, 1233, 661, 1322))

        self.assertEqual(bands.trim_rows(scan, {}), 0)
        picture.refresh_from_db()
        self.assertEqual(picture.y1, 2009.0)

    def test_a_second_run_writes_nothing(self):
        scan = ScanFactory(page_count=6)
        columns(scan, 3)
        self._page(scan)
        page = cells((757, 1098, 936, 1134), (864, 1172, 1434, 1243))
        bands.trim_rows(scan, {3: page})

        self.assertEqual(bands.trim_rows(scan, {3: page}), 0)
