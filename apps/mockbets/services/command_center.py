"""Mock-bet analytics command center — single facade for the dashboard.

Aggregates the existing analytics services (`compute_kpis`,
`compute_performance_by_status`, `compute_performance_by_tier`,
`compute_system_confidence_score`, `compute_loss_breakdown`) into one
structured object so the analytics view + AI summary speak from the
same fact set.

What's NEW here vs the existing services:
  - Hero KPI bundle (the 6-card top row)
  - CLV extremes (best / worst single bet)
  - Result drivers (best wins, worst losses, biggest misses, best validations)
  - Per-bet ledger with display-ready fields
  - Capability flags (has_settled_bets / has_clv_data / has_recommendation_data)
    so templates can render the right empty-state copy without computing
    those checks themselves.

Everything else (per-status table, per-tier table, system confidence
score, loss breakdown) is delegated to the existing services. This file
should NEVER reimplement math that already lives elsewhere.
"""
from decimal import Decimal
from typing import List

from .analytics import compute_kpis
from .recommendation_performance import (
    compute_loss_breakdown,
    compute_performance_by_status,
    compute_performance_by_tier,
    compute_system_confidence_score,
)


_TOP_N = 5


def _settled(bets):
    return [b for b in bets if b.result and b.result != 'pending']


def _record_string(kpis: dict) -> str:
    """'12-7-2' or '12-7' if no pushes."""
    w, l, p = kpis['wins'], kpis['losses'], kpis['pushes']
    return f"{w}-{l}-{p}" if p else f"{w}-{l}"


def _build_hero(kpis: dict, system_confidence: dict, clv: dict) -> dict:
    """Six high-impact cards for the top of the dashboard."""
    return {
        'net_pl': kpis['net_pl'],
        'roi': kpis['roi'],
        'record': _record_string(kpis),
        'win_rate': kpis['win_pct'],
        # CLV positive rate — across the whole settled set, not by bucket
        'clv_positive_rate': clv['positive_rate'],
        'clv_sample': clv['sample_size'],
        'system_confidence_score': system_confidence['score'],
        'system_confidence_total_bets': system_confidence['components']['total_bets'],
    }


def _build_clv_block(settled) -> dict:
    """Aggregate CLV stats for the whole filtered set + best/worst single bets."""
    with_clv = [b for b in settled if b.clv_cents is not None]
    sample = len(with_clv)
    if not sample:
        return {
            'sample_size': 0,
            'positive_count': 0,
            'positive_rate': 0.0,
            'avg_clv': 0.0,
            'best': None,
            'worst': None,
        }
    positive_count = sum(1 for b in with_clv if b.clv_direction == 'positive')
    avg_clv = sum(b.clv_cents for b in with_clv) / sample
    best = max(with_clv, key=lambda b: b.clv_cents)
    worst = min(with_clv, key=lambda b: b.clv_cents)
    return {
        'sample_size': sample,
        'positive_count': positive_count,
        'positive_rate': round(positive_count / sample * 100, 1),
        'avg_clv': round(avg_clv, 4),
        'best': best,
        'worst': worst,
    }


def _build_drivers(settled) -> dict:
    """Identify the bets that explain the result.

    A user opening the page should be able to see — at a glance — which
    bets did the work. These lists are the hot wash.
    """
    wins = [b for b in settled if b.result == 'win']
    losses = [b for b in settled if b.result == 'loss']
    with_clv = [b for b in settled if b.clv_cents is not None]

    # Top 5 wins by net profit (simulated_payout is the profit on a win).
    best_wins = sorted(
        wins,
        key=lambda b: float(b.simulated_payout or 0),
        reverse=True,
    )[:_TOP_N]

    # Top 5 losses by stake (= the loss amount).
    worst_losses = sorted(
        losses,
        key=lambda b: float(b.stake_amount),
        reverse=True,
    )[:_TOP_N]

    # CLV extremes — top 5 each.
    best_clv = sorted(with_clv, key=lambda b: b.clv_cents, reverse=True)[:_TOP_N]
    worst_clv = sorted(with_clv, key=lambda b: b.clv_cents)[:_TOP_N]

    # Biggest misses: losses on bets the system flagged as recommended/elite.
    # If neither field is populated (pre-snapshot bets), the bet is excluded.
    misses = [
        b for b in losses
        if b.recommendation_status == 'recommended' or b.recommendation_tier == 'elite'
    ]
    biggest_misses = sorted(misses, key=lambda b: float(b.stake_amount), reverse=True)[:_TOP_N]

    # Best validations: wins on recommended/elite bets WITH positive CLV.
    # Both signals firing on the same bet = the strongest possible signal
    # that the system did its job.
    validations = [
        b for b in wins
        if b.clv_direction == 'positive'
        and (b.recommendation_status == 'recommended' or b.recommendation_tier == 'elite')
    ]
    best_validations = sorted(
        validations,
        key=lambda b: float(b.simulated_payout or 0),
        reverse=True,
    )[:_TOP_N]

    return {
        'best_wins': best_wins,
        'worst_losses': worst_losses,
        'best_clv': best_clv,
        'worst_clv': worst_clv,
        'biggest_misses': biggest_misses,
        'best_validations': best_validations,
    }


def _build_ledger(bets) -> List:
    """Full bet list with the display-ready fields the template will render.

    We sort newest-first so the most recent bet is at the top — operators
    debug today's slate by scrolling down, not by hunting through history.
    Pending bets appear before settled because they're what's still in play.
    """
    materialized = list(bets)
    return sorted(
        materialized,
        key=lambda b: (b.result != 'pending', -b.placed_at.timestamp()),
    )


def _capabilities(bets, settled) -> dict:
    """Flags so the template can render correct empty-state copy.

    `has_clv_data` is True only when at least one settled bet has CLV
    captured. `has_recommendation_data` is True when at least one bet
    has snapshot status — pre-migration bets won't.
    """
    has_settled = bool(settled)
    has_clv = any(b.clv_cents is not None for b in settled)
    has_rec_data = any(
        (b.recommendation_status or b.recommendation_tier)
        for b in materialize(bets)
    )
    return {
        'has_any_bets': bool(bets),
        'has_settled_bets': has_settled,
        'has_clv_data': has_clv,
        'has_recommendation_data': has_rec_data,
    }


def materialize(bets) -> list:
    """Materialize a queryset once; pass list back for repeated iteration."""
    if isinstance(bets, list):
        return bets
    return list(bets)


def build_command_center(bets) -> dict:
    """Single structured analytics object for the dashboard.

    The template + AI summary read from this. If a section is missing data,
    its sub-dict is still present with empty/zero values plus a capability
    flag — the template renders the right empty-state copy from the flag.
    """
    materialized = materialize(bets)
    settled = _settled(materialized)

    kpis = compute_kpis(materialized)
    system_confidence = compute_system_confidence_score(materialized)
    clv = _build_clv_block(settled)
    by_status = compute_performance_by_status(materialized)
    by_tier = compute_performance_by_tier(materialized)
    loss_breakdown = compute_loss_breakdown(materialized)
    drivers = _build_drivers(settled)
    ledger = _build_ledger(materialized)
    capabilities = _capabilities(materialized, settled)

    return {
        'kpis': kpis,
        'hero': _build_hero(kpis, system_confidence, clv),
        'clv': clv,
        'by_status': by_status,
        'by_tier': by_tier,
        'system_confidence': system_confidence,
        'loss_breakdown': loss_breakdown,
        'drivers': drivers,
        'ledger': ledger,
        'capabilities': capabilities,
    }
