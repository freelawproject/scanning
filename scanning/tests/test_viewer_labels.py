"""Pins for the viewer's copy of blackletter's label taxonomy (#343).

``viewer_step2.js`` carries three label tables written by hand: the
name-to-id table the drawer posts with, the menu of the Add Detection
popup, and the overlay colours. Nothing derived them from
``blackletter.models.Label``, so bl-warm's ``heading`` and
``blockquote`` classes reached the rows and stopped at the browser.

The same silence cost the corpus its boxes once already: before
blackletter 0.4.1 the adapter had no ``Label`` for either name and
dropped them inside the worker, with no log line. These tests are the
scanning-side twin of blackletter's ``tests/test_bl_warm.py``: the
next checkpoint's new class fails a test here rather than being lost
again.
"""

import pathlib
import re

from blackletter.models import Label

from scanning.models import Detection
from scanning.tests.test_views import DetectionEndpointMixin, ScanningTestCase

VIEWER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static"
    / "scanning"
    / "viewer_step2.js"
)

#: Keys of ``LABEL_COLORS`` that name no detection label. The overlay
#: draws the page edges in the same table, and no id is posted for it.
NOT_A_LABEL = frozenset({"EDGES"})


def _literal(name: str, opener: str, closer: str) -> str:
    """Return the source text of one object or array literal.

    :param name: The variable name, e.g. ``LABEL_IDS``.
    :param opener: The character the literal opens with.
    :param closer: The character it closes with.
    :returns: Everything between the two, exclusive.
    :rtype: str
    :raises AssertionError: If the viewer declares it other than once.
    """
    source = VIEWER.read_text()
    opens = source.count(f"var {name} = {opener}")
    assert opens == 1, f"{name} is declared {opens} times"
    body = source.split(f"var {name} = {opener}", 1)[1]
    return body.split(f"{closer};", 1)[0]


def viewer_label_ids() -> dict[str, int]:
    """Return the viewer's ``LABEL_IDS`` table.

    :returns: ``{label name: id}`` as the browser holds it.
    :rtype: dict[str, int]
    """
    return {
        name: int(value)
        for name, value in re.findall(
            r"([A-Z_]+):\s*(\d+)", _literal("LABEL_IDS", "{", "}")
        )
    }


def viewer_menu_labels() -> list[str]:
    """Return the label names the Add Detection popup offers.

    :returns: The names, in menu order.
    :rtype: list[str]
    """
    return re.findall(r"'([A-Z_]+)'", _literal("labelOpts", "[", "]"))


def viewer_colour_labels() -> list[str]:
    """Return the label names ``LABEL_COLORS`` gives a colour.

    :returns: The names, in table order.
    :rtype: list[str]
    """
    return re.findall(r"([A-Z_]+):\s*'#", _literal("LABEL_COLORS", "{", "}"))


class TestViewerLabelIds(ScanningTestCase):
    """``LABEL_IDS`` is the browser's copy of ``Label`` (#343)."""

    def test_the_table_matches_the_enum_in_both_directions(self):
        """Both directions, or a class the next checkpoint adds is
        dropped at the browser with nothing to say so."""
        self.assertEqual(
            viewer_label_ids(),
            {label.name: int(label) for label in Label},
        )

    def test_every_name_the_menu_offers_has_an_id(self):
        """A name with no id is posted as ``label_id: -1``, which the
        server answers 400. The menu can never run ahead of the table."""
        ids = viewer_label_ids()

        for name in viewer_menu_labels():
            with self.subTest(label=name):
                self.assertIn(name, ids)

    def test_the_menu_offers_the_two_opinion_body_labels(self):
        """The point of the issue: a curator draws the heading or the
        block quote the model missed."""
        menu = viewer_menu_labels()

        self.assertIn("HEADING", menu)
        self.assertIn("BLOCKQUOTE", menu)

    def test_every_drawable_label_has_a_colour_of_its_own(self):
        """A label with no colour draws grey, and two labels that share
        one are indistinguishable on the page."""
        colours = dict(
            re.findall(
                r"([A-Z_]+):\s*'(#[0-9a-f]{6})'",
                _literal("LABEL_COLORS", "{", "}"),
            )
        )
        for name in viewer_colour_labels():
            with self.subTest(label=name):
                self.assertTrue(
                    name in NOT_A_LABEL or name in Label.__members__
                )
        self.assertIn("HEADING", colours)
        self.assertIn("BLOCKQUOTE", colours)
        self.assertNotEqual(colours["HEADING"], colours["BLOCKQUOTE"])
        self.assertNotIn(
            colours["HEADING"],
            [v for k, v in colours.items() if k != "HEADING"],
        )
        self.assertNotIn(
            colours["BLOCKQUOTE"],
            [v for k, v in colours.items() if k != "BLOCKQUOTE"],
        )


class TestDrawTheOpinionBodyLabels(DetectionEndpointMixin, ScanningTestCase):
    """The drawer's two new ids, through the endpoint (#343)."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def test_a_drawn_heading_and_block_quote_are_written(self):
        """The view derives the name from the id, so the row carries
        the name the overlay keys its colour by."""
        scan, _ = self._make_scan_with_detection()

        for label in (Label.HEADING, Label.BLOCKQUOTE):
            with self.subTest(label=label.name):
                response = self._post(
                    "add_single_detection",
                    scan,
                    {
                        "page_index": 0,
                        "label_id": int(label),
                        "bbox": [300.0, 300.0 + int(label), 400.0, 380.0],
                        "img_width": 1200,
                        "img_height": 1600,
                    },
                )

                self.assertEqual(response.status_code, 200)
                row = Detection.objects.get(pk=response.json()["detection_id"])
                self.assertEqual(row.label, label.name)
                self.assertEqual(row.label_id, int(label))
                self.assertEqual(row.model_name, Detection.ModelName.MANUAL)

    def test_an_id_no_label_carries_is_refused(self):
        """The guard the drawer relies on: a menu name with no id posts
        ``-1``, and nothing is written."""
        scan, _ = self._make_scan_with_detection()
        before = Detection.objects.count()

        with self.assertLogs("scanning.views_api", level="WARNING"):
            response = self._post(
                "add_single_detection",
                scan,
                {
                    "page_index": 0,
                    "label_id": len(Label),
                    "bbox": [300.0, 300.0, 400.0, 380.0],
                    "img_width": 1200,
                    "img_height": 1600,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Detection.objects.count(), before)
