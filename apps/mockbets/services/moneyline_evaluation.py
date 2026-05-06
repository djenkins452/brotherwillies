"""Moneyline Evaluation report — staff-only diagnostic for slate post-mortems.

Generates a single, copy-paste-ready packet that a human can paste into
ChatGPT or Claude to ask "why did yesterday's slate go badly?" Pure
functions throughout. No DB writes. No mutation of MockBet rows.

Architecture:
  build_evaluation_report(bets_qs, date_from, date_to, include_manual)
      → dict with executive_summary / bets / buckets / loss_review /
        packet_markdown.

Reuse:
  - _group_stats from recommendation_performance.py (canonical ROI/CLV math)
  - compute_data_confidence from system_tuning.py (sample-size band)
  - MockBet snapshot fields (every signal lives on the row)

What it adds vs the existing analytics surfaces:
  - Date-bounded query (placement_date semantics)
  - Bucket boundaries chosen for engine-relevant thresholds:
      edge       3-4 / 4-6 / 6-8 / 8+ pp     (engine MIN_EDGE = 3.0)
      confidence 55-60 / 60-65 / 65-70 / 70+ % (engine min prob = 0.55)
      odds_type  underdog / short_fav / favorite / heavy_favorite
  - Multi-cause loss tagging (every firing rule, ordered by actionability)
  - Stale-odds-at-placement proxy (settled bet with no closing_odds_american)
  - Markdown packet ready for clipboard copy

Date semantics: PLACEMENT date inclusive on both endpoints, in the server
timezone. A bet placed at 2026-05-02 23:50 local for a game tomorrow is
part of "May 2's slate" — the slate identifier is the decision date.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from django.utils import timezone

from apps.mockbets.services.recommendation_performance import _group_stats
from apps.mockbets.services.system_tuning import compute_data_confidence


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_evaluation_report(
    bets_qs: Iterable,
    date_from: date,
    date_to: date,
    include_manual: bool = False,
) -> dict:
    """Build the full evaluation packet for the given date range.

    Caller is expected to pass a MockBet queryset (or any iterable of
    MockBet rows). The service applies the moneyline-only filter, the
    is_system_generated filter (unless include_manual is True), and the
    placement-date window itself — so the caller only has to express
    user/sport scope.
    """
    bets = list(_filter_bets(bets_qs, date_from, date_to, include_manual))

    summary = _executive_summary(bets, date_from, date_to)
    bet_rows = [_bet_detail(b) for b in bets]
    buckets = {
        'by_edge': _bucket_by_edge(bets),
        'by_confidence': _bucket_by_confidence(bets),
        'by_odds_type': _bucket_by_odds_type(bets),
        'by_source': _bucket_by_source(bets),
    }
    loss_review = _loss_review(bets)

    return {
        'date_range': {
            'from': date_from,
            'to': date_to,
            'label': _label_for_range(date_from, date_to),
            'include_manual': include_manual,
        },
        'executive_summary': summary,
        'bets': bet_rows,
        'buckets': buckets,
        'loss_review': loss_review,
        'packet_markdown': _render_packet(
            date_from, date_to, summary, bet_rows, buckets, loss_review,
            include_manual=include_manual,
        ),
    }


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _filter_bets(bets_qs, date_from: date, date_to: date, include_manual: bool):
    """Apply all the report's filters at once.

    Date range is inclusive on both endpoints. Uses the local-tz date of
    placed_at (matches the mental model "yesterday's slate" without
    timezone gymnastics — the cron and the user are on the same TZ).
    """
    qs = bets_qs.filter(bet_type='moneyline')
    if not include_manual:
        qs = qs.filter(is_system_generated=True)
    qs = qs.filter(
        placed_at__date__gte=date_from,
        placed_at__date__lte=date_to,
    )
    return qs


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------

def _executive_summary(bets, date_from, date_to) -> dict:
    """Top-of-page numbers. Always returns the same shape, even when
    bets is empty — the template never has to defensive-check."""
    settled = [b for b in bets if b.result and b.result != 'pending']
    stats = _group_stats(settled)

    # Stale-odds-at-placement proxy: settled moneyline bet whose game
    # finalized but closing_odds_american was never captured. The capture
    # path runs at game start; missing it is the strongest historical
    # signal we have that the data feed was stale during the critical
    # pre-game window.
    stale_count = sum(
        1 for b in settled
        if b.closing_odds_american is None
        and b.bet_type == 'moneyline'
    )

    data_conf = compute_data_confidence(bets)

    return {
        'date_from': date_from,
        'date_to': date_to,
        'bets_count': len(bets),
        'settled_count': len(settled),
        'pending_count': len(bets) - len(settled),
        'wins': stats['wins'],
        'losses': stats['losses'],
        'pushes': stats['pushes'],
        'win_rate': stats['win_rate'],
        'roi': stats['roi'],
        'total_stake': float(stats['total_stake']),
        'net_pl': float(stats['net_pl']),
        'positive_clv_rate': stats['positive_clv_rate'],
        'avg_clv': stats['avg_clv'],
        'clv_sample': stats['clv_sample'],
        'stale_odds_count': stale_count,
        'data_confidence_level': data_conf['level'],
    }


# ---------------------------------------------------------------------------
# Per-bet detail
# ---------------------------------------------------------------------------

def _bet_detail(b) -> dict:
    """Flatten every relevant field of a MockBet into a render-ready dict.

    The model_prob / market_prob columns reuse the snapshot fields that
    were already populated at placement time — no recomputation here, so
    the report stays consistent with what the engine actually decided on.
    """
    rec_conf = b.recommendation_confidence  # Decimal in % (e.g. 60.5)
    edge = b.expected_edge                  # Decimal in pp
    market_prob = None
    if rec_conf is not None and edge is not None:
        # Same identity as Recommendation.market_implied_pct: model − edge.
        market_prob = float(rec_conf) - float(edge)

    pl = float(b.net_result) if b.net_result is not None else None
    had_stale_capture = (
        b.is_settled
        and b.bet_type == 'moneyline'
        and b.closing_odds_american is None
    )

    game = b.game
    game_label = (
        f"{game.away_team.name} @ {game.home_team.name}"
        if game is not None and hasattr(game, 'away_team') and hasattr(game, 'home_team')
        else '—'
    )

    return {
        'bet_id': str(b.id),
        'placed_at': b.placed_at,
        'game': game_label,
        'selection': b.selection,
        'odds': b.odds_american,
        'closing_odds': b.closing_odds_american,
        'clv_cents': b.clv_cents,
        'clv_direction': b.clv_direction,
        'model_prob': float(rec_conf) if rec_conf is not None else None,
        'market_prob': market_prob,
        'edge': float(edge) if edge is not None else None,
        'tier': b.recommendation_tier or '',
        'status_reason': b.status_reason or '',
        'result': b.result,
        'profit_loss': pl,
        'odds_source': b.odds_source or 'unknown',
        'had_stale_capture': had_stale_capture,
        'loss_causes': _classify_loss_causes(b) if b.result == 'loss' else [],
        'engine_loss_reason': b.loss_reason or '',
    }


# ---------------------------------------------------------------------------
# Multi-cause loss classifier
# ---------------------------------------------------------------------------

# Order matters: most actionable first. The first firing rule is the
# "primary" cause; the rest become secondary tags.
_LOSS_CAUSE_RULES = (
    'negative_clv',
    'stale_odds',
    'thin_edge',
    'heavy_juice',
    'low_confidence',
    'market_moved_against',
    'variance',
)

_LOSS_CAUSE_LABELS = {
    'negative_clv':         'Negative CLV — beat by the close',
    'stale_odds':           'Stale odds — no fresh close captured',
    'thin_edge':            'Thin edge — under 4pp',
    'heavy_juice':          'Heavy juice — odds at -150 or worse',
    'low_confidence':       'Low confidence — model under 60%',
    'market_moved_against': 'Market moved against the pick',
    'variance':             'Variance — solid bet that did not land',
    'unknown':              'Unknown — no decision-layer snapshot',
}


def _classify_loss_causes(bet) -> list:
    """Return the list of all firing causes, in actionability order.

    Empty list for non-losses. For losses, always returns at least one
    string ('unknown' if nothing else fires).
    """
    if bet.result != 'loss':
        return []

    causes = []

    if bet.clv_direction == 'negative':
        causes.append('negative_clv')

    # Stale-odds proxy: settled bet with no closing odds captured.
    if (
        bet.is_settled
        and bet.bet_type == 'moneyline'
        and bet.closing_odds_american is None
    ):
        causes.append('stale_odds')

    if bet.expected_edge is not None and float(bet.expected_edge) < 4.0:
        causes.append('thin_edge')

    if bet.odds_american is not None and bet.odds_american <= -150:
        causes.append('heavy_juice')

    if (
        bet.recommendation_confidence is not None
        and float(bet.recommendation_confidence) < 60.0
    ):
        causes.append('low_confidence')

    if bet.loss_reason == 'market_movement':
        causes.append('market_moved_against')

    if bet.loss_reason == 'variance':
        causes.append('variance')

    if not causes:
        causes.append('unknown')

    return causes


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

_EDGE_BUCKETS = (
    ('3-4pp', 3.0, 4.0),
    ('4-6pp', 4.0, 6.0),
    ('6-8pp', 6.0, 8.0),
    ('8+pp',  8.0, float('inf')),
)


def _bucket_by_edge(bets) -> list:
    """Edge buckets keyed off MockBet.expected_edge (pp). Excludes
    bets with no recorded edge — they can't be honestly bucketed."""
    settled = [
        b for b in bets
        if b.result and b.result != 'pending' and b.expected_edge is not None
    ]
    rows = []
    for label, lo, hi in _EDGE_BUCKETS:
        in_bucket = [b for b in settled if lo <= float(b.expected_edge) < hi]
        rows.append(_bucket_row(label, in_bucket))
    return rows


