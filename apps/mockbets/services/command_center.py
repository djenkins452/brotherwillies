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

    Also surfaces coverage rates so the UI can tell users how complete
    the historical record is — e.g. "CLV coverage: 62% of bets". This
    is the operational view of how much backfill has been able to do.
    """
    has_settled = bool(settled)
    settled_with_clv = [b for b in settled if b.clv_cents is not None]
    has_clv = bool(settled_with_clv)
    materialized_bets = materialize(bets)
    bets_with_rec_snapshot = [
        b for b in materialized_bets
        if (b.recommendation_status or b.recommendation_tier)
    ]
    has_rec_data = bool(bets_with_rec_snapshot)
    return {
        'has_any_bets': bool(bets),
        'has_settled_bets': has_settled,
        'has_clv_data': has_clv,
        'has_recommendation_data': has_rec_data,
        # Coverage % — useful for "62% of bets have CLV captured" indicator
        'clv_coverage_pct': (
            round(len(settled_with_clv) / len(settled) * 100, 1)
            if settled else 0.0
        ),
        'rec_coverage_pct': (
            round(len(bets_with_rec_snapshot) / len(materialized_bets) * 100, 1)
            if materialized_bets else 0.0
        ),
    }


def materialize(bets) -> list:
    """Materialize a queryset once; pass list back for repeated iteration."""
    if isinstance(bets, list):
        return bets
    return list(bets)


# --- System Verdict ---------------------------------------------------------
# A single "is the system working?" signal users can act on. Built from the
# already-computed CLV %+, ROI, and sample size. Deterministic, no AI.
#
# Verdict tiers (priority order — first match wins):
#   STRONG  — CLV %+ ≥ 60 AND ROI > 0 AND sample ≥ 25
#   WEAK    — CLV %+ < 50 OR ROI < 0
#   NEUTRAL — everything else (mixed CLV, near-breakeven ROI, etc.)
#
# Confidence level is sample-size-bound regardless of verdict — a STRONG
# verdict on 8 bets is still LOW confidence. This is the honest framing:
# "the early signal is good, but we don't yet have enough data to trust it."

_SAMPLE_LOW = 30
_SAMPLE_MED = 75
_VERDICT_STRONG_CLV = 60.0
_VERDICT_STRONG_SAMPLE = 25
_VERDICT_WEAK_CLV = 50.0
_NEAR_BREAKEVEN_ROI = 2.0  # |ROI| ≤ 2pp counts as "near 0"


def compute_system_verdict(cc: dict) -> dict:
    """Trust signal: should the user lean on the system's recommendations?

    Reads only fields already on the cc dict — never re-touches the database
    or the model layer. This means the verdict refreshes whenever the rest
    of the dashboard does.
    """
    kpis = cc['kpis']
    clv = cc['clv']
    settled = kpis['settled_count']
    roi = kpis['roi']
    clv_rate = clv['positive_rate'] if clv['sample_size'] else None
    clv_sample = clv['sample_size']

    reasons = []
    warnings = []

    # Confidence band based on sample size — independent of verdict tier.
    if settled < _SAMPLE_LOW:
        confidence = 'LOW'
        warnings.append(
            f"Small sample size ({settled} settled bet{'s' if settled != 1 else ''}) — "
            "patterns may be variance, not skill"
        )
    elif settled < _SAMPLE_MED:
        confidence = 'MEDIUM'
    else:
        confidence = 'HIGH'

    if clv_sample == 0 and settled > 0:
        warnings.append(
            "No CLV data captured yet — verdict can't fully measure whether "
            "the system is beating the market"
        )

    # WEAK takes priority — if the data is screaming "this isn't working",
    # don't sugar-coat it with NEUTRAL.
    is_weak = (
        roi < 0
        or (clv_rate is not None and clv_rate < _VERDICT_WEAK_CLV)
    )

    # STRONG requires all three: positive CLV, positive ROI, and enough
    # bets to be more than a streak.
    is_strong = (
        clv_rate is not None
        and clv_rate >= _VERDICT_STRONG_CLV
        and roi > 0
        and settled >= _VERDICT_STRONG_SAMPLE
    )

    if is_weak:
        verdict = 'WEAK'
        if roi < 0:
            reasons.append(f"Negative simulated ROI ({roi:.1f}%)")
        if clv_rate is not None and clv_rate < _VERDICT_WEAK_CLV:
            reasons.append(f"Losing the closing line ({clv_rate:.0f}% positive CLV)")
    elif is_strong:
        verdict = 'STRONG'
        reasons.append(f"Beating market (CLV {clv_rate:.0f}%)")
        reasons.append(f"Positive simulated ROI (+{roi:.1f}%)")
        if settled < _SAMPLE_MED:
            reasons.append(f"Encouraging signal across {settled} settled bets")
    else:
        verdict = 'NEUTRAL'
        if clv_rate is None:
            reasons.append("CLV data not yet captured — verdict provisional")
        elif clv_rate < _VERDICT_STRONG_CLV:
            reasons.append(f"Mixed CLV signal ({clv_rate:.0f}% positive)")
        if abs(roi) <= _NEAR_BREAKEVEN_ROI:
            reasons.append(f"ROI near breakeven ({roi:+.1f}%)")
        if settled < _VERDICT_STRONG_SAMPLE:
            reasons.append(f"Sample under threshold ({settled} of {_VERDICT_STRONG_SAMPLE} needed for STRONG)")

    return {
        'verdict': verdict,
        'confidence_level': confidence,
        'reasons': reasons,
        'warnings': warnings,
        # Inputs surfaced for the UI so the panel can show its own math
        'inputs': {
            'roi': roi,
            'clv_positive_rate': clv_rate,
            'clv_sample': clv_sample,
            'settled_bets': settled,
        },
    }


# --- Edge Buckets -----------------------------------------------------------
# "Where is my edge actually working?" — group settled bets by their snapshot
# expected_edge value, compute win rate, ROI, and CLV %+ per bucket. If the
# 4-6pp bucket consistently underperforms 6pp+, the user has empirical
# evidence that the MIN_EDGE threshold could be raised.

_EDGE_BUCKETS = [
    {'label': '0–2pp', 'min': 0.0, 'max': 2.0},
    {'label': '2–4pp', 'min': 2.0, 'max': 4.0},
    {'label': '4–6pp', 'min': 4.0, 'max': 6.0},
    {'label': '6pp+',  'min': 6.0, 'max': float('inf')},
]


def compute_edge_buckets(bets) -> list:
    """Per-edge-bucket performance rollup.

    Excludes bets with no expected_edge (pre-snapshot) — they can't be
    bucketed honestly. Excludes pending bets — no result yet to measure.
    """
    materialized = materialize(bets)
    settled_with_edge = [
        b for b in materialized
        if b.result and b.result != 'pending' and b.expected_edge is not None
    ]
    rows = []
    for bucket in _EDGE_BUCKETS:
        in_bucket = [
            b for b in settled_with_edge
            if bucket['min'] <= float(b.expected_edge) < bucket['max']
        ]
        decisive = [b for b in in_bucket if b.result in ('win', 'loss')]
        wins = sum(1 for b in decisive if b.result == 'win')
        with_clv = [b for b in in_bucket if b.clv_cents is not None]
        positive_clv = sum(1 for b in with_clv if b.clv_direction == 'positive')
        stake_total = sum(float(b.stake_amount) for b in in_bucket)
        net_total = sum(float(b.net_result or 0) for b in in_bucket)
        rows.append({
            'range': bucket['label'],
            'count': len(in_bucket),
            'win_rate': round(wins / len(decisive) * 100, 1) if decisive else 0.0,
            'roi': round(net_total / stake_total * 100, 1) if stake_total else 0.0,
            'clv_positive_rate': round(positive_clv / len(with_clv) * 100, 1) if with_clv else 0.0,
            'clv_sample': len(with_clv),
        })
    return rows


# --- Decision Quality Breakdown --------------------------------------------
# Aggregate counts of MockBet.decision_quality values across the slate.
# Used by the AI summary to answer "did results match decision quality?"

def compute_decision_quality_breakdown(bets) -> dict:
    """Counts of decision_quality categories across settled bets."""
    materialized = materialize(bets)
    counts = {'perfect': 0, 'lucky': 0, 'unlucky': 0, 'bad': 0, 'neutral': 0, 'unclassified': 0}
    classified = 0
    for b in materialized:
        if b.result == 'pending':
            continue
        dq = b.decision_quality or ''
        if dq:
            counts[dq] = counts.get(dq, 0) + 1
            classified += 1
        else:
            counts['unclassified'] += 1
    total = sum(counts.values())
    pcts = {
        k: round(v / total * 100, 1) if total else 0.0
        for k, v in counts.items()
    }
    return {
        'counts': counts,
        'pcts': pcts,
        'classified': classified,
        'total': total,
    }


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

    cc = {
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
    # Decision-intelligence layer — sits ON TOP of the assembled data, so it
    # can read everything else and produce trust signals.
    cc['system_verdict'] = compute_system_verdict(cc)
    cc['edge_buckets'] = compute_edge_buckets(materialized)
    cc['decision_quality'] = compute_decision_quality_breakdown(materialized)
    return cc
