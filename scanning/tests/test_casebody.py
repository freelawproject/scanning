"""Tests for the final XML of an opinion (``scanning/casebody.py``, #432):
the merge of the approved text and the tagger's spans, and the page and
the file that show it.

The merge is pure, so its tests are dicts. The page tests take the S3
stub of ``test_tagger``.
"""

from __future__ import annotations

import ast
import html
import json
import pathlib
import re
import xml.etree.ElementTree as ET
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from scanning import casebody, markup, tagger
from scanning.models import OpinionReviewStatus
from scanning.tests.test_tagger import _S3Case

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def para(text, *, pages=(0,), marks=(), breaks=(), **fields):
    return {
        "kind": fields.pop("kind", "paragraph"),
        "blockquote": fields.pop("blockquote", False),
        "text": text,
        "marks": [dict(m) for m in marks],
        "pages": list(pages),
        "page_breaks": [
            {"offset": offset, "page_in_opinion": page}
            for offset, page in breaks
        ],
        "joins": [],
        "human": False,
        **fields,
    }


def mark(start, end, kind):
    return {"start": start, "end": end, "kind": kind}


def span(paragraph, start, end, label):
    return {
        "paragraph": paragraph,
        "start": start,
        "end": end,
        "label": label,
    }


def doc(*body, footnotes=()):
    return {
        "opinion": {
            "first_printed_page": 502,
            "index_in_page": 0,
            "last_printed_page": 503,
            "scan": 7,
        },
        "pages": [
            {"page_in_opinion": 0, "printed": "502"},
            {"page_in_opinion": 1, "printed": "503"},
        ],
        "body": list(body),
        "footnotes": list(footnotes),
    }


def build(approved_doc, *spans):
    xml = casebody.build(approved_doc, {"spans": list(spans)})
    return xml, ET.fromstring(xml)


class TestTheElements(TestCase):
    """A whole span is the element; a part is inline."""

    def test_a_span_over_the_whole_paragraph_is_its_element(self):
        _xml, root = build(
            doc(para("Supreme Court of Example."), para("Smith, J.")),
            span(0, 0, 25, "court"),
            span(1, 0, 9, "author"),
        )

        self.assertEqual(root.find("court").text, "Supreme Court of Example.")
        self.assertEqual(root.find("opinion/author").text, "Smith, J.")

    def test_a_span_that_leaves_out_the_full_stop_is_still_whole(self):
        _xml, root = build(
            doc(
                para("Smith, J."), para("Affirmed.", marks=[mark(0, 9, "em")])
            ),
            span(0, 0, 9, "author"),
            span(1, 0, 8, "disposition"),
        )

        node = root.find("opinion/disposition")
        self.assertIsNotNone(node)
        self.assertEqual(node.find("em").text, "Affirmed.")

    def test_a_span_over_a_part_is_inline_in_the_paragraph(self):
        text = "ON MOTION FOR REHEARING"
        xml, root = build(
            doc(para(text, kind="heading"), para("Smith, J.")),
            span(0, 0, 2, "disposition"),
            span(0, 3, len(text), "heading"),
            span(1, 0, 9, "author"),
        )

        heading = root.find("heading")
        self.assertEqual(heading.find("disposition").text, "ON")
        # A span named as its paragraph adds no element of its own.
        self.assertIsNone(heading.find("heading"))
        self.assertIn("<heading><disposition>ON</disposition> MOTION FOR", xml)

    def test_datefiled_is_the_cap_decisiondate(self):
        _xml, root = build(
            doc(para("June 12, 2024")), span(0, 0, 13, "datefiled")
        )

        self.assertEqual(root.find("decisiondate").text, "June 12, 2024")
        self.assertIsNone(root.find("datefiled"))

    def test_an_unknown_label_keeps_its_own_name(self):
        _xml, root = build(doc(para("Syllabus")), span(0, 0, 8, "syllabus"))

        self.assertEqual(root.find("syllabus").text, "Syllabus")

    def test_the_parties_are_one_element(self):
        _xml, root = build(
            doc(para("Jane ROE,"), para("v."), para("STATE.")),
            span(0, 0, 9, "party"),
            span(1, 0, 2, "separator"),
            span(2, 0, 6, "party"),
        )

        parties = root.find("parties")
        self.assertEqual(
            [child.tag for child in parties],
            ["party", "separator", "party"],
        )
        self.assertEqual("".join(parties.itertext()), "Jane ROE, v. STATE.")


