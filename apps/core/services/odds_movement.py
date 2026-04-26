"""Odds Movement Intelligence — sport-agnostic.

Two questions this module answers:
  1. Is the new snapshot a "significant" change vs the prior one?
     -> is_significant(prev, curr)
  2. Across the last N snapshots, how strong is the movement signal?
     -> compute_movement_score(snapshots, market, side)

Design tenets:
  - Sport-agnostic. Operates on snapshot rows by attribute name (moneyline_home,
    moneyline_away, spread, total). MLB wires it first; other sports plug in
    by passing their snapshot rows through the same functions.
  - De-vigged math. We just shipped a de-vig pipeline; using raw vig-loaded
    probs here would be inconsistent with the rest of the recommendation
    engine.
  - Compute-on-write. The classification is persisted onto the snapshot row
    so dashboards never recompute on read.
  - Per (market, side). Spread, total, and each moneyline side move
    independently. The persistor picks the strongest signal for the row.
  - "Don't downgrade the model." Movement classifications are a SIGNAL, not
    a verdict. Decision-layer integration in Commit 2 will use them
    additively only.

Key math choices (see compute_movement_score for full breakdown):
  - "Cents" axis: American odds mapped to a continuous signed line where
    +100 ≡ -100 ≡ 0. -110 → -10, +120 → +20, -150 → -50.
  - Significance: line moved >= 7 cents OR de-vigged home prob shifted
    >= 2 percentage points (logical OR).
  - Score 0..100, weighted: 40% magnitude / 25% speed / 20% direction
    consistency / 15% timing.
  - Classification cuts: 0–25 noise / 25–55 moderate / 55–80 strong / 80+ sharp.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional, Sequence

from django.utils import timezone

from apps.core.utils.odds import american_to_implied_prob, devig_two_way

# --- Tunables (kept here so anyone reading the file sees the knobs at the top)
SIGNIFICANCE_LINE_CENTS = 7.0      # moneyline / spread juice change
SIGNIFICANCE_PROB_PP = 2.0         # de-vigged home-prob shift
SIGNIFICANCE_SPREAD_POINTS = 0.5   # spread or total points moved

HISTORY_MAX_SNAPSHOTS = 10
HISTORY_MAX_HOURS = 24

# Score component weights — must sum to 1.0
W_MAGNITUDE = 0.40
W_SPEED = 0.25
W_CONSISTENCY = 0.20
W_TIMING = 0.15

# Classification cuts (inclusive lower)
NOISE_MAX = 25.0
MODERATE_MAX = 55.0
STRONG_MAX = 80.0


# --- Cents axis -------------------------------------------------------------

def to_cents_axis(american: Optional[int]) -> Optional[float]:
    """Map American odds to a continuous signed "cents" axis.

    Convention: +100 and -100 collapse to 0 (no juice). Below 0 is the
    favorite side, above 0 is the underdog side.

      -150 → -50    -110 → -10    -100 → 0
      +100 → 0      +110 → +10    +150 → +50

    The continuity matters: naive `new - old` arithmetic flips sign when
    crossing the ±100 boundary (e.g., +110 → -110 looks like -220 but is
    really 20 cents of movement). Using this axis makes the delta a clean
    signed scalar.
    """
    if american is None:
        return None
    if american < 0:
        return -(abs(american) - 100)
    return american - 100


def cents_moved(old: Optional[int], new: Optional[int]) -> float:
    """Absolute cents distance between two American odds values."""
    a = to_cents_axis(old)
    b = to_cents_axis(new)
    if a is None or b is None:
        return 0.0
    return abs(b - a)


def cents_signed_delta(old: Optional[int], new: Optional[int]) -> float:
    """Signed cents delta: positive = price moved more positive (toward dog),
    negative = price moved more negative (toward favorite)."""
    a = to_cents_axis(old)
    b = to_cents_axis(new)
    if a is None or b is None:
        return 0.0
    return b - a


# --- De-vigged probability shift -------------------------------------------

def devigged_home_prob(snapshot) -> Optional[float]:
    """Compute the de-vigged home-win probability from a snapshot's two
    moneylines, falling back to `market_home_win_prob` when only one side
    is available.
    """
    ml_h = getattr(snapshot, 'moneyline_home', None)
    ml_a = getattr(snapshot, 'moneyline_away', None)
    if ml_h is not None and ml_a is not None:
        ip_h = american_to_implied_prob(ml_h)
        ip_a = american_to_implied_prob(ml_a)
        fair_h, _ = devig_two_way(ip_h, ip_a)
        return fair_h
    # Fallback: stored prob (already 0..1, may include vig).
    return getattr(snapshot, 'market_home_win_prob', None)


# --- Significance ----------------------------------------------------------

def is_significant(prev_snapshot, curr_snapshot,
                   *,
                   line_cents: float = SIGNIFICANCE_LINE_CENTS,
                   prob_pp: float = SIGNIFICANCE_PROB_PP,
                   spread_points: float = SIGNIFICANCE_SPREAD_POINTS) -> bool:
    """True if `curr_snapshot` represents a meaningful move vs `prev_snapshot`.

    Logical OR across markets:
      - Either moneyline shifted >= line_cents
      - OR de-vigged home prob shifted >= prob_pp percentage points
      - OR the spread moved >= spread_points
      - OR the total moved >= spread_points

    Any single trigger flips the row to "significant"; the score then
    quantifies how strong the move actually is.
    """
    if prev_snapshot is None or curr_snapshot is None:
        return False

    if cents_moved(getattr(prev_snapshot, 'moneyline_home', None),
                   getattr(curr_snapshot, 'moneyline_home', None)) >= line_cents:
        return True
    if cents_moved(getattr(prev_snapshot, 'moneyline_away', None),
                   getattr(curr_snapshot, 'moneyline_away', None)) >= line_cents:
        return True

    p_prev = devigged_home_prob(prev_snapshot)
    p_curr = devigged_home_prob(curr_snapshot)
    if p_prev is not None and p_curr is not None:
        if abs(p_curr - p_prev) * 100.0 >= prob_pp:
            return True

    s_prev = getattr(prev_snapshot, 'spread', None)
    s_curr = getattr(curr_snapshot, 'spread', None)
    if s_prev is not None and s_curr is not None and abs(s_curr - s_prev) >= spread_points:
        return True

    t_prev = getattr(prev_snapshot, 'total', None)
    t_curr = getattr(curr_snapshot, 'total', None)
    if t_prev is not None and t_curr is not None and abs(t_curr - t_prev) >= spread_points:
        return True

    return False


# --- Score computation ------------------------------------------------------

@dataclass
class MovementResult:
    score: float                # 0..100
    classification: str         # noise / moderate / strong / sharp
    direction: int              # +1 toward home, -1 toward away, 0 neutral
    magnitude: float
    speed: float
    consistency: float
    timing: float
    market: str                 # which market dominated
    side: str                   # which side dominated
    components: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            'score': self.score,
            'classification': self.classification,
            'direction': self.direction,
            'magnitude': self.magnitude,
            'speed': self.speed,
            'consistency': self.consistency,
            'timing': self.timing,
            'market': self.market,
            'side': self.side,
        }


def classify_score(score: float) -> str:
    """Bucket a score in [0,100] into noise / moderate / strong / sharp.

    Cuts are inclusive on the upper bound so 25.0 = noise, 25.01 = moderate.
    Avoids fence-sitting on round numbers.
    """
    if score is None or score <= NOISE_MAX:
        return 'noise'
    if score <= MODERATE_MAX:
        return 'moderate'
    if score <= STRONG_MAX:
        return 'strong'
    return 'sharp'


def _normalize_magnitude_cents(cents: float) -> float:
    """0 cents → 0, 30+ cents → 100. Linear cap."""
    return min(100.0, (cents / 30.0) * 100.0)


def _normalize_magnitude_pp(pp: float) -> float:
    """0pp → 0, 8+pp → 100. Linear cap."""
    return min(100.0, (pp / 8.0) * 100.0)


def _normalize_magnitude_points(points: float) -> float:
    """0 → 0, 1.5+ pts → 100. Linear cap.

    1.5 points is "key-number worth" of movement on a baseball run line or a
    half-run total — empirically substantial for these markets.
    """
    return min(100.0, (points / 1.5) * 100.0)


def _normalize_speed_cents_per_hour(cph: float) -> float:
    """0 cph → 0, 20+ cph → 100. Sharp money landing fast caps high."""
    return min(100.0, (cph / 20.0) * 100.0)


def _timing_weight(minutes_since_last_move: Optional[float]) -> float:
    """Recency boost for the most recent move.

    Sharp money typically lands close to game time. We weight late movement
    as a stronger signal than the same magnitude landing 8 hours out.

      ≤ 30 min   → 100
      ≤ 90 min   → 60
      ≤ 240 min  → 30
      > 240 min  → 0
    """
    if minutes_since_last_move is None:
        return 0.0
    if minutes_since_last_move <= 30:
        return 100.0
    if minutes_since_last_move <= 90:
        return 60.0
    if minutes_since_last_move <= 240:
        return 30.0
    return 0.0


def _per_market_signal(snapshots: Sequence, attr: str) -> Optional[dict]:
    """Compute per-market movement metrics over the given snapshot sequence.

    `attr` is a snapshot attribute name: 'moneyline_home', 'moneyline_away',
    'spread', or 'total'. snapshots are oldest→newest. Returns None if the
    market doesn't have at least 2 non-null observations.
    """
    values = [(s.captured_at, getattr(s, attr, None)) for s in snapshots]
    # Filter to rows that actually have this market
    pairs = [(t, v) for t, v in values if v is not None]
    if len(pairs) < 2:
        return None

    first_t, first_v = pairs[0]
    last_t, last_v = pairs[-1]

    # Magnitude — pick the appropriate normalizer
    if attr in ('moneyline_home', 'moneyline_away'):
        magnitude_raw = cents_moved(first_v, last_v)
        magnitude = _normalize_magnitude_cents(magnitude_raw)
        signed = cents_signed_delta(first_v, last_v)
    else:  # spread or total
        magnitude_raw = abs(last_v - first_v)
        magnitude = _normalize_magnitude_points(magnitude_raw)
        signed = last_v - first_v

    # Speed — magnitude / hours elapsed, capped
    elapsed_hours = max(0.05, (last_t - first_t).total_seconds() / 3600.0)
    if attr in ('moneyline_home', 'moneyline_away'):
        speed = _normalize_speed_cents_per_hour(magnitude_raw / elapsed_hours)
    else:
        # Treat 0.5 pts/hr as the speed cap for spread/total
        cph_eq = (magnitude_raw / elapsed_hours) * 40.0  # 0.5 pts/hr → ~20 equivalent cents/hr
        speed = _normalize_speed_cents_per_hour(cph_eq)

    # Direction consistency — fraction of step-to-step deltas in dominant direction
    deltas = []
    for (_, v_prev), (_, v_next) in zip(pairs[:-1], pairs[1:]):
        if attr in ('moneyline_home', 'moneyline_away'):
            d = cents_signed_delta(v_prev, v_next)
        else:
            d = v_next - v_prev
        if d != 0:
            deltas.append(1 if d > 0 else -1)
    if deltas:
        ups = deltas.count(1)
        downs = deltas.count(-1)
        consistency = 100.0 * max(ups, downs) / len(deltas)
    else:
        consistency = 0.0

    # Timing — minutes since the last non-zero step
    last_move_t = last_t
    for (t_prev, v_prev), (t_next, v_next) in reversed(list(zip(pairs[:-1], pairs[1:]))):
        if v_next != v_prev:
            last_move_t = t_next
            break
    minutes_since = (timezone.now() - last_move_t).total_seconds() / 60.0
    timing = _timing_weight(minutes_since)

    return {
        'attr': attr,
        'magnitude': magnitude,
        'magnitude_raw': magnitude_raw,
        'speed': speed,
        'consistency': consistency,
        'timing': timing,
        'signed_delta': signed,
        'minutes_since_last_move': minutes_since,
    }


def _direction_for_attr(attr: str, signed_delta: float) -> int:
    """Map a per-market signed delta into a "direction toward home" score.

    Positive direction = market moved toward backing the HOME side (or OVER on
    totals). Negative = away/under. Zero = no movement.

      moneyline_home:  signed_delta < 0 → home favored more  → +1
                       signed_delta > 0 → home favored less  → -1
      moneyline_away:  signed_delta < 0 → away favored more  → -1
                       signed_delta > 0 → away favored less  → +1
      spread (home perspective): more negative spread = home laying more  → -1
                                 less negative / positive  = home getting points → +1
                                 NOTE: spread stored from home-team perspective
                                 per project convention. A move from -3 to -4
                                 means home is favored MORE — that's "toward home"
                                 from the bettors-backing-home angle. So signed_delta < 0 → +1.
      total:           up = over-bias → +1, down = under-bias → -1
                       (no "home" semantic — we use this for the magnitude only)
    """
    if signed_delta == 0:
        return 0
    if attr == 'moneyline_home':
        # moneyline_home went more negative → home odds got shorter → home
        # is more favored → market moved toward home (+1).
        return 1 if signed_delta < 0 else -1
    if attr == 'moneyline_away':
        # moneyline_away went more negative → away is more favored → market
        # moved AWAY from home (-1).
        return -1 if signed_delta < 0 else 1
    if attr == 'spread':
        # Spread stored from home perspective. Going from -3 to -4 (more
        # negative) means home is laying more = more favored → +1 toward home.
        return 1 if signed_delta < 0 else -1
    if attr == 'total':
        # Total has no home/away semantic; map up=over (+1), down=under (-1).
        return 1 if signed_delta > 0 else -1
    return 0


def compute_movement_score(snapshots: Sequence) -> Optional[MovementResult]:
    """Score the movement across the given snapshot sequence (oldest→newest).

    Returns the result for the DOMINANT (highest-scoring) market across
    moneyline_home, moneyline_away, spread, total. The choice of "dominant"
    rather than "average" is deliberate: a 30-cent moneyline move tells us
    much more than a 0.5-pt total nudge, and averaging across markets dilutes
    the actual signal.

    Returns None if there's no usable signal (fewer than 2 snapshots, or no
    market has at least 2 observations).
    """
    if not snapshots or len(snapshots) < 2:
        return None

    candidates = []
    for attr in ('moneyline_home', 'moneyline_away', 'spread', 'total'):
        sig = _per_market_signal(snapshots, attr)
        if sig is None:
            continue
        score = (
            W_MAGNITUDE * sig['magnitude']
            + W_SPEED * sig['speed']
            + W_CONSISTENCY * sig['consistency']
            + W_TIMING * sig['timing']
        )
        sig['score'] = score
        candidates.append(sig)

    if not candidates:
        return None

    # Dominant = highest-scoring market signal
    best = max(candidates, key=lambda s: s['score'])
    direction = _direction_for_attr(best['attr'], best['signed_delta'])

    market = 'spread' if best['attr'] == 'spread' else (
        'total' if best['attr'] == 'total' else 'moneyline'
    )
    side = 'home' if best['attr'] == 'moneyline_home' else (
        'away' if best['attr'] == 'moneyline_away' else best['attr']
    )

    return MovementResult(
        score=round(best['score'], 2),
        classification=classify_score(best['score']),
        direction=direction,
        magnitude=round(best['magnitude'], 2),
        speed=round(best['speed'], 2),
        consistency=round(best['consistency'], 2),
        timing=round(best['timing'], 2),
        market=market,
        side=side,
        components={c['attr']: round(c['score'], 2) for c in candidates},
    )


# --- Convenience: pull the most recent N snapshots for a (game, sportsbook)

def recent_snapshots_for_book(model_class, game, sportsbook,
                              *, limit=HISTORY_MAX_SNAPSHOTS,
                              max_hours=HISTORY_MAX_HOURS):
    """Return the last `limit` snapshots for (game, sportsbook), oldest→newest,
    bounded by `max_hours` so opening-line movement on a far-future game
    doesn't dominate the score on the day-of.

    Sport-agnostic: caller passes the OddsSnapshot model class for whichever
    sport (mlb.OddsSnapshot, cfb.OddsSnapshot, etc.).
    """
    cutoff = timezone.now() - timedelta(hours=max_hours)
    qs = (
        model_class.objects
        .filter(game=game, sportsbook=sportsbook, captured_at__gte=cutoff)
        .order_by('-captured_at')[:limit]
    )
    # Reverse to oldest→newest for sequence semantics
    return list(reversed(list(qs)))


# --- Public entry point used by providers -----------------------------------

def apply_movement_intelligence(model_class, snapshot) -> Optional[MovementResult]:
    """Provider hook: called immediately after a new snapshot is created.

    Looks up the previous snapshot for the same (game, sportsbook). If the
    new row is significant relative to it, computes a movement score over
    the last N snapshots and persists snapshot_type/movement_score/
    movement_class on the new row.

    Returns the MovementResult (or None) so callers can log it.

    Exception-safe: any failure logs a warning and returns None — telemetry
    must NEVER break the persist path.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        prev = (
            model_class.objects
            .filter(game=snapshot.game, sportsbook=snapshot.sportsbook,
                    captured_at__lt=snapshot.captured_at)
            .order_by('-captured_at')
            .first()
        )
        if prev is None or not is_significant(prev, snapshot):
            return None

        history = recent_snapshots_for_book(
            model_class, snapshot.game, snapshot.sportsbook,
        )
        # Ensure the new snapshot is in the history (it usually is).
        if not history or history[-1].pk != snapshot.pk:
            history = history + [snapshot]
        result = compute_movement_score(history)
        if result is None:
            return None

        snapshot.snapshot_type = 'significant'
        snapshot.movement_score = result.score
        snapshot.movement_class = result.classification
        snapshot.save(update_fields=['snapshot_type', 'movement_score', 'movement_class'])
        return result
    except Exception as exc:  # noqa: BLE001 — telemetry must not break ingestion
        logger.warning('apply_movement_intelligence failed: %s', exc)
        return None


