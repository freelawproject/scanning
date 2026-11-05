from django.db import migrations, models


class Migration(migrations.Migration):
    """The weights that wrote an opinion's tagger spans (#573)."""

    dependencies = [
        ("scanning", "0067_job_volume_runs_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="opinion",
            name="tag_model",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "The weights that wrote the spans at ``tag_key``, as "
                    "the worker reports them (``repo@revision``, #573). The "
                    "spans are of the current weights when this is "
                    "``tagger.MODEL`` (``tagger.is_current``). Blank for "
                    "spans glued before the field: the per-token weights."
                ),
                max_length=255,
            ),
        ),
    ]
