from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    """The index of the volume runs per engine and scan (#588).

    Built concurrently: the job table is written by every tick, and a
    plain CREATE INDEX holds its writes for the build.
    """

    atomic = False

    dependencies = [
        ("scanning", "0066_job_poll_indexes"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="externaljob",
            index=models.Index(
                condition=models.Q(("apply_run__isnull", True)),
                fields=["stage", "engine", "provider", "scan", "run"],
                include=("status",),
                name="idx_job_volume_runs",
            ),
        ),
    ]