_CONFIDENCE_BUCKETS = (
    ('55-60%', 55.0, 60.0),
    ('60-65%', 60.0, 65.0),
    ('65-70%', 65.0, 70.0),
    ('70%+',   70.0, float('inf')),
)


def _bucket_by_confidence(bets) -> list:
    """Model-confidence buckets keyed off recommendation_confidence (%)."""
    settled = [
        b for b in bets
        if b.result and b.result != 'pending'
        and b.recommendation_confidence is not None
    ]
    rows = []
    for label, lo, hi in _CONFIDENCE_BUCKETS:
        in_bucket = [
            b for b in settled
            if lo <= float(b.recommendation_confidence) < hi
        ]
        rows.append(_bucket_row(label, in_bucket))
    return rows


def _classify_odds_type(odds: Optional[int]) -> Optional[str]:
    """4 buckets per spec. Implementation chosen to be unambiguous at
    every American-odds value:
      underdog        → odds_american >= +100
      short_favorite  → -150 < odds < +100  (the 'pick-em' zone)
      favorite        → -300 <= odds <= -150
      heavy_favorite  → odds < -300
    """
    if odds is None:
        return None
    if odds >= 100:
        return 'underdog'
    if odds > -150:
        return 'short_favorite'
    if odds >= -300:
        return 'favorite'
    return 'heavy_favorite'


