"""The pages of the third review (issue #334), stage 1.

Two groups: the opinions list (``views.opinion_list``) and the review
page of one opinion (``views.opinion_review``). The legacy pipeline's
pages keep their tests in ``test_views.py``, under their new names.
"""

from unittest.mock import patch

from django.urls import reverse

from scanning import stats
from scanning.factories import (
    OpinionBoundaryFactory,
    OpinionFactory,
    OpinionFindingFactory,
    OpinionScanFactory,
    ReporterFactory,
    ScanFactory,
)
from scanning.models import (
    ApplyRun,
    Opinion,
    OpinionCheck,
    OpinionFindingDismissal,
    OpinionReviewStatus,
    Status,
)
from scanning.tests.test_views import ScanningTestCase


class TestOpinionList(ScanningTestCase):
    """The list of the opinions of the new pipeline."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.reporter = ReporterFactory(short_name="cal", full_name="Cal")
        self.scan = ScanFactory(reporter=self.reporter, volume=237)
        self.opinion = OpinionFactory(
            scan=self.scan,
            first_printed_page=412,
            last_printed_page=415,
            page_count=4,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )

    def test_the_login_is_required(self):
        self.client.logout()

        response = self.client.get(reverse("opinion_list"))

        self.assertEqual(response.status_code, 302)

    def test_the_page_lists_the_new_rows(self):
        response = self.client.get(reverse("opinion_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"]), [self.opinion])
        self.assertContains(response, "237 CAL 412")

    def test_the_page_links_the_legacy_page(self):
        response = self.client.get(reverse("opinion_list"))

        self.assertContains(response, reverse("legacy_opinion_list"))

    def test_the_scan_filter_narrows_the_list(self):
        other = OpinionFactory()

        response = self.client.get(
            reverse("opinion_list"), {"scan": self.scan.pk}
        )

        rows = list(response.context["page_obj"])
        self.assertIn(self.opinion, rows)
        self.assertNotIn(other, rows)

    def test_the_reporter_filter_narrows_the_list(self):
        other = OpinionFactory()

        response = self.client.get(
            reverse("opinion_list"), {"reporter": self.reporter.pk}
        )

        rows = list(response.context["page_obj"])
        self.assertIn(self.opinion, rows)
        self.assertNotIn(other, rows)

    def test_the_volume_filter_narrows_the_list(self):
        other = OpinionFactory(scan=ScanFactory(volume=238))

        response = self.client.get(reverse("opinion_list"), {"volume": "237"})

        rows = list(response.context["page_obj"])
        self.assertIn(self.opinion, rows)
        self.assertNotIn(other, rows)

    def test_the_status_filter_narrows_the_list(self):
        other = OpinionFactory(
            scan=self.scan, status=OpinionReviewStatus.PROCESSING
        )

        response = self.client.get(
            reverse("opinion_list"),
            {"status": OpinionReviewStatus.READY_FOR_TEXT_REVIEW},
        )

        rows = list(response.context["page_obj"])
        self.assertIn(self.opinion, rows)
        self.assertNotIn(other, rows)

    def test_a_volume_that_is_not_a_number_is_refused(self):
        response = self.client.get(
            reverse("opinion_list"), {"volume": "notanumber"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_volume"], "")
        self.assertContains(response, "Volume must be a number.")

    def test_a_reporter_that_is_not_a_number_is_ignored(self):
        response = self.client.get(
            reverse("opinion_list"), {"reporter": "notanumber"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"]), [self.opinion])

    def test_a_digit_that_is_not_a_decimal_is_refused(self):
        """``str.isdigit`` is true for a character ``int`` refuses.

        ``"\u00b2".isdigit()`` is true, ``int("\u00b2")`` raises, and
        Django re-raises that ``ValueError``, so the old guard gave an
        unhandled 500 on each of the three integer filters.
        """
        for name in ("scan", "reporter", "volume"):
            with self.subTest(name=name):
                response = self.client.get(
                    reverse("opinion_list"), {name: "\u00b2"}
                )

                self.assertEqual(response.status_code, 200)

    def test_the_ordering_is_total(self):
        """Two scans of one reporter and volume tie on the four keys
        above the pk, so a paginated walk could show one opinion twice
        and another not at all."""
        twin = ScanFactory(reporter=self.reporter, volume=237)
        OpinionFactory(
            scan=twin,
            first_printed_page=412,
            last_printed_page=415,
            page_count=4,
        )

        response = self.client.get(reverse("opinion_list"))

        rows = list(response.context["page_obj"])
        self.assertEqual([row.pk for row in rows], sorted(r.pk for r in rows))

    def test_the_row_links_carry_the_filters(self):
        response = self.client.get(
            reverse("opinion_list"), {"scan": self.scan.pk}
        )

        self.assertContains(
            response,
            "{}?scan={}".format(
                reverse("opinion_review", kwargs={"pk": self.opinion.pk}),
                self.scan.pk,
            ),
        )

    def test_the_row_carries_its_warning_count(self):
        OpinionFindingFactory(opinion=self.opinion)
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.ORPHANED_OPINION,
        )

        response = self.client.get(reverse("opinion_list"))

        row = response.context["page_obj"][0]
        self.assertEqual(row.open_findings, 2)
        self.assertEqual(row.stale_findings, 1)

    def test_an_opinion_with_no_finding_reads_zero(self):
        response = self.client.get(reverse("opinion_list"))

        row = response.context["page_obj"][0]
        self.assertEqual((row.open_findings, row.stale_findings), (0, 0))

    def test_the_badge_costs_no_query_per_row(self):
        """The count is one grouped query, whatever the row count."""
        for index in range(4):
            OpinionFactory(
                scan=self.scan,
                first_printed_page=500 + index,
                index_in_page=0,
            )
        with self.assertNumQueries(7):
            self.client.get(reverse("opinion_list"))

        for index in range(20):
            OpinionFactory(
                scan=self.scan,
                first_printed_page=600 + index,
                index_in_page=0,
            )
        with self.assertNumQueries(7):
            self.client.get(reverse("opinion_list"))


class TestOpinionReview(ScanningTestCase):
    """The review page of one opinion."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.reporter = ReporterFactory(short_name="cal", full_name="Cal")
        self.scan = ScanFactory(reporter=self.reporter, volume=237)
        self.opinion = OpinionFactory(
            scan=self.scan,
            first_printed_page=412,
            last_printed_page=415,
            page_count=4,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
        self.url = reverse("opinion_review", kwargs={"pk": self.opinion.pk})

    def test_the_login_is_required(self):
        self.client.logout()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)

    def test_an_opinion_that_does_not_exist_is_a_404(self):
        response = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk + 1000})
        )

        self.assertEqual(response.status_code, 404)

    def test_the_page_names_the_opinion(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "237 CAL 412")
        self.assertContains(response, "Ready for text review")

    def test_the_page_shows_the_findings(self):
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=1,
            message="The engines do not agree.",
        )

        response = self.client.get(self.url)

        self.assertEqual(len(response.context["findings"]), 1)
        self.assertEqual(response.context["open_findings"], 1)
        self.assertContains(response, "The engines do not agree.")
        self.assertContains(response, "page 2 of the opinion")

    def test_a_finding_of_the_whole_opinion_says_so(self):
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.ORPHANED_OPINION,
        )

        response = self.client.get(self.url)

        self.assertContains(response, "the whole opinion")
        self.assertContains(response, "not applied")

    def test_a_dismissed_finding_does_not_count_as_open(self):
        dismissal = OpinionFindingDismissal.objects.create(
            opinion=self.opinion,
            page_in_opinion=0,
            check_name=OpinionCheck.ENGINES_DISAGREE,
        )
        OpinionFindingFactory(opinion=self.opinion, dismissal=dismissal)

        response = self.client.get(self.url)

        self.assertEqual(response.context["open_findings"], 0)
        self.assertContains(response, "dismissed")

    def test_the_page_offers_no_write_control(self):
        """This stage has no write endpoint, so it offers no control.

        The header's sign-out form is the only form of the page, so the
        test looks for a form that posts to an opinion instead of for
        any form at all.
        """
        OpinionFindingFactory(opinion=self.opinion)

        response = self.client.get(self.url)

        body = response.content.decode()
        self.assertNotIn('action="/opinions/', body)
        self.assertNotIn("Dismiss", body)
        self.assertNotIn("Undo", body)
        self.assertNotIn("The opinion text is correct", body)

    def test_the_back_link_keeps_the_filters(self):
        response = self.client.get(self.url, {"scan": self.scan.pk})

        self.assertEqual(
            response.context["list_query"], f"scan={self.scan.pk}"
        )
        self.assertContains(
            response,
            f"{reverse('opinion_list')}?scan={self.scan.pk}",
        )

    def test_an_error_shows_its_reason(self):
        self.opinion.status = OpinionReviewStatus.ERROR
        self.opinion.error_message = "The glue failed."
        self.opinion.save(update_fields=["status", "error_message"])

        response = self.client.get(self.url)

        self.assertContains(response, "The glue failed.")
        # The four alert classes are alert-success, alert-info,
        # alert-warning and alert-danger. There is no alert-error.
        self.assertContains(response, "alert-danger")


