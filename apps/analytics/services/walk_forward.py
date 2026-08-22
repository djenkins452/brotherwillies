"""Walk-Forward Optimization Study Harness (v3 → ≥60% out-of-sample).

Answers ONE question:

  Can the existing v3 information set (blend=0.55, starter recent form
  active) be transformed into a robust ≥60% OUT-OF-SAMPLE moneyline
  recommendation system through better selection/threshold rules?

READ-ONLY. STAFF-ONLY. NO WRITES. NO MODIFICATIONS to any production
decision path. This module simulates alternative gate/threshold rules
against historical MLB game data, holds out temporal windows, and
reports out-of-sample performance with statistical uncertainty.

METHOD

  1. For each game in the study window, run the existing v3 sim
     (apps.analytics.services.method_replay._simulate_recommendation)
     ONCE at blend=0.55 with use_recent_form=True. This gives us
     each game's raw edge_pp, pick_prob, pick_odds, won, clv — plus
     the production lane classification.

  2. For each candidate configuration (a CandidateConfig), re-apply
     a mirrored `apply_candidate_gates` function against the cached
     sim outputs — no re-simulation. Gate mirror is locked against
     production compute_status by test (default config → identical
     status/reason on a matrix of inputs).

  3. Walk-forward: divide the window into expanding-training +
     forward-holdout folds. In each fold, select the training-window
     winner (by primary objective = win_rate then ROI, subject to
     min-sample), then evaluate that selected candidate on the
     UNSEEN holdout window. Aggregate holdout results across all
     folds.

  4. Report per-fold selection log, per-candidate held-out aggregate
     (as-if selected every fold), and the true walk-forward
     aggregate (the selected candidate per fold), all with Wilson
     95% intervals.

  5. Also cross-tab the 60–65% bucket at baseline against odds,
     edge, tier, side, and risk flags to identify what actually
     drives that bucket's underperformance.

WHAT THIS HARNESS DOES NOT DO

  - It does not change any live methodology. Every production
    threshold, gate, and constant remains untouched.
  - It does not add new predictive features. Every candidate uses
    the same v3 information set.
  - It does not tune the sim itself. It tunes the gate that
    accepts or rejects the sim's output.

LEAKAGE SAFEGUARDS

  All L1–L5 safeguards from `method_replay._simulate_recommendation`
  are inherited (opening pre-game snapshot only, historical Elo,
  outcome only used for the `won` field). The walk-forward layer
  adds one additional safeguard:

  L6. Fold-time selection uses TRAIN-WINDOW sims only. The HOLDOUT
      window sims are never inspected during selection. Enforced by
      construction (train_sims and holdout_sims are separate
      variable bindings) and by test.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# --- Locked mirror of production gate constants ------------------------------
# Imported by NAME so a test can assert the mirror stays in sync. If any
# of these values change in production, the default CandidateConfig moves
# with them automatically.
from apps.core.services.recommendations import (
    HARD_MIN_PROBABILITY,
    MIN_PROBABILITY_FOR_RECOMMENDED,
    MIN_EDGE,
    STRONG_EDGE,
    HEAVY_FAVORITE_ODDS,
    MAX_ABS_ODDS_FOR_RECOMMENDED,
    STATUS_RECOMMENDED,
    STATUS_NOT_RECOMMENDED,
)


# ---------------------------------------------------------------------------
# Candidate configuration


@dataclass(frozen=True)
class CandidateConfig:
    """A single rule variant of the recommendation gate.

    Defaults mirror production. Overrides are ADDITIVE and TIGHTENING
    only — a per-bucket override may only make the gate stricter, never
    looser, because the production lane hard-gates already enforce the
    baseline floors and we don't experiment with lane logic here.
    """

    label: str
    min_probability: float = MIN_PROBABILITY_FOR_RECOMMENDED   # 0.60
    min_edge_pp: float = MIN_EDGE                              # 6.0
    max_abs_odds: float = MAX_ABS_ODDS_FOR_RECOMMENDED         # 300
    heavy_favorite_odds: float = HEAVY_FAVORITE_ODDS           # -150
    strong_edge_pp: float = STRONG_EDGE                        # 6.0

    # Bucket-specific tightening. `None` means "no override — use base".
    # short_fav bucket = pick_odds in [-149, +99] (matches replay's own
    # aggregation and the user's cited production-evidence bucket).
    short_fav_min_probability: Optional[float] = None
    short_fav_min_edge_pp: Optional[float] = None
    # heavy_fav bucket = pick_odds <= -200 (matches replay's aggregation).
    heavy_fav_min_probability: Optional[float] = None
    heavy_fav_min_edge_pp: Optional[float] = None


def apply_candidate_gates(
    *,
    edge_pp: Optional[float],
    pick_odds: Optional[int],
    pick_prob: Optional[float],
    config: CandidateConfig,
) -> Tuple[str, str]:
    """Mirror of `apps.core.services.recommendations.compute_status`, but
    with per-candidate thresholds and optional per-bucket tightening.

    LOCKED against production: when `config` is `CandidateConfig(label=…)`
    with no overrides, the returned (status, reason) is identical to
    `compute_status(edge_pp, pick_odds, probability=pick_prob,
    is_secondary=False)` on all inputs. Enforced by
    `apps/analytics/test_walk_forward.py::GateMirrorLockTests`.
    """
    if edge_pp is None:
        return STATUS_NOT_RECOMMENDED, 'low_edge'

    # Resolve effective thresholds, applying bucket-scoped tightening.
    min_prob = config.min_probability
    min_edge = config.min_edge_pp
    if pick_odds is not None:
        if -149 <= pick_odds <= 99:
            if config.short_fav_min_probability is not None:
                min_prob = max(min_prob, config.short_fav_min_probability)
            if config.short_fav_min_edge_pp is not None:
                min_edge = max(min_edge, config.short_fav_min_edge_pp)
        elif pick_odds <= -200:
            if config.heavy_fav_min_probability is not None:
                min_prob = max(min_prob, config.heavy_fav_min_probability)
            if config.heavy_fav_min_edge_pp is not None:
                min_edge = max(min_edge, config.heavy_fav_min_edge_pp)

    # Gate 1 — HARD probability floor (unchanged from production; 0.50).
    if pick_prob is not None and pick_prob < HARD_MIN_PROBABILITY:
        if edge_pp >= min_edge:
            return STATUS_NOT_RECOMMENDED, 'value'
        return STATUS_NOT_RECOMMENDED, 'low_edge'

    # Gate 2 — longshot ceiling on |odds|.
    if pick_odds is not None and abs(pick_odds) > config.max_abs_odds:
        return STATUS_NOT_RECOMMENDED, 'longshot'

    # Gate 3 — source. Replay sims are all primary by filter (only_primary
    # in _pregame_snapshots), so this gate never fires and we skip it.

    # Gate 4 — candidate probability floor.
    if pick_prob is not None and pick_prob < min_prob:
        return STATUS_NOT_RECOMMENDED, 'low_probability'

    # Gate 5 — candidate edge floor.
    if edge_pp < min_edge:
        return STATUS_NOT_RECOMMENDED, 'low_edge'

    # Gate 6 — heavy-favorite juice floor.
    if (
        pick_odds is not None
        and pick_odds <= config.heavy_favorite_odds
        and edge_pp < config.strong_edge_pp
    ):
        return STATUS_NOT_RECOMMENDED, 'high_juice'

    return STATUS_RECOMMENDED, ''


# ---------------------------------------------------------------------------
# Default candidate grid — spec directly maps to the phase-3 grid in the
# 2026-08-22 user brief. Every candidate is a STRICT tightening of the
# baseline (or the baseline itself). No candidate loosens any gate — the
# lane hard-gates would block that anyway.

DEFAULT_CANDIDATE_GRID: Tuple[CandidateConfig, ...] = (
    # 1. Baseline V3 — MUST be first for reporting.
    CandidateConfig(label='v3_baseline (prob>=0.60, edge>=6)'),

    # 2. Probability-floor sweep (edge held at baseline 6pp).
    CandidateConfig(label='prob>=0.62, edge>=6', min_probability=0.62),
    CandidateConfig(label='prob>=0.63, edge>=6', min_probability=0.63),
    CandidateConfig(label='prob>=0.65, edge>=6', min_probability=0.65),

    # 3. Edge-floor sweep (probability held at baseline 0.60).
    CandidateConfig(label='prob>=0.60, edge>=7', min_edge_pp=7.0),
    CandidateConfig(label='prob>=0.60, edge>=8', min_edge_pp=8.0),

    # 4. Interactions.
    CandidateConfig(label='prob>=0.62, edge>=7', min_probability=0.62, min_edge_pp=7.0),
    CandidateConfig(label='prob>=0.63, edge>=7', min_probability=0.63, min_edge_pp=7.0),
    CandidateConfig(label='prob>=0.65, edge>=7', min_probability=0.65, min_edge_pp=7.0),
    CandidateConfig(label='prob>=0.65, edge>=8', min_probability=0.65, min_edge_pp=8.0),

    # 5. Short-favorite-specific tightening. Production evidence shows
    #    short-favorite bets at 21-29 / 42.0% / -26.2% ROI — the largest
    #    single-bucket drag. Test whether targeting the interaction
    #    (rather than globally raising floors) recovers it.
    CandidateConfig(
        label='baseline + short_fav prob>=0.65',
        short_fav_min_probability=0.65,
    ),
    CandidateConfig(
        label='baseline + short_fav prob>=0.65 edge>=7',
        short_fav_min_probability=0.65,
        short_fav_min_edge_pp=7.0,
    ),
    CandidateConfig(
        label='baseline + short_fav prob>=0.68',
        short_fav_min_probability=0.68,
    ),

    # 6. Heavy-favorite information — production evidence shows favorites
    #    -150..-300 at 73.5% / +16.3% ROI (strong). Test whether raising
    #    heavy-fav edge to 8pp meaningfully hurts volume without helping
    #    win rate (should degrade, but we want the counterfactual on
    #    record so the "don't over-tighten heavy-fav" claim is defensible).
    CandidateConfig(
        label='baseline + heavy_fav edge>=8',
        heavy_fav_min_edge_pp=8.0,
    ),
)


# ---------------------------------------------------------------------------
# Metric computation


def _american_to_decimal(odds: int) -> float:
    """Convert American moneyline to decimal odds (winner receives
    stake * decimal_odds, so profit = stake * (decimal - 1))."""
    if odds is None:
        return 1.0
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def wilson_interval(
    wins: int, n: int, *, confidence: float = 0.95,
) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the Normal approximation for small n and for
    proportions near 0/1. Returns (low, high) as decimals in [0, 1].
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    # z for 95% = 1.96; for 99% = 2.5758; for 90% = 1.645. Only 95%
    # is exposed in the deliverable so we hard-code it — no scipy dep.
    z = 1.96 if confidence == 0.95 else 2.5758 if confidence == 0.99 else 1.645
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _odds_type(odds: Optional[int]) -> str:
    """Match the replay's own bucketing so metrics compose."""
    if odds is None:
        return 'unknown'
    o = int(odds)
    if o <= -200:
        return 'heavy_fav'
    if -199 <= o <= -150:
        return 'mid_fav'
    if -149 <= o <= 99:
        return 'short_fav'
    if 100 <= o <= 150:
        return 'short_dog'
    if 151 <= o <= 250:
        return 'mid_dog'
    return 'long_dog'


