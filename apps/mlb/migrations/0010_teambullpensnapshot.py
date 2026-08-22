"""v3.3 SHADOW — TeamBullpenSnapshot table (2026-08-22).

Append-only per-team bullpen state timeline. Populated (going forward
only) by `ingest_bullpen_snapshots`. Consumed by
`apps.mlb.services.bullpen.team_bullpen_signal` with a strict
`as_of__lt=game.first_pitch` leakage guard.

Shadow only — no production behavior change. `USE_BULLPEN_QUALITY` and
`USE_BULLPEN_FATIGUE` both default False; the shadow value is stored on
`BettingRecommendation.feature_contributions` for research/audit but
never enters the score until both a real data source is ingested and
production activation is explicitly authorized.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mlb', '0009_team_elo_last_updated_team_elo_rating'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamBullpenSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('as_of', models.DateTimeField(db_index=True)),
                ('bullpen_era', models.FloatField(blank=True, null=True)),
                ('bullpen_whip', models.FloatField(blank=True, null=True)),
                ('bullpen_k_per_9', models.FloatField(blank=True, null=True)),
                ('bullpen_bb_per_9', models.FloatField(blank=True, null=True)),
                ('bullpen_ip_last30', models.FloatField(blank=True, null=True)),
                ('appearances_last_1_day', models.IntegerField(blank=True, null=True)),
                ('appearances_last_2_days', models.IntegerField(blank=True, null=True)),
                ('appearances_last_3_days', models.IntegerField(blank=True, null=True)),
                ('high_leverage_rest_days_min', models.IntegerField(blank=True, null=True)),
                ('top_reliever_available', models.BooleanField(blank=True, null=True)),
                ('source', models.CharField(blank=True, choices=[('mlb_stats_api', 'MLB Stats API'), ('odds_api', 'Odds API'), ('manual', 'Manual')], default='', max_length=30)),
                ('data_confidence', models.CharField(choices=[('low', 'Low'), ('med', 'Medium'), ('high', 'High')], default='low', max_length=6)),
                ('notes', models.CharField(blank=True, default='', max_length=200)),
                ('team', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='bullpen_snapshots', to='mlb.team')),
            ],
            options={
                'ordering': ['-as_of'],
                'indexes': [
                    models.Index(fields=['team', '-as_of'], name='mlb_teambul_team_id_208099_idx'),
                ],
            },
        ),
    ]
