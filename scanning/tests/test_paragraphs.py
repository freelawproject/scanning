"""Tests for the approved text of an opinion: the flow (issue #375).

The approval turns the groups of the ensemble document into a flow of
paragraphs and a list of footnotes. A paragraph the layout cut at a
column or a page edge is put together again; every break the text
makes stays. The documents here are built by hand, in the shape of
``ensemble.build_page`` (schema 9).
"""

import ast
import pathlib

from django.test import TestCase

from scanning import ensemble, markup, paragraphs


def group(
    gid, text, column: str | None = "L", band="body", section="text", **fields
):
    """One kept group of an ensemble page."""
    return {
        "id": gid,
        "text": text,
        "column": column,
        "band": band,
        "section": section,
        "kind": fields.pop("kind", "paragraph"),
        "blockquote": fields.pop("blockquote", False),
        "marks": fields.pop("marks", []),
        "human": fields.pop("human", None),
        **fields,
    }


def drop(after, band="body", section="text", column="L"):
    """One dropped group, after the kept group of id ``after``."""
    return {"after": after, "band": band, "section": section, "column": column}


def page(number, groups=(), dropped=(), error=None):
    """One page of an ensemble document."""
    entry = {
        "page_in_opinion": number,
        "page_index": 10 + number,
        "groups": list(groups),
        "dropped": list(dropped),
    }
    if error:
        entry["error"] = error
    return entry


def document(*pages):
    return {
        "schema_version": ensemble.SCHEMA_VERSION,
        "scan_pk": 7,
        "opinion": {"first_printed_page": 502, "index_in_page": 0},
        "edit_revision": 2,
        "engines": ["dots_mocr", "mistral_ocr"],
        "pages": list(pages),
    }


def texts(entries):
    return [entry["text"] for entry in entries]


class TestContinues(TestCase):
    """Condition 4: the text says the sentence goes on."""

    def test_no_end_punctuation_goes_on(self):
        self.assertTrue(paragraphs.continues("the court held that", "The"))

    def test_a_period_and_a_capital_is_a_break(self):
        self.assertFalse(paragraphs.continues("It is affirmed.", "The"))

    def test_a_period_before_a_closing_quote_is_a_break(self):
        self.assertFalse(paragraphs.continues('he said "no."', "The"))
        self.assertFalse(paragraphs.continues("(see id.)", "The"))

    def test_a_footnote_mark_after_the_period_is_a_sentence_end(self):
        """The engine did not mark the number: "so.12" still ends."""
        self.assertFalse(paragraphs.continues("The Court held so.12", "The"))
        self.assertFalse(paragraphs.continues("held so.\u00b9\u00b2", "The"))
        self.assertFalse(paragraphs.continues('it "failed."3', "The"))

    def test_a_number_after_a_space_is_no_sentence_end(self):
        """A citation puts a space before its number."""
        self.assertTrue(
            paragraphs.continues("as held in 536 U.S., at p. 12", "The")
        )

    def test_a_word_cut_by_a_hyphen_goes_on(self):
        self.assertTrue(paragraphs.continues("the defen-", "dant"))

    def test_a_lowercase_start_goes_on_after_a_period(self):
        """An abbreviation ends in a period: "U.S." then "and"."""
        self.assertTrue(paragraphs.continues("see 12 U.S.", "and cases"))
        self.assertTrue(paragraphs.continues("a colon:", '"the rule'))

    def test_an_empty_side_is_a_break(self):
        self.assertFalse(paragraphs.continues("", "the"))
        self.assertFalse(paragraphs.continues("the", "  "))


