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

**The section is the group's own (#399).** The ensemble writes the body
groups of a page before its footnote groups, so a viewer that split
the page by position would look right on every page of today's
documents and go wrong in silence the day the order changes. The page
column draws the footnote zones beside the boxes, and a zone left
over from an older scale is a band in the wrong place.

**The risk is the document's (#419).** ``ensemble.disagreement_level``
is the one rule of the cards, the colours and the badge. A copy of it
in the browser would drift from the cards in silence.
"""

import pathlib
import re

from django.test import SimpleTestCase

from scanning import ensemble, opinion_ocr

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
        self.assertIn("ensemble-footnotes", written, "the block is gone")
        self.assertIn("ensemble-zone", written, "the zone is gone")
        self.assertIn("ensemble-blockquote", written, "the quote is gone")
        self.assertIn("ensemble-quote-zone", written, "the band is gone")
        for name in sorted(written):
            self.assertIn(
                f".{name}",
                styles,
                f"{name} has no rule in checker.css",
            )


class TestTheSectionIsTheGroupsOwn(SimpleTestCase):
    """The viewer splits a page by ``group.section`` (#399)."""

    def test_the_viewer_spells_the_sections_of_the_ensemble(self):
        """A renamed section would put every footnote in the body."""
        source = VIEWER.read_text()
        for name, value in (
            ("BODY", ensemble.BODY),
            ("FOOTNOTES", ensemble.FOOTNOTES),
        ):
            self.assertRegex(source, rf"var {name} = '{re.escape(value)}';")

    def test_the_split_reads_the_group_and_not_the_page_text(self):
        """The page's ``footnotes`` string is text, not a list of groups."""
        source = VIEWER.read_text()
        self.assertIn("group.section === FOOTNOTES", source)
        self.assertNotRegex(source, r"\bpage\.footnotes\b")

    def test_a_footnote_box_has_a_rule(self):
        """The colour is the agreement, so the section needs its own rule."""
        self.assertIn(
            '.ensemble-box[data-section="footnotes"]', STYLES.read_text()
        )


class TestTheZonesGoWithTheBoxes(SimpleTestCase):
    """Every removal of the boxes of a page removes its zones too."""

    def test_no_removal_takes_the_boxes_alone(self):
        """A zone of an older scale would stay on the page."""
        source = VIEWER.read_text()
        self.assertNotRegex(
            source,
            r"querySelectorAll\(\s*'\.ensemble-box'\s*\)\s*"
            r"\.forEach\(function \(el\) \{\s*el\.remove",
        )
        self.assertIn("'.ensemble-box, .ensemble-zone'", source)


class TestTheMarksAreNodes(SimpleTestCase):
    """The formatting is standoff marks, and the viewer builds nodes
    from them (#404); no string of the document becomes HTML."""

    def test_the_viewer_builds_the_marks_and_never_sets_html(self):
        source = VIEWER.read_text()
        self.assertIn("function markedNodes(", source)
        block = source[
            source.index("var KIND_ELEMENTS") : source.index(
                "function hasLevels("
            )
        ]
        self.assertIn("createTextNode", block)
        self.assertIn("textContent", block)
        for name in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(name, block, f"{name} builds a group node")

    def test_every_kind_of_the_ensemble_has_an_element(self):
        source = VIEWER.read_text()
        block = source[source.index("var KIND_ELEMENTS") :]
        block = block[: block.index("};")]
        from scanning import markup

        for kind in markup.BLOCK_KINDS:
            self.assertIn(f"{kind}:", block, f"{kind} has no element")

    def test_a_table_with_no_rows_draws_its_text(self):
        source = VIEWER.read_text()
        self.assertIn("kind === 'table' && (group.table || []).length", source)


class TestTheBlockquoteIsTheDocumentsOwn(SimpleTestCase):
    """The viewer draws the runs the ensemble wrote (#411)."""

    def test_the_viewer_reads_the_runs_of_the_page(self):
        """A run the browser found by itself could differ from the one
        the tagger reads."""
        source = VIEWER.read_text()
        self.assertIn("page.blockquotes", source)
        self.assertIn("run.list_groups", source)
        self.assertIn("document.createElement('blockquote')", source)

    def test_the_quote_is_built_with_the_group_nodes(self):
        """The blockquote holds the group nodes and sets no HTML."""
        source = VIEWER.read_text()
        block = source[
            source.index("function blockquoteNode(") : source.index(
                "function hasLevels("
            )
        ]
        for name in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(name, block)


class TestTheRiskIsTheDocuments(SimpleTestCase):
    """The viewer reads ``level`` and derives no risk (#419)."""

    def test_the_viewer_reads_the_level_of_the_group(self):
        source = VIEWER.read_text()
        self.assertIn("group.level", source)
        self.assertIn("dataset.level", source)

    def test_the_viewer_holds_no_copy_of_the_rule(self):
        """``differs`` was the browser's copy of ``ensemble._differs``;
        the document says it now."""
        source = VIEWER.read_text()
        self.assertNotIn("function differs(", source)
        self.assertNotIn("_differs", source)

    def test_the_viewer_knows_the_schema_that_writes_the_level(self):
        source = VIEWER.read_text()
        match = re.search(r"var LEVEL_SCHEMA = (\d+);", source)
        self.assertIsNotNone(match)
        self.assertLessEqual(int(match.group(1)), ensemble.SCHEMA_VERSION)

    def test_the_stylesheet_draws_both_levels(self):
        styles = STYLES.read_text()
        for level in (ensemble.WARNING, ensemble.BLOCKING):
            self.assertIn(f'[data-level="{level}"]', styles)

    def test_the_stylesheet_draws_the_quiet_boxes_and_the_lock(self):
        styles = STYLES.read_text()
        self.assertIn(".ensemble-quiet", styles)
        self.assertIn(".show-quiet", styles)
        self.assertIn(".ensemble-locked", styles)


class TestTheLockIsReleased(SimpleTestCase):
    """Every single click outside releases the lock (#419)."""

    def listener(self) -> str:
        source = VIEWER.read_text()
        start = source.index("document.addEventListener('click'")
        return source[start : source.index("true);", start) + 6]

    def test_a_keyboard_click_releases_it(self):
        """A click of the keyboard has a detail of 0."""
        self.assertIn("event.detail > 1", self.listener())
        self.assertNotIn("event.detail !== 1", self.listener())

    def test_a_button_that_stops_the_click_releases_it(self):
        """The listener runs in the capture phase, before a badge
        stops the click."""
        self.assertIn("}, true);", self.listener())


class TestTheStorageIsAConvenience(SimpleTestCase):
    """The page works when the browser refuses the storage (#419)."""

    def test_every_storage_call_is_inside_a_try(self):
        source = VIEWER.read_text()
        calls = [
            m.start()
            for m in re.finditer(r"(?:local|session)Storage\.", source)
        ]
        self.assertTrue(calls, "the choice of the quiet boxes is gone")
        self.assertIn("sessionStorage", source, "the place of an edit is gone")
        for at in calls:
            before = source[max(0, at - 80) : at]
            self.assertIn("try {", before, "a storage call outside a try")


class TestTheEditsAreTheLockedBlocks(SimpleTestCase):
    """A write control exists on the locked block alone (#376)."""

    def test_the_toolbar_is_built_by_the_lock_alone(self):
        source = VIEWER.read_text()
        calls = re.findall(r"editBar\(", source)
        # The definition and the one call, in ``lock``.
        self.assertEqual(len(calls), 2)
        lock = source[source.index("function lock(") :]
        lock = lock[: lock.index("\n    }\n")]
        self.assertIn("editBar(", lock)
        self.assertIn("canEdit()", lock)

    def test_the_viewer_reads_the_routes_of_the_page(self):
        source = VIEWER.read_text()
        for name in (
            "editTextUrl",
            "editSectionUrl",
            "editMoveUrl",
            "editWithdrawUrl",
        ):
            self.assertIn(f"endpoint('{name}')", source)
        self.assertNotIn("/edits/", source)

    def test_the_viewer_knows_the_schema_that_holds_the_edits(self):
        source = VIEWER.read_text()
        match = re.search(r"var EDIT_SCHEMA = (\d+);", source)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), ensemble.SCHEMA_VERSION)

    def test_the_edit_reads_the_level_and_derives_none(self):
        source = VIEWER.read_text()
        bar = source[source.index("function fillBar(") :]
        bar = bar[: bar.index("\n    }\n")]
        self.assertIn("group.level", bar)
        self.assertNotIn("agreement", bar)