class TestTheHeadMatter(TestCase):
    """The head matter ends at the author."""

    def test_the_opinion_starts_at_an_author_in_the_tagged_run(self):
        _xml, root = build(
            doc(
                para("Supreme Court."),
                para("ON MOTION"),
                para("Smith, J."),
                para("We affirm."),
                para("Jones, J., concurs."),
            ),
            span(0, 0, 14, "court"),
            span(1, 0, 9, "heading"),
            span(2, 0, 9, "author"),
            span(4, 0, 19, "judges"),
        )

        self.assertEqual(
            [c.tag for c in root], ["court", "heading", "opinion"]
        )
        self.assertEqual(
            [c.tag for c in root.find("opinion")],
            ["author", "p", "judges"],
        )

    def test_an_untagged_paragraph_ends_the_head_matter(self):
        _xml, root = build(
            doc(para("Supreme Court."), para("OPINION"), para("Smith, J.")),
            span(0, 0, 14, "court"),
            span(2, 0, 9, "author"),
        )

        self.assertEqual([c.tag for c in root], ["court", "opinion"])
        self.assertEqual(
            [c.tag for c in root.find("opinion")], ["p", "author"]
        )

    def test_a_per_curiam_opinion_is_no_head_matter(self):
        """The first author span is the dissent's: the main opinion has
        none, and its text stays in the opinion."""
        _xml, root = build(
            doc(
                para("Smith v. Jones"),
                para("PER CURIAM."),
                para("We affirm the judgment."),
                para("JONES, J., dissenting."),
                para("I dissent."),
            ),
            span(0, 0, 14, "party"),
            span(3, 0, 22, "author"),
        )

        self.assertEqual(
            [c.tag for c in root], ["parties", "opinion", "opinion"]
        )
        majority, dissent = root.findall("opinion")
        self.assertEqual(majority.get("type"), "majority")
        self.assertEqual([c.tag for c in majority], ["p", "p"])
        self.assertEqual(dissent.get("type"), "dissent")
        self.assertEqual([c.tag for c in dissent], ["author", "p"])

    def test_an_untagged_caption_line_does_not_end_the_head_matter(self):
        """The tagger missed a line of the caption: the caption labels
        after it keep it in the head matter."""
        _xml, root = build(
            doc(
                para("Smith v. Jones"),
                para("ON MOTION FOR REHEARING"),
                para("Supreme Court of Florida."),
                para("Attorneys for appellant."),
                para("JONES, J."),
                para("We affirm."),
            ),
            span(0, 0, 14, "party"),
            span(2, 0, 25, "court"),
            span(3, 0, 24, "attorneys"),
            span(4, 0, 9, "author"),
        )

        self.assertEqual(
            [c.tag for c in root],
            ["parties", "p", "court", "attorneys", "opinion"],
        )
        self.assertEqual(
            [c.tag for c in root.find("opinion")], ["author", "p"]
        )

    def test_a_per_curiam_with_an_untagged_caption_line(self):
        """Both cases at once: the caption's own labels end it, and the
        judges of the main opinion are not caption labels."""
        _xml, root = build(
            doc(
                para("Smith v. Jones"),
                para("Rehearing denied."),
                para("Supreme Court of Florida."),
                para("PER CURIAM."),
                para("We affirm."),
                para("Kuntz and Artau, JJ., concur."),
                para("JONES, J., dissenting."),
                para("I dissent."),
            ),
            span(0, 0, 14, "party"),
            span(2, 0, 25, "court"),
            span(5, 0, 29, "judges"),
            span(6, 0, 22, "author"),
        )

        self.assertEqual(
            [c.tag for c in root],
            ["parties", "p", "court", "opinion", "opinion"],
        )
        majority, dissent = root.findall("opinion")
        self.assertEqual([c.tag for c in majority], ["p", "p", "judges"])
        self.assertEqual([c.tag for c in dissent], ["author", "p"])

    def test_a_caption_label_inside_the_text_is_no_caption_line(self):
        """A per curiam, a date the tagger read in its text, and a
        dissent: the date is a span over some words of a sentence, and
        the main opinion stays in the opinion."""
        text = "The trial court ruled on June 1, 2020, that the claim failed."
        date = text.index("June")
        _xml, root = build(
            doc(
                para("Smith v. Jones"),
                para("PER CURIAM."),
                para("We affirm."),
                para(text),
                para("JONES, J., dissenting."),
                para("I dissent."),
            ),
            span(0, 0, 14, "party"),
            span(3, date, date + len("June 1, 2020"), "otherdate"),
            span(4, 0, 22, "author"),
        )

        self.assertEqual(
            [c.tag for c in root], ["parties", "opinion", "opinion"]
        )
        paragraph = root.find("opinion")[2]
        self.assertEqual(paragraph.find("otherdate").text, "June 1, 2020")

    def test_with_no_author_a_span_deep_in_the_text_is_no_caption_line(self):
        text = "The case came from the circuit court."
        court = text.index("the circuit")
        _xml, root = build(
            doc(
                para("Supreme Court."),
                para("We affirm."),
                para(text),
                para("More text."),
            ),
            span(0, 0, 14, "court"),
            span(2, court, court + len("the circuit court"), "history"),
        )

        self.assertEqual([c.tag for c in root], ["court", "opinion"])
        self.assertEqual(
            [c.tag for c in root.find("opinion")], ["p", "p", "p"]
        )

    def test_with_no_author_the_leading_tagged_run_is_the_head_matter(self):
        _xml, root = build(
            doc(para("Supreme Court."), para("We affirm.")),
            span(0, 0, 14, "court"),
        )

        self.assertEqual([c.tag for c in root], ["court", "opinion"])


