"""Recommendation Health Score service.

Computes a composite 0–100 score expressing whether Brother Willies is
behaving like a disciplined predictive system. NOT an outcome predictor.
NOT a betting signal. Cannot affect recommendations — every consumer is
analytics-only.

Architecture contract (locked by tests):

  - Pure functions throughout. No DB writes from this module. Snapshot
    persistence lives in `health_snapshot.py`.
  - Deterministic. Same inputs → same outputs. No randomness.
  - Decomposable. Every dimension scores independently; the composite
    is a documented weighted average. An operator looking at a score
    can always answer "which dimension dragged this down?".
  - Explainable. Every per-dimension scoring formula is a piecewise
    linear function with named inputs and named output. No black-box
    transforms.
  - Bounded. Each dimension scores in [0, 100]. Composite in [0, 100].
  - Auditable. Every score is accompanied by the raw aggregation it
    was derived from (sample sizes, observed values, healthy bands).

Dimension weights and formulas come from
docs/recommendation_quality_framework.md §3 — the authoritative spec.
Code changes to weights or formulas require an amendment to that doc
in the same commit.

The Health Score IS the gate referenced in Law 4 ("Do Not Overfit").
Tuning decisions reference the score; the score does not auto-tune
anything.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone


# ---------------------------------------------------------------------------
# Score-band classification
# ---------------------------------------------------------------------------

BAND_STRONG = 'strong'
BAND_HEALTHY = 'healthy'
BAND_WATCH = 'watch'
BAND_INTERVENE = 'intervene'

BAND_LABELS = {
    BAND_STRONG: 'Strong',
    BAND_HEALTHY: 'Healthy',
    BAND_WATCH: 'Watch',
    BAND_INTERVENE: 'Intervene',
}


def classify_band(score: float) -> str:
    """Map a numeric score to its band per the framework §3.3 table.

    Boundaries are inclusive on the upper edge (75.0 is strong; 74.99
    is healthy). Below 0 / above 100 are clamped by caller; this
    function assumes well-formed input.
    """
    if score >= 75:
        return BAND_STRONG
    if score >= 50:
        return BAND_HEALTHY
    if score >= 25:
        return BAND_WATCH
    return BAND_INTERVENE


# ---------------------------------------------------------------------------
# Dimension weights — authoritative copy of the framework's §3.1 table.
# Sum MUST equal 1.0 (locked by test).
# ---------------------------------------------------------------------------

DIMENSION_WEIGHTS = {
    'clv_trend': 0.25,
    'calibration': 0.20,
    'edge_realism': 0.15,
    'recommendation_stability': 0.10,
    'market_alignment': 0.10,
    'stale_odds': 0.10,
    'volume_vs_target': 0.10,
}

# Stable ordering for templates and JSON output.
DIMENSION_ORDER = [
    'clv_trend',
    'calibration',
    'edge_realism',
    'recommendation_stability',
    'market_alignment',
    'stale_odds',
    'volume_vs_target',
]

DIMENSION_LABELS = {
    'clv_trend': 'CLV Trend',
    'calibration': 'Calibration Accuracy',
    'edge_realism': 'Edge Realism',
    'recommendation_stability': 'Recommendation Stability',
    'market_alignment': 'Market Alignment',
    'stale_odds': 'Stale-Odds Rate',
    'volume_vs_target': 'Volume vs Target',
}


# ---------------------------------------------------------------------------
# Per-dimension scoring formulas
# ---------------------------------------------------------------------------
#
# Every dimension is a piecewise linear function of one observable
# quantity. Boundary values come from the framework §3.1 table and are
# treated as architecture constants — moving them is a framework
# amendment, not a tune.


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _linear_score(
    value: float, *, score_at_low: float, score_at_high: float,
    low_input: float, high_input: float,
) -> float:
    """Map `value` linearly from input range to score range.

    If high_input < low_input the slope is reversed (used for "lower is
    better" metrics like stale-odds and Brier).
    """
    if high_input == low_input:
        return score_at_low
    fraction = (value - low_input) / (high_input - low_input)
    raw = score_at_low + fraction * (score_at_high - score_at_low)
    # Clamp at both endpoints — the function is defined as piecewise
    # constant outside the [low_input, high_input] range.
    bounds = sorted([score_at_low, score_at_high])
    return _clamp(raw, bounds[0], bounds[1])


def score_clv_trend(positive_clv_rate: Optional[float], sample: int) -> dict:
    """CLV+ rate dimension. Linear 0 at 30% rate → 100 at 60% rate.

    No-sample case: score is None (no signal yet, dimension excluded
    from composite). The composite handles None dimensions by
    re-weighting the remaining dimensions to sum to 1.0.
    """
    if positive_clv_rate is None or sample < 1:
        return {
            'score': None, 'value': None, 'sample': sample,
            'status': 'no_data',
            'healthy_range': 'CLV+ ≥ 45%',
        }
    score = _linear_score(
        positive_clv_rate,
        score_at_low=0.0, score_at_high=100.0,
        low_input=0.30, high_input=0.60,
    )
    return {
        'score': round(score, 2),
        'value': round(positive_clv_rate, 4),
        'sample': sample,
        'status': classify_band(score),
        'healthy_range': 'CLV+ ≥ 45%',
    }


def score_calibration(brier_score: Optional[float], sample: int) -> dict:
    """Calibration dimension via Brier score. Lower is better.

    Linear: Brier=0.30 → 0 score; Brier=0.18 → 100 score.
    """
    if brier_score is None or sample < 1:
        return {
            'score': None, 'value': None, 'sample': sample,
            'status': 'no_data',
            'healthy_range': 'Brier < 0.22',
        }
    # Slope is reversed (high score at LOW brier). Use the linear helper
    # with inverted endpoints.
    score = _linear_score(
        brier_score,
        score_at_low=100.0, score_at_high=0.0,
        low_input=0.18, high_input=0.30,
    )
    return {
        'score': round(score, 2),
        'value': round(brier_score, 4),
        'sample': sample,
        'status': classify_band(score),
        'healthy_range': 'Brier < 0.22',
    }


def score_edge_realism(
    roi_8plus: Optional[float], roi_4to6: Optional[float],
    sample_8plus: int, sample_4to6: int,
) -> dict:
    """Edge realism: 8+ edge bucket ROI vs 4-6 edge bucket ROI.

    Healthy state: higher edge buckets should outperform lower. The
    score:
      - 100 when 8+ ROI exceeds 4-6 ROI by ≥ 5 pp (clear monotonic).
      - 50 when they're equal (edge math produces no extra signal at
        the high end).
      - 0 when 8+ ROI is ≥ 5 pp BELOW 4-6 ROI (perverse — the "fake
        giant edge" pathology this dimension exists to detect).

    Linear interpolation in between. The 5 pp shoulder is generous
    because per-bucket ROI is high-variance — we don't want to flip
    bands on noise.
    """
    if (
        roi_8plus is None or roi_4to6 is None
        or sample_8plus < 5 or sample_4to6 < 5
    ):
        return {
            'score': None,
            'value': None,
            'sample_8plus': sample_8plus,
            'sample_4to6': sample_4to6,
            'status': 'no_data',
            'healthy_range': '8+ ROI ≥ 4-6 ROI',
        }
    delta = roi_8plus - roi_4to6  # positive when 8+ outperforms 4-6
    score = _linear_score(
        delta,
        score_at_low=0.0, score_at_high=100.0,
        low_input=-0.05, high_input=0.05,  # in decimal share units (5 pp)
    )
    return {
        'score': round(score, 2),
        'value': round(delta, 4),
        'roi_8plus': round(roi_8plus, 4),
        'roi_4to6': round(roi_4to6, 4),
        'sample_8plus': sample_8plus,
        'sample_4to6': sample_4to6,
        'status': classify_band(score),
        'healthy_range': '8+ ROI ≥ 4-6 ROI',
    }


def score_recommendation_stability(
    week_volumes: list, sample_weeks: int,
) -> dict:
    """Stability via week-over-week volume variance.

    100 when current week is within 2σ of the rolling mean.
    Linearly decays to 0 at 4σ.

    Requires ≥ 3 weeks of history to compute a meaningful σ.
    """
    if sample_weeks < 3:
        return {
            'score': None, 'value': None, 'sample_weeks': sample_weeks,
            'status': 'no_data',
            'healthy_range': '|current - mean| ≤ 2σ',
        }
    # Zero-activity guard: when no recommendations exist at all (all
    # weeks are zero), this dimension has nothing to measure. Treat
    # as no_data, not "perfectly stable at zero." The empty-DB case
    # would otherwise score 100 and falsely lift the composite.
    if sum(week_volumes) == 0:
        return {
            'score': None, 'value': None, 'sample_weeks': sample_weeks,
            'status': 'no_data',
            'healthy_range': '|current - mean| ≤ 2σ',
        }
    history = week_volumes[:-1]  # exclude current week from baseline
    current = week_volumes[-1]
    if len(history) < 2:
        return {
            'score': None, 'value': None, 'sample_weeks': sample_weeks,
            'status': 'no_data',
            'healthy_range': '|current - mean| ≤ 2σ',
        }
    mean_v = statistics.mean(history)
    stdev = statistics.stdev(history) if len(history) > 1 else 0.0
    if stdev == 0:
        # Degenerate: all prior weeks had identical volume. Treat any
        # deviation as a 100 (volume is constant; current is at the
        # mean by definition) when current matches; otherwise penalize
        # linearly with the absolute delta.
        score = 100.0 if current == mean_v else 50.0
        sigmas = 0.0
    else:
        sigmas = abs(current - mean_v) / stdev
        score = _linear_score(
            sigmas,
            score_at_low=100.0, score_at_high=0.0,
            low_input=2.0, high_input=4.0,
        )
        # Below 2σ pin to 100; above 4σ pin to 0 (handled by clamp in
        # _linear_score).
        if sigmas < 2.0:
            score = 100.0
    return {
        'score': round(score, 2),
        'value': round(sigmas, 3),
        'current_volume': current,
        'mean_volume': round(mean_v, 2),
        'stdev_volume': round(stdev, 2),
        'sample_weeks': sample_weeks,
        'status': classify_band(score),
        'healthy_range': '|current − mean| ≤ 2σ',
    }


def score_market_alignment(
    avg_disagreement: Optional[float], sample: int,
) -> dict:
    """Mean |final_prob − fair_market_prob| across recent recommendations.

    Linear: 100 at 0.05 mean disagreement → 0 at 0.20.
    """
    if avg_disagreement is None or sample < 1:
        return {
            'score': None, 'value': None, 'sample': sample,
            'status': 'no_data',
            'healthy_range': 'mean disagreement < 0.08',
        }
    score = _linear_score(
        avg_disagreement,
        score_at_low=100.0, score_at_high=0.0,
        low_input=0.05, high_input=0.20,
    )
    return {
        'score': round(score, 2),
        'value': round(avg_disagreement, 4),
        'sample': sample,
        'status': classify_band(score),
        'healthy_range': 'mean disagreement < 0.08',
    }


def score_stale_odds(stale_rate: Optional[float], sample: int) -> dict:
    """Stale-odds rate: fraction of settled bets without a usable closing snapshot.

    Linear: 100 at 0% → 0 at 15%.
    """
    if stale_rate is None or sample < 1:
        return {
            'score': None, 'value': None, 'sample': sample,
            'status': 'no_data',
            'healthy_range': 'stale < 5%',
        }
    score = _linear_score(
        stale_rate,
        score_at_low=100.0, score_at_high=0.0,
        low_input=0.0, high_input=0.15,
    )
    return {
        'score': round(score, 2),
        'value': round(stale_rate, 4),
        'sample': sample,
        'status': classify_band(score),
        'healthy_range': 'stale < 5%',
    }


def score_volume_vs_target(
    current_volume: int, target_mean: Optional[float], target_stdev: float,
    sample_weeks: int,
) -> dict:
    """Current week volume against a rolling target band.

    Symmetric: 100 inside 2σ of the target_mean, decaying to 0 at 4σ.

    Note: this dimension overlaps with `recommendation_stability` in
    spirit but answers a different question. Stability is "is volume
    varying chaotically week to week?"; volume-vs-target is "is this
    week inside the historical envelope?". A run that's been stable
    but low for months passes stability and fails volume-vs-target.
    """
    if target_mean is None or sample_weeks < 4:
        return {
            'score': None, 'value': None, 'sample_weeks': sample_weeks,
            'status': 'no_data',
            'healthy_range': 'within 2σ of rolling mean',
        }
    # Zero-activity guard mirrors recommendation_stability — when no
    # rolling target exists yet (all-zero history + zero current),
    # the dimension has nothing to measure.
    if target_mean == 0 and current_volume == 0:
        return {
            'score': None, 'value': None, 'sample_weeks': sample_weeks,
            'status': 'no_data',
            'healthy_range': 'within 2σ of rolling mean',
        }
    if target_stdev == 0:
        score = 100.0 if current_volume == target_mean else 50.0
        sigmas = 0.0
    else:
        sigmas = abs(current_volume - target_mean) / target_stdev
        if sigmas <= 2.0:
            score = 100.0
        else:
            score = _linear_score(
                sigmas,
                score_at_low=100.0, score_at_high=0.0,
                low_input=2.0, high_input=4.0,
            )
    return {
        'score': round(score, 2),
        'value': round(sigmas, 3),
        'current_volume': current_volume,
        'target_mean': round(target_mean, 2),
        'target_stdev': round(target_stdev, 2),
        'sample_weeks': sample_weeks,
        'status': classify_band(score),
        'healthy_range': 'within 2σ of rolling mean',
    }


# ---------------------------------------------------------------------------
# Composite — weighted average with re-weighting for missing dimensions
# ---------------------------------------------------------------------------


def compute_composite(dimension_scores: dict) -> Optional[float]:
    """Weighted composite of per-dimension scores.

    Dimensions whose score is None (insufficient data) are excluded
    from the composite AND from the weight denominator — the remaining
    dimensions are re-normalized so the composite stays in [0, 100].

    Returns None when no dimension has data. The caller (snapshot
    layer) treats None as "insufficient data; do not classify."
    """
    weighted_sum = 0.0
    weight_total = 0.0
    for key, weight in DIMENSION_WEIGHTS.items():
        info = dimension_scores.get(key)
        if info is None:
            continue
        sc = info.get('score')
        if sc is None:
            continue
        weighted_sum += sc * weight
        weight_total += weight
    if weight_total == 0:
        return None
    return round(weighted_sum / weight_total, 2)


# ---------------------------------------------------------------------------
# Data-collection helpers — read from MockBet, BettingRecommendation,
# and OddsSnapshot. All read-only. All operate over a configurable
# window (default 14 days).
# ---------------------------------------------------------------------------


def _settled_mlb_mockbets_in_window(window_days: int, now=None):
    """Return the queryset of settled moneyline MLB bets in window.

    Restricted to MLB because the Health Score is currently scoped to
    the MLB Elo cutover; expansion to other sports would amend this
    helper and add per-sport context.
    """
    from apps.mockbets.models import MockBet
    now = now or timezone.now()
    cutoff = now - timedelta(days=window_days)
    return MockBet.objects.filter(
        sport='mlb',
        bet_type='moneyline',
        placed_at__gte=cutoff,
        result__in=('win', 'loss', 'push'),
    )


def _aggregate_clv(window_days: int, now=None) -> dict:
    """CLV+ rate over settled MLB moneyline bets in window.

    Restricted to `odds_source='odds_api'` per the framework — ESPN
    fallback CLV is capture-artifact, not real movement.
    """
    qs = _settled_mlb_mockbets_in_window(window_days, now)
    bets_with_clv = qs.filter(
        clv_cents__isnull=False,
        odds_source='odds_api',
    )
    sample = bets_with_clv.count()
    if sample == 0:
        return {'positive_clv_rate': None, 'sample': 0}
    positive = bets_with_clv.filter(clv_cents__gt=0).count()
    return {
        'positive_clv_rate': positive / sample,
        'sample': sample,
    }


def _aggregate_calibration(window_days: int, now=None) -> dict:
    """Brier score across settled MLB moneyline bets in window.

    Reads each bet's recommendation_confidence (the model probability
    at placement, in percent) and the binary outcome. Brier =
    mean((predicted_prob − actual)^2).
    """
    qs = _settled_mlb_mockbets_in_window(window_days, now).filter(
        recommendation_confidence__isnull=False,
        result__in=('win', 'loss'),  # pushes excluded (no binary outcome)
    )
    rows = list(qs.values('recommendation_confidence', 'result'))
    if not rows:
        return {'brier': None, 'sample': 0}
    squared_errors = []
    for r in rows:
        p = float(r['recommendation_confidence']) / 100.0
        actual = 1.0 if r['result'] == 'win' else 0.0
        squared_errors.append((p - actual) ** 2)
    return {
        'brier': sum(squared_errors) / len(squared_errors),
        'sample': len(squared_errors),
    }


def _aggregate_edge_realism(window_days: int, now=None) -> dict:
    """ROI for the 8+ edge bucket vs the 4-6 edge bucket.

    Edge buckets in pp:
      4-6 : [4, 6)
      8+  : [8, +inf)
    """
    qs = _settled_mlb_mockbets_in_window(window_days, now).filter(
        expected_edge__isnull=False,
    )
    rows = list(qs.values(
        'expected_edge', 'stake_amount', 'simulated_payout', 'result',
    ))

    def _roi(rows_subset):
        if not rows_subset:
            return None, 0
        stake = sum(float(r['stake_amount']) for r in rows_subset)
        payout = sum(
            float(r['simulated_payout'] or 0) for r in rows_subset
            if r['result'] == 'win'
        )
        if stake == 0:
            return None, 0
        return (payout - stake) / stake, len(rows_subset)

    bucket_4to6 = [
        r for r in rows
        if 4.0 <= float(r['expected_edge']) < 6.0
    ]
    bucket_8plus = [
        r for r in rows
        if float(r['expected_edge']) >= 8.0
    ]
    roi_4to6, sample_4to6 = _roi(bucket_4to6)
    roi_8plus, sample_8plus = _roi(bucket_8plus)
    return {
        'roi_4to6': roi_4to6, 'sample_4to6': sample_4to6,
        'roi_8plus': roi_8plus, 'sample_8plus': sample_8plus,
    }


def _aggregate_stable_odds(window_days: int, now=None) -> dict:
    """Fraction of settled MLB moneyline bets with no closing-odds snapshot."""
    qs = _settled_mlb_mockbets_in_window(window_days, now)
    sample = qs.count()
    if sample == 0:
        return {'stale_rate': None, 'sample': 0}
    stale = qs.filter(closing_odds_american__isnull=True).count()
    return {'stale_rate': stale / sample, 'sample': sample}


def _aggregate_weekly_volumes(weeks: int = 8, now=None) -> dict:
    """Weekly recommendation counts over the past `weeks` ISO-style weeks.

    Uses BettingRecommendation rows for MLB. Each week is a 7-day
    bucket counting backwards from now. Returns the list in
    chronological order (oldest first) so the last element is the
    current week.
    """
    from apps.core.models import BettingRecommendation
    now = now or timezone.now()
    volumes = []
    for i in range(weeks, 0, -1):
        start = now - timedelta(days=i * 7)
        end = now - timedelta(days=(i - 1) * 7)
        count = BettingRecommendation.objects.filter(
            sport='mlb',
            created_at__gte=start,
            created_at__lt=end,
        ).count()
        volumes.append(count)
    return {'volumes': volumes, 'sample_weeks': weeks}


def _aggregate_market_alignment(window_days: int, now=None) -> dict:
    """Mean |final_prob − fair_market| over recent MLB recommendations.

    Reads `final_model_prob` and `market_prob` snapshots on
    BettingRecommendation. Both are populated since the 2026-05-03
    calibration snapshot work.
    """
    from apps.core.models import BettingRecommendation
    now = now or timezone.now()
    cutoff = now - timedelta(days=window_days)
    qs = BettingRecommendation.objects.filter(
        sport='mlb',
        created_at__gte=cutoff,
        final_model_prob__isnull=False,
        market_prob__isnull=False,
    )
    rows = list(qs.values('final_model_prob', 'market_prob'))
    if not rows:
        return {'avg_disagreement': None, 'sample': 0}
    deltas = [
        abs(float(r['final_model_prob']) - float(r['market_prob']))
        for r in rows
    ]
    return {
        'avg_disagreement': sum(deltas) / len(deltas),
        'sample': len(deltas),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class HealthScore:
    """Composite score + per-dimension breakdown + supporting data.

    Returned by `compute_health_score`; consumed by the snapshot layer
    and the analytics view. Asdict-friendly for JSON serialization to
    `RecommendationHealthSnapshot.dimension_scores` and `supporting_data`.
    """
    overall_score: Optional[float]
    band: Optional[str]
    dimension_scores: dict
    supporting_data: dict
    window_days: int
    rating_mode_active: str
    calibration_state: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _capture_calibration_state() -> dict:
    """Snapshot the relevant calibration constants at compute time.

    Imported here (not module-level) so future calibration retunes that
    move constants between modules don't break this helper at import.
    """
    from apps.core.services import probability_calibration as pc
    from apps.core.services import recommendations as rec
    return {
        'market_blend_weight': pc.MARKET_BLEND_WEIGHT,
        'market_blend_weight_cap': pc.MARKET_BLEND_WEIGHT_CAP,
        'prob_min': pc.PROB_MIN,
        'prob_max': pc.PROB_MAX,
        'min_edge': rec.MIN_EDGE,
        'min_probability_for_recommended': rec.MIN_PROBABILITY_FOR_RECOMMENDED,
        'extreme_disagreement_gap': rec.EXTREME_DISAGREEMENT_GAP,
        'heavy_favorite_odds': rec.HEAVY_FAVORITE_ODDS,
    }


def _active_rating_mode() -> str:
    from apps.core.services.elo_service import is_dynamic_active
    return 'elo' if is_dynamic_active() else 'static'


def compute_health_score(window_days: int = 14, now=None) -> HealthScore:
    """Compute the composite Health Score for the given window.

    All inputs are read live from MockBet + BettingRecommendation. No
    DB writes. Safe to call from any read path. Cost: O(N) over the
    in-window bets and recs; typically < 100ms.

    Returns a HealthScore dataclass with:
      - overall_score: weighted composite, 0-100 or None
      - band: 'strong' / 'healthy' / 'watch' / 'intervene' or None
      - dimension_scores: per-dimension dict (see scoring functions)
      - supporting_data: raw aggregations (for snapshot audit)
      - window_days, rating_mode_active, calibration_state
    """
    now = now or timezone.now()

    # --- Raw aggregations ----------------------------------------------
    clv = _aggregate_clv(window_days, now)
    calibration = _aggregate_calibration(window_days, now)
    edge_realism = _aggregate_edge_realism(window_days, now)
    stale_odds = _aggregate_stable_odds(window_days, now)
    volumes = _aggregate_weekly_volumes(weeks=8, now=now)
    market_align = _aggregate_market_alignment(window_days, now)

    supporting_data = {
        'clv': clv,
        'calibration': calibration,
        'edge_realism': edge_realism,
        'stale_odds': stale_odds,
        'weekly_volumes': volumes,
        'market_alignment': market_align,
    }

    # --- Per-dimension scores ------------------------------------------
    dimension_scores = {
        'clv_trend': score_clv_trend(
            clv['positive_clv_rate'], clv['sample'],
        ),
        'calibration': score_calibration(
            calibration['brier'], calibration['sample'],
        ),
        'edge_realism': score_edge_realism(
            edge_realism['roi_8plus'], edge_realism['roi_4to6'],
            edge_realism['sample_8plus'], edge_realism['sample_4to6'],
        ),
        'recommendation_stability': score_recommendation_stability(
            volumes['volumes'], volumes['sample_weeks'],
        ),
        'market_alignment': score_market_alignment(
            market_align['avg_disagreement'], market_align['sample'],
        ),
        'stale_odds': score_stale_odds(
            stale_odds['stale_rate'], stale_odds['sample'],
        ),
        'volume_vs_target': _volume_vs_target_score(volumes),
    }

    overall = compute_composite(dimension_scores)
    band = classify_band(overall) if overall is not None else None

    return HealthScore(
        overall_score=overall,
        band=band,
        dimension_scores=dimension_scores,
        supporting_data=supporting_data,
        window_days=window_days,
        rating_mode_active=_active_rating_mode(),
        calibration_state=_capture_calibration_state(),
    )


def _volume_vs_target_score(volumes_blob: dict) -> dict:
    """Glue: extract the rolling-target inputs from the volumes blob
    and call score_volume_vs_target.

    The history-vs-current split lives here so the scoring function
    stays purely numeric.
    """
    series = volumes_blob['volumes']
    sample_weeks = volumes_blob['sample_weeks']
    if len(series) < 4:
        return score_volume_vs_target(
            current_volume=series[-1] if series else 0,
            target_mean=None, target_stdev=0.0,
            sample_weeks=sample_weeks,
        )
    history = series[:-1]
    current = series[-1]
    mean_v = statistics.mean(history)
    stdev = statistics.stdev(history) if len(history) > 1 else 0.0
    return score_volume_vs_target(
        current_volume=current,
        target_mean=mean_v, target_stdev=stdev,
        sample_weeks=sample_weeks,
    )


# ---------------------------------------------------------------------------
# Warning thresholds — surfaced to the operator alongside the score
# ---------------------------------------------------------------------------


def detect_warnings(health: HealthScore) -> list:
    """Return a list of per-dimension warnings the operator should review.

    Warnings are categorical, not score-derived: they fire when a
    specific dimension crosses a danger threshold even if the
    composite is healthy. The composite can mask a single broken
    dimension; the warnings surface it.

    Warning structure:
        {
            'dimension': 'clv_trend',
            'severity': 'critical' | 'warning',
            'message': 'human-readable explanation',
        }
    """
    warnings = []
    dims = health.dimension_scores

    clv = dims.get('clv_trend', {})
    if (
        clv.get('value') is not None and clv.get('sample', 0) >= 30
        and clv['value'] < 0.35
    ):
        warnings.append({
            'dimension': 'clv_trend',
            'severity': 'critical',
            'message': (
                f'CLV+ rate {clv["value"]:.1%} over {clv["sample"]} bets — '
                'below the 35% danger threshold. Investigate model/market '
                'misalignment.'
            ),
        })

    cal = dims.get('calibration', {})
    if (
        cal.get('value') is not None and cal.get('sample', 0) >= 50
        and cal['value'] > 0.27
    ):
        warnings.append({
            'dimension': 'calibration',
            'severity': 'warning',
            'message': (
                f'Brier score {cal["value"]:.3f} — model predictions are '
                'less accurate than typical. Investigate calibration drift.'
            ),
        })

    edge = dims.get('edge_realism', {})
    if (
        edge.get('value') is not None
        and edge.get('sample_8plus', 0) >= 20
        and edge.get('sample_4to6', 0) >= 20
        and edge['value'] < -0.05
    ):
        warnings.append({
            'dimension': 'edge_realism',
            'severity': 'critical',
            'message': (
                f'8+ edge bucket ROI is {edge["value"]:.1%} below the '
                '4-6 bucket — the "fake giant edge" pathology. Edge '
                'realism compression candidate.'
            ),
        })

    stability = dims.get('recommendation_stability', {})
    if (
        stability.get('value') is not None and stability['value'] > 3.0
    ):
        warnings.append({
            'dimension': 'recommendation_stability',
            'severity': 'warning',
            'message': (
                f'Current volume is {stability["value"]:.1f}σ from the rolling '
                'mean. Investigate ingestion / regime change before tuning.'
            ),
        })

    market = dims.get('market_alignment', {})
    if (
        market.get('value') is not None and market.get('sample', 0) >= 30
        and market['value'] > 0.15
    ):
        warnings.append({
            'dimension': 'market_alignment',
            'severity': 'warning',
            'message': (
                f'Mean disagreement {market["value"]:.3f} is in the "investigate" '
                'band (>0.10). Inspect Model Inventory for the highest-disagreement '
                'games to identify the cause.'
            ),
        })

    stale = dims.get('stale_odds', {})
    if (
        stale.get('value') is not None and stale.get('sample', 0) >= 30
        and stale['value'] > 0.10
    ):
        warnings.append({
            'dimension': 'stale_odds',
            'severity': 'warning',
            'message': (
                f'{stale["value"]:.1%} of settled bets have no closing-odds '
                'snapshot. Provider health may be degraded.'
            ),
        })

    return warnings
