# The two Surya fields of a corrected volume (#368).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanning", "0038_opinion_pdf_stamp"),
    ]

    operations = [
        migrations.AddField(
            model_name="applyrun",
            name="surya_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "S3 key of the final Surya OCR volume JSON (#368). The "
                    "twin of extract_key: blank until glued, which waits "
                    "for a Surya volume run, and a volume nobody read with "
                    "Surya never has one. No review state reads it."
                ),
                max_length=1024,
            ),
        ),
        migrations.AddField(
            model_name="applyrun",
            name="surya_run",
            field=models.PositiveIntegerField(
                blank=True,
                help_text=(
                    "The Surya volume run surya_key was glued from. A "
                    "second read of the volume gets a later run number, "
                    "and that is what makes the glue due again."
                ),
                null=True,
            ),
        ),
    ]
