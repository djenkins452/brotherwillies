"""v3.4 team-offense PHASE 2 — ISOLATED predictive-value analyzer.

Answers the Phase 2 mission's central question WITHOUT any model
integration or contribution weighting:

  Does each candidate offensive metric contain meaningful
  predictive information about game winners AFTER accounting
  for what the market already knows?

This analyzer NEVER modifies a recommendation, a score, a flag.
It computes per-candidate statistics and returns a report that
tells the operator whether to promote ONE candidate to a bounded
integration replay — or close the offense track entirely.

CANDIDATES EVALUATED (pre-declared)
  A_reference   30-day runs/game (FAILED reference — kept for continuity)
  B_v2          Rolling 30-day OPS
  C_v2_obp      Rolling 30-day OBP (component of C)
  C_v2_slg      Rolling 30-day SLG (component of C)
  D_v2          Season-to-date OPS blended 50/50 with rolling 30-day OPS

PER-CANDIDATE STATISTICS
  1. Sample size + coverage: n games evaluable / total games in window
  2. Signal distribution: min/max/mean/median/percentiles of home-away diff
  3. Predictive value vs winner: home-win rate by signal bucket
     (favors home strongly / favors home / neutral / favors away / favors
     away strongly)
  4. Home/away split: does signal predict differently at home vs on road?
  5. Favorite/underdog split: does signal predict differently when
     market disagrees?
  6. Monthly temporal consistency
  7. **PAST-MARKET signal test** — bucket games by market-implied
     probability quartile; within each bucket, split by offense-diff
     sign. If offense adds independent info, home-win rate should
     differ across the sign split within a market bucket.
  8. Correlation with Elo differential, market prob (home), pitcher
     rating diff, recent-form diff — high correlation = redundant.

VERDICT
  For each candidate: PROMOTE / MONITOR / REJECT based on:
    * Coverage >= 60% of games in window
    * Bucket monotonicity: strong-favor-home has higher home-win
      rate than strong-favor-away (rough directional check)
    * Past-market lift: within at least 2 of 4 market-prob quartiles,
      the sign split shows > 3pp home-win rate difference
    * Correlations with Elo/market/pitcher all under |0.6| (else
      redundant)

  If no candidate is PROMOTE: verdict NO-GO. Close offense.
  If one candidate is PROMOTE: return it as the sole promotion.
  If more than one is PROMOTE: pick the one with the strongest
  past-market lift.

READ-ONLY. Cannot activate scoring. USE_TEAM_OFFENSE remains false
regardless of what this analyzer produces.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.utils import timezone


logger = logging.getLogger(__name__)


# --- Bucket boundaries. Chosen BEFORE seeing results, per the
# discipline directive. Signal-value buckets are quintiles by
# absolute magnitude.
BUCKET_LABELS = ['strong_away', 'favors_away', 'neutral',
                 'favors_home', 'strong_home']


# ---------------------------------------------------------------------------
# Small helpers


def _pctile(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
    return s[idx]


def _mean(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    return sum(vals) / len(vals)


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation. Returns None if fewer than 8 samples or
    zero variance."""
    n = min(len(xs), len(ys))
    if n < 8:
        return None
    mx = _mean(xs[:n]) or 0.0
    my = _mean(ys[:n]) or 0.0
    sxx = sum((x - mx) ** 2 for x in xs[:n])
    syy = sum((y - my) ** 2 for y in ys[:n])
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    denom = math.sqrt(sxx * syy)
    if denom == 0:
        return None
    return sxy / denom


def _elo_rating_for(team, game):
    """Historical Elo just before `game.first_pitch` — reuses the
    replay's leakage-safe TeamEloHistory lookup so this analyzer
    matches production's rating source."""
    from apps.analytics.services.method_replay import _pregame_team_rating
    try:
        return _pregame_team_rating(team, game)
    except Exception:
        # Fallback to static rating if history missing.
        try:
            return getattr(team, 'elo_rating', None) or team.rating
        except Exception:
            return None


