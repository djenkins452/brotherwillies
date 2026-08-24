"""V3.2 forward-validation autonomous-capture tests.

Locks:
  * Canonical capture window (T-45min..T-75min).
  * Idempotence — one snapshot per (game, engine_version).
  * All decision classes captured (recommended/potential/not_recommended/no_signal).
  * Immutability — settlement fields update, decision fields never do.
  * Autonomous settlement — no user activity required.
  * Forward-health reads canonical population, NOT BettingRecommendation.
  * User bets never mix into the validation sample.
  * No historical backfill masquerading as forward observation.
  * Wilson CI + pre-registered threshold lock.
  * Production flags remain false.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import ForwardValidationSnapshot
from apps.analytics.services import v3_2_capture, v3_2_forward_health as fh
from apps.analytics.services import v3_2_settlement as sett
from apps.mlb.models import Conference, Game, Team


def _mk_conf():
    return Conference.objects.first() or Conference.objects.create(
        name='MLB', slug='mlb',
    )


def _mk_team(name, slug):
    return Team.objects.create(
        name=name, slug=slug, conference=_mk_conf(),
        source='mlb_stats_api', external_id=name,
    )


def _mk_game(home, away, first_pitch, *, home_score=None, away_score=None,
             status='scheduled'):
    return Game.objects.create(
        source='mlb_stats_api',
        external_id=f'g-{first_pitch.isoformat()}-{home.slug}-{away.slug}',
        home_team=home, away_team=away,
        first_pitch=first_pitch, status=status,
        home_score=home_score, away_score=away_score,
    )


class _FakeRec:
    """Minimal shape of get_recommendation's return."""
    def __init__(self, *, status='recommended', lane='core',
                 pick='Yankees', odds=-150, prob=0.65, edge=8.0,
                 tier='standard'):
        self.status = status
        self.status_reason = ''
        self.lane = lane
        self.pick = pick
        self.odds_american = odds
        self.raw_model_prob = prob
        self.final_model_prob = prob
        self.market_prob = prob - (edge / 100.0)
        self.model_edge = edge
        self.confidence_score = round(prob * 100, 2)
        self.tier = tier
        self.risk_flags = {}
        self.risk_score = 0
        self.is_secondary = False
        self.movement_class = None
        self.movement_score = None
        self.movement_supports_pick = False
        self.market_warning = False
        self.feature_contributions = {'engine_version': 'v3.2'}