class TestTheBody(TestCase):
    """The join rule over the body groups."""

    def test_a_column_break_in_a_sentence_joins(self):
        doc = document(
            page(
                0,
                [
                    group(0, "The court held"),
                    group(
                        1,
                        "that the lease ended.",
                        column="R",
                        marks=[{"start": 0, "end": 4, "kind": "em"}],
                    ),
                ],
            )
        )

        (entry,) = paragraphs.body(doc)

        self.assertEqual(
            entry["text"], "The court held\nthat the lease ended."
        )
        self.assertEqual(entry["joins"], [{"offset": 15, "at": "column"}])
        self.assertEqual(entry["page_breaks"], [])
        self.assertEqual(
            entry["marks"], [{"start": 15, "end": 19, "kind": "em"}]
        )

    def test_a_page_break_in_a_sentence_joins_and_says_where(self):
        doc = document(
            page(0, [group(0, "A", column="R"), group(1, "the court")]),
            page(
                1,
                [
                    group(0, "SMITH v. JONES", band="head", column=None),
                    group(1, "held it."),
                ],
            ),
        )

        entries = paragraphs.body(doc)

        self.assertEqual(texts(entries), ["A", "the court\nheld it."])
        joined = entries[1]
        self.assertEqual(joined["pages"], [0, 1])
        self.assertEqual(
            joined["page_breaks"], [{"offset": 10, "page_in_opinion": 1}]
        )
        self.assertEqual(joined["joins"], [{"offset": 10, "at": "page"}])

    def test_the_running_head_is_furniture_and_not_body(self):
        doc = document(
            page(0, [group(0, "SMITH v. JONES", band="head", column=None)])
        )

        self.assertEqual(paragraphs.body(doc), [])
        (entry,) = paragraphs.page_table(doc, {10: "502"})
        self.assertEqual(entry["printed"], "502")
        self.assertEqual(
            entry["furniture"], [{"band": "head", "text": "SMITH v. JONES"}]
        )

    def test_a_hyphen_at_the_edge_joins_with_a_line_break(self):
        """The dehyphenation downstream reads it as a line end."""
        doc = document(
            page(0, [group(0, "the defen-"), group(1, "Dant", column="R")])
        )

        self.assertEqual(texts(paragraphs.body(doc)), ["the defen-\nDant"])

    def test_a_sentence_end_with_a_capital_next_is_a_break(self):
        doc = document(
            page(0, [group(0, "It ended."), group(1, "The next", column="R")])
        )

        self.assertEqual(
            texts(paragraphs.body(doc)), ["It ended.", "The next"]
        )

    def test_a_sup_mark_at_the_end_is_read_past(self):
        """A footnote mark after the period ends the sentence (#375)."""
        doc = document(
            page(
                0,
                [
                    group(
                        0,
                        "The Court held so.12",
                        marks=[{"start": 18, "end": 20, "kind": "sup"}],
                    ),
                    group(1, "The next paragraph starts here.", column="R"),
                ],
            )
        )

        self.assertEqual(
            texts(paragraphs.body(doc)),
            ["The Court held so.12", "The next paragraph starts here."],
        )

    def test_a_sup_mark_inside_a_sentence_still_joins(self):
        doc = document(
            page(
                0,
                [
                    group(
                        0,
                        "the rule12 of the",
                        marks=[{"start": 8, "end": 10, "kind": "sup"}],
                    ),
                    group(1, "Court held.", column="R"),
                ],
            )
        )

        self.assertEqual(len(paragraphs.body(doc)), 1)

    def test_a_redaction_between_is_a_break(self):
        doc = document(
            page(
                0,
                [group(0, "the court"), group(1, "held", column="R")],
                dropped=[drop(0, column="R")],
            )
        )

        self.assertEqual(texts(paragraphs.body(doc)), ["the court", "held"])

    def test_a_redaction_at_the_top_of_the_next_page_is_a_break(self):
        doc = document(
            page(0, [group(0, "the court")]),
            page(1, [group(0, "held")], dropped=[drop(None)]),
        )

        self.assertEqual(texts(paragraphs.body(doc)), ["the court", "held"])

    def test_a_dropped_page_number_is_no_break(self):
        """The page number the glue took (#396) is furniture."""
        doc = document(
            page(0, [group(0, "the court")], dropped=[drop(0, band="foot")]),
            page(1, [group(0, "held")], dropped=[drop(None, band="head")]),
        )

        self.assertEqual(texts(paragraphs.body(doc)), ["the court\nheld"])

    def test_two_groups_of_one_column_stay_apart(self):
        doc = document(page(0, [group(0, "the court"), group(1, "held")]))

        self.assertEqual(texts(paragraphs.body(doc)), ["the court", "held"])

    def test_a_heading_never_joins(self):
        doc = document(
            page(
                0,
                [
                    group(0, "OPINION", kind="heading"),
                    group(1, "the court", column="R"),
                ],
            )
        )

        self.assertEqual(len(paragraphs.body(doc)), 2)

    def test_a_syllabus_start_never_joins_and_carries_its_stamp(self):
        """A box over the first syllabus paragraph (#518): the join
        would hide the start inside the paragraph above."""
        doc = document(
            page(
                0,
                [
                    group(0, "the sentence goes on and"),
                    group(1, "on here", column="R", syllabus_start=True),
                    group(2, "then this", column="R"),
                ],
            )
        )

        entries = paragraphs.body(doc)

        self.assertEqual(
            [e["text"] for e in entries],
            ["the sentence goes on and", "on here", "then this"],
        )
        self.assertEqual(
            [e["syllabus_start"] for e in entries], [False, True, False]
        )

    def test_a_blockquote_joins_a_blockquote_alone(self):
        doc = document(
            page(
                0,
                [
                    group(0, "the statute says", blockquote=True),
                    group(1, "that a lease", column="R"),
                ],
            ),
            page(
                1,
                [
                    group(0, "no person shall", blockquote=True),
                    group(1, "enter the land", blockquote=True, column="R"),
                ],
            ),
        )

        entries = paragraphs.body(doc)

        self.assertEqual(
            texts(entries),
            [
                "the statute says",
                "that a lease",
                "no person shall\nenter the land",
            ],
        )
        self.assertTrue(entries[2]["blockquote"])

    def test_a_group_quoted_in_part_is_its_parts(self):
        """A curator quoted the first paragraph of a box that holds a
        quote and a paragraph of the body (#419): two paragraphs, each
        with its flag, and each with its own marks."""
        quote = '"(1) Dangerous weapon means any weapon."'
        rest = "Defendant points to several statutes."
        doc = document(
            page(
                0,
                [
                    group(
                        0,
                        f"{quote} {rest}",
                        quote_span=[0, len(quote)],
                        marks=[{"start": 5, "end": 14, "kind": "em"}],
                    )
                ],
            )
        )

        entries = paragraphs.body(doc)

        self.assertEqual(texts(entries), [quote, rest])
        self.assertEqual([e["blockquote"] for e in entries], [True, False])
        self.assertEqual(
            entries[0]["marks"], [{"start": 5, "end": 14, "kind": "em"}]
        )
        self.assertEqual(entries[1]["marks"], [])

    def test_only_the_first_part_joins_the_group_above(self):
        """A span that starts the text is a quote, so it joins a quote
        above it at a column edge, and the text after it never joins."""
        quote = "no person shall enter"
        rest = "The court held so."
        doc = document(
            page(
                0,
                [
                    group(0, "the statute says", blockquote=True),
                    group(
                        1,
                        f"{quote} {rest}",
                        column="R",
                        quote_span=[0, len(quote)],
                    ),
                ],
            )
        )

        entries = paragraphs.body(doc)

        self.assertEqual(texts(entries), [f"the statute says\n{quote}", rest])
        self.assertEqual([e["blockquote"] for e in entries], [True, False])

    def test_a_page_with_no_body_between_is_a_break(self):
        doc = document(
            page(0, [group(0, "the court")]),
            page(
                1,
                [group(0, "1. A note", band="footnotes", section="footnotes")],
            ),
            page(2, [group(0, "held")]),
        )

        self.assertEqual(texts(paragraphs.body(doc)), ["the court", "held"])

    def test_a_page_nobody_read_is_a_break(self):
        doc = document(
            page(0, [group(0, "the court")]),
            page(1, error="no engine read this page"),
        )
        doc["pages"].append(page(2, [group(0, "held")]))
        doc["pages"][2]["page_in_opinion"] = 1

        self.assertEqual(texts(paragraphs.body(doc)), ["the court", "held"])

    def test_a_human_group_marks_its_paragraph(self):
        doc = document(
            page(
                0,
                [
                    group(0, "the court"),
                    group(1, "held", column="R", human={"edit_id": 3}),
                ],
            )
        )

        (entry,) = paragraphs.body(doc)

        self.assertTrue(entry["human"])

    def test_three_columns_of_one_paragraph_are_one_entry(self):
        doc = document(
            page(0, [group(0, "one"), group(1, "two", column="R")]),
            page(1, [group(0, "three.")]),
        )

        (entry,) = paragraphs.body(doc)

        self.assertEqual(entry["text"], "one\ntwo\nthree.")
        self.assertEqual([j["at"] for j in entry["joins"]], ["column", "page"])