# --- Decision-layer helper --------------------------------------------------

def movement_signal_for_pick(snapshot_model, game, pick_side: str) -> dict:
    """Compute the movement signal *as it pertains to a specific picked side*.

    The general compute_movement_score() returns the dominant market across
    moneyline / spread / total — useful for "did anything move?". For
    recommendation integration we need a sharper question: "did the
    moneyline move toward or against MY pick?"

    Args:
        snapshot_model: the per-sport OddsSnapshot model class.
        game: a sport Game instance.
        pick_side: 'home' or 'away' — the side the recommendation backs.

    Returns:
        Dict with:
            movement_class:        noise/moderate/strong/sharp or None
            movement_score:        0..100 or None
            supports_pick:         True if direction matches the pick AND
                                   class is moderate or stronger
            market_warning:        True if direction is OPPOSITE the pick AND
                                   class is strong or sharp
            direction:             +1 toward home, -1 toward away, 0 neutral

    Returns all-None / False on failure or insufficient data — callers can
    treat the result as "no signal" without special-casing.
    """
    empty = {
        'movement_class': None,
        'movement_score': None,
        'supports_pick': False,
        'market_warning': False,
        'direction': 0,
    }
    if pick_side not in ('home', 'away'):
        return empty
    if game is None:
        return empty

    try:
        # Pull the most recent significant snapshot's history. We deliberately
        # look across *all* sportsbooks for a game and pick the consensus
        # signal — recommendation fires once per game, not per book.
        cutoff = timezone.now() - timedelta(hours=HISTORY_MAX_HOURS)
        snaps = list(
            snapshot_model.objects
            .filter(game=game, captured_at__gte=cutoff)
            .order_by('-captured_at')[:HISTORY_MAX_SNAPSHOTS * 3]  # over-pull for cross-book averaging
        )
        if len(snaps) < 2:
            return empty
        snaps = list(reversed(snaps))  # oldest → newest

        # Compute moneyline-specific signal for the picked side, since
        # spread/total moves shouldn't drive ML-rec confidence.
        attr = 'moneyline_home' if pick_side == 'home' else 'moneyline_away'
        sig = _per_market_signal(snaps, attr)
        if sig is None:
            return empty

        score = (
            W_MAGNITUDE * sig['magnitude']
            + W_SPEED * sig['speed']
            + W_CONSISTENCY * sig['consistency']
            + W_TIMING * sig['timing']
        )
        cls = classify_score(score)
        direction = _direction_for_attr(attr, sig['signed_delta'])

        # "Pick side" → expected direction for support:
        #   pick_side=='home' wants direction == +1 (market moved toward home)
        #   pick_side=='away' wants direction == -1 (market moved toward away)
        expected = 1 if pick_side == 'home' else -1
        supports = (cls in ('moderate', 'strong', 'sharp')) and direction == expected
        warning = (cls in ('strong', 'sharp')) and direction == -expected

        return {
            'movement_class': cls if cls != 'noise' else None,
            'movement_score': round(score, 2),
            'supports_pick': supports,
            'market_warning': warning,
            'direction': direction,
        }
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning('movement_signal_for_pick failed: %s', exc)
        return empty


