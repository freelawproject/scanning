"""Tests for the apply of the page edits (issue #224), review-1 side.

The plan and the walk over synthetic PDFs, the supersede rule of one
standing row per address, the lock on the edit endpoints after the
approval, and the reopen. The build phase and the glues have their own
modules (``test_apply_build.py``, ``test_apply_glue.py``).
"""

import io
import json
import pathlib
import tempfile
from unittest import mock

import fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from scanning import apply, page_edits
from scanning.factories import PageEditFactory, ScanFactory
from scanning.models import ApplyRun, Issue, PageEdit, Scan, Status
from scanning.tests.test_sharding import write_image_volume
from scanning.tests.test_views import ScanningTestCase
from scanning.views_process import EDITS_LOCKED_MESSAGE

MEDIA_ROOT = tempfile.mkdtemp()


def png_bytes(width: int = 40, height: int = 60) -> bytes:
    """Return a small PNG.

    :param width: Pixels across.
    :param height: Pixels down.
    :returns: The encoded image.
    """
    buf = io.BytesIO()
    Image.new("L", (width, height), color=128).save(buf, format="PNG")
    return buf.getvalue()


def pdf_bytes(
    pages: int = 1, width: float = 300, height: float = 500
) -> bytes:
    """Return a PDF of blank pages of one size.

    :param pages: How many pages.
    :param width: Page width in points.
    :param height: Page height in points.
    :returns: The encoded document.
    """
    with fitz.open() as doc:
        for _ in range(pages):
            doc.new_page(width=width, height=height)
        return doc.tobytes()


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    """Wrap bytes as an uploaded file.

    :param name: The file name, whose extension the storage keeps.
    :param data: The bytes.
    :returns: The upload.
    """
    content_type = "application/pdf" if name.endswith(".pdf") else "image/png"
    return SimpleUploadedFile(name, data, content_type=content_type)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ApplyTestCase(ScanningTestCase):
    """A scan with a six-page original on disk."""

    PAGES = 6

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            page_count=self.PAGES,
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            source_fingerprint="100:6",
        )
        output_dir = pathlib.Path(self.scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.original = (
            output_dir / pathlib.Path(self.scan.original_pdf.name).name
        )
        write_image_volume(self.original, pages=self.PAGES)

    def edit(self, kind, **kwargs):
        """Write one standing row against this scan's fingerprint.

        :param kind: The ``PageEdit.Kind``.
        :param kwargs: Address and value fields.
        :returns: The row.
        """
        kwargs.setdefault("value", "")
        kwargs.setdefault("source_fingerprint", self.scan.source_fingerprint)
        if kind == PageEdit.Kind.INSERT_PAGE:
            kwargs.setdefault("pdf_page", None)
        return PageEditFactory(scan=self.scan, kind=kind, **kwargs)

    def upload_edit(self, kind, name, data, **kwargs):
        """Write one row that carries a file.

        :param kind: Insert or replace.
        :param name: The file name.
        :param data: The file's bytes.
        :param kwargs: Address fields.
        :returns: The row.
        """
        row = self.edit(kind, **kwargs)
        row.image.save(name, upload(name, data), save=True)
        return row

    def sources(self, plan):
        """Return the map's sources, compactly, for an assertion.

        :param plan: The plan.
        :returns: ``("o", pdf_page)`` or ``("e", edit_id, page)`` per
            final page.
        """
        out = []
        for entry in plan.pages:
            src = entry["source"]
            if src["kind"] == "original":
                out.append(("o", src["pdf_page"]))
            else:
                out.append(("e", src["edit_id"], src["page"]))
        return out


class TestPlanRun(ApplyTestCase):
    """The offset map, from the rows."""

    def test_no_edits_is_the_identity(self):
        plan = apply.plan_run(self.scan)

        self.assertTrue(plan.is_identity)
        self.assertEqual(plan.final_page_count, self.PAGES)
        self.assertEqual(
            self.sources(plan), [("o", p) for p in range(1, self.PAGES + 1)]
        )
        self.assertEqual(plan.to_map()["deleted_pages"], [])

    def test_a_deleted_page_is_dropped(self):
        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=3)

        plan = apply.plan_run(self.scan)

        self.assertEqual(
            self.sources(plan),
            [("o", 1), ("o", 2), ("o", 4), ("o", 5), ("o", 6)],
        )
        self.assertEqual(plan.deleted_pages, [3])
        self.assertEqual(apply.final_page_of(plan.to_map(), 4), 3)
        self.assertIsNone(apply.final_page_of(plan.to_map(), 3))

    def test_inserts_follow_their_anchor_in_ordinal_order(self):
        second = self.edit(
            PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=2, ordinal=1
        )
        first = self.edit(
            PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=2, ordinal=0
        )
        front = self.edit(PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=0)

        plan = apply.plan_run(
            self.scan, page_counts={first.pk: 1, second.pk: 1, front.pk: 1}
        )

        self.assertEqual(
            self.sources(plan)[:5],
            [
                ("e", front.pk, 0),
                ("o", 1),
                ("o", 2),
                ("e", first.pk, 0),
                ("e", second.pk, 0),
            ],
        )
        self.assertEqual(plan.final_page_count, self.PAGES + 3)

    def test_a_multi_page_insert_takes_several_slots(self):
        leaf = self.edit(PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=1)

        plan = apply.plan_run(self.scan, page_counts={leaf.pk: 2})

        self.assertEqual(
            self.sources(plan)[:4],
            [("o", 1), ("e", leaf.pk, 0), ("e", leaf.pk, 1), ("o", 2)],
        )

    def test_a_replacement_takes_the_slot_of_its_page(self):
        swap = self.edit(PageEdit.Kind.REPLACE_PAGE, pdf_page=4)

        plan = apply.plan_run(self.scan, page_counts={swap.pk: 1})

        self.assertEqual(self.sources(plan)[3], ("e", swap.pk, 0))
        self.assertEqual(plan.pages[3]["source"]["reference_pdf_page"], 4)
        self.assertIsNone(apply.final_page_of(plan.to_map(), 4))

    def test_a_rotation_keeps_its_slot_as_a_shard(self):
        turn = self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=2, value="90")

        plan = apply.plan_run(self.scan)

        self.assertEqual(self.sources(plan)[1], ("e", turn.pk, 0))
        self.assertEqual(plan.pages[1]["source"]["rotation"], 90)
        self.assertEqual(plan.pages[1]["source"]["pdf_page"], 2)

    def test_a_deletion_outranks_a_replacement(self):
        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=4)
        swap = self.edit(PageEdit.Kind.REPLACE_PAGE, pdf_page=4)

        plan = apply.plan_run(self.scan, page_counts={swap.pk: 1})

        self.assertEqual(plan.final_page_count, self.PAGES - 1)
        self.assertNotIn(("e", swap.pk, 0), self.sources(plan))
        # Considered and stamped, but no shard is cut for it.
        self.assertIn(swap, plan.edits)
        self.assertEqual(plan.shard_edits, [])

    def test_a_replacement_outranks_a_rotation(self):
        turn = self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=4, value="90")
        swap = self.edit(PageEdit.Kind.REPLACE_PAGE, pdf_page=4)

        plan = apply.plan_run(self.scan, page_counts={swap.pk: 1})

        self.assertEqual(self.sources(plan)[3], ("e", swap.pk, 0))
        self.assertIn(turn, plan.edits)
        self.assertEqual(plan.shard_edits, [swap])

    def test_a_stale_edit_is_not_planned(self):
        self.edit(
            PageEdit.Kind.DELETE_PAGE, pdf_page=3, source_fingerprint="9:9"
        )

        plan = apply.plan_run(self.scan)

        self.assertTrue(plan.is_identity)

    def test_a_withdrawn_edit_is_not_planned_and_an_applied_one_is(self):
        self.edit(
            PageEdit.Kind.DELETE_PAGE,
            pdf_page=3,
            withdrawn_at=timezone.now(),
        )
        self.edit(
            PageEdit.Kind.DELETE_PAGE, pdf_page=5, applied_at=timezone.now()
        )

        plan = apply.plan_run(self.scan)

        self.assertEqual(plan.deleted_pages, [5])

    def test_an_insert_past_the_last_page_goes_last(self):
        tail = self.edit(PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=99)

        plan = apply.plan_run(self.scan, page_counts={tail.pk: 1})

        self.assertEqual(self.sources(plan)[-1], ("e", tail.pk, 0))


