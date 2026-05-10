"""Tests for the Phase 1A segment instrumentation extension.

Coverage:
  1. _fav_size_bucket boundaries — heavy/mid/short fav, short/mid/long dog.
  2. _pitcher_completeness — both_real / one_default / both_default / tbd / n_a.
  3. _starter_known_label — coarse known / tbd / n_a derivation.
  4. GameEvaluation now exposes fav_size_bucket / pitcher_completeness / starter_known.
  5. _BacktestAggregator absorbs evaluations into the new buckets and
     emits the new top-level keys without disturbing existing ones.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock

from django.test import TestCase

from apps.core.services.backtesting_service import (
    FAV_SIZE_BUCKET_LABELS,
    GameEvaluation,
    PITCHER_COMPLETENESS_LABELS,
    STARTER_KNOWN_LABELS,
    _BacktestAggregator,
    _fav_size_bucket,
    _pitcher_completeness,
    _starter_known_label,
    aggregate_results,
)


# Boundaries we expect (mirrored from the spec):
#   ≤-200      → heavy_fav
#   -199..-150 → mid_fav
#   -149..+99  → short_fav
#   +100..+150 → short_dog
#   +151..+250 → mid_dog
#   +251..+    → long_dog


class FavSizeBucketTests(TestCase):
    def test_heavy_favorite(self):
        self.assertEqual(_fav_size_bucket(-200), 'heavy_fav')
        self.assertEqual(_fav_size_bucket(-500), 'heavy_fav')

    def test_mid_favorite(self):
        self.assertEqual(_fav_size_bucket(-199), 'mid_fav')
        self.assertEqual(_fav_size_bucket(-150), 'mid_fav')

    def test_short_favorite_lower_boundary(self):
        self.assertEqual(_fav_size_bucket(-149), 'short_fav')

    def test_short_favorite_upper_boundary(self):
        self.assertEqual(_fav_size_bucket(99), 'short_fav')

    def test_short_dog(self):
        self.assertEqual(_fav_size_bucket(100), 'short_dog')
        self.assertEqual(_fav_size_bucket(150), 'short_dog')

    def test_mid_dog(self):
        self.assertEqual(_fav_size_bucket(151), 'mid_dog')
        self.assertEqual(_fav_size_bucket(250), 'mid_dog')

    def test_long_dog(self):
        self.assertEqual(_fav_size_bucket(251), 'long_dog')
        self.assertEqual(_fav_size_bucket(500), 'long_dog')

    def test_none_safe_default(self):
        # Defensive: shouldn't normally happen; bucket falls into short_fav.
        self.assertEqual(_fav_size_bucket(None), 'short_fav')


class PitcherCompletenessTests(TestCase):
    def test_non_baseball_sport_returns_n_a(self):
        # CFB / CBB games don't carry pitcher fields — bucket is n_a so
        # the breakdown isn't polluted by football/basketball games.
        game = MagicMock()
        self.assertEqual(_pitcher_completeness('cfb', game), 'n_a')
        self.assertEqual(_pitcher_completeness('cbb', game), 'n_a')

    def test_tbd_when_either_pitcher_missing(self):
        game = MagicMock()
        game.home_pitcher = None
        game.away_pitcher = MagicMock(rating=70.0)
        self.assertEqual(_pitcher_completeness('mlb', game), 'tbd')

        game.home_pitcher = MagicMock(rating=60.0)
        game.away_pitcher = None
        self.assertEqual(_pitcher_completeness('mlb', game), 'tbd')

    def test_both_default(self):
        game = MagicMock()
        game.home_pitcher = MagicMock(rating=50.0)
        game.away_pitcher = MagicMock(rating=50.0)
        self.assertEqual(_pitcher_completeness('mlb', game), 'both_default')

    def test_one_default(self):
        game = MagicMock()
        game.home_pitcher = MagicMock(rating=50.0)
        game.away_pitcher = MagicMock(rating=70.0)
        self.assertEqual(_pitcher_completeness('mlb', game), 'one_default')

    def test_both_real(self):
        game = MagicMock()
        game.home_pitcher = MagicMock(rating=70.0)
        game.away_pitcher = MagicMock(rating=40.0)
        self.assertEqual(_pitcher_completeness('mlb', game), 'both_real')


class StarterKnownLabelTests(TestCase):
    def test_n_a_passes_through(self):
        self.assertEqual(_starter_known_label('n_a'), 'n_a')

    def test_tbd_passes_through(self):
        self.assertEqual(_starter_known_label('tbd'), 'tbd')

    def test_any_real_completeness_collapses_to_known(self):
        for label in ('both_real', 'one_default', 'both_default'):
            self.assertEqual(_starter_known_label(label), 'known')


# ---------------------------------------------------------------------------
# Aggregator integration


_eval_counter = [0]


def _make_eval(
    *,
    sport='mlb',
    pick_closing_odds=-130,
    pitcher_completeness='both_real',
    starter_known='known',
    won=True,
    edge=0.06,
    fav_size_bucket=None,
):
    """Test factory — yields a fully-populated GameEvaluation. Defaults
    chosen so a single eval lands in legitimate buckets across all
    breakdowns; tests only override what they care about.

    Game IDs are monotonic to avoid the aggregator's dedup short-circuit
    on (sport, game_id) — two evaluations with identical (sport, edge,
    won) inputs in the same test would otherwise count once.
    """
    _eval_counter[0] += 1
    return GameEvaluation(
        sport=sport,
        game_id=f'g-{_eval_counter[0]}',
        game_label='X @ Y',
        game_time=datetime(2026, 5, 1, 18, 0, tzinfo=dt_timezone.utc),
        predicted_home_prob=0.65,
        market_home_prob_fair=0.58,
        pick_is_home=True,
        pick_predicted_prob=0.65,
        pick_market_prob_fair=0.58,
        pick_opening_odds_american=-125,
        pick_closing_odds_american=pick_closing_odds,
        edge=edge,
        status='recommended',
        status_reason='',
        tier='standard',
        won=won,
        clv_decimal=0.02,
        is_approximate=False,
        is_favorite=pick_closing_odds < 0,
        is_home_pick=True,
        decision_quality='perfect' if won else 'bad',
        fav_size_bucket=fav_size_bucket or _fav_size_bucket(pick_closing_odds),
        pitcher_completeness=pitcher_completeness,
        starter_known=starter_known,
    )


class GameEvaluationFieldsTests(TestCase):
    """The new fields exist with sensible defaults and accept overrides."""

    def test_default_field_values(self):
        ev = GameEvaluation(
            sport='cfb',
            game_id='1',
            game_label='X',
            game_time=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
            predicted_home_prob=0.6,
            market_home_prob_fair=0.55,
            pick_is_home=True,
            pick_predicted_prob=0.6,
            pick_market_prob_fair=0.55,
            pick_opening_odds_american=None,
            pick_closing_odds_american=-110,
            edge=0.05,
            status='recommended',
            status_reason='',
            tier='standard',
            won=True,
            clv_decimal=None,
            is_approximate=False,
            is_favorite=True,
            is_home_pick=True,
        )
        # Defaults — no segment data set.
        self.assertEqual(ev.fav_size_bucket, 'short_fav')
        self.assertEqual(ev.pitcher_completeness, 'n_a')
        self.assertEqual(ev.starter_known, 'n_a')


class AggregatorBucketingTests(TestCase):
    def test_aggregator_initializes_all_segment_buckets_empty(self):
        agg = _BacktestAggregator()
        self.assertEqual(set(agg.by_fav_size.keys()), set(FAV_SIZE_BUCKET_LABELS))
        self.assertEqual(
            set(agg.by_pitcher_completeness.keys()),
            set(PITCHER_COMPLETENESS_LABELS),
        )
        self.assertEqual(set(agg.by_starter_known.keys()), set(STARTER_KNOWN_LABELS))

    def test_evaluation_lands_in_correct_fav_size_bucket(self):
        agg = _BacktestAggregator()
        agg.add(_make_eval(pick_closing_odds=-250))   # heavy_fav
        agg.add(_make_eval(pick_closing_odds=-130))   # short_fav
        agg.add(_make_eval(pick_closing_odds=300))    # long_dog
        out = agg.to_summary()['by_fav_size']
        self.assertEqual(out['heavy_fav']['sample'], 1)
        self.assertEqual(out['short_fav']['sample'], 1)
        self.assertEqual(out['long_dog']['sample'], 1)
        # Bucket dict shape preserved — same keys as _BucketAccumulator.to_dict().
        self.assertIn('roi_pct', out['heavy_fav'])
        self.assertIn('avg_clv', out['heavy_fav'])

    def test_pitcher_completeness_bucketing(self):
        agg = _BacktestAggregator()
        agg.add(_make_eval(pitcher_completeness='both_real'))
        agg.add(_make_eval(pitcher_completeness='one_default'))
        agg.add(_make_eval(pitcher_completeness='both_default'))
        agg.add(_make_eval(pitcher_completeness='tbd', starter_known='tbd'))
        out = agg.to_summary()['by_pitcher_completeness']
        self.assertEqual(out['both_real']['sample'], 1)
        self.assertEqual(out['one_default']['sample'], 1)
        self.assertEqual(out['both_default']['sample'], 1)
        self.assertEqual(out['tbd']['sample'], 1)

    def test_starter_known_bucketing_is_coarser(self):
        agg = _BacktestAggregator()
        # Three known evaluations + one tbd → starter_known known=3, tbd=1.
        for completeness in ('both_real', 'one_default', 'both_default'):
            agg.add(_make_eval(pitcher_completeness=completeness, starter_known='known'))
        agg.add(_make_eval(pitcher_completeness='tbd', starter_known='tbd'))
        out = agg.to_summary()['by_starter_known']
        self.assertEqual(out['known']['sample'], 3)
        self.assertEqual(out['tbd']['sample'], 1)

    def test_segment_buckets_dont_disturb_existing_keys(self):
        # Phase 1 + Phase 2 keys must still be present. Drift here would
        # signal an additive contract violation.
        out = aggregate_results([_make_eval()])
        for key in (
            'overall', 'overall_recommended_only',
            'by_sport', 'by_sport_recommended_only',
            'by_edge_bucket', 'by_tier',
            'by_favorite_underdog', 'by_home_away',
            'calibration_curve', 'validation',
            'where_is_my_edge', 'decision_quality',
            'clv_metrics', 'system_verdict',
            # New Phase 1A keys present too
            'by_fav_size', 'by_pitcher_completeness', 'by_starter_known',
        ):
            self.assertIn(key, out, f'Missing summary key: {key}')

    def test_recommendation_tier_breakdown_still_works_unchanged(self):
        # Tier breakdown is a Phase 1 key — segment instrumentation
        # explicitly does not touch it.
        agg = _BacktestAggregator()
        elite = _make_eval(edge=0.10)
        elite.tier = 'elite'
        strong = _make_eval(edge=0.06)
        strong.tier = 'strong'
        agg.add(elite)
        agg.add(strong)
        out = agg.to_summary()['by_tier']
        self.assertEqual(out['elite']['sample'], 1)
        self.assertEqual(out['strong']['sample'], 1)
        self.assertEqual(out['standard']['sample'], 0)