# --- Confidence nudge math (used by both Recommendation + BettingRecommendation)

def confidence_nudge_pp(movement_class: Optional[str], supports_pick: bool) -> float:
    """How much to nudge the displayed confidence based on movement.

    Strictly additive, capped at +5pp. Never negative — when the market is
    against the pick we surface a warning chip but do NOT downgrade the
    model's stated confidence (the model is still our source of truth).
    """
    if not supports_pick or not movement_class:
        return 0.0
    return {'sharp': 5.0, 'strong': 3.0, 'moderate': 1.0}.get(movement_class, 0.0)


def displayed_confidence(base_confidence, movement_class, supports_pick) -> Optional[float]:
    """Apply the nudge and clamp at 99 (we never want to render '100%')."""
    if base_confidence is None:
        return None
    nudge = confidence_nudge_pp(movement_class, supports_pick)
    return min(99.0, float(base_confidence) + nudge)


# --- UI label helpers (chips) ----------------------------------------------

MOVEMENT_CHIP_LABELS = {
    'sharp': '🔥 Sharp Action',
    'strong': '📈 Strong Movement',
    'moderate': '↗ Market Moving',
}

MARKET_SUPPORT_LABEL = '📈 Market Support'
MARKET_WARNING_LABEL = '📉 Market Against You'


def chip_label_for(movement_class: Optional[str], supports_pick: bool, market_warning: bool) -> Optional[str]:
    """Pick the right chip text for a recommendation row.

    Precedence (highest first): warning chip > support chip > raw movement.
    Each is mutually exclusive in the UI to avoid a wall of badges.
    """
    if market_warning:
        return MARKET_WARNING_LABEL
    if supports_pick and movement_class in ('strong', 'sharp'):
        return MARKET_SUPPORT_LABEL
    if movement_class in MOVEMENT_CHIP_LABELS:
        return MOVEMENT_CHIP_LABELS[movement_class]
    return None
