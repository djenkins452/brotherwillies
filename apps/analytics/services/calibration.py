"""Model calibration audit — read-only.

For every game in a date window, runs `_simulate_recommendation` at the
chosen blend weight (default 0.55), then buckets the resulting picks by
the model's pick_prob and compares:

    predicted (bucket midpoint)  vs  actual win rate

across these buckets:
    [0.55, 0.60)  [0.60, 0.65)  [0.65, 0.70)  [0.70, 0.75)  [0.75, 1.0]

Calibration error per bucket = actual − predicted_midpoint (signed).
Positive = model is UNDER-confident (actually wins more than it claims).
Negative = model is OVER-confident (actually wins less than it claims).

Two scopes:
  - LANE-CORRECTED: only sims that pass `is_lane_corrected_recommended`
    (the production-equivalent recommended population). This is the
    calibration that matters operationally.
  - ALL SIMS: every sim that successfully ran, regardless of status.
    Lets you check whether the lane filter is selecting better-calibrated
    picks or just rejecting low-quality ones.

Read-only. Never re-runs decision rules — every value comes from
SimulatedRecommendation fields already produced by the replay.
"""
from __future__ import annotations

from datetime import date
from typing import Optional


# Bucket edges (lower-inclusive, upper-exclusive except the last).
_CALIB_BUCKETS = [
    ('0.55–0.60', 0.55, 0.60),
    ('0.60–0.65', 0.60, 0.65),
    ('0.65–0.70', 0.65, 0.70),
    ('0.70–0.75', 0.70, 0.75),
    ('0.75+',     0.75, 1.01),   # 1.01 so the upper bound includes 1.0
]


def _bucket_for(prob):
    for label, lo, hi in _CALIB_BUCKETS:
        if lo <= prob < hi:
            return label, (lo + hi) / 2.0
    return None, None


def _empty_bucket():
    return {
        'count': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'pending': 0,
        'predicted_pct': None,    # bucket midpoint × 100
        'actual_pct': None,       # wins / (wins+losses) × 100
        'calibration_error_pp': None,   # actual − predicted (pp)
        'roi_pct': None,
        'net_pl': 0.0,
        'avg_clv': None,
        'clv_sample': 0,
        'clv_beat': 0, 'clv_matched': 0, 'clv_lost': 0,
    }


def _add_sim(bucket, sim):
    bucket['count'] += 1
    if sim.won is True:
        bucket['wins'] += 1
    elif sim.won is False:
        bucket['losses'] += 1
    elif sim.won is None and getattr(sim, 'home_score', None) is None:
        bucket['pending'] += 1
    else:
        bucket['pushes'] += 1


def _finalize_bucket(bucket, *, midpoint):
    from apps.core.utils.odds import american_to_decimal
    decisive = bucket['wins'] + bucket['losses']
    if decisive == 0:
        return
    bucket['predicted_pct'] = round(midpoint * 100.0, 2)
    bucket['actual_pct'] = round(100.0 * bucket['wins'] / decisive, 2)
    bucket['calibration_error_pp'] = round(
        bucket['actual_pct'] - bucket['predicted_pct'], 2
    )


def _add_pl_and_clv(buckets_by_label, sims):
    """Walk sims once more to compute per-bucket ROI + CLV from the same
    flat-$100 convention used elsewhere in the audit tooling."""
    from apps.core.utils.odds import american_to_decimal
    for sim in sims:
        label, _ = _bucket_for(sim.pick_prob)
        if label is None:
            continue
        b = buckets_by_label[label]
        # Stake / P/L (flat $100)
        if sim.won is True:
            dec = american_to_decimal(int(sim.pick_odds))
            b['net_pl'] += round((dec - 1.0) * 100.0, 2)
        elif sim.won is False:
            b['net_pl'] += -100.0
        # CLV
        if sim.clv_decimal is not None:
            b['clv_sample'] += 1
            if sim.clv_decimal > 0:
                b['clv_beat'] += 1
            elif sim.clv_decimal < 0:
                b['clv_lost'] += 1
            else:
                b['clv_matched'] += 1


def _finalize_pl_clv(bucket):
    settled = bucket['wins'] + bucket['losses'] + bucket['pushes']
    if settled > 0:
        stake = settled * 100.0
        bucket['roi_pct'] = round(bucket['net_pl'] / stake * 100.0, 2)
    if bucket['clv_sample'] > 0:
        # avg CLV could be re-derived; simpler: leave avg_clv None unless
        # we capture the sum. Keeping the mix counts is the high-value bit.
        pass


