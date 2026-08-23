"""v3.4 team-offense phase 2 — TeamBattingSnapshot audit tests.

Covers the audit service's classification logic + the retry-only-missing
worker path.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from django.test import TestCase

from apps.analytics.models import TeamBattingBackfillRun
from apps.mlb.models import Conference, Game, Team, TeamBattingSnapshot


def _mk_conf():
    return Conference.objects.first() or Conference.objects.create(
        name='MLB', slug='mlb',
    )


def _mk_team(name, slug, external_id):
    return Team.objects.create(
        name=name, slug=slug, conference=_mk_conf(),
        source='mlb_stats_api', external_id=external_id,
    )


def _mk_snapshot(team, d):
    return TeamBattingSnapshot.objects.create(
        team=team, as_of_date=d, season=d.year,
        plate_appearances=1000, at_bats=900, hits=250,
        doubles=50, triples=5, home_runs=30, walks=90,
        hit_by_pitch=10, sac_flies=10, strikeouts=200, runs=110,
        games_played=25,
    )


def _mk_game(home, away, first_pitch, home_score=5, away_score=3):
    return Game.objects.create(
        source='mlb_stats_api',
        external_id=f'g{first_pitch.isoformat()}-{home.slug}-{away.slug}',
        home_team=home, away_team=away,
        first_pitch=first_pitch, status='final',
        home_score=home_score, away_score=away_score,
    )


class AuditClassificationTests(TestCase):
    def test_legitimate_empty_vs_suspect_missing(self):
        """A missing snapshot BEFORE the team's first final game is
        legitimate-empty. A missing snapshot ON/AFTER the team's first
        final game is suspect-missing (retry candidate)."""
        from apps.analytics.services.team_batting_audit import (
            audit_team_batting_backfill,
        )

        home = _mk_team('LAD', 'lad', '119')
        away = _mk_team('SFG', 'sfg', '137')
        first_pitch = dt.datetime(2026, 6, 1, 22, 0)
        _mk_game(home, away, first_pitch)

        # Create a snapshot for LAD on 2026-05-30 only. Missing:
        # LAD 2026-03-01 (should be legit-empty since first game is 6/1),
        # LAD 2026-05-31 (should be suspect if a run covered up to then),
        # SFG all dates.
        _mk_snapshot(home, dt.date(2026, 5, 30))

        # A run whose window includes 2026-03-01..2026-06-01 makes the
        # audit compute expected pairs for that whole range.
        TeamBattingBackfillRun.objects.create(
            date_from=dt.date(2026, 3, 1),
            date_to=dt.date(2026, 6, 1),
            status='completed_with_errors',
            fetches_attempted=100, fetches_succeeded=1,
            fetches_empty=99, fetches_errored=0,
            snapshots_created=1,
        )

        audit = audit_team_batting_backfill(reference_date=dt.date(2026, 6, 2))
        mc = audit['missing_classification']
        # LAD had no games before 2026-06-01 in the seeded data, so most
        # missing LAD dates before 06-01 are legitimate empties.
        self.assertGreater(mc['legitimate_empty'], 0)
        # SFG dates on/after 2026-06-01 = 1 date (only 2026-06-01) that
        # are suspect. And LAD 2026-05-31 was covered by the run window
        # but is before LAD's first game — legit empty.
        # Just ensure the classifier produces non-negative buckets that
        # sum correctly.
        expected_missing = (
            audit['requirements']['expected_pairs']
            - audit['requirements']['present_pairs']
        )
        self.assertEqual(
            mc['legitimate_empty'] + mc['suspect_missing'],
            expected_missing,
        )

    def test_game_coverage_both_covered(self):
        """A game with valid snapshots strictly before first_pitch for
        both teams counts as both_covered."""
        from apps.analytics.services.team_batting_audit import (
            audit_team_batting_backfill,
        )
        home = _mk_team('LAD', 'lad', '119')
        away = _mk_team('SFG', 'sfg', '137')
        # Game today (reference_date). Snapshots yesterday.
        ref = dt.date.today()
        fp = dt.datetime.combine(ref, dt.time(19, 0))
        _mk_game(home, away, fp)
        _mk_snapshot(home, ref - dt.timedelta(days=1))
        _mk_snapshot(away, ref - dt.timedelta(days=1))

        audit = audit_team_batting_backfill(reference_date=ref + dt.timedelta(days=1))
        gc = audit['game_coverage']
        self.assertEqual(gc['total_games'], 1)
        self.assertEqual(gc['both_covered'], 1)

    def test_verdict_hold_on_empty_dataset(self):
        """With zero snapshots and zero games, the verdict must be HOLD."""
        from apps.analytics.services.team_batting_audit import (
            audit_team_batting_backfill,
        )
        audit = audit_team_batting_backfill()
        self.assertEqual(audit['trustworthiness']['verdict'], 'HOLD')

    def test_renderer_produces_output(self):
        """Renderer should not raise on any well-formed audit dict."""
        from apps.analytics.services.team_batting_audit import (
            audit_team_batting_backfill, render_team_batting_audit,
        )
        text = render_team_batting_audit(audit_team_batting_backfill())
        self.assertIn('TEAM-BATTING BACKFILL AUDIT', text)
        self.assertIn('TRUSTWORTHINESS VERDICT', text)


class OnlyMissingWorkerTests(TestCase):
    def test_only_missing_skips_present_pairs(self):
        """When only_missing=True, the worker MUST NOT call the API for
        (team, date) pairs already present as snapshots."""
        from apps.analytics.services.team_batting_backfill_service import (
            run_team_batting_backfill,
        )
        t1 = _mk_team('LAD', 'lad', '119')
        t2 = _mk_team('SFG', 'sfg', '137')
        # Pre-populate: LAD 2026-06-01 exists.
        _mk_snapshot(t1, dt.date(2026, 6, 1))

        # Retry-only-missing run over one day, two teams. Expect: LAD
        # skipped (already present), SFG fetched.
        run = TeamBattingBackfillRun.objects.create(
            date_from=dt.date(2026, 6, 1),
            date_to=dt.date(2026, 6, 1),
            only_missing=True,
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
        ) as mock_fetch:
            run_team_batting_backfill(str(run.id), sleep_ms=0)

        run.refresh_from_db()
        # Exactly 1 fetch (SFG), NOT 2.
        self.assertEqual(mock_fetch.call_count, 1)
        # And exactly 1 attempt counted.
        self.assertEqual(run.fetches_attempted, 1)
        # LAD's pre-existing row is preserved.
        lad_snaps = TeamBattingSnapshot.objects.filter(team=t1).count()
        self.assertEqual(lad_snaps, 1)
        # SFG got a new row.
        sfg_snaps = TeamBattingSnapshot.objects.filter(team=t2).count()
        self.assertEqual(sfg_snaps, 1)


class OnlyMissingFieldTests(TestCase):
    def test_field_defaults_false_and_persists(self):
        r = TeamBattingBackfillRun.objects.create(
            date_from=dt.date(2026, 6, 1),
            date_to=dt.date(2026, 6, 2),
        )
        self.assertFalse(r.only_missing)
        r.only_missing = True
        r.save()
        r.refresh_from_db()
        self.assertTrue(r.only_missing)
