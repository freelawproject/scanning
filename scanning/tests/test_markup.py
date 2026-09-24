"""Tests for the markup parse of the OCR engines (#404).

The fixtures are lines of the survey documents (the comment of
2026-09-24 on #404): a dots.mocr cell, a Mistral block, a Surya block,
each as the engine wrote it.
"""

import random

from django.test import SimpleTestCase

from scanning import markup
from scanning.markup import EM, STRONG, SUP, Mark, Parsed


def marked(parsed: Parsed) -> list[tuple[str, str]]:
    """The marks as ``(kind, the text they cover)``, in order."""
    return [(m.kind, parsed.text[m.start : m.end]) for m in parsed.marks]


class TestParseMarkdown(SimpleTestCase):
    def test_an_italic_is_an_em_mark_over_plain_text(self):
        parsed = markup.parse_markdown("In *Castleman*, Justice Scalia wrote")
        self.assertEqual(parsed.text, "In Castleman, Justice Scalia wrote")
        self.assertEqual(parsed.marks, [Mark(3, 12, EM)])
        self.assertEqual(parsed.kind, markup.PARAGRAPH)

    def test_a_bold_is_a_strong_mark_as_the_engine_wrote_it(self):
        # dots wrote a case name bold once; the mark says what it wrote.
        parsed = markup.parse_markdown(
            "(citing **Evans v. Lungrin**, 97-0541)"
        )
        self.assertEqual(marked(parsed), [(STRONG, "Evans v. Lungrin")])
        self.assertEqual(parsed.text, "(citing Evans v. Lungrin, 97-0541)")

    def test_a_mark_may_nest_in_another(self):
        parsed = markup.parse_markdown("**Held:** *see* **the *Court* said**")
        self.assertEqual(
            marked(parsed),
            [
                (STRONG, "Held:"),
                (EM, "see"),
                (STRONG, "the Court said"),
                (EM, "Court"),
            ],
        )

    def test_a_heading_mark_is_the_kind_and_leaves_the_text(self):
        parsed = markup.parse_markdown("## FACTS AND PROCEDURAL HISTORY")
        self.assertEqual(parsed.kind, markup.HEADING)
        self.assertEqual(parsed.text, "FACTS AND PROCEDURAL HISTORY")
        self.assertEqual(parsed.marks, [])

    def test_the_level_of_a_heading_is_dropped(self):
        for level in range(1, 7):
            with self.subTest(level=level):
                parsed = markup.parse_markdown("#" * level + " II.")
                self.assertEqual(
                    (parsed.kind, parsed.text), (markup.HEADING, "II.")
                )

    def test_an_enumerated_line_is_a_list_item_that_keeps_its_number(self):
        parsed = markup.parse_markdown("1. The Second Circuit assumed that")
        self.assertEqual(parsed.kind, markup.LIST_ITEM)
        self.assertEqual(parsed.text, "1. The Second Circuit assumed that")

    def test_a_bullet_is_a_list_item_and_an_asterism_is_text(self):
        self.assertEqual(
            markup.parse_markdown("- a fact the jury could consider").kind,
            markup.LIST_ITEM,
        )
        self.assertEqual(
            markup.parse_markdown("* The syllabus constitutes no part").kind,
            markup.LIST_ITEM,
        )
        asterism = markup.parse_markdown("* * *")
        self.assertEqual(
            (asterism.kind, asterism.text), (markup.PARAGRAPH, "* * *")
        )
        self.assertEqual(asterism.marks, [])

    def test_the_engine_s_label_names_the_kind_before_the_line_shape(self):
        # A ``Section-header`` that reads like a list item is a heading.
        parsed = markup.parse_markdown(
            "1. Standard of Review", kind=markup.HEADING
        )
        self.assertEqual(parsed.kind, markup.HEADING)
        # A heading mark is stripped whatever the label says.
        parsed = markup.parse_markdown("## Facts", kind=markup.LIST_ITEM)
        self.assertEqual((parsed.kind, parsed.text), (markup.HEADING, "Facts"))

    def test_a_unicode_superscript_is_a_sup_mark_in_ascii(self):
        parsed = markup.parse_markdown('perform sexual acts."¹ The defendants')
        self.assertEqual(parsed.text, 'perform sexual acts."1 The defendants')
        self.assertEqual(marked(parsed), [(SUP, "1")])
        parsed = markup.parse_markdown("at 121-122.¹²")
        self.assertEqual(marked(parsed), [(SUP, "12")])

    def test_a_lone_star_after_a_word_is_a_footnote_mark(self):
        parsed = markup.parse_markdown("Syllabus *")
        self.assertEqual(marked(parsed), [(SUP, "*")])
        parsed = markup.parse_markdown("(“Untrue *); id., at 721")
        self.assertEqual(marked(parsed), [(SUP, "*")])

    def test_a_star_page_is_text(self):
        parsed = markup.parse_markdown("2017 WL 2399020, at *1. See *5 there")
        self.assertEqual(parsed.text, "2017 WL 2399020, at *1. See *5 there")
        self.assertEqual(parsed.marks, [])

    def test_the_mistral_latex_superscript(self):
        parsed = markup.parse_markdown("(opinion of Scalia, J.).$^{4}$ The")
        self.assertEqual(parsed.text, "(opinion of Scalia, J.).4 The")
        self.assertEqual(marked(parsed), [(SUP, "4")])
        parsed = markup.parse_markdown("no further discovery.$^{[1]}$")
        self.assertEqual(marked(parsed), [(SUP, "[1]")])

    def test_a_dollar_amount_is_not_a_superscript(self):
        parsed = markup.parse_markdown(
            "three loans totaling $219,000 from one bank"
        )
        self.assertEqual(
            parsed.text, "three loans totaling $219,000 from one bank"
        )
        self.assertEqual(parsed.marks, [])

    def test_an_image_placeholder_is_dropped(self):
        self.assertEqual(
            markup.parse_markdown("![img-0.jpeg](img-0.jpeg)").text, ""
        )

    def test_an_underline_and_an_entity(self):
        parsed = markup.parse_markdown(
            "See <u>id</u>. Smith &amp; Jones &lt;3"
        )
        self.assertEqual(parsed.text, "See id. Smith & Jones <3")
        self.assertEqual(parsed.marks, [])

    def test_a_stray_html_tag_in_markdown_is_read(self):
        parsed = markup.parse_markdown("Lost Rents<sup>30</sup> and<br>more")
        self.assertEqual(parsed.text, "Lost Rents30 and\nmore")
        self.assertEqual(marked(parsed), [(SUP, "30")])

    def test_a_table_is_rows_of_cells(self):
        parsed = markup.parse_markdown(
            "<table><tr><td>Property Damage</td><td>$35,000.00</td></tr>"
            "<tr><td>Lost Rents<sup>30</sup></td><td>&nbsp;$1</td></tr></table>"
        )
        self.assertEqual(parsed.kind, markup.TABLE)
        self.assertEqual(
            parsed.table,
            [["Property Damage", "$35,000.00"], ["Lost Rents30", "$1"]],
        )
        self.assertEqual(
            parsed.text, "Property Damage $35,000.00\nLost Rents30 $1"
        )
        self.assertEqual(parsed.marks, [])

    def test_a_literal_angle_bracket_is_text(self):
        parsed = markup.parse_markdown('("Untrue <a false statement>")')
        self.assertEqual(parsed.text, '("Untrue <a false statement>")')

    def test_whitespace_is_one_space_or_one_line_break(self):
        parsed = markup.parse_markdown(
            "  Court of Appeal of Louisiana, \t\n\n First Circuit.  "
        )
        self.assertEqual(
            parsed.text, "Court of Appeal of Louisiana,\nFirst Circuit."
        )

    def test_an_empty_text(self):
        self.assertEqual(markup.parse_markdown(""), Parsed(text=""))
        self.assertEqual(markup.parse_markdown(None).text, "")