class TestTheMarks(TestCase):
    """The marks of the approved text, and their crossings."""

    def test_a_span_that_crosses_an_italic_is_well_formed(self):
        text = "See Smith v. Jones, 1 U.S. 1."
        xml, root = build(
            doc(para("Smith, J."), para(text, marks=[mark(4, 18, "em")])),
            span(0, 0, 9, "author"),
            span(1, 0, 9, "history"),
        )

        paragraph = root.find("opinion/p")
        self.assertEqual("".join(paragraph.itertext()), text)
        self.assertIn(
            "<history>See <em>Smith</em></history><em> v. Jones</em>", xml
        )

    def test_a_list_writes_one_li_per_item(self):
        text = "(a) one;\n(b) two."
        _xml, root = build(
            doc(
                para(
                    text,
                    kind="list_item",
                    list="ol",
                    marks=[mark(0, 8, "li"), mark(9, 17, "li")],
                )
            )
        )

        items = root.findall("opinion/ol/li")
        self.assertEqual([i.text for i in items], ["(a) one;", "(b) two."])

    def test_a_sup_with_a_footnote_label_is_a_footnotemark(self):
        text = "The rule.1 The star*"
        _xml, root = build(
            doc(
                para(text, marks=[mark(9, 10, "sup"), mark(19, 20, "sup")]),
                footnotes=[
                    {
                        "label": "1",
                        "pages": [0],
                        "paragraphs": [para("A note.")],
                    }
                ],
            )
        )

        paragraph = root.find("opinion/p")
        self.assertEqual(paragraph.find("footnotemark").text, "1")
        self.assertEqual(paragraph.find("sup").text, "*")

    def test_the_footnotes_close_the_opinion(self):
        _xml, root = build(
            doc(
                para("Text."),
                footnotes=[
                    {
                        "label": "1",
                        "pages": [0],
                        "paragraphs": [para("A note."), para("More.")],
                    },
                    {"label": None, "pages": [0], "paragraphs": [para("X")]},
                ],
            )
        )

        notes = root.findall("opinion/footnote")
        self.assertEqual(notes[0].get("label"), "1")
        self.assertEqual([p.text for p in notes[0]], ["A note.", "More."])
        self.assertIsNone(notes[1].get("label"))
        self.assertEqual(root.find("opinion")[-1].tag, "footnote")

    def test_a_quoted_paragraph_is_a_blockquote(self):
        _xml, root = build(doc(para("Quoted.", blockquote=True)))

        self.assertEqual(root.find("opinion/blockquote").text, "Quoted.")

    def test_text_and_attributes_are_escaped(self):
        _xml, root = build(doc(para('A & B <c> "d"')))

        self.assertEqual(root.find("opinion/p").text, 'A & B <c> "d"')


class TestThePages(TestCase):
    """A page the text crosses is a ``page-number``."""

    def test_a_page_break_inside_a_paragraph(self):
        xml, root = build(doc(para("one two", pages=(0, 1), breaks=[(4, 1)])))

        number = root.find("opinion/p/page-number")
        self.assertEqual(number.get("label"), "503")
        self.assertIn(
            'one <page-number label="503">*503</page-number>two', xml
        )

    def test_a_paragraph_that_starts_a_page(self):
        _xml, root = build(doc(para("one"), para("two", pages=(1,))))

        paragraphs = root.findall("opinion/p")
        self.assertIsNone(paragraphs[0].find("page-number"))
        self.assertEqual(paragraphs[1].find("page-number").text, "*503")

    def test_a_page_with_no_printed_number_writes_none(self):
        approved_doc = doc(para("one"), para("two", pages=(1,)))
        approved_doc["pages"][1]["printed"] = None

        _xml, root = build(approved_doc)

        self.assertIsNone(root.find(".//page-number"))


class TestTheTables(TestCase):
    """A table keeps its rows, and the page that starts at it."""

    def table(self, rows, pages=(0,)):
        return para("", kind="table", table=rows, pages=pages)

    def test_the_rows_are_cells(self):
        _xml, root = build(doc(self.table([["a", "b"], ["c", "d"]])))

        self.assertEqual(
            [[td.text for td in tr] for tr in root.find("opinion/table")],
            [["a", "b"], ["c", "d"]],
        )

    def test_a_page_that_starts_at_a_table_is_numbered_in_its_first_cell(self):
        _xml, root = build(
            doc(
                para("one"),
                self.table([["a", "b"]], pages=(1,)),
                para("two", pages=(1,)),
            )
        )

        numbers = list(root.iter("page-number"))
        self.assertEqual([n.get("label") for n in numbers], ["503"])
        cell = root.find("opinion/table/tr/td")
        self.assertEqual("".join(cell.itertext()), "*503a")