def _confidence_bucket(pick_prob: Optional[float]) -> str:
    if pick_prob is None:
        return 'unknown'
    p = pick_prob * 100.0
    if p < 65:
        return '60-65'
    if p < 70:
        return '65-70'
    if p < 75:
        return '70-75'
    if p < 80:
        return '75-80'
    return '80+'


def _edge_bucket(edge_pp: Optional[float]) -> str:
    if edge_pp is None:
        return 'unknown'
    if edge_pp < 6:
        return '<6'
    if edge_pp < 7:
        return '6-7'
    if edge_pp < 8:
        return '7-8'
    if edge_pp < 10:
        return '8-10'
    return '10+'


def _empty_bucket_row() -> dict:
    return {'n': 0, 'wins': 0, 'losses': 0, 'net_pl': 0.0, 'stake': 0.0}


def _tally_into(row: dict, sim) -> None:
    """Accumulate one $100-stake bet into a bucket row."""
    row['n'] += 1
    row['stake'] += 100.0
    if sim.won is True:
        row['wins'] += 1
        row['net_pl'] += 100.0 * (_american_to_decimal(sim.pick_odds) - 1.0)
    elif sim.won is False:
        row['losses'] += 1
        row['net_pl'] -= 100.0
    # `won is None` (push or unresolved) contributes to n but neither
    # W nor L nor P/L — same treatment as method_replay._compute_metrics.