class TestTheFootnotes(TestCase):
    """The footnotes: a list keyed by label, with their paragraphs."""

    @staticmethod
    def note(gid, text, column="L", **fields):
        return group(
            gid,
            text,
            column=column,
            band="footnotes",
            section="footnotes",
            **fields,
        )

    def test_a_label_starts_a_footnote_and_leaves_the_text(self):
        doc = document(
            page(
                0,
                [
                    self.note(
                        0,
                        "1. See id.",
                        marks=[{"start": 3, "end": 10, "kind": "em"}],
                    ),
                    self.note(1, "2 Cf. Smith.", column="R"),
                ],
            )
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["1", "2"])
        self.assertEqual(notes[0]["paragraphs"][0]["text"], "See id.")
        self.assertEqual(
            notes[0]["paragraphs"][0]["marks"],
            [{"start": 0, "end": 7, "kind": "em"}],
        )
        self.assertEqual(notes[1]["paragraphs"][0]["text"], "Cf. Smith.")

    def test_a_sup_mark_is_a_label(self):
        doc = document(
            page(
                0,
                [
                    self.note(
                        0,
                        "3Sellers do not",
                        marks=[{"start": 0, "end": 1, "kind": "sup"}],
                    )
                ],
            )
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "3")
        self.assertEqual(note["paragraphs"][0]["text"], "Sellers do not")
        self.assertEqual(note["paragraphs"][0]["marks"], [])

    def test_a_symbol_is_a_label(self):
        doc = document(page(0, [self.note(0, "* Reporter's note.")]))

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "*")

    def test_the_first_group_of_a_page_goes_on_with_the_last_note(self):
        doc = document(
            page(0, [self.note(0, "1. The rule of the")]),
            page(1, [self.note(0, "court is plain."), self.note(1, "2. Id.")]),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["1", "2"])
        first = notes[0]
        self.assertEqual(len(first["paragraphs"]), 1)
        self.assertEqual(
            first["paragraphs"][0]["text"], "The rule of the\ncourt is plain."
        )
        self.assertEqual(first["pages"], [0, 1])
        self.assertEqual(
            first["paragraphs"][0]["page_breaks"],
            [{"offset": 16, "page_in_opinion": 1}],
        )

    def test_a_second_paragraph_of_a_note_stays_apart(self):
        doc = document(
            page(
                0,
                [
                    self.note(0, "1. It ended."),
                    self.note(1, "The next.", column="R"),
                ],
            )
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(texts(note["paragraphs"]), ["It ended.", "The next."])

    def test_a_group_before_any_label_is_kept_with_no_label(self):
        doc = document(
            page(
                0, [self.note(0, "of the prior page."), self.note(1, "4. Id.")]
            )
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], [None, "4"])
        self.assertEqual(
            notes[0]["paragraphs"][0]["text"], "of the prior page."
        )

    def test_a_number_far_past_the_last_label_is_text(self):
        """A continuation can start with a number: "28 U.S.C. 1291"."""
        doc = document(
            page(0, [self.note(0, "3. The appeal lies under")]),
            page(1, [self.note(0, "28 U.S.C. 1291.")]),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "3")
        self.assertEqual(
            note["paragraphs"][0]["text"],
            "The appeal lies under\n28 U.S.C. 1291.",
        )

    def test_a_number_the_body_cites_is_a_label_past_the_step(self):
        """One label the engines missed does not end the series: notes
        3 and 4 read as one block leave the last label at 3, and note 8
        is still a note because the body cites it on its page."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "3"),
                    self.cited(1, "B.", "7"),
                    self.note(2, "3. Three. 4. Four. 5. Five. 6. Seven."),
                ],
            ),
            page(
                1,
                [
                    self.cited(0, "C.", "8"),
                    self.cited(1, "D.", "9"),
                    self.note(2, "8. Eight."),
                    self.note(3, "9. Nine.", column="R"),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["3", "8", "9"])

    def test_a_far_number_cited_on_another_page_is_text(self):
        """A continuation that starts with a number the body cites far
        away is still the rest of the note before it."""
        doc = document(
            page(0, [self.cited(0, "A.", "28")]),
            page(1, [group(0, "B.")]),
            page(
                2,
                [self.cited(0, "C.", "1"), self.note(1, "1. See generally")],
            ),
            page(3, [self.note(0, "28 U.S.C. § 1291.")]),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "1")

    def test_a_label_inside_a_group_the_body_cites_cuts_the_group(self):
        """The engines read the short notes of a page as two blocks cut
        at the column edge: each label the body cites starts its note,
        and the rest of note 4 in the next column goes on with it."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "3"),
                    self.cited(1, "B.", "4"),
                    self.cited(2, "C.", "5"),
                    self.cited(3, "D.", "6"),
                    self.note(4, "3. Three. 4. Four goes"),
                    self.note(
                        5,
                        "on here. 5. Five, 6. Six.",
                        column="R",
                        marks=[{"kind": "em", "start": 12, "end": 16}],
                    ),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["3", "4", "5", "6"])
        self.assertEqual(
            [n["paragraphs"][0]["text"] for n in notes],
            ["Three.", "Four goes\non here.", "Five,", "Six."],
        )
        self.assertEqual(
            notes[2]["paragraphs"][0]["marks"],
            [{"kind": "em", "start": 0, "end": 4}],
        )

    def test_a_number_inside_a_group_the_body_does_not_cite_is_text(self):
        """A sentence that ends in a number is no note without its mark,
        and "ECF No. 4." holds no label even with one."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "3"),
                    self.note(1, "3. See page 2. 4. The rule."),
                ],
            ),
            page(
                1,
                [
                    self.cited(0, "B.", "4"),
                    self.cited(1, "C.", "5"),
                    self.note(2, "4. See ECF No. 5. The brief."),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["3", "4"])

    def test_a_footnote_is_no_heading_and_keeps_no_heading_bold(self):
        """One engine read the first line of a note as a heading: the
        kind and the bold over the whole group go; the bold of a part
        of it is the print's, and stays."""
        doc = document(
            page(
                0,
                [
                    self.note(
                        0,
                        "1. Section 5 provides:",
                        kind="heading",
                        marks=[{"kind": "strong", "start": 0, "end": 22}],
                    ),
                    self.note(
                        1,
                        "2. ORPC 1.1 provides:",
                        column="R",
                        kind="heading",
                        marks=[{"kind": "strong", "start": 0, "end": 11}],
                    ),
                ],
            ),
        )

        first, second = paragraphs.footnotes(doc)

        self.assertEqual(first["paragraphs"][0]["kind"], "paragraph")
        self.assertEqual(first["paragraphs"][0]["marks"], [])
        self.assertEqual(
            second["paragraphs"][0]["marks"],
            [{"kind": "strong", "start": 0, "end": 8}],
        )
        self.assertEqual(second["paragraphs"][0]["kind"], "paragraph")

    def test_the_notes_of_the_opinion_before_do_not_take_the_first_note(
        self,
    ):
        """The first page holds the last notes of the opinion before:
        the body's first mark starts the opinion's own series."""
        doc = document(
            page(0, [group(0, "A caption."), self.note(1, "26. The last.")]),
            page(
                1,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "For two reasons.", "2"),
                    self.note(2, "1. One."),
                    self.note(3, "2. Two.", column="R"),
                ],
            ),
            page(2, [self.note(0, "1 U.S.C. § 1 says so.")]),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["26", "1", "2"])
        self.assertEqual(len(notes[2]["paragraphs"]), 2)

    def test_a_list_label_does_not_break_a_note_at_the_column(self):
        """One engine labels a numbered note a list item: the half in
        the next column is a paragraph, and the note is still one."""
        doc = document(
            page(
                0,
                [
                    self.note(0, "1. The presen-", kind="list_item"),
                    self.note(1, "tence report says so.", column="R"),
                ],
            ),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(
            [p["text"] for p in note["paragraphs"]],
            ["The presen-\ntence report says so."],
        )
        self.assertEqual(note["paragraphs"][0]["kind"], "paragraph")

    def test_a_label_the_body_cites_on_the_page_before_is_a_label(self):
        """A mark in the last line of a page sends its note to the next
        page: the label past the step is still a label there."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "1"),
                    self.cited(1, "B.", "8"),
                    self.note(2, "1. One."),
                ],
            ),
            page(1, [group(0, "C."), self.note(1, "8. Eight.")]),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["1", "8"])

    def test_the_labels_inside_a_group_go_on_from_its_own_far_label(self):
        """A group that starts with a label past the step, proved by the
        body, is cut at the labels that follow that label."""
        doc = document(
            page(0, [self.cited(0, "A.", "3"), self.note(1, "3. Three.")]),
            page(
                1,
                [
                    self.cited(0, "B.", "8"),
                    self.cited(1, "C.", "9"),
                    self.note(2, "8. Eight. 9. Nine."),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["3", "8", "9"])
        self.assertEqual(notes[2]["paragraphs"][0]["text"], "Nine.")

    def test_a_cited_number_two_ahead_inside_a_group_is_text(self):
        """A cut takes the last label plus one and no other. "See App.
        10, 14. The witness" inside note 12 is no note 14, though the
        body cites 14: notes 13 and 14 are groups of their own."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "12"),
                    self.cited(1, "B.", "13"),
                    self.cited(2, "C.", "14"),
                    self.note(3, "12. See the record. 14. The witness said."),
                    self.note(4, "13. Thirteen."),
                    self.note(5, "14. Fourteen.", column="R"),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["12", "13", "14"])
        self.assertEqual(
            notes[0]["paragraphs"][0]["text"],
            "See the record. 14. The witness said.",
        )

    def test_a_number_of_a_citation_is_no_note_before_the_real_one(self):
        """A sentence of note 12 goes on with "13. The" and the body cites 13:
        the real note 13 is the next group, so the number stays text
        and the mark links to the real note."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "12"),
                    self.cited(1, "B.", "13"),
                    self.note(2, "12. See the order, supra. 13. The court."),
                    self.note(3, "13. Thirteen.", column="R"),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["12", "13"])
        self.assertEqual(notes[1]["paragraphs"][0]["text"], "Thirteen.")
        self.assertEqual(
            notes[0]["paragraphs"][0]["text"],
            "See the order, supra. 13. The court.",
        )

    def test_a_cited_number_a_later_group_owns_starts_no_note(self):
        """The rest of note 5 starts with "9 F.3d" and the body cites 9
        on the page: the real note 9 is a group further down, so the
        line stays in note 5 and notes 6 to 9 are notes."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "5"),
                    self.cited(1, "B.", "6"),
                    self.cited(2, "C.", "7"),
                    self.cited(3, "D.", "8"),
                    self.cited(4, "E.", "9"),
                    self.note(5, "5. See Smith v. Jones,"),
                    self.note(6, "9 F.3d 100 (1st Cir. 1993).", column="R"),
                    self.note(7, "6. Six.", column="R"),
                    self.note(8, "7. Seven.", column="R"),
                    self.note(9, "8. Eight.", column="R"),
                    self.note(10, "9. Nine.", column="R"),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual(
            [n["label"] for n in notes], ["5", "6", "7", "8", "9"]
        )
        self.assertIn("9 F.3d 100", notes[0]["paragraphs"][0]["text"])
        self.assertEqual(notes[4]["paragraphs"][0]["text"], "Nine.")

    def test_a_cited_number_past_the_step_still_starts_a_note(self):
        """The guard reads the later groups alone: a label the body
        cites, with no later group of its number, starts its note."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "5"),
                    self.cited(1, "B.", "9"),
                    self.note(2, "5. Five."),
                    self.note(3, "9. Nine.", column="R"),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["5", "9"])

    def test_a_number_out_of_the_series_inside_a_group_is_text(self):
        """A cut inside a group takes the next numbers of the series
        alone, whatever the body cites: "9." after note 3 stays text."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "3"),
                    self.cited(1, "B.", "9"),
                    self.note(2, "3. Three. 9. The rest."),
                ],
            ),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "3")
        self.assertEqual(note["paragraphs"][0]["text"], "Three. 9. The rest.")

    def test_a_cut_keeps_every_mark_on_its_own_note(self):
        """The marks of a cut group go to the note that holds them, in
        the offsets of that note's text."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "1"),
                    self.cited(1, "B.", "2"),
                    self.note(
                        2,
                        "1. See Roe. 2. See Doe.",
                        marks=[
                            {"kind": "em", "start": 7, "end": 10},
                            {"kind": "em", "start": 19, "end": 22},
                        ],
                    ),
                ],
            ),
        )

        first, second = paragraphs.footnotes(doc)

        self.assertEqual(first["paragraphs"][0]["text"], "See Roe.")
        self.assertEqual(
            first["paragraphs"][0]["marks"],
            [{"kind": "em", "start": 4, "end": 7}],
        )
        self.assertEqual(second["paragraphs"][0]["text"], "See Doe.")
        self.assertEqual(
            second["paragraphs"][0]["marks"],
            [{"kind": "em", "start": 4, "end": 7}],
        )

    def test_the_notes_of_the_opinion_before_on_the_page_of_the_mark(self):
        """An opinion that starts in the middle of a page: the last note
        of the opinion before and the first mark are on one page."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "For two reasons.", "2"),
                    self.note(2, "26. The last."),
                    self.note(3, "1. One."),
                    self.note(4, "2. Two.", column="R"),
                ],
            ),
            page(1, [self.note(0, "1 U.S.C. § 1 says so.")]),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["26", "1", "2"])
        self.assertEqual(len(notes[2]["paragraphs"]), 2)

    def test_the_first_mark_opens_no_series_two_pages_on(self):
        """The first note is on the page of its mark or the next: a
        small number further on is the rest of the note before it."""
        doc = document(
            page(0, [self.cited(0, "A.", "1"), self.note(1, "26. See")]),
            page(1, [group(0, "B.")]),
            page(2, [self.note(0, "1 U.S.C. § 1.")]),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "26")

    def test_the_first_notes_in_one_block_after_the_notes_before(self):
        """The opinion's own notes 1 and 2, read as one block under the
        last note of the opinion before, are both notes."""
        doc = document(
            page(0, [group(0, "A caption."), self.note(1, "26. The last.")]),
            page(
                1,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "For two reasons.", "2"),
                    self.note(2, "1. One. 2. Two."),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["26", "1", "2"])

    def test_with_no_note_before_the_first_mark_one_series_stands(self):
        """The first mark starts a series only after the notes of the
        opinion before: with none, a small number after note 5 is text."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "1"),
                    self.cited(1, "B.", "5"),
                    self.note(2, "5. See"),
                ],
            ),
            page(1, [self.note(0, "1 U.S.C. § 1.")]),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "5")

    def test_the_bold_over_a_whole_group_with_no_label_goes(self):
        """The rest of a note one engine read as a heading is plain."""
        doc = document(
            page(
                0,
                [
                    self.note(0, "1. Section 5 provides:"),
                    self.note(
                        1,
                        "The court shall decide.",
                        kind="heading",
                        marks=[{"kind": "strong", "start": 0, "end": 23}],
                    ),
                ],
            ),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(
            [(p["kind"], p["marks"]) for p in note["paragraphs"]],
            [("paragraph", []), ("paragraph", [])],
        )

    def test_a_heading_in_the_body_keeps_its_kind_and_its_bold(self):
        """The rule of the footnotes changes no group of the body."""
        doc = document(
            page(
                0,
                [
                    group(
                        0,
                        "III. DISCUSSION",
                        kind="heading",
                        marks=[{"kind": "strong", "start": 0, "end": 15}],
                    ),
                ],
            ),
        )

        (paragraph,) = paragraphs.body(doc)

        self.assertEqual(paragraph["kind"], "heading")
        self.assertEqual(
            paragraph["marks"], [{"kind": "strong", "start": 0, "end": 15}]
        )

    def test_the_body_marks_are_listed_by_number_and_page(self):
        """``mark_pages`` reads the numbered ``sup`` marks alone."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "1"),
                    self.cited(1, "B.", "*"),
                    self.note(2, "1. One."),
                ],
            ),
            page(1, [self.cited(0, "C.", "1"), self.cited(1, "D.", "2")]),
        )

        self.assertEqual(paragraphs.mark_pages(doc), {1: {0, 1}, 2: {1}})

    def test_a_label_inside_a_text(self):
        """``inline_labels``: the end of a sentence, the next number, a
        mark of the body, and no abbreviation before the number."""
        marked = {4: {0}, 5: {0}}
        cases = [
            ("Three. 4. Four.", 3, [7]),
            ("Three, 4. Four.", 3, [7]),
            ('He said "no." 4. Four.', 3, [14]),
            ("Three. 4) Four.", 3, [7]),
            ("Three. 4. Four. 5. Five.", 3, [7, 16]),
            ("See ECF No. 4. The brief.", 3, []),
            ("See id. at 12 n. 4. The rule.", 3, []),
            ("See p. 4. The rule.", 3, []),
            ("Three. 4. four.", 3, []),
            ("Three 4. Four.", 3, []),
            ("Three. 4 Four.", 3, []),
            ("Three. 6. Six.", 3, []),
            ("Three. 5. Five.", 3, []),
            ("See Fed. R. Civ. P. 4. The rule.", 3, []),
            ("See Doc. 4. The order.", 3, []),
            ("See Dkt. 4. The order.", 3, []),
            ("See Tr. 4. The witness.", 3, []),
            ("See App. 4. The record.", 3, []),
            ("See Def.'s Br. 4. The court.", 3, []),
            ("See Mem. 4. The court.", 3, []),
            ("See Slip Op. 4. The court.", 3, []),
            ("See Mot. 4. The court.", 3, []),
            ("See Smith Dep. 4. The witness.", 3, []),
            ("See Smith Decl. 4. The witness.", 3, []),
            ("See Id. at 2, 4. The rule.", 3, []),
            ("Id. 4. Four.", 3, [4]),
            ("Three. 4. Four.", 4, []),
            ("Three. 4. Four.", None, []),
        ]
        for text, last, cuts in cases:
            with self.subTest(text=text, last=last):
                self.assertEqual(
                    paragraphs.inline_labels(text, 0, last, 0, marked), cuts
                )

    def test_a_number_a_later_group_starts_with_is_no_label_inside(self):
        """``inline_labels`` leaves a number to the group that owns it."""
        self.assertEqual(
            paragraphs.inline_labels(
                "Three. 4. Four.", 0, 3, 0, {4: {0}}, frozenset({4})
            ),
            [],
        )

    def test_a_label_inside_a_text_needs_its_mark_near(self):
        """The mark is on the page of the group or the page before."""
        for page_of_mark, cuts in ((2, [7]), (1, [7]), (0, []), (3, [])):
            with self.subTest(page_of_mark=page_of_mark):
                self.assertEqual(
                    paragraphs.inline_labels(
                        "Three. 4. Four.", 0, 3, 2, {4: {page_of_mark}}
                    ),
                    cuts,
                )

    def test_a_skipped_number_is_still_a_label(self):
        """A footnote on a page a redaction took leaves a gap."""
        doc = document(
            page(
                0, [self.note(0, "3. Id."), self.note(1, "5. Id.", column="R")]
            )
        )

        self.assertEqual(
            [n["label"] for n in paragraphs.footnotes(doc)], ["3", "5"]
        )

    @staticmethod
    def cited(gid, text, label, column="L"):
        """A body group that ends in the footnote mark ``label``."""
        return group(
            gid,
            f"{text}{label}",
            column=column,
            marks=[
                {
                    "start": len(text),
                    "end": len(text) + len(label),
                    "kind": "sup",
                }
            ],
        )

    def test_a_dissent_that_numbers_from_one_again_starts_a_note(self):
        """A concurrence or a dissent numbers its notes from 1 (#442)."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "For two reasons.", "2"),
                    self.note(2, "1. The first note."),
                    self.note(3, "2. The second note.", column="R"),
                ],
            ),
            page(
                1,
                [
                    group(0, "Jones, J., dissenting."),
                    self.cited(1, "I would reverse.", "1"),
                    self.note(2, "1. The dissent's note."),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["1", "2", "1"])
        self.assertEqual(
            notes[2]["paragraphs"][0]["text"], "The dissent's note."
        )
        self.assertEqual(paragraphs.mark_restarts(doc), [("1", 1)])

    def test_with_no_restart_of_the_marks_a_small_number_is_text(self):
        """The rule of one series stands where the body numbers on: a
        continuation can start with a small number."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "For two reasons.", "2"),
                    self.note(2, "2. The rule has"),
                ],
            ),
            page(1, [self.note(0, "1 exception.")]),
        )

        (note,) = paragraphs.footnotes(doc)

        self.assertEqual(note["label"], "2")
        self.assertEqual(
            note["paragraphs"][0]["text"], "The rule has\n1 exception."
        )

    def test_a_continued_note_that_starts_with_another_small_number(self):
        """Only the label where the body marks start again takes the
        restart: "3 Am. Jur. 2d" goes on with the note before it, and the
        dissent's note 1 is still a note."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "1"),
                    self.cited(1, "B.", "2"),
                    self.cited(2, "C.", "3"),
                    self.cited(3, "D.", "4"),
                    self.note(4, "1. One."),
                    self.note(5, "2. Two."),
                    self.note(6, "3. Three."),
                    self.note(7, "4. See generally"),
                ],
            ),
            page(
                1,
                [
                    self.note(0, "3 Am. Jur. 2d Appeal § 5."),
                    group(1, "Jones, J., dissenting."),
                    self.cited(2, "I would reverse.", "1"),
                    self.note(3, "1. The dissent's note."),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual(
            [n["label"] for n in notes], ["1", "2", "3", "4", "1"]
        )
        self.assertEqual(
            notes[3]["paragraphs"][0]["text"],
            "See generally\n3 Am. Jur. 2d Appeal § 5.",
        )
        self.assertEqual(
            notes[4]["paragraphs"][0]["text"], "The dissent's note."
        )

    def test_a_continued_note_that_starts_with_the_restart_label(self):
        """A note starts on the page of its mark: "1 U.S.C. 1" on a page
        before the dissent's first mark goes on with the majority's note,
        and the dissent's note 1 is still a note."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "A.", "1"),
                    self.cited(1, "B.", "2"),
                    self.note(2, "1. One."),
                    self.note(3, "2. The statute is"),
                ],
            ),
            page(
                1,
                [
                    self.note(0, "1 U.S.C. 1, which reads."),
                    group(1, "More of the majority."),
                ],
            ),
            page(
                2,
                [
                    group(0, "Jones, J., dissenting."),
                    self.cited(1, "I would reverse.", "1"),
                    self.note(2, "1. The dissent's note."),
                ],
            ),
        )

        notes = paragraphs.footnotes(doc)

        self.assertEqual([n["label"] for n in notes], ["1", "2", "1"])
        self.assertEqual(
            notes[1]["paragraphs"][0]["text"],
            "The statute is\n1 U.S.C. 1, which reads.",
        )
        self.assertEqual(notes[2]["pages"], [2])
        self.assertEqual(
            notes[2]["paragraphs"][0]["text"], "The dissent's note."
        )

    def test_a_restart_is_taken_once(self):
        doc = document(
            page(
                0,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "For two reasons.", "2"),
                    self.cited(2, "I would reverse.", "1"),
                    self.note(3, "1. One."),
                    self.note(4, "2. Two."),
                    self.note(5, "1. The dissent's."),
                    self.note(6, "1 more of it.", column="R"),
                ],
            ),
        )

        self.assertEqual(
            [n["label"] for n in paragraphs.footnotes(doc)], ["1", "2", "1"]
        )

    def test_a_restart_past_a_redacted_note(self):
        """The dissent's note 1 went with a redaction: it starts at 2."""
        doc = document(
            page(
                0,
                [
                    self.cited(0, "We affirm.", "1"),
                    self.cited(1, "Again.", "2"),
                    self.cited(2, "Thrice.", "3"),
                    self.cited(3, "I would reverse.", "2"),
                    self.note(4, "1. One."),
                    self.note(5, "2. Two."),
                    self.note(6, "3. Three."),
                    self.note(7, "2. The dissent's second."),
                ],
            ),
        )

        self.assertEqual(
            [n["label"] for n in paragraphs.footnotes(doc)],
            ["1", "2", "3", "2"],
        )

    def test_the_body_holds_no_footnote(self):
        doc = document(page(0, [group(0, "Body."), self.note(1, "1. Note.")]))

        self.assertEqual(texts(paragraphs.body(doc)), ["Body."])
        self.assertEqual(len(paragraphs.footnotes(doc)), 1)