class TestTheXmlIsAlwaysWellFormed(TestCase):
    """A character or a label XML does not allow never breaks the file."""

    def test_a_control_character_of_the_ocr_is_dropped(self):
        _xml, root = build(doc(para("a\x0bb\x0cc")))

        self.assertEqual(root.find("opinion/p").text, "abc")

    def test_a_label_that_is_no_xml_name_is_made_one(self):
        _xml, root = build(
            doc(para("June 1."), para("Smith, J.")),
            span(0, 0, 7, "other date"),
            span(1, 0, 9, "author"),
        )

        self.assertEqual(root.find("other-date").text, "June 1.")

    def test_a_label_named_as_a_structure_element_gets_a_prefix(self):
        for label in ("opinion", "footnote", "p", "page-number", "parties"):
            with self.subTest(label=label):
                self.assertEqual(casebody.element_of(label), f"label-{label}")
        # A label of the table keeps its CAP name, heading too.
        self.assertEqual(casebody.element_of("heading"), "heading")

    def test_a_label_that_starts_badly_gets_a_prefix(self):
        self.assertEqual(casebody.element_of("1st"), "label-1st")
        self.assertEqual(casebody.element_of("xmlish"), "label-xmlish")
        self.assertEqual(casebody.element_of(""), "label")

    def test_the_display_draws_both(self):
        xml = casebody.build(
            doc(para("a\x0bb")), {"spans": [span(0, 0, 3, "other date")]}
        )

        self.assertIn('data-role="other-date"', casebody.display_html(xml))


class TestTheText(TestCase):
    """The text is the text the tagger read."""

    def test_a_word_cut_at_a_line_end_is_joined(self):
        text = "the find-\nings of fact"
        _xml, root = build(
            doc(para("Smith, J."), para(text)),
            span(0, 0, 9, "author"),
            span(1, 4, 14, "history"),
        )

        paragraph = root.find("opinion/p")
        self.assertEqual("".join(paragraph.itertext()), "the findings of fact")
        self.assertEqual(paragraph.find("history").text, "findings")

    def test_a_kept_prefix_keeps_its_hyphen(self):
        _xml, root = build(doc(para("a self-\nmade man")))

        self.assertEqual(root.find("opinion/p").text, "a self-made man")


class TestTheRefusals(TestCase):
    """A span that does not address the body is refused."""

    def test_a_span_outside_the_body(self):
        with self.assertRaises(casebody.CasebodyError):
            build(doc(para("one")), span(3, 0, 1, "court"))

    def test_a_span_outside_its_text(self):
        with self.assertRaises(casebody.CasebodyError):
            build(doc(para("one")), span(0, 0, 9, "court"))

    def test_a_span_with_no_label(self):
        with self.assertRaises(casebody.CasebodyError):
            build(doc(para("one")), {"paragraph": 0, "start": 0, "end": 1})


class TestTheSample(TestCase):
    """Scan 3593, opinion 25.0: the pair the issue was built on."""

    def setUp(self):
        self.approved = json.loads(
            (FIXTURES / "approved.so3d.388.25.json").read_text()
        )
        self.tags = json.loads(
            (FIXTURES / "tags.so3d.388.25.json").read_text()
        )
        self.xml = casebody.build(self.approved, self.tags)
        self.root = ET.fromstring(self.xml)

    def test_the_head_matter(self):
        self.assertEqual(
            [c.tag for c in self.root][:7],
            [
                "parties",
                "docketnumber",
                "court",
                "decisiondate",
                "history",
                "attorneys",
                "attorneys",
            ],
        )
        self.assertEqual(
            self.root.find("docketnumber").text, "No. 4D2023-0049"
        )

    def test_the_opinion(self):
        opinion = self.root.find("opinion")
        self.assertEqual(opinion[0].tag, "author")
        self.assertEqual(
            [c.tag for c in opinion if c.tag != "footnote"][-2:],
            ["disposition", "judges"],
        )
        self.assertEqual(len(opinion.findall("footnote")), 2)
        self.assertEqual(
            [m.text for m in opinion.iter("footnotemark")], ["1", "2"]
        )

    def test_every_page_after_the_first_is_numbered_once(self):
        self.assertEqual(
            [n.get("label") for n in self.root.iter("page-number")],
            ["26", "27", "28", "29", "30", "31"],
        )

    def test_the_text_is_every_character_of_the_body(self):
        projection = markup.project(self.approved["body"])
        body = [c for c in self.root if c.tag != "opinion"] + [
            c for c in self.root.find("opinion") if c.tag != "footnote"
        ]
        text = "".join("".join(node.itertext()) for node in body)
        for number in ("*26", "*27", "*28", "*29", "*30", "*31"):
            text = text.replace(number, "", 1)
        sent = html.unescape(
            "".join(
                char
                for char, address in zip(
                    projection.text, projection.offsets, strict=True
                )
                if address is not None
            )
        )
        self.assertEqual(text.replace(" ", ""), sent.replace(" ", ""))

    def test_the_display_and_the_source_escape_the_text(self):
        display = casebody.display_html(self.xml)
        source = casebody.source_html(self.xml)

        self.assertIn('class="cb-row role-start" data-role="court"', display)
        # A run of one role is named once: the second attorneys row is
        # no role start.
        self.assertEqual(
            display.count('class="cb-row role-start" data-role="attorneys"'), 1
        )
        self.assertIn('<span class="cb-tag" data-role="disposition"', display)
        self.assertIn('<sup class="cb-fnmark"', display)
        self.assertIn("Appeals &amp; Trials", display)
        self.assertIn(
            '<span class="x-tag" data-role="court">&lt;court', source
        )
        self.assertIn('<span class="x-tag">&lt;p&gt;</span>', source)
        self.assertNotIn("<court>", source)


