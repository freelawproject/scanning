# The ledger of the per-opinion OCR glue (#350).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0035_create_opinions_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="opinion",
            name="ocr_glue_revision",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text=(
                    "The glue_revision the OCR documents were written at "
                    "(#350). Null: no OCR glue. Equal to glue_revision: the "
                    "glue exists, the one rule opinion_ocr.is_written reads."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="opinion",
            name="ocr_glue_attempts",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    "Failed OCR glue ticks on this row at the live revision "
                    "(#350). At opinion_ocr.MAX_ATTEMPTS the row goes to ERROR."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="opinion",
            name="glue_revision",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    "The revision of the per-opinion glues. Every glue key is "
                    "derived from the key and this number; a re-glue raises it."
                ),
            ),
        ),
    ]
