"""The pictures and tables the readers saw (``pictures``): the rows, the
cards and the fit of the model's image boxes."""

from unittest import mock

from blackletter.models import Label
from django.urls import reverse

from scanning import card_review, findings, opinion_ocr, pictures
from scanning.models import CheckName, Detection, Issue, PictureReading
from scanning.tests.test_findings import (
    make_boundary,
    make_detection,
    make_scan,
)
from scanning.tests.test_views import ScanningTestCase

WIDTH, HEIGHT = 1700, 2200

#: A picture across both columns, a fifth of the page.
PICTURE = [300.0, 500.0, 1400.0, 1300.0]
#: The West key at the head of an opinion: under half a percent of the page.
KEY = [600.0, 1500.0, 720.0, 1545.0]


def dots(*pages):
    return {"pages": list(pages)}


def dots_page(index, *cells, width=WIDTH, height=HEIGHT):
    return {
        "page_index": index,
        "origin_width": width,
        "origin_height": height,
        "cells": [
            {"bbox": list(box), "category": kind, "text": ""}
            for kind, box in cells
        ],
    }


def mistral(*pages, width=WIDTH, height=HEIGHT):
    return {"render": {"width": width, "height": height}, "pages": list(pages)}


def mistral_page(index, *blocks):
    return {
        "page_index": index,
        "blocks": [
            {"id": i, "type": kind, "bbox": list(box), "content": ""}
            for i, (kind, box) in enumerate(blocks)
        ],
    }


def surya(*pages):
    return {"pages": list(pages)}


def surya_page(index, *blocks, width=WIDTH, height=HEIGHT):
    return {
        "page_index": index,
        "origin_width": width,
        "origin_height": height,
        "blocks": [
            {"label": kind, "bbox": list(box), "html": "", "text": ""}
            for kind, box in blocks
        ],
    }


def image_row(scan, box=PICTURE, page_index=0, **fields):
    x0, y0, x1, y1 = box
    return make_detection(
        scan,
        Label.IMAGE.name,
        page_index=page_index,
        label_id=int(Label.IMAGE),
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        **fields,
    )


def reading(scan, marks, page_index=0, width=WIDTH, height=HEIGHT):
    return PictureReading.objects.create(
        scan=scan,
        page_index=page_index,
        source_page=page_index + 1,
        img_width=width,
        img_height=height,
        marks=marks,
    )


def mark(engine, box=PICTURE, kind="picture"):
    return {"engine": engine, "kind": kind, "box": list(box)}


class TestTheLabels(ScanningTestCase):
    def test_every_reader_is_an_engine_and_the_engine_table_names_no_picture(
        self,
    ):
        """The readers' picture labels live here, keyed to the engine
        table, and that table keeps no picture label of its own (#463);
        a table label is in both, a block kind there and a box here."""
        for name, labels in pictures.PICTURE_LABELS.items():
            spec = opinion_ocr.ENGINES[name]
            picture_labels = {
                label
                for label, kind in labels.items()
                if kind == pictures.PICTURE
            }
            self.assertFalse(picture_labels & set(spec.kind_types), name)
        self.assertEqual(
            set(pictures.PICTURE_LABELS), set(opinion_ocr.ENGINES)
        )