def compute_candidate_metrics(sims: Iterable, config: CandidateConfig) -> dict:
    """Apply the candidate's gates to `sims` and return aggregate metrics.

    `sims` are `SimulatedRecommendation` instances from
    `method_replay._simulate_recommendation`. This function does NOT re-run
    the sim — it filters cached outputs by re-applying `apply_candidate_gates`
    and requires production lane == 'core' (we're not experimenting with
    lane logic in this study).

    Excludes:
      - lane != 'core'                (production lane hard-gate failure)
      - won is None                   (unresolved / push)
      - candidate gate rejects        (status != 'recommended')
    """
    recommended = []
    for s in sims:
        if s.lane != 'core':
            continue
        if s.won is None:
            continue
        status, _reason = apply_candidate_gates(
            edge_pp=s.edge_pp,
            pick_odds=s.pick_odds,
            pick_prob=s.pick_prob,
            config=config,
        )
        if status != STATUS_RECOMMENDED:
            continue
        recommended.append(s)

    if not recommended:
        return {
            'n': 0, 'wins': 0, 'losses': 0,
            'win_rate': None, 'roi': None, 'net_pl': 0.0,
            'wilson_ci_95': (0.0, 1.0),
            'positive_clv_rate': None, 'avg_clv': None, 'clv_sample': 0,
            'clv_beat': 0, 'clv_matched': 0, 'clv_lost': 0,
            'by_confidence': {}, 'by_odds_type': {}, 'by_edge': {},
        }

    wins = sum(1 for s in recommended if s.won is True)
    losses = sum(1 for s in recommended if s.won is False)
    n = wins + losses
    stake = 100.0 * n
    payout_profit = 0.0
    for s in recommended:
        if s.won is True:
            payout_profit += 100.0 * (_american_to_decimal(s.pick_odds) - 1.0)
        elif s.won is False:
            payout_profit -= 100.0
    win_rate = wins / n if n else None
    roi = payout_profit / stake if stake else None
    ci = wilson_interval(wins, n) if n else (0.0, 1.0)

    # CLV
    clv_vals = [s.clv_decimal for s in recommended if s.clv_decimal is not None]
    clv_beat = sum(1 for c in clv_vals if c > 0)
    clv_matched = sum(1 for c in clv_vals if c == 0)
    clv_lost = sum(1 for c in clv_vals if c < 0)
    positive_clv_rate = clv_beat / len(clv_vals) if clv_vals else None
    avg_clv = sum(clv_vals) / len(clv_vals) if clv_vals else None

    # Buckets
    by_conf: Dict[str, dict] = defaultdict(_empty_bucket_row)
    by_odds: Dict[str, dict] = defaultdict(_empty_bucket_row)
    by_edge: Dict[str, dict] = defaultdict(_empty_bucket_row)
    for s in recommended:
        _tally_into(by_conf[_confidence_bucket(s.pick_prob)], s)
        _tally_into(by_odds[_odds_type(s.pick_odds)], s)
        _tally_into(by_edge[_edge_bucket(s.edge_pp)], s)

    return {
        'n': n,
        'wins': wins,
        'losses': losses,
        'net_pl': payout_profit,
        'win_rate': win_rate,
        'roi': roi,
        'wilson_ci_95': ci,
        'positive_clv_rate': positive_clv_rate,
        'avg_clv': avg_clv,
        'clv_sample': len(clv_vals),
        'clv_beat': clv_beat,
        'clv_matched': clv_matched,
        'clv_lost': clv_lost,
        'by_confidence': dict(by_conf),
        'by_odds_type': dict(by_odds),
        'by_edge': dict(by_edge),
    }


