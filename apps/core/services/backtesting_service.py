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


# ---------------------------------------------------------------------------
# Phase 2 — Backtest Intelligence Layer
#
# Phase 2 is strictly ADDITIVE on top of Phase 1: it derives decision-quality
# tags from existing GameEvaluation fields, slices edge into finer buckets,
# and emits a system-level verdict — but never modifies a Phase 1 metric or
# bucket. The Phase 1 contract (game-time-only data, ROI = profit/stake,
# decimal edge, calibration shape, CLV source guard, incremental aggregation,
# stable JSON keys) is unchanged.

# Decision-quality classes — answer "good decision vs lucky outcome".
#   perfect:  won AND beat the closing line (good pick, good price)
#   lucky:    won BUT got worse-than-close price (right side, wrong timing)
#   unlucky:  lost BUT beat the closing line (right side, dice didn't roll)
#   bad:      lost AND lost the line (wrong on both)
# Bets with no CLV (single snapshot, ESPN-source, manual/cached) are
# excluded from this breakdown — we can't classify "did you beat the
# line" without two trustworthy snapshots.
DECISION_QUALITY_LABELS = ['perfect', 'lucky', 'unlucky', 'bad']


def _decision_quality(won: bool, clv_decimal: Optional[float]) -> Optional[str]:
    """Classify a settled bet by outcome × CLV. None when CLV is undefined."""
    if clv_decimal is None:
        return None
    if won and clv_decimal > 0:
        return 'perfect'
    if won and clv_decimal <= 0:
        return 'lucky'
    if (not won) and clv_decimal > 0:
        return 'unlucky'
    return 'bad'


# Phase 2 edge buckets — finer-grained than Phase 1's 0-4/4-6/6-8/8+ for
# the "Where Is My Edge?" intelligence table. Decimal scale, [low, high).
EDGE_INTEL_BUCKET_LABELS = ['0-2', '2-4', '4-6', '6+']
EDGE_INTEL_BUCKETS = [
    ('0-2', 0.00, 0.02),
    ('2-4', 0.02, 0.04),
    ('4-6', 0.04, 0.06),
    ('6+',  0.06, float('inf')),
]


def _edge_intel_bucket(edge: float) -> str:
    for label, low, high in EDGE_INTEL_BUCKETS:
        if low <= edge < high:
            return label
    return EDGE_INTEL_BUCKET_LABELS[0]  # negative edges → '0-2'


# ---------------------------------------------------------------------------
# Phase 1A — Segment Instrumentation (2026-05-10)
#
# Adds three new breakdowns alongside the existing Phase 1 / Phase 2 ones,
# all additive. The recommendation engine is NOT touched — these are
# measurement-only buckets so eval can answer:
#   - "How does the model perform per favorite-size band?"
#   - "How does it perform when both starters are real (non-default
#     ratings) vs when one or both are at the seed-default 50.0?"
#   - "How does the engine fare on TBD-pitcher games?"
#
# Favorite-size bucket boundaries match the moneyline-evaluation report
# odds-type classifier so segment numbers align across surfaces. The
# short-favorite band (-149..+99) is the one called out in the engineering
# report as the model's danger zone.
FAV_SIZE_BUCKET_LABELS = [
    'heavy_fav',     # ≤ -200
    'mid_fav',       # -199..-150
    'short_fav',     # -149..+99
    'short_dog',     # +100..+150
    'mid_dog',       # +151..+250
    'long_dog',      # +251..+
]


def _fav_size_bucket(american_odds) -> str:
    """Map a closing-odds American int to a favorite-size band.

    None / 0 fall into 'short_fav' as the safest neutral default — but
    in practice evaluate_game never produces a row without odds.
    """
    if american_odds is None:
        return 'short_fav'
    o = int(american_odds)
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


# Pitcher-data completeness — only meaningful for the baseball sports
# (mlb / college_baseball). Other sports report 'n_a' so they're cleanly
# excluded from the breakdown.
PITCHER_COMPLETENESS_LABELS = [
    'both_real',       # both starters known, neither at default rating
    'one_default',     # both known but at least one rating == 50.0
    'both_default',    # both known but both at default rating (suggests no stats)
    'tbd',             # at least one starter unknown / unset
    'n_a',             # non-baseball sport
]