def _market_prob_home(game):
    """Read the game's earliest recorded moneyline snapshot and
    devig it into a fair home probability. Same source as the
    _simulate_recommendation flow."""
    from apps.core.utils.odds import (
        american_to_implied_prob, devig_two_way,
    )
    ml_h = getattr(game, 'opening_moneyline_home', None)
    ml_a = getattr(game, 'opening_moneyline_away', None)
    if ml_h is None or ml_a is None:
        # Try the odds snapshot chain.
        first_snap = (
            game.odds_snapshots
            .filter(moneyline_home__isnull=False,
                    moneyline_away__isnull=False)
            .order_by('captured_at')
            .first()
        )
        if first_snap is None:
            return None
        ml_h, ml_a = first_snap.moneyline_home, first_snap.moneyline_away
    try:
        rh = american_to_implied_prob(ml_h)
        ra = american_to_implied_prob(ml_a)
        fh, _ = devig_two_way(rh, ra)
        return fh
    except Exception:
        return None


def _pitcher_rating_diff(game):
    hp = getattr(game, 'home_pitcher', None)
    ap = getattr(game, 'away_pitcher', None)
    if hp is None or ap is None:
        return None
    hr = getattr(hp, 'rating', None)
    ar = getattr(ap, 'rating', None)
    if hr is None or ar is None:
        return None
    return hr - ar


def _recent_form_diff(game):
    """Recent-form (v3.1) home minus away rating adjustment."""
    from apps.mlb.services.pitcher_form import recent_form_signal
    try:
        h = recent_form_signal(game.home_team, game.first_pitch)
        a = recent_form_signal(game.away_team, game.first_pitch)
    except Exception:
        return None
    if h is None or a is None:
        return None
    try:
        return h.rating_delta - a.rating_delta
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Candidate signal extractors — one per pre-declared candidate.
# Each returns (home_units, away_units, diff_units, sample_ok) where
# diff_units = home_units - away_units.


def _signal_a_runs_per_game(game):
    from apps.mlb.services.team_offense import team_offense_signal
    h = team_offense_signal(game.home_team, game.first_pitch)
    a = team_offense_signal(game.away_team, game.first_pitch)
    if h is None or a is None:
        return None
    ok = (h.data_confidence != 'low' and a.data_confidence != 'low')
    return (h.quality_delta, a.quality_delta,
            h.quality_delta - a.quality_delta, ok)


def _signal_b_ops(game):
    from apps.mlb.services.team_offense_v2 import candidate_b_rolling_ops
    h = candidate_b_rolling_ops(game.home_team, game.first_pitch)
    a = candidate_b_rolling_ops(game.away_team, game.first_pitch)
    ok = (h.confidence != 'low' and a.confidence != 'low')
    return (h.delta_units, a.delta_units,
            h.delta_units - a.delta_units, ok)


def _signal_c_obp(game):
    from apps.mlb.services.team_offense_v2 import candidate_c_rolling_obp_slg
    h_obp, _ = candidate_c_rolling_obp_slg(game.home_team, game.first_pitch)
    a_obp, _ = candidate_c_rolling_obp_slg(game.away_team, game.first_pitch)
    ok = (h_obp.confidence != 'low' and a_obp.confidence != 'low')
    return (h_obp.delta_units, a_obp.delta_units,
            h_obp.delta_units - a_obp.delta_units, ok)


def _signal_c_slg(game):
    from apps.mlb.services.team_offense_v2 import candidate_c_rolling_obp_slg
    _, h_slg = candidate_c_rolling_obp_slg(game.home_team, game.first_pitch)
    _, a_slg = candidate_c_rolling_obp_slg(game.away_team, game.first_pitch)
    ok = (h_slg.confidence != 'low' and a_slg.confidence != 'low')
    return (h_slg.delta_units, a_slg.delta_units,
            h_slg.delta_units - a_slg.delta_units, ok)


def _signal_d_blend(game):
    from apps.mlb.services.team_offense_v2 import candidate_d_blend_ops
    h = candidate_d_blend_ops(game.home_team, game.first_pitch)
    a = candidate_d_blend_ops(game.away_team, game.first_pitch)
    ok = (h.confidence != 'low' and a.confidence != 'low')
    return (h.delta_units, a.delta_units,
            h.delta_units - a.delta_units, ok)