class TestBuildFinalPdf(ApplyTestCase):
    """The walk, over the plan."""

    def build(self, plan, files=None):
        """Run the walk and return the final document's pages.

        :param plan: The plan.
        :param files: ``{edit pk: bytes}`` for the uploaded files.
        :returns: ``(page_count, [(width, height, rotation), ...])``.
        """
        files = files or {}
        with fitz.open(str(self.original)) as source:
            with apply.build_final_pdf(
                source, plan, read_file=lambda edit: files[edit.pk]
            ) as out:
                shapes = [
                    (round(p.rect.width), round(p.rect.height), p.rotation)
                    for p in out
                ]
                return out.page_count, shapes

    def test_the_identity_copies_every_page(self):
        count, shapes = self.build(apply.plan_run(self.scan))

        self.assertEqual(count, self.PAGES)
        self.assertEqual(shapes, [(612, 792, 0)] * self.PAGES)

    def test_deletes_and_inserts_are_applied(self):
        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=2)
        leaf = self.edit(PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=4)
        # The walk reads the file's kind off the row, so the name is on
        # the row before the plan reads it.
        PageEdit.objects.filter(pk=leaf.pk).update(image="x.pdf")
        plan = apply.plan_run(self.scan, page_counts={leaf.pk: 2})

        count, shapes = self.build(plan, {leaf.pk: pdf_bytes(2, 300, 500)})

        self.assertEqual(count, self.PAGES + 1)
        # Pages 1, 3, 4, then the two inserted, then 5 and 6.
        self.assertEqual(shapes[3], (300, 500, 0))
        self.assertEqual(shapes[4], (300, 500, 0))
        self.assertEqual(shapes[5], (612, 792, 0))

    def test_an_image_takes_the_size_of_its_reference_page(self):
        swap = self.edit(PageEdit.Kind.REPLACE_PAGE, pdf_page=3)
        PageEdit.objects.filter(pk=swap.pk).update(image="x.png")
        plan = apply.plan_run(self.scan, page_counts={swap.pk: 1})

        count, shapes = self.build(plan, {swap.pk: png_bytes()})

        self.assertEqual(count, self.PAGES)
        self.assertEqual(shapes[2], (612, 792, 0))

    def test_a_rotation_turns_the_page(self):
        self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=5, value="90")
        plan = apply.plan_run(self.scan)

        count, shapes = self.build(plan)

        self.assertEqual(count, self.PAGES)
        # A turned page reports its rectangle as it displays.
        self.assertEqual(shapes[4], (792, 612, 90))
        self.assertEqual(shapes[3], (612, 792, 0))

    def test_a_shard_with_too_few_pages_is_refused(self):
        leaf = self.edit(PageEdit.Kind.INSERT_PAGE, anchor_pdf_page=1)
        PageEdit.objects.filter(pk=leaf.pk).update(image="x.pdf")
        plan = apply.plan_run(self.scan, page_counts={leaf.pk: 3})

        with self.assertRaises(apply.ApplyError):
            self.build(plan, {leaf.pk: pdf_bytes(1)})

    def test_the_shard_of_an_edit_matches_its_final_pages(self):
        # The shard the stages read and the final PDF come from one
        # rule, so page k of the shard is the map's entry k.
        turn = self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=2, value="180")

        with fitz.open(str(self.original)) as source:
            with apply.build_edit_shard(source, turn, None) as shard:
                self.assertEqual(shard.page_count, 1)
                self.assertEqual(shard[0].rotation, 180)


