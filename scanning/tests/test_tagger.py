"""Tests for the tagger stage (``scanning/tagger.py``, #272): one row per
approved opinion, the input written once by digest from the approved
text, the glue that places the spans on that text, and the button of
the review page that is its only caller.

No HTTP and no S3: ``s3_sync`` is patched at the functions the stage
calls, and the approved text is a dict in the shape of
``paragraphs.approved_document``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib import messages
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse

from scanning import jobs, markup, paragraphs, runpod_client, tagger
from scanning.factories import OpinionFactory, ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    OpinionReviewStatus,
    Status,
)
from scanning.tests.test_views import ScanningTestCase
from scanning.views_api import TAG_PLACED_MESSAGE

TAGGER = {
    "RUNPOD_ENABLED": True,
    "RUNPOD_API_KEY": "key-1",
    "RUNPOD_PRESIGNED_TTL": 3600,
    "RUNPOD_REQUEST_TIMEOUT": 600,
    "TAGGER_ENABLED": True,
    "RUNPOD_TAGGER_ENDPOINT_ID": "ep-tagger",
    "TAGGER_MAX_CONCURRENCY": 3,
    "TAGGER_MAX_ATTEMPTS": 3,
    "TAGGER_SECONDS_PER_PAGE": 1.0,
    "DOCTOR_ENABLED": False,
}

APPROVED_KEY = "processing/x/jobs/opinions/502.0/approved/r1.e0.j1.t1.json"

CAPTION = ("Jane ROE, Appellant,", "v.", "STATE of Example, Appellee.")


def paragraph(text, **fields):
    return {
        "kind": fields.pop("kind", "paragraph"),
        "blockquote": fields.pop("blockquote", False),
        "text": text,
        "marks": fields.pop("marks", []),
        "pages": [0],
        "page_breaks": [],
        "joins": [],
        "human": False,
    }


def approved(*texts, footnotes=()):
    """An approved object of two pages, in the shape of ``paragraphs``."""
    return {
        "schema": paragraphs.APPROVED_SCHEMA,
        "join_rule": paragraphs.JOIN_RULE,
        "opinion": {"first_printed_page": 502, "index_in_page": 0},
        "approved_by": "curator",
        "approved_at": "2026-09-26T00:00:00+00:00",
        "engines": ["dots_mocr", "mistral_ocr", "surya"],
        "pages": [
            {"page_in_opinion": 0, "page_index": 10, "printed": "502"},
            {"page_in_opinion": 1, "page_index": 11, "printed": "503"},
        ],
        "body": [paragraph(text) for text in texts],
        "footnotes": list(footnotes),
    }


def tag_jobs(opinion):
    return list(
        ExternalJob.objects.filter(
            opinion=opinion,
            stage=JobStage.TAG,
            engine=JobEngine.CASELAW_TAGGER,
        ).order_by("run")
    )


class _S3Case(ScanningTestCase):
    """An approved opinion and an S3 that answers without a network."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        self.opinion = OpinionFactory(
            scan=self.scan,
            first_printed_page=502,
            status=OpinionReviewStatus.TEXT_REVIEW_DONE,
            approved_text_key=APPROVED_KEY,
        )
        self.stored: dict[str, dict] = {APPROVED_KEY: approved(*CAPTION)}
        self.enterContext(
            patch("scanning.s3_sync.s3_active", return_value=True)
        )
        self.download = self.enterContext(
            patch(
                "scanning.s3_sync.download_json_object",
                side_effect=lambda key: json.loads(
                    json.dumps(self.stored[key])
                ),
            )
        )
        self.enterContext(
            patch(
                "scanning.s3_sync.object_exists",
                side_effect=lambda key: key in self.stored,
            )
        )
        self.upload = self.enterContext(
            patch(
                "scanning.s3_sync.upload_json_object", side_effect=self._store
            )
        )

    def _store(self, key, data):
        self.stored[key] = json.loads(json.dumps(data))
        return True

    def approve_again(self, *texts, key=APPROVED_KEY + ".2"):
        """A second approval: another object, and the row moves to it."""
        self.stored[key] = approved(*texts)
        self.opinion.approved_text_key = key
        self.opinion.save(update_fields=["approved_text_key"])
        return key

    def completed_row(self):
        row = tagger.ensure_tag_jobs(self.opinion)[0]
        row.result_key = f"{tagger.prefix(self.opinion)}r{row.run}-s0-a1.json"
        row.status = JobStatus.COMPLETED
        row.attempt = 1
        row.save()
        return row

    def envelope(self, row, spans=None, **overrides):
        text = self.stored[row.input_key]["sequences"][0]["text"]
        if spans is None:
            start = text.index("Jane")
            spans = [
                {
                    "start": start,
                    "end": start + len("Jane ROE, Appellant"),
                    "label": "party",
                    "text": "Jane ROE, Appellant",
                }
            ]
        env = {
            "schema_version": runpod_client.RESULT_SCHEMA_VERSION,
            "action": "tag",
            "scan_pk": self.scan.pk,
            "result_key": row.result_key,
            "payload": {
                "sequences": [
                    {
                        "id": tagger.sequence_id(self.opinion),
                        "token_count": 40,
                        "window_count": 1,
                        "spans": spans,
                    }
                ],
                "sequence_count": 1,
                "failed_sequences": [],
                "model": "freelawproject/caselaw-block-tagger",
                "max_tokens": 8192,
            },
        }
        env.update(overrides)
        return env

    def finished_row(self):
        row = self.completed_row()
        self.stored[row.result_key] = self.envelope(row)
        return row


