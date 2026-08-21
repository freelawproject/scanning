# Adds the interim Status.AWAITING_VALIDATION parking state (issue
# #173): the upload-side pipeline finished but page-number validation
# is disabled until the dots.mocr adapter (#149) replaces the retired
# PaddleOCR path. Choices-only change; no schema alteration.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0008_externaljob"),
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
