# The ledger of the per-opinion redacted PDF (#336, part 3).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanning", "0035_create_opinions_action"),
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
                    "(#336). Reset when the revision rises and when the PDF "
                    "is written."
                ),
            ),
        ),
    ]
