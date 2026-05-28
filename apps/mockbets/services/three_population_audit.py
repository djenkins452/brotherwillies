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
            'clv_beat': 0,
            'clv_matched': 0,
            'clv_lost': 0,
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

    # UNIT NOTE: expected_edge is stored in PERCENTAGE POINTS already
    # (BettingRecommendation.model_edge = round(prob_diff * 100, 1); copied
    # verbatim to MockBet.expected_edge). e.g. 11.3 means 11.3 pp. The engine
    # compares it against MIN_EDGE = 6.0 (also pp). Do NOT multiply by 100
    # here — an earlier version did, producing impossible "1130.80 pp"
    # displays. The value is already in pp.
    edges = [float(b.expected_edge) for b in bets if b.expected_edge is not None]
    avg_edge_pp = round(sum(edges) / len(edges), 2) if edges else None

    # CLV trust contract: only primary-source (odds_api) rows count.
    clv_rows = [
        b for b in bets
        if b.clv_cents is not None and (getattr(b, 'odds_source', '') == 'odds_api')
    ]
    # CLV MIX — beat / matched / lost the close. "matched" (clv==0) is
    # surfaced separately so it is NOT silently lumped into "lost"; the
    # integrity audit showed clv==0 is a real, common state under coarse
    # snapshot cadence.
    if clv_rows:
        avg_clv_cents = round(sum(b.clv_cents for b in clv_rows) / len(clv_rows), 4)
        clv_beat = sum(1 for b in clv_rows if b.clv_cents > 0)
        clv_lost = sum(1 for b in clv_rows if b.clv_cents < 0)
        clv_matched = sum(1 for b in clv_rows if b.clv_cents == 0)
        clv_plus_pct = round(100.0 * clv_beat / len(clv_rows), 1)
    else:
        avg_clv_cents = None
        clv_beat = clv_lost = clv_matched = 0
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
        'clv_beat': clv_beat,
        'clv_matched': clv_matched,
        'clv_lost': clv_lost,
    }


# ---------------------------------------------------------------------------
# SPLITS — odds buckets + favorite/underdog (read-only diagnostic)
# ---------------------------------------------------------------------------
#
# Pure re-partitioning of a population by the PLACED price (odds_american =
# the price of the side the bet was on). Each sub-slice is run through the
# SAME compute_metrics, so W-L / ROI / CLV math is identical to the headline.
# No new math, no thresholds, no methodology.

# (label, predicate on american odds). Ordered favorite→dog. Matches the
# price-bucket scheme used in the earlier diagnostic discussion.
ODDS_BUCKETS = [
    ('Heavy fav (≤ -181)',      lambda o: o is not None and o <= -181),
    ('Fav (-151..-180)',        lambda o: o is not None and -180 <= o <= -151),
    ('Fav (-131..-150)',        lambda o: o is not None and -150 <= o <= -131),
    ('Short fav (-101..-130)',  lambda o: o is not None and -130 <= o <= -101),
    ('Pick (-100..+100)',       lambda o: o is not None and -100 <= o <= 100),
    ('Dog (+101..+150)',        lambda o: o is not None and 101 <= o <= 150),
    ('Long dog (≥ +151)',       lambda o: o is not None and o >= 151),
]