class TestEnabled(ScanningTestCase):
    @override_settings(**TAGGER)
    def test_on_when_every_switch_is_set(self):
        self.assertTrue(tagger.enabled())

    @override_settings(**{**TAGGER, "TAGGER_ENABLED": False})
    def test_the_stage_switch(self):
        self.assertFalse(tagger.enabled())

    @override_settings(**{**TAGGER, "RUNPOD_TAGGER_ENDPOINT_ID": ""})
    def test_a_blank_endpoint_turns_this_engine_off_alone(self):
        self.assertFalse(tagger.enabled())

    def test_the_engine_table_knows_the_row(self):
        row = ExternalJob(
            scan=ScanFactory(),
            stage=JobStage.TAG,
            engine=JobEngine.CASELAW_TAGGER,
            provider=JobProvider.RUNPOD,
        )
        spec = jobs._runpod_engine(row)
        self.assertEqual(spec.endpoint_setting, "RUNPOD_TAGGER_ENDPOINT_ID")
        self.assertIs(spec.build_payload, tagger.build_payload)


class TestEnsureTagJobs(_S3Case):
    def test_one_row_per_opinion_with_the_input_written_by_digest(self):
        rows = tagger.ensure_tag_jobs(self.opinion)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.stage, row.engine, row.provider, row.status, row.opinion_id),
            (
                JobStage.TAG,
                JobEngine.CASELAW_TAGGER,
                JobProvider.RUNPOD,
                JobStatus.PENDING,
                self.opinion.pk,
            ),
        )
        self.assertTrue(
            row.input_key.startswith(f"{tagger.prefix(self.opinion)}input-"),
            row.input_key,
        )
        self.assertIn("jobs/opinions/502.0/tag/", row.input_key)
        sequences = self.stored[row.input_key]["sequences"]
        text = (
            "<p>Jane ROE, Appellant,</p>\n<p>v.</p>\n"
            "<p>STATE of Example, Appellee.</p>"
        )
        self.assertEqual(
            sequences, [{"id": tagger.sequence_id(self.opinion), "text": text}]
        )
        self.assertEqual(
            row.input_manifest,
            {
                "digest": tagger.text_digest(text),
                "projection": tagger.PROJECTION_VERSION,
                "chars": len(text),
                "paragraphs": 3,
                "page_count": 1,
            },
        )

    def test_the_input_is_the_projection_of_the_body(self):
        row = tagger.ensure_tag_jobs(self.opinion)[0]

        self.assertEqual(
            self.stored[row.input_key]["sequences"][0]["text"],
            markup.project(self.stored[APPROVED_KEY]["body"]).text,
        )

    def test_the_footnotes_are_not_sent(self):
        self.stored[APPROVED_KEY]["footnotes"] = [
            {
                "label": "1",
                "pages": [1],
                "paragraphs": [
                    paragraph("A footnote the tagger never reads.")
                ],
            }
        ]

        row = tagger.ensure_tag_jobs(self.opinion)[0]

        self.assertNotIn(
            "footnote", self.stored[row.input_key]["sequences"][0]["text"]
        )

    def test_a_second_call_reuses_the_run_and_writes_nothing(self):
        first = tagger.ensure_tag_jobs(self.opinion)
        writes = self.upload.call_count

        second = tagger.ensure_tag_jobs(self.opinion)

        self.assertEqual([r.pk for r in first], [r.pk for r in second])
        self.assertEqual(self.upload.call_count, writes)

    def test_the_page_count_comes_from_the_text(self):
        long = ("word " * 1300).strip()
        self.stored[APPROVED_KEY] = approved(long, long)

        row = tagger.ensure_tag_jobs(self.opinion)[0]

        chars = row.input_manifest["chars"]
        self.assertGreater(chars, 2 * tagger.CHARS_PER_PAGE)
        self.assertEqual(
            row.input_manifest["page_count"],
            -(-chars // tagger.CHARS_PER_PAGE),
        )

    def test_another_page_table_with_the_same_text_reuses_the_run(self):
        first = tagger.ensure_tag_jobs(self.opinion)[0]
        key = self.approve_again(*CAPTION)
        self.stored[key]["pages"].append(
            {"page_in_opinion": 2, "page_index": 12, "printed": "504"}
        )

        second = tagger.ensure_tag_jobs(self.opinion)[0]

        self.assertEqual(second.pk, first.pk)

    def test_a_second_approval_of_the_same_text_reuses_the_run(self):
        first = tagger.ensure_tag_jobs(self.opinion)[0]
        self.approve_again(*CAPTION)

        second = tagger.ensure_tag_jobs(self.opinion)[0]

        self.assertEqual(second.pk, first.pk)

    def test_a_changed_text_starts_a_new_run(self):
        first = tagger.ensure_tag_jobs(self.opinion)[0]
        self.approve_again("Jane ROE, Appellant,", "v.", "STATE, Appellee.")

        second = tagger.ensure_tag_jobs(self.opinion)[0]

        self.assertEqual((first.run, second.run), (1, 2))
        self.assertNotEqual(first.input_key, second.input_key)

    def test_a_dead_row_forces_a_fresh_run(self):
        first = tagger.ensure_tag_jobs(self.opinion)[0]
        ExternalJob.objects.filter(pk=first.pk).update(status=JobStatus.FAILED)

        second = tagger.ensure_tag_jobs(self.opinion)[0]

        self.assertEqual(second.run, 2)

    def test_the_run_of_one_opinion_leaves_the_others_alone(self):
        other = OpinionFactory(
            scan=self.scan,
            first_printed_page=502,
            index_in_page=1,
            status=OpinionReviewStatus.TEXT_REVIEW_DONE,
            approved_text_key=APPROVED_KEY,
        )
        mine = tagger.ensure_tag_jobs(self.opinion)[0]

        theirs = tagger.ensure_tag_jobs(other)[0]

        self.assertNotEqual(mine.pk, theirs.pk)
        self.assertEqual((mine.run, theirs.run), (1, 1))
        self.assertEqual(tagger.live_tag_jobs(self.opinion), [mine])

    def test_an_opinion_not_approved_is_refused(self):
        self.opinion.status = OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        self.opinion.save(update_fields=["status"])

        with self.assertRaises(tagger.TaggerInputError):
            tagger.ensure_tag_jobs(self.opinion)
        self.assertEqual(tag_jobs(self.opinion), [])

    def test_an_approved_text_of_another_schema_is_refused(self):
        self.stored[APPROVED_KEY]["schema"] = paragraphs.APPROVED_SCHEMA + 1

        with self.assertRaises(tagger.TaggerInputError):
            tagger.ensure_tag_jobs(self.opinion)

    def test_no_s3_is_refused(self):
        with (
            patch("scanning.s3_sync.s3_active", return_value=False),
            self.assertRaises(tagger.TaggerInputError),
        ):
            tagger.ensure_tag_jobs(self.opinion)

    def test_build_payload(self):
        row = tagger.ensure_tag_jobs(self.opinion)[0]
        row.result_key = "jobs/opinions/502.0/tag/r1-s0-a1.json"

        self.assertEqual(
            tagger.build_payload(row, "https://get", "https://put"),
            {
                "action": "tag",
                "scan_pk": self.scan.pk,
                "input_url": "https://get",
                "result_url": "https://put",
                "result_key": "jobs/opinions/502.0/tag/r1-s0-a1.json",
            },
        )

    @override_settings(**TAGGER)
    def test_the_wave_submits_the_row_with_the_payload(self):
        tagger.ensure_tag_jobs(self.opinion)
        with (
            patch("scanning.s3_sync.presign_get", return_value="https://get"),
            patch("scanning.s3_sync.presign_put", return_value="https://put"),
            patch(
                "scanning.runpod_client.submit_job", return_value="job-1"
            ) as submit,
        ):
            jobs.submit_pending()

        submit.assert_called_once()
        endpoint, _headers, payload = submit.call_args[0][:3]
        self.assertTrue(endpoint.endswith("/ep-tagger"), endpoint)
        self.assertEqual(payload["input_url"], "https://get")
        row = tag_jobs(self.opinion)[0]
        self.assertEqual(row.status, JobStatus.SUBMITTED)
        self.assertEqual(row.external_id, "job-1")


class TestGlue(_S3Case):
    def test_a_completed_run_is_placed_and_consumed(self):
        row = self.finished_row()

        self.assertEqual(tagger.finish_ready_runs(), 1)

        row.refresh_from_db()
        self.opinion.refresh_from_db()
        self.assertEqual(row.status, JobStatus.CONSUMED)
        self.assertTrue(tagger.is_written(self.opinion))
        self.assertEqual(self.opinion.tagged_text_key, APPROVED_KEY)
        spans = self.stored[self.opinion.tag_key]
        self.assertEqual(
            spans["spans"],
            [{"paragraph": 0, "start": 0, "end": 19, "label": "party"}],
        )
        self.assertEqual(CAPTION[0][0:19], "Jane ROE, Appellant")
        self.assertEqual(spans["approved_text_key"], APPROVED_KEY)
        self.assertEqual(spans["raw"]["spans"][0]["label"], "party")

    def test_a_span_over_two_blocks_is_cut_per_paragraph(self):
        row = self.completed_row()
        text = self.stored[row.input_key]["sequences"][0]["text"]
        start = text.index("Jane")
        end = text.index("v.") + 2
        self.stored[row.result_key] = self.envelope(
            row, spans=[{"start": start, "end": end, "label": "party"}]
        )

        tagger.finish_ready_runs()

        self.opinion.refresh_from_db()
        self.assertEqual(
            self.stored[self.opinion.tag_key]["spans"],
            [
                {"paragraph": 0, "start": 0, "end": 20, "label": "party"},
                {"paragraph": 1, "start": 0, "end": 2, "label": "party"},
            ],
        )

    def test_a_text_approved_again_after_the_press_is_not_placed(self):
        row = self.finished_row()
        self.approve_again("Another text.")

        self.assertEqual(tagger.finish_ready_runs(), 0)

        row.refresh_from_db()
        self.opinion.refresh_from_db()
        self.assertEqual(row.status, JobStatus.CONSUMED)
        self.assertFalse(tagger.is_written(self.opinion))
        self.assertEqual(tagger.state(self.opinion), tagger.STALE)

    def test_a_foreign_envelope_is_refused_and_counted(self):
        row = self.completed_row()
        self.stored[row.result_key] = self.envelope(row, scan_pk=-1)

        self.assertEqual(tagger.finish_ready_runs(), 0)

        row.refresh_from_db()
        self.assertEqual(row.status, JobStatus.COMPLETED)
        self.assertEqual(row.provider_meta["glue"]["attempts"], 1)
        self.assertEqual(tagger.state(self.opinion), tagger.RUNNING)

    def test_a_span_with_no_start_is_a_counted_fault(self):
        row = self.completed_row()
        self.stored[row.result_key] = self.envelope(
            row, spans=[{"end": 4, "label": "party"}]
        )

        self.assertEqual(tagger.finish_ready_runs(), 0)

        row.refresh_from_db()
        self.assertEqual(row.status, JobStatus.COMPLETED)
        self.assertIn("span", row.provider_meta["glue"]["last_error"])

    def test_out_of_tries_is_left_alone_and_reads_as_failed(self):
        row = self.completed_row()
        row.provider_meta = {"glue": {"attempts": tagger.GLUE_MAX_ATTEMPTS}}
        row.save(update_fields=["provider_meta"])
        self.stored[row.result_key] = self.envelope(row)

        self.assertEqual(tagger.finish_ready_runs(), 0)

        row.refresh_from_db()
        self.assertEqual(row.status, JobStatus.COMPLETED)
        self.assertEqual(tagger.state(self.opinion), tagger.FAILED)

    def test_a_pending_row_is_not_glued(self):
        tagger.ensure_tag_jobs(self.opinion)

        self.assertEqual(tagger.finish_ready_runs(), 0)
        self.assertEqual(tagger.state(self.opinion), tagger.RUNNING)

    def test_a_reopen_keeps_the_spans_valid(self):
        self.finished_row()
        tagger.finish_ready_runs()
        self.opinion.refresh_from_db()
        self.opinion.status = OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        self.opinion.save(update_fields=["status"])

        self.assertTrue(tagger.is_written(self.opinion))


class TestState(_S3Case):
    def test_before_any_run(self):
        self.assertEqual(tagger.state(self.opinion), tagger.NONE)

    def test_a_dead_row(self):
        row = tagger.ensure_tag_jobs(self.opinion)[0]
        ExternalJob.objects.filter(pk=row.pk).update(status=JobStatus.FAILED)

        self.assertEqual(tagger.state(self.opinion), tagger.FAILED)

    def test_placed_spans(self):
        self.finished_row()
        tagger.finish_ready_runs()
        self.opinion.refresh_from_db()

        self.assertEqual(tagger.state(self.opinion), tagger.DONE)


@override_settings(**TAGGER)
class TestTheButton(_S3Case):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def post(self, scan=None):
        self.client.cookies.pop("messages", None)
        return self.client.post(
            reverse(
                "start_caselaw_tagger",
                kwargs={
                    "pk": (scan or self.scan).pk,
                    "opinion_pk": self.opinion.pk,
                },
            )
        )

    def said(self, response):
        return [
            (message.level, message.message)
            for message in get_messages(response.wsgi_request)
        ]

    def test_any_logged_in_user_starts_the_job(self):
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(len(tag_jobs(self.opinion)), 1)
        self.assertEqual(
            self.said(response),
            [(messages.SUCCESS, response.json()["message"])],
        )

    def test_an_opinion_of_another_scan_is_404(self):
        response = self.post(scan=ScanFactory())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(tag_jobs(self.opinion), [])

    def test_an_opinion_not_approved_is_refused(self):
        self.opinion.status = OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        self.opinion.save(update_fields=["status"])

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(tag_jobs(self.opinion), [])

    @override_settings(RUNPOD_TAGGER_ENDPOINT_ID="")
    def test_a_disabled_stage_is_refused(self):
        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(tag_jobs(self.opinion), [])

    def test_a_running_job_is_refused(self):
        self.post()

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(tag_jobs(self.opinion)), 1)

    def test_a_tagged_text_is_refused(self):
        self.finished_row()
        tagger.finish_ready_runs()

        response = self.post()

        self.assertEqual(response.status_code, 409)

    def test_a_second_approval_of_the_same_text_is_placed_with_no_job(self):
        row = self.finished_row()
        tagger.finish_ready_runs()
        key = self.approve_again(*CAPTION)

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], TAG_PLACED_MESSAGE)
        self.assertEqual([r.pk for r in tag_jobs(self.opinion)], [row.pk])
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.tagged_text_key, key)
        self.assertTrue(tagger.is_written(self.opinion))

    def gave_up(self, envelope=None):
        """A completed row whose glue reached the cap on the tick."""
        row = self.completed_row()
        row.provider_meta = {"glue": {"attempts": tagger.GLUE_MAX_ATTEMPTS}}
        row.save(update_fields=["provider_meta"])
        self.stored[row.result_key] = envelope or self.envelope(row)
        return row

    def test_a_press_after_the_glue_gave_up_places_the_spans(self):
        row = self.gave_up()

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], TAG_PLACED_MESSAGE)
        row.refresh_from_db()
        self.opinion.refresh_from_db()
        self.assertEqual(row.status, JobStatus.CONSUMED)
        self.assertTrue(tagger.is_written(self.opinion))
        self.assertEqual([r.pk for r in tag_jobs(self.opinion)], [row.pk])

    def test_a_press_after_the_glue_gave_up_answers_the_fault(self):
        row = self.completed_row()
        row = self.gave_up(self.envelope(row, scan_pk=-1))

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertIn("envelope", response.json()["message"])
        row.refresh_from_db()
        self.assertEqual(row.status, JobStatus.COMPLETED)
        self.assertEqual(
            row.provider_meta["glue"]["attempts"], tagger.GLUE_MAX_ATTEMPTS + 1
        )
        self.assertEqual([r.pk for r in tag_jobs(self.opinion)], [row.pk])
        self.assertEqual(tagger.state(self.opinion), tagger.FAILED)

    def test_a_malformed_span_is_a_refusal_and_not_a_crash(self):
        row = self.completed_row()
        self.gave_up(self.envelope(row, spans=[{"label": "party"}]))

        response = self.post()

        self.assertEqual(response.status_code, 409)

    def test_a_carried_run_is_placed_in_the_request(self):
        # A, then B, then A again: the third run carries the result of
        # the first, is born COMPLETED, and no job runs.
        first = self.finished_row()
        tagger.finish_ready_runs()
        self.approve_again("Another text.", key=APPROVED_KEY + ".b")
        second = tagger.ensure_tag_jobs(self.opinion)[0]
        ExternalJob.objects.filter(pk=second.pk).update(
            status=JobStatus.FAILED
        )
        key = self.approve_again(*CAPTION, key=APPROVED_KEY + ".a")

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], TAG_PLACED_MESSAGE)
        third = tag_jobs(self.opinion)[-1]
        self.assertEqual(third.run, 3)
        self.assertEqual(third.status, JobStatus.CONSUMED)
        self.assertEqual(third.result_key, first.result_key)
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.tagged_text_key, key)

    def test_an_input_that_does_not_load_is_a_refusal(self):
        self.download.side_effect = ValueError("no such key")

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertIn("no such key", response.json()["message"])
        self.assertEqual(tag_jobs(self.opinion), [])


