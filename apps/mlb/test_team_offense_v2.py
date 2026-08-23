"""v3.4 team-offense PHASE 2 — tests for OPS/OBP/SLG signal service.

Covers:
  * TeamBattingSnapshot uniqueness + save/load round-trip
  * team_offense_v2.rolling_window subtraction math
  * team_offense_v2.season_to_date_window leakage boundary
  * candidate B/C/D behavior (min-sample gate, non-zero delta on
    real data)
  * fetch_team_hitting_range integration (mocked)
  * team_batting_backfill_service upsert idempotence
  * isolated analyzer verdict rules with synthetic scenarios
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.mlb.models import (
    Conference, Game, Team, TeamBattingSnapshot,
)


def _mk_team(name, slug, external_id):
    conf = Conference.objects.first() or Conference.objects.create(
        name='MLB', slug='mlb',
    )
    return Team.objects.create(
        name=name, slug=slug, conference=conf,
        source='mlb_stats_api', external_id=external_id,
    )


def _mk_snapshot(team, as_of_date, *, pa=1000, ab=900, hits=250,
                 doubles=50, triples=5, hr=30, bb=90, hbp=10,
                 sf=10, k=200, runs=110, games=25,
                 season=2026,
                 obp=None, slg=None, ops=None):
    return TeamBattingSnapshot.objects.create(
        team=team, as_of_date=as_of_date, season=season,
        plate_appearances=pa, at_bats=ab, hits=hits,
        doubles=doubles, triples=triples, home_runs=hr,
        walks=bb, hit_by_pitch=hbp, sac_flies=sf,
        strikeouts=k, runs=runs, games_played=games,
        obp_reported=obp, slg_reported=slg, ops_reported=ops,
    )


class TeamBattingSnapshotModelTests(TestCase):
    def test_unique_per_team_and_date(self):
        t = _mk_team('Los Angeles Dodgers', 'lad', '119')
        _mk_snapshot(t, dt.date(2026, 6, 1))
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _mk_snapshot(t, dt.date(2026, 6, 1))

    def test_different_teams_or_dates_ok(self):
        t1 = _mk_team('LAD', 'lad', '119')
        t2 = _mk_team('SFG', 'sfg', '137')
        _mk_snapshot(t1, dt.date(2026, 6, 1))
        _mk_snapshot(t1, dt.date(2026, 6, 2))
        _mk_snapshot(t2, dt.date(2026, 6, 1))
        self.assertEqual(TeamBattingSnapshot.objects.count(), 3)


class RollingWindowMathTests(TestCase):
    def test_subtract_math_matches_definition(self):
        """rolling(D) = snapshot(D-1) MINUS snapshot(D-1-window). Verify
        we recover the delta counts correctly."""
        from apps.mlb.services.team_offense_v2 import (
            _subtract,
        )
        t = _mk_team('LAD', 'lad', '119')
        # Prior snapshot: 500 PA, 100 hits.
        old = _mk_snapshot(
            t, dt.date(2026, 5, 1),
            pa=500, ab=450, hits=100, doubles=20, triples=2, hr=15,
            bb=45, hbp=5, sf=5, k=100, runs=60, games=12,
        )
        # Newer snapshot: 1000 PA, 250 hits — delta = 500 PA, 150 hits.
        new = _mk_snapshot(
            t, dt.date(2026, 6, 1),
            pa=1000, ab=900, hits=250, doubles=50, triples=5, hr=30,
            bb=90, hbp=10, sf=10, k=200, runs=110, games=25,
        )
        w = _subtract(old, new)
        self.assertEqual(w.pa, 500)
        self.assertEqual(w.ab, 450)
        self.assertEqual(w.hits, 150)
        self.assertEqual(w.doubles, 30)
        self.assertEqual(w.games, 13)

    def test_subtract_none_returns_full_counts(self):
        """When no prior snapshot exists (early-season), rolling window
        degrades to season-to-date — subtract(None, snap) returns snap."""
        from apps.mlb.services.team_offense_v2 import _subtract
        t = _mk_team('SFG', 'sfg', '137')
        snap = _mk_snapshot(t, dt.date(2026, 4, 1),
                            pa=200, ab=180, hits=50)
        w = _subtract(None, snap)
        self.assertEqual(w.pa, 200)
        self.assertEqual(w.hits, 50)

    def test_obp_slg_ops_computed_from_raw_counts(self):
        """Standard formulas hold on a known input."""
        from apps.mlb.services.team_offense_v2 import _subtract
        t = _mk_team('LAD', 'lad', '119')
        # Snapshot with easy math: AB=100, H=25 (5 2B, 2 3B, 3 HR, 15 1B),
        # BB=10, HBP=1, SF=1, PA=112 (100+10+1+1).
        snap = _mk_snapshot(
            t, dt.date(2026, 6, 1),
            pa=112, ab=100, hits=25, doubles=5, triples=2, hr=3,
            bb=10, hbp=1, sf=1, k=20, runs=15, games=3,
        )
        w = _subtract(None, snap)
        # OBP = (25+10+1)/(100+10+1+1) = 36/112 = 0.3214...
        self.assertAlmostEqual(w.obp, 36/112, places=4)
        # TB = 15*1 + 5*2 + 2*3 + 3*4 = 15+10+6+12 = 43
        # SLG = 43/100 = 0.43
        self.assertAlmostEqual(w.slg, 0.43, places=4)
        # OPS = OBP + SLG
        self.assertAlmostEqual(w.ops, 36/112 + 0.43, places=4)


class LeakageBoundaryTests(TestCase):
    def test_strict_less_than_reference_date(self):
        """A snapshot with as_of_date EQUAL to the reference date must
        NOT be used — that would represent post-first-pitch data."""
        from apps.mlb.services.team_offense_v2 import (
            _latest_snapshot_strictly_before,
        )
        t = _mk_team('LAD', 'lad', '119')
        ref = dt.date(2026, 6, 15)
        _mk_snapshot(t, ref, pa=999)   # same-day — MUST be excluded
        _mk_snapshot(t, ref - dt.timedelta(days=1), pa=888)
        found = _latest_snapshot_strictly_before(t, ref)
        self.assertIsNotNone(found)
        self.assertEqual(found.plate_appearances, 888)

    def test_season_to_date_returns_zero_when_no_snapshot(self):
        from apps.mlb.services.team_offense_v2 import season_to_date_window
        t = _mk_team('LAD', 'lad', '119')
        w = season_to_date_window(t, dt.date(2026, 6, 1))
        self.assertEqual(w.pa, 0)


class CandidateSignalTests(TestCase):
    def test_b_low_confidence_below_min_pa(self):
        """Rolling window with < MIN_ROLLING_PA returns zero delta +
        low confidence."""
        from apps.mlb.services.team_offense_v2 import candidate_b_rolling_ops
        t = _mk_team('LAD', 'lad', '119')
        _mk_snapshot(t, dt.date(2026, 5, 30),
                     pa=100, ab=90, hits=25, games=3)
        sig = candidate_b_rolling_ops(t, dt.date(2026, 6, 1))
        self.assertEqual(sig.confidence, 'low')
        self.assertEqual(sig.delta_units, 0.0)
        self.assertEqual(sig.candidate, 'B_v2_rolling_ops')

    def test_b_positive_delta_on_above_avg_ops(self):
        from apps.mlb.services.team_offense_v2 import (
            candidate_b_rolling_ops, LEAGUE_AVG_OPS,
        )
        t = _mk_team('LAD', 'lad', '119')
        # Snapshot heavy on hits, walks, doubles → OPS ~1.000
        _mk_snapshot(
            t, dt.date(2026, 5, 30),
            pa=1000, ab=850, hits=300, doubles=70, triples=8,
            hr=45, bb=140, hbp=10, sf=8, k=180, runs=180, games=30,
        )
        sig = candidate_b_rolling_ops(t, dt.date(2026, 6, 1))
        self.assertNotEqual(sig.confidence, 'low')
        self.assertGreater(sig.raw_value, LEAGUE_AVG_OPS)
        self.assertGreater(sig.delta_units, 0)

    def test_c_returns_two_signals_obp_and_slg(self):
        from apps.mlb.services.team_offense_v2 import candidate_c_rolling_obp_slg
        t = _mk_team('LAD', 'lad', '119')
        _mk_snapshot(
            t, dt.date(2026, 5, 30),
            pa=1000, ab=850, hits=250, doubles=50, triples=5,
            hr=30, bb=120, hbp=8, sf=10, k=200, runs=120, games=25,
        )
        obp_sig, slg_sig = candidate_c_rolling_obp_slg(t, dt.date(2026, 6, 1))
        self.assertEqual(obp_sig.candidate, 'C_v2_rolling_obp')
        self.assertEqual(slg_sig.candidate, 'C_v2_rolling_slg')

    def test_d_blend_requires_both_windows(self):
        """D degrades to low confidence when only one window is available."""
        from apps.mlb.services.team_offense_v2 import candidate_d_blend_ops
        t = _mk_team('LAD', 'lad', '119')
        # Small rolling window only — insufficient season-to-date.
        _mk_snapshot(
            t, dt.date(2026, 5, 30),
            pa=250, ab=220, hits=60, doubles=12, triples=1,
            hr=8, bb=25, hbp=2, sf=3, k=55, runs=32, games=8,
        )
        sig = candidate_d_blend_ops(t, dt.date(2026, 6, 1))
        # Season-to-date PA = 250 which is < MIN_SEASON_PA (400) → low.
        self.assertEqual(sig.confidence, 'low')


class StatsApiClientTests(TestCase):
    def test_fetch_team_hitting_range_parses_first_split(self):
        """Verify the client extracts the first split's `stat` dict."""
        from apps.datahub.providers.mlb.statsapi_client import (
            fetch_team_hitting_range,
        )
        fake_payload = {
            'stats': [{
                'exemptions': [], 'group': {'displayName': 'hitting'},
                'type': {'displayName': 'byDateRange'},
                'splits': [{
                    'numTeams': 1,
                    'stat': {
                        'plateAppearances': 100, 'atBats': 90, 'hits': 25,
                        'doubles': 5, 'triples': 1, 'homeRuns': 3,
                        'baseOnBalls': 8, 'hitByPitch': 1, 'sacFlies': 1,
                        'strikeOuts': 20, 'runs': 12, 'gamesPlayed': 25,
                        'obp': '.333', 'slg': '.456', 'ops': '.789',
                    },
                    'team': {'id': 119, 'name': 'LAD'},
                }],
            }],
        }
        with patch(
            'apps.datahub.providers.mlb.statsapi_client.fetch_json',
            return_value=fake_payload,
        ):
            stat = fetch_team_hitting_range(
                team_mlb_id=119,
                start_date=dt.date(2026, 3, 1),
                end_date=dt.date(2026, 6, 1),
            )
        self.assertEqual(stat['plateAppearances'], 100)
        self.assertEqual(stat['obp'], '.333')

    def test_fetch_team_hitting_range_empty_when_no_splits(self):
        from apps.datahub.providers.mlb.statsapi_client import (
            fetch_team_hitting_range,
        )
        with patch(
            'apps.datahub.providers.mlb.statsapi_client.fetch_json',
            return_value={'stats': [{'splits': []}]},
        ):
            stat = fetch_team_hitting_range(
                team_mlb_id=119,
                start_date=dt.date(2026, 3, 1),
                end_date=dt.date(2026, 6, 1),
            )
        self.assertEqual(stat, {})


