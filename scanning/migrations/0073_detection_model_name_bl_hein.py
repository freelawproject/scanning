from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0072_opinion_card_actions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="detection",
            name="model_name",
            field=models.CharField(
                blank=True,
                choices=[
                    ("small", "Small"),
                    ("medium", "Medium"),
                    ("large", "Large"),
                    ("bl_warm", "bl-warm"),
                    ("bl_hein", "bl-hein"),
                    ("manual", "Manual"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
