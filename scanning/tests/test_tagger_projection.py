"""Tests for the tagger's projection of an approved text (#272, #404).

``markup.project`` writes the tagger's HTML from the ``body`` of an
approved text, and ``markup.lift_span`` places a span of the answer back
on that body. The map is exact because the projection changes no
character but a ``\\n``; the round trip and the fuzz test pin that.
"""

import random

from django.test import TestCase

from scanning import markup


def para(text, kind="paragraph", blockquote=False, marks=()):
    """One paragraph of an approved body, in the shape of ``paragraphs``."""
    return {
        "kind": kind,
        "blockquote": blockquote,
        "text": text,
        "marks": [
            {"start": start, "end": end, "kind": mark}
            for start, end, mark in marks
        ],
        "pages": [0],
        "page_breaks": [],
        "joins": [],
        "human": False,
    }


def assert_exact(test, body, projection):
    """Every character the map names is its source character, or a
    space for a source ``\\n``."""
    for index, address in enumerate(projection.offsets):
        if address is None:
            continue
        paragraph, char = address
        source = body[paragraph]["text"][char]
        shown = projection.text[index]
        if source == "\n":
            test.assertEqual(shown, " ")
        elif source in "&<>":
            # Each character of the entity maps to its source character.
            test.assertIn(shown, markup._html.escape(source))
        else:
            test.assertEqual(shown, source, (index, address))


class TestTheBlocks(TestCase):
    def test_one_p_per_paragraph_and_one_newline_between(self):
        body = [para("Jane ROE, Appellant,"), para("v."), para("STATE.")]

        projection = markup.project(body)

        self.assertEqual(
            projection.text,
            "<p>Jane ROE, Appellant,</p>\n<p>v.</p>\n<p>STATE.</p>",
        )
        self.assertEqual(projection.paragraphs, [0, 1, 2])

    def test_a_heading_and_a_list_item_are_paragraphs(self):
        body = [para("OPINION", kind="heading"), para("1. First", "list_item")]

        self.assertEqual(
            markup.project(body).text, "<p>OPINION</p>\n<p>1. First</p>"
        )

    def test_a_list_group_is_one_p_per_item(self):
        # One group can hold several items (#428): one ``li`` mark per
        # item, with a line end between two items.
        text = "a. First item\nb. The appel-\nlant"
        body = [
            para(
                text,
                kind="list_item",
                marks=[(0, 13, "li"), (14, len(text), "li")],
            )
        ]

        projection = markup.project(body)

        self.assertEqual(
            projection.text,
            "<p>a. First item</p>\n<p>b. The appellant</p>",
        )
        assert_exact(self, body, projection)
        start = projection.text.index("b.")
        self.assertEqual(
            markup.lift_span(projection, start, start + 2),
            [{"paragraph": 0, "start": 14, "end": 16}],
        )

    def test_no_word_is_joined_across_two_items(self):
        text = "a. appel-\nb. fine"
        body = [
            para(
                text,
                kind="list_item",
                marks=[(0, 9, "li"), (10, len(text), "li")],
            )
        ]

        self.assertEqual(
            markup.project(body).text, "<p>a. appel-</p>\n<p>b. fine</p>"
        )

    def test_a_table_is_left_out_with_its_text(self):
        body = [para("Before."), para("a | b", kind="table"), para("After.")]

        projection = markup.project(body)

        self.assertEqual(projection.text, "<p>Before.</p>\n<p>After.</p>")
        self.assertEqual(projection.paragraphs, [0, 2])

    def test_an_empty_paragraph_is_not_a_block(self):
        body = [para("One."), para(""), para("Two.")]

        self.assertEqual(markup.project(body).text, "<p>One.</p>\n<p>Two.</p>")

    def test_a_quoted_paragraph_is_a_blockquote_block(self):
        # The worker cuts its windows at </p> and at </blockquote>, so a
        # quote is a block beside the paragraphs, one per paragraph.
        body = [
            para("The court said:"),
            para("First part.", blockquote=True),
            para("Second part.", blockquote=True),
            para("We agree."),
        ]

        self.assertEqual(
            markup.project(body).text,
            "<p>The court said:</p>\n<blockquote>First part.</blockquote>\n"
            "<blockquote>Second part.</blockquote>\n<p>We agree.</p>",
        )

    def test_the_text_is_escaped(self):
        body = [para("Smith & Jones <Inc.>")]

        projection = markup.project(body)

        self.assertEqual(
            projection.text, "<p>Smith &amp; Jones &lt;Inc.&gt;</p>"
        )
        assert_exact(self, body, projection)


class TestTheInlineMarks(TestCase):
    def test_em_and_sup_are_copied(self):
        body = [
            para("See Roe v. Wade.1", marks=[(4, 15, "em"), (16, 17, "sup")])
        ]

        self.assertEqual(
            markup.project(body).text,
            "<p>See <em>Roe v. Wade</em>.<sup>1</sup></p>",
        )

    def test_strong_goes_and_its_text_stays(self):
        body = [para("AFFIRMED.", marks=[(0, 8, "strong")])]

        self.assertEqual(markup.project(body).text, "<p>AFFIRMED.</p>")

    def test_a_sup_inside_an_em_nests(self):
        body = [para("Id. at 5.2", marks=[(0, 10, "em"), (9, 10, "sup")])]

        self.assertEqual(
            markup.project(body).text,
            "<p><em>Id. at 5.<sup>2</sup></em></p>",
        )