class TestTheApprovedDocument(TestCase):
    def test_the_object_holds_the_flow_and_the_page_table(self):
        doc = document(
            page(0, [group(0, "the court"), group(1, "held.", column="R")]),
            page(1, error="no engine read this page"),
        )

        text = paragraphs.approved_document(
            doc, {10: "502"}, "curator", "2026-09-25T17:00:00+00:00"
        )

        self.assertEqual(text["schema"], paragraphs.APPROVED_SCHEMA)
        self.assertEqual(text["join_rule"], paragraphs.JOIN_RULE)
        self.assertEqual(
            text["opinion"],
            {
                "first_printed_page": 502,
                "index_in_page": 0,
                "scan": 7,
                "edit_revision": 2,
            },
        )
        self.assertEqual(text["approved_by"], "curator")
        self.assertEqual(texts(text["body"]), ["the court\nheld."])
        self.assertEqual(text["footnotes"], [])
        self.assertEqual(
            [(p["printed"], p["error"]) for p in text["pages"]],
            [("502", None), (None, "no engine read this page")],
        )


class TestTheModuleIsPure(TestCase):
    """The flow reads the document alone, so a rule is a rewrite."""

    def test_it_imports_no_django_and_no_scanning_module(self):
        tree = ast.parse(pathlib.Path("scanning/paragraphs.py").read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        self.assertEqual(names, {"re"})

    def test_the_section_names_are_the_ensembles(self):
        self.assertEqual(paragraphs.BODY_SECTION, ensemble.BODY)
        self.assertEqual(paragraphs.FOOTNOTE_SECTION, ensemble.FOOTNOTES)

    def test_the_list_names_are_the_parse_s(self):
        self.assertEqual(paragraphs.ITEM, markup.ITEM)
        self.assertEqual(paragraphs.LIST_ITEM, markup.LIST_ITEM)


def item(start, end):
    return {"start": start, "end": end, "kind": "li"}


class TestTheListItems(TestCase):
    """A list item a column cut goes on; the next item starts (#428)."""

    def test_the_rest_of_an_item_goes_on_with_it(self):
        doc = document(
            page(
                0,
                [
                    group(
                        0,
                        "a 1997 conviction for possession of 2",
                        kind="list_item",
                        list="ul",
                        marks=[item(0, 37)],
                    ),
                    group(
                        1,
                        "grams or less; and a 1999 conviction.",
                        column="R",
                        kind="list_item",
                        marks=[item(19, 37)],
                    ),
                ],
            )
        )

        (paragraph,) = paragraphs.body(doc)

        self.assertEqual(paragraph["list"], "ul")
        self.assertEqual(
            [
                paragraph["text"][m["start"] : m["end"]]
                for m in paragraph["marks"]
            ],
            [
                "a 1997 conviction for possession of 2\ngrams or less; and",
                "a 1999 conviction.",
            ],
        )

    def test_a_group_that_starts_an_item_starts_a_paragraph(self):
        doc = document(
            page(
                0,
                [
                    group(
                        0,
                        "a 1997 conviction for possession; and",
                        kind="list_item",
                        list="ul",
                        marks=[item(0, 37)],
                    ),
                    group(
                        1,
                        "a 1999 conviction.",
                        column="R",
                        kind="list_item",
                        list="ul",
                        marks=[item(0, 18)],
                    ),
                ],
            )
        )

        self.assertEqual(
            texts(paragraphs.body(doc)),
            ["a 1997 conviction for possession; and", "a 1999 conviction."],
        )