class TestTheRedactedPdfPanel(ScanningTestCase):
    """The frame of the redacted PDF on the review page (#334/#336)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory()
        self.opinion = OpinionFactory(
            scan=self.scan,
            status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
        self.url = reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        self.pdf_url = reverse(
            "serve_opinion_pdf",
            kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
        )

    def _write_the_pdf(self):
        """Stamp the row as the PDF pass does, and read it back."""
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=self.opinion.glue_revision
        )

    def test_a_written_pdf_is_shown_in_a_frame(self):
        self._write_the_pdf()

        response = self.client.get(self.url)

        self.assertEqual(response.context["redacted_pdf_url"], self.pdf_url)
        self.assertContains(
            response, f'src="{self.pdf_url}?disposition=inline'
        )
        self.assertContains(response, "Download")

    def test_a_pdf_of_an_older_revision_is_not_shown(self):
        """The stamp must name the live revision, the one rule of #336."""
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=0, glue_revision=1
        )

        response = self.client.get(self.url)

        self.assertEqual(response.context["redacted_pdf_url"], "")
        self.assertContains(response, "The redacted PDF is not written yet.")

    def test_an_unwritten_pdf_gets_no_frame(self):
        response = self.client.get(self.url)

        self.assertEqual(response.context["redacted_pdf_url"], "")
        self.assertNotContains(response, "<iframe")

    def test_the_page_still_makes_no_s3_call(self):
        """The two ledgers are rows; the frame is a navigation."""
        self._write_the_pdf()

        with patch("scanning.s3_sync.s3_active") as active:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        active.assert_not_called()

    def test_only_a_staff_reader_gets_the_files_link(self):
        files_url = reverse(
            "opinion_file_index",
            kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
        )

        response = self.client.get(self.url)
        self.assertNotContains(response, files_url)

        self.client.force_login(self.make_staff_user())
        response = self.client.get(self.url)
        self.assertContains(response, files_url)