# ---------------------------------------------------------------------------
# Baseline sim — one pass per (blend, form) combo


def simulate_baseline(
    *,
    date_from: date,
    date_to: date,
    blend_weight: float = 0.55,
    use_recent_form: bool = True,
) -> Tuple[List, int]:
    """Run the existing v3 sim once per game in the window. Returns
    (sims, sim_errors). Each sim is a SimulatedRecommendation with all the
    fields the candidate gate function needs.
    """
    # Local import — the analytics service pulls in MLB models etc., and
    # this module is loaded on Django boot via `apps/analytics/views.py`.
    from apps.mlb.models import Game
    from apps.analytics.services.method_replay import _simulate_recommendation

    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=date_from,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    sims: List = []
    errors = 0
    for g in games:
        try:
            sim = _simulate_recommendation(
                g, blend_weight, 'v3_baseline',
                use_recent_form=use_recent_form,
            )
        except Exception:
            errors += 1
            logger.exception(
                'walk_forward: sim failed game=%s', getattr(g, 'id', None),
            )
            continue
        if sim is not None:
            sims.append(sim)
    return sims, errors


def _sim_date(sim) -> date:
    """Extract the date from a sim's first_pitch_iso for fold assignment."""
    return datetime.fromisoformat(sim.first_pitch_iso).date()


# ---------------------------------------------------------------------------
# Selection


def select_winner(
    train_metrics: Dict[str, dict],
    *,
    min_sample: int,
    objective: str,
    default_label: str,
) -> str:
    """Pick the training-window winner by `objective`, subject to min_sample.

    Objectives:
      - 'win_rate_then_roi' — primary per user brief (accuracy first).
      - 'roi'               — legacy peak-backtest objective.
      - 'wilson_lower'      — most conservative; picks the candidate with
                              the highest lower-95% CI on win rate.

    Falls back to `default_label` when no candidate meets min_sample. The
    fallback is what "don't force a change" looks like at fold time.
    """
    eligible = [
        (label, m) for label, m in train_metrics.items()
        if m['n'] >= min_sample and m['win_rate'] is not None
    ]
    if not eligible:
        return default_label
    if objective == 'roi':
        eligible.sort(key=lambda x: -(x[1]['roi'] or -math.inf))
    elif objective == 'wilson_lower':
        eligible.sort(key=lambda x: -x[1]['wilson_ci_95'][0])
    else:  # 'win_rate_then_roi'
        eligible.sort(
            key=lambda x: (
                -(x[1]['win_rate'] or 0.0),
                -(x[1]['roi'] or -math.inf),
            )
        )
    return eligible[0][0]


# ---------------------------------------------------------------------------
# Walk-forward driver