_ODDS_TYPE_KEYS = ('underdog', 'short_favorite', 'favorite', 'heavy_favorite')
_ODDS_TYPE_LABELS = {
    'underdog':       'Underdog (+100 and up)',
    'short_favorite': 'Short Favorite (-149 to +99)',
    'favorite':       'Favorite (-300 to -150)',
    'heavy_favorite': 'Heavy Favorite (-301 and below)',
}


def _bucket_by_odds_type(bets) -> list:
    settled = [b for b in bets if b.result and b.result != 'pending']
    by_key = {k: [] for k in _ODDS_TYPE_KEYS}
    for b in settled:
        key = _classify_odds_type(b.odds_american)
        if key is not None:
            by_key[key].append(b)
    return [
        _bucket_row(_ODDS_TYPE_LABELS[k], by_key[k])
        for k in _ODDS_TYPE_KEYS
    ]


_SOURCE_KEYS = ('odds_api', 'espn', 'manual', 'unknown')
_SOURCE_LABELS = {
    'odds_api': 'Odds API (primary)',
    'espn':     'ESPN (fallback)',
    'manual':   'Manual',
    'unknown':  'Unknown / pre-feature',
}


def _bucket_by_source(bets) -> list:
    settled = [b for b in bets if b.result and b.result != 'pending']
    by_key = {k: [] for k in _SOURCE_KEYS}
    for b in settled:
        key = b.odds_source if b.odds_source in _SOURCE_KEYS else 'unknown'
        by_key[key].append(b)
    return [
        _bucket_row(_SOURCE_LABELS[k], by_key[k])
        for k in _SOURCE_KEYS
    ]


