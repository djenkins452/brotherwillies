"""System Tuning analytics — staff-only diagnostic that turns mock-bet history
into deterministic, actionable signals about the recommendation engine.

Design tenets:
  - **Reuse, don't recompute.** Wraps existing services (analytics.compute_kpis,
    edge_analysis, recommendation_performance._group_stats) instead of forking
    the math.
  - **Deterministic.** No LLM, no randomness — same bets in, same insights out.
  - **Advisory only.** Reports on what the engine has done; never mutates
    config or models.
  - **Stable shape.** Every top-level key is always present, even with zero
    bets, so the template never has to defensive-check.

Mixed-scope CLV: ROI uses all settled bets, CLV is moneyline-only (the only
place CLV is captured today — see services/clv.py:77).
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Iterable, List

from django.utils import timezone

# Engine config we surface read-only — the real constants, not invented values.
from apps.core.services.recommendations import (
    MIN_EDGE,
    STRONG_EDGE,
    ELITE_EDGE,
    HEAVY_FAVORITE_ODDS,
    MAX_ELITE_PER_SLATE,
)
from apps.mockbets.services.recommendation_performance import _group_stats


# --- Time windows ------------------------------------------------------------
# Window keys are stable across calls so the template can index them directly.
WINDOW_KEYS = ('7d', '30d', 'all_time')


def _filter_window(bets, days):
    """Return bets placed within the last `days` days. None = all-time."""
    if days is None:
        return list(bets)
    cutoff = timezone.now() - timedelta(days=days)
    return [b for b in bets if b.placed_at >= cutoff]


def compute_time_windows(bets):
    """Per-window metrics. Always returns all three window keys."""
    materialized = list(bets)
    return {
        '7d': _window_stats(_filter_window(materialized, 7)),
        '30d': _window_stats(_filter_window(materialized, 30)),
        'all_time': _window_stats(_filter_window(materialized, None)),
    }


def _window_stats(window_bets):
    """Compact KPI block for a time window. Same math as _group_stats but
    expressed in window terms with explicit zero-handling for empty windows."""
    settled = [b for b in window_bets if b.result and b.result != 'pending']
    if not settled:
        return {
            'count': 0,
            'roi': 0.0,
            'win_rate': 0.0,
            'net_pl': 0.0,
            'avg_clv': 0.0,
            'positive_clv_rate': 0.0,
            'clv_sample': 0,
        }
    stats = _group_stats(settled)
    return {
        'count': stats['total_bets'],
        'roi': stats['roi'],
        'win_rate': stats['win_rate'],
        'net_pl': float(stats['net_pl']),
        'avg_clv': stats['avg_clv'],
        'positive_clv_rate': stats['positive_clv_rate'],
        'clv_sample': stats['clv_sample'],
    }


# --- Data confidence ---------------------------------------------------------
# Sample-size signal so a 3-bet lucky streak doesn't pretend to be a verdict.

def compute_data_confidence(bets):
    """Bucket the sample size into LOW/MEDIUM/HIGH per spec."""
    total = sum(1 for _ in bets)
    if total < 30:
        level = 'LOW'
    elif total < 100:
        level = 'MEDIUM'
    else:
        level = 'HIGH'
    return {
        'level': level,
        'total_bets': total,
        'thresholds': {'low_max': 30, 'medium_max': 100},
    }


# --- Segmentations -----------------------------------------------------------

BET_TYPE_KEYS = ('moneyline', 'spread', 'total')


def segment_by_bet_type(bets):
    """ROI/CLV per bet type. Spread/total are ROI-only (no CLV in v1)."""
    settled = [b for b in bets if b.result and b.result != 'pending']
    buckets = defaultdict(list)
    for b in settled:
        buckets[b.bet_type].append(b)
    out = {}
    for key in BET_TYPE_KEYS:
        s = _group_stats(buckets.get(key, []))
        out[key] = {
            **s,
            # CLV math is only defined for moneyline today (services/clv.py:77).
            # For spread/total we explicitly null the CLV fields rather than
            # showing 0% which would imply "every bet had negative CLV".
            'clv_available': key == 'moneyline',
        }
    return out


ODDS_RANGE_KEYS = ('underdog', 'mid_dog', 'mid', 'favorite', 'heavy_favorite')


def classify_odds_range(odds):
    """Map American odds → bucket key. Pure function for tests."""
    if odds is None:
        return None
    if odds >= 150:
        return 'underdog'
    if 100 <= odds < 150:
        return 'mid_dog'
    if -150 <= odds < 100:
        return 'mid'
    if -300 <= odds < -150:
        return 'favorite'
    return 'heavy_favorite'


def segment_by_odds_range(bets):
    """ROI by American-odds bucket per spec thresholds."""
    settled = [b for b in bets if b.result and b.result != 'pending']
    buckets = defaultdict(list)
    for b in settled:
        key = classify_odds_range(b.odds_american)
        if key is not None:
            buckets[key].append(b)
    return {key: _group_stats(buckets.get(key, [])) for key in ODDS_RANGE_KEYS}


def segment_by_source_quality(bets):
    """Source-quality placeholder.

    Returning a stub keeps the page's output shape stable and signals to the
    template exactly why the section is empty. v1 has no odds-provenance field
    on MockBet, and inferring it from OddsSnapshot.sportsbook would require a
    join + heuristics that the spec rejected. The placeholder is honest: the
    instrumentation needed for this segment doesn't exist yet.
    """
    return {
        'instrumented': False,
        'message': 'Not yet instrumented — requires odds provenance tracking on bets.',
        'rows': {},
    }


# --- Insights engine ---------------------------------------------------------
# Rule-based, no LLM. Each rule produces a (category, message, evidence) tuple.
# Evidence carries the numbers that triggered the rule so the UI/tests can
# verify exactly why it fired.

INSIGHT_CATEGORIES = ('strength', 'weakness', 'risk')

# Sample-size floor for any per-segment insight to fire. Below this, the
# segment number is treated as too noisy to recommend action on.
_MIN_SEGMENT_SAMPLE = 10


def generate_insights(ctx):
    """Run all rules. ctx is the bag returned by compute_all so rules can
    cross-reference each other (e.g. high-edge ROI vs low-edge ROI)."""
    insights = []

    overall = ctx['overall']
    by_type = ctx['by_bet_type']
    by_odds = ctx['by_odds_range']
    edge = ctx['edge']

    # Strength / risk on the global CLV signal.
    if overall['clv_sample'] >= _MIN_SEGMENT_SAMPLE:
        if overall['positive_clv_rate'] < 50.0:
            insights.append((
                'risk',
                'Market moving against picks',
                {'positive_clv_rate': overall['positive_clv_rate'], 'sample': overall['clv_sample']},
            ))
        elif overall['positive_clv_rate'] >= 55.0:
            insights.append((
                'strength',
                'Consistently beating the closing line',
                {'positive_clv_rate': overall['positive_clv_rate'], 'sample': overall['clv_sample']},
            ))

    # Spread underperforming.
    spread = by_type.get('spread', {})
    if spread.get('total_bets', 0) >= _MIN_SEGMENT_SAMPLE and spread.get('roi', 0.0) < -5.0:
        insights.append((
            'weakness',
            'Spread bets underperforming',
            {'roi': spread['roi'], 'sample': spread['total_bets']},
        ))

    # Total underperforming (same threshold as spread).
    total = by_type.get('total', {})
    if total.get('total_bets', 0) >= _MIN_SEGMENT_SAMPLE and total.get('roi', 0.0) < -5.0:
        insights.append((
            'weakness',
            'Total bets underperforming',
            {'roi': total['roi'], 'sample': total['total_bets']},
        ))

    # High-edge bets should outperform low-edge. If they don't, the model is
    # likely inflating its edge estimates — a real tuning signal. Reads the
    # canonical edge buckets from command_center (list of 4 buckets in
    # ascending order: 0-2pp / 2-4pp / 4-6pp / 6pp+).
    if edge and len(edge) >= 4:
        small = edge[0]   # 0-2pp
        large = edge[-1]  # 6pp+
        if (
            small.get('count', 0) >= _MIN_SEGMENT_SAMPLE
            and large.get('count', 0) >= _MIN_SEGMENT_SAMPLE
            and large.get('roi', 0.0) <= small.get('roi', 0.0)
        ):
            insights.append((
                'weakness',
                'High-edge bets not outperforming low-edge bets',
                {'large_roi': large['roi'], 'small_roi': small['roi']},
            ))

    # Underdog value detection — strong ROI on +150-and-up dogs.
    underdog = by_odds.get('underdog', {})
    if underdog.get('total_bets', 0) >= _MIN_SEGMENT_SAMPLE and underdog.get('roi', 0.0) > 8.0:
        insights.append((
            'strength',
            'Underdog value detection performing well',
            {'roi': underdog['roi'], 'sample': underdog['total_bets']},
        ))

    # Heavy favorites bleeding money — engine should already gate these by
    # juice rules but if mock bets show losses, surface it.
    heavy = by_odds.get('heavy_favorite', {})
    if heavy.get('total_bets', 0) >= _MIN_SEGMENT_SAMPLE and heavy.get('roi', 0.0) < -5.0:
        insights.append((
            'weakness',
            'Heavy-favorite bets losing money',
            {'roi': heavy['roi'], 'sample': heavy['total_bets']},
        ))

    # Overall ROI floor — surface as a risk so it makes it onto the verdict.
    if overall['total_bets'] >= _MIN_SEGMENT_SAMPLE and overall['roi'] < -5.0:
        insights.append((
            'risk',
            'Overall ROI is negative beyond variance band',
            {'roi': overall['roi'], 'sample': overall['total_bets']},
        ))

    # Format into dicts for template-friendly access.
    return [
        {'category': c, 'message': m, 'evidence': e}
        for (c, m, e) in insights
    ]


# --- Verdict -----------------------------------------------------------------

VERDICT_HEALTH_LEVELS = ('strong', 'needs_adjustment', 'weak')


def compute_verdict(ctx):
    """Top-of-page summary. Always returns the same shape."""
    insights = ctx['insights']
    overall = ctx['overall']

    roi = overall['roi']
    pos_clv_rate = overall['positive_clv_rate']
    has_clv_sample = overall['clv_sample'] >= _MIN_SEGMENT_SAMPLE

    # Order matters — weak short-circuits first, then strong's positive
    # gate, then needs_adjustment as the catch-all.
    if overall['total_bets'] == 0:
        health = 'needs_adjustment'  # nothing to go on; default to "watch this"
    elif roi < -5.0:
        health = 'weak'
    elif roi > 0 and (not has_clv_sample or pos_clv_rate >= 52.0):
        health = 'strong'
    elif roi <= 0 or (has_clv_sample and pos_clv_rate < 50.0):
        health = 'needs_adjustment'
    else:
        health = 'needs_adjustment'

    return {
        'health': health,
        'strength': [i['message'] for i in insights if i['category'] == 'strength'],
        'weakness': [i['message'] for i in insights if i['category'] == 'weakness'],
        'risk': [i['message'] for i in insights if i['category'] == 'risk'],
    }


# --- Recommended actions -----------------------------------------------------
# Map insight messages → 1-3 concrete advisory actions. Each insight contributes
# at most one action, deduped, capped at 3.

# Keyed by insight message (the deterministic identifier). Order in this map
# defines display priority when more than 3 actions could be emitted.
_ACTION_MAP = {
    'High-edge bets not outperforming low-edge bets':
        'Raise minimum edge threshold (e.g. MIN_EDGE 3.0 → 4.0) — investigate model edge inflation',
    'Market moving against picks':
        'Increase weight on line movement when scoring bets',
    'Spread bets underperforming':
        'Spread bets are manual-only and currently underperforming — use caution',
    'Total bets underperforming':
        'Total bets are manual-only and currently underperforming — use caution',
    'Heavy-favorite bets losing money':
        'Tighten the heavy-favorite juice gate (HEAVY_FAVORITE_ODDS or STRONG_EDGE)',
    'Overall ROI is negative beyond variance band':
        'Raise the recommendation threshold band before adding new bet types',
    'Consistently beating the closing line':
        'CLV signal is strong — preserve current selection rules',
    'Underdog value detection performing well':
        'Underdog detection is working — preserve dog-side weighting',
}


def recommend_actions(insights):
    """Map insight list → up to 3 deduped, ordered action strings."""
    seen = set()
    actions = []
    for insight in insights:
        msg = insight['message']
        action = _ACTION_MAP.get(msg)
        if action and action not in seen:
            seen.add(action)
            actions.append(action)
        if len(actions) >= 3:
            break
    return actions


# --- Engine config snapshot --------------------------------------------------

def current_config():
    """Read-only snapshot of the actual engine constants. No invented fields."""
    return {
        'MIN_EDGE': MIN_EDGE,
        'STRONG_EDGE': STRONG_EDGE,
        'ELITE_EDGE': ELITE_EDGE,
        'HEAVY_FAVORITE_ODDS': HEAVY_FAVORITE_ODDS,
        'MAX_ELITE_PER_SLATE': MAX_ELITE_PER_SLATE,
        'engine_emits': ['moneyline'],
        'note': 'Engine currently emits: Moneyline only',
    }


# --- Top-level entry point ---------------------------------------------------

# --- Stale-odds count -------------------------------------------------------
# Scans upcoming games across all team sports for snapshots that are stale
# inside the 30-min pre-game window. Surfaced as a single advisory line on
# the System Tuning page; does not affect verdict / insights / actions.

def compute_stale_games_count(threshold_minutes: int = 30) -> int:
    """Count team-sport games in the pre-game window with stale odds.

    Scope: next 24 hours, all four team sports. Golf has no per-game start
    time on a snapshot basis so it's excluded. Returns 0 on any DB / import
    failure rather than blocking the page render.
    """
    try:
        from datetime import timedelta
        from apps.core.utils.multi_book import is_odds_stale
        from django.utils import timezone as _tz

        now = _tz.now()
        cutoff = now + timedelta(hours=24)

        # Lazy imports — keep this module dependency-light at import time so
        # the helper functions above are usable in tests without loading
        # every sport's Game model.
        from apps.cfb.models import Game as CFBGame
        from apps.cbb.models import Game as CBBGame
        from apps.mlb.models import Game as MLBGame
        from apps.college_baseball.models import Game as CBBaseGame

        count = 0
        for model, start_field in [
            (CFBGame, 'kickoff'),
            (CBBGame, 'tipoff'),
            (MLBGame, 'first_pitch'),
            (CBBaseGame, 'first_pitch'),
        ]:
            qs = model.objects.filter(
                **{f'{start_field}__gt': now, f'{start_field}__lte': cutoff}
            )
            for game in qs:
                if is_odds_stale(game, threshold_minutes):
                    count += 1
        return count
    except Exception:
        return 0


def compute_all(bets):
    """Single call the view uses. Stable output shape even with zero bets."""
    materialized = list(bets)
    settled = [b for b in materialized if b.result and b.result != 'pending']

    # Reuse the existing edge-bucket analyzer rather than re-bucketing here.
    # command_center.compute_edge_buckets is the canonical post-2026-04-30
    # source of truth for edge bucketing across the analytics surfaces.
    from apps.mockbets.services.command_center import compute_edge_buckets
    edge = compute_edge_buckets(materialized)  # list of 4 bucket rows

    overall = _group_stats(settled)

    ctx = {
        'overall': overall,
        'data_confidence': compute_data_confidence(materialized),
        'time_windows': compute_time_windows(materialized),
        'by_bet_type': segment_by_bet_type(materialized),
        'by_odds_range': segment_by_odds_range(materialized),
        'source_quality': segment_by_source_quality(materialized),
        'edge': edge or [],
        'config': current_config(),
        'stale_games_count': compute_stale_games_count(),
    }

    ctx['insights'] = generate_insights(ctx)
    ctx['verdict'] = compute_verdict(ctx)
    ctx['actions'] = recommend_actions(ctx['insights'])
    return ctx
