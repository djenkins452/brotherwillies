"""Replay-vs-Actual overlap diagnostic.

Read-only. For a date window:
  1. Run the lane-corrected replay at a given blend weight.
  2. Fetch all actual MLB moneyline MockBets placed in the same window.
  3. Cross-reference by mlb_game_id.
  4. Bucket each game into:
        OVERLAP        — replay recommended AND placed
        PRODUCTION-ONLY — placed, replay did NOT recommend
        REPLAY-ONLY    — replay recommended, NOT placed
  5. Report count / W-L / ROI / avg CLV per bucket.

Used for the post-deployment validation question: "did our actual production
bets match what the replay said we should have bet under 0.55?"

NO methodology / engine / threshold edits. This module reads existing
simulations and existing MockBet rows and reports correlations.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from django.utils import timezone


def _flat_pl_from_actual(bet) -> Decimal:
    """Net P/L for an actually-placed bet using the canonical convention
    (matches recommendation_performance._group_stats):
      win  → +simulated_payout (profit)
      push → 0
      loss → -stake
    """
    if bet.result == 'pending':
        return Decimal('0.00')
    if bet.result == 'win':
        return bet.simulated_payout or Decimal('0.00')
    if bet.result == 'push':
        return Decimal('0.00')
    return -(bet.stake_amount or Decimal('0.00'))


def _flat_pl_from_sim(sim, *, stake: Decimal = Decimal('100.00')) -> Decimal:
    """Synthetic flat-$100 P/L for a replay-only sim. Uses the same
    convention as _flat_pl_from_actual."""
    from apps.core.utils.odds import american_to_decimal
    if sim.won is True:
        # decimal_odds includes stake — profit is (dec - 1) * stake
        dec = american_to_decimal(int(sim.pick_odds))
        return Decimal(str(round((dec - 1.0) * float(stake), 2)))
    if sim.won is False:
        return -stake
    return Decimal('0.00')   # pending or push


def _bucket_metrics(rows) -> dict:
    """rows: list of dicts each with 'won' (True/False/None) and 'pl' (Decimal)
    and optional 'stake' (Decimal, default 100) and 'clv' (float or None,
    primary-source only)."""
    n = len(rows)
    if n == 0:
        return {
            'count': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'pending': 0,
            'settled': 0, 'win_pct': None, 'roi_pct': None,
            'net_pl': Decimal('0.00'), 'total_stake': Decimal('0.00'),
            'clv_sample': 0, 'avg_clv': None, 'clv_beat': 0, 'clv_matched': 0,
            'clv_lost': 0, 'clv_plus_pct': None,
        }
    wins = sum(1 for r in rows if r['won'] is True)
    losses = sum(1 for r in rows if r['won'] is False)
    pushes = sum(1 for r in rows if r.get('result') == 'push')
    pending = sum(1 for r in rows if r['won'] is None and r.get('result') != 'push')
    settled = wins + losses + pushes
    win_pct = round(100.0 * wins / (wins + losses), 1) if (wins + losses) > 0 else None
    total_stake = sum((r.get('stake') or Decimal('100.00')) for r in rows
                      if r.get('result') != 'pending')
    net_pl = sum(r['pl'] for r in rows)
    roi_pct = round(float(net_pl) / float(total_stake) * 100.0, 1) if total_stake else None

    clv_rows = [r for r in rows if r.get('clv') is not None]
    if clv_rows:
        avg_clv = round(sum(r['clv'] for r in clv_rows) / len(clv_rows), 4)
        clv_beat = sum(1 for r in clv_rows if r['clv'] > 0)
        clv_matched = sum(1 for r in clv_rows if r['clv'] == 0)
        clv_lost = sum(1 for r in clv_rows if r['clv'] < 0)
        clv_plus_pct = round(100.0 * clv_beat / len(clv_rows), 1)
    else:
        avg_clv = None; clv_beat = clv_matched = clv_lost = 0; clv_plus_pct = None

    return {
        'count': n, 'wins': wins, 'losses': losses, 'pushes': pushes,
        'pending': pending, 'settled': settled,
        'win_pct': win_pct, 'roi_pct': roi_pct,
        'net_pl': net_pl, 'total_stake': total_stake,
        'clv_sample': len(clv_rows), 'avg_clv': avg_clv,
        'clv_beat': clv_beat, 'clv_matched': clv_matched, 'clv_lost': clv_lost,
        'clv_plus_pct': clv_plus_pct,
    }


def build_overlap(date_from: date, date_to: date, *,
                  blend_weight: float = 0.55,
                  username: Optional[str] = None) -> dict:
    """Compute the three buckets + per-game detail rows for the window."""
    from apps.analytics.services.method_replay import run_replay
    from apps.mockbets.models import MockBet

    replay = run_replay(date_from, date_to, [blend_weight])
    variant = replay['variants'][0]
    # Lane-corrected (production-equivalent) recommended set — same definition
    # used everywhere in the audit tooling.
    replay_recs_by_gid = {
        s.game_id: s for s in variant['simulations']
        if s.is_lane_corrected_recommended
    }
    all_sims_by_gid = {s.game_id: s for s in variant['simulations']}

    # Actual placed MLB moneyline bets in the window. Use FIRST_PITCH date
    # (not placed_at) so the comparison axis matches the replay's game
    # population exactly.
    actual_qs = MockBet.objects.filter(
        sport='mlb', bet_type='moneyline',
        mlb_game__first_pitch__date__gte=date_from,
        mlb_game__first_pitch__date__lte=date_to,
    ).select_related('mlb_game', 'recommendation')
    if username:
        actual_qs = actual_qs.filter(user__username=username)
    actual_bets = list(actual_qs)
    actual_by_gid = {}
    for b in actual_bets:
        gid = str(b.mlb_game_id) if b.mlb_game_id else None
        if gid:
            actual_by_gid.setdefault(gid, []).append(b)

    overlap_rows, prod_only_rows, replay_only_rows = [], [], []
    overlap_detail, prod_only_detail, replay_only_detail = [], [], []

    # Iterate over the union of game ids seen in either set.
    all_gids = set(replay_recs_by_gid.keys()) | set(actual_by_gid.keys())
    for gid in all_gids:
        replay_rec = replay_recs_by_gid.get(gid)
        placed_bets = actual_by_gid.get(gid, [])

        if replay_rec and placed_bets:
            # OVERLAP — count each placed bet on this game (handles dup placements).
            for b in placed_bets:
                pl = _flat_pl_from_actual(b)
                won = (b.result == 'win' if b.result in ('win', 'loss')
                       else None)
                overlap_rows.append({
                    'won': won, 'pl': pl, 'result': b.result,
                    'stake': b.stake_amount or Decimal('100.00'),
                    'clv': (b.clv_cents if b.odds_source == 'odds_api' else None),
                })
                overlap_detail.append({
                    'game_id': gid, 'label': replay_rec.game_label,
                    'first_pitch': replay_rec.first_pitch_iso,
                    'placed_odds': b.odds_american,
                    'placed_source': b.odds_source,
                    'replay_edge_pp': replay_rec.edge_pp,
                    'replay_conf_pct': round(replay_rec.pick_prob * 100, 1),
                    'result': b.result,
                    'pl': float(pl),
                    'clv': b.clv_cents,
                })
        elif placed_bets and not replay_rec:
            for b in placed_bets:
                pl = _flat_pl_from_actual(b)
                won = (b.result == 'win' if b.result in ('win', 'loss')
                       else None)
                prod_only_rows.append({
                    'won': won, 'pl': pl, 'result': b.result,
                    'stake': b.stake_amount or Decimal('100.00'),
                    'clv': (b.clv_cents if b.odds_source == 'odds_api' else None),
                })
                sim = all_sims_by_gid.get(gid)
                prod_only_detail.append({
                    'game_id': gid,
                    'label': (sim.game_label if sim else
                              f'{b.mlb_game.away_team.name} @ {b.mlb_game.home_team.name}'
                              if b.mlb_game else gid),
                    'placed_odds': b.odds_american,
                    'placed_source': b.odds_source,
                    'replay_status': (sim.status if sim else 'not_evaluable'),
                    'replay_reason': (sim.status_reason if sim else ''),
                    'is_system_generated': b.is_system_generated,
                    'result': b.result,
                    'pl': float(pl),
                })
        elif replay_rec and not placed_bets:
            pl = _flat_pl_from_sim(replay_rec)
            result = ('win' if replay_rec.won is True
                      else 'loss' if replay_rec.won is False
                      else 'pending')
            replay_only_rows.append({
                'won': replay_rec.won, 'pl': pl, 'result': result,
                'stake': Decimal('100.00'),
                'clv': replay_rec.clv_decimal,
            })
            replay_only_detail.append({
                'game_id': gid, 'label': replay_rec.game_label,
                'first_pitch': replay_rec.first_pitch_iso,
                'replay_edge_pp': replay_rec.edge_pp,
                'replay_conf_pct': round(replay_rec.pick_prob * 100, 1),
                'pick_odds': replay_rec.pick_odds,
                'replay_won': replay_rec.won,
                'pl_synth': float(pl),
            })

    return {
        'window': {'from': date_from, 'to': date_to, 'blend_weight': blend_weight},
        'overlap': _bucket_metrics(overlap_rows),
        'production_only': _bucket_metrics(prod_only_rows),
        'replay_only': _bucket_metrics(replay_only_rows),
        'detail': {
            'overlap': overlap_detail,
            'production_only': prod_only_detail,
            'replay_only': replay_only_detail,
        },
        'totals': {
            'replay_recommended_count': len(replay_recs_by_gid),
            'actual_placed_count': len(actual_bets),
            'distinct_games_actual': len(actual_by_gid),
        },
    }


def render_overlap(o: dict) -> str:
    w = o['window']
    lines = []
    lines.append('#' * 100)
    lines.append(f"#  REPLAY vs ACTUAL OVERLAP — blend {w['blend_weight']:.2f}")
    lines.append(f"#  Window (first_pitch date): {w['from']} → {w['to']}")
    lines.append('#' * 100)
    lines.append('')
    t = o['totals']
    lines.append(
        f"  Replay recommended games:  {t['replay_recommended_count']}    "
        f"Actual placed bets: {t['actual_placed_count']}    "
        f"Distinct actual games: {t['distinct_games_actual']}"
    )
    lines.append('')

    def _block(label, m):
        lines.append('=' * 100)
        lines.append(f'  {label}')
        lines.append('=' * 100)
        wlp = f"{m['wins']}-{m['losses']}-{m['pushes']}"
        lines.append(f"  Count ....... {m['count']}   "
                     f"W-L-P ....... {wlp}   ({m['pending']} pending)")
        win = f"{m['win_pct']:.1f}%" if m['win_pct'] is not None else '—'
        roi = f"{m['roi_pct']:+.1f}%" if m['roi_pct'] is not None else '—'
        clv = f"{m['avg_clv']:+.4f}" if m['avg_clv'] is not None else '—'
        clv_plus = f"{m['clv_plus_pct']:.1f}%" if m['clv_plus_pct'] is not None else '—'
        net_pl_f = float(m['net_pl'])
        lines.append(f"  Win %   ..... {win}   ROI .... {roi}   "
                     f"Net P/L .... ${net_pl_f:+,.2f}")
        mix = f"{m['clv_beat']}/{m['clv_matched']}/{m['clv_lost']}"
        lines.append(f"  CLV beat/match/lost .. {mix}  (n={m['clv_sample']})   "
                     f"avg CLV {clv}   CLV+ {clv_plus}")
        lines.append('')

    _block('OVERLAP — replay recommended AND actually placed', o['overlap'])
    _block('PRODUCTION-ONLY — placed but replay did NOT recommend', o['production_only'])
    _block('REPLAY-ONLY — replay recommended but NOT placed (synthetic flat-$100)',
           o['replay_only'])

    # Per-bet detail for the small populations.
    for label, key in [('OVERLAP detail', 'overlap'),
                       ('PRODUCTION-ONLY detail', 'production_only'),
                       ('REPLAY-ONLY detail', 'replay_only')]:
        rows = o['detail'][key]
        if not rows:
            continue
        lines.append('-' * 100)
        lines.append(f'  {label}  (showing {len(rows)})')
        lines.append('-' * 100)
        for r in rows:
            lines.append(f"    {r.get('label','?')[:50]:<50}  "
                         f"{str(r.get('first_pitch',''))[:19]:>19}  "
                         f"odds={r.get('placed_odds') or r.get('pick_odds','?'):>5}  "
                         f"edge={r.get('replay_edge_pp','?')}  "
                         f"result={r.get('result', r.get('replay_won',''))}  "
                         f"pl=${r.get('pl', r.get('pl_synth',0)):+,.2f}")
        lines.append('')

    return '\n'.join(lines) + '\n'
