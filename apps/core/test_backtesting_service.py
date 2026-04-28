"""Tests for the backtesting service.

Coverage targets the nine validation requirements:
  1. Game-time data only — stored snapshot AND closing odds must precede start
  2. Approximate flag set whenever any game uses recompute fallback
  3. One recommendation per game — final pre-game snapshot, dedup defensive
  4. ROI = profit / total_stake (not win rate)
  5. Edge stored as decimal internally
  6. Calibration buckets include count, avg predicted, actual win rate
  7. CLV uses same market + side, includes positive CLV rate
  8. Aggregation is incremental (single-pass over evaluations)
  9. JSON shape is stable: every breakdown pre-populates known buckets
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import BacktestRun, ModelResultSnapshot
from apps.core.services.backtesting_service import (
    CALIBRATION_LABELS,
    EDGE_BUCKET_LABELS,
    FAV_DOG_LABELS,
    FLAT_STAKE,
    HOME_AWAY_LABELS,
    SPORT_LABELS,
    TIER_LABELS,
    GameEvaluation,
    _BacktestAggregator,
    _BucketAccumulator,
    _calibration_bucket,
    _edge_bucket,
    aggregate_results,
    evaluate_game,
    iter_evaluations,
    run_backtest,
)
from apps.mlb.models import Conference, Game, OddsSnapshot, Team


def _make_settled_mlb_game(
    home_rating=60,
    away_rating=40,
    home_score=5,
    away_score=3,
    moneyline_home=-150,
    moneyline_away=130,
    market_home_prob=0.60,
    capture_offset=timedelta(hours=2),
    first_pitch_offset=timedelta(days=-1),
    extra_snapshots=None,
    closing_source='odds_api',
):
    """Build a fully-set-up MLB game with an OddsSnapshot + final score.

    extra_snapshots: list of (timedelta_before_first_pitch, ml_home, ml_away) or
    (timedelta, ml_home, ml_away, source) tuples appended in addition to the
    default closing snapshot. Useful for CLV tests.

    closing_source: odds_source value for the default closing snapshot
    ('odds_api' / 'espn' / 'manual' / 'cached').
    """
    league = Conference.objects.create(
        name='AL East', slug=f'al-east-{timezone.now().timestamp()}'
    )
    home = Team.objects.create(
        name='Home Team',
        slug=f'home-{id(home_rating)}-{id(away_rating)}',
        conference=league, rating=home_rating,
    )
    away = Team.objects.create(
        name='Away Team',
        slug=f'away-{id(away_rating)}-{id(home_rating)}',
        conference=league, rating=away_rating,
    )
    first_pitch = timezone.now() + first_pitch_offset
    game = Game.objects.create(
        home_team=home, away_team=away,
        first_pitch=first_pitch, status='final',
        home_score=home_score, away_score=away_score,
    )
    OddsSnapshot.objects.create(
        game=game,
        captured_at=first_pitch - capture_offset,
        odds_source=closing_source,
        market_home_win_prob=market_home_prob,
        moneyline_home=moneyline_home,
        moneyline_away=moneyline_away,
    )
    for entry in (extra_snapshots or []):
        if len(entry) == 4:
            offset, mlh, mla, src = entry
        else:
            offset, mlh, mla = entry
            src = 'odds_api'
        OddsSnapshot.objects.create(
            game=game,
            captured_at=first_pitch - offset,
            odds_source=src,
            market_home_win_prob=market_home_prob,
            moneyline_home=mlh, moneyline_away=mla,
        )
    return game


# ---------------------------------------------------------------------------
# Edge bucket boundaries (decimal scale per requirement #5)

class EdgeBucketTests(TestCase):
    def test_zero_falls_in_zero_to_four(self):
        self.assertEqual(_edge_bucket(0.0), '0-4')
        self.assertEqual(_edge_bucket(0.0399), '0-4')

    def test_four_pct_starts_four_to_six(self):
        self.assertEqual(_edge_bucket(0.04), '4-6')

    def test_six_pct_starts_six_to_eight(self):
        self.assertEqual(_edge_bucket(0.06), '6-8')

    def test_eight_pct_starts_eight_plus(self):
        self.assertEqual(_edge_bucket(0.08), '8+')
        self.assertEqual(_edge_bucket(0.5), '8+')

    def test_negative_edge_falls_into_zero_to_four(self):
        self.assertEqual(_edge_bucket(-0.02), '0-4')


class CalibrationBucketTests(TestCase):
    """Decimal labels (0.5-0.55) + None below 0.5."""

    def test_below_50_returns_none(self):
        self.assertIsNone(_calibration_bucket(0.40))

    def test_lower_inclusive_upper_exclusive(self):
        self.assertEqual(_calibration_bucket(0.50), '0.5-0.55')
        self.assertEqual(_calibration_bucket(0.549), '0.5-0.55')
        self.assertEqual(_calibration_bucket(0.55), '0.55-0.6')

    def test_high_end(self):
        self.assertEqual(_calibration_bucket(0.95), '0.95-1.0')
        self.assertEqual(_calibration_bucket(0.999), '0.95-1.0')
        self.assertEqual(_calibration_bucket(1.0), '0.95-1.0')


# ---------------------------------------------------------------------------
# Bucket accumulator — ROI math (requirement #4)

class BucketAccumulatorTests(TestCase):
    def _ev(self, won=True, edge=0.05, predicted=0.6, closing_odds=120, clv=None):
        return GameEvaluation(
            sport='mlb', game_id=str(id(self)) + str(closing_odds),
            game_label='X', game_time=timezone.now(),
            predicted_home_prob=predicted, market_home_prob_fair=0.5,
            pick_is_home=True, pick_predicted_prob=predicted,
            pick_market_prob_fair=0.5,
            pick_opening_odds_american=closing_odds,
            pick_closing_odds_american=closing_odds,
            edge=edge, status='recommended', status_reason='', tier='strong',
            won=won, clv_decimal=clv, is_approximate=False,
            is_favorite=closing_odds < 0, is_home_pick=True,
        )

    def test_roi_is_profit_over_stake_not_win_rate(self):
        # +120 win on $100 stake: payout 220, net +120, ROI = 1.20
        acc = _BucketAccumulator()
        acc.add(self._ev(won=True, closing_odds=120))
        d = acc.to_dict()
        self.assertAlmostEqual(d['roi_pct'], 1.20, places=2)
        # Win rate is reported separately and is NOT roi
        self.assertAlmostEqual(d['win_rate'], 1.0, places=2)
        self.assertNotEqual(d['win_rate'], d['roi_pct'])

    def test_minus_money_loss_roi_minus_one(self):
        acc = _BucketAccumulator()
        acc.add(self._ev(won=False, closing_odds=-150))
        self.assertAlmostEqual(acc.to_dict()['roi_pct'], -1.0, places=2)

    def test_break_even_at_plus_money(self):
        acc = _BucketAccumulator()
        acc.add(self._ev(won=True, closing_odds=100))
        acc.add(self._ev(won=False, closing_odds=100))
        d = acc.to_dict()
        self.assertAlmostEqual(d['roi_pct'], 0.0, places=2)
        self.assertAlmostEqual(d['win_rate'], 0.5, places=2)

    def test_clv_aggregation_includes_positive_rate(self):
        # Requirement #7: same market (moneyline) + positive CLV rate
        acc = _BucketAccumulator()
        acc.add(self._ev(clv=0.05))    # positive
        acc.add(self._ev(clv=-0.02))   # negative
        acc.add(self._ev(clv=None))    # excluded
        d = acc.to_dict()
        self.assertEqual(d['clv_sample'], 2)
        self.assertAlmostEqual(d['avg_clv'], 0.015, places=4)
        self.assertAlmostEqual(d['positive_clv_rate'], 0.5, places=4)

    def test_empty_accumulator_returns_none_metrics(self):
        d = _BucketAccumulator().to_dict()
        self.assertEqual(d['sample'], 0)
        self.assertIsNone(d['win_rate'])
        self.assertIsNone(d['roi_pct'])
        self.assertIsNone(d['avg_edge'])
        self.assertEqual(d['clv_sample'], 0)

    def test_edge_stored_as_decimal_in_aggregate(self):
        # Requirement #5: edge as decimal (0.06 = 6%)
        acc = _BucketAccumulator()
        acc.add(self._ev(edge=0.06))
        acc.add(self._ev(edge=0.10))
        d = acc.to_dict()
        self.assertAlmostEqual(d['avg_edge'], 0.08, places=4)


# ---------------------------------------------------------------------------
# Per-game evaluation — game-time data only (requirement #1, #3)

class EvaluateGameTests(TestCase):
    def test_stored_snapshot_after_game_is_ignored(self):
        # Requirement #1: game-time data only. A snapshot captured AFTER
        # first_pitch must not be used as the prediction source.
        game = _make_settled_mlb_game()

        # Post-game snapshot — should be ignored.
        post_game_snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.99,
        )
        # Force its captured_at to AFTER the game.
        post_game_snap.captured_at = game.first_pitch + timedelta(hours=3)
        post_game_snap.save()

        ev = evaluate_game('mlb', game)
        # Without a valid pre-game snapshot we fall back to recompute,
        # which marks approximate. Critically we did NOT use the 0.99 leak.
        self.assertIsNotNone(ev)
        self.assertTrue(ev.is_approximate)

    def test_pre_game_stored_snapshot_used_when_present(self):
        game = _make_settled_mlb_game(market_home_prob=0.50)
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.65,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=3)
        snap.save()
        ev = evaluate_game('mlb', game)
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_approximate)
        # Predicted home prob came from the stored snapshot (0.65).
        self.assertAlmostEqual(ev.predicted_home_prob, 0.65, places=4)

    def test_only_final_pre_game_snapshot_used(self):
        # Requirement #3: when multiple stored snapshots exist before
        # kickoff, the LATEST is used (the final pre-game prediction).
        game = _make_settled_mlb_game()
        early = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.55,
        )
        early.captured_at = game.first_pitch - timedelta(hours=24)
        early.save()
        late = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.70,
        )
        late.captured_at = game.first_pitch - timedelta(hours=1)
        late.save()
        ev = evaluate_game('mlb', game)
        self.assertAlmostEqual(ev.predicted_home_prob, 0.70, places=4)

    def test_picks_home_when_house_above_market(self):
        game = _make_settled_mlb_game(
            market_home_prob=0.50,
            moneyline_home=-110, moneyline_away=-110,
        )
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.60,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=2)
        snap.save()
        ev = evaluate_game('mlb', game)
        self.assertTrue(ev.pick_is_home)
        self.assertGreater(ev.edge, 0.05)

    def test_won_correctly_classified(self):
        game = _make_settled_mlb_game(home_score=10, away_score=2)
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.65,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=2)
        snap.save()
        ev = evaluate_game('mlb', game)
        self.assertTrue(ev.pick_is_home)
        self.assertTrue(ev.won)

    def test_skip_when_no_pre_game_odds(self):
        # Game with NO snapshots at all → unevaluable.
        league = Conference.objects.create(name='X', slug=f'x-{timezone.now().timestamp()}')
        h = Team.objects.create(name='H', slug=f'hh-{id(self)}', conference=league)
        a = Team.objects.create(name='A', slug=f'aa-{id(self)}', conference=league)
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() - timedelta(days=1),
            status='final', home_score=1, away_score=0,
        )
        self.assertIsNone(evaluate_game('mlb', g))

    def test_skip_when_closing_snapshot_missing_moneyline(self):
        game = _make_settled_mlb_game(moneyline_home=None, moneyline_away=None)
        self.assertIsNone(evaluate_game('mlb', game))

    def test_clv_uses_opening_to_closing_for_picked_side(self):
        # Requirement #7: same market (moneyline), same side.
        # Picked side = home (with house_prob 0.65 vs market 0.50).
        # Opening home ML = -110, closing home ML = -130
        # CLV = bet_dec - close_dec = decimal(-110) - decimal(-130)
        #     = 1.9091 - 1.7692 ≈ +0.1399 → positive
        # Both snapshots default to source='odds_api' so guard passes.
        game = _make_settled_mlb_game(
            market_home_prob=0.50,
            moneyline_home=-130, moneyline_away=110,
            extra_snapshots=[
                (timedelta(hours=10), -110, 100),  # opening — older
            ],
        )
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.65,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=2)
        snap.save()
        ev = evaluate_game('mlb', game)
        self.assertTrue(ev.pick_is_home)
        self.assertEqual(ev.pick_opening_odds_american, -110)
        self.assertEqual(ev.pick_closing_odds_american, -130)
        self.assertIsNotNone(ev.clv_decimal)
        self.assertGreater(ev.clv_decimal, 0)

    def test_clv_none_when_only_one_snapshot(self):
        # Requirement #7 corollary: no movement observable → exclude from CLV.
        game = _make_settled_mlb_game()
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.65,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=2)
        snap.save()
        ev = evaluate_game('mlb', game)
        self.assertIsNone(ev.clv_decimal)


# ---------------------------------------------------------------------------
# Aggregation — incremental + JSON shape (requirements #6, #8, #9)

class IncrementalAggregationTests(TestCase):
    """Aggregator processes evaluations one-at-a-time without holding a list."""

    def _ev(self, edge=0.05, won=True):
        return GameEvaluation(
            sport='mlb', game_id=f'g{id(edge) + id(won)}',
            game_label='X', game_time=timezone.now(),
            predicted_home_prob=0.6, market_home_prob_fair=0.5,
            pick_is_home=True, pick_predicted_prob=0.6,
            pick_market_prob_fair=0.5,
            pick_opening_odds_american=110, pick_closing_odds_american=110,
            edge=edge, status='recommended', status_reason='', tier='strong',
            won=won, clv_decimal=None, is_approximate=False,
            is_favorite=False, is_home_pick=True,
        )

    def test_aggregator_works_as_generator_consumer(self):
        # Requirement #8: incremental, not full in-memory list.
        agg = _BacktestAggregator()
        def gen():
            for i in range(3):
                yield self._ev(edge=0.05 + i * 0.01)
        for ev in gen():
            agg.add(ev)
        summary = agg.to_summary()
        self.assertEqual(summary['overall']['sample'], 3)

    def test_aggregator_dedupes_by_game_id(self):
        # Requirement #3: defensive — same game can't double-count.
        agg = _BacktestAggregator()
        ev = self._ev()
        agg.add(ev)
        agg.add(ev)
        s = agg.to_summary()
        self.assertEqual(s['overall']['sample'], 1)
        self.assertEqual(s['validation']['duplicates_dropped'], 1)


class StableJSONShapeTests(TestCase):
    """Every breakdown dict pre-populates all known labels (requirement #9)."""

    def test_empty_run_has_all_buckets(self):
        run = run_backtest(sport='mlb', persist=False)
        s = run.summary
        # Every defined sport must appear, even though MLB-only filter
        # means only mlb has data.
        for sport in SPORT_LABELS:
            self.assertIn(sport, s['by_sport'])
            self.assertIn(sport, s['by_sport_recommended_only'])
        for label in EDGE_BUCKET_LABELS:
            self.assertIn(label, s['by_edge_bucket'])
        for label in TIER_LABELS:
            self.assertIn(label, s['by_tier'])
        for label in FAV_DOG_LABELS:
            self.assertIn(label, s['by_favorite_underdog'])
        for label in HOME_AWAY_LABELS:
            self.assertIn(label, s['by_home_away'])
        for label in CALIBRATION_LABELS:
            self.assertIn(label, s['calibration_curve'])

    def test_empty_buckets_have_zero_metrics(self):
        run = run_backtest(sport='mlb', persist=False)
        for label, metrics in run.summary['by_sport'].items():
            self.assertEqual(metrics['sample'], 0)
            self.assertIsNone(metrics['win_rate'])
            self.assertIsNone(metrics['roi_pct'])

    def test_calibration_buckets_use_strict_slim_shape(self):
        """Calibration buckets emit ONLY {count, predicted, actual} — no extras.

        Required by the JSON-shape contract: ROI/CLV/edge fields don't make
        sense per-probability-bucket, and consumers building calibration
        plots should see a uniform shape.
        """
        agg = _BacktestAggregator()
        ev = GameEvaluation(
            sport='mlb', game_id='g1', game_label='X', game_time=timezone.now(),
            predicted_home_prob=0.62, market_home_prob_fair=0.5,
            pick_is_home=True, pick_predicted_prob=0.62,
            pick_market_prob_fair=0.5,
            pick_opening_odds_american=120, pick_closing_odds_american=120,
            edge=0.12, status='recommended', status_reason='', tier='elite',
            won=True, clv_decimal=None, is_approximate=False,
            is_favorite=False, is_home_pick=True,
        )
        agg.add(ev)
        s = agg.to_summary()
        bucket = s['calibration_curve']['0.6-0.65']
        self.assertEqual(set(bucket.keys()), {'count', 'predicted', 'actual'})
        self.assertEqual(bucket['count'], 1)
        self.assertAlmostEqual(bucket['predicted'], 0.62, places=4)
        self.assertAlmostEqual(bucket['actual'], 1.0, places=4)

    def test_empty_calibration_buckets_zero_filled_not_null(self):
        """Empty calibration buckets are zero-filled (count=0, predicted=0.0, actual=0.0).

        Distinct from the rich-bucket dicts which use null for empty avgs —
        the calibration shape is stricter to keep downstream JSON consumers
        (e.g. chart libraries) from having to handle nulls.
        """
        run = run_backtest(sport='mlb', persist=False)
        for label in CALIBRATION_LABELS:
            self.assertIn(label, run.summary['calibration_curve'])
            bucket = run.summary['calibration_curve'][label]
            self.assertEqual(bucket, {'count': 0, 'predicted': 0.0, 'actual': 0.0})

    def test_calibration_labels_use_decimal_format(self):
        """Labels are decimal-formatted ('0.5-0.55'), not integer-percent ('50-55')."""
        run = run_backtest(sport='mlb', persist=False)
        labels = list(run.summary['calibration_curve'].keys())
        self.assertIn('0.5-0.55', labels)
        self.assertIn('0.55-0.6', labels)
        self.assertIn('0.95-1.0', labels)
        self.assertNotIn('50-55', labels)


class CLVSourceGuardTests(TestCase):
    """Requirement: CLV is only computed when BOTH snapshots are odds_api-sourced.

    ESPN-embedded odds and seeded/derived rows lack the line-movement signal
    CLV requires; using them would produce misleading results, so they're
    excluded entirely (closing_clv = None).
    """

    def _setup_with_two_snapshots(self, *, closing_source, opening_source):
        game = _make_settled_mlb_game(
            market_home_prob=0.50,
            moneyline_home=-130, moneyline_away=110,
            closing_source=closing_source,
            extra_snapshots=[
                (timedelta(hours=10), -110, 100, opening_source),
            ],
        )
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.65,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=2)
        snap.save()
        return game

    def test_clv_calculated_when_both_sources_are_odds_api(self):
        game = self._setup_with_two_snapshots(
            closing_source='odds_api', opening_source='odds_api',
        )
        ev = evaluate_game('mlb', game)
        self.assertIsNotNone(ev.clv_decimal)
        self.assertGreater(ev.clv_decimal, 0)

    def test_clv_none_when_closing_source_is_espn(self):
        game = self._setup_with_two_snapshots(
            closing_source='espn', opening_source='odds_api',
        )
        ev = evaluate_game('mlb', game)
        self.assertIsNone(ev.clv_decimal)

    def test_clv_none_when_opening_source_is_espn(self):
        game = self._setup_with_two_snapshots(
            closing_source='odds_api', opening_source='espn',
        )
        ev = evaluate_game('mlb', game)
        self.assertIsNone(ev.clv_decimal)

    def test_clv_none_when_either_source_is_cached(self):
        # 'cached' is one of the existing odds_source choices alongside
        # odds_api/espn/manual. Cached entries lack live movement signal.
        game = self._setup_with_two_snapshots(
            closing_source='odds_api', opening_source='cached',
        )
        ev = evaluate_game('mlb', game)
        self.assertIsNone(ev.clv_decimal)

    def test_clv_none_when_either_source_is_manual(self):
        game = self._setup_with_two_snapshots(
            closing_source='odds_api', opening_source='manual',
        )
        ev = evaluate_game('mlb', game)
        self.assertIsNone(ev.clv_decimal)


