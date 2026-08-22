"""v3.3 SHADOW — tests for the deterministic bullpen builder and the
MLB Stats API boxscore ingestion.

Covers:
  1. Builder correctness — rate stats, rolling window, IP-scaled confidence.
  2. Builder leakage — strict `<` at reference_date; appearances at or
     after are excluded.
  3. Builder determinism — same inputs → same outputs, byte-for-byte.
  4. Fatigue counts — 1/2/3-day team appearance windows.
  5. Top-reliever identification + availability heuristic.
  6. Boxscore parsing — `_ip_to_outs` and `_extract_pitcher_stats` on a
     real 2026-08-10 boxscore fixture (apps/mlb/test_fixtures/).
  7. Ingest idempotency — running twice writes the same row (update, not
     duplicate). Locked by the (game, pitcher) unique constraint.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.mlb.models import (
    Conference, Game, RelieverAppearance, StartingPitcher, Team,
    TeamBullpenSnapshot,
)
from apps.mlb.services.bullpen_builder import (
    CONFIDENCE_HIGH_IP_MIN,
    CONFIDENCE_MED_IP_MIN,
    build_snapshot,
    persist_snapshot,
)


BOXSCORE_FIXTURE = (
    Path(__file__).parent / 'test_fixtures' / 'boxscore_822780.json'
)


# ---------------------------------------------------------------------------
# Fixtures


def _mk_team(slug, name=None):
    c, _ = Conference.objects.get_or_create(
        slug=f'div-{slug}', defaults={'name': 'Div'},
    )
    return Team.objects.create(
        name=name or f'T-{slug}', slug=f't-{slug}', conference=c,
        rating=50.0, elo_rating=1500,
        source='mlb_stats_api', external_id=f't-{slug}',
        abbreviation=slug[:5].upper(),
    )


def _mk_pitcher(team, name, external_id=None):
    return StartingPitcher.objects.create(
        team=team, name=name, rating=50.0,
        source='mlb_stats_api',
        external_id=external_id or f'p-{team.slug}-{name}',
    )


def _mk_game(home, away, when, external_id=None):
    return Game.objects.create(
        home_team=home, away_team=away, first_pitch=when,
        status='final', source='mlb_stats_api',
        external_id=external_id or f'g-{home.slug}-{when.timestamp()}',
    )


def _mk_appearance(game, team, pitcher, *, is_starter=False,
                   outs=3, pitches=15, hits=1, er=0, bb=1, k=2, hr=0,
                   is_save=False, is_hold=False):
    return RelieverAppearance.objects.create(
        game=game, team=team, pitcher=pitcher,
        is_starter=is_starter, outs_recorded=outs, pitches=pitches,
        hits=hits, earned_runs=er, walks=bb, strikeouts=k, home_runs=hr,
        is_save=is_save, is_hold=is_hold,
    )


# ---------------------------------------------------------------------------
# 1. Builder correctness


class BuilderCorrectnessTests(TestCase):

    def test_era_and_whip_arithmetic(self):
        team = _mk_team('bc1')
        pitcher = _mk_pitcher(team, 'RP1')
        # 3 appearances @ 1 IP each (3 outs) = 3 IP total.
        # Aggregate: 3 H, 2 ER, 1 BB, 4 K → ERA = 9*2/3 = 6.00,
        # WHIP = (3+1)/3 = 1.333, K/9 = 9*4/3 = 12.0
        base = timezone.now() - timedelta(days=1)
        for i in range(3):
            g = _mk_game(team, _mk_team(f'bc1x{i}'),
                        base - timedelta(days=i))
            _mk_appearance(g, team, pitcher, outs=3, hits=1, er=(1 if i < 2 else 0),
                           bb=(1 if i == 0 else 0), k=(2 if i < 2 else 0))
        snap = build_snapshot(team, timezone.now())
        self.assertAlmostEqual(snap.bullpen_era, 6.0, places=2)
        self.assertAlmostEqual(snap.bullpen_whip, 4.0/3.0, places=2)
        self.assertAlmostEqual(snap.bullpen_k_per_9, 12.0, places=2)

    def test_empty_appearances_returns_zeros(self):
        team = _mk_team('bc2')
        snap = build_snapshot(team, timezone.now())
        self.assertIsNone(snap.bullpen_era)
        self.assertIsNone(snap.bullpen_whip)
        self.assertEqual(snap.bullpen_ip_last30, 0.0)
        self.assertEqual(snap.appearances_last_1_day, 0)
        self.assertEqual(snap.data_confidence, 'low')

    def test_confidence_thresholds(self):
        team = _mk_team('bc3')
        pitcher = _mk_pitcher(team, 'RP')
        base = timezone.now() - timedelta(days=2)
        # Push 25 IP (= 75 outs) into the window via 25 apps of 1 IP.
        for i in range(25):
            g = _mk_game(team, _mk_team(f'bc3x{i}'),
                        base - timedelta(hours=i))
            _mk_appearance(g, team, pitcher, outs=3)
        snap = build_snapshot(team, timezone.now())
        self.assertGreaterEqual(snap.bullpen_ip_last30, CONFIDENCE_HIGH_IP_MIN)
        self.assertEqual(snap.data_confidence, 'high')


# ---------------------------------------------------------------------------
# 2. Leakage — strict `<` on reference_date


class LeakageTests(TestCase):

    def test_appearance_at_reference_date_is_excluded(self):
        team = _mk_team('lk1')
        pitcher = _mk_pitcher(team, 'RP')
        ref = timezone.now()
        # Game AT the reference date exactly — MUST be excluded.
        g = _mk_game(team, _mk_team('lk1x'), ref)
        _mk_appearance(g, team, pitcher, outs=3, er=1, hits=1)
        snap = build_snapshot(team, ref)
        self.assertEqual(snap.bullpen_ip_last30, 0.0)
        self.assertIsNone(snap.bullpen_era)

    def test_appearance_one_second_before_is_included(self):
        team = _mk_team('lk2')
        pitcher = _mk_pitcher(team, 'RP')
        ref = timezone.now()
        g = _mk_game(team, _mk_team('lk2x'), ref - timedelta(seconds=1))
        _mk_appearance(g, team, pitcher, outs=3, er=1)
        snap = build_snapshot(team, ref)
        self.assertGreater(snap.bullpen_ip_last30, 0.0)
        # outs=3 → IP=1.0; 1 ER over 1 IP → ERA = 9*1/1 = 9.0
        self.assertEqual(snap.bullpen_era, 9.0)

    def test_appearance_outside_window_excluded(self):
        team = _mk_team('lk3')
        pitcher = _mk_pitcher(team, 'RP')
        ref = timezone.now()
        # 45 days ago — outside 30-day window.
        g = _mk_game(team, _mk_team('lk3x'), ref - timedelta(days=45))
        _mk_appearance(g, team, pitcher, outs=3, er=5)  # big ERA
        snap = build_snapshot(team, ref)
        # Outside window → doesn't influence rolling metric.
        self.assertEqual(snap.bullpen_ip_last30, 0.0)


# ---------------------------------------------------------------------------
# 3. Determinism — same inputs → same outputs


class DeterminismTests(TestCase):

    def test_two_builds_produce_identical_snapshot(self):
        team = _mk_team('det1')
        pitcher = _mk_pitcher(team, 'RP')
        ref = timezone.now()
        for i in range(5):
            g = _mk_game(team, _mk_team(f'det1x{i}'),
                        ref - timedelta(days=i+1))
            _mk_appearance(g, team, pitcher, outs=3, er=1, hits=2)
        a = build_snapshot(team, ref)
        b = build_snapshot(team, ref)
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# 4. Fatigue day-window counts


class FatigueCountsTests(TestCase):

    def test_1_2_3_day_appearance_counts_are_team_totals(self):
        team = _mk_team('ft1')
        p1 = _mk_pitcher(team, 'A')
        p2 = _mk_pitcher(team, 'B')
        ref = timezone.now()
        # Yesterday: 3 apps (p1 x2, p2 x1).
        g1 = _mk_game(team, _mk_team('ft1x1'), ref - timedelta(hours=8))
        g2 = _mk_game(team, _mk_team('ft1x2'), ref - timedelta(hours=6))
        _mk_appearance(g1, team, p1); _mk_appearance(g2, team, p1)
        _mk_appearance(g1, team, p2)
        # 2 days ago: 1 app.
        g3 = _mk_game(team, _mk_team('ft1x3'),
                     ref - timedelta(days=1, hours=6))
        _mk_appearance(g3, team, p2)
        # 3 days ago: 1 app.
        g4 = _mk_game(team, _mk_team('ft1x4'),
                     ref - timedelta(days=2, hours=6))
        _mk_appearance(g4, team, p1)
        snap = build_snapshot(team, ref)
        self.assertEqual(snap.appearances_last_1_day, 3)
        self.assertEqual(snap.appearances_last_2_days, 4)
        self.assertEqual(snap.appearances_last_3_days, 5)


# ---------------------------------------------------------------------------
# 5. Top-reliever + availability


class TopRelieverTests(TestCase):

    def test_top_reliever_unavailable_when_appeared_yesterday(self):
        team = _mk_team('tr1')
        closer = _mk_pitcher(team, 'Closer')
        setup = _mk_pitcher(team, 'Setup')
        ref = timezone.now()
        # Closer racked up 5 saves in the last 20 days.
        for i in range(5):
            g = _mk_game(team, _mk_team(f'tr1x{i}'),
                        ref - timedelta(days=i+2, hours=6))
            _mk_appearance(g, team, closer, outs=3, is_save=True)
            _mk_appearance(g, team, setup, outs=3, is_hold=True)
        # Yesterday — closer pitched. Should be unavailable.
        g_yesterday = _mk_game(team, _mk_team('tr1y'), ref - timedelta(hours=8))
        _mk_appearance(g_yesterday, team, closer, outs=3)
        snap = build_snapshot(team, ref)
        self.assertIs(snap.top_reliever_available, False)

    def test_top_reliever_available_when_rested(self):
        team = _mk_team('tr2')
        closer = _mk_pitcher(team, 'Closer')
        ref = timezone.now()
        # Closer collected 3 saves over 3 recent days but the most recent
        # was 3 days ago — rested.
        for i in range(3):
            g = _mk_game(team, _mk_team(f'tr2x{i}'),
                        ref - timedelta(days=3+i, hours=6))
            _mk_appearance(g, team, closer, outs=3, is_save=True)
        snap = build_snapshot(team, ref)
        self.assertIs(snap.top_reliever_available, True)

    def test_no_top_reliever_returns_none(self):
        team = _mk_team('tr3')
        snap = build_snapshot(team, timezone.now())
        self.assertIsNone(snap.top_reliever_available)


# ---------------------------------------------------------------------------
# 6. Boxscore parsing — real MLB Stats API fixture


class BoxscoreParsingTests(TestCase):

    def test_ip_to_outs_conversion(self):
        from apps.datahub.management.commands.ingest_reliever_appearances import (
            _ip_to_outs,
        )
        self.assertEqual(_ip_to_outs('0.0'), 0)
        self.assertEqual(_ip_to_outs('0.1'), 1)
        self.assertEqual(_ip_to_outs('0.2'), 2)
        self.assertEqual(_ip_to_outs('1.0'), 3)
        self.assertEqual(_ip_to_outs('1.2'), 5)
        self.assertEqual(_ip_to_outs('4.0'), 12)
        self.assertEqual(_ip_to_outs('7.1'), 22)
        self.assertEqual(_ip_to_outs(None), 0)

    def test_extract_pitcher_stats_from_real_fixture(self):
        from apps.datahub.management.commands.ingest_reliever_appearances import (
            _extract_pitcher_stats,
        )
        data = json.loads(BOXSCORE_FIXTURE.read_text())
        # Toronto home pitcher #592791 (the starter) → 4.0 IP, 0 ER, 3 K, 61 pitches, gamesStarted=1
        pitcher_block = data['teams']['home']['players']['ID592791']
        stats = _extract_pitcher_stats(pitcher_block)
        self.assertTrue(stats['is_starter'])
        self.assertEqual(stats['outs_recorded'], 12)  # 4.0 IP
        self.assertEqual(stats['earned_runs'], 0)
        self.assertEqual(stats['strikeouts'], 3)
        self.assertEqual(stats['walks'], 3)
        self.assertEqual(stats['hits'], 1)
        self.assertEqual(stats['pitches'], 61)
        self.assertFalse(stats['is_save'])
        self.assertFalse(stats['is_hold'])

    def test_extract_reliever_stats_from_real_fixture(self):
        from apps.datahub.management.commands.ingest_reliever_appearances import (
            _extract_pitcher_stats,
        )
        data = json.loads(BOXSCORE_FIXTURE.read_text())
        # Toronto reliever #643511 got a HOLD.
        p = data['teams']['home']['players']['ID643511']
        stats = _extract_pitcher_stats(p)
        self.assertFalse(stats['is_starter'])
        self.assertTrue(stats['is_hold'])
        self.assertFalse(stats['is_save'])
        self.assertEqual(stats['outs_recorded'], 3)  # 1.0 IP


# ---------------------------------------------------------------------------
# 7. Ingest command — idempotency via mocked HTTP


class IngestIdempotencyTests(TestCase):
    """The ingest command uses `update_or_create` on (game, pitcher).
    Re-running with the same boxscore leaves the DB in the same state."""

    def _seed_game_and_teams(self):
        # Boxscore fixture is Toronto (home, id=141) vs Boston (away, id=111).
        # Our test only needs Teams + Game rows keyed by external_id=822780.
        home = _mk_team('tor', name='Toronto Blue Jays')
        home.external_id = '141'; home.save()
        away = _mk_team('bos', name='Boston Red Sox')
        away.external_id = '111'; away.save()
        # Game — external_id must match gamePk.
        g = _mk_game(home, away, timezone.now() - timedelta(days=1),
                    external_id='822780')
        return g, home, away

    def _fixture_response(self, url, params=None, timeout=None):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        if 'schedule' in url:
            m.json = MagicMock(return_value={
                'dates': [{'games': [{
                    'gamePk': 822780,
                    'status': {'detailedState': 'Final'},
                }]}]
            })
        else:
            m.json = MagicMock(return_value=json.loads(BOXSCORE_FIXTURE.read_text()))
        return m

    @patch('apps.datahub.management.commands.ingest_reliever_appearances.requests.get')
    def test_first_run_writes_appearances(self, mock_get):
        mock_get.side_effect = self._fixture_response
        self._seed_game_and_teams()
        call_command('ingest_reliever_appearances', '--gamepk=822780', '--sleep-ms=0')
        n = RelieverAppearance.objects.count()
        self.assertGreater(n, 0)
        # Roughly: home has 4 pitchers per boxscore, away has some too.
        # We just assert the writes happened; the boxscore-parsing tests
        # already lock the per-pitcher values.

    @patch('apps.datahub.management.commands.ingest_reliever_appearances.requests.get')
    def test_second_run_produces_same_state(self, mock_get):
        mock_get.side_effect = self._fixture_response
        self._seed_game_and_teams()
        call_command('ingest_reliever_appearances', '--gamepk=822780', '--sleep-ms=0')
        after_first = list(
            RelieverAppearance.objects.order_by('id')
            .values('game_id', 'pitcher_id', 'outs_recorded',
                    'earned_runs', 'is_starter', 'pitches')
        )
        # Re-run WITH --refresh so update-or-create fires again.
        call_command('ingest_reliever_appearances',
                     '--gamepk=822780', '--sleep-ms=0', '--refresh')
        after_second = list(
            RelieverAppearance.objects.order_by('id')
            .values('game_id', 'pitcher_id', 'outs_recorded',
                    'earned_runs', 'is_starter', 'pitches')
        )
        self.assertEqual(after_first, after_second)

    @patch('apps.datahub.management.commands.ingest_reliever_appearances.requests.get')
    def test_default_skips_already_ingested_games(self, mock_get):
        mock_get.side_effect = self._fixture_response
        self._seed_game_and_teams()
        call_command('ingest_reliever_appearances', '--gamepk=822780', '--sleep-ms=0')
        first_ingested_at = RelieverAppearance.objects.first().ingested_at
        # Second call without --refresh should skip.
        call_command('ingest_reliever_appearances', '--gamepk=822780', '--sleep-ms=0')
        # ingested_at unchanged → confirms skip.
        self.assertEqual(
            RelieverAppearance.objects.first().ingested_at,
            first_ingested_at,
        )


# ---------------------------------------------------------------------------
# 8. persist_snapshot writes an append-only TeamBullpenSnapshot row


class PersistSnapshotTests(TestCase):

    def test_persist_writes_row_with_built_values(self):
        team = _mk_team('ps1')
        pitcher = _mk_pitcher(team, 'RP')
        ref = timezone.now()
        g = _mk_game(team, _mk_team('ps1x'), ref - timedelta(hours=6))
        _mk_appearance(g, team, pitcher, outs=3, er=1, hits=2, bb=1, k=3)
        snap = persist_snapshot(team, ref)
        self.assertEqual(TeamBullpenSnapshot.objects.count(), 1)
        self.assertEqual(snap.team, team)
        self.assertEqual(snap.as_of, ref)
        # outs=3 → IP=1.0; 1 ER in 1 IP → ERA = 9.0
        self.assertEqual(snap.bullpen_era, 9.0)
        self.assertEqual(snap.source, 'mlb_stats_api')

    def test_backfill_command_is_idempotent(self):
        team = _mk_team('ps2')
        _mk_pitcher(team, 'RP')
        # Create a scheduled game so backfill finds it.
        future = timezone.now() + timedelta(hours=1)
        _mk_game(team, _mk_team('ps2x'), future,
                external_id='ps2-game')
        call_command(
            'backfill_bullpen_snapshots', '--today',
        )
        n_first = TeamBullpenSnapshot.objects.count()
        # Re-run without --refresh → same count.
        call_command(
            'backfill_bullpen_snapshots', '--today',
        )
        self.assertEqual(TeamBullpenSnapshot.objects.count(), n_first)
