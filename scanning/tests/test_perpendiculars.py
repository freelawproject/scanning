"""The perpendiculars: West's inverted-T page marks, found on a page
image and painted out before an engine reads it.

A synthetic page at 200 dpi: a bar with a stem inside its left half
and three digit-sized blobs after it, a word of tall letters beyond,
and an underlined word elsewhere, whose bar has its I at the very left
end and is no mark.
"""

import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from scanning import perpendiculars

DPI = 200
W, H = 1700, 2200


def page_with_mark(bar=(600, 1000, 50, 6), stem_in=12, word_at=None):
    """A white page with one perpendicular and, optionally, a word."""
    img = np.full((H, W), 255, np.uint8)
    x, y, w, h = bar
    img[y : y + h, x : x + w] = 0  # the bar
    img[y - 24 : y, x + stem_in : x + stem_in + 3] = 0  # the stem
    for i in range(3):  # three digits after the stem, 8 by 18 px, 3 apart
        dx = x + stem_in + 8 + i * 11
        img[y - 18 : y, dx : dx + 8] = 0
    if word_at is not None:  # a word of tall letters, 30 px high
        for i in range(4):
            img[y - 30 : y, word_at + i * 12 : word_at + i * 12 + 8] = 0
    return img


def ink(area: np.ndarray) -> int:
    return int((area < 128).sum())


class TestFind(SimpleTestCase):
    def test_a_bar_with_a_stem_inside_its_left_half_is_a_mark(self):
        found = perpendiculars.find(Image.fromarray(page_with_mark()), DPI)

        self.assertEqual(len(found), 1)
        x, y, w, h, stem = found[0]
        # The opening's kernel anchors a pixel in: a box, not corners.
        self.assertTrue(abs(x - 600) <= 1 and abs(y - 1000) <= 1, (x, y))
        self.assertEqual((w, h), (50, 6))
        self.assertTrue(10 <= stem <= 14, stem)

    def test_an_underline_with_its_letter_at_the_end_is_no_mark(self):
        img = np.full((H, W), 255, np.uint8)
        img[1000:1006, 600:650] = 0  # the underline
        img[976:1000, 600:603] = 0  # the I of ``Inc.`` at its very end

        self.assertEqual(perpendiculars.find(Image.fromarray(img), DPI), [])

    def test_a_blank_page_has_none(self):
        blank = Image.new("RGB", (W, H), "white")

        self.assertEqual(perpendiculars.find(blank, DPI), [])


class TestPaint(SimpleTestCase):
    def test_the_mark_and_its_digits_are_painted_and_the_word_stays(self):
        img = page_with_mark(word_at=700)
        painted, marks = perpendiculars.paint(Image.fromarray(img), DPI)

        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0][2:4], [50, 6])
        out = np.asarray(painted.convert("L"))
        self.assertEqual(ink(out[970:1010, 590:660]), 0, "the T stays")
        self.assertEqual(ink(out[980:1001, 618:652]), 0, "the digits stay")
        self.assertEqual(
            ink(out[968:1001, 698:746]),
            ink(img[968:1001, 698:746]),
            "the word",
        )

    def test_a_page_with_no_mark_comes_back_as_it_was(self):
        blank = Image.new("RGB", (W, H), "white")
        painted, marks = perpendiculars.paint(blank, DPI)

        self.assertEqual(marks, [])
        self.assertIs(painted, blank)
