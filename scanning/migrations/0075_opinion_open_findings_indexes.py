from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    """The two indexes of the opinions list sorted by warnings.

    Built concurrently: the passes write the opinion table, and a plain
    CREATE INDEX holds those writes for the build.
    """

    atomic = False

    dependencies = [
        ("scanning", "0074_opinion_open_findings"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                fields=["open_findings", "id"],
                name="idx_opinion_open_findings",
            ),
        ),
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                fields=["status", "open_findings", "id"],
                name="idx_opinion_status_open",
            ),
        ),
    ]
