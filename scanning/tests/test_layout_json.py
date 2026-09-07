"""Tests for the layout JSON repair (issue #242).

The fixtures have the shapes of the four measured pages: a lone
quotation mark inside a string (scans 2726 and 2665), a lone backslash
where an escaped quotation mark belongs (scan 2702), and a doubled
closer (scan 2705). The rest are the refusals: what the arms must not
turn into cells.

:class:`TestRelaxedControlCharacters` covers the fourth shape, measured
on 22 pages by the corpus survey of issue #268: a raw control character
inside a string, answered by a parse mode rather than by an arm.
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

    def test_a_comma_after_the_stray_quotation_mark_is_repaired(self):
        # The same fault as scan 2726, with a comma where that page had
        # a space. The parser then takes the comma as the member
        # separator and asks for a key, so the arm has to step back
        # over it. A comma after a quotation is ordinary in an opinion,
        # so without this the arm reached only half of shape 1.
        good = _array('her death "almost took [her] out[.]", and she felt')
        broken = good.replace('out[.]\\"', 'out[.]"')
        self.assertNotEqual(good, broken)

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 1)
        self.assertTrue(result.edits[0].startswith("escape_quote@"))

    def test_a_comma_after_it_and_another_member_behind(self):
        # The stray quotation mark closes the *last* member's value, so
        # the parser reads the object's own closing brace as the key it
        # wanted. Same arm, same one edit.
        good = json.dumps(
            [
                {
                    "category": "Text",
                    "text": 'she said "no", and left',
                    "bbox": [100, 200, 900, 280],
                }
            ]
        )
        broken = good.replace('no\\"', 'no"')

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 1)

    def test_a_stray_quotation_mark_inside_a_key_is_repaired(self):
        # ``Expecting ':' delimiter``: the third early-close message,
        # and the only one that names a key rather than a value.
        good = json.dumps(
            [
                {
                    "bbox": [100, 200, 900, 280],
                    "category": "Text",
                    'te"xt': "x",
                }
            ]
        )
        broken = good.replace('te\\"xt', 'te"xt')

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 1)

    def test_a_doubled_comma_is_refused(self):
        # The message matches the arm and the text does not: behind the
        # comma the walk finds another comma, not a quotation mark.
        broken = _array("a").replace('], "category"', '],, "category"', 1)
        self.assertNotEqual(broken, _array("a"))

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(result.edits, [])

    def test_a_comma_before_the_first_key_is_refused(self):
        broken = _array("a").replace("[{", "[{,", 1)

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(result.edits, [])

    def test_an_unquoted_key_is_refused(self):
        broken = _array("a").replace('"category": "Page-header"', "cat: 1", 1)
        self.assertNotEqual(broken, _array("a"))

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(result.edits, [])

    def test_a_missing_comma_between_objects_is_refused(self):
        # The nearest non-space character behind the fault is a closing
        # brace, so nothing is escaped and the page stays filtered.
        broken = _array("a").replace("}, {", "} {", 1)
        self.assertNotEqual(broken, _array("a"))

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(result.edits, [])

    def test_an_empty_array_is_refused(self):
        # Upstream refuses one too (``post_process_cells`` asserts a
        # first cell), and a page called repaired with no cell would
        # leave ``filtered_pages`` while still offering the reader
        # nothing.
        for raw in ("[]", '[]"}]'):
            with self.subTest(raw=raw):
                result = layout_json.repair(raw)
                self.assertIsNone(result.cells)
                self.assertIn("no cell", result.fault)

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


class TestRelaxedControlCharacters(SimpleTestCase):
    """The fourth shape (issue #268): a line break inside a string.

    The model copies the line break of the printed page into a ``text``
    value in place of ``\\n``. Every fixture here has the shape of a
    measured page: the break falls at a quotation that ends a line.
    """

    def test_a_raw_line_break_is_read(self):
        # Scan 2816 page 21: "(brackets in *Sills*<break><break>))".
        good = _array(
            "the only thing you have left of [Richard]?\n Julee said"
        )
        broken = good.replace("\\n", "\n")
        self.assertNotEqual(good, broken)

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 1)
        self.assertTrue(result.edits[0].startswith("relax_controls@"))
        self.assertIsNone(result.fault)

    def test_the_character_survives_in_the_value(self):
        # The repair keeps the model's text. It moves no character and
        # drops none: only the parse mode changes.
        broken = _array("who is it\n\n)); see also").replace("\\n", "\n")

        result = layout_json.repair(broken)

        self.assertEqual(result.cells[1]["text"], "who is it\n\n)); see also")

    def test_a_doubled_break_costs_one_edit(self):
        # An arm that escaped one character each would spend two of the
        # three edits here, and six of the 22 measured pages carry a
        # doubled break.
        broken = _array("(brackets in *Sills*\n\n)); see also *id.*").replace(
            "\\n", "\n"
        )

        result = layout_json.repair(broken)

        self.assertEqual(len(result.edits), 1)
        self.assertIsNotNone(result.cells)

    def test_every_break_of_the_page_is_read_by_the_one_edit(self):
        # The mode holds for the rest of the parse, so a page with
        # breaks in several cells still costs one edit.
        broken = _array("first\ncell", "second\ncell", "third\ncell").replace(
            "\\n", "\n"
        )

        result = layout_json.repair(broken)

        self.assertEqual(len(result.edits), 1)
        self.assertEqual(result.cells[1]["text"], "first\ncell")
        self.assertEqual(result.cells[3]["text"], "third\ncell")

    def test_a_stray_quotation_mark_is_still_escaped_beside_it(self):
        # The mode reaches the control characters only. The arms keep
        # answering everything else, on the same page.
        good = _array('he said "no" to it\nand left')
        broken = good.replace('no\\"', 'no"').replace("\\n", "\n")

        result = layout_json.repair(broken)

        self.assertEqual(json.dumps(result.cells), good)
        self.assertEqual(len(result.edits), 2)
        self.assertEqual(result.edits[0].split("@")[0], "escape_quote")
        self.assertEqual(result.edits[1].split("@")[0], "relax_controls")

    def test_a_relaxed_parse_that_is_not_an_array_is_refused(self):
        # ``_check_cells`` is still the guard: a page is never called
        # repaired unless it came out a layout array.
        broken = '{"category": "Text", "text": "a\nb"}'

        result = layout_json.repair(broken)

        self.assertIsNone(result.cells)
        self.assertEqual(len(result.edits), 1)
        self.assertIn("not an array", result.fault)

    def test_the_mode_does_not_leak_to_the_next_call(self):
        # The flag is local to one call. A later answer that is
        # genuinely broken must not be read as if it were relaxed.
        layout_json.repair(_array("a\nb").replace("\\n", "\n"))

        result = layout_json.repair(_array("plain text"))

        self.assertEqual(result.edits, [])
        self.assertIsNone(result.fault)

    def test_the_fault_line_does_not_double_the_word_at(self):
        # The parser's own message ends in "at", and this line is the
        # whole triage path for a shape no arm reaches.
        broken = _array("a\nb").replace("\\n", "\n")

        result = layout_json.repair(broken, max_edits=0)

        self.assertIsNone(result.cells)
        self.assertIn("Invalid control character at char ", result.fault)
        self.assertNotIn("at at", result.fault)
