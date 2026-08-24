"""2026-08-24 — enforce Golfer uniqueness AFTER the reconciliation
migration (0008) has merged historical duplicates.

Two partial unique constraints:
  * external_id unique when non-blank
  * name_normalized unique when non-blank

Both use `condition=~Q(field='')` so pre-migration rows that lack
these fields don't block the constraint. Post-reconciliation, no
row should have duplicate values for either key.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('golf', '0008_reconcile_duplicate_golfers'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='golfer',
            constraint=models.UniqueConstraint(
                condition=models.Q(('external_id', ''), _negated=True),
                fields=('external_id',),
                name='golf_golfer_external_id_unique',
            ),
        ),
        migrations.AddConstraint(
            model_name='golfer',
            constraint=models.UniqueConstraint(
                condition=models.Q(('name_normalized', ''), _negated=True),
                fields=('name_normalized',),
                name='golf_golfer_name_normalized_unique',
            ),
        ),
    ]