class TestTheDehyphenation(SimpleTestCase):
    def test_a_word_broken_at_the_line_end_is_joined(self):
        parsed = markup.parse_markdown(
            "The Government agrees with this princi-\nple, and"
        )
        self.assertEqual(
            parsed.text, "The Government agrees with this principle, and"
        )

    def test_a_prefix_of_the_keep_list_keeps_its_hyphen(self):
        self.assertEqual(markup.dehyphenate("a non-\nparty"), "a non-party")
        self.assertEqual(
            markup.dehyphenate("rea-\nsoning fol-\nlows"), "reasoning follows"
        )

    def test_a_syllable_that_pr_310_kept_is_joined(self):
        # ``pro-tection``, ``in-vestigated``, ``de-fining`` in the survey.
        self.assertEqual(
            markup.dehyphenate("equal pro-\ntection"), "equal protection"
        )
        self.assertEqual(markup.dehyphenate("in-\nvestigated"), "investigated")

    def test_a_mark_across_the_join_keeps_its_offsets(self):
        parsed = markup.parse_markdown("see *Wharton's Crimi-\nnal Law* there")
        self.assertEqual(parsed.text, "see Wharton's Criminal Law there")
        self.assertEqual(marked(parsed), [(EM, "Wharton's Criminal Law")])


