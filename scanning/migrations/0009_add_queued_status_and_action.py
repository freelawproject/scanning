from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0008_remove_scan_unique_reporter_volume_source_start"),
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
                    ("pending_review", "Pending Review"),
                    ("approved", "Approved"),
                    ("extracted", "Extracted"),
                    ("error", "Error"),
                    ("cancelled", "Cancelled"),
                ],
                default="uploaded",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="scan",
            name="queued_action",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Action for daemon to run: full_pipeline, detect, reprocess, generate_files, validate.",
                max_length=30,
            ),
        ),
    ]
