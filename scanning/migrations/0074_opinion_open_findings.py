from django.db import migrations, models


class Migration(migrations.Migration):
    """The stored count of an opinion's open findings.

    The column keeps its default in the database (``db_default``), so a
    pod of the release before, which names no such column, still
    inserts its opinion rows while the deploy rolls out.
    """

    dependencies = [
        ("scanning", "0073_detection_model_name_bl_hein"),
    ]

    operations = [
        migrations.AddField(
            model_name="opinion",
            name="open_findings",
            field=models.PositiveIntegerField(
                db_default=0,
                default=0,
                help_text=(
                    "How many findings of this opinion carry no dismissal: "
                    "the number of the list's Warnings column, stored so "
                    "the list sorts by it off an index. A cache of the "
                    "finding rows, written by opinion_findings.stamp_open "
                    "after every write of one; the rows are the truth."
                ),
            ),
        ),
    ]