def compute_splits(bets: list) -> dict:
    """Return headline metrics + odds-bucket + favorite/underdog breakdowns.

    `bets` is a materialized list (already the population/window of interest).
    """
    base = compute_metrics(bets)

    buckets = []
    for label, pred in ODDS_BUCKETS:
        sub = [b for b in bets if pred(b.odds_american)]
        if sub:
            buckets.append((label, compute_metrics(sub)))

    favorites = [b for b in bets if (b.odds_american or 0) < 0]
    underdogs = [b for b in bets if (b.odds_american or 0) > 0]

    return {
        'base': base,
        'odds_buckets': buckets,
        'favorites': compute_metrics(favorites),
        'underdogs': compute_metrics(underdogs),
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


# ---------------------------------------------------------------------------
# CLV LINEAGE DIAGNOSTIC  (read-only — measurement integrity audit)
# ---------------------------------------------------------------------------
#
# Surfaces the raw inputs to every CLV computation so an operator can
# spot-check whether CLV+ is real or a measurement artifact. Investigates
# the failure modes flagged in the integrity audit:
#
#   - clv_cents == 0  → bet "matched the market"; counted as NOT-positive by
#     CLV+ %. If many system bets sit at 0, CLV+ is deflated by definition,
#     not by the model.
#   - single-snapshot games → placement snapshot IS the closing snapshot, so
#     CLV is structurally forced to ~0 (coarse ingestion cadence).
#   - source mismatch → placement priced off odds_api but the closing snapshot
#     is from a different provider, contaminating the raw-odds delta.
#   - timing → how early the bet was placed vs first pitch, and how close to
#     first pitch the "closing" snapshot actually was.
#
# NO methodology, threshold, or CLV-math changes. Pure read.

def _game_start(game):
    if game is None:
        return None
    return (
        getattr(game, 'first_pitch', None)
        or getattr(game, 'kickoff', None)
        or getattr(game, 'tipoff', None)
    )


def _minutes_between(later, earlier):
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() / 60.0, 1)


def clv_lineage(bets: list, *, limit: int = 50) -> dict:
    """Per-bet CLV input lineage + aggregate artifact counters.

    `bets` should already be the population of interest (e.g. system-approved).
    Read-only. Issues a few small queries per bet (snapshot counts); fine for
    staff-scale diagnostics over a 30–90 day window.
    """
    rows = []
    n = 0
    n_clv_none = n_clv_zero = n_clv_pos = n_clv_neg = 0
    n_single_snap = n_no_pregame = 0
    n_source_mismatch = 0
    n_closing_is_placement_snap = 0
    snap_counts = []

    for b in bets:
        n += 1
        game = b.game
        start = _game_start(game)

        snaps = list(game.odds_snapshots.all()) if game is not None else []
        snap_counts.append(len(snaps))
        pregame = [s for s in snaps if start is not None and s.captured_at < start]
        closing_snap = pregame[0] if pregame else None  # latest pre-game (-captured_at)

        clv = b.clv_cents
        if clv is None:
            n_clv_none += 1
        elif clv > 0:
            n_clv_pos += 1
        elif clv < 0:
            n_clv_neg += 1
        else:
            n_clv_zero += 1

        if len(snaps) <= 1:
            n_single_snap += 1
        if not pregame:
            n_no_pregame += 1

        closing_source = getattr(closing_snap, 'odds_source', None)
        source_match = (
            closing_source is not None
            and b.odds_source is not None
            and closing_source == b.odds_source
        )
        if closing_source and b.odds_source and closing_source != b.odds_source:
            n_source_mismatch += 1

        # Is the stored closing price identical to the placement price?
        # (proxy for "the closing snapshot was the same line we placed at")
        if (b.closing_odds_american is not None
                and b.closing_odds_american == b.odds_american):
            n_closing_is_placement_snap += 1

        if len(rows) < limit:
            rows.append({
                'selection': b.selection,
                'placed_at': b.placed_at,
                'first_pitch': start,
                'min_placed_before_fp': _minutes_between(start, b.placed_at),
                'odds_placement': b.odds_american,
                'source_placement': b.odds_source,
                'odds_closing': b.closing_odds_american,
                'closing_source': closing_source or '—',
                'closing_captured_at': getattr(closing_snap, 'captured_at', None),
                'min_closing_before_fp': _minutes_between(
                    start, getattr(closing_snap, 'captured_at', None)),
                'clv_cents': clv,
                'clv_direction': b.clv_direction or '—',
                'snap_total': len(snaps),
                'snap_pregame': len(pregame),
                'source_match': source_match,
                'result': b.result,
            })

    avg_snaps = round(sum(snap_counts) / len(snap_counts), 2) if snap_counts else 0

    return {
        'rows': rows,
        'shown': len(rows),
        'aggregate': {
            'n': n,
            'clv_none': n_clv_none,
            'clv_zero': n_clv_zero,
            'clv_positive': n_clv_pos,
            'clv_negative': n_clv_neg,
            'single_snapshot_games': n_single_snap,
            'no_pregame_snapshot': n_no_pregame,
            'source_mismatch': n_source_mismatch,
            'closing_equals_placement': n_closing_is_placement_snap,
            'avg_snapshots_per_game': avg_snaps,
        },
    }