def _bucket_row(label: str, in_bucket: list) -> dict:
    """Compact per-bucket KPI row. _group_stats handles the math; we
    just attach the label and pretty-print floats."""
    s = _group_stats(in_bucket)
    return {
        'label': label,
        'bets': s['total_bets'],
        'wins': s['wins'],
        'losses': s['losses'],
        'win_rate': s['win_rate'],
        'roi': s['roi'],
        'net_pl': float(s['net_pl']),
        'positive_clv_rate': s['positive_clv_rate'],
        'clv_sample': s['clv_sample'],
    }


# ---------------------------------------------------------------------------
# Loss review (per-bet, losses only, ordered by impact)
# ---------------------------------------------------------------------------

def _loss_review(bets) -> list:
    """Losses ordered by stake DESC so the biggest hits surface first.
    Each row carries the multi-cause classification so the markdown
    packet can render `causes: negative_clv, thin_edge` per bet."""
    losses = [b for b in bets if b.result == 'loss']
    losses.sort(key=lambda b: float(b.stake_amount), reverse=True)
    out = []
    for b in losses:
        causes = _classify_loss_causes(b)
        out.append({
            'bet_id': str(b.id),
            'game': _bet_detail(b)['game'],
            'selection': b.selection,
            'odds': b.odds_american,
            'stake': float(b.stake_amount),
            'profit_loss': float(b.net_result) if b.net_result is not None else None,
            'edge': float(b.expected_edge) if b.expected_edge is not None else None,
            'confidence': (
                float(b.recommendation_confidence)
                if b.recommendation_confidence is not None else None
            ),
            'clv_direction': b.clv_direction or '',
            'causes': causes,
            'primary_cause': causes[0] if causes else 'unknown',
            'engine_loss_reason': b.loss_reason or '',
        })
    return out


# ---------------------------------------------------------------------------
# Date-range labels
# ---------------------------------------------------------------------------

def _label_for_range(date_from: date, date_to: date) -> str:
    """Human-readable label for the canonical quick-ranges. Falls
    through to 'YYYY-MM-DD to YYYY-MM-DD' for arbitrary custom ranges."""
    today = timezone.localdate()
    if date_from == date_to:
        if date_from == today:
            return 'Today'
        if date_from == today - timedelta(days=1):
            return 'Yesterday'
        return date_from.isoformat()
    if date_to == today:
        diff = (date_to - date_from).days + 1
        if diff == 7:
            return 'Last 7 Days'
        if diff == 30:
            return 'Last 30 Days'
    return f'{date_from.isoformat()} to {date_to.isoformat()}'


# ---------------------------------------------------------------------------
# Markdown packet
# ---------------------------------------------------------------------------