def run_walk_forward(
    *,
    date_from: date,
    date_to: date,
    train_days: int = 30,
    holdout_days: int = 14,
    step_days: int = 14,
    blend_weight: float = 0.55,
    use_recent_form: bool = True,
    candidates: Sequence[CandidateConfig] = DEFAULT_CANDIDATE_GRID,
    min_sample_for_selection: int = 20,
    selection_objective: str = 'win_rate_then_roi',
) -> dict:
    """Expanding-window walk-forward validation.

    For each fold f (holdout window [H_start, H_end]):
      * training window   = [date_from, H_start - 1]           (expanding)
      * evaluate each candidate on the training window
      * SELECT best candidate by objective, subject to min_sample
      * EVALUATE selected candidate on the HELD-OUT window
      * also record every candidate's held-out metrics (counterfactual)
      * advance H_start by step_days

    Returns a report dict; `render_walk_forward` produces the plaintext.

    LEAKAGE (L6): selection consumes train_sims only. holdout_sims is a
    separate binding never inspected during select_winner. Locked by test.
    """
    default_label = candidates[0].label if candidates else 'v3_baseline'

    sims, sim_errors = simulate_baseline(
        date_from=date_from,
        date_to=date_to,
        blend_weight=blend_weight,
        use_recent_form=use_recent_form,
    )

    if not sims:
        return {
            'error': 'no_sims',
            'window': (date_from, date_to),
            'sim_errors': sim_errors,
        }

    # Group sims by first-pitch date for fast fold slicing.
    sims_by_date: Dict[date, list] = defaultdict(list)
    for s in sims:
        sims_by_date[_sim_date(s)].append(s)

    # Build folds.
    folds_meta = []
    fold_start = date_from + timedelta(days=train_days)
    while fold_start + timedelta(days=holdout_days) - timedelta(days=1) <= date_to:
        holdout_end = fold_start + timedelta(days=holdout_days) - timedelta(days=1)
        train_end = fold_start - timedelta(days=1)
        folds_meta.append({
            'train_from': date_from,
            'train_to': train_end,
            'holdout_from': fold_start,
            'holdout_to': holdout_end,
        })
        fold_start += timedelta(days=step_days)

    fold_results = []
    # Per-candidate cumulative held-out list — used for the "as-if selected
    # every fold" counterfactual aggregate. Uses id(sim) as key to dedup
    # sims that appear in overlapping holdout windows (guard for
    # step_days < holdout_days).
    per_candidate_holdout: Dict[str, dict] = {
        c.label: {} for c in candidates
    }
    aggregate_selected: Dict = {}   # keyed id(sim) -> sim

    for fold in folds_meta:
        train_sims = [
            s for d, day_sims in sims_by_date.items()
            if fold['train_from'] <= d <= fold['train_to']
            for s in day_sims
        ]
        holdout_sims = [
            s for d, day_sims in sims_by_date.items()
            if fold['holdout_from'] <= d <= fold['holdout_to']
            for s in day_sims
        ]

        # Score every candidate on training window.
        train_metrics = {
            c.label: compute_candidate_metrics(train_sims, c)
            for c in candidates
        }

        # Select best per objective; fallback = baseline.
        winner_label = select_winner(
            train_metrics,
            min_sample=min_sample_for_selection,
            objective=selection_objective,
            default_label=default_label,
        )
        winner_config = next(c for c in candidates if c.label == winner_label)

        # Evaluate on holdout.
        holdout_selected_metrics = compute_candidate_metrics(
            holdout_sims, winner_config,
        )
        holdout_all_metrics = {
            c.label: compute_candidate_metrics(holdout_sims, c)
            for c in candidates
        }

        # Accumulate held-out sims into per-candidate counterfactual.
        for c in candidates:
            for s in holdout_sims:
                if s.lane != 'core' or s.won is None:
                    continue
                status, _ = apply_candidate_gates(
                    edge_pp=s.edge_pp, pick_odds=s.pick_odds,
                    pick_prob=s.pick_prob, config=c,
                )
                if status == STATUS_RECOMMENDED:
                    per_candidate_holdout[c.label][id(s)] = s

        # And the true walk-forward: selected candidate's held-out sims.
        for s in holdout_sims:
            if s.lane != 'core' or s.won is None:
                continue
            status, _ = apply_candidate_gates(
                edge_pp=s.edge_pp, pick_odds=s.pick_odds,
                pick_prob=s.pick_prob, config=winner_config,
            )
            if status == STATUS_RECOMMENDED:
                aggregate_selected[id(s)] = s

        fold_results.append({
            'fold': fold,
            'train_n_sims': len(train_sims),
            'holdout_n_sims': len(holdout_sims),
            'selected_candidate': winner_label,
            'holdout_selected_metrics': holdout_selected_metrics,
            'holdout_all_metrics': holdout_all_metrics,
            'train_baseline_metrics': train_metrics.get(default_label, {}),
        })

    # Aggregate metrics from selected-per-fold union.
    def _agg_from_sims(sim_dict: dict) -> dict:
        # Re-use compute_candidate_metrics with a permissive "identity"
        # config that admits every sim in the dict (we've already gated
        # them in the loop above).
        class _NullConfig:
            label = 'identity'
            min_probability = 0.0
            min_edge_pp = -1000.0
            max_abs_odds = 1e9
            heavy_favorite_odds = -10_000
            strong_edge_pp = -1000.0
            short_fav_min_probability = None
            short_fav_min_edge_pp = None
            heavy_fav_min_probability = None
            heavy_fav_min_edge_pp = None
        # But those sims already have lane/won requirements met (filtered
        # in the loop). compute_candidate_metrics re-checks lane/won —
        # which is fine, all cached sims pass.
        return compute_candidate_metrics(list(sim_dict.values()), _NullConfig())

    aggregate_selected_metrics = _agg_from_sims(aggregate_selected)
    per_candidate_aggregate_metrics = {
        label: _agg_from_sims(sim_dict)
        for label, sim_dict in per_candidate_holdout.items()
    }

    return {
        'window': (date_from, date_to),
        'blend_weight': blend_weight,
        'use_recent_form': use_recent_form,
        'train_days': train_days,
        'holdout_days': holdout_days,
        'step_days': step_days,
        'min_sample_for_selection': min_sample_for_selection,
        'selection_objective': selection_objective,
        'total_sims': len(sims),
        'sim_errors': sim_errors,
        'n_folds': len(fold_results),
        'folds': fold_results,
        'aggregate_selected': aggregate_selected_metrics,
        'per_candidate_aggregate': per_candidate_aggregate_metrics,
        'baseline_label': default_label,
    }


