"""V3.2 forward-validation health service tests.

Locks:
  * Wilson CI math on a hand-computed case.
  * SYSTEM-only filter (excludes model_source='user' bets).
  * Pick-side outcome computation.
  * Cohort bucket boundaries.
  * Verdict rules on synthetic scenarios (INSUFFICIENT_DATA / HEALTHY /
    WATCH / DEGRADED).
  * Renderer produces expected top-level headers.
  * Pre-registered thresholds have not drifted from their v3.2 lock
    values — any change requires a documented evidence-driven decision.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.analytics.services import v3_2_forward_health as fh
from apps.core.models import BettingRecommendation
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


def _mk_rec(mlb_game, *, pick, odds, model_source='house',
            status='recommended', lane='core',
            final_model_prob=0.65, model_edge=8.0,
            created_at=None):
    r = BettingRecommendation.objects.create(
        sport='mlb', mlb_game=mlb_game,
        bet_type='moneyline', pick=pick,
        odds_american=int(odds),
        confidence_score=Decimal(str(round(final_model_prob * 100, 2))),
        model_edge=Decimal(str(model_edge)),
        model_source=model_source,
        status=status, lane=lane,
        raw_model_prob=final_model_prob, final_model_prob=final_model_prob,
        market_prob=final_model_prob - (model_edge / 100.0),
        feature_contributions={'engine_version': 'v3.2'},
    )
    if created_at is not None:
        BettingRecommendation.objects.filter(id=r.id).update(created_at=created_at)
    r.refresh_from_db()
    return r


class WilsonCITests(TestCase):
    def test_wilson_ci_known_case(self):
        """20 wins in 30 trials → Wilson95 ≈ [.488, .812] (formula-verified)."""
        lo, hi = fh._wilson_ci(20, 30)
        self.assertAlmostEqual(lo, 0.488, places=2)
        self.assertAlmostEqual(hi, 0.812, places=2)

    def test_wilson_ci_zero_n(self):
        self.assertEqual(fh._wilson_ci(0, 0), (0.0, 0.0))


class OutcomeComputationTests(TestCase):
    def test_home_pick_wins_when_home_scores_more(self):
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        game = _mk_game(home, away, timezone.now() - dt.timedelta(days=2),
                        home_score=5, away_score=3, status='final')
        rec = _mk_rec(game, pick='Yankees', odds=-150)
        self.assertTrue(fh._rec_outcome(rec))

    def test_away_pick_wins_when_away_scores_more(self):
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        game = _mk_game(home, away, timezone.now() - dt.timedelta(days=2),
                        home_score=1, away_score=4, status='final')
        rec = _mk_rec(game, pick='Red Sox', odds=+130)
        self.assertTrue(fh._rec_outcome(rec))

    def test_unfinished_game_returns_none(self):
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        game = _mk_game(home, away, timezone.now() + dt.timedelta(hours=2))
        rec = _mk_rec(game, pick='Yankees', odds=-150)
        self.assertIsNone(fh._rec_outcome(rec))


class SystemOnlyFilterTests(TestCase):
    def test_user_model_source_recs_excluded(self):
        """User-tuned model recommendations must NOT count in the
        forward-health sample — those judge the user, not the model."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        game = _mk_game(home, away, timezone.now() - dt.timedelta(days=3),
                        home_score=5, away_score=3, status='final')
        # System rec — should count.
        _mk_rec(game, pick='Yankees', odds=-150, model_source='house')
        # User rec on same game — must NOT count.
        _mk_rec(game, pick='Yankees', odds=-150, model_source='user')
        report = fh.compute_forward_health(days=30)
        self.assertEqual(report['population']['generated'], 1)