def build_calibration(date_from: date, date_to: date,
                      *, blend_weight: float = 0.55) -> dict:
    """Bucket the lane-corrected replay set by pick_prob and report
    predicted vs actual win rate per bucket."""
    from apps.analytics.services.method_replay import run_replay

    replay = run_replay(date_from, date_to, [blend_weight])
    variant = replay['variants'][0]
    sims = variant['simulations']

    lane_corrected = [s for s in sims if s.is_lane_corrected_recommended]
    all_sims = sims

    def _build(sims_subset):
        buckets = {label: _empty_bucket() for label, _, _ in _CALIB_BUCKETS}
        midpoints = {label: (lo + hi) / 2.0 for label, lo, hi in _CALIB_BUCKETS}
        for sim in sims_subset:
            label, mid = _bucket_for(sim.pick_prob)
            if label is None:
                continue
            _add_sim(buckets[label], sim)
        _add_pl_and_clv(buckets, sims_subset)
        for label in buckets:
            _finalize_bucket(buckets[label], midpoint=midpoints[label])
            _finalize_pl_clv(buckets[label])
        return [(label, buckets[label]) for label, _, _ in _CALIB_BUCKETS]

    return {
        'window': {'from': date_from, 'to': date_to,
                   'blend_weight': blend_weight},
        'lane_corrected_buckets': _build(lane_corrected),
        'all_sims_buckets': _build(all_sims),
        'totals': {
            'all_sims': len(all_sims),
            'lane_corrected': len(lane_corrected),
            'games_evaluable': replay['total_games_evaluable'],
        },
    }


def render_calibration(c: dict) -> str:
    w = c['window']
    lines = []
    lines.append('#' * 110)
    lines.append(f"#  MODEL CALIBRATION — blend {w['blend_weight']:.2f}")
    lines.append(f"#  Window: {w['from']} → {w['to']}")
    lines.append('#' * 110)
    lines.append('')
    t = c['totals']
    lines.append(
        f"  Games evaluable: {t['games_evaluable']}   "
        f"All sims: {t['all_sims']}   "
        f"Lane-corrected (production-equivalent): {t['lane_corrected']}"
    )
    lines.append('')

    def _block(label, rows):
        lines.append('=' * 110)
        lines.append(f"  {label}")
        lines.append('=' * 110)
        lines.append(
            f"  {'Bucket':<14}  {'n':>5}  {'W-L-P':>10}  "
            f"{'predicted':>11}  {'actual':>9}  {'error':>10}  "
            f"{'ROI':>8}  {'CLV beat/match/lost':>22}"
        )
        for blabel, b in rows:
            wlp = f"{b['wins']}-{b['losses']}-{b['pushes']}"
            pred = f"{b['predicted_pct']:.1f}%" if b['predicted_pct'] is not None else '—'
            actu = f"{b['actual_pct']:.1f}%" if b['actual_pct'] is not None else '—'
            err = b['calibration_error_pp']
            err_str = f"{err:+.1f}pp" if err is not None else '—'
            roi = f"{b['roi_pct']:+.1f}%" if b['roi_pct'] is not None else '—'
            clv_mix = f"{b['clv_beat']}/{b['clv_matched']}/{b['clv_lost']}"
            clv_str = f"{clv_mix} (n={b['clv_sample']})"
            lines.append(
                f"  {blabel:<14}  {b['count']:>5}  {wlp:>10}  "
                f"{pred:>11}  {actu:>9}  {err_str:>10}  "
                f"{roi:>8}  {clv_str:>22}"
            )
        lines.append('')

    _block(
        'LANE-CORRECTED (production-equivalent — the calibration that matters operationally)',
        c['lane_corrected_buckets'],
    )
    _block(
        'ALL SIMS (every successful simulation regardless of status — for comparison)',
        c['all_sims_buckets'],
    )

    lines.append('-' * 110)
    lines.append('  HOW TO READ THIS')
    lines.append('-' * 110)
    lines.append("  - error +Xpp means the model is UNDER-confident in that bucket")
    lines.append("    (actually wins MORE than predicted). Safer to trust.")
    lines.append("  - error -Xpp means the model is OVER-confident in that bucket")
    lines.append("    (actually wins LESS than predicted). The picks are weaker than the")
    lines.append("    confidence number suggests.")
    lines.append("  - Buckets with n < 30 are directional only — wide CIs.")
    lines.append('')
    return '\n'.join(lines) + '\n'