class TestParseHtml(SimpleTestCase):
    def test_the_inline_tags(self):
        parsed = markup.parse_html(
            "<p>See <i>United States v. Castleman</i>, <b>572</b> U.S.<sup>1</sup></p>"
        )
        self.assertEqual(
            parsed.text, "See United States v. Castleman, 572 U.S.1"
        )
        self.assertEqual(
            marked(parsed),
            [(EM, "United States v. Castleman"), (STRONG, "572"), (SUP, "1")],
        )

    def test_a_superscript_stays_attached_to_its_word(self):
        parsed = markup.parse_html("<p>x<sup>1</sup> y</p>")
        self.assertEqual(parsed.text, "x1 y")
        self.assertEqual(parsed.marks, [Mark(1, 2, SUP)])

    def test_a_heading_tag_is_the_kind(self):
        for tag in ("h2", "h3", "h4"):
            with self.subTest(tag=tag):
                parsed = markup.parse_html(
                    f"<{tag}>12. Weapons ©194(2)</{tag}>"
                )
                self.assertEqual(
                    (parsed.kind, parsed.text),
                    (markup.HEADING, "12. Weapons ©194(2)"),
                )

    def test_a_list_group_is_list_items_one_per_line(self):
        parsed = markup.parse_html(
            '<ol style="list-style-type: none;">\n<li>(1) knowing or intentional</li>\n'
            "<li>(2) causing bodily injury</li>\n</ol>"
        )
        self.assertEqual(parsed.kind, markup.LIST_ITEM)
        self.assertEqual(
            parsed.text,
            "(1) knowing or intentional\n(2) causing bodily injury",
        )

    def test_a_paragraph_end_and_a_break_are_lines(self):
        parsed = markup.parse_html(
            "<p>DELLIGATTI v. U.S.<br/>Cite as 145 S.Ct. 797 (2025)</p>"
        )
        self.assertEqual(
            parsed.text, "DELLIGATTI v. U.S.\nCite as 145 S.Ct. 797 (2025)"
        )
        parsed = markup.parse_html(
            "<p>83 F.4th 113, affirmed.</p>\n<p>THOMAS, J., delivered</p>"
        )
        self.assertEqual(
            parsed.text, "83 F.4th 113, affirmed.\nTHOMAS, J., delivered"
        )

    def test_an_unknown_tag_is_dropped_and_an_entity_decoded(self):
        parsed = markup.parse_html(
            '<p><span class="x">Detroit Timber &amp; Lumber Co.</span></p>'
        )
        self.assertEqual(parsed.text, "Detroit Timber & Lumber Co.")

    def test_the_engine_s_label_names_the_kind(self):
        parsed = markup.parse_html("<p>DISCUSSION</p>", kind=markup.HEADING)
        self.assertEqual(parsed.kind, markup.HEADING)

    def test_a_table(self):
        parsed = markup.parse_html(
            "<table><tr><th>State</th><th>Status</th></tr><tr><td>Georgia</td><td>A crime</td></tr></table>"
        )
        self.assertEqual(parsed.kind, markup.TABLE)
        self.assertEqual(
            parsed.table, [["State", "Status"], ["Georgia", "A crime"]]
        )

    def test_an_unbalanced_tag_does_not_raise(self):
        parsed = markup.parse_html("<p>a </i>b<i> c</p>")
        self.assertEqual(parsed.text, "a b c")
        self.assertEqual(marked(parsed), [(EM, "c")])