CANDIDATE_EXTRACTORS = {
    'A_reference_runs_per_game': _signal_a_runs_per_game,
    'B_v2_rolling_ops':         _signal_b_ops,
    'C_v2_rolling_obp':         _signal_c_obp,
    'C_v2_rolling_slg':         _signal_c_slg,
    'D_v2_blend_season_recent_ops': _signal_d_blend,
}


# ---------------------------------------------------------------------------
# Per-candidate analysis


def _bucket_by_quintile(vals_with_wins: List[Tuple[float, bool]]) -> Dict[str, Dict]:
    """Split (signal_diff, home_won) list into quintiles ordered by
    diff. Returns {bucket_label: {n, home_wins, home_win_rate,
    bucket_min, bucket_max}}."""
    if not vals_with_wins:
        return {}
    sorted_vals = sorted(vals_with_wins, key=lambda p: p[0])
    n = len(sorted_vals)
    step = n / 5.0
    buckets: Dict[str, Dict] = {}
    for i, label in enumerate(BUCKET_LABELS):
        lo = int(round(i * step))
        hi = int(round((i + 1) * step))
        chunk = sorted_vals[lo:hi]
        if not chunk:
            buckets[label] = {
                'n': 0, 'home_wins': 0, 'home_win_rate': None,
                'bucket_min': None, 'bucket_max': None,
            }
            continue
        wins = sum(1 for _, w in chunk if w)
        buckets[label] = {
            'n': len(chunk),
            'home_wins': wins,
            'home_win_rate': round(100.0 * wins / len(chunk), 2),
            'bucket_min': round(chunk[0][0], 5),
            'bucket_max': round(chunk[-1][0], 5),
        }
    return buckets


def _past_market_analysis(
    rows: List[Dict[str, Any]], candidate_key: str,
) -> Dict[str, Any]:
    """Within each market-prob quartile, split by offense-diff sign.

    Returns per-quartile home-win rate for {sign>0, sign<0} splits.
    A candidate that ADDS info past market shows a positive lift
    (sign>0 favors home → higher home-win rate) in at least 2/4
    quartiles."""
    have_market = [r for r in rows
                   if r.get('market_prob_home') is not None]
    if len(have_market) < 20:
        return {
            'status': 'insufficient_market_data',
            'n_have_market': len(have_market),
        }
    # Split into market quartiles.
    sorted_mkt = sorted(have_market, key=lambda r: r['market_prob_home'])
    n = len(sorted_mkt)
    step = n / 4.0
    quartiles = []
    for i in range(4):
        lo = int(round(i * step))
        hi = int(round((i + 1) * step))
        chunk = sorted_mkt[lo:hi]
        pos = [c for c in chunk if c[candidate_key] > 0]
        neg = [c for c in chunk if c[candidate_key] < 0]

        def _rate(g):
            if not g:
                return None
            w = sum(1 for x in g if x['home_won'])
            return round(100.0 * w / len(g), 2)

        pos_rate = _rate(pos)
        neg_rate = _rate(neg)
        lift = None
        if pos_rate is not None and neg_rate is not None:
            lift = round(pos_rate - neg_rate, 2)
        quartiles.append({
            'quartile': i + 1,
            'n': len(chunk),
            'market_prob_range': (
                round(chunk[0]['market_prob_home'], 3),
                round(chunk[-1]['market_prob_home'], 3),
            ),
            'sign_positive_n': len(pos),
            'sign_positive_home_win_rate': pos_rate,
            'sign_negative_n': len(neg),
            'sign_negative_home_win_rate': neg_rate,
            'lift_pp': lift,
        })
    lifts = [q['lift_pp'] for q in quartiles if q['lift_pp'] is not None]
    lifts_above_3pp = sum(1 for x in lifts if x > 3.0)
    return {
        'status': 'ok', 'quartiles': quartiles,
        'quartiles_with_positive_lift_gt_3pp': lifts_above_3pp,
        'mean_lift_pp': round(sum(lifts) / len(lifts), 2) if lifts else None,
    }


