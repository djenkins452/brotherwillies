"""Backtesting & calibration service.

Reconstructs what the moneyline recommendation engine would have emitted for
historical games, then aggregates win rate, ROI, calibration, and CLV by
several breakdowns. Persists each run as a `BacktestRun` row with all
aggregations in the `summary` JSON column.

Design contract — required guarantees
-------------------------------------
1. **Game-time data only.** Every input must have been observable strictly
   before the game start (kickoff / tipoff / first_pitch). The closing odds
   snapshot is filtered with `captured_at__lt=game_time`. The stored house
   probability (ModelResultSnapshot) is filtered the same way — without
   that filter, a post-game analytics-write could leak the outcome back
   into the prediction.

2. **Approximate fallback is flagged.** If a game has no pre-game
   ModelResultSnapshot, we recompute using *current* ratings/injuries.
   That is leaky (today's data, not the game-time data) and any run that
   uses this path is marked `is_approximate=True` so consumers can discount.

3. **One recommendation per game.** We pick the *final* pre-game snapshot
   for prediction (latest captured_at before game start) and emit a single
   GameEvaluation per game. The aggregator dedupes defensively by game id.

4. **Edge in decimal units.** Internally, `edge` is a probability-point
   delta in [-1, 1] (0.06 = 6%). Bucket boundaries are decimal too. We only
   convert to percentage points when calling the live decision-rule helpers
   (`compute_status`, `_raw_tier`) which expect pp on their existing API.

5. **ROI = profit / total_stake.** Flat $100 stake per bet. Net P/L =
   (gross payout on wins) - (total stake). ROI percentage = net_pl / stake
   * 100. Win-rate is reported separately and is never used as a substitute.

6. **CLV uses the same market and side.** For each picked side we compare
   the *opening* moneyline (earliest pre-game snapshot) with the *closing*
   moneyline (latest pre-game snapshot) for that exact side. CLV is in
   decimal-odds units, signed from the bettor's perspective (positive =
   beat the line). Games with only one pre-game snapshot have no
   meaningful CLV (None) and are excluded from CLV stats.

7. **Incremental aggregation.** Evaluations are added to a single
   `_BacktestAggregator` as they are produced — no intermediate list of
   all evaluations is held in memory. Per-bucket counters are O(1) per
   evaluation and the aggregator size is fixed.

8. **Stable JSON shape.** Every breakdown dict pre-populates all known
   labels (every sport, every edge bucket, every tier, etc.) with
   zero-filled metrics. Consumers can always count on the same keys
   appearing — empty buckets render as `sample=0` not as missing entries.

9. **Calibration buckets include count, avg predicted, and actual win rate.**
   Per the metrics dict: `sample`, `avg_predicted_prob` (mean predicted
   probability for picks landing in the bucket), and `win_rate` (actual
   wins / sample). Calibration is the matrix of (predicted vs actual)
   across probability buckets.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Iterator, Optional

from apps.analytics.models import BacktestRun, ModelResultSnapshot
from apps.core.sport_registry import SPORT_REGISTRY
from apps.core.services.recommendations import (
    compute_status,
    _raw_tier,
)
from apps.core.utils.odds import (
    american_to_decimal,
    american_to_implied_prob,
    closing_line_value,
    devig_two_way,
)


# Flat stake per simulated bet — matches MockBet default and the analytics
# service. Changes magnitudes, not relative comparisons.
FLAT_STAKE = 100.0

# Edge buckets in decimal probability units (0.04 = 4 percentage points).
# Boundaries are [low, high) so a 0.04 edge is in '4-6' not '0-4'.
EDGE_BUCKET_LABELS = ['0-4', '4-6', '6-8', '8+']
EDGE_BUCKETS = [
    ('0-4', 0.00, 0.04),
    ('4-6', 0.04, 0.06),
    ('6-8', 0.06, 0.08),
    ('8+',  0.08, float('inf')),
]

# Calibration: 5pp probability buckets in [0.5, 1.0). Picks always have
# predicted >= 0.5, so we don't need bins below. Labels use decimal form
# (e.g. "0.5-0.55") matching the documented contract for the JSON shape.
CALIBRATION_BUCKETS = [
    (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
    (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90),
    (0.90, 0.95), (0.95, 1.0001),  # upper bound > 1.0 so prob=1.0 lands in bucket
]


def _calibration_label(low: float, high: float) -> str:
    """Formatted decimal range, e.g. (0.50, 0.55) -> '0.5-0.55', (0.95, ...) -> '0.95-1.0'."""
    if high > 1.0:
        return f'{low:g}-1.0'
    return f'{low:g}-{high:g}'


CALIBRATION_LABELS = [_calibration_label(low, high) for low, high in CALIBRATION_BUCKETS]

TIER_LABELS = ['elite', 'strong', 'standard']
FAV_DOG_LABELS = ['favorite', 'underdog']
HOME_AWAY_LABELS = ['home', 'away']
SPORT_LABELS = list(SPORT_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Per-game evaluation record

@dataclass
class GameEvaluation:
    """One game's reconstructed recommendation + actual outcome.

    All probabilities are in decimal [0, 1]. `edge` is decimal (0.06 = 6%).
    """
    sport: str
    game_id: str
    game_label: str
    game_time: datetime
    predicted_home_prob: float
    market_home_prob_fair: float
    pick_is_home: bool
    pick_predicted_prob: float
    pick_market_prob_fair: float
    pick_opening_odds_american: Optional[int]
    pick_closing_odds_american: int
    edge: float                      # decimal, signed (predicted - fair)
    status: str                      # 'recommended' | 'not_recommended'
    status_reason: str
    tier: str                        # 'elite' | 'strong' | 'standard'
    won: bool
    clv_decimal: Optional[float]     # opening→closing CLV for the picked side
    is_approximate: bool
    is_favorite: bool
    is_home_pick: bool


# ---------------------------------------------------------------------------
# Bucket accumulator

@dataclass
class _BucketAccumulator:
    """Running totals for one breakdown bucket.

    All metrics derive from these counters. Calling `to_dict` is idempotent
    and never mutates state.
    """
    sample: int = 0
    wins: int = 0
    losses: int = 0
    stake: float = 0.0
    payout: float = 0.0
    edge_sum: float = 0.0            # sum of decimal edges
    predicted_prob_sum: float = 0.0
    clv_sample: int = 0
    clv_sum: float = 0.0
    clv_positive: int = 0

    def add(self, ev: 'GameEvaluation'):
        self.sample += 1
        self.stake += FLAT_STAKE
        if ev.won:
            self.wins += 1
            self.payout += FLAT_STAKE * american_to_decimal(ev.pick_closing_odds_american)
        else:
            self.losses += 1
        self.edge_sum += ev.edge
        self.predicted_prob_sum += ev.pick_predicted_prob
        if ev.clv_decimal is not None:
            self.clv_sample += 1
            self.clv_sum += ev.clv_decimal
            if ev.clv_decimal > 0:
                self.clv_positive += 1

    def to_dict(self):
        if self.sample == 0:
            return {
                'sample': 0,
                'wins': 0,
                'losses': 0,
                'win_rate': None,
                'roi_pct': None,
                'avg_edge': None,
                'avg_predicted_prob': None,
                'clv_sample': 0,
                'avg_clv': None,
                'positive_clv_rate': None,
            }
        net_pl = self.payout - self.stake
        return {
            'sample': self.sample,
            'wins': self.wins,
            'losses': self.losses,
            # Win rate as decimal share (0.55 = 55%). Display layer formats.
            'win_rate': round(self.wins / self.sample, 4),
            # ROI = profit / total stake. Decimal share (0.05 = 5%).
            'roi_pct': round(net_pl / self.stake, 4) if self.stake else None,
            # Edge as decimal (0.06 = 6%).
            'avg_edge': round(self.edge_sum / self.sample, 4),
            # Predicted prob as decimal.
            'avg_predicted_prob': round(self.predicted_prob_sum / self.sample, 4),
            'clv_sample': self.clv_sample,
            'avg_clv': round(self.clv_sum / self.clv_sample, 4) if self.clv_sample else None,
            'positive_clv_rate': (
                round(self.clv_positive / self.clv_sample, 4)
                if self.clv_sample else None
            ),
        }


def _empty_bucket_dict():
    return _BucketAccumulator().to_dict()


@dataclass
class _CalibrationAccumulator:
    """Slim accumulator for calibration buckets.

    Calibration only cares about three signals: how many picks landed in
    this probability range, what the model predicted on average for them,
    and the actual win rate. ROI / CLV / edge breakdowns are not relevant
    here — they live in their own breakdowns.

    Empty buckets zero-fill (count=0, predicted=0.0, actual=0.0) per the
    JSON-shape contract — the keys are always present, never null.
    """
    count: int = 0
    predicted_sum: float = 0.0
    wins: int = 0

    def add(self, predicted: float, won: bool):
        self.count += 1
        self.predicted_sum += predicted
        if won:
            self.wins += 1

    def to_dict(self) -> dict:
        if self.count == 0:
            return {'count': 0, 'predicted': 0.0, 'actual': 0.0}
        return {
            'count': self.count,
            'predicted': round(self.predicted_sum / self.count, 4),
            'actual': round(self.wins / self.count, 4),
        }


# ---------------------------------------------------------------------------
# Aggregator — incremental, fixed-size

class _BacktestAggregator:
    """Holds one accumulator per bucket and incrementally absorbs evaluations.

    The set of buckets is fixed at construction time so the resulting JSON
    has a stable shape across runs (every sport / tier / edge bucket / etc.
    appears, even when empty).
    """

    def __init__(self):
        self.overall = _BucketAccumulator()
        self.overall_recommended = _BucketAccumulator()
        self.by_sport = OrderedDict((s, _BucketAccumulator()) for s in SPORT_LABELS)
        self.by_sport_recommended = OrderedDict((s, _BucketAccumulator()) for s in SPORT_LABELS)
        self.by_edge = OrderedDict((label, _BucketAccumulator()) for label in EDGE_BUCKET_LABELS)
        self.by_tier = OrderedDict((label, _BucketAccumulator()) for label in TIER_LABELS)
        self.by_fav_dog = OrderedDict((label, _BucketAccumulator()) for label in FAV_DOG_LABELS)
        self.by_home_away = OrderedDict((label, _BucketAccumulator()) for label in HOME_AWAY_LABELS)
        self.calibration = OrderedDict((label, _CalibrationAccumulator()) for label in CALIBRATION_LABELS)

        self._seen_keys = set()
        self.total = 0
        self.duplicates = 0
        self.approximate_count = 0

    def add(self, ev: GameEvaluation):
        # Defensive dedup. If a game ends up in the eval stream twice
        # (e.g., overlapping date ranges), it counts once.
        key = (ev.sport, ev.game_id)
        if key in self._seen_keys:
            self.duplicates += 1
            return
        self._seen_keys.add(key)
        self.total += 1
        if ev.is_approximate:
            self.approximate_count += 1

        self.overall.add(ev)
        if ev.sport in self.by_sport:
            self.by_sport[ev.sport].add(ev)
        edge_label = _edge_bucket(ev.edge)
        self.by_edge[edge_label].add(ev)
        if ev.tier in self.by_tier:
            self.by_tier[ev.tier].add(ev)
        self.by_fav_dog['favorite' if ev.is_favorite else 'underdog'].add(ev)
        self.by_home_away['home' if ev.is_home_pick else 'away'].add(ev)
        cal_label = _calibration_bucket(ev.pick_predicted_prob)
        if cal_label and cal_label in self.calibration:
            self.calibration[cal_label].add(ev.pick_predicted_prob, ev.won)

        if ev.status == 'recommended':
            self.overall_recommended.add(ev)
            if ev.sport in self.by_sport_recommended:
                self.by_sport_recommended[ev.sport].add(ev)

    def to_summary(self) -> dict:
        """Build the persisted JSON. Shape is stable across runs."""
        return {
            'overall': self.overall.to_dict(),
            'overall_recommended_only': self.overall_recommended.to_dict(),
            'by_sport': {k: v.to_dict() for k, v in self.by_sport.items()},
            'by_sport_recommended_only': {
                k: v.to_dict() for k, v in self.by_sport_recommended.items()
            },
            'by_edge_bucket': {k: v.to_dict() for k, v in self.by_edge.items()},
            'by_tier': {k: v.to_dict() for k, v in self.by_tier.items()},
            'by_favorite_underdog': {k: v.to_dict() for k, v in self.by_fav_dog.items()},
            'by_home_away': {k: v.to_dict() for k, v in self.by_home_away.items()},
            'calibration_curve': {k: v.to_dict() for k, v in self.calibration.items()},
            'validation': {
                'evaluated': self.total,
                'duplicates_dropped': self.duplicates,
                'approximate_games': self.approximate_count,
            },
        }


def _edge_bucket(edge: float) -> str:
    """Map a decimal edge to one of the named buckets."""
    for label, low, high in EDGE_BUCKETS:
        if low <= edge < high:
            return label
    return EDGE_BUCKET_LABELS[0]  # negative edges → '0-4'


def _calibration_bucket(prob: float) -> Optional[str]:
    """Map a predicted prob to a labeled calibration bucket; None if < 0.50."""
    if prob < 0.50:
        return None
    for low, high in CALIBRATION_BUCKETS:
        if low <= prob < high:
            return _calibration_label(low, high)
    return None


# ---------------------------------------------------------------------------
# Per-game reconstruction helpers

def _settled_games_for_sport(sport: str, start_date=None, end_date=None):
    """Iterate `final` games with both scores populated, in chronological order."""
    entry = SPORT_REGISTRY.get(sport)
    if not entry:
        return []
    GameModel = entry['game_model']
    time_field = entry['time_field']

    qs = GameModel.objects.filter(
        status='final',
        home_score__isnull=False,
        away_score__isnull=False,
    ).select_related('home_team', 'away_team').prefetch_related('odds_snapshots')

    if start_date is not None:
        qs = qs.filter(**{f'{time_field}__date__gte': start_date})
    if end_date is not None:
        qs = qs.filter(**{f'{time_field}__date__lte': end_date})

    return qs.order_by(time_field)


def _pre_game_snapshots(game, time_field: str):
    """All OddsSnapshots for the game, captured strictly before start, oldest first.

    Materialized once per game so the caller can pick opening (first) and
    closing (last) without two queries.
    """
    game_time = getattr(game, time_field)
    return list(
        game.odds_snapshots
        .filter(captured_at__lt=game_time)
        .order_by('captured_at')
    )


def _stored_house_prob(sport: str, game, time_field: str) -> Optional[float]:
    """Return the FINAL pre-game stored house_prob for the game, or None.

    Critical: filters by `captured_at__lt=game_time`. Without this, a
    post-game write to ModelResultSnapshot (from any view that calls
    compute_game_data after the game finalizes) would leak the answer into
    the prediction.
    """
    fk_field = 'game' if sport == 'cfb' else f'{sport}_game'
    game_time = getattr(game, time_field)
    snap = (
        ModelResultSnapshot.objects
        .filter(**{f'{fk_field}__id': game.id})
        .filter(captured_at__lt=game_time)
        .order_by('-captured_at')
        .first()
    )
    return snap.house_prob if snap else None


def _recompute_house_prob(sport: str, game) -> Optional[float]:
    """Fallback: recompute house prob using current ratings + injuries.

    Approximate — uses ratings as-of-now, not as-of-game-time. Caller flags
    the run as approximate when this path is used.
    """
    entry = SPORT_REGISTRY.get(sport)
    if not entry:
        return None
    try:
        data = entry['compute_fn'](game, user=None)
    except Exception:
        return None
    house_prob_pct = data.get('house_prob')
    if house_prob_pct is None:
        return None
    # compute_game_data returns probability as 0-100; normalize to [0, 1].
    return house_prob_pct / 100.0


def _ml_for_pick(snapshot, pick_is_home: bool) -> Optional[int]:
    """Return the moneyline price for the picked side from a snapshot."""
    if snapshot is None:
        return None
    return snapshot.moneyline_home if pick_is_home else snapshot.moneyline_away


def evaluate_game(sport: str, game) -> Optional[GameEvaluation]:
    """Reconstruct a single game's recommendation + actual outcome.

    Returns None when the game is unevaluable: missing scores, no pre-game
    odds snapshot, or the closing snapshot is missing either moneyline.
    """
    entry = SPORT_REGISTRY.get(sport)
    if not entry:
        return None
    if game.home_score is None or game.away_score is None:
        return None

    time_field = entry['time_field']
    snapshots = _pre_game_snapshots(game, time_field)
    if not snapshots:
        return None

    closing = snapshots[-1]
    opening = snapshots[0]  # may equal closing when only one snapshot exists

    if closing.moneyline_home is None or closing.moneyline_away is None:
        return None

    # Predicted home prob — prefer stored final pre-game prediction,
    # fall back to recomputation flagged as approximate.
    home_prob = _stored_house_prob(sport, game, time_field)
    is_approximate = False
    if home_prob is None:
        home_prob = _recompute_house_prob(sport, game)
        if home_prob is None:
            return None
        is_approximate = True

    # Defensive clamp — old snapshots could have stored 0/1 edge cases.
    home_prob = max(0.001, min(0.999, float(home_prob)))
    away_prob = 1.0 - home_prob

    raw_home = american_to_implied_prob(closing.moneyline_home)
    raw_away = american_to_implied_prob(closing.moneyline_away)
    fair_home, fair_away = devig_two_way(raw_home, raw_away)

    home_edge = home_prob - fair_home
    away_edge = away_prob - fair_away

    # Pick the side with the larger edge — same rule as the live engine.
    if home_edge >= away_edge:
        pick_is_home = True
        pick_predicted = home_prob
        pick_fair = fair_home
        pick_closing_odds = closing.moneyline_home
        edge = home_edge
    else:
        pick_is_home = False
        pick_predicted = away_prob
        pick_fair = fair_away
        pick_closing_odds = closing.moneyline_away
        edge = away_edge

    # Decision rules live in pp scale; convert at the call site only.
    edge_pp = round(edge * 100, 2)
    status, reason = compute_status(edge_pp, pick_closing_odds)
    tier = _raw_tier(edge_pp)

    home_won = game.home_score > game.away_score
    won = (pick_is_home and home_won) or (not pick_is_home and not home_won)

    # CLV — same market (moneyline), same side. Compare opening price for
    # the picked side vs closing price. None when no movement is observable
    # (single snapshot) so it's excluded from CLV stats rather than counted
    # as 0 (which would skew positive-CLV rate).
    #
    # SOURCE GUARD: only odds_api-sourced snapshots are eligible. ESPN's
    # embedded odds carry no real movement history (single bookmaker, single
    # capture moment), and cached/manual rows are synthetic or stale. If
    # either the opening OR the closing snapshot wasn't sourced from The
    # Odds API we null CLV out — better to have no CLV than a misleading one.
    #
    # Reads `odds_source` (added by the Provider Health Reliability work in
    # apps/{cfb,cbb,mlb,college_baseball}/models.py::OddsSnapshot). Choices
    # are 'odds_api' / 'espn' / 'manual' / 'cached'.
    pick_opening_odds = _ml_for_pick(opening, pick_is_home)
    sources_ok = (
        getattr(opening, 'odds_source', '') == 'odds_api'
        and getattr(closing, 'odds_source', '') == 'odds_api'
    )
    if (
        sources_ok
        and opening is not closing
        and pick_opening_odds is not None
        and pick_closing_odds is not None
    ):
        clv_decimal = closing_line_value(pick_opening_odds, pick_closing_odds)
    else:
        clv_decimal = None

    label = f"{game.away_team.name} @ {game.home_team.name}"

    return GameEvaluation(
        sport=sport,
        game_id=str(game.id),
        game_label=label,
        game_time=getattr(game, time_field),
        predicted_home_prob=home_prob,
        market_home_prob_fair=fair_home,
        pick_is_home=pick_is_home,
        pick_predicted_prob=pick_predicted,
        pick_market_prob_fair=pick_fair,
        pick_opening_odds_american=pick_opening_odds,
        pick_closing_odds_american=pick_closing_odds,
        edge=round(edge, 6),
        status=status,
        status_reason=reason,
        tier=tier,
        won=won,
        clv_decimal=clv_decimal,
        is_approximate=is_approximate,
        is_favorite=pick_closing_odds < 0,
        is_home_pick=pick_is_home,
    )


# ---------------------------------------------------------------------------
# Public entry points

def iter_evaluations(
    sport: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Iterator[GameEvaluation]:
    """Stream GameEvaluations one at a time. Skipped games are silently dropped."""
    if sport == 'all':
        sports = list(SPORT_REGISTRY.keys())
    elif sport in SPORT_REGISTRY:
        sports = [sport]
    else:
        raise ValueError(f"Unknown sport: {sport}")

    for s in sports:
        for game in _settled_games_for_sport(s, start_date, end_date):
            ev = evaluate_game(s, game)
            if ev is not None:
                yield ev


def _count_skipped(sport: str, start_date, end_date, evaluated_count: int) -> int:
    """Total settled games minus successfully evaluated. Cheap separate query."""
    if sport == 'all':
        sports = list(SPORT_REGISTRY.keys())
    elif sport in SPORT_REGISTRY:
        sports = [sport]
    else:
        return 0
    total = 0
    for s in sports:
        total += len(list(_settled_games_for_sport(s, start_date, end_date)))
    return max(0, total - evaluated_count)


def aggregate_results(evaluations: Iterable[GameEvaluation]) -> dict:
    """Build the JSON `summary` from raw evaluations (used by tests)."""
    agg = _BacktestAggregator()
    for ev in evaluations:
        agg.add(ev)
    return agg.to_summary()


def run_backtest(
    sport: str = 'all',
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    persist: bool = True,
) -> BacktestRun:
    """Reconstruct, aggregate incrementally, and (optionally) persist a backtest."""
    if sport == 'all':
        sports = list(SPORT_REGISTRY.keys())
    elif sport in SPORT_REGISTRY:
        sports = [sport]
    else:
        raise ValueError(f"Unknown sport: {sport}")

    agg = _BacktestAggregator()
    seen = 0  # settled games we considered (evaluable or not)

    for s in sports:
        for game in _settled_games_for_sport(s, start_date, end_date):
            seen += 1
            ev = evaluate_game(s, game)
            if ev is None:
                continue
            agg.add(ev)

    summary = agg.to_summary()
    skipped = max(0, seen - agg.total)

    is_approximate = agg.approximate_count > 0
    notes_lines = []
    if is_approximate:
        notes_lines.append(
            f"APPROXIMATE: {agg.approximate_count} of {agg.total} games were "
            "reconstructed using current team ratings (no stored "
            "ModelResultSnapshot before kickoff). Ratings drift over time "
            "so older games may have inflated/deflated predictions."
        )
    if skipped:
        notes_lines.append(
            f"Skipped {skipped} settled games due to missing pre-game odds "
            "or missing moneyline prices."
        )

    run = BacktestRun(
        sport=sport,
        start_date=start_date,
        end_date=end_date,
        games_evaluated=agg.total,
        games_skipped=skipped,
        is_approximate=is_approximate,
        summary=summary,
        notes='\n'.join(notes_lines),
    )
    if persist:
        run.save()
    return run


def latest_run(sport: Optional[str] = None) -> Optional[BacktestRun]:
    """Convenience for the admin/debug view."""
    qs = BacktestRun.objects.all()
    if sport:
        qs = qs.filter(sport=sport)
    return qs.first()
