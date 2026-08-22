# Adds Status.AWAITING (issue #176): the scan's shards are with an
# external provider (doctor, for bitonal conversion) and nothing of
# ours is running. Distinct from PROCESSING because only PROCESSING may
# be swept as stale and re-queued; sweeping a scan that is merely
# waiting would charge it an interruption and redo work already paid
# for. Choices-only change; no schema alteration.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0009_alter_scan_status_awaiting_validation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scan",
            name="status",
            field=models.CharField(
                choices=[
                    ("uploaded", "Uploaded"),
                    ("queued", "Queued"),
                    ("processing", "Processing"),
                    ("awaiting", "Waiting on external jobs"),
                    ("awaiting_validation", "Awaiting Validation"),
                    ("pending_review", "Pending Review"),
                    ("approved", "Approved"),
                    ("extracted", "Extracted"),
                    ("error", "Error"),
                    ("error_max_retries", "Error (retry cap hit)"),
                    ("error_interrupted", "Error (interrupted too often)"),
                    ("cancelled", "Cancelled"),
                ],
                default="uploaded",
                max_length=20,
            ),
        ),
    ]