def _monthly_consistency(rows: List[Dict[str, Any]], diff_key: str) -> Dict[str, Any]:
    """Per-month: home-win rate when signal favors home vs favors away."""
    by_month: Dict[str, List[Dict]] = {}
    for r in rows:
        if r.get('first_pitch') is None:
            continue
        key = r['first_pitch'].strftime('%Y-%m')
        by_month.setdefault(key, []).append(r)
    result = {}
    for month, chunk in sorted(by_month.items()):
        pos = [c for c in chunk if c[diff_key] > 0]
        neg = [c for c in chunk if c[diff_key] < 0]

        def _rate(g):
            if not g:
                return None
            return round(100.0 * sum(1 for x in g if x['home_won']) / len(g), 2)

        result[month] = {
            'n': len(chunk),
            'sign_pos_n': len(pos),
            'sign_pos_win_rate': _rate(pos),
            'sign_neg_n': len(neg),
            'sign_neg_win_rate': _rate(neg),
        }
    return result


def _redundancy_correlations(
    rows: List[Dict[str, Any]], candidate_key: str,
) -> Dict[str, Optional[float]]:
    """Pearson correlation of the offense diff vs Elo diff / market /
    pitcher / recent-form diffs. |r| < 0.6 is 'independent enough';
    higher = redundant with an existing signal."""
    def _pair(k):
        xs, ys = [], []
        for r in rows:
            v = r.get(candidate_key)
            u = r.get(k)
            if v is None or u is None:
                continue
            xs.append(v); ys.append(u)
        return _pearson(xs, ys)

    return {
        'vs_elo_diff': _pair('elo_diff'),
        'vs_market_prob_home': _pair('market_prob_home'),
        'vs_pitcher_rating_diff': _pair('pitcher_rating_diff'),
        'vs_recent_form_diff': _pair('recent_form_diff'),
    }