def render_clv_lineage(lineage: dict, *, label: str = 'SYSTEM-APPROVED') -> str:
    """Plaintext render of clv_lineage() for the staff HTTP view."""
    agg = lineage['aggregate']
    n = agg['n'] or 1
    lines = []
    lines.append("#" * 100)
    lines.append(f"#  CLV LINEAGE DIAGNOSTIC — {label} population  (read-only)")
    lines.append("#" * 100)
    lines.append("")
    lines.append("AGGREGATE ARTIFACT COUNTERS")
    lines.append("-" * 100)
    lines.append(f"  Bets in population ............... {agg['n']}")
    lines.append(f"  CLV not captured (None) .......... {agg['clv_none']}  "
                 f"({100.0*agg['clv_none']/n:.1f}%)")
    lines.append(f"  CLV == 0 (matched the close) ..... {agg['clv_zero']}  "
                 f"({100.0*agg['clv_zero']/n:.1f}%)  ← counted as NOT-positive by CLV+%")
    lines.append(f"  CLV  > 0 (beat the close) ........ {agg['clv_positive']}  "
                 f"({100.0*agg['clv_positive']/n:.1f}%)")
    lines.append(f"  CLV  < 0 (close beat us) ......... {agg['clv_negative']}  "
                 f"({100.0*agg['clv_negative']/n:.1f}%)")
    lines.append("")
    lines.append(f"  Single-snapshot games ............ {agg['single_snapshot_games']}  "
                 f"({100.0*agg['single_snapshot_games']/n:.1f}%)  "
                 f"← placement snapshot IS the closing snapshot → CLV forced ~0")
    lines.append(f"  No pre-game snapshot ............. {agg['no_pregame_snapshot']}")
    lines.append(f"  Closing price == placement price . {agg['closing_equals_placement']}  "
                 f"({100.0*agg['closing_equals_placement']/n:.1f}%)")
    lines.append(f"  Placement/closing SOURCE mismatch  {agg['source_mismatch']}  "
                 f"({100.0*agg['source_mismatch']/n:.1f}%)  ← raw-odds delta contaminated")
    lines.append(f"  Avg snapshots per game ........... {agg['avg_snapshots_per_game']}")
    lines.append("")
    lines.append(f"PER-BET LINEAGE  (showing {lineage['shown']} of {agg['n']})")
    lines.append("-" * 100)
    hdr = (
        f"{'selection':<22} {'plc_before_fp':>13} {'place':>7} {'src':>9} "
        f"{'close':>7} {'cl_src':>9} {'cl_b4_fp':>9} {'clv':>9} {'snaps(pre)':>11} {'res':>5}"
    )
    lines.append(hdr)
    for r in lineage['rows']:
        plc = f"{r['min_placed_before_fp']}m" if r['min_placed_before_fp'] is not None else '—'
        clb = f"{r['min_closing_before_fp']}m" if r['min_closing_before_fp'] is not None else '—'
        clv = f"{r['clv_cents']:+.4f}" if r['clv_cents'] is not None else '—'
        place = str(r['odds_placement']) if r['odds_placement'] is not None else '—'
        close = str(r['odds_closing']) if r['odds_closing'] is not None else '—'
        snaps = f"{r['snap_total']}({r['snap_pregame']})"
        lines.append(
            f"{(r['selection'] or '')[:22]:<22} {plc:>13} {place:>7} "
            f"{(r['source_placement'] or '—')[:9]:>9} {close:>7} "
            f"{(r['closing_source'] or '—')[:9]:>9} {clb:>9} {clv:>9} {snaps:>11} "
            f"{(r['result'] or '')[:5]:>5}"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SPLITS rendering
# ---------------------------------------------------------------------------

def _splits_metric_line(label: str, m: dict) -> str:
    wlp = f"{m['wins']}-{m['losses']}-{m['pushes']}"
    win = f"{m['win_pct']:.1f}%" if m['win_pct'] is not None else '—'
    roi = f"{m['roi_pct']:+.1f}%" if m['roi_pct'] is not None else '—'
    clv = f"{m['avg_clv_cents']:+.4f}" if m['avg_clv_cents'] is not None else '—'
    mix = f"{m['clv_beat']}/{m['clv_matched']}/{m['clv_lost']}"
    return (
        f"  {label:<24} n={m['count']:<4} {wlp:>9}  win {win:>6}  "
        f"ROI {roi:>7}  avgCLV {clv:>9}  beat/match/lost {mix:>10}  "
        f"(clv n={m['clv_sample']})"
    )


def render_splits(splits: dict, *, label: str, window_desc: str) -> str:
    """Plaintext render of compute_splits() for the staff HTTP view."""
    base = splits['base']
    lines = []
    lines.append("#" * 110)
    lines.append(f"#  SPLITS DIAGNOSTIC — {label} population")
    lines.append(f"#  {window_desc}")
    lines.append("#" * 110)
    lines.append("")
    lines.append("HEADLINE")
    lines.append("-" * 110)
    lines.append(_splits_metric_line('TOTAL', base))
    avg_edge_txt = f"{base['avg_edge_pp']:.2f} pp" if base['avg_edge_pp'] is not None else '—'
    lines.append(
        f"  Net P/L ${float(base['net_pl']):+,.2f}   "
        f"settled stake ${float(base['total_stake']):,.2f}   "
        f"avg edge {avg_edge_txt}"
    )
    lines.append("")
    lines.append("CLV MIX  (primary-source odds_api only)")
    lines.append("-" * 110)
    if base['clv_sample']:
        s = base['clv_sample']
        lines.append(
            f"  Beat market ... {base['clv_beat']:>4}  ({100.0*base['clv_beat']/s:.1f}%)"
        )
        lines.append(
            f"  Matched ....... {base['clv_matched']:>4}  ({100.0*base['clv_matched']/s:.1f}%)"
            f"   ← clv==0; NOT counted as 'beat'"
        )
        lines.append(
            f"  Lost market ... {base['clv_lost']:>4}  ({100.0*base['clv_lost']/s:.1f}%)"
        )
        lines.append(
            f"  Avg CLV ....... {base['avg_clv_cents']:+.4f}   "
            f"CLV+ {base['clv_plus_pct']:.1f}%   sample n={s}"
        )
    else:
        lines.append("  (no primary-source CLV rows in this window)")
    lines.append("")
    lines.append("BY ODDS BUCKET  (placed-side price)")
    lines.append("-" * 110)
    if splits['odds_buckets']:
        for blabel, m in splits['odds_buckets']:
            lines.append(_splits_metric_line(blabel, m))
    else:
        lines.append("  (no bets in window)")
    lines.append("")
    lines.append("FAVORITE vs UNDERDOG  (by placed-side price sign)")
    lines.append("-" * 110)
    lines.append(_splits_metric_line('Favorites (odds < 0)', splits['favorites']))
    lines.append(_splits_metric_line('Underdogs (odds > 0)', splits['underdogs']))
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# WEEKLY SCORECARD  (0.55 validation — read-only)
# ---------------------------------------------------------------------------
#
# Lightweight, trustworthy weekly readout for the 0.55 observation window.
# SYSTEM-APPROVED BETS ONLY (is_true_system_approved): status='recommended',
# lane='core', system/linked, post-rules date, complete snapshot.
#
# Odds buckets match the favorites-experiment definition EXACTLY so the
# scorecard, the splits view, and the replay all speak the same language:
#   heavy fav ≤ -200 / mid fav -150..-199 / short fav -149..+99 / dog ≥ +100
SCORECARD_ODDS_BUCKETS = [
    ('Heavy fav (≤ -200)',    lambda o: o is not None and o <= -200),
    ('Mid fav (-150..-199)',  lambda o: o is not None and -199 <= o <= -150),
    ('Short fav (-149..+99)', lambda o: o is not None and -149 <= o <= 99),
    ('Underdog (≥ +100)',     lambda o: o is not None and o >= 100),
]


def weekly_scorecard(bets: list) -> dict:
    """System-approved scorecard for a window. `bets` MUST already be the
    system-approved population (is_true_system_approved). Pure re-partition
    through the SAME compute_metrics — no new math."""
    base = compute_metrics(bets)
    buckets = []
    for label, pred in SCORECARD_ODDS_BUCKETS:
        buckets.append((label, compute_metrics([b for b in bets if pred(b.odds_american)])))
    # Favorite/dog split uses the +100 cutoff to match the odds-bucket scheme
    # and the favorites experiment (favorite = priced below +100; +99 is a fav).
    favorites = compute_metrics(
        [b for b in bets if b.odds_american is not None and b.odds_american < 100])
    underdogs = compute_metrics(
        [b for b in bets if b.odds_american is not None and b.odds_american >= 100])
    return {
        'base': base,
        'odds_buckets': buckets,
        'favorites': favorites,
        'underdogs': underdogs,
    }


def render_scorecard(sc: dict, *, window_desc: str) -> str:
    """Plaintext weekly scorecard for the staff HTTP view."""
    base = sc['base']
    lines = []
    lines.append("#" * 100)
    lines.append("#  WEEKLY SCORECARD — SYSTEM-APPROVED BETS ONLY  (blend 0.55 validation)")
    lines.append(f"#  {window_desc}")
    lines.append("#  Scope: status=recommended AND lane=core AND post-rules AND complete snapshot")
    lines.append("#" * 100)
    lines.append("")
    lines.append("HEADLINE")
    lines.append("-" * 100)
    lines.append(f"  Total system-approved bets .. {base['count']}")
    lines.append(f"  Recommendation count ........ {base['count']}  (placed system-approved)")
    lines.append(
        f"  W-L-P ....................... {base['wins']}-{base['losses']}-{base['pushes']}"
        f"  ({base['pending']} pending)"
    )
    lines.append(f"  Win % (settled) ............. {_fmt_pct(base['win_pct'])}")
    lines.append(f"  ROI % (settled) ............. {_fmt_pct(base['roi_pct'])}")
    lines.append(f"  Net P/L ..................... {_fmt_money(base['net_pl'])}")
    lines.append(f"  Settled stake ............... ${base['total_stake']:,.2f}")
    lines.append("")
    lines.append("CLV  (primary-source odds_api only)")
    lines.append("-" * 100)
    s = base['clv_sample']
    if s:
        lines.append(
            f"  Beat / Matched / Lost ....... {base['clv_beat']} / "
            f"{base['clv_matched']} / {base['clv_lost']}   (n={s})"
        )
        lines.append(f"  Beat-market % ............... {_fmt_pct(base['clv_plus_pct'])}")
        lines.append(f"  Avg CLV ..................... {_fmt_clv(base['avg_clv_cents'])}")
    else:
        lines.append("  (no primary-source CLV rows this window)")
    lines.append("")
    lines.append("FAVORITE vs UNDERDOG  (favorite = priced < +100)")
    lines.append("-" * 100)
    lines.append(_splits_metric_line('Favorites (< +100)', sc['favorites']))
    lines.append(_splits_metric_line('Underdogs (≥ +100)', sc['underdogs']))
    lines.append("")
    lines.append("ODDS BUCKET BREAKDOWN")
    lines.append("-" * 100)
    for label, m in sc['odds_buckets']:
        lines.append(_splits_metric_line(label, m))
    lines.append("")
    return "\n".join(lines) + "\n"