# ---------------------------------------------------------------------------
# 60–65% bucket root-cause deep dive


def run_60_65_deep_dive(
    *,
    date_from: date,
    date_to: date,
    blend_weight: float = 0.55,
    use_recent_form: bool = True,
) -> dict:
    """Cross-tabulate every baseline-v3-RECOMMENDED sim in [0.60, 0.65)
    against odds bucket, edge bucket, tier, pick_side, movement class,
    and risk flags.

    Produces the input for Phase-2 root-cause analysis: which SUB-SEGMENT
    of the 60–65% bucket actually carries the -28.6% ROI weight, so any
    tightening rule can target the interaction rather than blanket-raise
    the probability floor.
    """
    sims, sim_errors = simulate_baseline(
        date_from=date_from, date_to=date_to,
        blend_weight=blend_weight, use_recent_form=use_recent_form,
    )

    baseline = CandidateConfig(label='v3_baseline')
    bucket = []
    for s in sims:
        if s.lane != 'core':
            continue
        if s.won is None:
            continue
        if s.pick_prob is None or not (0.60 <= s.pick_prob < 0.65):
            continue
        status, _ = apply_candidate_gates(
            edge_pp=s.edge_pp, pick_odds=s.pick_odds,
            pick_prob=s.pick_prob, config=baseline,
        )
        if status != STATUS_RECOMMENDED:
            continue
        bucket.append(s)

    def _bucket_agg(subset):
        n = 0
        wins = losses = 0
        net = 0.0
        for s in subset:
            n += 1
            if s.won is True:
                wins += 1
                net += 100.0 * (_american_to_decimal(s.pick_odds) - 1.0)
            elif s.won is False:
                losses += 1
                net -= 100.0
        decisive = wins + losses
        return {
            'n': n,
            'wins': wins,
            'losses': losses,
            'win_rate': wins / decisive if decisive else None,
            'roi': net / (n * 100.0) if n else None,
            'net_pl': net,
        }

    def _group_by(key_fn):
        groups: Dict[str, list] = defaultdict(list)
        for s in bucket:
            groups[key_fn(s)].append(s)
        return {k: _bucket_agg(v) for k, v in groups.items()}

    by_odds_type = _group_by(lambda s: _odds_type(s.pick_odds))
    by_edge = _group_by(lambda s: _edge_bucket(s.edge_pp))
    by_tier = _group_by(lambda s: s.tier or 'unknown')
    by_pick_side = _group_by(lambda s: s.pick_side or 'unknown')
    by_movement = _group_by(lambda s: s.movement_class or 'none')

    # Cross: odds_type × edge_bucket — the interaction the user asked for.
    cross_odds_edge: Dict[str, Dict[str, dict]] = {}
    for s in bucket:
        o = _odds_type(s.pick_odds)
        e = _edge_bucket(s.edge_pp)
        cross_odds_edge.setdefault(o, {}).setdefault(e, [])
        cross_odds_edge[o][e].append(s)
    cross_odds_edge_agg = {
        o: {e: _bucket_agg(v) for e, v in em.items()}
        for o, em in cross_odds_edge.items()
    }

    # Risk-flag firing counts across the bucket.
    flag_counts: Dict[str, int] = defaultdict(int)
    for s in bucket:
        for flag, fired in (s.risk_flags or {}).items():
            if fired:
                flag_counts[flag] += 1

    return {
        'window': (date_from, date_to),
        'blend_weight': blend_weight,
        'use_recent_form': use_recent_form,
        'total_sims': len(sims),
        'sim_errors': sim_errors,
        'bucket_size': len(bucket),
        'bucket_overall': _bucket_agg(bucket),
        'by_odds_type': by_odds_type,
        'by_edge_bucket': by_edge,
        'by_tier': by_tier,
        'by_pick_side': by_pick_side,
        'by_movement_class': by_movement,
        'cross_odds_edge': cross_odds_edge_agg,
        'risk_flag_counts': dict(flag_counts),
    }


# ---------------------------------------------------------------------------
# Plaintext renderers


