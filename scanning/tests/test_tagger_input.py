"""Tests for the tagger input converter (``scanning/tagger_input.py``).

Pure functions over dicts, so no database: a small synthetic dots.mocr
volume document and a handful of reviewed boxes shaped like
``Detection`` rows. Under test is what the worker never sees and has to
be right before anything is sent: which cells are text, where an
opinion starts and ends, what is held out and why, and that the map's
character ranges index the text that was built.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from scanning import tagger_input as ti

#: dots.mocr's frame for a 200 dpi letter page.
W, H = 1708, 2212
#: The detection frame (a 200 dpi render of the same page).
DW, DH = 1700, 2200


def cell(category, text, x0, y0, x1, y1):
    return {"category": category, "bbox": [x0, y0, x1, y1], "text": text}


def page(index, cells, *, cite=None, head=None):
    """One page: a running head in the band, then the cells given.

    :param cite: The printed page this page cites as a first page, or
        None for a page inside an opinion.
    :param head: The running head text; default names the reporter.
    """
    band = []
    if cite is not None:
        band.append(
            cell(
                "Page-header",
                f"Cite as 100 X.2d {cite} (Ct. 2020)",
                400,
                60,
                1300,
                100,
            )
        )
    else:
        band.append(
            cell("Page-header", head or "100 X.2d 10", 400, 60, 1300, 100)
        )
    band.append(cell("Page-header", str(10 + index), 1500, 60, 1600, 100))
    return {
        "page_index": index,
        "pdf_page": index + 1,
        "input_width": W,
        "input_height": H,
        "origin_width": W,
        "origin_height": H,
        "cells": band + cells,
    }


def document():
    """Three pages: page 0 holds the end of an opinion, a key symbol,
    then a caption and body of the next; page 1 continues it with a
    footnote; page 2 is the tail with a figure."""
    return {
        "schema_version": 1,
        "engine": "dots_mocr",
        "action": "parse",
        "scan_pk": 7,
        "run": 2,
        "source_page_count": 3,
        "pages": [
            page(
                0,
                [
                    cell(
                        "Text", "The judgment is affirmed.", 150, 300, 800, 340
                    ),
                    # West's key symbol: a small picture in the left column.
                    cell("Picture", "", 380, 380, 590, 465),
                    cell("Text", "Jane ROE, Appellant,", 150, 520, 800, 560),
                    cell("Text", "v.", 150, 580, 800, 620),
                    cell("Text", "STATE, Appellee.", 150, 640, 800, 680),
                    cell("Text", "No. 24-123", 150, 700, 800, 740),
                    cell(
                        "Text",
                        "Opinion by *Judge* Smith.¹",
                        150,
                        800,
                        800,
                        840,
                    ),
                    # Right column.
                    cell("Text", "The facts are these.", 900, 300, 1550, 340),
                    cell(
                        "Text",
                        "[1] West headnote text to be redacted.",
                        900,
                        400,
                        1550,
                        440,
                    ),
                    cell("Footnote", "1. The footnote.", 150, 1900, 800, 1940),
                ],
                cite=11,
            ),
            page(
                1,
                [
                    cell(
                        "Text",
                        "More reasoning fol-\nlows here.",
                        150,
                        300,
                        800,
                        340,
                    ),
                    cell(
                        "Text",
                        "2. A footnote dots filed as text.",
                        150,
                        1900,
                        800,
                        1940,
                    ),
                ],
            ),
            page(
                2,
                [
                    cell("Text", "Reversed.", 150, 300, 800, 340),
                    cell("Picture", "", 150, 500, 1500, 1500),  # a figure
                ],
            ),
        ],
    }


def box(page_index, label, x0, y0, x1, y1):
    return {
        "page_index": page_index,
        "label": label,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "img_width": DW,
        "img_height": DH,
    }


def boxes():
    return [
        box(0, "KEY_ICON", 370, 375, 600, 470),
        box(0, "CASE_CAPTION", 140, 510, 810, 750),
        box(0, "FOOTNOTES", 140, 1880, 810, 1960),
        box(0, "PAGE_HEADER", 390, 50, 1310, 110),
        box(1, "FOOTNOTES", 140, 1880, 810, 1960),
        box(2, "IMAGE", 140, 490, 1510, 1510),
    ]


class TestRender(SimpleTestCase):
    def test_markdown_becomes_the_models_inline_html(self):
        r = ti.render("The *Court* held **so**.¹² See <u>id</u>.")
        self.assertEqual(
            r.html, "The <em>Court</em> held so.<sup>12</sup> See id."
        )
        self.assertEqual(r.text, "The Court held so.12 See id.")
        self.assertEqual(r.marks, ["12"])

    def test_line_breaks_and_hyphenation(self):
        self.assertEqual(
            ti.render("rea-\nsoning fol-\nlows").html, "reasoning follows"
        )
        # A real hyphen after a known prefix stays.
        self.assertEqual(ti.render("non-\nparty").html, "non-party")

    def test_text_is_escaped(self):
        self.assertEqual(
            ti.render("Smith & Jones <3").html, "Smith &amp; Jones &lt;3"
        )

    def test_footnote_label_is_split(self):
        self.assertEqual(
            ti.split_footnote_label("1. The note.", loose=True),
            ("1", "The note."),
        )
        self.assertEqual(
            ti.split_footnote_label("continues here", loose=True),
            (None, "continues here"),
        )
        # A citation is not a label.
        self.assertEqual(
            ti.split_footnote_label("28 U.S.C. § 1291", loose=True)[0], None
        )


class TestLoadVolume(SimpleTestCase):
    def test_cells_become_blocks_in_reading_order(self):
        vol = ti.load_volume(document())
        kinds = [b.kind for b in vol.blocks if b.page_index == 0]
        # Head band first, then left column, then right column, footnotes last.
        self.assertEqual(kinds[:2], ["header", "header"])
        texts = [
            b.text
            for b in vol.blocks
            if b.page_index == 0 and b.kind == "text"
        ]
        self.assertEqual(texts[0], "The judgment is affirmed.")
        self.assertEqual(texts[-1], "[1] West headnote text to be redacted.")
        self.assertEqual(
            [b.kind for b in vol.blocks if b.page_index == 0][-1], "footnote"
        )

    def test_printed_pages_come_from_the_scan_not_the_head_band(self):
        vol = ti.load_volume(document(), {0: "10", 1: "11", 2: None})
        self.assertEqual(
            [vol.pages[i].printed_page for i in range(3)], ["10", "11", None]
        )
        # Nothing in the head band is read for it.
        self.assertIsNone(ti.load_volume(document()).pages[0].printed_page)

    def test_a_page_with_only_a_transcript_is_read_from_markdown(self):
        doc = document()
        doc["pages"][1]["cells"] = None
        doc["pages"][1]["md"] = (
            "100 X.2d 11\n\nBody from the transcript.\n\n2. A note."
        )
        vol = ti.load_volume(doc)
        self.assertEqual(vol.meta["pages_from_markdown"], [1])
        self.assertIn(
            "Body from the transcript.", [b.text for b in vol.blocks]
        )


class TestConvert(SimpleTestCase):
    def convert(self, dets=None, **kwargs):
        return ti.convert(
            document(), boxes() if dets is None else dets, scan_pk=7, **kwargs
        )

    def test_a_key_icon_closes_an_opinion_and_the_caption_opens_the_next(self):
        inp, mp, stats = self.convert()
        self.assertEqual(len(inp["sequences"]), 2)
        first, second = inp["sequences"]
        self.assertEqual(first["text"], "<p>The judgment is affirmed.</p>")
        self.assertTrue(
            second["text"].startswith("<p>Jane ROE, Appellant,</p>\n<p>v.</p>")
        )
        self.assertEqual(mp["sequences"][1]["opened_by"], "key_icon")
        self.assertEqual(stats["key_icons"], 1)
        # The caption cells carry their box on the map.
        self.assertEqual(
            [b["box"] for b in mp["sequences"][1]["blocks"][:4]],
            ["CASE_CAPTION"] * 4,
        )

    def test_the_map_ranges_index_the_text(self):
        inp, mp, _ = self.convert()
        for seq, m in zip(inp["sequences"], mp["sequences"], strict=True):
            for b in m["blocks"]:
                piece = seq["text"][b["start"] : b["end"]]
                self.assertTrue(
                    piece.startswith("<p>") and piece.endswith("</p>"), piece
                )
            self.assertEqual(seq["id"], m["id"])

    def test_footnotes_are_held_out_and_listed(self):
        inp, mp, stats = self.convert()
        text = "\n".join(s["text"] for s in inp["sequences"])
        self.assertNotIn("The footnote.", text)
        self.assertNotIn("A footnote dots filed as text.", text)
        # The mark stays in the body, the wire for the assembly step.
        self.assertIn("Smith.<sup>1</sup>", text)
        held = mp["sequences"][1]["footnotes"]
        self.assertEqual(
            [(f["page_index"], f["label"]) for f in held], [(0, "1"), (1, "2")]
        )
        # The cell dots filed as text was reclassified by the box.
        self.assertEqual(stats["footnote_cells"], 1)

    def test_furniture_and_key_icon_cells_are_not_sent(self):
        inp, _, _ = self.convert()
        text = "\n".join(s["text"] for s in inp["sequences"])
        self.assertNotIn("Cite as", text)
        self.assertNotIn("100 X.2d", text)

    def test_an_image_box_is_recorded_not_sent(self):
        _, mp, stats = self.convert()
        self.assertEqual(stats["images"], 1)
        images = mp["sequences"][1]["images"]
        self.assertEqual(images[0]["page_index"], 2)
        self.assertEqual(images[0]["frame"], [DW, DH])

    def test_a_redaction_rect_removes_the_cell(self):
        rects = {0: [(890.0, 390.0, 1560.0, 450.0)]}
        inp, _, stats = self.convert(redaction_rects=rects)
        text = "\n".join(s["text"] for s in inp["sequences"])
        self.assertNotIn("West headnote", text)
        self.assertIn("The facts are these.", text)
        self.assertEqual(stats["redacted_cells"], 1)

    def test_the_reviewed_boundaries_override_the_cuts(self):
        # One boundary from the caption's corner on page 0 to the
        # figure's corner on page 2: one opinion, and the tail of the
        # earlier one before the caption is in no boundary.
        inp, mp, stats = self.convert(
            boundaries=[((0, 140.0, 510.0), (2, 1500.0, 1500.0))]
        )
        self.assertEqual(len(inp["sequences"]), 1)
        self.assertTrue(
            inp["sequences"][0]["text"].startswith(
                "<p>Jane ROE, Appellant,</p>"
            )
        )
        self.assertNotIn("affirmed", inp["sequences"][0]["text"])
        self.assertEqual(mp["opinions_from"], "boundaries")
        self.assertEqual(stats["blocks_outside_boundaries"], 1)

    def test_two_boundaries_give_the_cut_the_key_icon_gives(self):
        # The first ends at the icon's bottom-right, the second opens at
        # the caption: the same two sequences the icon cut produces.
        by_icon, _, _ = self.convert()
        inp, mp, stats = self.convert(
            boundaries=[
                ((0, 150.0, 300.0), (0, 600.0, 470.0)),
                ((0, 140.0, 510.0), (2, 1500.0, 1500.0)),
            ]
        )
        self.assertEqual(
            [s["text"] for s in inp["sequences"]],
            [s["text"] for s in by_icon["sequences"]],
        )
        self.assertEqual(stats["blocks_outside_boundaries"], 0)

    def test_an_anchor_after_every_block_opens_on_the_next_page(self):
        vol = ti.load_volume(document())
        # At the foot of the right column nothing on the page follows.
        ranges = ti.ranges_from_anchors(
            vol, [((0, 1500.0, 2100.0), (2, 1500.0, 1500.0))]
        )
        first_on_page_1 = min(b.idx for b in vol.blocks if b.page_index == 1)
        self.assertEqual(ranges[0][0], first_on_page_1)

    def test_the_right_column_follows_the_foot_of_the_left(self):
        vol = ti.load_volume(document())
        ranges = ti.ranges_from_anchors(
            vol, [((0, 150.0, 2100.0), (2, 1500.0, 1500.0))]
        )
        opened = vol.blocks[ranges[0][0]]
        self.assertEqual(opened.raw, "The facts are these.")

    def test_ranges_are_clipped_at_the_next_start(self):
        vol = ti.load_volume(document())
        # The first boundary claims the whole volume; the second starts
        # at the caption, so the first ends there.
        ranges = ti.ranges_from_anchors(
            vol,
            [
                ((0, 150.0, 300.0), (2, 1500.0, 1500.0)),
                ((0, 140.0, 510.0), (2, 1500.0, 1500.0)),
            ],
        )
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0][1], ranges[1][0])

    def test_points_to_frame(self):
        self.assertEqual(ti.points_to_frame(72.0, 36.0), (200.0, 100.0))

    def test_without_boxes_the_volume_is_one_sequence(self):
        # No reviewed rows, no boundaries: a test shape, never a
        # production one. dots' own Footnote category still holds the
        # note out; the note dots filed as text stays text.
        inp, mp, _ = self.convert(dets=[])
        self.assertEqual(len(inp["sequences"]), 1)
        self.assertEqual(mp["sequences"][0]["opened_by"], "start")
        text = inp["sequences"][0]["text"]
        self.assertNotIn("The footnote.", text)
        self.assertIn("A footnote dots filed as text.", text)

    def test_a_caption_box_does_not_open_an_opinion_on_its_own(self):
        # bl_warm draws caption boxes on the rows of an orders table
        # too; only an icon closes an opinion.
        dets = [b for b in boxes() if b["label"] != "KEY_ICON"]
        doc = document()
        doc["pages"][0]["cells"] = [
            c for c in doc["pages"][0]["cells"] if c["category"] != "Picture"
        ]
        inp, _, _ = ti.convert(doc, dets, scan_pk=7)
        self.assertEqual(len(inp["sequences"]), 1)

    def test_the_digest_follows_the_text(self):
        inp, _, _ = self.convert()
        other, _, _ = self.convert(
            redaction_rects={0: [(890.0, 390.0, 1560.0, 450.0)]}
        )
        self.assertEqual(ti.text_digest(inp), ti.text_digest(inp))
        self.assertNotEqual(ti.text_digest(inp), ti.text_digest(other))

    def test_stats_and_map_meta(self):
        inp, mp, stats = self.convert(
            reporter="X.2d",
            reporter_volume=100,
            printed_pages={0: "10", 1: "11", 2: "12"},
        )
        self.assertEqual(mp["sequences"][1]["printed_pages"], ["10", "12"])
        self.assertEqual(mp["sequences"][1]["blocks"][0]["printed_page"], "10")
        self.assertEqual(stats["opinions"], 2)
        self.assertEqual(
            stats["chars"], sum(len(s["text"]) for s in inp["sequences"])
        )
        self.assertEqual(mp["reporter"], "X.2d")
        self.assertEqual(mp["reporter_volume"], 100)
        self.assertEqual(mp["ocr_run"], 2)
        self.assertEqual(mp["opinions_from"], "key_icons")
        self.assertEqual(mp["converter_version"], ti.CONVERTER_VERSION)
