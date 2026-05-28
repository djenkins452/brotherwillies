"""Three-Population Audit — pure-function service.

Splits a queryset of MLB moneyline MockBets into three populations and
emits structured metrics + a rendered plaintext report.

  1. ALL ACTUAL BETS PLACED — every placed MLB moneyline bet (no filters).
  2. TRUE SYSTEM-APPROVED BETS — bets that would have passed
     `is_bulk_moneyline_eligible` under current production rules:
       - is_system_generated=True OR linked to a real BettingRecommendation
       - recommendation_status == 'recommended'
       - linked recommendation.lane == 'core'
       - placed on/after MODEL_RULES_EFFECTIVE_DATE
       - complete decision-layer snapshot
  3. MANUAL / CONTAMINATED BETS — (1) minus (2).

For each population emits: count, W-L-P (+pending), win%, ROI%, net P/L,
avg edge (pp), avg CLV (cents, primary-source only), CLV+ %, sample size.

Used by:
  - apps/mockbets/management/commands/audit_three_populations.py (CLI/Railway-run)
  - apps/mockbets/views.py::three_population_audit_view (staff-only HTTP)

READ-ONLY. Pure functions. No DB writes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from apps.mockbets.services.moneyline_evaluation import MODEL_RULES_EFFECTIVE_DATE


# ---------------------------------------------------------------------------
# Population predicate
# ---------------------------------------------------------------------------

def _is_system_or_linked(bet) -> bool:
    """True if the bet is system-generated OR linked to a real recommendation."""
    if bool(getattr(bet, 'is_system_generated', False)):
        return True
    return getattr(bet, 'recommendation_id', None) is not None


def is_true_system_approved(bet) -> bool:
    """Population 2: passes every production-equivalence filter.

    Mirrors `is_bulk_moneyline_eligible` semantics post-fix.
    """
    if not _is_system_or_linked(bet):
        return False

    placed_date = bet.placed_at.date() if bet.placed_at else None
    if placed_date is None or placed_date < MODEL_RULES_EFFECTIVE_DATE:
        return False

    if (getattr(bet, 'recommendation_status', '') or '') != 'recommended':
        return False

    rec = getattr(bet, 'recommendation', None)
    if rec is None:
        return False
    if getattr(rec, 'lane', None) != 'core':
        return False

    if not (getattr(bet, 'recommendation_tier', '') or ''):
        return False
    if getattr(bet, 'expected_edge', None) is None:
        return False
    if getattr(bet, 'recommendation_confidence', None) is None:
        return False

    return True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(bets: list) -> dict:
    """Standard metric block for a population."""
    n = len(bets)
    if n == 0:
        return {
            'count': 0,
            'wins': 0, 'losses': 0, 'pushes': 0, 'pending': 0,
            'settled': 0,
            'win_pct': None,
            'roi_pct': None,
            'net_pl': Decimal('0.00'),
            'total_stake': Decimal('0.00'),
            'avg_edge_pp': None,
            'avg_clv_cents': None,
            'clv_plus_pct': None,
            'clv_sample': 0,
        }

    counter = Counter(b.result for b in bets)
    wins = counter.get('win', 0)
    losses = counter.get('loss', 0)
    pushes = counter.get('push', 0)
    pending = counter.get('pending', 0)
    settled = wins + losses + pushes

    win_pct = round(100.0 * wins / (wins + losses), 1) if (wins + losses) > 0 else None

    # P/L convention — MUST match the canonical math in
    # recommendation_performance._group_stats (lines 69-93):
    #   simulated_payout stores PROFIT ONLY on a win (see
    #   MockBet.calculate_payout: +150 / $100 → $150 profit, NOT $250 return).
    #   So a winning bet's net P/L is exactly simulated_payout; a loss is
    #   -stake; a push is 0. Equivalently: net_result property on the model.
    #
    #   The earlier version computed (simulated_payout - stake) for ALL
    #   settled rows, which double-subtracts the stake on wins and produced
    #   a phantom ROI ~= true_roi - win_rate. Do NOT reintroduce that.
    total_stake_settled = Decimal('0.00')
    net_pl = Decimal('0.00')
    for b in bets:
        if b.result == 'pending':
            continue
        stake = b.stake_amount or Decimal('0.00')
        total_stake_settled += stake
        if b.result == 'win':
            net_pl += (b.simulated_payout or Decimal('0.00'))   # profit only
        elif b.result == 'push':
            net_pl += Decimal('0.00')
        else:  # loss
            net_pl += (-stake)
    roi_pct = (
        round(float(net_pl) / float(total_stake_settled) * 100.0, 1)
        if total_stake_settled > 0 else None
    )

    edges = [float(b.expected_edge) for b in bets if b.expected_edge is not None]
    avg_edge_pp = round(sum(edges) / len(edges) * 100.0, 2) if edges else None

    # CLV trust contract: only primary-source (odds_api) rows count.
    clv_rows = [
        b for b in bets
        if b.clv_cents is not None and (getattr(b, 'odds_source', '') == 'odds_api')
    ]
    if clv_rows:
        avg_clv_cents = round(sum(b.clv_cents for b in clv_rows) / len(clv_rows), 4)
        pluses = sum(1 for b in clv_rows if b.clv_cents > 0)
        clv_plus_pct = round(100.0 * pluses / len(clv_rows), 1)
    else:
        avg_clv_cents = None
        clv_plus_pct = None

    return {
        'count': n,
        'wins': wins,
        'losses': losses,
        'pushes': pushes,
        'pending': pending,
        'settled': settled,
        'win_pct': win_pct,
        'roi_pct': roi_pct,
        'net_pl': net_pl,
        'total_stake': total_stake_settled,
        'avg_edge_pp': avg_edge_pp,
        'avg_clv_cents': avg_clv_cents,
        'clv_plus_pct': clv_plus_pct,
        'clv_sample': len(clv_rows),
    }


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def build_audit(
    bets_qs,
    *,
    cutoff: datetime,
    now: datetime,
    days: int,
    sport: str = 'mlb',
    username: Optional[str] = None,
    settled_only: bool = False,
) -> dict:
    """Build the audit packet. `bets_qs` should already be filtered to the
    correct user-scope and sport-scope; this function applies bet_type +
    date + optional settled filters and partitions the result into the
    three populations.
    """
    qs = bets_qs.filter(
        bet_type='moneyline',
        sport=sport,
        placed_at__gte=cutoff,
    )
    if username:
        qs = qs.filter(user__username=username)
    if settled_only:
        qs = qs.exclude(result='pending')

    all_bets = list(qs.select_related('recommendation'))

    pop_actual = all_bets
    pop_system = [b for b in all_bets if is_true_system_approved(b)]
    system_ids = {b.id for b in pop_system}
    pop_manual = [b for b in all_bets if b.id not in system_ids]

    m_actual = compute_metrics(pop_actual)
    m_system = compute_metrics(pop_system)
    m_manual = compute_metrics(pop_manual)

    return {
        'window': {
            'days': days,
            'cutoff': cutoff,
            'now': now,
            'sport': sport,
            'username': username,
            'settled_only': settled_only,
        },
        'rules_effective_date': MODEL_RULES_EFFECTIVE_DATE,
        'populations': {
            'actual': m_actual,
            'system_approved': m_system,
            'manual_contaminated': m_manual,
        },
        'answers': _answer_block(m_system, m_manual, days),
    }


def _answer_block(m_system: dict, m_manual: dict, days: int) -> dict:
    """Derive the A/B/C answers from the metric blocks."""
    # (A) Beat market?
    clv_plus = m_system.get('clv_plus_pct')
    if clv_plus is None:
        a = {'verdict': 'unknown',
             'text': 'No primary-source CLV in system-approved set.'}
    elif clv_plus > 50.0:
        a = {'verdict': 'yes',
             'text': f"System-approved CLV+ % = {clv_plus:.1f}% (n={m_system['clv_sample']}, threshold 50%)."}
    else:
        a = {'verdict': 'no',
             'text': f"System-approved CLV+ % = {clv_plus:.1f}% (n={m_system['clv_sample']}, threshold 50%)."}

    # (B) Did manual hurt?
    m_sys_roi = m_system.get('roi_pct')
    m_man_roi = m_manual.get('roi_pct')
    if m_sys_roi is None or m_man_roi is None:
        b = {'verdict': 'undetermined',
             'text': 'One population has no settled bets.'}
    else:
        delta = m_sys_roi - m_man_roi
        if delta > 0:
            b = {'verdict': 'yes',
                 'text': f"System ROI {m_sys_roi:.1f}% vs Manual ROI {m_man_roi:.1f}% "
                         f"(gap {delta:+.1f} pp in system's favor)."}
        else:
            b = {'verdict': 'no',
                 'text': f"System ROI {m_sys_roi:.1f}% vs Manual ROI {m_man_roi:.1f}% "
                         f"(gap {delta:+.1f} pp; manual matched or beat system)."}

    # (C) Blind-follow bankroll
    if m_system['count'] == 0:
        c = {'verdict': 'na',
             'text': f"Zero system-approved bets in window."}
    else:
        c = {'verdict': 'computed',
             'text': (
                 f"{days} days, $100 flat stake: "
                 f"{m_system['wins']}-{m_system['losses']}-{m_system['pushes']} "
                 f"({m_system['pending']} pending), "
                 f"ROI {_fmt_pct(m_system['roi_pct'])}, "
                 f"Net P/L {_fmt_money(m_system['net_pl'])} "
                 f"on ${m_system['total_stake']:,.2f} settled stake."
             )}
    return {'A_beat_market': a, 'B_manual_hurt': b, 'C_blind_follow': c}


# ---------------------------------------------------------------------------
# Plaintext rendering
# ---------------------------------------------------------------------------

def _fmt_pct(v):
    return f"{v:.1f}%" if v is not None else "—"


def _fmt_pp(v):
    return f"{v:.2f} pp" if v is not None else "—"


def _fmt_money(v):
    return f"${v:+,.2f}" if v is not None else "—"


def _fmt_clv(v):
    return f"{v:+.4f}" if v is not None else "—"


def _block(label: str, m: dict) -> str:
    return (
        "\n" + "=" * 78 + f"\n  {label}\n" + "=" * 78 + "\n"
        f"  Total bets ............ {m['count']}\n"
        f"  W-L-P (Pending) ....... {m['wins']}-{m['losses']}-{m['pushes']} "
        f"({m['pending']} pending)\n"
        f"  Win % (settled) ....... {_fmt_pct(m['win_pct'])}\n"
        f"  ROI %  (settled) ...... {_fmt_pct(m['roi_pct'])}\n"
        f"  Net P/L (simulated) ... {_fmt_money(m['net_pl'])}\n"
        f"  Stake settled ......... ${m['total_stake']:,.2f}\n"
        f"  Avg edge (snapshot) ... {_fmt_pp(m['avg_edge_pp'])}\n"
        f"  Avg CLV (cents) ....... {_fmt_clv(m['avg_clv_cents'])}  "
        f"[primary-source only; n={m['clv_sample']}]\n"
        f"  CLV+ % ................ {_fmt_pct(m['clv_plus_pct'])}"
    )


def render_report(audit: dict) -> str:
    """Format the audit dict into a plaintext report (copy-paste ready)."""
    w = audit['window']
    m_actual = audit['populations']['actual']
    m_system = audit['populations']['system_approved']
    m_manual = audit['populations']['manual_contaminated']
    a = audit['answers']

    lines = []
    lines.append("#" * 78)
    lines.append(f"#  THREE-POPULATION AUDIT — Production Truth Test")
    lines.append(
        f"#  Window: last {w['days']} days  "
        f"({w['cutoff']:%Y-%m-%d %H:%M %Z} → {w['now']:%Y-%m-%d %H:%M %Z})"
    )
    lines.append(f"#  Sport: {w['sport']}    Bet type: moneyline")
    if w['username']:
        lines.append(f"#  User filter: {w['username']}")
    if w['settled_only']:
        lines.append(f"#  Settled-only: YES")
    lines.append(f"#  Rules-effective date: {audit['rules_effective_date'].isoformat()}")
    lines.append("#" * 78)

    lines.append(_block("(1) ALL ACTUAL BETS PLACED", m_actual))
    lines.append(_block(
        "(2) TRUE SYSTEM-APPROVED BETS  [is_system|linked AND status=recommended "
        "AND lane=core AND post-rules AND complete snapshot]",
        m_system))
    lines.append(_block("(3) MANUAL / CONTAMINATED BETS  [(1) minus (2)]", m_manual))

    lines.append("\n" + "=" * 78)
    lines.append("  ANSWER BLOCK")
    lines.append("=" * 78)
    lines.append(f"(A) Beat market? — {a['A_beat_market']['verdict'].upper()}.  "
                 f"{a['A_beat_market']['text']}")
    lines.append(f"(B) Did manual hurt? — {a['B_manual_hurt']['verdict'].upper()}.  "
                 f"{a['B_manual_hurt']['text']}")
    lines.append(f"(C) Blind-follow bankroll — {a['C_blind_follow']['text']}")

    lines.append("\n" + "-" * 78)
    lines.append("  SAMPLE-SIZE NOTES")
    lines.append("-" * 78)
    if m_system['settled'] < 30:
        lines.append(
            f"  WARN: System-approved settled n = {m_system['settled']} (<30). "
            f"Treat ROI/win% as DIRECTIONAL, not conclusive."
        )
    if (m_system.get('clv_sample') or 0) < 30:
        lines.append(
            f"  WARN: System-approved CLV sample n = {m_system.get('clv_sample') or 0} "
            f"(<30). CLV+ % is directional."
        )
    if m_manual['settled'] < 10:
        lines.append(
            f"  WARN: Manual settled n = {m_manual['settled']} (<10). "
            f"Manual-vs-system comparison may be unstable."
        )

    return "\n".join(lines) + "\n"
