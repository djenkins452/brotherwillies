# Generated 2026-08-24 — split from auto-generated migration so
# duplicate reconciliation (0008) runs BEFORE the unique constraints
# are enforced (0009). Adding both in one step fails on any DB that
# already has duplicate Golfer rows.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('golf', '0006_golfoddssnapshot_is_derived'),
    ]

    operations = [
        migrations.AddField(
            model_name='golfer',
            name='name_normalized',
            field=models.CharField(blank=True, db_index=True,
                                   default='', max_length=100),
        ),
    ]
