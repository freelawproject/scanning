# The ledger of the per-opinion redacted PDF (#336, part 3).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanning", "0037_opinion_ocr_glue_ledger"),
    ]

    operations = [
        migrations.AddField(
            model_name="opinion",
            name="redacted_pdf_revision",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text=(
                    "The glue_revision the redacted PDF was written at "
                    "(#336). Equal to glue_revision: the PDF exists at the "
                    "live revision. Null: no PDF was ever written. A stamp "
                    "and not a key; the key is derived from the pk and the "
                    "revision."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="opinion",
            name="pdf_attempts",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    "Failed ticks of the PDF pass at the live revision "
                    "(#336) that the rows explain; a transient fault counts "
                    "none. Reset when the revision rises and when the PDF "
                    "is written."
                ),
            ),
        ),
        migrations.AddField(
            model_name="opinion",
            name="pdf_attempted_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When the PDF pass last failed on this row, of either "
                    "kind (#336). The row is not due again before "
                    "opinion_pdf.retry_after() has passed. Cleared when "
                    "the PDF is written."
                ),
                null=True,
            ),
        ),
    ]
