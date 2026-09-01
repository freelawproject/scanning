"""Tests for the grouping of compound page issues (issue #227).

Pure-function tests: the inputs come from the same blackletter
functions the production funnel uses (``build_analysis`` and
``build_issues``), so the tests break when either side of the
contract moves. The funnel itself (``recalculate_issues`` writes
grouped ``Issue`` rows) is covered in ``test_services.py``; the
dismissal fan-out lives in ``test_page_edits.py``.
"""

from collections.abc import Sequence

from blackletter.validate import build_analysis, build_issues
from django.test import SimpleTestCase

from scanning.issue_grouping import group_issues


def make_ocr(values: Sequence[str | None]) -> list[dict]:
    """Build OCR result dicts for one page per value.

    :param values: One detected value per page, in page order.
        ``None`` is a page without a number.
    :returns: The dicts ``build_analysis`` reads.
    :rtype: list[dict]
    """
    return [
        {
            "pdf_page": index + 1,
            "detected": value,
            "type": "single" if value else None,
        }
        for index, value in enumerate(values)
    ]


def grouped_issues(
    values: Sequence[str | None],
    exp_start: int | None = None,
    exp_end: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Run the production funnel over one synthetic volume.

    :param values: One detected value per page, in page order.
    :param exp_start: Expected first printed number, or ``None``.
    :param exp_end: Expected last printed number, or ``None``.
    :returns: ``(grouped, raw)`` issue lists.
    :rtype: tuple[list[dict], list[dict]]
    """
    ocr = make_ocr(values)
    analysis = build_analysis(ocr, exp_start, exp_end)
    result = build_issues(
        analysis, len(ocr), exp_start=exp_start, exp_end=exp_end
    )
    return group_issues(result["issues"], analysis), result["issues"]


def by_check(issues: list[dict], check: str) -> list[dict]:
    """Filter one issue list by check name.

    :param issues: The issue dicts.
    :param check: The check name to keep.
    :returns: The matching dicts, in order.
    :rtype: list[dict]
    """
    return [i for i in issues if i["check_name"] == check]


class TestStrayNumberGrouping(SimpleTestCase):
    """One stray number makes one card per fact, not one per pair.

    The screenshot-1 shape of #227: a stray "81" read between the
    real pages 84, 86 and 88 makes the overlapping gaps 81->84,
    81->86 and 81->88.
    """

    VALUES = ["81", "84", "81", "86", "81", "88"]

    def test_one_missing_card_per_number(self):
        grouped, raw = grouped_issues(self.VALUES)

        raw_missing = by_check(raw, "missing_page")
        self.assertGreater(len(raw_missing), 6)
        missing = by_check(grouped, "missing_page")
        self.assertEqual(
            sorted(i["page_number"] for i in missing), [82, 83, 85, 87]
        )

    def test_the_message_names_the_tightest_gap(self):
        grouped, _ = grouped_issues(self.VALUES)

        card = next(
            i
            for i in by_check(grouped, "missing_page")
            if i["page_number"] == 83
        )
        self.assertIn("gap between 81 and 84", card["message"])
        self.assertIn("Reported by 3 gaps", card["message"])
        self.assertEqual(
            card["metadata"]["gaps"], [[81, 84], [81, 86], [81, 88]]
        )

    def test_a_detected_number_gets_no_missing_card(self):
        # 84 and 86 sit inside the wider gaps, but the volume shows
        # both, so a "missing" card for them is noise.
        grouped, raw = grouped_issues(self.VALUES)

        raw_numbers = {i["page_number"] for i in by_check(raw, "missing_page")}
        self.assertIn(84, raw_numbers)
        grouped_numbers = {
            i["page_number"] for i in by_check(grouped, "missing_page")
        }
        self.assertNotIn(84, grouped_numbers)
        self.assertNotIn(86, grouped_numbers)

    def test_one_backward_card_with_every_page(self):
        grouped, raw = grouped_issues(self.VALUES)

        self.assertEqual(len(by_check(raw, "backward_page")), 2)
        backward = by_check(grouped, "backward_page")
        self.assertEqual(len(backward), 1)
        card = backward[0]
        self.assertEqual(card["page_number"], 81)
        self.assertEqual(card["metadata"]["pdf_pages"], [3, 5])
        self.assertIn("PDF pages 3 and 5", card["message"])

    def test_one_duplicate_card_with_the_bracket_list(self):
        grouped, _ = grouped_issues(self.VALUES)

        duplicates = by_check(grouped, "duplicate_page")
        self.assertEqual(len(duplicates), 1)
        card = duplicates[0]
        self.assertEqual(card["page_number"], 81)
        # deleteDuplicates in the viewer parses the bracket list.
        self.assertIn("[1, 3, 5]", card["message"])
        self.assertEqual(card["metadata"]["pdf_pages"], [1, 3, 5])


class TestMissingNumberMeetsItsBlankPage(SimpleTestCase):
    """The screenshot-3 shape of #227: one fact, one card."""

    def test_the_two_cards_merge(self):
        grouped, raw = grouped_issues(["79", None, "81"])

        self.assertEqual(len(by_check(raw, "missing_page")), 1)
        self.assertEqual(len(by_check(raw, "no_page_number")), 1)

        missing = by_check(grouped, "missing_page")
        self.assertEqual(len(missing), 1)
        card = missing[0]
        self.assertEqual(card["page_number"], 80)
        self.assertIn("PDF page 2", card["message"])
        self.assertIn("no detected number", card["message"])
        self.assertEqual(card["metadata"]["pdf_pages"], [2])
        self.assertEqual(by_check(grouped, "no_page_number"), [])

    def test_no_merge_when_the_neighbors_disagree(self):
        # The blank page interpolates to 80 from the left and to 81
        # from the right, so it names no number: both cards stay.
        grouped, _ = grouped_issues(["79", None, "82"])

        self.assertEqual(len(by_check(grouped, "no_page_number")), 1)
        for card in by_check(grouped, "missing_page"):
            self.assertNotIn("pdf_pages", card.get("metadata", {}))

    def test_no_merge_when_the_run_does_not_fit(self):
        # Two blank pages, one missing number: neither blank page
        # interpolates consistently, so the merge stays out and both
        # info cards stay.
        grouped, _ = grouped_issues(["79", None, None, "81"])

        self.assertEqual(len(by_check(grouped, "no_page_number")), 2)


class TestStrayValueGrouping(SimpleTestCase):
    """A repeated out-of-range value makes one card, not one per page."""

    def test_the_pages_share_one_card(self):
        grouped, raw = grouped_issues(
            ["1", "2", "1881", "4", "1881", "6"], exp_start=1, exp_end=6
        )

        self.assertEqual(len(by_check(raw, "suspicious_reading")), 2)
        suspicious = by_check(grouped, "suspicious_reading")
        self.assertEqual(len(suspicious), 1)
        card = suspicious[0]
        self.assertEqual(card["page_number"], 3)
        self.assertIn("PDF pages 3 and 5", card["message"])
        self.assertIn("'1881'", card["message"])
        self.assertEqual(card["metadata"]["pdf_pages"], [3, 5])

    def test_a_lone_stray_keeps_its_card(self):
        grouped, raw = grouped_issues(
            ["1", "2", "1881", "4"], exp_start=1, exp_end=4
        )

        self.assertEqual(
            by_check(grouped, "suspicious_reading"),
            by_check(raw, "suspicious_reading"),
        )


class TestLargeGapGrouping(SimpleTestCase):
    """Overlapping large gaps from one stray anchor collapse."""

    def test_one_card_per_span(self):
        grouped, raw = grouped_issues(["10", "30", "10", "40"])

        self.assertGreater(len(by_check(raw, "large_gap")), 2)
        pages = [i["page_number"] for i in by_check(grouped, "large_gap")]
        self.assertEqual(len(pages), len(set(pages)))
        self.assertEqual(
            len([p for p in pages if p == 10]),
            1,
        )


class TestPassThrough(SimpleTestCase):
    """A card the pass does not recognize passes through unchanged."""

    def test_unknown_checks_and_single_cards_survive(self):
        ocr = make_ocr(["1", "2", "4"])
        analysis = build_analysis(ocr, None, None)
        result = build_issues(analysis, len(ocr))
        flag = {
            "page_number": 2,
            "check_name": "process_flag",
            "severity": "warning",
            "message": "flagged by a curator",
        }
        issues = result["issues"] + [flag]

        grouped = group_issues(issues, analysis)

        self.assertIn(flag, grouped)
        missing = by_check(grouped, "missing_page")
        self.assertEqual(missing, by_check(result["issues"], "missing_page"))