class CanonicalWindowTests(TestCase):
    def test_in_window_is_captured(self):
        """Game with first_pitch 60min from now — captured."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        g = _mk_game(home, away, now + dt.timedelta(minutes=60))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(),
        ):
            result = v3_2_capture.capture_pending(now=now)
        self.assertEqual(result['captured'], 1)
        self.assertEqual(result['candidates_in_window'], 1)
        self.assertEqual(ForwardValidationSnapshot.objects.count(), 1)

    def test_too_early_not_captured(self):
        """Game first_pitch 180min out — outside window."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        _mk_game(home, away, now + dt.timedelta(minutes=180))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(),
        ) as mock:
            result = v3_2_capture.capture_pending(now=now)
        self.assertEqual(result['captured'], 0)
        self.assertEqual(result['candidates_in_window'], 0)
        # get_recommendation MUST NOT have been called for out-of-window games.
        mock.assert_not_called()

    def test_too_late_not_captured(self):
        """Game first_pitch 20min out — past canonical window."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        _mk_game(home, away, now + dt.timedelta(minutes=20))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(),
        ):
            result = v3_2_capture.capture_pending(now=now)
        self.assertEqual(result['captured'], 0)


class IdempotenceTests(TestCase):
    def test_repeated_capture_ticks_do_not_duplicate(self):
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        _mk_game(home, away, now + dt.timedelta(minutes=60))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(),
        ):
            v3_2_capture.capture_pending(now=now)
            v3_2_capture.capture_pending(now=now)
            r3 = v3_2_capture.capture_pending(now=now)
        self.assertEqual(ForwardValidationSnapshot.objects.count(), 1)
        # Third tick reports already_captured=1, captured=0.
        self.assertEqual(r3['captured'], 0)
        self.assertEqual(r3['already_captured'], 1)


class AllDecisionClassesCapturedTests(TestCase):
    def test_recommended_potential_not_recommended_and_no_signal_all_stored(self):
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        recs = [
            _FakeRec(status='recommended', lane='core'),                 # recommended
            _FakeRec(status='recommended', lane='qualified'),            # potential
            _FakeRec(status='not_recommended', lane='pass'),             # not_recommended
            None,                                                        # no_signal
        ]
        games = [
            _mk_game(home, away,
                     now + dt.timedelta(minutes=50 + 5 * i))
            for i in range(4)
        ]
        for game, rec in zip(games, recs):
            with patch(
                'apps.core.services.recommendations.get_recommendation',
                return_value=rec,
            ):
                v3_2_capture._capture_one(game, now=now)
        classes = sorted(
            ForwardValidationSnapshot.objects.values_list('decision_class', flat=True)
        )
        self.assertEqual(classes, ['no_signal', 'not_recommended',
                                    'potential', 'recommended'])


class ImmutabilityTests(TestCase):
    def test_settlement_updates_only_settlement_fields(self):
        """After settlement, decision-time fields (pick, edge, odds,
        etc.) must be BYTE-IDENTICAL to their captured values."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        g = _mk_game(home, away, now + dt.timedelta(minutes=60))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(pick='Yankees', odds=-150,
                                  prob=0.65, edge=8.0),
        ):
            v3_2_capture.capture_pending(now=now)
        snap = ForwardValidationSnapshot.objects.get()
        original = {
            'pick': snap.pick, 'pick_side': snap.pick_side,
            'odds_american': snap.odds_american,
            'edge_pp': snap.edge_pp,
            'final_model_prob': snap.final_model_prob,
            'feature_contributions': snap.feature_contributions,
            'captured_at': snap.captured_at,
            'minutes_to_first_pitch': snap.minutes_to_first_pitch,
        }
        # Game finishes — Yankees win.
        g.status = 'final'
        g.home_score = 5
        g.away_score = 3
        g.save()

        sett.settle_pending()
        snap.refresh_from_db()
        # Decision fields — unchanged.
        for k, v in original.items():
            self.assertEqual(getattr(snap, k), v,
                             f'immutable field {k} was mutated during settlement')
        # Settlement fields — populated.
        self.assertTrue(snap.won)
        self.assertIsNotNone(snap.settled_at)
        self.assertGreater(snap.profit_per_dollar, 0)


class AutonomousSettlementTests(TestCase):
    def test_settle_pending_runs_with_no_user_activity(self):
        """settle_pending must attach outcomes automatically — no
        BettingRecommendation, no MockBet, no request/user needed."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        g = _mk_game(home, away, now + dt.timedelta(minutes=60))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(pick='Yankees', odds=-110),
        ):
            v3_2_capture.capture_pending(now=now)
        g.status = 'final'; g.home_score = 4; g.away_score = 2; g.save()
        r = sett.settle_pending()
        self.assertEqual(r['settled'], 1)
        snap = ForwardValidationSnapshot.objects.get()
        self.assertTrue(snap.won)


class ForwardHealthReadsCanonicalPopulationTests(TestCase):
    def test_forward_health_query_uses_snapshot_not_recommendation(self):
        """Historic: report queried BettingRecommendation and returned
        generated=0 in production. Now must return snapshot counts."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        for i in range(3):
            g = _mk_game(home, away, now + dt.timedelta(minutes=60, seconds=i))
            with patch(
                'apps.core.services.recommendations.get_recommendation',
                return_value=_FakeRec(),
            ):
                v3_2_capture._capture_one(g, now=now)
        report = fh.compute_forward_health(days=30)
        self.assertEqual(report['population']['total_captured'], 3)
        self.assertEqual(report['population']['recommended'], 3)

    def test_forward_health_ignores_user_placed_bets(self):
        """User activity via place_mock_bet creates BettingRecommendation
        rows. The forward-health report MUST NOT count those — it reads
        ForwardValidationSnapshot only."""
        from apps.core.models import BettingRecommendation
        from decimal import Decimal
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        g = _mk_game(home, away, now + dt.timedelta(hours=6),
                     home_score=5, away_score=3, status='final')
        BettingRecommendation.objects.create(
            sport='mlb', mlb_game=g,
            bet_type='moneyline', pick='Yankees',
            odds_american=-150,
            confidence_score=Decimal('65.00'),
            model_edge=Decimal('8.00'),
            model_source='house', status='recommended', lane='core',
            final_model_prob=0.65, market_prob=0.57,
        )
        report = fh.compute_forward_health(days=30)
        # Zero ForwardValidationSnapshot rows even though a
        # BettingRecommendation exists.
        self.assertEqual(report['population']['total_captured'], 0)