class TestExportUsesTheWalk(ApplyTestCase):
    """``export_pdf`` applies the two kinds it used to skip."""

    def export(self):
        """Download the corrected PDF.

        :returns: The open document's ``(page_count, rotations)``.
        """
        response = self.client.get(
            reverse("export_pdf", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 200)
        data = b"".join(response.streaming_content)
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count, [p.rotation for p in doc]

    def test_a_replacement_and_a_rotation_are_applied(self):
        self.upload_edit(
            PageEdit.Kind.REPLACE_PAGE, "r.png", png_bytes(), pdf_page=2
        )
        self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=4, value="270")

        count, rotations = self.export()

        self.assertEqual(count, self.PAGES)
        self.assertEqual(rotations[3], 270)


class TestSupersede(ApplyTestCase):
    """One standing row per address."""

    def test_an_open_row_is_updated_in_place(self):
        first = page_edits.supersede(
            self.scan,
            PageEdit.Kind.SET_NUMBER,
            {"pdf_page": 3},
            {"value": "300"},
            self.user,
        )
        again = page_edits.supersede(
            self.scan,
            PageEdit.Kind.SET_NUMBER,
            {"pdf_page": 3},
            {"value": "301"},
            self.user,
        )

        self.assertEqual(first.pk, again.pk)
        self.assertEqual(again.value, "301")
        self.assertEqual(self.scan.page_edits.count(), 1)

    def test_an_applied_row_is_withdrawn_and_replaced(self):
        applied = self.edit(
            PageEdit.Kind.SET_NUMBER,
            pdf_page=3,
            value="300",
            applied_at=timezone.now(),
        )

        again = page_edits.supersede(
            self.scan,
            PageEdit.Kind.SET_NUMBER,
            {"pdf_page": 3},
            {"value": "301"},
            self.user,
        )

        applied.refresh_from_db()
        self.assertNotEqual(applied.pk, again.pk)
        self.assertIsNotNone(applied.withdrawn_at)
        self.assertEqual(applied.value, "300")
        self.assertEqual(applied.withdrawn_by, self.user)
        self.assertEqual(list(page_edits.standing_edits(self.scan)), [again])

    def test_a_lost_insert_race_answers_the_winner(self):
        # Two requests read no standing row; the second insert loses to
        # the unique key and must hand back the first one's row.
        real = page_edits.standing_edits
        winner = self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=3, value="300")
        with mock.patch.object(
            page_edits,
            "standing_edits",
            side_effect=[PageEdit.objects.none(), real(self.scan)],
        ):
            row = page_edits.supersede(
                self.scan,
                PageEdit.Kind.SET_NUMBER,
                {"pdf_page": 3},
                {"value": "301"},
                self.user,
            )

        self.assertEqual(row.pk, winner.pk)
        self.assertEqual(row.value, "301")
        self.assertEqual(self.scan.page_edits.count(), 1)

    def test_a_deletion_is_not_refreshed(self):
        first = page_edits.supersede(
            self.scan,
            PageEdit.Kind.DELETE_PAGE,
            {"pdf_page": 3},
            {"source_fingerprint": "a"},
            self.user,
            refresh_open=False,
        )
        other = self.make_user()
        again = page_edits.supersede(
            self.scan,
            PageEdit.Kind.DELETE_PAGE,
            {"pdf_page": 3},
            {"source_fingerprint": "b"},
            other,
            refresh_open=False,
        )

        self.assertEqual(first.pk, again.pk)
        self.assertEqual(again.author, self.user)
        self.assertEqual(again.source_fingerprint, "a")

    def test_pending_counts_the_unapplied_structural_rows_only(self):
        self.edit(
            PageEdit.Kind.DELETE_PAGE, pdf_page=2, applied_at=timezone.now()
        )
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=3, value="3")
        self.assertFalse(page_edits.has_pending_changes(self.scan))

        self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=4, value="90")
        flags = page_edits.pending_edit_flags(self.scan)
        self.assertTrue(flags["has_pending_changes"])
        self.assertFalse(flags["has_pending_inserts"])

    def test_the_applied_deletion_still_reads_as_deleted(self):
        self.edit(
            PageEdit.Kind.DELETE_PAGE, pdf_page=2, applied_at=timezone.now()
        )

        self.assertEqual(page_edits.deleted_pages(self.scan), {2})

    def test_the_endpoints_supersede_an_applied_row(self):
        applied = self.edit(
            PageEdit.Kind.ROTATE_PAGE,
            pdf_page=2,
            value="90",
            applied_at=timezone.now(),
        )

        response = self.client.post(
            reverse("rotate_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2, "degrees": "180"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        applied.refresh_from_db()
        self.assertIsNotNone(applied.withdrawn_at)
        rows = page_edits.rotations_by_page(self.scan)
        self.assertEqual(rows, {2: 180})

    def test_an_undo_takes_back_an_applied_deletion(self):
        applied = self.edit(
            PageEdit.Kind.DELETE_PAGE, pdf_page=2, applied_at=timezone.now()
        )

        response = self.client.post(
            reverse("undo_delete_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        applied.refresh_from_db()
        self.assertIsNotNone(applied.withdrawn_at)
        self.assertEqual(page_edits.deleted_pages(self.scan), set())


class TestEditLock(ApplyTestCase):
    """DONE locks the nine edit endpoints."""

    JSON_ENDPOINTS = (
        ("assign_page", {"pdf_page": 1, "page_number": "7"}),
        ("delete_page", {"pdf_page": 1}),
        ("undo_delete_page", {"pdf_page": 1}),
        ("remove_page_insert", {"edit_id": 1}),
        ("undo_replace_page", {"pdf_page": 1}),
        ("rotate_page", {"pdf_page": 1, "degrees": "90"}),
        ("dismiss_issue", {"issue_id": 1}),
    )

    def lock(self, status=Status.PAGE_COMPLETENESS_REVIEW_DONE):
        """Move the scan to a locked status.

        :param status: The status.
        """
        Scan.objects.filter(pk=self.scan.pk).update(status=status)

    def test_every_json_endpoint_answers_409_under_done(self):
        self.lock()
        for name, body in self.JSON_ENDPOINTS:
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(name, kwargs={"pk": self.scan.pk}),
                    data=json.dumps(body),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["error"], EDITS_LOCKED_MESSAGE
                )
        self.assertEqual(self.scan.page_edits.count(), 0)

    def test_the_two_uploads_answer_409_under_done(self):
        self.lock()
        for name, extra in (
            ("add_page_insert", {"anchor_pdf_page": "1", "page_number": "2"}),
            ("replace_page", {"pdf_page": "1"}),
        ):
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(name, kwargs={"pk": self.scan.pk}),
                    data={"image": upload("p.png", png_bytes()), **extra},
                )
                self.assertEqual(response.status_code, 409)
        self.assertEqual(self.scan.page_edits.count(), 0)

    def test_a_busy_scan_is_locked_too(self):
        self.lock(Status.QUEUED)
        response = self.client.post(
            reverse("delete_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

    def test_the_review_states_are_open(self):
        for status in (
            Status.AWAITING_VALIDATION,
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.PENDING_REVIEW,
        ):
            with self.subTest(status=status):
                self.lock(status)
                response = self.client.post(
                    reverse("delete_page", kwargs={"pk": self.scan.pk}),
                    data=json.dumps({"pdf_page": 1}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)

    def test_a_dismissal_under_done_keeps_the_issue(self):
        issue = Issue.objects.create(
            scan=self.scan,
            check_name="duplicate_page",
            severity=Issue.Severity.WARNING,
            message="x",
            page_number=1,
        )
        self.lock()
        self.client.post(
            reverse("dismiss_issue", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"issue_id": issue.pk}),
            content_type="application/json",
        )
        self.assertTrue(Issue.objects.filter(pk=issue.pk).exists())


class TestReopen(ApplyTestCase):
    """The staff way back from DONE."""

    def reopen(self):
        """Press the reopen button.

        :returns: The response.
        """
        return self.client.post(
            reverse("reopen_page_review", kwargs={"pk": self.scan.pk})
        )

    def test_a_curator_cannot_reopen(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.reopen()
        self.scan.refresh_from_db()
        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

    def test_staff_reopen_unlocks_and_supersedes_the_run(self):
        self.client.force_login(self.make_staff_user())
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        run = ApplyRun.objects.create(scan=self.scan, number=1)

        response = self.reopen()

        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(
            self.scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertIsNotNone(run.superseded_at)
        self.assertIsNone(apply.current_run(self.scan))
        self.assertEqual(apply.latest_run(self.scan), run)

    def test_only_done_reopens(self):
        self.client.force_login(self.make_staff_user())
        self.reopen()
        self.scan.refresh_from_db()
        self.assertEqual(
            self.scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(self.scan.apply_runs.count(), 0)

    def test_the_bar_offers_the_reopen_to_staff_under_done(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        url = reverse("scan_process", kwargs={"pk": self.scan.pk})

        self.assertNotContains(self.client.get(url), "Reopen page review")
        self.client.force_login(self.make_staff_user())
        self.assertContains(self.client.get(url), "Reopen page review")