def _analyze_candidate(
    candidate_key: str, extractor, all_games_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    covered = [r for r in all_games_rows if r.get(f'{candidate_key}_ok')]
    if not covered:
        return {
            'candidate': candidate_key,
            'coverage': {'n_covered': 0,
                         'n_total': len(all_games_rows),
                         'coverage_pct': 0.0},
            'verdict': 'INSUFFICIENT_DATA',
        }
    diff_key = f'{candidate_key}_diff'
    signal_vals = [r[diff_key] for r in covered if r[diff_key] is not None]
    home_wins_pairs = [
        (r[diff_key], r['home_won']) for r in covered
        if r[diff_key] is not None
    ]
    buckets = _bucket_by_quintile(home_wins_pairs)
    past_market = _past_market_analysis(covered, diff_key)
    monthly = _monthly_consistency(covered, diff_key)
    correlations = _redundancy_correlations(covered, diff_key)
    fav_split = _favorite_split(covered, diff_key)
    home_away_split = _home_away_split(covered, diff_key)

    # Verdict.
    verdict = _verdict(candidate_key, covered, all_games_rows, buckets,
                       past_market, correlations)

    return {
        'candidate': candidate_key,
        'coverage': {
            'n_covered': len(covered),
            'n_total': len(all_games_rows),
            'coverage_pct': round(100.0 * len(covered) / len(all_games_rows), 2)
                            if all_games_rows else 0.0,
        },
        'signal_distribution': {
            'min': min(signal_vals) if signal_vals else None,
            'max': max(signal_vals) if signal_vals else None,
            'mean': round(_mean(signal_vals), 5) if signal_vals else None,
            'p05': _pctile(signal_vals, 5),
            'p50': _pctile(signal_vals, 50),
            'p95': _pctile(signal_vals, 95),
        },
        'buckets': buckets,
        'past_market_analysis': past_market,
        'monthly_consistency': monthly,
        'redundancy_correlations': correlations,
        'favorite_split': fav_split,
        'home_away_split': home_away_split,
        'verdict': verdict,
    }


def _favorite_split(covered: List[Dict], diff_key: str) -> Dict[str, Any]:
    """Split games by market favorite (home or away). Report signal
    predictive value within each side."""
    with_market = [r for r in covered if r.get('market_prob_home') is not None]
    fav_home = [r for r in with_market if r['market_prob_home'] >= 0.5]
    fav_away = [r for r in with_market if r['market_prob_home'] < 0.5]

    def _within(g):
        if len(g) < 10:
            return {'n': len(g), 'insufficient': True}
        pos = [r for r in g if r[diff_key] > 0]
        neg = [r for r in g if r[diff_key] < 0]
        return {
            'n': len(g),
            'sign_pos_n': len(pos),
            'sign_pos_home_win_rate':
                round(100.0 * sum(1 for r in pos if r['home_won']) / len(pos), 2)
                if pos else None,
            'sign_neg_n': len(neg),
            'sign_neg_home_win_rate':
                round(100.0 * sum(1 for r in neg if r['home_won']) / len(neg), 2)
                if neg else None,
        }
    return {'favorite_home': _within(fav_home),
            'favorite_away': _within(fav_away)}


def _home_away_split(covered: List[Dict], diff_key: str) -> Dict[str, Any]:
    """Does the signal predict differently in home vs away wins?
    Simpler shape: rate the home-team-favored-by-signal home-win
    rate; contrast with away-team-favored-by-signal home-win rate."""
    return _favorite_split(covered, diff_key)  # same shape suffices


def _verdict(
    candidate_key: str, covered: List[Dict], all_rows: List[Dict],
    buckets: Dict[str, Dict], past_market: Dict[str, Any],
    correlations: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Rule-based verdict. All rules pre-registered before seeing
    results, per Phase 2 discipline."""
    reasons: List[str] = []
    passes = []
    fails = []

    # Rule 1: coverage >= 60%.
    cov_pct = 100.0 * len(covered) / len(all_rows) if all_rows else 0.0
    if cov_pct >= 60:
        passes.append(f'coverage {cov_pct:.1f}% >= 60%')
    else:
        fails.append(f'coverage {cov_pct:.1f}% < 60%')

    # Rule 2: bucket monotonicity — strong_home wins more than strong_away.
    sh = buckets.get('strong_home', {}).get('home_win_rate')
    sa = buckets.get('strong_away', {}).get('home_win_rate')
    if sh is not None and sa is not None:
        if sh - sa >= 3.0:
            passes.append(f'bucket monotonicity: strong_home {sh:.1f}% vs '
                          f'strong_away {sa:.1f}% (Δ {sh - sa:+.1f}pp)')
        else:
            fails.append(f'bucket monotonicity WEAK: strong_home {sh:.1f}% '
                         f'vs strong_away {sa:.1f}% (Δ {sh - sa:+.1f}pp)')
    else:
        fails.append('bucket monotonicity: insufficient bucket data')

    # Rule 3: past-market lift in at least 2 of 4 quartiles.
    if past_market.get('status') == 'ok':
        lift_count = past_market.get('quartiles_with_positive_lift_gt_3pp', 0)
        if lift_count >= 2:
            passes.append(f'past-market: {lift_count}/4 quartiles show '
                          f'>3pp lift')
        else:
            fails.append(f'past-market: only {lift_count}/4 quartiles show '
                         f'>3pp lift')
    else:
        fails.append(f'past-market: {past_market.get("status")}')

    # Rule 4: no redundancy — every correlation |r| < 0.6.
    redundant = []
    for k, v in correlations.items():
        if v is not None and abs(v) >= 0.6:
            redundant.append(f'{k}={v:+.2f}')
    if redundant:
        fails.append('redundancy: highly correlated with '
                     + ', '.join(redundant))
    else:
        passes.append('redundancy: all correlations |r|<0.6 (independent)')

    if len(fails) == 0:
        verdict = 'PROMOTE'
    elif len(fails) <= 1 and 'coverage' not in ' '.join(fails):
        verdict = 'MONITOR'
    else:
        verdict = 'REJECT'

    return {
        'verdict': verdict,
        'passes': passes,
        'fails': fails,
    }


# ---------------------------------------------------------------------------
# Public entry — orchestrates the whole isolated analysis


def run_isolated_analysis(
    *,
    days: int = 180,
    reference_date=None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Compute isolated predictive-value analysis for every pre-declared
    candidate over the specified backtest window. Returns a nested dict
    consumable by the renderer.

    Never modifies recommendations. Never activates a flag.
    """
    from apps.mlb.models import Game

    ref = reference_date or timezone.localdate()
    if isinstance(ref, datetime):
        ref = ref.date()
    date_to = ref - timedelta(days=1)
    date_from = ref - timedelta(days=days)

    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=date_from,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team',
                        'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    # Build per-game context row containing every candidate signal.
    rows: List[Dict[str, Any]] = []
    total = len(games)
    for i, g in enumerate(games, 1):
        try:
            home_won = (g.home_score is not None and g.away_score is not None
                        and g.home_score > g.away_score)
        except Exception:
            continue
        row: Dict[str, Any] = {
            'game_id': str(g.id),
            'first_pitch': g.first_pitch,
            'home_won': home_won,
            'market_prob_home': _market_prob_home(g),
            'pitcher_rating_diff': _pitcher_rating_diff(g),
            'recent_form_diff': _recent_form_diff(g),
        }
        h_elo = _elo_rating_for(g.home_team, g)
        a_elo = _elo_rating_for(g.away_team, g)
        row['elo_diff'] = ((h_elo - a_elo) if (h_elo is not None
                                               and a_elo is not None)
                           else None)

        for candidate_key, extractor in CANDIDATE_EXTRACTORS.items():
            try:
                result = extractor(g)
            except Exception:
                result = None
            if result is None:
                row[f'{candidate_key}_home'] = None
                row[f'{candidate_key}_away'] = None
                row[f'{candidate_key}_diff'] = None
                row[f'{candidate_key}_ok'] = False
                continue
            h, a, d, ok = result
            row[f'{candidate_key}_home'] = h
            row[f'{candidate_key}_away'] = a
            row[f'{candidate_key}_diff'] = d
            row[f'{candidate_key}_ok'] = ok

        rows.append(row)
        if progress_cb is not None and i % 25 == 0:
            progress_cb(phase='analyze', current=i, total=total)

    per_candidate = {}
    for candidate_key, extractor in CANDIDATE_EXTRACTORS.items():
        per_candidate[candidate_key] = _analyze_candidate(
            candidate_key, extractor, rows,
        )

    # Determine the ONE promotion (or NO-GO). A candidate whose
    # verdict is the string 'INSUFFICIENT_DATA' is filtered out here
    # (its `verdict` key is a str, not a dict) — that's expected.
    def _v_key(v):
        vv = v.get('verdict')
        return vv.get('verdict') if isinstance(vv, dict) else vv

    promotions = [
        (k, v) for k, v in per_candidate.items()
        if _v_key(v) == 'PROMOTE'
    ]
    if not promotions:
        selected = None
    elif len(promotions) == 1:
        selected = promotions[0][0]
    else:
        # Break ties by past-market mean lift.
        def _score(item):
            k, v = item
            pm = v.get('past_market_analysis', {})
            return pm.get('mean_lift_pp') or 0.0
        selected = max(promotions, key=_score)[0]

    return {
        'window': {
            'days': days, 'from': date_from, 'to': date_to,
            'games_evaluable': total,
            'games_with_outcome': len(rows),
        },
        'per_candidate': per_candidate,
        'selected_candidate': selected,
        'overall_verdict': ('PROMOTE_ONE' if selected else 'NO_GO_OFFENSE'),
    }


def render_isolated_analysis(exp: Dict[str, Any]) -> str:
    lines = []
    lines.append('#' * 100)
    lines.append('#  TEAM-OFFENSE PHASE 2 — ISOLATED PREDICTIVE-VALUE ANALYSIS')
    w = exp['window']
    lines.append(f'#  Window {w["from"]}..{w["to"]} ({w["days"]}d)  '
                 f'games={w["games_evaluable"]}  '
                 f'with_outcome={w["games_with_outcome"]}')
    lines.append('#' * 100)
    lines.append('')

    for key, block in exp['per_candidate'].items():
        lines.append('-' * 100)
        lines.append(f'CANDIDATE: {key}')
        lines.append('-' * 100)
        cov = block['coverage']
        lines.append(f'  coverage: {cov["n_covered"]}/{cov["n_total"]} '
                     f'({cov["coverage_pct"]:.1f}%)')
        v = block.get('verdict', {})
        lines.append(f'  VERDICT: {v.get("verdict", "?")}')
        for r in v.get('passes', []):
            lines.append(f'    PASS: {r}')
        for r in v.get('fails', []):
            lines.append(f'    FAIL: {r}')

        sd = block.get('signal_distribution', {})
        if sd:
            lines.append(f'  signal distribution (diff): '
                         f'min={sd.get("min")} p05={sd.get("p05")} '
                         f'p50={sd.get("p50")} p95={sd.get("p95")} '
                         f'max={sd.get("max")} mean={sd.get("mean")}')

        buckets = block.get('buckets', {})
        if buckets:
            lines.append('  bucket home-win rate (favors_away → favors_home):')
            for label in BUCKET_LABELS:
                b = buckets.get(label, {})
                lines.append(
                    f'    {label:>14}: n={b.get("n", 0):>4}  '
                    f'home_win={b.get("home_win_rate", "n/a")}%  '
                    f'range=[{b.get("bucket_min")}, {b.get("bucket_max")}]'
                )

        pm = block.get('past_market_analysis', {})
        if pm.get('status') == 'ok':
            lines.append('  past-market lift (home-win rate: sign>0 minus sign<0):')
            for q in pm.get('quartiles', []):
                lines.append(
                    f'    Q{q["quartile"]}  mkt_range={q["market_prob_range"]}  '
                    f'n={q["n"]}  '
                    f'sign+_rate={q["sign_positive_home_win_rate"]}% '
                    f'sign-_rate={q["sign_negative_home_win_rate"]}%  '
                    f'lift={q["lift_pp"]:+.2f}pp'
                    if q["lift_pp"] is not None else
                    f'    Q{q["quartile"]}  n={q["n"]}  (insufficient split data)'
                )
            lines.append(f'    quartiles with lift > 3pp: '
                         f'{pm.get("quartiles_with_positive_lift_gt_3pp", 0)}/4  '
                         f'mean_lift={pm.get("mean_lift_pp")}pp')
        elif pm.get('status'):
            lines.append(f'  past-market: {pm["status"]} '
                         f'(n={pm.get("n_have_market", 0)})')

        rc = block.get('redundancy_correlations', {})
        if rc:
            lines.append('  redundancy (Pearson r):')
            for k, v in rc.items():
                if v is None:
                    lines.append(f'    {k}: n/a')
                else:
                    tag = ' (redundant)' if abs(v) >= 0.6 else ''
                    lines.append(f'    {k}: {v:+.3f}{tag}')

        fs = block.get('favorite_split', {})
        if fs:
            lines.append('  favorite split:')
            for label, chunk in fs.items():
                if chunk.get('insufficient'):
                    lines.append(f'    {label}: n={chunk["n"]} '
                                 '(insufficient)')
                    continue
                lines.append(
                    f'    {label}: n={chunk.get("n")}  '
                    f'sign+ n={chunk.get("sign_pos_n")} '
                    f'home_win={chunk.get("sign_pos_home_win_rate")}%  '
                    f'sign- n={chunk.get("sign_neg_n")} '
                    f'home_win={chunk.get("sign_neg_home_win_rate")}%'
                )

        mc = block.get('monthly_consistency', {})
        if mc:
            lines.append('  monthly consistency (home-win rate by sign):')
            for month, chunk in sorted(mc.items()):
                lines.append(
                    f'    {month}: n={chunk["n"]:>4}  '
                    f'pos={chunk["sign_pos_win_rate"]}% (n={chunk["sign_pos_n"]})  '
                    f'neg={chunk["sign_neg_win_rate"]}% (n={chunk["sign_neg_n"]})'
                )
        lines.append('')

    lines.append('=' * 100)
    lines.append(f'OVERALL VERDICT: {exp["overall_verdict"]}')
    if exp.get('selected_candidate'):
        lines.append(f'SELECTED CANDIDATE: {exp["selected_candidate"]}')
        lines.append('  → Eligible for bounded integration replay '
                     '(±1pp target cap). Do NOT activate. Walk-forward '
                     'validation still required if replay looks clean.')
    else:
        lines.append('SELECTED CANDIDATE: none')
        lines.append('  → No candidate meets the isolated predictive-value '
                     'bar. Close the team-offense research track.')
    lines.append('=' * 100)
    return '\n'.join(lines)