@override_settings(**TAGGER)
class TestThePage(_S3Case):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def page(self):
        return self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

    def test_an_approved_text_offers_the_button_to_any_user(self):
        response = self.page()

        self.assertContains(response, 'id="start-tagger"')
        self.assertContains(
            response,
            reverse(
                "start_caselaw_tagger",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
            ),
        )

    def test_a_text_in_review_offers_no_button(self):
        self.opinion.status = OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        self.opinion.save(update_fields=["status"])

        self.assertNotContains(self.page(), 'id="start-tagger"')

    def test_a_running_job_shows_its_state_and_no_button(self):
        tagger.ensure_tag_jobs(self.opinion)

        response = self.page()

        self.assertNotContains(response, 'id="start-tagger"')
        self.assertContains(response, "Tagging the text")

    @override_settings(RUNPOD_TAGGER_ENDPOINT_ID="")
    def test_a_disabled_stage_offers_no_button(self):
        self.assertNotContains(self.page(), 'id="start-tagger"')

    def test_the_file_index_names_the_spans(self):
        self.finished_row()
        tagger.finish_ready_runs()
        self.opinion.refresh_from_db()

        response = self.client.get(
            reverse(
                "opinion_file_index",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
            )
        )

        entry = next(
            f
            for f in response.json()["files"]
            if f["output"] == "opinion-tags"
        )
        self.assertTrue(entry["written"])
        self.assertEqual(entry["key"], self.opinion.tag_key)
        self.assertIn("url", entry)

    def test_the_spans_route_is_404_before_the_glue(self):
        response = self.client.get(
            reverse(
                "serve_opinion_tags",
                kwargs={"pk": self.scan.pk, "opinion_pk": self.opinion.pk},
            )
        )

        self.assertEqual(response.status_code, 404)
