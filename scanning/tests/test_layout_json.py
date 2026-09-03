"""Tests for the layout JSON repair (issue #242).

The fixtures have the shapes of the four measured pages: a lone
quotation mark inside a string (scans 2726 and 2665), a lone backslash
where an escaped quotation mark belongs (scan 2702), and a doubled
closer (scan 2705). The rest are the refusals: what the arms must not
turn into cells.
"""

import json

from django.test import SimpleTestCase

from scanning import layout_json

HEADER = {
    "bbox": [276, 93, 426, 129],
    "category": "Page-header",
    "text": "878 N. C.",
}


def _array(*texts: str) -> str:
    """Build a valid layout array with one ``Text`` cell per string."""
    cells = [HEADER]
    for index, text in enumerate(texts):
        cells.append(
            {
                "bbox": [100, 200 + index * 100, 900, 280 + index * 100],
                "category": "Text",
                "text": text,
            }
        )
    return json.dumps(cells)


class TestRepair(SimpleTestCase):
    def test_a_valid_answer_needs_no_edit(self):
        result = layout_json.repair(_array("plain text"))
        self.assertEqual(result.edits, [])
        self.assertIsNone(result.fault)
        self.assertEqual(result.cells[0], HEADER)

    def test_a_lone_quotation_mark_is_escaped(self):
        # Scan 2726 page 44: the opening mark was escaped, the closing
        # one was not, and the string closed early.
        good = _array(
            'her husband\'s death "almost took [her] out[.]" and she felt'
        )
        broken = good.replace('out[.]\\"', 'out[.]"')
        self.assertNotEqual(good, broken)

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 1)
        self.assertTrue(result.edits[0].startswith("escape_quote@"))
        self.assertIsNone(result.fault)

    def test_a_lone_backslash_gets_its_quotation_mark(self):
        # Scan 2702 page 32: the model wrote ``\)))`` for ``\")))``.
        good = _array('at "14.1 million" to "18.2 million adults")))."')
        broken = good.replace('adults\\"', "adults\\")
        self.assertNotEqual(good, broken)

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 1)
        self.assertTrue(result.edits[0].startswith("restore_quote@"))

    def test_a_doubled_closer_is_cut(self):
        # Scan 2705 page 72: the array closed, and the model wrote the
        # last three characters again.
        good = _array("his territory either")
        broken = good + '"}]'

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(result.edits, [f"cut_extra@{len(good)}"])

    def test_two_faults_take_two_edits(self):
        good = _array('she said "no" twice', 'and "yes" once')
        broken = good.replace('no\\"', 'no"').replace('yes\\"', 'yes"')

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 2)

    def test_the_edits_run_out(self):
        good = _array('"a" b', '"c" d', '"e" f', '"g" h')
        broken = good.replace('\\" ', '" ')

        result = layout_json.repair(broken, max_edits=3)

        self.assertIsNone(result.cells)
        self.assertEqual(len(result.edits), 3)
        self.assertIn("edits spent", result.fault)

    def test_a_truncated_array_is_not_repaired(self):
        # The truncation of #238 is another fault with another fix.
        broken = _array("some text")[:-20]

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(result.edits, [])
        self.assertIn("no arm", result.fault)
        self.assertIn(">>", result.fault)

    def test_a_delimiter_fault_with_no_quotation_mark_behind_it(self):
        # Two objects with no comma between them: the message matches
        # the first arm, the text does not, and nothing is invented.
        broken = _array("a").replace("}, {", "} {")

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(result.edits, [])

    def test_an_escaped_quotation_mark_is_left_alone(self):
        # ``\\"`` is a backslash then a quotation mark that really
        # closes the string; escaping it again would swallow the rest.
        broken = (
            '[{"bbox": [1, 1, 2, 2], "category": "Text", "text": "a\\\\" x}]'
        )

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)

    def test_a_repair_that_gives_an_object_is_refused(self):
        result = layout_json.repair('{"bbox": [1, 1, 2, 2]}"}')
        self.assertIsNone(result.cells)
        self.assertIn("not an array", result.fault)

    def test_a_three_coordinate_bbox_is_refused(self):
        result = layout_json.repair(
            '[{"bbox": [1, 1, 2], "category": "Text", "text": "x"}]'
        )
        self.assertIsNone(result.cells)
        self.assertIn("four numbers", result.fault)

    def test_an_illegal_bbox_is_refused(self):
        result = layout_json.repair(
            '[{"bbox": [5, 1, 2, 2], "category": "Text", "text": "x"}]'
        )
        self.assertIsNone(result.cells)
        self.assertIn("illegal bbox", result.fault)

    def test_a_cell_without_a_category_is_refused(self):
        result = layout_json.repair('[{"bbox": [1, 1, 2, 2], "text": "x"}]')
        self.assertIsNone(result.cells)
        self.assertIn("no category", result.fault)

    def test_a_picture_cell_needs_no_text(self):
        result = layout_json.repair(
            '[{"bbox": [1, 1, 2, 2], "category": "Picture"}]'
        )
        self.assertEqual(len(result.cells), 1)


class TestRescale(SimpleTestCase):
    def test_the_arithmetic_matches_upstream(self):
        # ``int(float(v) / (input / origin))`` per axis, as upstream's
        # ``post_process_cells`` computes it.
        cells = [{"bbox": [323, 143, 364, 177], "category": "Page-header"}]

        out = layout_json.rescale(cells, 1024, 1536, 1700, 2200)

        scale_x, scale_y = 1024 / 1700, 1536 / 2200
        self.assertEqual(
            out[0]["bbox"],
            [
                int(323 / scale_x),
                int(143 / scale_y),
                int(364 / scale_x),
                int(177 / scale_y),
            ],
        )
        self.assertEqual(out[0]["category"], "Page-header")
        # A copy, not the input.
        self.assertEqual(cells[0]["bbox"], [323, 143, 364, 177])


class TestExcerpt(SimpleTestCase):
    def test_the_fault_is_marked_and_newlines_are_flattened(self):
        text = "abc\ndef" * 20
        out = layout_json.excerpt(text, 70, radius=5)
        self.assertIn(">>", out)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 12 + 2 + 2)