class TestTheFootnoteLinks(TestCase):
    """The display links a mark to its note, and the note back."""

    def display(self, text, sups, notes):
        approved_doc = doc(
            para(text, marks=[mark(a, b, "sup") for a, b in sups]),
            footnotes=[
                {"label": label, "pages": [0], "paragraphs": [para("A note.")]}
                for label in notes
            ],
        )
        return casebody.display_html(
            casebody.build(approved_doc, {"spans": []})
        )

    def test_a_mark_links_to_its_note_and_the_note_back(self):
        html_ = self.display("The rule.1", [(9, 10)], ["1"])

        self.assertIn(
            '<sup class="cb-fnmark" id="cb-fn-1-1-ref-1" '
            'title="footnotemark"><a href="#cb-fn-1-1">1</a></sup>',
            html_,
        )
        self.assertIn('<div class="cb-fn" id="cb-fn-1-1">', html_)
        self.assertIn(
            '<a href="#cb-fn-1-1-ref-1" title="Back to the text">1</a>', html_
        )

    def test_a_note_two_marks_cite_links_back_to_each(self):
        html_ = self.display("A.1 B.1", [(2, 3), (6, 7)], ["1"])

        self.assertIn('id="cb-fn-1-1-ref-2"', html_)
        self.assertIn('href="#cb-fn-1-1-ref-2"', html_)

    def test_a_note_no_mark_cites_has_no_back_link(self):
        html_ = self.display("No mark.", [], ["1"])

        self.assertIn(
            '<div class="cb-fn" id="cb-fn-1-1"><span class="cb-lbl">1</span>',
            html_,
        )

    def test_a_symbol_label_gives_an_id(self):
        html_ = self.display("The rule.*", [(9, 10)], ["*"])

        self.assertIn('href="#cb-fn-1-_2a"', html_)
        self.assertIn('id="cb-fn-1-_2a"', html_)

    def test_a_second_note_of_one_label_is_no_target(self):
        html_ = self.display("The rule.1", [(9, 10)], ["1", "1"])

        self.assertEqual(html_.count('id="cb-fn-1-1"'), 1)


def note(label, *, pages=(0,), text="A note."):
    return {"label": label, "pages": list(pages), "paragraphs": [para(text)]}


def sup_para(text, label, **fields):
    """A paragraph that ends in the footnote mark ``label``."""
    return para(
        f"{text}{label}",
        marks=[mark(len(text), len(text) + len(label), "sup")],
        **fields,
    )