class NoHistoricalBackfillTests(TestCase):
    def test_forward_validation_started_at_reflects_first_snapshot_only(self):
        """The started_at marker is derived from the FIRST captured_at
        (which is auto_now_add). We can never fake historical captures
        because ForwardValidationSnapshot.captured_at is not settable
        via .create()."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        now = timezone.now()
        g = _mk_game(home, away, now + dt.timedelta(minutes=60))
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(),
        ):
            v3_2_capture._capture_one(g, now=now)
        started = v3_2_capture.get_forward_validation_started_at()
        self.assertIsNotNone(started)
        # Roughly within a few seconds of "now" — never in the past.
        self.assertLess(abs((timezone.now() - started).total_seconds()), 60)


class WilsonCIAndThresholdLockTests(TestCase):
    def test_wilson_ci_known_case(self):
        lo, hi = fh._wilson_ci(20, 30)
        self.assertAlmostEqual(lo, 0.488, places=2)
        self.assertAlmostEqual(hi, 0.812, places=2)

    def test_locked_thresholds(self):
        self.assertEqual(fh.MIN_SETTLED_FOR_JUDGMENT, 30)
        self.assertEqual(fh.WATCH_WIN_RATE_DROP_PP, 4.0)
        self.assertEqual(fh.DEGRADED_WIN_RATE_DROP_PP, 8.0)
        self.assertEqual(fh.WATCH_ROI_DROP_PP, 5.0)
        self.assertEqual(fh.DEGRADED_ROI_DROP_PP, 12.0)
        self.assertEqual(fh.WATCH_CLV_DROP_PP, 5.0)
        self.assertEqual(fh.DEGRADED_CLV_DROP_PP, 12.0)


class ProductionFlagsFrozenTests(TestCase):
    def test_shadow_flags_default_false(self):
        from brotherwillies import settings as s
        self.assertFalse(getattr(s, 'USE_BULLPEN_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_BULLPEN_FATIGUE', False))
        self.assertFalse(getattr(s, 'USE_LINEUP_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_TEAM_OFFENSE', False))


class RendererSmokeTests(TestCase):
    def test_renderer_includes_capture_health_and_verdict(self):
        report = fh.compute_forward_health(days=30)
        text = fh.render_forward_health(report)
        self.assertIn('CAPTURE HEALTH', text)
        self.assertIn('POPULATION', text)
        self.assertIn('HEALTH VERDICT', text)
        self.assertIn('canonical window', text)


class VerdictOnEmptyPopulationTests(TestCase):
    def test_no_snapshots_and_no_eligible_games_yields_awaiting(self):
        """With no games at all, verdict is AWAITING_FIRST_CAPTURE
        (the corrected state — was INSUFFICIENT_DATA before the fix)."""
        report = fh.compute_forward_health(days=30)
        self.assertEqual(report['verdict']['verdict'], 'AWAITING_FIRST_CAPTURE')


class CaptureCommandChainTests(TestCase):
    def test_capture_command_is_registered(self):
        """capture_v3_2_validation management command must be
        discoverable — this is what refresh_data calls."""
        from django.core.management import get_commands
        self.assertIn('capture_v3_2_validation', get_commands())


class ActivationBoundaryTests(TestCase):
    """Locks the fix that stopped pre-activation history from
    triggering DATA_COLLECTION_DEGRADED."""

    def test_activation_returns_tz_aware_datetime(self):
        act = v3_2_capture.activation_at()
        self.assertIsNotNone(act)
        self.assertIsNotNone(act.tzinfo)

    def test_pre_activation_game_not_missed(self):
        """A game whose canonical window closed BEFORE activation must
        NOT appear as missed — it was never eligible for prospective
        capture."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        # Game 20 days ago — first_pitch far before any plausible
        # activation timestamp.
        fp = timezone.now() - dt.timedelta(days=20)
        _mk_game(home, away, fp, home_score=5, away_score=3, status='final')
        report = fh.compute_forward_health(days=30)
        ch = report['capture_health']
        # The game IS in the report window, but classified as pre-activation.
        self.assertEqual(ch['report_window_games'], 1)
        self.assertEqual(ch['pre_activation_excluded'], 1)
        self.assertEqual(ch['post_activation_eligible'], 0)
        self.assertEqual(ch['missed_eligible'], 0)
        # Coverage % is None when denominator is 0 — never fake it.
        self.assertIsNone(ch['capture_coverage_pct'])

    def test_verdict_awaiting_first_capture_not_degraded_on_pre_activation_only(self):
        """The exact production regression: 409 pre-activation games
        must not trigger DATA_COLLECTION_DEGRADED."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        for i in range(50):
            _mk_game(home, away,
                     timezone.now() - dt.timedelta(days=15 + i, hours=i),
                     home_score=5, away_score=3, status='final')
        report = fh.compute_forward_health(days=60)
        v = report['verdict']['verdict']
        self.assertEqual(v, 'AWAITING_FIRST_CAPTURE')
        self.assertNotEqual(v, 'DATA_COLLECTION_DEGRADED')

    def test_post_activation_uncaptured_game_is_missed(self):
        """A game whose canonical window opened AFTER activation and
        finished without a snapshot MUST count as missed."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        # Game 2h ago — its canonical window (T-75..T-45) was 45..75
        # min before first_pitch, i.e. 45..75 min ago, well after
        # any test activation.
        activation = v3_2_capture.activation_at()
        # Guard: only meaningful if activation is in the past.
        if activation > timezone.now():
            self.skipTest('activation is in the future; setup would '
                          'not create a post-activation game')
        fp = timezone.now() - dt.timedelta(minutes=30)
        _mk_game(home, away, fp,
                 home_score=5, away_score=3, status='final')
        report = fh.compute_forward_health(days=30)
        ch = report['capture_health']
        self.assertEqual(ch['post_activation_eligible'], 1)
        self.assertEqual(ch['missed_eligible'], 1)
        # And missed_captures details include this game with a
        # SCHEDULER_MISS classification (no CronRunLog exists).
        misses = ch['missed_captures']
        self.assertEqual(len(misses), 1)
        self.assertEqual(misses[0]['classification'], 'SCHEDULER_MISS')

    def test_canonical_window_unchanged(self):
        """Locks the 45..75 window against silent widening (the brief
        explicitly forbids widening the window to accommodate a weak
        scheduler)."""
        self.assertEqual(v3_2_capture.MIN_WINDOW_MIN, 45)
        self.assertEqual(v3_2_capture.MAX_WINDOW_MIN, 75)

    def test_no_backfill_via_report_computation(self):
        """Computing the forward-health report must NOT create any
        ForwardValidationSnapshot rows — the population is
        prospective-only, never derived from historical replay."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        for i in range(5):
            _mk_game(home, away,
                     timezone.now() - dt.timedelta(days=i + 1),
                     home_score=5, away_score=3, status='final')
        before = ForwardValidationSnapshot.objects.count()
        fh.compute_forward_health(days=30)
        after = ForwardValidationSnapshot.objects.count()
        self.assertEqual(before, after,
                         'Report computation must not manufacture snapshots')


class RefreshCadenceAuditTests(TestCase):
    def test_cadence_reports_none_when_no_runs(self):
        report = fh.compute_forward_health(days=30)
        cad = report['capture_health']['cadence']
        self.assertEqual(cad['run_count'], 0)
        self.assertFalse(cad['guarantees_capture'])

    def test_cadence_guarantees_when_intervals_below_window_width(self):
        """max interval < 30min → guarantees_capture=True."""
        from apps.ops.models import CronRunLog
        now = timezone.now()
        for i in range(5):
            r = CronRunLog.objects.create(
                command='refresh_data', trigger='cron',
                status='success',
            )
            # started_at is auto_now_add on this model; force it back.
            CronRunLog.objects.filter(id=r.id).update(
                started_at=now - dt.timedelta(minutes=i * 10),
            )
        report = fh.compute_forward_health(days=30)
        cad = report['capture_health']['cadence']
        self.assertEqual(cad['run_count'], 5)
        # intervals of 10min each < 30min canonical width
        self.assertTrue(cad['guarantees_capture'])

    def test_cadence_does_not_guarantee_when_max_gap_exceeds_window(self):
        from apps.ops.models import CronRunLog
        now = timezone.now()
        r1 = CronRunLog.objects.create(
            command='refresh_data', trigger='cron', status='success',
        )
        r2 = CronRunLog.objects.create(
            command='refresh_data', trigger='cron', status='success',
        )
        CronRunLog.objects.filter(id=r1.id).update(
            started_at=now - dt.timedelta(minutes=90),
        )
        CronRunLog.objects.filter(id=r2.id).update(
            started_at=now - dt.timedelta(minutes=30),
        )
        report = fh.compute_forward_health(days=30)
        cad = report['capture_health']['cadence']
        self.assertFalse(cad['guarantees_capture'])
        self.assertGreaterEqual(cad['max_interval_min'], 30)
