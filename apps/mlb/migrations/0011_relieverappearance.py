"""v3.3 SHADOW — RelieverAppearance table (2026-08-22).

Append-only raw data source for the deterministic bullpen builder.
One row per pitcher per game, produced from
MLB Stats API /api/v1/game/{gamePk}/boxscore. Feeds
`apps.mlb.services.bullpen_builder`.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mlb', '0010_teambullpensnapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='RelieverAppearance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_starter', models.BooleanField(db_index=True)),
                ('outs_recorded', models.IntegerField(default=0, help_text='Total outs. 3.1 IP = 10 outs.')),
                ('pitches', models.IntegerField(blank=True, null=True)),
                ('hits', models.IntegerField(default=0)),
                ('earned_runs', models.IntegerField(default=0)),
                ('walks', models.IntegerField(default=0)),
                ('strikeouts', models.IntegerField(default=0)),
                ('home_runs', models.IntegerField(default=0)),
                ('is_save', models.BooleanField(default=False)),
                ('is_hold', models.BooleanField(default=False)),
                ('ingested_at', models.DateTimeField(auto_now_add=True)),
                ('game', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='pitcher_appearances', to='mlb.game')),
                ('pitcher', models.ForeignKey(help_text='StartingPitcher row is reused for relievers too; the name is legacy from v3.0. is_starter distinguishes.', on_delete=models.deletion.CASCADE, related_name='appearances', to='mlb.startingpitcher')),
                ('team', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='pitcher_appearances', to='mlb.team')),
            ],
            options={
                'ordering': ['-game__first_pitch'],
                'constraints': [
                    models.UniqueConstraint(fields=['game', 'pitcher'], name='mlb_reliever_appearance_game_pitcher_unique'),
                ],
                'indexes': [
                    models.Index(fields=['team', '-game'], name='mlb_relieve_team_id_ea6b41_idx'),
                    models.Index(fields=['pitcher', '-game'], name='mlb_relieve_pitcher_677418_idx'),
                    models.Index(fields=['team', 'is_starter'], name='mlb_relieve_team_id_f30f04_idx'),
                ],
            },
        ),
    ]