class TestOpinionFileIndex(ScanningTestCase):
    """The ``files`` index of one opinion (#334)."""

    def setUp(self):
        self.client.force_login(self.make_user())
        self.scan = ScanFactory()
        # A run that was read with dots.mocr and not with Mistral, the
        # shape of every volume today: the stage is switched off (#191).
        self.run = ApplyRun.objects.create(
            scan=self.scan, number=1, ocr_key="processing/1/a1/ocr.json"
        )
        self.opinion = OpinionFactory(
            scan=self.scan, first_printed_page=11, apply_run=self.run
        )
        self.url = reverse(
            "opinion_file_index",
            kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
        )

    def test_the_login_is_required(self):
        self.client.logout()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)

    def test_an_opinion_of_another_scan_is_a_404(self):
        other = OpinionFactory()

        response = self.client.get(
            reverse(
                "opinion_file_index",
                kwargs={"pk": self.scan.pk, "opinion_pk": other.pk},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_it_lists_every_glue_of_the_live_revision(self):
        response = self.client.get(self.url)

        body = response.json()
        self.assertEqual(body["opinion"], self.opinion.pk)
        self.assertEqual(body["glue_revision"], 0)
        self.assertEqual(body["prefix"], "jobs/opinions/11.0/r0/")
        self.assertEqual(
            [entry["name"] for entry in body["files"]],
            [
                "redacted.pdf",
                "dots_mocr.json",
                "mistral_ocr.json",
                "surya.json",
                "manifest.json",
            ],
        )
        for entry in body["files"]:
            self.assertTrue(
                entry["key"].endswith(
                    f"{self.opinion.glue_prefix}{entry['name']}"
                )
            )

    def test_an_object_that_is_not_glued_carries_no_url(self):
        response = self.client.get(self.url)

        for entry in response.json()["files"]:
            self.assertFalse(entry["written"])
            self.assertNotIn("url", entry)

    def test_an_engine_the_run_never_read_is_not_written(self):
        """The stamp is one over four files, the glue writes what it has.

        ``opinion_ocr.write`` writes one document per engine the run
        carries, so a stamped row of a volume nobody read with Mistral
        has no ``mistral_ocr.json`` in the bucket (#191).
        """
        Opinion.objects.filter(pk=self.opinion.pk).update(ocr_glue_revision=0)

        response = self.client.get(self.url)

        files = {entry["name"]: entry for entry in response.json()["files"]}
        self.assertTrue(files["dots_mocr.json"]["written"])
        self.assertTrue(files["manifest.json"]["written"])
        self.assertFalse(files["mistral_ocr.json"]["written"])
        self.assertNotIn("url", files["mistral_ocr.json"])
        self.assertFalse(files["surya.json"]["written"])

    def test_an_engine_the_run_read_is_written(self):
        ApplyRun.objects.filter(pk=self.run.pk).update(
            extract_key="processing/1/a1/extract.json"
        )
        Opinion.objects.filter(pk=self.opinion.pk).update(ocr_glue_revision=0)

        response = self.client.get(self.url)

        files = {entry["name"]: entry for entry in response.json()["files"]}
        self.assertTrue(files["mistral_ocr.json"]["written"])
        self.assertIn("url", files["mistral_ocr.json"])

    def test_a_row_in_the_original_space_writes_no_engine_document(self):
        """No run, no key, so no engine document can be in the bucket."""
        Opinion.objects.filter(pk=self.opinion.pk).update(
            apply_run=None, ocr_glue_revision=0
        )

        response = self.client.get(self.url)

        files = {entry["name"]: entry for entry in response.json()["files"]}
        self.assertFalse(files["dots_mocr.json"]["written"])
        self.assertTrue(files["manifest.json"]["written"])

    def test_a_written_object_carries_its_route(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(
            redacted_pdf_revision=0, ocr_glue_revision=0
        )

        response = self.client.get(self.url)

        files = {entry["name"]: entry for entry in response.json()["files"]}
        self.assertEqual(
            files["redacted.pdf"]["url"],
            reverse(
                "serve_opinion_pdf",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
            ),
        )
        self.assertEqual(
            files["manifest.json"]["url"],
            reverse(
                "serve_opinion_ocr",
                kwargs={
                    "pk": self.scan.pk,
                    "opinion_pk": self.opinion.pk,
                    "engine": "manifest",
                },
            ),
        )

    def test_it_makes_no_s3_call(self):
        """Every fact is on the row, so the index answers anywhere."""
        with patch("scanning.s3_sync.object_exists") as exists:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        exists.assert_not_called()


class TestTheStepThreeTab(ScanningTestCase):
    """Where the step chooser of a volume sends a curator (#334)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        self.url = reverse("scan_process", kwargs={"pk": self.scan.pk})

    def test_a_volume_with_opinions_links_the_filtered_list(self):
        OpinionFactory(scan=self.scan)

        response = self.client.get(self.url)

        self.assertEqual(response.context["review3_opinions"], 1)
        self.assertContains(
            response, f"{reverse('opinion_list')}?scan={self.scan.pk}"
        )
        self.assertContains(response, "Opinion text")

    def test_a_volume_with_no_opinion_has_a_dead_tab(self):
        response = self.client.get(self.url)

        self.assertEqual(response.context["review3_opinions"], 0)
        self.assertNotContains(
            response, f"{reverse('opinion_list')}?scan={self.scan.pk}"
        )
        self.assertContains(response, "Opinion text")
        self.assertContains(response, "cursor-not-allowed")

    def test_every_legacy_status_keeps_its_own_step_three(self):
        """A legacy volume also holds APPROVED and EXTRACTED (#334)."""
        for status in stats.LEGACY_STATUSES:
            with self.subTest(status=status):
                self.scan.status = status
                self.scan.save(update_fields=["status"])

                response = self.client.get(self.url)

                self.assertTrue(response.context["legacy_pipeline"])
                self.assertContains(response, 'href="?step=3"')
                self.assertContains(response, "Generate")
                self.assertNotContains(response, "Opinion text")

    def test_the_legacy_cards_carry_their_addresses(self):
        """The viewer reads the address off the card, never builds it."""
        self.scan.status = Status.PENDING_REVIEW
        self.scan.save(update_fields=["status"])
        opinion_scan = OpinionScanFactory(scan=self.scan)

        response = self.client.get(self.url, {"step": 3})

        for variant in ("redacted", "original"):
            self.assertContains(
                response,
                reverse(
                    "serve_opinionscan_pdf",
                    kwargs={"pk": opinion_scan.pk, "variant": variant},
                ),
            )


class TestTheNextButton(ScanningTestCase):
    """Where the step-2 bar sends a curator next (#334)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        self.url = reverse("scan_process", kwargs={"pk": self.scan.pk})

    def _bar(self, **params) -> str:
        """Return the step-2 action bar of the page."""
        response = self.client.get(self.url, {"step": 2, **params})
        return response.content.decode()

    def test_a_volume_with_opinions_is_sent_to_them(self):
        OpinionFactory(scan=self.scan)

        body = self._bar()

        self.assertIn("Next: Opinion text", body)
        self.assertIn(f"{reverse('opinion_list')}?scan={self.scan.pk}", body)
        self.assertNotIn("Next: Generate", body)

    def test_a_new_volume_with_no_opinion_gets_no_button(self):
        OpinionBoundaryFactory(scan=self.scan)

        body = self._bar()

        self.assertNotIn("Next: Opinion text", body)
        self.assertNotIn("Next: Generate", body)

    def test_a_legacy_volume_keeps_the_generate_button(self):
        self.scan.status = Status.PENDING_REVIEW
        self.scan.save(update_fields=["status"])
        OpinionBoundaryFactory(scan=self.scan)

        body = self._bar()

        self.assertIn("Next: Generate", body)
        self.assertNotIn("Next: Opinion text", body)
