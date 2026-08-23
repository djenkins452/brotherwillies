"""v3.4 team-offense phase 2 — per-kind visibility + stale-running
recovery + batched isolated-analyzer parity tests.

Covers the operational-visibility fixes shipped after the isolated
analysis went invisible for 35+ minutes on production. The bug had
two roots:

  1. Template didn't display `kind` in the recent-runs table — a
     running/completed isolated-analysis row was indistinguishable
     from a phase-1 offense-replay row.
  2. `last_completed` was a global "most recent completed" — so the
     phase-1 offense-replay hid a later isolated-analysis completion.

Also verifies:
  * Stale-running detection catches rows in `running` > 60min.
  * Force-clear endpoint refuses to flip fresh rows.
  * Batched (cached) isolated analyzer matches the DB-backed extractors
    on synthetic data (parity lock).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import BullpenExperimentRun
from apps.mlb.models import (
    Conference, Game, Team, TeamBattingSnapshot,
)


def _staff():
    u = User.objects.create_user('staff', 'staff@x.com', 'x', is_staff=True)
    return u


def _conf():
    return Conference.objects.first() or Conference.objects.create(
        name='MLB', slug='mlb',
    )


def _team(name, slug, ext_id):
    return Team.objects.create(
        name=name, slug=slug, conference=_conf(),
        source='mlb_stats_api', external_id=ext_id,
    )


class PerKindVisibilityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(_staff())

    def test_view_context_has_per_kind_dict(self):
        """The page context must expose per_kind for every declared
        BullpenExperimentRun kind, so each experiment's status
        renders independently."""
        # Seed one row per kind.
        for k in ('experiment', 'attribution', 'veto_walkforward',
                  'offense_replay', 'offense_isolated', 'offense_v2_replay'):
            BullpenExperimentRun.objects.create(
                kind=k, days=180, blend_weight=0.55,
                status='completed',
                started_at=timezone.now() - dt.timedelta(minutes=5),
                finished_at=timezone.now(),
                result={'window': {'from': '2026-01-01', 'to': '2026-01-02',
                                   'days': 180, 'games_evaluable': 0}},
            )

        resp = self.client.get(reverse('analytics:bullpen_experiment'))
        self.assertEqual(resp.status_code, 200)
        pk = resp.context['per_kind']
        for k in ('experiment', 'attribution', 'veto_walkforward',
                  'offense_replay', 'offense_isolated', 'offense_v2_replay'):
            self.assertIn(k, pk, f'per_kind missing {k}')
            self.assertIsNotNone(pk[k]['last_completed'],
                                 f'{k} last_completed should be non-null')

    def test_isolated_analysis_hidden_by_older_completed_bug_is_fixed(self):
        """Regression: BEFORE the fix, `last_completed` was
        `.filter(status='completed').first()` (global). Now the
        per_kind dict returns each kind's OWN most-recent completed
        run — an older phase-1 completion CANNOT hide a phase-2
        isolated completion."""
        # Newer phase-1 completion (would win the old global race).
        old = BullpenExperimentRun.objects.create(
            kind='offense_replay', days=180, blend_weight=0.55,
            status='completed',
            started_at=timezone.now() - dt.timedelta(hours=2),
            finished_at=timezone.now() - dt.timedelta(hours=1),
            result={'window': {'days': 180, 'from': '2026-01-01',
                               'to': '2026-01-02',
                               'games_evaluable': 0,
                               'blend_weight': 0.55,
                               'offense_weight': 0.5}},
        )
        # And an EARLIER isolated completion.
        iso = BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='completed',
            started_at=timezone.now() - dt.timedelta(hours=10),
            finished_at=timezone.now() - dt.timedelta(hours=9),
            result={'window': {'days': 180, 'from': '2026-01-01',
                               'to': '2026-01-02',
                               'games_evaluable': 0,
                               'games_with_outcome': 0},
                    'per_candidate': {},
                    'selected_candidate': None,
                    'overall_verdict': 'NO_GO_OFFENSE'},
        )

        resp = self.client.get(reverse('analytics:bullpen_experiment'))
        pk = resp.context['per_kind']
        # Even though the offense_replay is newer overall, the isolated
        # entry MUST retain its own last_completed reference.
        self.assertEqual(pk['offense_isolated']['last_completed'].id, iso.id)
        self.assertEqual(pk['offense_replay']['last_completed'].id, old.id)

    def test_running_isolated_analysis_visible_in_per_kind(self):
        """A row in `running` state for kind=offense_isolated must be
        exposed on the per_kind panel so the operator can see status
        without inferring from a generic flash message."""
        BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='running',
            started_at=timezone.now(),
            progress_variant='analyze',
            progress_current=42, progress_total=1000,
        )
        resp = self.client.get(reverse('analytics:bullpen_experiment'))
        self.assertIsNotNone(resp.context['per_kind']['offense_isolated']['running'])


class StaleRunningDetectionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(_staff())

    def test_stale_running_appears_in_context(self):
        """Row running for >60min shows up in stale_running_runs."""
        BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='running',
            started_at=timezone.now() - dt.timedelta(minutes=90),
        )
        resp = self.client.get(reverse('analytics:bullpen_experiment'))
        stale = resp.context['stale_running_runs']
        self.assertEqual(len(stale), 1)

    def test_fresh_running_not_stale(self):
        """Row running for <60min must NOT show up as stale."""
        BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='running',
            started_at=timezone.now() - dt.timedelta(minutes=5),
        )
        resp = self.client.get(reverse('analytics:bullpen_experiment'))
        self.assertEqual(len(resp.context['stale_running_runs']), 0)

    def test_force_clear_flips_stale_row(self):
        """POST to force_clear_stale_experiment flips a stale
        `running` row to `failed`."""
        stale = BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='running',
            started_at=timezone.now() - dt.timedelta(minutes=120),
        )
        resp = self.client.post(
            reverse('analytics:force_clear_stale_experiment'),
            data={'run_id': str(stale.id)},
        )
        self.assertEqual(resp.status_code, 302)
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'failed')
        self.assertIn('Force-cleared', stale.failure_summary)

    def test_force_clear_refuses_fresh_row(self):
        """Force-clear MUST NOT flip a row that started <60min ago
        (genuinely running work)."""
        fresh = BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='running',
            started_at=timezone.now() - dt.timedelta(minutes=5),
        )
        self.client.post(
            reverse('analytics:force_clear_stale_experiment'),
            data={'run_id': str(fresh.id)},
        )
        fresh.refresh_from_db()
        # Row unchanged — still running.
        self.assertEqual(fresh.status, 'running')


class CachedAnalyzerParityTests(TestCase):
    def test_snapshot_cache_matches_db_windows(self):
        """SnapshotCache.rolling/season_to_date must return the SAME
        TeamHittingWindow values as the DB-backed team_offense_v2
        functions on the same data. Parity lock — future perf changes
        can't drift from the reference path."""
        from apps.analytics.services.team_offense_isolated_analysis import (
            _SnapshotCache,
        )
        from apps.mlb.services.team_offense_v2 import (
            rolling_window as db_rolling,
            season_to_date_window as db_season,
        )

        t = _team('LAD', 'lad', '119')
        for day_offset in range(0, 60):
            d = dt.date(2026, 6, 1) - dt.timedelta(days=day_offset)
            TeamBattingSnapshot.objects.create(
                team=t, as_of_date=d, season=2026,
                plate_appearances=1000 - day_offset * 10,
                at_bats=900 - day_offset * 9,
                hits=250 - day_offset * 2,
                doubles=50 - day_offset,
                triples=5, home_runs=30 - day_offset // 3,
                walks=90, hit_by_pitch=10, sac_flies=10,
                strikeouts=200, runs=110, games_played=25,
            )

        snapshots = list(TeamBattingSnapshot.objects.all())
        cache = _SnapshotCache(snapshots)

        ref = dt.date(2026, 6, 15)
        cached_r = cache.rolling(t.id, ref, 30)
        db_r = db_rolling(t, ref, window_days=30)
        # PA & counts should match exactly.
        self.assertEqual(cached_r.pa, db_r.pa)
        self.assertEqual(cached_r.hits, db_r.hits)
        self.assertEqual(cached_r.ab, db_r.ab)

        cached_s = cache.season_to_date(t.id, ref)
        db_s = db_season(t, ref)
        self.assertEqual(cached_s.pa, db_s.pa)
        self.assertEqual(cached_s.hits, db_s.hits)

    def test_cache_returns_empty_when_no_data(self):
        from apps.analytics.services.team_offense_isolated_analysis import (
            _SnapshotCache,
        )
        cache = _SnapshotCache([])
        w = cache.rolling(999, dt.date(2026, 6, 1), 30)
        self.assertEqual(w.pa, 0)


class NoDuplicateRunOnRefreshTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(_staff())

    def test_trigger_isolated_refuses_when_running(self):
        """A second POST while a run is already 'running' must NOT
        create a duplicate row — the concurrency guard fires. This
        protects against operator refresh/re-submit noise."""
        BullpenExperimentRun.objects.create(
            kind='offense_isolated', days=180, blend_weight=0.55,
            status='running',
            started_at=timezone.now(),
        )
        before = BullpenExperimentRun.objects.count()
        resp = self.client.post(
            reverse('analytics:trigger_team_offense_isolated'),
            data={'days': 180},
        )
        self.assertEqual(resp.status_code, 302)
        # No new row added.
        self.assertEqual(BullpenExperimentRun.objects.count(), before)


class IsolatedAnalyzerImportLockTests(TestCase):
    """Regression: the isolated analyzer's per-game context builder
    imports several helpers from other modules. If ANY of these names
    change, the ImportError propagates out of the per-game try/except
    (which only guards the CALL, not the module-level import) and
    kills the entire run at the first game.

    This test loads the analyzer's dependency modules and asserts each
    imported name exists. It would have caught the 2026-08-24 crash
    where the analyzer imported `recent_form_signal` (non-existent —
    the real name is `recent_form_delta`)."""

    def test_pitcher_form_recent_form_delta_exists(self):
        from apps.mlb.services import pitcher_form
        self.assertTrue(hasattr(pitcher_form, 'recent_form_delta'),
                        'recent_form_delta missing on pitcher_form — '
                        'isolated analyzer will crash')

    def test_odds_helpers_exist(self):
        from apps.core.utils import odds
        self.assertTrue(hasattr(odds, 'american_to_implied_prob'))
        self.assertTrue(hasattr(odds, 'devig_two_way'))

    def test_method_replay_pregame_helper_exists(self):
        from apps.analytics.services import method_replay
        self.assertTrue(hasattr(method_replay, '_pregame_team_rating'))

    def test_odds_snapshot_related_name_is_odds_snapshots(self):
        """The analyzer accesses `game.odds_snapshots` — the related
        name on the OddsSnapshot FK must be exactly that."""
        from apps.mlb.models import Game
        fields = [f.name for f in Game._meta.get_fields()]
        self.assertIn('odds_snapshots', fields,
                      'Game.odds_snapshots related_name changed — '
                      'analyzer market_prob lookup will fail')

    def test_run_isolated_analysis_runs_end_to_end_on_synthetic_game(self):
        """Full-path smoke: build one final game + snapshots + Elo
        history, run the analyzer, confirm it returns a dict and
        does NOT raise. Would have caught the ImportError in
        production."""
        from apps.analytics.services.team_offense_isolated_analysis import (
            run_isolated_analysis,
        )
        home = _team('LAD', 'lad', '119')
        away = _team('SFG', 'sfg', '137')
        # One completed game 20 days ago (well inside the 180d window).
        fp = timezone.now() - dt.timedelta(days=20)
        Game.objects.create(
            source='mlb_stats_api', external_id='g-smoke-1',
            home_team=home, away_team=away,
            first_pitch=fp, status='final',
            home_score=5, away_score=3,
        )
        # Enough snapshots for candidate B/D coverage.
        for days_back in range(0, 45):
            snap_date = fp.date() - dt.timedelta(days=days_back + 1)
            for t in (home, away):
                TeamBattingSnapshot.objects.create(
                    team=t, as_of_date=snap_date, season=fp.year,
                    plate_appearances=1000 - days_back * 10,
                    at_bats=900 - days_back * 9,
                    hits=250 - days_back * 2,
                    doubles=50, triples=5, home_runs=30,
                    walks=90, hit_by_pitch=10, sac_flies=10,
                    strikeouts=200, runs=110, games_played=25,
                )
        result = run_isolated_analysis(days=180)
        self.assertIn('per_candidate', result)
        self.assertIn('overall_verdict', result)
        # Per-game errors should be empty on a clean synthetic path.
        self.assertEqual(result.get('per_game_errors_total', 0), 0)


class ProductionMethodologyUnchangedTests(TestCase):
    def test_use_team_offense_flag_default_false(self):
        """USE_TEAM_OFFENSE must remain false in the code default — no
        visibility fix should silently activate the phase-2 feature."""
        from brotherwillies import settings as s
        self.assertFalse(getattr(s, 'USE_TEAM_OFFENSE', False))

    def test_bullpen_flags_default_false(self):
        from brotherwillies import settings as s
        self.assertFalse(getattr(s, 'USE_BULLPEN_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_BULLPEN_FATIGUE', False))