def _fmt_pct(v: Optional[float], *, decimals: int = 2, signed: bool = False) -> str:
    if v is None:
        return '  n/a'
    fmt = f"{{:{'+' if signed else ''}.{decimals}f}}%"
    return fmt.format(v * 100.0)


def _fmt_money(v: float, *, signed: bool = True) -> str:
    fmt = "${:+,.2f}" if signed else "${:,.2f}"
    return fmt.format(v)


def _fmt_ci(ci: Tuple[float, float]) -> str:
    lo, hi = ci
    return f"[{lo*100:5.2f}%, {hi*100:5.2f}%]"


def _metric_line(label: str, m: dict, *, width_label: int = 60) -> str:
    n = m['n']
    if n == 0:
        return f"  {label[:width_label]:<{width_label}}  n=0    (no picks)"
    w = m['wins']
    l = m['losses']
    wr = _fmt_pct(m['win_rate'], decimals=2)
    roi = _fmt_pct(m['roi'], decimals=2, signed=True)
    ci = _fmt_ci(m['wilson_ci_95'])
    return (
        f"  {label[:width_label]:<{width_label}}  n={n:>4}  {w:>3}-{l:<3}  "
        f"win {wr}  ROI {roi}  95%CI {ci}"
    )


def render_walk_forward(result: dict) -> str:
    if result.get('error'):
        return (
            f"WALK-FORWARD — no simulations returned for window "
            f"{result['window']} (sim errors: {result.get('sim_errors', 0)}). "
            f"Confirm the window contains final MLB games with pre-game "
            f"OddsSnapshot rows."
        )
    lines: List[str] = []
    push = lines.append

    push('=' * 78)
    push('WALK-FORWARD OPTIMIZATION STUDY (v3 → ≥60% out-of-sample)')
    push('=' * 78)
    push(f"Window          : {result['window'][0]} → {result['window'][1]}")
    push(f"Baseline stack  : blend={result['blend_weight']}, "
         f"use_recent_form={result['use_recent_form']}")
    push(f"Fold config     : train_days={result['train_days']}, "
         f"holdout_days={result['holdout_days']}, "
         f"step_days={result['step_days']}, "
         f"min_sample={result['min_sample_for_selection']}, "
         f"objective={result['selection_objective']}")
    push(f"Total sims      : {result['total_sims']} (errors: {result['sim_errors']})")
    push(f"Folds           : {result['n_folds']}")
    push('')

    # --- Aggregate walk-forward result ---
    push('-' * 78)
    push('AGGREGATE OUT-OF-SAMPLE — SELECTED CANDIDATE PER FOLD, UNION OF HOLDOUTS')
    push('-' * 78)
    agg = result['aggregate_selected']
    push(_metric_line('walk_forward_selected', agg))
    push(f"  CLV positive rate: {_fmt_pct(agg['positive_clv_rate'], decimals=1)}"
         f"  (sample: {agg['clv_sample']}; beat/match/lose "
         f"{agg['clv_beat']}/{agg['clv_matched']}/{agg['clv_lost']})")
    push(f"  Net P/L           : {_fmt_money(agg['net_pl'])}")
    push('')

    # --- Per-candidate aggregate ---
    push('-' * 78)
    push('PER-CANDIDATE — HELD-OUT AGGREGATE (as-if selected every fold)')
    push("Ranked by win rate (accuracy is the primary objective).")
    push('-' * 78)
    ranked = sorted(
        result['per_candidate_aggregate'].items(),
        key=lambda kv: -(kv[1]['win_rate'] or 0.0),
    )
    for label, m in ranked:
        push(_metric_line(label, m))
    push('')

    # --- Fold-by-fold selection log ---
    push('-' * 78)
    push('FOLD-BY-FOLD SELECTION LOG')
    push("Each fold: what training picked, and what it produced on held-out data.")
    push('-' * 78)
    for i, f in enumerate(result['folds'], 1):
        fold = f['fold']
        m = f['holdout_selected_metrics']
        baseline_holdout = f['holdout_all_metrics'].get(result['baseline_label'], {})
        push(f"Fold {i:>2}: train {fold['train_from']}..{fold['train_to']}  "
             f"hold {fold['holdout_from']}..{fold['holdout_to']}  "
             f"(train_sims={f['train_n_sims']}, hold_sims={f['holdout_n_sims']})")
        push(f"        Selected: {f['selected_candidate']}")
        push(_metric_line('        holdout_selected  ', m, width_label=30))
        if baseline_holdout and f['selected_candidate'] != result['baseline_label']:
            push(_metric_line('        holdout_baseline  ', baseline_holdout, width_label=30))
    push('')

    push('-' * 78)
    push('READ THE RESULT')
    push('-' * 78)
    push('  1. If aggregate_selected win rate 95%-CI LOWER BOUND is >= 60% AND')
    push('     ROI > 0 AND positive CLV rate not materially worse than baseline,')
    push('     the selected methodology qualifies for a flag-gated production')
    push('     activation.')
    push('  2. If the aggregate CI lower bound is < 60% but the observed win')
    push('     rate is >= 60%, the sample is insufficient (Class B in the')
    push('     brief) — do NOT ship, extend the observation window.')
    push('  3. If neither, do NOT ship. The next improvement must come from')
    push('     new predictive information (bullpen quality is the queued')
    push('     candidate).')
    return '\n'.join(lines)


