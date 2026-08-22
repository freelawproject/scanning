"""Tests for scanning.s3_sync with mocked boto3."""

import os
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from scanning import s3_sync
from scanning.factories import ReporterFactory, ScanFactory
from scanning.models import Status

MEDIA_ROOT = tempfile.mkdtemp()


def _fake_download(bucket, key, dest):
    """Stand in for ``boto3``'s ``download_file``, including its temp file.

    s3transfer writes to ``<dest>.<random>`` in the destination's own
    directory and renames it into place, so downloading into a directory
    that does not exist yet fails on that temp name. Creating the parent
    here instead would make every caller look correct.

    :param bucket: Unused; matches the signature being faked.
    :param key: Unused; matches the signature being faked.
    :param dest: Local path to write to.
    """
    del bucket, key
    dest = pathlib.Path(dest)
    tmp = dest.with_suffix(dest.suffix + ".abc12345")
    tmp.write_bytes(b"12345")
    tmp.replace(dest)


def _reporter_scan(**kwargs):
    """Build a scan with a reporter for predictable S3 prefixes.

    :returns: A Scan instance with reporter/volume/start_page set.
    :rtype: Scan
    """
    reporter = ReporterFactory(short_name="tc")
    defaults = {
        "reporter": reporter,
        "volume": 164,
        "start_page": 1,
        "end_page": 2,
    }
    defaults.update(kwargs)
    return ScanFactory(**defaults)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DEVELOPMENT=True,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
)
class TestShortCircuitOnDevelopment(TestCase):
    """When DEVELOPMENT=True, sync helpers must never call boto3."""

    def test_upload_processing_files_noop(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.upload_processing_files(scan)
        self.assertEqual(result, 0)
        mock_boto3.client.assert_not_called()

    def test_download_processing_files_noop(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.download_processing_files(scan)
        self.assertIsNone(result)
        mock_boto3.client.assert_not_called()

    def test_upload_file_to_s3_noop(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.upload_file_to_s3(scan, "detections.json")
        self.assertFalse(result)
        mock_boto3.client.assert_not_called()


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DEVELOPMENT=False,
    TESTING=False,
    AWS_ACCESS_KEY_ID="fake",
    AWS_SECRET_ACCESS_KEY="fake",
    AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
    PROCESSING_TMP_DIR=tempfile.mkdtemp(),
)
class TestSyncHelpersWithCredentials(TestCase):
    """With creds and DEV=False, helpers should hit boto3."""

    def setUp(self):
        # _s3_client() is lru_cached; drop any client cached under a prior
        # test's boto3 patch so this test's mock is the one that's used.
        s3_sync._cached_s3_client.cache_clear()
        # has_s3_credentials reads os.environ, not Django settings.
        self._env_patch = patch.dict(
            os.environ,
            {"AWS_ACCESS_KEY_ID": "fake", "AWS_SECRET_ACCESS_KEY": "fake"},
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_prefix_is_deterministic(self):
        scan = _reporter_scan()
        expected = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(s3_sync.s3_processing_prefix(scan), expected)

    def test_upload_processing_files_walks_local_root(self):
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"pdf")
        (local_root / "detections.json").write_text("[]")
        (local_root / "images").mkdir()
        (local_root / "images" / "1-001.png").write_bytes(b"png")
        # Now these should also be uploaded (recursive walk).
        (local_root / "redacted").mkdir()
        (local_root / "redacted" / "op.pdf").write_bytes(b"r")
        (local_root / "unredacted").mkdir()
        (local_root / "unredacted" / "op.pdf").write_bytes(b"u")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 5)
        uploaded_keys = {
            call.args[2] for call in mock_s3.upload_file.call_args_list
        }
        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(
            uploaded_keys,
            {
                f"{prefix}bitonal.pdf",
                f"{prefix}detections.json",
                f"{prefix}images/1-001.png",
                f"{prefix}redacted/op.pdf",
                f"{prefix}unredacted/op.pdf",
            },
        )

    def test_job_result_key_is_per_scan_and_stage(self):
        scan = _reporter_scan()
        key = s3_sync.s3_job_result_key(scan, "detect")
        self.assertEqual(
            key,
            f"processing/{scan.pk}/tc/164/1/jobs/detect/result.json",
        )

    def test_upload_processing_files_skips_job_results(self):
        # GPU job results are wire artifacts the daemon consumes into
        # Postgres. If one ever lands in the local tree it must not be
        # pushed back up with the deliverables.
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        (local_root / "jobs" / "detect").mkdir(parents=True)
        (local_root / "jobs" / "detect" / "result.json").write_text("{}")
        (local_root / "bitonal.pdf").write_bytes(b"pdf")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 1)
        uploaded_keys = {
            call.args[2] for call in mock_s3.upload_file.call_args_list
        }
        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(uploaded_keys, {f"{prefix}bitonal.pdf"})

    def test_unchanged_stage_inputs_are_not_re_uploaded(self):
        # A stage input's LastModified is the reference timestamp for GPU
        # job-result reuse. Re-uploading identical bytes would advance it
        # and make every stored result read as stale, so a pipeline that
        # merely finished must leave both inputs untouched.
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"pdf")
        (local_root / "tc.164.1.2.original.pdf").write_bytes(b"original")
        (local_root / "detections.json").write_text("[]")

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = lambda Bucket, Key: {
            "ContentLength": 8 if Key.endswith(".original.pdf") else 3
        }
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 1)
        uploaded_keys = {
            call.args[2] for call in mock_s3.upload_file.call_args_list
        }
        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(uploaded_keys, {f"{prefix}detections.json"})

    def test_edited_stage_input_is_re_uploaded(self):
        # Page edits rewrite bitonal.pdf in place. A different size means
        # the pages changed, so it must go up and its timestamp must move.
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"much longer pdf now")

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 3}
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 1)
        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(
            {call.args[2] for call in mock_s3.upload_file.call_args_list},
            {f"{prefix}bitonal.pdf"},
        )

    def test_missing_remote_stage_input_is_uploaded(self):
        # Nothing there to compare against: upload, don't skip.
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"pdf")

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "404"}}, "HeadObject"
        )
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 1)

    def test_invalidate_job_results_drops_both_stages(self):
        # The escape hatch for "re-run this even though the pages are the
        # same": the timestamp check can't express that, so the caller
        # removes the objects instead.
        scan = _reporter_scan()
        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            s3_sync.invalidate_job_results(scan)

        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(
            {c.kwargs["Key"] for c in mock_s3.delete_object.call_args_list},
            {
                f"{prefix}jobs/detect/result.json",
                f"{prefix}jobs/analyze/result.json",
            },
        )

    def test_invalidate_job_results_survives_a_failed_delete(self):
        # Best effort: it runs inside a request and an admin action, and
        # a leftover object costs only a wasted reuse.
        scan = _reporter_scan()
        mock_s3 = MagicMock()
        mock_s3.delete_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}},
            "DeleteObject",
        )
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            s3_sync.invalidate_job_results(scan)

        self.assertEqual(mock_s3.delete_object.call_count, 2)

    def test_download_processing_files_skips_job_results(self):
        scan = _reporter_scan()
        prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}bitonal.pdf", "Size": 5},
                    {
                        "Key": f"{prefix}jobs/detect/result.json",
                        "Size": 9,
                    },
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            s3_sync.download_processing_files(scan)

        downloaded = {c.args[1] for c in mock_s3.download_file.call_args_list}
        self.assertEqual(downloaded, {f"{prefix}bitonal.pdf"})

    def test_upload_processing_files_skips_shards(self):
        # The shard set duplicates the original's bytes and is pushed
        # once by sharding.ensure_shards; the end-of-pipeline push must
        # not re-upload gigabytes of it (there is no hash check).
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        (local_root / "shards").mkdir(parents=True)
        (local_root / "shards" / "0001.pdf").write_bytes(b"pdf")
        (local_root / "shards" / "manifest.json").write_text("{}")
        (local_root / "bitonal.pdf").write_bytes(b"pdf")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 1)
        uploaded_keys = {
            call.args[2] for call in mock_s3.upload_file.call_args_list
        }
        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(uploaded_keys, {f"{prefix}bitonal.pdf"})

    def test_download_processing_files_skips_shards(self):
        scan = _reporter_scan()
        prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}bitonal.pdf", "Size": 5},
                    {"Key": f"{prefix}shards/0001.pdf", "Size": 9},
                    {"Key": f"{prefix}shards/manifest.json", "Size": 2},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator
        mock_s3.download_file.side_effect = _fake_download

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            s3_sync.download_processing_files(scan)

        downloaded = {c.args[1] for c in mock_s3.download_file.call_args_list}
        self.assertEqual(downloaded, {f"{prefix}bitonal.pdf"})

    def test_shards_prefix(self):
        scan = _reporter_scan()
        self.assertEqual(
            s3_sync.shards_prefix(scan),
            f"processing/{scan.pk}/tc/164/1/shards/",
        )

    def test_fetch_shard_manifest_parses_json(self):
        scan = _reporter_scan()
        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b'{"version": 1, "shards": []}'
        mock_s3.get_object.return_value = {"Body": body}

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            manifest = s3_sync.fetch_shard_manifest(scan)

        self.assertEqual(manifest, {"version": 1, "shards": []})
        self.assertEqual(
            mock_s3.get_object.call_args.kwargs["Key"],
            f"processing/{scan.pk}/tc/164/1/shards/manifest.json",
        )

    def test_fetch_shard_manifest_missing_returns_none(self):
        scan = _reporter_scan()
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            self.assertIsNone(s3_sync.fetch_shard_manifest(scan))

    def test_fetch_shard_manifest_reraises_unexpected_s3_errors(self):
        # Only "the manifest is not there" may map to None: a throttle
        # or IAM error must not read as "no committed shard set", or
        # ensure_shards would delete and re-cut a healthy multi-GB set
        # (PR #169 review).
        scan = _reporter_scan()
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "SlowDown"}}, "GetObject"
        )

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            with self.assertRaises(ClientError):
                s3_sync.fetch_shard_manifest(scan)

    def test_fetch_shard_manifest_rejects_non_object_json(self):
        # Parseable JSON that isn't a manifest gets the unparseable
        # treatment: None, so ensure_shards re-cuts instead of crashing
        # on it downstream.
        scan = _reporter_scan()
        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b'["not", "a", "manifest"]'
        mock_s3.get_object.return_value = {"Body": body}

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            self.assertIsNone(s3_sync.fetch_shard_manifest(scan))

    def test_upload_shards_sends_manifest_last(self):
        # The manifest is the commit marker: a reader that finds it can
        # trust every shard it lists is already in the bucket.
        scan = _reporter_scan()
        shard_dir = pathlib.Path(scan.output_dir) / "shards"
        shard_dir.mkdir(parents=True)
        (shard_dir / "0002.pdf").write_bytes(b"pdf2")
        (shard_dir / "0001.pdf").write_bytes(b"pdf1")
        (shard_dir / "manifest.json").write_text("{}")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_shards(scan, shard_dir)

        self.assertEqual(count, 3)
        prefix = f"processing/{scan.pk}/tc/164/1/shards/"
        keys = [c.args[2] for c in mock_s3.upload_file.call_args_list]
        self.assertEqual(
            keys,
            [
                f"{prefix}0001.pdf",
                f"{prefix}0002.pdf",
                f"{prefix}manifest.json",
            ],
        )

    def test_delete_shard_objects_sweeps_the_prefix(self):
        scan = _reporter_scan()
        prefix = f"processing/{scan.pk}/tc/164/1/shards/"
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}0001.pdf"},
                    {"Key": f"{prefix}manifest.json"},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            deleted = s3_sync.delete_shard_objects(scan)

        self.assertEqual(deleted, 2)
        mock_s3.delete_objects.assert_called_once_with(
            Bucket="test-bucket",
            Delete={
                "Objects": [
                    {"Key": f"{prefix}0001.pdf"},
                    {"Key": f"{prefix}manifest.json"},
                ]
            },
        )

    def test_is_approved_deliverable(self):
        self.assertTrue(s3_sync._is_approved_deliverable("redacted/foo.pdf"))
        self.assertTrue(s3_sync._is_approved_deliverable("images/1-001.png"))
        self.assertTrue(
            s3_sync._is_approved_deliverable("tc.1.1.2.original.pdf")
        )
        self.assertTrue(
            s3_sync._is_approved_deliverable("tc.1.1.2.redacted.pdf")
        )
        self.assertFalse(
            s3_sync._is_approved_deliverable("unredacted/foo.pdf")
        )
        self.assertFalse(s3_sync._is_approved_deliverable("bitonal.pdf"))
        self.assertFalse(s3_sync._is_approved_deliverable("detections.json"))
        # Guard against nested paths under one of the approved subdirs.
        self.assertFalse(
            s3_sync._is_approved_deliverable("redacted/sub/x.pdf")
        )

    def test_copy_processing_to_approved_copies_only_deliverables(self):
        scan = _reporter_scan()
        src_prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{src_prefix}bitonal.pdf"},
                    {"Key": f"{src_prefix}detections.json"},
                    {"Key": f"{src_prefix}tc.164.1.2.original.pdf"},
                    {"Key": f"{src_prefix}tc.164.1.2.redacted.pdf"},
                    {"Key": f"{src_prefix}redacted/a.pdf"},
                    {"Key": f"{src_prefix}unredacted/a.pdf"},
                    {"Key": f"{src_prefix}images/1-001.png"},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            prefix, count = s3_sync.copy_processing_to_approved(scan)

        expected_prefix = "approved/tc/164/1/"
        self.assertEqual(prefix, expected_prefix)
        self.assertEqual(count, 4)
        copied = {c.kwargs["Key"] for c in mock_s3.copy_object.call_args_list}
        self.assertEqual(
            copied,
            {
                f"{expected_prefix}tc.164.1.2.original.pdf",
                f"{expected_prefix}tc.164.1.2.redacted.pdf",
                f"{expected_prefix}redacted/a.pdf",
                f"{expected_prefix}images/1-001.png",
            },
        )

    def test_download_processing_files_skips_same_size(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        tmp_root = s3_sync.tmp_output_dir(scan)
        tmp_root.mkdir(parents=True, exist_ok=True)
        existing = tmp_root / "detections.json"
        existing.write_text("abc")

        prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}detections.json", "Size": 3},
                    {"Key": f"{prefix}bitonal.pdf", "Size": 5},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        mock_s3.download_file.side_effect = _fake_download

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = s3_sync.download_processing_files(scan)

        self.assertEqual(result, tmp_root)
        # Only the new file (bitonal.pdf) should be downloaded.
        self.assertEqual(mock_s3.download_file.call_count, 1)
        self.assertEqual(
            mock_s3.download_file.call_args.args[1],
            f"{prefix}bitonal.pdf",
        )

    def test_download_processing_file_pulls_only_that_key(self):
        """Serving one generated file must not drag the whole prefix.

        A full volume's prefix runs to gigabytes (the original, every
        opinion, every LLM page), and under ASGI one long sync pull stalls
        every other request in the process.
        """
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])

        prefix = s3_sync.s3_processing_prefix(scan)
        wanted = "redacted/a.164.0001-0027.pdf"
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}{wanted}", "Size": 5},
                    {
                        "Key": f"{prefix}redacted/a.164.0028-0031.pdf",
                        "Size": 5,
                    },
                    {"Key": f"{prefix}a.164.1.31.original.pdf", "Size": 9999},
                    {"Key": f"{prefix}llm/page_0001.pdf", "Size": 5},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        mock_s3.download_file.side_effect = _fake_download

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = s3_sync.download_processing_file(scan, wanted)

        self.assertEqual(mock_s3.download_file.call_count, 1)
        self.assertEqual(
            mock_s3.download_file.call_args.args[1], f"{prefix}{wanted}"
        )
        # The deliverables live in subdirectories, and nothing has created
        # them on a machine that only ever served this one file, so the
        # pull has to. Without it the download fails on its own temp file
        # and the request 404s with the object sitting in S3.
        self.assertTrue((pathlib.Path(result) / wanted).is_file())

    def test_download_preview_pdf_skips_original_and_images(self):
        """Only the bitonal/OCR preview is pulled, not original or images."""
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])

        prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}bitonal.pdf", "Size": 5},
                    {"Key": f"{prefix}a.164.1.pdf", "Size": 5},  # OCR pdf
                    {"Key": f"{prefix}a.164.1.original.pdf", "Size": 5},
                    {"Key": f"{prefix}stamped.pdf", "Size": 5},
                    {"Key": f"{prefix}images/p1.png", "Size": 5},
                    {"Key": f"{prefix}redacted/op1.pdf", "Size": 5},
                    {"Key": f"{prefix}detections.json", "Size": 5},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        mock_s3.download_file.side_effect = _fake_download

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            s3_sync.download_preview_pdf(scan)

        pulled = {
            call.args[1] for call in mock_s3.download_file.call_args_list
        }
        self.assertEqual(
            pulled,
            {f"{prefix}bitonal.pdf", f"{prefix}a.164.1.pdf"},
        )

    def test_delete_uploaded_object(self):
        mock_s3 = MagicMock()
        key = "processing/9/tc/164/1/x.original.pdf"
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = s3_sync.delete_uploaded_object(key)
        self.assertTrue(result)
        mock_s3.delete_object.assert_called_once()
        self.assertEqual(mock_s3.delete_object.call_args.kwargs["Key"], key)

    def test_delete_uploaded_object_empty_key_noop(self):
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.delete_uploaded_object("")
        self.assertFalse(result)
        mock_boto3.client.assert_not_called()

    def test_upload_file_to_s3_missing_local_file(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.upload_file_to_s3(scan, "missing.json")
        self.assertFalse(result)
        mock_boto3.client.assert_not_called()

    def test_upload_file_to_s3_happy_path(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        # output_dir now resolves to /tmp/...
        local_dir = pathlib.Path(scan.output_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "detections.json").write_text("[]")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            ok = s3_sync.upload_file_to_s3(scan, "detections.json")

        self.assertTrue(ok)
        mock_s3.upload_file.assert_called_once()
        _, bucket, key = mock_s3.upload_file.call_args.args
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"processing/{scan.pk}/tc/164/1/detections.json")

    def test_upload_fileobj_to_s3_streams_small_file(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        upload = SimpleUploadedFile(
            "x.original.pdf",
            b"%PDF-1.4 body",
            content_type="application/pdf",
        )

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            ok = s3_sync.upload_fileobj_to_s3(scan, upload, "x.original.pdf")

        self.assertTrue(ok)
        # Small in-memory upload has no temp path, so it streams the
        # file object directly. No local file is touched.
        mock_s3.upload_fileobj.assert_called_once()
        mock_s3.upload_file.assert_not_called()
        _, bucket, key = mock_s3.upload_fileobj.call_args.args
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"processing/{scan.pk}/tc/164/1/x.original.pdf")
        self.assertEqual(
            mock_s3.upload_fileobj.call_args.kwargs["ExtraArgs"],
            {"ContentType": "application/pdf"},
        )

    def test_upload_fileobj_to_s3_uses_temporary_file_path(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        # Large uploads spool to a TemporaryUploadedFile exposing
        # temporary_file_path(); boto3 reads that path directly.
        upload = MagicMock()
        upload.temporary_file_path.return_value = "/tmp/spool.pdf"
        upload.size = 2048

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            ok = s3_sync.upload_fileobj_to_s3(scan, upload, "x.original.pdf")

        self.assertTrue(ok)
        mock_s3.upload_file.assert_called_once()
        mock_s3.upload_fileobj.assert_not_called()
        path_arg, bucket, key = mock_s3.upload_file.call_args.args
        self.assertEqual(path_arg, "/tmp/spool.pdf")
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"processing/{scan.pk}/tc/164/1/x.original.pdf")
        self.assertEqual(
            mock_s3.upload_file.call_args.kwargs["ExtraArgs"],
            {"ContentType": "application/pdf"},
        )


class TestS3EnabledBranches(SimpleTestCase):
    """Coverage for the ``_s3_enabled()`` branch matrix.

    ``TESTING`` is auto-set during the test run, so each case overrides
    it explicitly to exercise the intended branch. ``has_s3_credentials``
    is patched to keep the result independent of the developer's
    environment.
    """

    @override_settings(DEVELOPMENT=True, RUNPOD_ENABLED=True, TESTING=False)
    def test_dev_with_runpod_enabled_returns_true(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertTrue(s3_sync._s3_enabled())

    @override_settings(
        DEVELOPMENT=True,
        RUNPOD_ENABLED=False,
        DOCTOR_ENABLED=False,
        TESTING=False,
    )
    def test_dev_without_a_provider_returns_false(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertFalse(s3_sync._s3_enabled())

    @override_settings(DEVELOPMENT=False, RUNPOD_ENABLED=False, TESTING=True)
    def test_testing_short_circuits_regardless_of_runpod(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertFalse(s3_sync._s3_enabled())
        with self.settings(RUNPOD_ENABLED=True):
            with patch(
                "scanning.s3_sync.has_s3_credentials", return_value=True
            ):
                self.assertFalse(s3_sync._s3_enabled())


class TestS3ClientRegion(SimpleTestCase):
    """The shared client signs for the bucket's region, not the ambient one.

    ``generate_presigned_post`` signs with SigV4, which folds the region
    into the credential scope, so a client left on boto3's ``us-east-1``
    default hands the browser an upload policy S3 rejects.
    """

    @override_settings(AWS_S3_REGION_NAME="us-west-2", TESTING=True)
    def test_client_is_built_with_the_configured_region(self):
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            s3_sync._s3_client()
        mock_boto3.client.assert_called_once_with(
            "s3", region_name="us-west-2"
        )

    @override_settings(AWS_S3_REGION_NAME="us-west-2", TESTING=False)
    def test_cached_client_is_built_with_the_configured_region(self):
        s3_sync._cached_s3_client.cache_clear()
        self.addCleanup(s3_sync._cached_s3_client.cache_clear)
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            s3_sync._s3_client()
        mock_boto3.client.assert_called_once_with(
            "s3", region_name="us-west-2"
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestJobAttemptKeys(TestCase):
    """Keys for one attempt of one job (issue #176)."""

    def _job(self, **kwargs):
        from scanning.factories import ExternalJobFactory
        from scanning.models import JobEngine, JobProvider, JobStage

        defaults = {
            "scan": _reporter_scan(),
            "stage": JobStage.CONVERT,
            "engine": JobEngine.BITONAL,
            "provider": JobProvider.DOCTOR,
        }
        defaults.update(kwargs)
        return ExternalJobFactory(**defaults)

    def test_the_key_names_run_shard_and_attempt(self):
        job = self._job(run=2, shard_index=3, shard_count=4, attempt=5)

        key = s3_sync.s3_job_attempt_key(job, suffix=".pdf")

        self.assertEqual(
            key,
            f"processing/{job.scan.pk}/tc/164/1/jobs/convert/bitonal/"
            "r2-s3-a5.pdf",
        )

    def test_the_suffix_defaults_to_json(self):
        job = self._job()
        self.assertTrue(s3_sync.s3_job_attempt_key(job).endswith(".json"))

    def test_two_attempts_of_one_shard_never_share_a_key(self):
        """Or an abandoned worker's late upload is read as the new one.

        Doctor finishes a conversion even after we stop listening, so
        this is the normal case for it, not a rare race.
        """
        job = self._job(attempt=1)
        first = s3_sync.s3_job_attempt_key(job, suffix=".pdf")
        job.attempt = 2
        second = s3_sync.s3_job_attempt_key(job, suffix=".pdf")

        self.assertNotEqual(first, second)

    def test_shards_and_runs_are_distinct_too(self):
        job = self._job(run=1, shard_index=0, shard_count=2)
        base = s3_sync.s3_job_attempt_key(job, suffix=".pdf")
        job.shard_index = 1
        by_shard = s3_sync.s3_job_attempt_key(job, suffix=".pdf")
        job.shard_index = 0
        job.run = 2
        by_run = s3_sync.s3_job_attempt_key(job, suffix=".pdf")

        self.assertEqual(len({base, by_shard, by_run}), 3)

    def test_an_opinion_job_is_namespaced_by_its_opinion(self):
        from scanning.factories import OpinionScanFactory
        from scanning.models import JobEngine, JobStage

        scan = _reporter_scan()
        opinion = OpinionScanFactory(scan=scan)
        job = self._job(
            scan=scan,
            opinion=opinion,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
        )

        self.assertIn(f"/op{opinion.pk}/", s3_sync.s3_job_attempt_key(job))

    def test_the_key_lives_under_the_unsynced_jobs_subtree(self):
        """Results are wire artifacts; the generic sync must skip them."""
        job = self._job()

        key = s3_sync.s3_job_attempt_key(job, suffix=".pdf")
        relative = key.split(f"/{job.scan.start_page}/", 1)[1]

        self.assertTrue(relative.startswith(s3_sync.JOB_RESULTS_SUBDIR))
        self.assertFalse(s3_sync._is_synced_by_default(relative))


class TestPresignHelpers(SimpleTestCase):
    """Presigning for an external worker."""

    @override_settings(
        AWS_S3_REGION_NAME="us-west-2",
        AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
        TESTING=True,
    )
    def test_the_signing_client_pins_sigv4_and_the_region(self):
        """Both are load-bearing for a PUT that sends a Content-Type.

        SigV2 folds the header into the string to sign whether or not we
        signed it, and SigV4 puts the region in the credential scope, so
        either default being wrong is a 403 at upload time.
        """
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            s3_sync._signing_s3_client()

        kwargs = mock_boto3.client.call_args.kwargs
        self.assertEqual(kwargs["region_name"], "us-west-2")
        self.assertEqual(kwargs["config"].signature_version, "s3v4")

    @override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket", TESTING=True)
    def test_presign_get_is_scoped_to_one_object(self):
        client = MagicMock()
        with patch("scanning.s3_sync._signing_s3_client", return_value=client):
            s3_sync.presign_get("shards/0001.pdf", 3600)

        client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "bucket", "Key": "shards/0001.pdf"},
            ExpiresIn=3600,
        )

    @override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket", TESTING=True)
    def test_presign_put_signs_the_content_type(self):
        """Doctor sends application/pdf; an unsigned PUT gets a 403."""
        client = MagicMock()
        with patch("scanning.s3_sync._signing_s3_client", return_value=client):
            s3_sync.presign_put("jobs/r1-s0-a1.pdf", "application/pdf", 60)

        client.generate_presigned_url.assert_called_once_with(
            "put_object",
            Params={
                "Bucket": "bucket",
                "Key": "jobs/r1-s0-a1.pdf",
                "ContentType": "application/pdf",
            },
            ExpiresIn=60,
        )


@override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket", TESTING=True)
class TestObjectExists(SimpleTestCase):
    """The completion test for a provider that reports nothing back."""

    def _head(self, side_effect=None):
        client = MagicMock()
        if side_effect is not None:
            client.head_object.side_effect = side_effect
        return client

    def test_a_present_object_is_true(self):
        client = self._head()
        with patch("scanning.s3_sync._s3_client", return_value=client):
            self.assertTrue(s3_sync.object_exists("some/key.pdf"))

    def test_a_missing_object_is_false(self):
        for code in ("404", "NoSuchKey", "NotFound"):
            with self.subTest(code=code):
                client = self._head(
                    ClientError({"Error": {"Code": code}}, "HeadObject")
                )
                with patch("scanning.s3_sync._s3_client", return_value=client):
                    self.assertFalse(s3_sync.object_exists("some/key.pdf"))

    def test_an_access_denied_is_raised_not_reported_as_absent(self):
        """Otherwise a bad IAM policy looks like a worker producing nothing.

        S3 answers 403 for a missing key when the caller lacks
        ListBucket, so swallowing it would make every job silently time
        out instead of surfacing the misconfiguration.
        """
        client = self._head(
            ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
        )
        with patch("scanning.s3_sync._s3_client", return_value=client):
            with self.assertRaises(ClientError):
                s3_sync.object_exists("some/key.pdf")


@override_settings(
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
    TESTING=False,
    DEVELOPMENT=False,
)
class TestDeleteObjects(SimpleTestCase):
    """Sweeping consumed job results."""

    def setUp(self):
        s3_sync._cached_s3_client.cache_clear()
        self.addCleanup(s3_sync._cached_s3_client.cache_clear)

    def test_keys_are_deleted_in_batches_of_a_thousand(self):
        keys = [f"key-{i}" for i in range(1500)]
        client = MagicMock()
        client.delete_objects.side_effect = [
            {"Deleted": [{"Key": k} for k in keys[:1000]]},
            {"Deleted": [{"Key": k} for k in keys[1000:]]},
        ]
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            with patch("scanning.s3_sync._s3_client", return_value=client):
                deleted = s3_sync.delete_objects(keys)

        self.assertEqual(deleted, 1500)
        self.assertEqual(client.delete_objects.call_count, 2)

    def test_a_failure_is_logged_not_raised(self):
        """An orphan costs storage; a raise would fail a done pipeline."""
        client = MagicMock()
        client.delete_objects.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "DeleteObjects"
        )
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            with patch("scanning.s3_sync._s3_client", return_value=client):
                with self.assertLogs("scanning.s3_sync", level="WARNING"):
                    self.assertEqual(s3_sync.delete_objects(["k"]), 0)

    def test_nothing_happens_without_keys_or_s3(self):
        with patch("scanning.s3_sync._s3_client") as client:
            self.assertEqual(s3_sync.delete_objects([]), 0)
        client.assert_not_called()


@override_settings(TESTING=False)
class TestS3EnabledWithDoctor(SimpleTestCase):
    """A doctor-only dev environment still needs the sync (issue #176).

    Doctor reads its input through a presigned GET, so without the sync
    the shards never reach the bucket it fetches them from.
    """

    @override_settings(
        DEVELOPMENT=True, RUNPOD_ENABLED=False, DOCTOR_ENABLED=True
    )
    def test_dev_with_doctor_enabled_returns_true(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertTrue(s3_sync._s3_enabled())

    @override_settings(
        DEVELOPMENT=True, RUNPOD_ENABLED=False, DOCTOR_ENABLED=False
    )
    def test_dev_with_neither_provider_returns_false(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertFalse(s3_sync._s3_enabled())
