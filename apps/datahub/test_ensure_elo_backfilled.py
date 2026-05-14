"""Tests for the Phase 2A Task 1 production-safe Elo backfill hook.

Coverage:
  1. No-op when state looks already-backfilled.
  2. Runs rebuild when state is empty.
  3. Runs rebuild when --force is passed even if already-backfilled.
  4. Detection guard: teams with elo_rating but no history → not
     considered backfilled (catches the "single test team" false positive).
  5. Failure inside rebuild_elo_ratings does not propagate (deploy-safe).
"""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import TeamEloHistory


def _seed_thirty_mlb_teams_with_final_games():
    """Create the minimum data shape ensure_elo_backfilled inspects.

    Returns a list of created Team rows. Each pair of teams plays one
    final game so rebuild_elo_ratings has something to process.
    """
    from apps.mlb.models import Conference, Game, Team
    league = Conference.objects.create(
        name='AL', slug=f'al-{timezone.now().timestamp()}',
    )
    teams = []
    for i in range(30):
        teams.append(Team.objects.create(
            name=f'T{i}',
            slug=f't-{timezone.now().timestamp()}-{i}',
            conference=league,
        ))
    # Pair them up into 15 final games — gives rebuild work to do.
    now = timezone.now()
    for idx in range(0, len(teams), 2):
        Game.objects.create(
            home_team=teams[idx],
            away_team=teams[idx + 1],
            first_pitch=now - timedelta(days=10 - idx // 2),
            status='final',
            home_score=5,
            away_score=3,
        )
    return teams


class IdempotentDetectionTests(TestCase):
    def test_empty_state_triggers_rebuild(self):
        _seed_thirty_mlb_teams_with_final_games()
        out = StringIO()
        call_command('ensure_elo_backfilled', stdout=out)
        body = out.getvalue()
        self.assertIn('not yet backfilled', body)
        self.assertIn('rebuild', body.lower())
        # After the run, history rows exist.
        self.assertGreater(TeamEloHistory.objects.filter(sport='mlb').count(), 0)

    def test_already_backfilled_state_is_skipped(self):
        _seed_thirty_mlb_teams_with_final_games()
        # First call does the work.
        call_command('ensure_elo_backfilled', stdout=StringIO())
        history_before = TeamEloHistory.objects.filter(sport='mlb').count()

        # Second call should detect existing state and skip.
        out = StringIO()
        call_command('ensure_elo_backfilled', stdout=out)
        body = out.getvalue()
        self.assertIn('already backfilled', body)
        self.assertIn('skipping', body)

        # History row count unchanged — no re-processing.
        history_after = TeamEloHistory.objects.filter(sport='mlb').count()
        self.assertEqual(history_before, history_after)


class ForceFlagTests(TestCase):
    def test_force_flag_rebuilds_even_if_backfilled(self):
        _seed_thirty_mlb_teams_with_final_games()
        call_command('ensure_elo_backfilled', stdout=StringIO())
        history_after_first = TeamEloHistory.objects.filter(sport='mlb').count()

        out = StringIO()
        call_command('ensure_elo_backfilled', '--force', stdout=out)
        body = out.getvalue()
        self.assertIn('--force passed', body)

        # rebuild_elo_ratings is idempotent — same history count.
        history_after_force = TeamEloHistory.objects.filter(sport='mlb').count()
        self.assertEqual(history_after_first, history_after_force)


class DetectionEdgeCaseTests(TestCase):
    def test_single_team_with_elo_does_not_count_as_backfilled(self):
        # Threshold is MIN_BACKFILLED_TEAMS=20 AND at least one history
        # row. A single team with elo_rating but no history (e.g., from
        # a test or manual shell call) should NOT count.
        from apps.mlb.models import Conference, Team
        league = Conference.objects.create(
            name='AL', slug=f'al-{timezone.now().timestamp()}',
        )
        Team.objects.create(
            name='T0',
            slug=f't-{timezone.now().timestamp()}',
            conference=league,
            elo_rating=1525.0,
        )

        out = StringIO()
        call_command('ensure_elo_backfilled', stdout=out)
        body = out.getvalue()
        # Detection sees only 1 team with elo and 0 history rows → not backfilled.
        self.assertIn('not yet backfilled', body)

    def test_history_without_threshold_teams_does_not_count(self):
        # If you somehow had history rows but only a handful of teams
        # with elo (extremely unlikely; defensive), still not backfilled.
        from apps.mlb.models import Conference, Game, Team
        league = Conference.objects.create(
            name='AL', slug=f'al-{timezone.now().timestamp()}',
        )
        a = Team.objects.create(
            name='A', slug=f'a-{timezone.now().timestamp()}',
            conference=league, elo_rating=1500.0,
        )
        b = Team.objects.create(
            name='B', slug=f'b-{timezone.now().timestamp()}',
            conference=league, elo_rating=1500.0,
        )
        game = Game.objects.create(
            home_team=a, away_team=b,
            first_pitch=timezone.now() - timedelta(days=2),
            status='final', home_score=5, away_score=3,
        )
        # Manually write a history row (simulates the partial state).
        TeamEloHistory.objects.create(
            sport='mlb', mlb_team=a, mlb_game=game,
            pre_rating=1500.0, post_rating=1525.0, k_factor=4.0,
            is_home=True, won=True, margin=None, margin_multiplier=1.0,
        )

        out = StringIO()
        call_command('ensure_elo_backfilled', stdout=out)
        # 2 teams with elo < threshold of 20 → not backfilled.
        self.assertIn('not yet backfilled', out.getvalue())


class FailureIsolationTests(TestCase):
    def test_rebuild_failure_does_not_raise(self):
        # Even if rebuild_elo_ratings explodes, the deploy must continue.
        # Patch the inner call_command to raise; ensure outer handler
        # catches it and prints a warning.
        _seed_thirty_mlb_teams_with_final_games()

        with patch(
            'apps.datahub.management.commands.ensure_elo_backfilled.call_command',
            side_effect=RuntimeError('simulated rebuild failure'),
        ):
            out = StringIO()
            try:
                call_command('ensure_elo_backfilled', stdout=out)
            except RuntimeError:
                self.fail(
                    'ensure_elo_backfilled must swallow inner failures; '
                    'a failed Elo backfill should never block a Railway deploy.'
                )

            body = out.getvalue()
            self.assertIn('rebuild failed', body)
            self.assertIn('continuing', body)
            self.assertIn('Live behavior unaffected', body)