def render_60_65_deep_dive(result: dict) -> str:
    lines: List[str] = []
    push = lines.append

    push('=' * 78)
    push('60–65% CONFIDENCE-BUCKET ROOT-CAUSE DEEP DIVE')
    push('=' * 78)
    push(f"Window          : {result['window'][0]} → {result['window'][1]}")
    push(f"Baseline stack  : blend={result['blend_weight']}, "
         f"use_recent_form={result['use_recent_form']}")
    push(f"Total sims      : {result['total_sims']} (errors: {result['sim_errors']})")
    push(f"Bucket size     : {result['bucket_size']} (lane=core, won!=None, "
         f"baseline-recommended, 0.60 <= pick_prob < 0.65)")
    push('')

    o = result['bucket_overall']
    push('-' * 78)
    push('BUCKET OVERALL')
    push('-' * 78)
    push(f"  n = {o['n']}   {o['wins']}-{o['losses']}   "
         f"win {_fmt_pct(o['win_rate'])}   ROI {_fmt_pct(o['roi'], signed=True)}   "
         f"P/L {_fmt_money(o['net_pl'])}")
    push('')

    def _dump(title: str, groups: Dict[str, dict], order: Optional[List[str]] = None):
        push('-' * 78)
        push(title)
        push('-' * 78)
        keys = order or sorted(groups.keys())
        for k in keys:
            if k not in groups:
                continue
            g = groups[k]
            push(f"  {k:<24}  n={g['n']:>4}  {g['wins']:>3}-{g['losses']:<3}  "
                 f"win {_fmt_pct(g['win_rate'])}  "
                 f"ROI {_fmt_pct(g['roi'], signed=True)}  "
                 f"P/L {_fmt_money(g['net_pl'])}")
        push('')

    _dump(
        'BY ODDS BUCKET  (heavy_fav<=-200 / mid_fav -199..-150 / short_fav -149..+99 / dogs)',
        result['by_odds_type'],
        order=['heavy_fav', 'mid_fav', 'short_fav', 'short_dog', 'mid_dog', 'long_dog', 'unknown'],
    )
    _dump(
        'BY EDGE BUCKET  (baseline requires >=6pp)',
        result['by_edge_bucket'],
        order=['<6', '6-7', '7-8', '8-10', '10+', 'unknown'],
    )
    _dump(
        'BY TIER  (elite >=8pp / strong 6-8pp / standard <6pp — should be all strong/elite)',
        result['by_tier'],
    )
    _dump(
        'BY PICK SIDE',
        result['by_pick_side'],
    )
    _dump(
        'BY MARKET-MOVEMENT CLASS  (movement_signal_for_pick)',
        result['by_movement_class'],
    )

    push('-' * 78)
    push('CROSS-TAB: ODDS TYPE × EDGE BUCKET')
    push("Where does the bucket's P/L actually concentrate?")
    push('-' * 78)
    odds_order = ['heavy_fav', 'mid_fav', 'short_fav', 'short_dog', 'mid_dog', 'long_dog']
    edge_order = ['6-7', '7-8', '8-10', '10+']
    for o_key in odds_order:
        if o_key not in result['cross_odds_edge']:
            continue
        push(f"  {o_key}")
        for e_key in edge_order:
            if e_key not in result['cross_odds_edge'][o_key]:
                continue
            g = result['cross_odds_edge'][o_key][e_key]
            push(f"    edge {e_key:<5}  n={g['n']:>3}  "
                 f"{g['wins']:>2}-{g['losses']:<2}  "
                 f"win {_fmt_pct(g['win_rate'])}  "
                 f"ROI {_fmt_pct(g['roi'], signed=True)}  "
                 f"P/L {_fmt_money(g['net_pl'])}")
    push('')

    push('-' * 78)
    push('RISK FLAGS FIRED IN THE 60–65% BUCKET')
    push('-' * 78)
    if not result['risk_flag_counts']:
        push('  (none fired)')
    else:
        for flag, cnt in sorted(
            result['risk_flag_counts'].items(), key=lambda kv: -kv[1],
        ):
            share = cnt / result['bucket_size'] * 100 if result['bucket_size'] else 0
            push(f"  {flag:<24}  {cnt:>4}  ({share:5.1f}% of bucket)")
    push('')

    push('-' * 78)
    push('READ THE RESULT')
    push('-' * 78)
    push('  The bucket-overall row is the reference. Look for a SUB-SEGMENT')
    push('  where n is large and ROI is severely negative. That is the')
    push("  interaction that should drive the candidate rule — NOT a blanket")
    push("  probability-floor raise.")
    return '\n'.join(lines)