class TestReadDocuments(ScanningTestCase):
    def test_every_readers_marks_land_in_the_first_readers_render(self):
        """Mistral read the page at half size: its box is doubled into
        dots.mocr's render, and a Surya table joins the same page."""
        by_page = pictures.read_documents(
            {
                "dots_mocr": dots(dots_page(0, ("Picture", PICTURE))),
                "mistral_ocr": mistral(
                    mistral_page(0, ("image", [150, 250, 700, 650])),
                    width=850,
                    height=1100,
                ),
                "surya": surya(
                    surya_page(0, ("Table", [200, 1600, 900, 2000]))
                ),
            }
        )
        width, height, marks = by_page[0]
        self.assertEqual((width, height), (1700.0, 2200.0))
        self.assertEqual(
            marks,
            [
                {"engine": "dots_mocr", "kind": "picture", "box": PICTURE},
                {
                    "engine": "mistral_ocr",
                    "kind": "picture",
                    "box": [300.0, 500.0, 1400.0, 1300.0],
                },
                {
                    "engine": "surya",
                    "kind": "table",
                    "box": [200.0, 1600.0, 900.0, 2000.0],
                },
            ],
        )

    def test_a_page_with_no_mark_and_a_text_cell_are_left_out(self):
        by_page = pictures.read_documents(
            {
                "dots_mocr": dots(
                    dots_page(0, ("Text", PICTURE)),
                    dots_page(1, ("Table", PICTURE), ("Picture", KEY)),
                )
            }
        )
        self.assertEqual(list(by_page), [1])
        self.assertEqual(
            [m["kind"] for m in by_page[1][2]], ["table", "picture"]
        )


class TestClusters(ScanningTestCase):
    def test_three_readers_of_one_picture_are_one_box_at_the_median(self):
        groups = pictures.clusters(
            [
                mark("dots_mocr", [300, 500, 1400, 1300]),
                mark("surya", [310, 490, 1390, 1310], kind="table"),
                mark("mistral_ocr", [280, 520, 1500, 1280]),
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0]["readers"], ["dots_mocr", "mistral_ocr", "surya"]
        )
        self.assertEqual(groups[0]["kinds"], ["picture", "table"])
        self.assertEqual(groups[0]["box"], [300.0, 500.0, 1400.0, 1300.0])

    def test_two_pictures_side_by_side_are_two_boxes(self):
        groups = pictures.clusters(
            [
                mark("dots_mocr", [300, 500, 800, 1300]),
                mark("dots_mocr", [900, 500, 1400, 1300]),
                mark("surya", [905, 505, 1395, 1295]),
            ]
        )
        self.assertEqual(
            [(g["readers"], g["box"][0]) for g in groups],
            [(["dots_mocr"], 300.0), (["dots_mocr", "surya"], 902.5)],
        )