class TestTheSubOpinions(TestCase):
    """One ``opinion`` per writing of the cluster, with its type (#442)."""

    def types(self, *body, spans=()):
        _xml, root = build(doc(*body), *spans)
        return [o.get("type") for o in root.findall("opinion")]

    def test_an_author_line_with_its_role_starts_a_writing(self):
        _xml, root = build(
            doc(
                para("Smith, J."),
                para("We affirm."),
                para("Jones, J., dissenting."),
                para("I would reverse."),
            ),
            span(0, 0, 9, "author"),
            span(2, 0, 22, "author"),
        )

        majority, dissent = root.findall("opinion")
        self.assertEqual(majority.get("type"), "majority")
        self.assertEqual([c.tag for c in majority], ["author", "p"])
        self.assertEqual(dissent.get("type"), "dissent")
        self.assertEqual(dissent.find("author").text, "Jones, J., dissenting.")

    def test_the_role_in_the_next_paragraph_is_read(self):
        """The CAP shape of Palsgraf: ``<author>`` then "(dissenting)."."""
        self.assertEqual(
            self.types(
                para("Cardozo, Ch. J."),
                para("We reverse."),
                para("Andrews, J."),
                para("(dissenting). Assisting a passenger to board a train."),
                spans=[span(0, 0, 15, "author"), span(2, 0, 11, "author")],
            ),
            ["majority", "dissent"],
        )

    def test_the_role_in_the_rest_of_the_author_paragraph_is_read(self):
        text = "Andrews, J. (dissenting). Assisting a passenger."
        self.assertEqual(
            self.types(
                para("Cardozo, Ch. J."),
                para("We reverse."),
                para(text),
                spans=[span(0, 0, 15, "author"), span(2, 0, 11, "author")],
            ),
            ["majority", "dissent"],
        )

    def test_a_role_later_in_the_next_sentence_is_text_not_a_role(self):
        """The majority's author line, then a sentence that names a role
        in its middle: one writing."""
        self.assertEqual(
            self.types(
                para("OPINION"),
                para("Smith, J."),
                para("We concur with the trial court that the claim fails."),
                spans=[span(1, 0, 9, "author")],
            ),
            ["majority"],
        )

    def test_the_types_of_a_partial_dissent(self):
        for line in (
            "Ciklin, J., concurring in part and dissenting in part.",
            "Ciklin, J., dissenting in part and concurring in part.",
            "Ciklin, J., concurring in part, dissenting in part.",
            "Ciklin, J., dissenting in part.",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    self.types(
                        para("Gerber, J."),
                        para("We affirm."),
                        para(line),
                        spans=[
                            span(0, 0, 10, "author"),
                            span(2, 0, len(line), "author"),
                        ],
                    ),
                    ["majority", casebody.IN_PART],
                )

    def test_the_first_role_word_decides(self):
        cases = {
            "Jones, J., dissenting, in which Smith, J., concurs.": "dissent",
            "Jones, J., specially concurring.": "concurrence",
            "Jones, J., concurring in the result.": "concurrence",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(
                    self.types(
                        para("Gerber, J."),
                        para("We affirm."),
                        para(line),
                        spans=[
                            span(0, 0, 10, "author"),
                            span(2, 0, len(line), "author"),
                        ],
                    )[1],
                    expected,
                )

    def test_a_stage_of_the_case_is_read_from_its_heading(self):
        self.assertEqual(
            self.types(
                para("Gerber, J."),
                para("We affirm."),
                para("ON PETITION FOR REHEARING", kind="heading"),
                para("PER CURIAM."),
                para("The petition is denied."),
                spans=[span(0, 0, 10, "author"), span(3, 0, 11, "author")],
            ),
            ["majority", "rehearing"],
        )

    def test_a_vote_line_cut_into_author_spans_starts_nothing(self):
        """Scan 3593, opinion 826.0: the tagger cuts the vote line into
        ``judges`` and ``author`` spans of a word or two."""
        line = "Ciklin, J., concurs in part and dissents in part with opinion."
        self.assertEqual(
            self.types(
                para("Gerber, J."),
                para("We affirm."),
                para(line),
                spans=[
                    span(0, 0, 10, "author"),
                    span(2, 0, 19, "judges"),
                    span(2, 20, 22, "author"),
                    span(2, 28, 31, "author"),
                ],
            ),
            ["majority"],
        )

    def test_a_plurality_says_so(self):
        line = "Smith, J., announced the judgment and delivered a plurality opinion."
        self.assertEqual(
            self.types(
                para(line),
                para("We affirm."),
                spans=[span(0, 0, len(line), "author")],
            ),
            ["plurality"],
        )

    def test_a_writing_whose_type_is_not_read_refuses_the_xml(self):
        with self.assertRaises(casebody.OpinionTypeError) as caught:
            build(
                doc(
                    para("Gerber, J."),
                    para("We affirm."),
                    para("Jones, J., dissenting."),
                    para("I would reverse."),
                    para("Brown, J."),
                    para("The majority is right in part."),
                ),
                span(0, 0, 10, "author"),
                span(2, 0, 22, "author"),
                span(4, 0, 9, "author"),
            )

        self.assertIn("brown, j.", str(caught.exception))
        self.assertIsInstance(caught.exception, casebody.CasebodyError)

    def test_every_type_is_a_key_of_the_courtlistener_map(self):
        """The keys of ``harvard_opinions.map_opinion_type``, spelled here
        at courtlistener 58784cac: a value outside them is ``combined``
        in one importer and an error in the other."""
        harvard_keys = {
            "unanimous",
            "majority",
            "plurality",
            "concurrence",
            "concurring-in-part-and-dissenting-in-part",
            "dissent",
            "remittitur",
            "rehearing",
            "on-the-merits",
            "on-motion-to-strike-cost-bill",
        }
        self.assertEqual(casebody.CL_TYPES, harvard_keys)
        tables = casebody.OPINION_TYPES + casebody.FIRST_OPINION_TYPES
        values = {value for _pattern, value in tables if value}
        self.assertLessEqual(
            values | casebody.SEPARATE_TYPES | {casebody.MAJORITY},
            harvard_keys,
        )
        self.assertEqual(set(casebody.TYPE_NAMES), harvard_keys)


class TestTheSubOpinionFootnotes(TestCase):
    """A footnote goes to the writing that holds its mark (#442)."""

    def opinions(self, body, notes, spans):
        _xml, root = build(doc(*body, footnotes=notes), *spans)
        return [
            [n.get("label") for n in o.findall("footnote")]
            for o in root.findall("opinion")
        ]

    def test_a_note_goes_to_the_writing_of_its_mark(self):
        self.assertEqual(
            self.opinions(
                [
                    para("Gerber, J."),
                    sup_para("We affirm.", "1"),
                    para("Jones, J., dissenting."),
                    sup_para("I would reverse.", "2"),
                ],
                [note("1"), note("2")],
                [span(0, 0, 10, "author"), span(2, 0, 22, "author")],
            ),
            [["1"], ["2"]],
        )

    def test_a_dissent_that_numbers_from_one_again_takes_its_own_notes(self):
        self.assertEqual(
            self.opinions(
                [
                    para("Gerber, J."),
                    sup_para("We affirm.", "1"),
                    sup_para("For two reasons.", "2"),
                    para("Jones, J., dissenting."),
                    sup_para("I would reverse.", "1"),
                ],
                [note("1"), note("2"), note("1")],
                [span(0, 0, 10, "author"), span(3, 0, 22, "author")],
            ),
            [["1", "2"], ["1"]],
        )

    def test_a_note_with_no_mark_goes_by_its_page(self):
        self.assertEqual(
            self.opinions(
                [
                    para("Gerber, J."),
                    sup_para("We affirm.", "1"),
                    para("Jones, J., dissenting.", pages=(1,)),
                    para("I would reverse.", pages=(1,)),
                ],
                [note("1"), note("2", pages=(1,))],
                [span(0, 0, 10, "author"), span(2, 0, 22, "author")],
            ),
            [["1"], ["2"]],
        )

    def test_the_display_scopes_the_note_ids_by_writing(self):
        xml, _root = build(
            doc(
                para("Gerber, J."),
                sup_para("We affirm.", "1"),
                para("Jones, J., dissenting."),
                sup_para("I would reverse.", "1"),
                footnotes=[note("1"), note("1", text="The other note.")],
            ),
            span(0, 0, 10, "author"),
            span(2, 0, 22, "author"),
        )

        display = casebody.display_html(xml)

        ids = re.findall(r'id="([^"]+)"', display)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('href="#cb-fn-2-1"', display)
        self.assertIn('<div class="cb-fn" id="cb-fn-2-1">', display)
        self.assertIn('<nav class="cb-opinions"', display)
        self.assertIn(
            '<a href="#cb-op-2">Dissent — Jones, J., dissenting.</a>', display
        )
        self.assertIn('id="cb-op-1" data-type="majority"', display)

    def test_one_writing_has_no_nav(self):
        xml, _root = build(
            doc(para("Gerber, J."), para("We affirm.")),
            span(0, 0, 10, "author"),
        )

        self.assertNotIn("cb-opinions", casebody.display_html(xml))


class TestTheClusterSample(TestCase):
    """Scan 3593, opinion 826.0: the cluster of the issue (#442)."""

    def setUp(self):
        approved = json.loads(
            (FIXTURES / "approved.so3d.388.826.json").read_text()
        )
        tags = json.loads((FIXTURES / "tags.so3d.388.826.json").read_text())
        self.root = ET.fromstring(casebody.build(approved, tags))

    def test_a_majority_and_a_partial_dissent(self):
        majority, partial = self.root.findall("opinion")

        self.assertEqual(majority.get("type"), "majority")
        self.assertEqual(majority.find("author").text, "Gerber, J.")
        self.assertEqual(partial.get("type"), casebody.IN_PART)
        self.assertEqual(
            partial.find("author").text,
            "Ciklin, J., concurring in part and dissenting in part.",
        )

    def test_the_vote_line_stays_in_the_majority(self):
        majority = self.root.findall("opinion")[0]
        last = "".join(majority[-1].itertext())

        self.assertTrue(last.startswith("Ciklin, J., concurs in part"))

    def test_the_notes_are_the_partial_dissents(self):
        majority, partial = self.root.findall("opinion")

        self.assertEqual(majority.findall("footnote"), [])
        self.assertEqual(
            [n.get("label") for n in partial.findall("footnote")], ["1", "2"]
        )


class TestTheSourceView(TestCase):
    """The XML view is the XML, escaped, with its tags coloured."""

    def test_it_is_the_xml_character_for_character(self):
        xml, _root = build(
            doc(para('A & "B" <c>', marks=[mark(0, 1, "em")])),
            span(0, 0, 1, "court"),
        )

        shown = casebody.source_html(xml)

        self.assertEqual(html.unescape(re.sub(r"<[^>]+>", "", shown)), xml)

    def test_an_unclosed_attribute_is_plain_text(self):
        """The shape CodeQL named: no tag matches, and it is escaped."""
        text = "<a" + ' b="c"' * 50 + ' -="' * 50

        self.assertEqual(casebody.source_html(text), html.escape(text))


class TestTheModuleIsPure(TestCase):
    """The merge reads its two dicts alone, like ``paragraphs``."""

    def test_it_imports_no_django(self):
        tree = ast.parse(pathlib.Path("scanning/casebody.py").read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(
                    f"{node.module}.{node.names[0].name}"
                    if node.module == "scanning"
                    else node.module or ""
                )
        self.assertEqual(
            names,
            {
                "__future__",
                "html",
                "re",
                "xml.etree.ElementTree",
                "bisect",
                "dataclasses",
                "scanning.markup",
            },
        )


class TestTheFinalXmlRoutes(_S3Case):
    """The page, the file, and the link of the review page."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def tagged(self):
        """Glue a finished run, so ``tagger.is_written`` holds."""
        self.finished_row()
        tagger.finish_ready_runs()
        self.opinion.refresh_from_db()
        self.assertTrue(tagger.is_written(self.opinion))

    def url(self, name):
        return reverse(
            name, kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk}
        )

    def test_both_routes_are_404_before_the_spans(self):
        for name in ("opinion_final_xml", "serve_opinion_final_xml"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(self.url(name)).status_code, 404
                )

    def test_the_file_is_the_xml(self):
        self.tagged()

        response = self.client.get(self.url("serve_opinion_final_xml"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"], "application/xml; charset=utf-8"
        )
        root = ET.fromstring(response.content)
        self.assertEqual(
            root.find("parties/party").text, "Jane ROE, Appellant,"
        )
        self.assertNotIn("Content-Disposition", response)

    def test_the_download_is_a_file(self):
        self.tagged()

        response = self.client.get(
            self.url("serve_opinion_final_xml") + "?download=1"
        )

        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("-final.xml", response["Content-Disposition"])

    def test_another_download_value_is_no_download(self):
        self.tagged()

        response = self.client.get(
            self.url("serve_opinion_final_xml") + "?download=0"
        )

        self.assertNotIn("Content-Disposition", response)

    def test_a_refusal_names_no_exception(self):
        self.tagged()
        self.stored[self.opinion.tag_key]["spans"] = [span(9, 0, 1, "court")]

        message = self.client.get(self.url("serve_opinion_final_xml")).json()[
            "message"
        ]

        self.assertNotIn("paragraph 9", message)
        self.assertIn("The log of the web pod has the reason", message)

    def test_the_page_draws_both_views(self):
        self.tagged()

        response = self.client.get(self.url("opinion_final_xml"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pane-reading"')
        self.assertContains(response, 'id="pane-xml"')
        self.assertContains(response, 'data-role="parties"')

    def test_a_type_that_is_not_read_is_a_409_that_says_why(self):
        """The words are the opinion's own, so the answer says them, and
        the log is an error so that a developer is told (#442)."""
        self.tagged()
        with (
            patch.object(
                casebody,
                "build",
                side_effect=casebody.OpinionTypeError("'brown, j.'"),
            ),
            self.assertLogs("scanning.views_process", level="ERROR") as logs,
        ):
            response = self.client.get(self.url("serve_opinion_final_xml"))

        self.assertEqual(response.status_code, 409)
        self.assertIn("brown, j.", response.json()["message"])
        self.assertIn("does not read yet", response.json()["message"])
        self.assertIn("brown, j.", logs.output[0])

    def test_spans_that_do_not_fit_the_text_are_a_409(self):
        self.tagged()
        self.stored[self.opinion.tag_key]["spans"] = [span(9, 0, 1, "court")]

        response = self.client.get(self.url("serve_opinion_final_xml"))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")

    def test_a_new_approval_takes_the_xml_away(self):
        self.tagged()
        self.approve_again("Another text.")

        self.assertEqual(
            self.client.get(self.url("serve_opinion_final_xml")).status_code,
            404,
        )

    def test_an_opinion_of_another_scan_is_404(self):
        self.tagged()

        response = self.client.get(
            reverse(
                "serve_opinion_final_xml",
                kwargs={
                    "pk": self.scan.pk + 1000,
                    "opinion_pk": self.opinion.pk,
                },
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_the_review_page_links_the_page_once_tagged(self):
        page = reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        self.assertNotContains(self.client.get(page), "Display final XML")

        self.tagged()

        response = self.client.get(page)
        self.assertContains(response, "Display final XML")
        self.assertContains(response, self.url("opinion_final_xml"))

    def test_a_text_in_review_has_no_link(self):
        self.tagged()
        self.opinion.status = OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        self.opinion.save(update_fields=["status"])

        response = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

        self.assertNotContains(response, "Display final XML")

    def test_the_file_index_names_the_xml(self):
        response = self.client.get(self.url("opinion_file_index"))
        entry = next(
            f
            for f in response.json()["files"]
            if f["output"] == "opinion-final-xml"
        )
        self.assertFalse(entry["written"])
        self.assertNotIn("url", entry)

        self.tagged()

        response = self.client.get(self.url("opinion_file_index"))
        entry = next(
            f
            for f in response.json()["files"]
            if f["output"] == "opinion-final-xml"
        )
        self.assertTrue(entry["written"])
        self.assertTrue(entry["computed"])
        self.assertEqual(entry["url"], self.url("serve_opinion_final_xml"))
