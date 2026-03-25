"""Integration tests for the YOLO detect + pair pipeline.

Requires the test fixture PDF to be present at:
  scanning/tests/fixtures/a3d.332.1.1.pdf

Copy it in with:
  cp /path/to/a3d.332.1.1.pdf scanning/tests/fixtures/
"""

import json
import pathlib
import tempfile

import django.test

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
PDF_PATH = FIXTURE_DIR / "a3d.332.1.1.pdf"


class TestDetectionPipeline(django.test.SimpleTestCase):
    """Detect and pair on a known one-page A.3d vol. 332 PDF.

    Expected: 3 CASE_CAPTIONs, 3 KEY_ICONs, 3 paired opinions.
    """

    def setUp(self):
        if not PDF_PATH.exists():
            self.skipTest(f"Test PDF not found at {PDF_PATH}. Copy it into scanning/tests/fixtures/")

    def test_detect_finds_three_case_captions(self):
        from blackletter.api import detect

        with tempfile.TemporaryDirectory() as tmpdir:
            dets = detect(str(PDF_PATH), tmpdir, models=["small", "medium", "large"])
            captions = [d for d in dets if d.get("label") == "CASE_CAPTION"]
            self.assertEqual(
                len(captions), 3,
                f"Expected 3 CASE_CAPTIONs, got {len(captions)}. "
                f"All labels: {[d['label'] for d in dets]}"
            )

    def test_detect_finds_three_key_icons(self):
        from blackletter.api import detect

        with tempfile.TemporaryDirectory() as tmpdir:
            dets = detect(str(PDF_PATH), tmpdir, models=["small", "medium", "large"])
            keys = [d for d in dets if d.get("label") == "KEY_ICON"]
            self.assertEqual(
                len(keys), 3,
                f"Expected 3 KEY_ICONs, got {len(keys)}."
            )

    def test_detect_and_pair_finds_three_opinions(self):
        from blackletter.api import detect, pair

        with tempfile.TemporaryDirectory() as tmpdir:
            dets = detect(str(PDF_PATH), tmpdir, models=["small", "medium", "large"])
            det_path = pathlib.Path(tmpdir) / "detections.json"
            det_path.write_text(json.dumps(dets))
            opinions = pair(
                str(det_path), str(PDF_PATH),
                reporter="a3d", volume="332", first_page=1,
            )
            self.assertEqual(
                len(opinions), 3,
                f"Expected 3 opinions, got {len(opinions)}."
            )