class BackfillIdempotenceTests(TestCase):
    def test_backfill_upsert_updates_in_place(self):
        """Re-running the backfill on the same (team, date) UPDATES
        rather than duplicating."""
        from apps.analytics.models import TeamBattingBackfillRun
        from apps.analytics.services.team_batting_backfill_service import (
            run_team_batting_backfill,
        )

        _mk_team('LAD', 'lad', '119')
        _mk_team('SFG', 'sfg', '137')

        run = TeamBattingBackfillRun.objects.create(
            date_from=dt.date(2026, 6, 1),
            date_to=dt.date(2026, 6, 2),
        )
        fake_stat = {
            'plateAppearances': 500, 'atBats': 450, 'hits': 120,
            'doubles': 25, 'triples': 3, 'homeRuns': 18,
            'baseOnBalls': 45, 'hitByPitch': 4, 'sacFlies': 5,
            'strikeOuts': 100, 'runs': 62, 'gamesPlayed': 15,
            'obp': '.310', 'slg': '.402', 'ops': '.712',
        }
        with patch(
            'apps.analytics.services.team_batting_backfill_service.fetch_team_hitting_range',
            return_value=fake_stat,
        ):
            run_team_batting_backfill(str(run.id), sleep_ms=0)

        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        # 2 teams × 2 dates = 4 snapshots.
        self.assertEqual(TeamBattingSnapshot.objects.count(), 4)

        # Re-run — should update, not duplicate.
        run2 = TeamBattingBackfillRun.objects.create(
            date_from=dt.date(2026, 6, 1),
            date_to=dt.date(2026, 6, 2),
        )
        with patch(
            'apps.analytics.services.team_batting_backfill_service.fetch_team_hitting_range',
            return_value=fake_stat,
        ):
            run_team_batting_backfill(str(run2.id), sleep_ms=0)
        self.assertEqual(TeamBattingSnapshot.objects.count(), 4)