class TestTheOffsets(SimpleTestCase):
    FIXTURES = [
        ("md", "In *Castleman*, Justice Scalia wrote an opinion"),
        (
            "md",
            "**Background:** Claimants seeking *benefits*.¹ See <u>id</u>. Smith &amp; Jones",
        ),
        ("md", "(opinion of Scalia, J.).$^{4}$ *ex proprio motu*, issued"),
        ("md", "see *Wharton's Crimi-\nnal Law* there **and *more* here**"),
        (
            "html",
            "<p>See <i>Holt v. Hobbs</i>, 574 U.S. 352<sup>3</sup> &amp; <b>more</b><br/>next</p>",
        ),
    ]

    def test_every_mark_covers_text_with_no_edge_space(self):
        for dialect, source in self.FIXTURES:
            with self.subTest(source=source):
                parsed = (
                    markup.parse_html
                    if dialect == "html"
                    else markup.parse_markdown
                )(source)
                for mark in parsed.marks:
                    covered = parsed.text[mark.start : mark.end]
                    self.assertTrue(covered)
                    self.assertEqual(covered, covered.strip())
                self.assertEqual(
                    parsed.marks, sorted(parsed.marks, key=lambda m: m.start)
                )
                for kind in markup.INLINE_KINDS:
                    ends = 0
                    for mark in [m for m in parsed.marks if m.kind == kind]:
                        self.assertGreaterEqual(mark.start, ends)
                        ends = mark.end

    def test_the_whitespace_contract(self):
        for dialect, source in self.FIXTURES:
            with self.subTest(source=source):
                parsed = (
                    markup.parse_html
                    if dialect == "html"
                    else markup.parse_markdown
                )(source)
                text = parsed.text
                self.assertEqual(text, text.strip())
                for bad in ("  ", " \n", "\n ", "\n\n", "\t"):
                    self.assertNotIn(bad, text)

    def test_serialize_and_parse_html_round_trip(self):
        rng = random.Random(404)
        words = [
            "Lewis",
            "v.",
            "Marcotte,",
            "Id.",
            "at",
            "1013.",
            "see",
            "&",
            "<3",
        ]
        for _ in range(200):
            text = " ".join(
                rng.choice(words) for _ in range(rng.randint(1, 8))
            )
            spans = markup.marks_of(self._random_flags(rng, text))
            parsed = Parsed(
                text=text, marks=spans, kind=rng.choice(markup.BLOCK_KINDS[:3])
            )
            with self.subTest(text=text, marks=spans):
                again = markup.parse_html(markup.serialize(parsed))
                self.assertEqual(again, parsed)

    @staticmethod
    def _random_flags(rng, text):
        # Flags per character, on whole words, so the marks never start
        # or end on a space (the contract of the parsers).
        flags = [frozenset() for _ in text]
        at = 0
        for word in text.split(" "):
            chosen = frozenset(
                k for k in markup.INLINE_KINDS if rng.random() < 0.3
            )
            for index in range(at, at + len(word)):
                flags[index] = chosen
            at += len(word) + 1
        for index, char in enumerate(text):
            if char == " " and 0 < index < len(text) - 1:
                flags[index] = flags[index - 1] & flags[index + 1]
        return flags

    def test_serialize_escapes_and_nests(self):
        parsed = markup.parse_markdown(
            "The *Court* held **so**.¹² Smith &amp; Jones <3"
        )
        self.assertEqual(
            markup.serialize(parsed),
            "The <em>Court</em> held <strong>so</strong>.<sup>12</sup> Smith &amp; Jones &lt;3",
        )
        heading = markup.parse_markdown("## FACTS")
        self.assertEqual(markup.serialize(heading), "<heading>FACTS</heading>")
        table = Parsed(text="a b", kind=markup.TABLE, table=[["a", "b"]])
        self.assertEqual(
            markup.serialize(table),
            "<table><tr><td>a</td><td>b</td></tr></table>",
        )


class TestShift(SimpleTestCase):
    def test_a_mark_after_a_deletion_moves_left(self):
        marks = [Mark(4, 9, EM)]
        self.assertEqual(markup.shift(marks, [(0, 4)]), [Mark(0, 5, EM)])

    def test_a_mark_inside_a_deletion_is_gone(self):
        self.assertEqual(markup.shift([Mark(1, 3, SUP)], [(0, 4)]), [])

    def test_a_mark_across_a_deletion_is_clipped(self):
        # "ab[3] cd" with em over "b[3] c": delete "[3] ".
        self.assertEqual(
            markup.shift([Mark(1, 7, EM)], [(2, 6)]), [Mark(1, 3, EM)]
        )

    def test_a_mark_before_a_deletion_stays(self):
        self.assertEqual(
            markup.shift([Mark(0, 2, STRONG)], [(5, 7)]), [Mark(0, 2, STRONG)]
        )

    def test_no_deletion_is_the_identity(self):
        marks = [Mark(0, 2, EM)]
        self.assertEqual(markup.shift(marks, []), marks)
