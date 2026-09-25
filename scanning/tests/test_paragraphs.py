"""Tests for the approved text of an opinion: the flow (issue #375).

The approval turns the groups of the ensemble document into a flow of
paragraphs and a list of footnotes. A paragraph the layout cut at a
column or a page edge is put together again; every break the text
makes stays. The documents here are built by hand, in the shape of
``ensemble.build_page`` (schema 8).
"""

import ast
import pathlib

from django.test import TestCase

from scanning import ensemble, paragraphs


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
