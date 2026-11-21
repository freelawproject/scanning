"""An index for the hand-drawn detections of a scan.

``detections.changed_since_compute`` asks on every render of step 2
whether a manual detection was created or withdrawn since the last
compute, and nothing indexed that question: the planner walked every
detection of the volume off disk to find the few manual rows. On a
large volume under a saturated database that was a minute per page
load. Built concurrently, because the table is written by the compute
while the build runs.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("scanning", "0063_externaljob_files_left_index"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="detection",
            index=models.Index(
                condition=models.Q(("model_name", "manual")),
                fields=["scan", "date_created", "withdrawn_at"],
                name="idx_det_manual_by_scan",
            ),
        ),
    ]
