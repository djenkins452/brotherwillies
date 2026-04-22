"""How good are the system's recommendations actually?

Computes performance metrics from the snapshot fields on MockBet — the
denormalized copies of recommendation_status / recommendation_tier / edge /
confidence captured at bet placement. Using the snapshot insulates analytics
from future rule changes: "what the system believed at the time" is locked
into the MockBet row.

Grouped outputs:
  - by status: recommended vs not_recommended
  - by tier: elite / strong / standard

System confidence score is a weighted combination of win rate, ROI, and
sample size — see compute_system_confidence_score() for the formula.
"""
from collections import defaultdict
from decimal import Decimal
from typing import Iterable, List


_TIER_KEYS = ('elite', 'strong', 'standard')
_STATUS_KEYS = ('recommended', 'not_recommended')


def _settled(bets):
    return [b for b in bets if b.result and b.result != 'pending']


def _group_stats(bets):
    """Core math for a single group — used by both status and tier rollups."""
    stake = Decimal('0')
    winnings = Decimal('0')   # stake returned + profit on wins
    edge_sum = Decimal('0')
    edge_count = 0
    wins = losses = pushes = 0
    for b in bets:
        stake += b.stake_amount
        if b.result == 'win':
            winnings += b.stake_amount + (b.simulated_payout or Decimal('0'))
            wins += 1
        elif b.result == 'push':
            winnings += b.stake_amount
            pushes += 1
        elif b.result == 'loss':
            losses += 1
        if b.expected_edge is not None:
            edge_sum += b.expected_edge
            edge_count += 1
    net_pl = winnings - stake
    total = wins + losses + pushes
    # ROI excludes pushes from the denominator since a push is a return-of-stake
    # event — the bet neither made nor lost money, so including it in the
    # denominator would dilute the signal for the rows that actually resolved.
    decisive = wins + losses
    return {
        'total_bets': total,
        'wins': wins,
        'losses': losses,
        'pushes': pushes,
        'total_stake': stake,
        'net_pl': net_pl,
        'win_rate': (wins / decisive * 100.0) if decisive else 0.0,
        'roi': (float(net_pl) / float(stake) * 100.0) if stake else 0.0,
        'avg_edge': (float(edge_sum) / edge_count) if edge_count else 0.0,
    }


def compute_performance_by_status(bets: Iterable) -> dict:
    """Group settled bets by recommendation_status snapshot."""
    settled = _settled(bets)
    buckets = defaultdict(list)
    for b in settled:
        key = b.recommendation_status or 'unlabeled'
        buckets[key].append(b)
    # Always emit both keys so the UI can render zeros instead of missing rows.
    result = {k: _group_stats(buckets.get(k, [])) for k in _STATUS_KEYS}
    if buckets.get('unlabeled'):
        result['unlabeled'] = _group_stats(buckets['unlabeled'])
    return result


def compute_performance_by_tier(bets: Iterable) -> dict:
    """Group settled bets by recommendation_tier snapshot."""
    settled = _settled(bets)
    buckets = defaultdict(list)
    for b in settled:
        key = b.recommendation_tier or 'unlabeled'
        buckets[key].append(b)
    result = {k: _group_stats(buckets.get(k, [])) for k in _TIER_KEYS}
    if buckets.get('unlabeled'):
        result['unlabeled'] = _group_stats(buckets['unlabeled'])
    return result


# --- System confidence score -------------------------------------------------
# How much should a user trust the system? Weighted combination of win rate,
# ROI, and sample size so tiny-sample lucky streaks don't inflate the score.

_SAMPLE_SIZE_TARGET = 50   # bets required for full sample-size credit
_ROI_SCALE = 20.0          # ±20% ROI maps to full ±1.0 for the normalized term


def compute_system_confidence_score(bets: Iterable) -> dict:
    """Compute a 0-100 score plus its component parts for UI display.

    Formula:
        score = (win_rate_term * 0.5 + roi_term * 0.3 + sample_term * 0.2) * 100
    where each term is in [0, 1]. ROI below zero pulls its term toward 0.
    """
    settled = _settled(bets)
    stats = _group_stats(settled)

    win_rate = stats['win_rate'] / 100.0                 # 0-1
    roi_normalized = max(-1.0, min(1.0, stats['roi'] / _ROI_SCALE))
    roi_term = (roi_normalized + 1.0) / 2.0              # 0-1
    sample_term = min(1.0, stats['total_bets'] / float(_SAMPLE_SIZE_TARGET))

    score = (win_rate * 0.5 + roi_term * 0.3 + sample_term * 0.2) * 100.0

    return {
        'score': round(score, 1),
        'components': {
            'win_rate_pct': round(stats['win_rate'], 1),
            'roi_pct': round(stats['roi'], 1),
            'total_bets': stats['total_bets'],
        },
        'thresholds': {
            'sample_size_target': _SAMPLE_SIZE_TARGET,
            'roi_scale': _ROI_SCALE,
        },
    }


def compute_all(bets) -> dict:
    """Convenience: everything the analytics widget needs in one call."""
    materialized: List = list(bets)
    return {
        'by_status': compute_performance_by_status(materialized),
        'by_tier': compute_performance_by_tier(materialized),
        'system_confidence': compute_system_confidence_score(materialized),
    }
