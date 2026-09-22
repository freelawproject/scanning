"""Pins for the text review's viewer (#380).

``viewer_step3.js`` marks the words of one engine's reading that the
shown reading does not hold. The rule of that mark lives in the
browser, and this project runs no test there. So these tests pin the
two ways the rule can go wrong in silence.

**The mark is not the vote.** ``ensemble.compare_text`` folds the
quotes, the dashes and the markdown marks, because those are not
differences of reading. The mark answers the other question: where
must the eye go? A curly quote against a straight one is what the
reviewer came to see. A later reader who copies ``compare_word`` into
the viewer would take that away, and nothing on the page would say so.

**A class with no rule draws nothing.** The viewer writes the marks as
spans. A class the stylesheet does not name leaves the words plain,
and the panel looks like a panel with no difference in it.
"""

import pathlib
import re

from django.test import SimpleTestCase

from scanning import opinion_ocr

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static" / "scanning"
VIEWER = STATIC / "viewer_step3.js"
STYLES = STATIC / "checker.css"

#: How the viewer writes a class. An id of the template is not one of
#: these, and Tailwind draws the ids.
CLASS_WRITE = (
    r"(?:className\s*=\s*|classList\.(?:add|remove|contains)\()"
    r"['\"]([^'\"]+)['\"]"
)

#: What a copy of ``ensemble.compare_word`` would bring with it: the
#: Unicode fold, and a table of the characters the vote calls the same.
NORMALIZERS = ("normalize(", "NFKC", "\\u2018", "\\u201c", "\\u2013")


class TestTheMarkIsTheTextAsItIsShown(SimpleTestCase):
    """The viewer compares the readings word by word and folds none."""

    def test_the_viewer_normalizes_nothing(self):
        """No fold of the vote reaches the browser."""
        source = VIEWER.read_text()
        for name in NORMALIZERS:
            self.assertNotIn(
                name,
                source,
                f"{name} is in viewer_step3.js. The mark compares the "
                "text as it is shown, and ensemble.compare_word is the "
                "rule of the vote alone.",
            )


class TestTheViewerNamesNoEngine(SimpleTestCase):
    """The rank comes from the document, never from the browser."""

    def test_no_engine_name_is_in_the_viewer(self):
        """A fourth engine costs the viewer no change (#368)."""
        source = VIEWER.read_text()
        for name in opinion_ocr.ENGINES:
            self.assertNotIn(
                name,
                source,
                f"{name} is in viewer_step3.js. The order of the "
                "engines of the page is the rank, and the document "
                "carries it.",
            )


class TestEveryPanelClassHasARule(SimpleTestCase):
    """A class the viewer writes is a class the stylesheet draws."""

    def test_the_stylesheet_names_every_ensemble_class(self):
        """A mark with no rule leaves the words plain."""
        source = VIEWER.read_text()
        styles = STYLES.read_text()
        written = {
            name
            for value in re.findall(CLASS_WRITE, source)
            for name in value.split()
            if name.startswith("ensemble-")
        }
        self.assertIn("ensemble-diff", written, "the mark is gone")
        self.assertIn("ensemble-why", written, "the reason line is gone")
        for name in sorted(written):
            self.assertIn(
                f".{name}",
                styles,
                f"{name} has no rule in checker.css",
            )
