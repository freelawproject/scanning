"""Tests for the guide of the review 2 viewer (issue #299).

Two controls had no text anywhere. A double click on a detection box
opened a button that said "Delete", and the endpoint behind it deletes
nothing: it writes a decision, or it withdraws a hand-drawn row. The key
"r" moved a cycle of four overlay modes, and the page showed no cue of
the mode, because the label the script wrote went to an element no
template held.

These tests pin the three new elements, the step that carries them, and
the four rows that are the one table of the modes.
"""

from django.urls import reverse

from scanning.factories import ScanFactory
from scanning.models import Status
from scanning.tests.test_views import ScanningTestCase

MODES = ("off", "bounds", "transparent", "solid")


class TestViewerHelp(ScanningTestCase):
    """The overlay cue and the guide of step 2."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_REDACTION_REVIEW
        )

    def get(self, step):
        """Read the process page at one step.

        :param step: The step number.
        :returns: The response.
        """
        url = reverse("scan_process", kwargs={"pk": self.scan.pk})
        return self.client.get(f"{url}?step={step}")

    def test_step_2_draws_the_button_and_the_panel(self):
        response = self.get(2)

        self.assertContains(response, 'id="toggle-overlays-btn"')
        self.assertContains(response, 'id="viewer-help-btn"')
        self.assertContains(response, 'id="viewer-help-panel"')

    def test_step_1_draws_none_of_them(self):
        scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        url = reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"

        response = self.client.get(url)

        self.assertNotContains(response, 'id="toggle-overlays-btn"')
        self.assertNotContains(response, 'id="viewer-help-btn"')
        self.assertNotContains(response, 'id="viewer-help-panel"')

    def test_the_panel_holds_one_row_for_each_mode(self):
        response = self.get(2)

        for mode in MODES:
            self.assertContains(response, f'data-overlay-mode="{mode}"')

    def test_every_mode_row_carries_the_button_label(self):
        # The row is the one table (#299): the button reads data-label off
        # it, so a row with no label leaves the cue empty.
        content = self.get(2).content.decode()

        for mode in MODES:
            start = content.index(f'data-overlay-mode="{mode}"')
            self.assertIn("data-label=", content[start : start + 120])

    def test_the_detections_part_waits_for_the_boxes(self):
        content = self.get(2).content.decode()

        start = content.index('id="viewer-help-detections"')

        self.assertIn("hidden", content[start : start + 60])

    def test_the_button_starts_in_the_off_mode(self):
        content = self.get(2).content.decode()

        start = content.index('id="toggle-overlays-btn"')

        self.assertIn('data-mode="off"', content[start : start + 200])