_BASEBALL_SPORTS = {'mlb', 'college_baseball'}
_PITCHER_DEFAULT_RATING = 50.0


def _pitcher_completeness(sport: str, game) -> str:
    """Classify the game's pitcher-data state.

    Reads `home_pitcher` / `away_pitcher` directly. For non-baseball
    sports the attributes don't exist; we short-circuit to 'n_a'.
    """
    if sport not in _BASEBALL_SPORTS:
        return 'n_a'
    hp = getattr(game, 'home_pitcher', None)
    ap = getattr(game, 'away_pitcher', None)
    if hp is None or ap is None:
        return 'tbd'
    h_default = float(getattr(hp, 'rating', _PITCHER_DEFAULT_RATING)) == _PITCHER_DEFAULT_RATING
    a_default = float(getattr(ap, 'rating', _PITCHER_DEFAULT_RATING)) == _PITCHER_DEFAULT_RATING
    if h_default and a_default:
        return 'both_default'
    if h_default or a_default:
        return 'one_default'
    return 'both_real'


# Starter-known is a coarser slice of the same data — useful as a single
# flag for plotting. Maps directly off pitcher_completeness.
STARTER_KNOWN_LABELS = ['known', 'tbd', 'n_a']


def _starter_known_label(pitcher_completeness: str) -> str:
    if pitcher_completeness == 'n_a':
        return 'n_a'
    if pitcher_completeness == 'tbd':
        return 'tbd'
    return 'known'


# System verdict thresholds (decimal — match the rest of the service).
VERDICT_STRONG_CLV_RATE = 0.5
VERDICT_LABELS = ['STRONG', 'NEUTRAL', 'WEAK']