class VerdictThresholdTests(TestCase):
    def test_insufficient_data_verdict_below_min_settled(self):
        """<30 settled → INSUFFICIENT_DATA regardless of rate."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        for i in range(5):
            game = _mk_game(
                home, away,
                timezone.now() - dt.timedelta(days=i + 1),
                home_score=5, away_score=3, status='final',
            )
            _mk_rec(game, pick='Yankees', odds=-150)
        report = fh.compute_forward_health(days=30)
        self.assertEqual(report['verdict']['verdict'], 'INSUFFICIENT_DATA')

    def test_healthy_verdict_at_baseline(self):
        """At baseline win rate (71.5%) and baseline ROI, verdict = HEALTHY."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        # 30 games total: 22 wins at -150 (win rate ~73.3%).
        for i in range(22):
            game = _mk_game(
                home, away,
                timezone.now() - dt.timedelta(days=i + 1, hours=i),
                home_score=5, away_score=3, status='final',
            )
            _mk_rec(game, pick='Yankees', odds=-150)
        for i in range(8):
            game = _mk_game(
                home, away,
                timezone.now() - dt.timedelta(days=i + 25, hours=i),
                home_score=2, away_score=6, status='final',
            )
            _mk_rec(game, pick='Yankees', odds=-150)
        report = fh.compute_forward_health(days=180)
        agg = report['aggregate']
        self.assertGreaterEqual(agg['win_rate_pp'], 60.0)
        # Should be HEALTHY (or WARN on CLV which has no closing snap
        # samples in tests).
        v = report['verdict']['verdict']
        self.assertIn(v, ('HEALTHY', 'WATCH'))

    def test_degraded_verdict_on_bad_win_rate(self):
        """30 settled, win rate 50% (far below baseline). Wilson lower
        bound drops well past DEGRADED_WIN_RATE_LOWER_BOUND_DROP_PP."""
        home = _mk_team('Yankees', 'nyy')
        away = _mk_team('Red Sox', 'bos')
        for i in range(15):
            g = _mk_game(home, away,
                         timezone.now() - dt.timedelta(days=i + 1, hours=i),
                         home_score=5, away_score=3, status='final')
            _mk_rec(g, pick='Yankees', odds=-150)
        for i in range(15):
            g = _mk_game(home, away,
                         timezone.now() - dt.timedelta(days=i + 20, hours=i),
                         home_score=2, away_score=5, status='final')
            _mk_rec(g, pick='Yankees', odds=-150)
        report = fh.compute_forward_health(days=180)
        self.assertEqual(report['verdict']['verdict'], 'DEGRADED')


class CohortBucketingTests(TestCase):
    def test_probability_bucket_boundaries(self):
        self.assertEqual(
            fh._bucket_for_value(0.60, fh.PROB_BUCKETS), '60-65',
        )
        self.assertEqual(
            fh._bucket_for_value(0.65, fh.PROB_BUCKETS), '65-70',
        )
        self.assertEqual(
            fh._bucket_for_value(0.999, fh.PROB_BUCKETS), '75+',
        )
        self.assertIsNone(fh._bucket_for_value(0.55, fh.PROB_BUCKETS))

    def test_edge_bucket_boundaries(self):
        self.assertEqual(fh._bucket_for_value(6.0, fh.EDGE_BUCKETS), '6-8')
        self.assertEqual(fh._bucket_for_value(9.99, fh.EDGE_BUCKETS), '8-10')
        self.assertEqual(fh._bucket_for_value(15.0, fh.EDGE_BUCKETS), '10+')


class PreRegisteredThresholdsLockTests(TestCase):
    """These thresholds were pre-registered when V3.2 was activated.
    Any change to them requires a documented evidence-driven decision
    (not a silent tuning). Test forces a code-review moment for anyone
    updating them."""
    def test_locked_values(self):
        self.assertEqual(fh.MIN_SETTLED_FOR_JUDGMENT, 30)
        self.assertEqual(fh.WATCH_WIN_RATE_DROP_PP, 4.0)
        self.assertEqual(fh.DEGRADED_WIN_RATE_DROP_PP, 8.0)
        self.assertEqual(fh.WATCH_ROI_DROP_PP, 5.0)
        self.assertEqual(fh.DEGRADED_ROI_DROP_PP, 12.0)
        self.assertEqual(fh.WATCH_CLV_DROP_PP, 5.0)
        self.assertEqual(fh.DEGRADED_CLV_DROP_PP, 12.0)
        self.assertEqual(fh.REPLAY_BASELINE_WIN_RATE, 71.5)
        self.assertEqual(fh.REPLAY_BASELINE_ROI, 21.0)
        self.assertEqual(fh.REPLAY_BASELINE_CLV_POS, 55.0)


class RendererSmokeTests(TestCase):
    def test_renderer_produces_expected_headers(self):
        report = fh.compute_forward_health(days=30)
        text = fh.render_forward_health(report)
        self.assertIn('V3.2 FORWARD-VALIDATION HEALTH', text)
        self.assertIn('POPULATION', text)
        self.assertIn('AGGREGATE', text)
        self.assertIn('COHORTS', text)
        self.assertIn('CALIBRATION', text)
        self.assertIn('HEALTH VERDICT', text)


class ProductionFlagsFrozenTests(TestCase):
    """No forward-health surface should ever accidentally activate a
    scoring feature. Lock the flags here so a future refactor can't
    silently drift."""
    def test_all_shadow_flags_default_false(self):
        from brotherwillies import settings as s
        self.assertFalse(getattr(s, 'USE_BULLPEN_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_BULLPEN_FATIGUE', False))
        self.assertFalse(getattr(s, 'USE_LINEUP_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_TEAM_OFFENSE', False))
