"""Drop the two models PageEdit replaced (#214).

0015 copied every row into ``PageEdit``. These two tables held one
human decision each, in two different address spaces, and neither is
read anywhere now.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('scanning', '0015_page_edits_from_inserts_and_deletions'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='pageinsert',
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name='pageinsert',
            name='scan',
        ),
        migrations.DeleteModel(
            name='PageDeletion',
        ),
        migrations.DeleteModel(
            name='PageInsert',
        ),
    ]