def _system_verdict(roi_pct: Optional[float], positive_clv_rate: Optional[float]) -> str:
    """Distill overall ROI + CLV signal into one of three verdicts.

    STRONG  — winning AND beating the close more than half the time.
              Both signals point the same way, so the edge is real, not
              just variance.
    NEUTRAL — winning but no CLV signal (or CLV below 50%). Could be edge,
              could be variance — needs more data or a CLV improvement.
    WEAK    — losing money. Doesn't matter what CLV says when ROI is red.
    """
    if roi_pct is None:
        return 'WEAK'
    if roi_pct > 0 and positive_clv_rate is not None and positive_clv_rate > VERDICT_STRONG_CLV_RATE:
        return 'STRONG'
    if roi_pct > 0:
        return 'NEUTRAL'
    return 'WEAK'

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
    # Phase 2 — derived after construction. None when CLV is undefined,
    # since we can't classify a bet's quality without a trustworthy line
    # movement signal. Set in evaluate_game().
    decision_quality: Optional[str] = None
    # Phase 1A segment instrumentation (2026-05-10). All three are
    # observable-at-game-time inputs; populated in evaluate_game().
    fav_size_bucket: str = 'short_fav'             # see FAV_SIZE_BUCKET_LABELS
    pitcher_completeness: str = 'n_a'              # baseball-only signal
    starter_known: str = 'n_a'                     # coarse known/tbd/n_a


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

        # Phase 2 — additive intelligence layer. Same _BucketAccumulator
        # type so we get all the metrics for free; just different binning.
        self.by_edge_intel = OrderedDict(
            (label, _BucketAccumulator()) for label in EDGE_INTEL_BUCKET_LABELS
        )
        self.decision_quality = OrderedDict(
            (label, _BucketAccumulator()) for label in DECISION_QUALITY_LABELS
        )

        # Phase 1A segment instrumentation. Same accumulator type so the
        # ROI/CLV/win-rate metrics come out of the box.
        self.by_fav_size = OrderedDict(
            (label, _BucketAccumulator()) for label in FAV_SIZE_BUCKET_LABELS
        )
        self.by_pitcher_completeness = OrderedDict(
            (label, _BucketAccumulator()) for label in PITCHER_COMPLETENESS_LABELS
        )
        self.by_starter_known = OrderedDict(
            (label, _BucketAccumulator()) for label in STARTER_KNOWN_LABELS
        )

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

        # Phase 2 — additive intelligence layer. Edge-intel slices the
        # same bet stream into finer 0-2/2-4/4-6/6+ bands; decision_quality
        # only counts bets with a defined CLV (others have None).
        self.by_edge_intel[_edge_intel_bucket(ev.edge)].add(ev)
        if ev.decision_quality and ev.decision_quality in self.decision_quality:
            self.decision_quality[ev.decision_quality].add(ev)

        # Phase 1A segment instrumentation. Defensive .get-style access
        # via `if ... in` guards against any caller that constructs a
        # GameEvaluation with an unrecognized bucket label (e.g. an
        # older snapshot replayed through a newer aggregator).
        if ev.fav_size_bucket in self.by_fav_size:
            self.by_fav_size[ev.fav_size_bucket].add(ev)
        if ev.pitcher_completeness in self.by_pitcher_completeness:
            self.by_pitcher_completeness[ev.pitcher_completeness].add(ev)
        if ev.starter_known in self.by_starter_known:
            self.by_starter_known[ev.starter_known].add(ev)

    def to_summary(self) -> dict:
        """Build the persisted JSON. Shape is stable across runs.

        Phase 1 keys (overall, by_sport, by_edge_bucket, by_tier, ...) are
        unchanged. Phase 2 adds new top-level keys without modifying any
        existing bucket — additive only, by contract.
        """
        overall_dict = self.overall.to_dict()
        # CLV metrics surfaced at the top level for the system verdict
        # consumer. These derive from the same overall accumulator, just
        # exposed under the names Phase 2 callers expect.
        clv_metrics = {
            'sample': overall_dict['clv_sample'],
            'avg_clv': overall_dict['avg_clv'],
            'positive_clv_rate': overall_dict['positive_clv_rate'],
        }
        verdict = _system_verdict(
            roi_pct=overall_dict['roi_pct'],
            positive_clv_rate=overall_dict['positive_clv_rate'],
        )
        return {
            # ---- Phase 1 (unchanged shape) ----
            'overall': overall_dict,
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
            # ---- Phase 2 (additive intelligence layer) ----
            'where_is_my_edge': {k: v.to_dict() for k, v in self.by_edge_intel.items()},
            'decision_quality': {k: v.to_dict() for k, v in self.decision_quality.items()},
            'clv_metrics': clv_metrics,
            'system_verdict': verdict,
            # ---- Phase 1A segment instrumentation (2026-05-10) ----
            # Strictly additive — pre-existing keys above are unchanged.
            # These breakdowns answer the engineering report's open
            # questions about which model segments leak edge.
            'by_fav_size': {k: v.to_dict() for k, v in self.by_fav_size.items()},
            'by_pitcher_completeness': {
                k: v.to_dict() for k, v in self.by_pitcher_completeness.items()
            },
            'by_starter_known': {
                k: v.to_dict() for k, v in self.by_starter_known.items()
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

    # Phase 1A segment instrumentation — derived purely from observable
    # at-game-time inputs (closing odds + game pitcher fields), so they
    # share the same "no future leakage" property as the rest of this
    # function.
    fav_size = _fav_size_bucket(pick_closing_odds)
    pitcher_completeness = _pitcher_completeness(sport, game)
    starter_known = _starter_known_label(pitcher_completeness)

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
        decision_quality=_decision_quality(won, clv_decimal),
        fav_size_bucket=fav_size,
        pitcher_completeness=pitcher_completeness,
        starter_known=starter_known,
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

    # Auto-tag the rating mode that was active during this run. Reads
    # the elo_service single-source-of-truth (override-aware) so the row
    # correctly reflects whether dynamic Elo or static ratings drove the
    # predictions, regardless of how the call was triggered.
    from apps.core.services.elo_service import is_dynamic_active
    rating_mode = 'elo' if is_dynamic_active() else 'static'

    run = BacktestRun(
        sport=sport,
        start_date=start_date,
        end_date=end_date,
        games_evaluated=agg.total,
        games_skipped=skipped,
        is_approximate=is_approximate,
        summary=summary,
        notes='\n'.join(notes_lines),
        # Lifecycle tagging — CLI runs go straight to 'completed' since
        # they're synchronous. The analytics page's trigger endpoint
        # creates a 'running' row up front and writes results into it
        # via update (not via this function), so its status flow is
        # handled in the views.
        status='completed',
        rating_mode=rating_mode,
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