def _render_packet(
    date_from, date_to, summary, bet_rows, buckets, loss_review,
    *, include_manual: bool,
) -> str:
    """Render the copy-paste markdown blob. Single source of truth
    with the on-page render — both reads from the same dicts. Format
    designed for ChatGPT/Claude to interpret without preamble."""
    label = _label_for_range(date_from, date_to)
    scope = 'all bets (system + manual)' if include_manual else 'system-generated only'

    lines = []
    lines.append('# Brother Willies Moneyline Evaluation Packet')
    lines.append('')

    # --- Date range
    lines.append('## Date Range')
    lines.append(f'{date_from.isoformat()} to {date_to.isoformat()} ({label})')
    lines.append(f'Scope: {scope}')
    lines.append('')

    # --- Executive summary
    lines.append('## Executive Summary')
    lines.append(f'- Total bets: {summary["bets_count"]} '
                 f'(settled {summary["settled_count"]}, pending {summary["pending_count"]})')
    lines.append(f'- Wins: {summary["wins"]}')
    lines.append(f'- Losses: {summary["losses"]}')
    lines.append(f'- Pushes: {summary["pushes"]}')
    lines.append(f'- Win rate: {summary["win_rate"]:.1f}%')
    lines.append(f'- ROI: {summary["roi"]:.1f}%')
    lines.append(f'- Total staked: ${summary["total_stake"]:.2f}')
    lines.append(f'- Net P/L: ${summary["net_pl"]:.2f}')
    lines.append(
        f'- Positive CLV rate: {summary["positive_clv_rate"]:.1f}% '
        f'(sample: {summary["clv_sample"]})'
    )
    lines.append(f'- Avg CLV: {summary["avg_clv"]:.4f}')
    lines.append(f'- Stale odds count: {summary["stale_odds_count"]}')
    lines.append(f'- Data confidence: {summary["data_confidence_level"]}')
    lines.append('')

    # --- Per-bet table
    lines.append('## Recommended Bets')
    if bet_rows:
        lines.append(
            '| Game | Pick | Odds | Close | CLV | Model% | Market% | Edge | '
            'Tier | Result | P/L | Source | Stale |'
        )
        lines.append(
            '|------|------|------|-------|-----|--------|---------|------|'
            '------|--------|-----|--------|-------|'
        )
        for r in bet_rows:
            lines.append(
                f'| {r["game"]} | {r["selection"]} | '
                f'{_fmt_odds(r["odds"])} | '
                f'{_fmt_odds(r["closing_odds"])} | '
                f'{_fmt_clv(r["clv_cents"], r["clv_direction"])} | '
                f'{_fmt_pct(r["model_prob"])} | '
                f'{_fmt_pct(r["market_prob"])} | '
                f'{_fmt_pp(r["edge"])} | '
                f'{r["tier"] or "—"} | '
                f'{r["result"]} | '
                f'{_fmt_money(r["profit_loss"])} | '
                f'{r["odds_source"]} | '
                f'{"⚠️" if r["had_stale_capture"] else ""} |'
            )
    else:
        lines.append('_No bets in this range._')
    lines.append('')

    # --- Bucket performance
    lines.append('## Bucket Performance')
    for title, key in (
        ('By Edge', 'by_edge'),
        ('By Model Confidence', 'by_confidence'),
        ('By Odds Type', 'by_odds_type'),
        ('By Source', 'by_source'),
    ):
        lines.append(f'### {title}')
        lines.append('| Bucket | Bets | W-L | Win % | ROI | CLV+ % |')
        lines.append('|--------|------|-----|-------|-----|--------|')
        for row in buckets[key]:
            lines.append(
                f'| {row["label"]} | {row["bets"]} | '
                f'{row["wins"]}-{row["losses"]} | '
                f'{row["win_rate"]:.1f}% | {row["roi"]:.1f}% | '
                f'{row["positive_clv_rate"]:.1f}% (n={row["clv_sample"]}) |'
            )
        lines.append('')

    # --- Loss review
    lines.append('## Loss Review')
    if loss_review:
        for loss in loss_review:
            cause_strs = [_LOSS_CAUSE_LABELS.get(c, c) for c in loss['causes']]
            lines.append(f'- **{loss["game"]}** — {loss["selection"]} '
                         f'at {_fmt_odds(loss["odds"])}, P/L {_fmt_money(loss["profit_loss"])}')
            lines.append(f'  - Edge: {_fmt_pp(loss["edge"])}, '
                         f'Confidence: {_fmt_pct(loss["confidence"])}, '
                         f'CLV: {loss["clv_direction"] or "n/a"}')
            lines.append(f'  - Causes: {", ".join(cause_strs)}')
            if loss['engine_loss_reason']:
                lines.append(f'  - Engine reason: {loss["engine_loss_reason"]}')
    else:
        lines.append('_No losses in this range._')
    lines.append('')

    # --- Questions
    lines.append('## Questions for Analysis')
    lines.append('1. What patterns explain this slate\'s results?')
    lines.append('2. Which thresholds (MIN_EDGE, MIN_PROBABILITY_FOR_RECOMMENDED, '
                 'HEAVY_FAVORITE_ODDS) should be tightened?')
    lines.append('3. Are losses more likely variance or model weakness?')
    lines.append('4. Should favorites/underdogs be treated differently?')
    lines.append('5. What should we change before the next slate?')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Markdown formatters (small helpers — kept private to stay deterministic)
# ---------------------------------------------------------------------------

def _fmt_odds(o):
    if o is None:
        return '—'
    return f'+{o}' if o > 0 else str(o)


def _fmt_pct(p):
    if p is None:
        return '—'
    return f'{p:.1f}%'


def _fmt_pp(p):
    if p is None:
        return '—'
    return f'{p:+.1f}pp'


def _fmt_money(m):
    if m is None:
        return '—'
    sign = '-' if m < 0 else ''
    return f'{sign}${abs(m):.2f}'


def _fmt_clv(clv, direction):
    """Format CLV with an explicit sign. Bug-fix 2026-05-06: the prior
    implementation prepended a sign char ('+' / '-') based on `direction`
    on top of the value's natural sign, producing '--0.0200' for negative
    CLVs. The `:+.4f` format spec already handles the sign, so we just
    let it. `direction` is no longer needed for the output but kept in
    the signature so callers don't break.
    """
    if clv is None:
        return '—'
    return f'{clv:+.4f}'