class TestTheLineEnds(TestCase):
    def test_a_line_end_is_a_space(self):
        body = [para("The appellant\nfiled a motion")]

        self.assertEqual(
            markup.project(body).text, "<p>The appellant filed a motion</p>"
        )

    def test_a_word_cut_at_a_line_end_is_joined(self):
        body = [para("the princi-\nple of law")]

        projection = markup.project(body)

        self.assertEqual(projection.text, "<p>the principle of law</p>")
        assert_exact(self, body, projection)

    def test_a_kept_prefix_keeps_its_hyphen(self):
        body = [para("a self-\nmade man")]

        self.assertEqual(markup.project(body).text, "<p>a self-made man</p>")

    def test_a_join_of_paragraphs_is_dehyphenated_too(self):
        # ``paragraphs.JOIN`` is a line end: a word cut at a column or a
        # page edge is joined by the same rule (#375).
        body = [para("the defend-\nant appealed")]

        self.assertEqual(
            markup.project(body).text, "<p>the defendant appealed</p>"
        )

    def test_a_number_range_keeps_its_hyphen(self):
        body = [para("pages 12-\n14")]

        self.assertEqual(markup.project(body).text, "<p>pages 12- 14</p>")


class TestTheLift(TestCase):
    def test_a_span_lands_on_its_paragraph(self):
        body = [para("Jane ROE, Appellant,"), para("v."), para("STATE.")]
        projection = markup.project(body)
        start = projection.text.index("Jane")
        end = start + len("Jane ROE")

        self.assertEqual(
            markup.lift_span(projection, start, end),
            [{"paragraph": 0, "start": 0, "end": 8}],
        )

    def test_a_span_over_markup_lands_on_the_text(self):
        body = [
            para("x"),
            para("See Roe v. Wade.", marks=[(4, 15, "em")]),
        ]
        projection = markup.project(body)
        start = projection.text.index("<em>")
        end = projection.text.index("</em>") + len("</em>")

        self.assertEqual(
            markup.lift_span(projection, start, end),
            [{"paragraph": 1, "start": 4, "end": 15}],
        )

    def test_a_span_over_two_blocks_is_cut_per_paragraph(self):
        body = [
            para("Jane ROE,"),
            para("a table", kind="table"),
            para("Appellant"),
        ]
        projection = markup.project(body)
        start = projection.text.index("Jane")
        end = projection.text.index("Appellant") + len("Appellant")

        self.assertEqual(
            markup.lift_span(projection, start, end),
            [
                {"paragraph": 0, "start": 0, "end": 9},
                {"paragraph": 2, "start": 0, "end": 9},
            ],
        )

    def test_a_span_across_a_joined_word_covers_the_source(self):
        body = [para("the princi-\nple of law")]
        projection = markup.project(body)
        start = projection.text.index("principle")

        self.assertEqual(
            markup.lift_span(projection, start, start + len("principle")),
            [{"paragraph": 0, "start": 4, "end": 15}],
        )

    def test_a_span_of_markup_alone_is_empty(self):
        projection = markup.project([para("x")])

        self.assertEqual(markup.lift_span(projection, 0, 3), [])


class TestTheMapIsExact(TestCase):
    """The round trip, over random marks, kinds and quote flags."""

    WORDS = [
        "court", "Roe", "v.", "Wade", "&", "<", "princi-\nple", "self-\nmade",
        "12-\n14", "Id.", "1", "¶", "said:", "“quote”", "a\nb",
    ]  # fmt: skip

    def random_body(self, rng):
        body = []
        for _ in range(rng.randint(1, 6)):
            text = " ".join(
                rng.choice(self.WORDS) for _ in range(rng.randint(1, 12))
            )
            marks = []
            for _ in range(rng.randint(0, 3)):
                start = rng.randrange(len(text))
                end = rng.randint(start + 1, len(text))
                marks.append(
                    (start, end, rng.choice(["em", "sup", "strong", "li"]))
                )
            body.append(
                para(
                    text,
                    kind=rng.choice(
                        ["paragraph", "paragraph", "heading", "table"]
                    ),
                    blockquote=rng.random() < 0.3,
                    marks=marks,
                )
            )
        return body

    def test_every_mapped_character_is_its_source(self):
        rng = random.Random(272)
        for _ in range(300):
            body = self.random_body(rng)
            projection = markup.project(body)
            self.assertEqual(len(projection.offsets), len(projection.text))
            assert_exact(self, body, projection)

    def test_every_sent_text_character_is_mapped(self):
        # Outside the tags, every character is text or the separator.
        rng = random.Random(404)
        for _ in range(300):
            body = self.random_body(rng)
            projection = markup.project(body)
            inside = False
            for char, address in zip(
                projection.text, projection.offsets, strict=True
            ):
                if address is not None:
                    continue
                if char == "<":
                    inside = True
                elif char == ">":
                    inside = False
                elif not inside:
                    self.assertEqual(char, "\n")

    def test_a_span_lifts_to_its_own_characters(self):
        rng = random.Random(375)
        for _ in range(300):
            body = self.random_body(rng)
            projection = markup.project(body)
            if not projection.text:
                continue
            start = rng.randrange(len(projection.text))
            end = rng.randint(start, len(projection.text))
            for part in markup.lift_span(projection, start, end):
                text = body[part["paragraph"]]["text"]
                self.assertLess(part["start"], part["end"])
                self.assertLessEqual(part["end"], len(text))
                self.assertIn(part["paragraph"], projection.paragraphs)