# ---------------------------------------------------------------------------
# End-to-end run

class RunBacktestTests(TestCase):
    def test_empty_run_persists_with_stable_shape(self):
        run = run_backtest(sport='mlb')
        self.assertIsInstance(run, BacktestRun)
        self.assertEqual(run.games_evaluated, 0)
        self.assertFalse(run.is_approximate)
        # Stable shape regardless of data.
        self.assertIn('overall', run.summary)
        self.assertIn('by_sport', run.summary)
        self.assertIn('calibration_curve', run.summary)

    def test_settled_game_with_pre_game_snapshot_is_not_approximate(self):
        game = _make_settled_mlb_game()
        snap = ModelResultSnapshot.objects.create(
            mlb_game=game, market_prob=0.50, house_prob=0.70,
        )
        snap.captured_at = game.first_pitch - timedelta(hours=2)
        snap.save()
        run = run_backtest(sport='mlb')
        self.assertEqual(run.games_evaluated, 1)
        self.assertFalse(run.is_approximate)

    def test_recompute_path_marks_approximate(self):
        # Requirement #2: any recompute → approximate.
        _make_settled_mlb_game()
        run = run_backtest(sport='mlb')
        self.assertEqual(run.games_evaluated, 1)
        self.assertTrue(run.is_approximate)
        self.assertIn('APPROXIMATE', run.notes)

    def test_iter_evaluations_streams(self):
        _make_settled_mlb_game()
        evs = list(iter_evaluations('mlb'))
        self.assertEqual(len(evs), 1)
