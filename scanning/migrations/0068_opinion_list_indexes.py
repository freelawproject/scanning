from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    """The two indexes of the opinions list (#599).

    Built concurrently: the computes write the detection table and the
    passes the opinion table, and a plain CREATE INDEX holds those
    writes for the build.
    """

    atomic = False

    dependencies = [
        ("scanning", "0067_job_volume_runs_index"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="detection",
            index=models.Index(
                condition=models.Q(("active", True)),
                fields=["apply_run", "page_index", "label"],
                name="idx_det_live_run_page",
            ),
        ),
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                fields=["status", "id"], name="idx_opinion_status_id"
            ),
        ),
    ]