class IsolatedAnalyzerVerdictTests(TestCase):
    def test_no_data_returns_insufficient(self):
        """With no games and no snapshots, isolated analyzer returns
        a NO-GO verdict (insufficient data)."""
        from apps.analytics.services.team_offense_isolated_analysis import (
            run_isolated_analysis,
        )
        result = run_isolated_analysis(days=30)
        self.assertIn('per_candidate', result)
        self.assertEqual(result['overall_verdict'], 'NO_GO_OFFENSE')


class OffenseV2ReplaySafetyTests(TestCase):
    def test_replay_returns_empty_when_no_games(self):
        from apps.analytics.services.offense_v2_replay import (
            run_offense_v2_replay,
        )
        result = run_offense_v2_replay(
            days=30,
            selected_candidate='B_v2_rolling_ops',
        )
        self.assertFalse(result['data_ok'])
        self.assertEqual(result['a_v3_2_baseline']['count'], 0)
        self.assertEqual(result['b_v2_bounded']['count'], 0)

    def test_v2_replay_kind_supported_in_dispatcher(self):
        """kind='offense_v2_replay' must dispatch to the v2 renderer."""
        from apps.analytics.models import BullpenExperimentRun
        supported_kinds = {c[0] for c in BullpenExperimentRun.KIND_CHOICES}
        self.assertIn('offense_v2_replay', supported_kinds)
        self.assertIn('offense_isolated', supported_kinds)

    def test_prob_cap_pre_registered_at_1pp(self):
        """The bounded-integration replay's cap must be 1pp exactly
        (per the pre-registration in the mission brief)."""
        from apps.analytics.services.offense_v2_replay import PROB_CAP_PP
        self.assertEqual(PROB_CAP_PP, 1.0)


class TeamBattingBackfillRunModelTests(TestCase):
    def test_elapsed_seconds_before_start(self):
        from apps.analytics.models import TeamBattingBackfillRun
        r = TeamBattingBackfillRun.objects.create(
            date_from=dt.date(2026, 6, 1),
            date_to=dt.date(2026, 6, 2),
        )
        self.assertEqual(r.elapsed_seconds, 0)
