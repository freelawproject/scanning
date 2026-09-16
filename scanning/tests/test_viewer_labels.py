"""Pins for the viewer's copy of blackletter's label taxonomy (#343).

``viewer_step2.js`` carries four label tables written by hand: the
name-to-id table the drawer posts with, the menu of the Add Detection
popup, the overlay colours, and the filter that decides which labels
are drawn at all. Nothing derived them from
``blackletter.models.Label``, so bl-warm's ``heading`` and
``blockquote`` classes reached the rows and stopped at the browser.

Each table hides a row in its own way. A name the menu offers with no
id is posted as ``label_id: -1`` and refused; a label the filter does
not name is held back from the page with no message at all.

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


def viewer_colours() -> dict[str, str]:
    """Return the ``LABEL_COLORS`` table.

    :returns: ``{label name: hex colour}`` as the overlay holds it.
    :rtype: dict[str, str]
    """
    return dict(
        re.findall(
            r"([A-Z_]+):\s*'(#[0-9a-fA-F]{6})'",
            _literal("LABEL_COLORS", "{", "}"),
        )
    )


def viewer_used_labels() -> list[str]:
    """Return the labels ``USED_LABELS`` lets the overlay draw.

    :returns: The names, in table order.
    :rtype: list[str]
    """
    return re.findall(r"([A-Z_]+):\s*true", _literal("USED_LABELS", "{", "}"))


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

    def test_every_name_the_menu_offers_is_drawn(self):
        """The filter one layer up. A label the drawer can add but the
        overlay does not name is held back from the page with no
        message, which is the failure of #343 a layer up."""
        drawn = viewer_used_labels()

        for name in viewer_menu_labels():
            with self.subTest(label=name):
                self.assertIn(name, drawn)

    def test_every_drawn_label_is_a_real_label(self):
        """A name the enum does not carry draws nothing and says
        nothing: no row can ever hold it."""
        for name in viewer_used_labels():
            with self.subTest(label=name):
                self.assertIn(name, Label.__members__)

    def test_every_colour_names_a_label(self):
        """``EDGES`` is the one key that names no detection label."""
        for name in viewer_colours():
            with self.subTest(label=name):
                self.assertTrue(
                    name in NOT_A_LABEL or name in Label.__members__
                )

    def test_the_two_new_labels_have_a_colour_no_other_label_holds(self):
        """Not a rule for the whole table: ``HEADNOTE`` and
        ``HEADNOTE_BRACKET`` share one on purpose, being a headnote and
        its bracket. These two belong to no such family, so a shared
        colour would only make them unreadable."""
        colours = viewer_colours()

        for name in ("HEADING", "BLOCKQUOTE"):
            with self.subTest(label=name):
                self.assertIn(name, colours)
                others = [
                    value for key, value in colours.items() if key != name
                ]
                self.assertNotIn(colours[name], others)


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