class TestWriteRows(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()

    def test_the_compute_writes_one_row_per_page_with_a_mark(self):
        written = pictures.write_rows(
            self.scan,
            {
                "dots_mocr": dots(
                    dots_page(0, ("Picture", PICTURE)),
                    dots_page(1, ("Text", PICTURE)),
                ),
                "surya": surya(surya_page(2, ("Figure", PICTURE))),
            },
            None,
        )
        self.assertEqual(written, 2)
        rows = list(PictureReading.objects.filter(scan=self.scan))
        self.assertEqual([r.page_index for r in rows], [0, 2])
        self.assertEqual(rows[0].source_page, 1)
        self.assertEqual(rows[1].marks[0]["engine"], "surya")

    def test_no_primary_document_leaves_the_rows_alone(self):
        reading(self.scan, [mark("dots_mocr")])
        self.assertEqual(pictures.write_rows(self.scan, None, None), 0)
        self.assertEqual(
            pictures.write_rows(self.scan, {"surya": surya()}, None), 0
        )
        self.assertEqual(
            PictureReading.objects.filter(scan=self.scan).count(), 1
        )

    def test_a_second_write_replaces_the_set(self):
        pictures.write_rows(
            self.scan,
            {"dots_mocr": dots(dots_page(0, ("Picture", PICTURE)))},
            None,
        )
        pictures.write_rows(
            self.scan,
            {"dots_mocr": dots(dots_page(3, ("Picture", PICTURE)))},
            None,
        )
        self.assertEqual(
            list(
                PictureReading.objects.filter(scan=self.scan).values_list(
                    "page_index", flat=True
                )
            ),
            [3],
        )


class TestEngineDocuments(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()

    def test_the_run_documents_are_read_and_a_fault_is_left_out(self):
        run = mock.Mock(
            ocr_key="k/dots", extract_key="k/mistral", surya_key=""
        )
        with mock.patch(
            "scanning.s3_sync.download_json_object",
            side_effect=lambda key: {"pages": [], "key": key},
        ) as download:
            documents = pictures.engine_documents(
                self.scan, run, {"pages": []}
            )
        self.assertEqual(list(documents), ["dots_mocr", "mistral_ocr"])
        self.assertEqual(documents["mistral_ocr"]["key"], "k/mistral")
        download.assert_called_once_with("k/mistral")
        with mock.patch(
            "scanning.s3_sync.download_json_object",
            side_effect=OSError("down"),
        ):
            documents = pictures.engine_documents(
                self.scan, run, {"pages": []}
            )
        self.assertEqual(list(documents), ["dots_mocr"])

    def test_without_a_run_the_glued_volume_keys_are_read(self):
        with (
            mock.patch(
                "scanning.mistral_ocr.glued_volume_key", return_value=""
            ),
            mock.patch(
                "scanning.surya.glued_volume_key", return_value="k/surya"
            ),
            mock.patch(
                "scanning.s3_sync.download_json_object",
                return_value={"pages": []},
            ) as download,
        ):
            documents = pictures.engine_documents(self.scan, None, None)
        self.assertEqual(list(documents), ["surya"])
        download.assert_called_once_with("k/surya")


class TestUncovered(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()

    def test_a_picture_the_readers_saw_with_no_image_box_is_a_card(self):
        reading(
            self.scan,
            [
                mark("dots_mocr"),
                mark("surya", kind="table"),
                mark("dots_mocr", KEY),
            ],
        )
        # The detections' render is half the readers': the box comes
        # back in the detections' pixels, the space of every finding.
        make_detection(
            self.scan,
            "TEXT_COLUMN",
            label_id=int(Label.TEXT_COLUMN),
            img_width=850,
            img_height=1100,
        )
        found = list(pictures.uncovered(self.scan, None))
        self.assertEqual(len(found), 1)
        finding = found[0]
        self.assertEqual(finding["check_name"], CheckName.UNBOXED_PICTURE)
        self.assertEqual(finding["page_number"], 1)
        meta = finding["metadata"]
        self.assertEqual(meta["bbox"], [150.0, 250.0, 700.0, 650.0])
        self.assertEqual(meta["guess"], meta["bbox"])
        self.assertEqual((meta["img_width"], meta["img_height"]), (850, 1100))
        self.assertEqual(meta["label"], "IMAGE")
        self.assertEqual(meta["label_id"], int(Label.IMAGE))
        self.assertEqual(meta["readers"], ["dots_mocr", "surya"])
        self.assertEqual(meta["kinds"], ["picture", "table"])
        self.assertIn(
            "dots.mocr and Surya read a picture or table here",
            finding["message"],
        )
        # The address a dismissal is keyed by.
        address = findings.address_of(finding)
        self.assertEqual(address["source_page"], 1)
        self.assertEqual(address["target_x0"], 150.0)

    def test_a_live_image_box_over_the_picture_is_cover_a_hand_drawn_one_too(
        self,
    ):
        reading(self.scan, [mark("dots_mocr")])
        row = image_row(self.scan, [250.0, 450.0, 1450.0, 1350.0])
        self.assertEqual(list(pictures.uncovered(self.scan, None)), [])
        row.delete()
        image_row(
            self.scan,
            [300.0, 500.0, 1400.0, 950.0],
            model_name=Detection.ModelName.MANUAL,
            found_by=[],
        )
        # Over half of the readers' box: cover.
        self.assertEqual(list(pictures.uncovered(self.scan, None)), [])

    def test_a_box_covering_less_than_half_is_no_cover(self):
        reading(self.scan, [mark("dots_mocr")])
        image_row(self.scan, [300.0, 500.0, 1400.0, 800.0])
        self.assertEqual(len(list(pictures.uncovered(self.scan, None))), 1)

    def test_a_lone_readers_table_counts_only_across_both_columns(self):
        """Mistral alone called a centred heading in one column a table:
        no card. The same claim across the page, or from two readers,
        or a lone reader's picture, is one."""
        narrow = [300.0, 500.0, 800.0, 900.0]
        reading(self.scan, [mark("mistral_ocr", narrow, kind="table")])
        self.assertEqual(list(pictures.uncovered(self.scan, None)), [])
        PictureReading.objects.filter(scan=self.scan).delete()
        reading(self.scan, [mark("dots_mocr", PICTURE, kind="table")])
        self.assertEqual(len(list(pictures.uncovered(self.scan, None))), 1)
        PictureReading.objects.filter(scan=self.scan).delete()
        reading(
            self.scan,
            [
                mark("mistral_ocr", narrow, kind="table"),
                mark("surya", narrow, kind="table"),
            ],
        )
        self.assertEqual(len(list(pictures.uncovered(self.scan, None))), 1)
        PictureReading.objects.filter(scan=self.scan).delete()
        reading(self.scan, [mark("mistral_ocr", narrow)])
        self.assertEqual(len(list(pictures.uncovered(self.scan, None))), 1)

    def test_a_key_icon_sized_mark_makes_no_card(self):
        reading(self.scan, [mark("dots_mocr", KEY), mark("surya", KEY)])
        self.assertEqual(list(pictures.uncovered(self.scan, None)), [])

    def test_the_rebuild_writes_the_card_and_the_sidebar_offers_the_add(self):
        make_boundary(
            self.scan,
            make_detection(self.scan, "CASE_CAPTION"),
            make_detection(self.scan, "KEY_ICON"),
        )
        reading(self.scan, [mark("dots_mocr"), mark("mistral_ocr")])
        findings.rebuild(self.scan, run=None)
        issue = Issue.objects.get(
            scan=self.scan, check_name=CheckName.UNBOXED_PICTURE
        )
        self.assertEqual(
            issue.metadata["readers"], ["dots_mocr", "mistral_ocr"]
        )
        self.client.force_login(self.make_user())
        fragment = self.client.get(
            reverse("review_findings", kwargs={"pk": self.scan.pk})
        )
        html = fragment.json()["html"]
        self.assertIn("+ image", html)
        self.assertIn('data-label-id="%d"' % int(Label.IMAGE), html)
        self.assertIn('data-guess="300.0,500.0,1400.0,1300.0"', html)


class TestFitFor(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()

    def test_a_box_run_over_the_text_gets_the_readers_box_in_its_own_render(
        self,
    ):
        reading(self.scan, [mark("dots_mocr"), mark("surya")])
        # The model's box at half the readers' render, twice the picture.
        row = image_row(
            self.scan,
            [50.0, 100.0, 800.0, 1000.0],
            img_width=850,
            img_height=1100,
        )
        fits = pictures.fit_for(self.scan, None, [row])
        self.assertEqual(fits[row.pk]["box"], [150.0, 250.0, 700.0, 650.0])
        self.assertEqual(fits[row.pk]["readers"], ["dots_mocr", "surya"])
        self.assertLess(fits[row.pk]["iou"], pictures.FIT_IOU)

    def test_a_box_over_two_pictures_side_by_side_is_pulled_in_to_both(self):
        """The readers see two photographs where the model drew one
        box: the fit is the union of both, never the left one alone."""
        left = [300.0, 500.0, 800.0, 1300.0]
        right = [900.0, 500.0, 1400.0, 1300.0]
        reading(
            self.scan,
            [
                mark("dots_mocr", left),
                mark("surya", left),
                mark("surya", right),
                mark("mistral_ocr", right),
            ],
        )
        row = image_row(self.scan, [100.0, 300.0, 1600.0, 1500.0])
        fit = pictures.fit_for(self.scan, None, [row])[row.pk]
        self.assertEqual(fit["box"], [300.0, 500.0, 1400.0, 1300.0])
        self.assertEqual(fit["readers"], ["dots_mocr", "mistral_ocr", "surya"])

    def test_a_box_drawn_short_of_the_picture_grows_to_it(self):
        reading(self.scan, [mark("dots_mocr"), mark("surya")])
        row = image_row(self.scan, [300.0, 500.0, 1400.0, 800.0])
        fit = pictures.fit_for(self.scan, None, [row])[row.pk]
        self.assertEqual(fit["box"], PICTURE)

    def test_a_box_the_readers_agree_with_gets_nothing(self):
        reading(self.scan, [mark("dots_mocr"), mark("surya")])
        row = image_row(self.scan, [305.0, 505.0, 1395.0, 1295.0])
        self.assertEqual(pictures.fit_for(self.scan, None, [row]), {})

    def test_a_box_no_reader_marked_under_says_so(self):
        reading(self.scan, [mark("dots_mocr", [100.0, 1600.0, 900.0, 2000.0])])
        row = image_row(self.scan, [300.0, 200.0, 1400.0, 900.0])
        self.assertEqual(
            pictures.fit_for(self.scan, None, [row])[row.pk],
            {"box": None, "readers": [], "kinds": [], "iou": 0.0},
        )

    def test_a_page_the_readers_did_not_read_gets_nothing(self):
        row = image_row(self.scan)
        self.assertEqual(pictures.fit_for(self.scan, None, [row]), {})


class TestTheCards(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _picture_issue(self, page_index=0):
        return Issue.objects.create(
            scan=self.scan,
            severity=Issue.Severity.WARNING,
            check_name=CheckName.UNBOXED_PICTURE,
            target=Issue.Target.DETECTION,
            page_number=page_index + 1,
            message="a picture",
            metadata={
                "page_index": page_index,
                "img_width": WIDTH,
                "img_height": HEIGHT,
                "bbox": PICTURE,
                "guess": PICTURE,
                "label": "IMAGE",
                "label_id": int(Label.IMAGE),
                "source_page": page_index + 1,
                "readers": ["dots_mocr", "surya"],
                "kinds": ["picture"],
            },
        )

    def test_a_picture_card_is_the_readers_box_in_the_images_section(self):
        issue = self._picture_issue()
        card = card_review.picture_card(issue)
        self.assertEqual(card["kind"], card_review.PICTURE)
        self.assertEqual(card["group"], card_review.IMAGE)
        self.assertEqual(card["label"], "IMAGE")
        self.assertEqual(card["label_id"], int(Label.IMAGE))
        self.assertEqual(card["box"], PICTURE)
        self.assertEqual(card["readers"], ["dots_mocr", "surya"])
        self.assertTrue(card["key"].startswith("f:unboxed_picture:0:"))

    def test_the_cards_hold_the_picture_beside_the_image_boxes_with_the_fit(
        self,
    ):
        self._picture_issue(page_index=2)
        reading(self.scan, [mark("dots_mocr"), mark("surya")], page_index=1)
        loose = image_row(
            self.scan, [50.0, 100.0, 1600.0, 2000.0], page_index=1
        )
        agreed = image_row(
            self.scan, [305.0, 505.0, 1395.0, 1295.0], page_index=3
        )
        reading(self.scan, [mark("dots_mocr")], page_index=3)
        cards = self.client.get(
            reverse("scan_review_cards", kwargs={"pk": self.scan.pk})
        ).json()["cards"]
        self.assertEqual(
            [(c["kind"], c["page_index"]) for c in cards],
            [("image", 1), ("picture", 2), ("image", 3)],
        )
        fit = cards[0]["fit"]
        self.assertEqual(fit["box"], PICTURE)
        self.assertIn(
            "dots.mocr and Surya draw the picture differently",
            cards[0]["message"],
        )
        self.assertEqual(cards[0]["detection_id"], loose.pk)
        self.assertIsNone(cards[2]["fit"])
        self.assertEqual(cards[2]["detection_id"], agreed.pk)

    def test_the_script_knows_the_picture_card_and_the_refinement(self):
        script = card_review.__file__  # the module beside the static files
        import pathlib

        text = (
            pathlib.Path(script).resolve().parent
            / "static"
            / "scanning"
            / "card_review.js"
        ).read_text()
        self.assertIn("picture: 'picture or table the readers saw'", text)
        self.assertIn("'Add image box'", text)
        self.assertIn("'Refine image box'", text)
        self.assertIn("function refineBox", text)
