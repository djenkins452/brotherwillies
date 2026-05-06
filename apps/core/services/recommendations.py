"""Decision layer — turns model probabilities into a single actionable pick per game.

Reuses each sport's existing `compute_game_data` (via SPORT_REGISTRY) and the latest
OddsSnapshot. No new modeling happens here.

v1 emits moneyline picks only: that is the market where the existing sport model
services actually produce a comparable win probability. Spread picks require a
margin-of-victory model; total picks require a runs/points projection. Both are
future extensions — stubbed below with `None` rather than fabricated.
"""
from dataclasses import dataclass, asdict, field
from decimal import Decimal
from typing import List, Optional

import logging

from apps.core.sport_registry import SPORT_REGISTRY

logger = logging.getLogger(__name__)


# Decision thresholds — units are percentage points (pp) to match the stored
# `model_edge` scale (e.g. 5.2 means 5.2 pp above market). Mixing units here
# with decimal probabilities would silently misclassify every recommendation.
#
# 2026-04-27 strict correction: lowered MIN_EDGE 4.0 → 3.0 + HARD gates.
# 2026-05-03 calibration tighten: bumped MIN_EDGE 3.0 → 5.0 and
# MIN_PROBABILITY_FOR_RECOMMENDED 0.55 → 0.60 after eval showed the engine
# producing too many recommendations with negative CLV. Tier markers
# (STRONG_EDGE / ELITE_EDGE) unchanged — they're UI sort labels, not
# decision gates. Edge alone is still not sufficient — the pick must also
# clear probability + longshot + source gates.
MIN_EDGE = 6.0      # 6 pp — minimum edge for any recommended bet (was 5.0)
STRONG_EDGE = 6.0   # 6 pp — edge required to overcome heavy-favorite juice
ELITE_EDGE = 8.0    # 8 pp — elite-tier marker for UI sorting (no longer
                    # overrides the probability/longshot/source gates)

# Heavy-favorite juice threshold. American odds at or below this are "expensive"
# enough that the model needs a strong edge to clear them. -150 ≈ 60% implied.
HEAVY_FAVORITE_ODDS = -150

# Per-slate guardrails.
# MAX_ELITE_PER_SLATE keeps the "🔥 Top Plays" section scarce + meaningful.
# Total Recommended count is INTENTIONALLY uncapped — every bet that
# clears the per-pick gates (probability ≥ 55%, |odds| ≤ 300, edge ≥ 3pp,
# primary source, no value-tier, no derived) gets surfaced. Per product
# direction 2026-04-28: the per-pick gates are the safety bar; any
# legitimate recommendation should reach the user, even if 10 or 20
# clear in a single slate.
MAX_ELITE_PER_SLATE = 2

# 2026-04-27 strict correction: HARD safety rules — a pick can NEVER be
# Recommended unless ALL of these clear.
# 2026-05-03 calibration tighten: bumped MIN_PROBABILITY_FOR_RECOMMENDED
# from 0.55 to 0.60 in concert with the heavier market blend (30%) to
# concentrate recommendations on higher-confidence picks.
HARD_MIN_PROBABILITY = 0.50              # absolute floor; below this → never recommended
MIN_PROBABILITY_FOR_RECOMMENDED = 0.60   # actual recommended threshold (was 0.55)
MAX_ABS_ODDS_FOR_RECOMMENDED = 300       # avoid extreme longshots / extreme favorites
                                          # (configurable; 300 keeps normal markets in range)

# Extreme model-vs-market disagreement cap. Downgrades elite/strong tiers
# to standard but NEVER blocks the bet — the user still sees it; it just
# falls out of "Top Plays".
#
# Spec evolution:
#   2026-05-03: cap fired at 15pp raw gap (= 10.5pp post-blend at 0.30 W).
#   2026-05-06: relaxed to 20pp raw gap (= 12pp post-blend at 0.40 W) per
#               eval feedback that the 15pp threshold was firing on most
#               recommendations and erasing tier signal.
# Threshold is computed at the post-blend layer because that's where
# `confidence` lives in `_moneyline_candidate`. Math:
#   gap_post_blend = (1 - MARKET_BLEND_WEIGHT) * gap_raw
EXTREME_DISAGREEMENT_GAP = 0.20 * (1.0 - 0.40)  # 0.12 post-blend

# ---------------------------------------------------------------------------
# Two-Lane System (2026-04-28)
#
# Orthogonal classifier on top of the existing status/tier rules. Splits
# every recommendation into:
#   - 'core'      → safe for "Bet All" automation (zero risk flags)
#   - 'qualified' → visible but excluded from automation (1-2 risk flags)
#   - 'pass'      → fails hard gates OR carries 3+ risk flags
#
# DOES NOT modify edge math, decision rules, or the existing status/tier
# axes — those continue to drive the model's "recommended" / "value" /
# "blocked" / "not_recommended" labels. The lane is a *separate* gate that
# protects automation: bulk-bet endpoints filter on lane=='core', so a
# qualified pick is still surfaced to the user (with risk flags shown)
# but cannot be placed in a one-click batch.
#
# Spec source: master prompt 2026-04-28 (two-lane recommendation system).
LANE_CORE = 'core'
LANE_QUALIFIED = 'qualified'
LANE_PASS = 'pass'

# 2026-05-03 calibration tighten: lane hard-gates raised to match the new
# recommendation thresholds. Without this bump, a pick could clear the
# lane (eligible for Bet All) but fail the recommendation gates — leaving
# automation surfacing picks that aren't actually recommended.
LANE_HARD_GATES_PROBABILITY_MIN = 0.60   # decimal — confidence floor (was 0.55)
LANE_HARD_GATES_EDGE_MIN = 0.06          # decimal — edge floor 6pp (was 0.05 / 5pp)
LANE_HARD_GATES_MAX_ABS_ODDS = 300       # |american odds| ceiling
LANE_RISK_FLAGS_MAX_FOR_QUALIFIED = 2    # >=3 flags drops the pick to 'pass'

_TIER_LABELS = {
    'elite': '🔥 High Confidence',
    'strong': 'Strong Edge',
    'standard': 'Model Pick',
    # Source-Aware Betting: 'blocked' tier marks recommendations whose
    # underlying snapshot was derived (synthetic moneyline). Status is
    # also forced to 'not_recommended' with reason='derived_odds'.
    # The UI hides these from primary surfaces; staff diagnostics still
    # see them with this label.
    'blocked': '🔒 Blocked (Derived Odds)',
    # 2026-04-27 correction: 'value' tier marks high-edge low-probability
    # picks (e.g., +1700 longshot with 15pp model edge). They are NEVER
    # recommended and NEVER in Bet All — but they render in their own
    # "Value Plays" section so the user can choose manually.
    'value': '💰 Value Underdog',
}

STATUS_RECOMMENDED = 'recommended'
STATUS_NOT_RECOMMENDED = 'not_recommended'

_STATUS_LABELS = {
    STATUS_RECOMMENDED: 'Recommended',
    STATUS_NOT_RECOMMENDED: 'Not Recommended',
}

# Action-oriented CTA copy — what the user should actually do.
# 2026-04-27: replaced "🔥 HIGH CONFIDENCE RECOMMENDED" with
# "✅ HIGH PROBABILITY PLAY" per spec — the old copy hid the fact that
# Recommended now requires probability >= 55% on top of the edge.
_STATUS_ACTION_LABELS = {
    STATUS_RECOMMENDED: '✅ High Probability Play',
    STATUS_NOT_RECOMMENDED: 'Model Lean',
}

_STATUS_REASON_LABELS = {
    'low_edge': 'Low Edge',
    'high_juice': 'High Juice Risk',
    'marginal': 'Marginal',
    'derived_odds': 'Derived Odds',
    # 2026-04-27 correction reasons:
    'low_probability': 'Low Probability',     # < 55% projected win
    'longshot': 'Longshot',                   # |odds| > 300
    'value': 'Value Underdog (Manual Only)',  # high edge / low prob — not bulk-eligible
    'secondary_source': 'ESPN Source (Not Bulk-Eligible)',
    '': '',
}

# Lower = higher priority in sort keys.
TIER_ORDER = {'elite': 0, 'strong': 1, 'standard': 2, None: 3}


def _raw_tier(model_edge: float) -> str:
    """Classify a recommendation by its edge vs market (in pp), not by confidence.

    Rationale: the product reality is "strength of the opportunity". A 92%
    model confidence against -900 market odds is not a strong edge — the
    market already priced it in. Edge-based tiering avoids that trap.
    """
    if model_edge is None:
        return 'standard'
    if model_edge >= ELITE_EDGE:
        return 'elite'
    if model_edge >= STRONG_EDGE:
        return 'strong'
    return 'standard'


def compute_status(model_edge: float, odds_american: int,
                   *, probability: float = None, is_secondary: bool = False):
    """Apply decision rules to determine recommended vs not_recommended vs value.

    2026-04-27 strict correction: a positive edge alone is no longer
    sufficient. The bug the correction targets: a +1700 longshot with
    21% probability and 15pp edge previously got Recommended via the
    elite-edge override ("edge >= 8pp short-circuits everything"). Now
    the gates fire in this priority order, and ALL must clear for a
    pick to be Recommended:

        1. HARD probability gate (< 50%) — never recommended.
           Edge >= MIN_EDGE → status='not_recommended', reason='value'
           Edge < MIN_EDGE → status='not_recommended', reason='low_edge'
        2. Longshot gate (|odds| > MAX_ABS_ODDS_FOR_RECOMMENDED).
           Even a 51% pick at +500 is too thin a margin to bulk-bet.
        3. Source gate (is_secondary=True). ESPN-source picks render
           but are never Recommended. Bulk-bet excludes them entirely.
        4. Recommended-probability gate (< MIN_PROBABILITY_FOR_RECOMMENDED,
           default 55%).
        5. Edge gate (< MIN_EDGE) — the original bar, still applies.
        6. Heavy-favorite juice gate (odds <= -150, edge < STRONG_EDGE).
        7. Otherwise → Recommended.

    Returns (status, status_reason). `status_reason` is '' when status is
    STATUS_RECOMMENDED. The `probability` arg is decimal 0..1 (use the
    model's home/away probability for the picked side, NOT confidence_score
    which is in 0..100). When `probability` is None the function falls
    back to its pre-correction behavior — keeps backward-compatibility for
    any external caller; new callers should always pass it.
    """
    # Gate 0 — defensive: edge missing means we can't classify at all.
    if model_edge is None:
        return STATUS_NOT_RECOMMENDED, 'low_edge'

    # Gate 1 — HARD probability floor. 49% pick at +1700 may have
    # massive edge mathematically but is not a "high-probability play".
    # Below 50% means more likely to lose than win — not bulk-eligible.
    if probability is not None and probability < HARD_MIN_PROBABILITY:
        if model_edge >= MIN_EDGE:
            # Real edge but low probability: VALUE classification.
            # Visible in its own UI section, never recommended, never bulk.
            return STATUS_NOT_RECOMMENDED, 'value'
        return STATUS_NOT_RECOMMENDED, 'low_edge'

    # Gate 2 — longshot. Even a 51% projection at +400 is too thin
    # in absolute terms to support bulk betting. Hard cap on |odds|.
    if odds_american is not None and abs(odds_american) > MAX_ABS_ODDS_FOR_RECOMMENDED:
        return STATUS_NOT_RECOMMENDED, 'longshot'

    # Gate 3 — source. ESPN-fallback picks render in their own section
    # (the existing "ESPN Recommended Bets" surface) but are NEVER
    # actually Recommended at the engine level. The visible-but-not-
    # bulk-eligible posture matches the spec.
    if is_secondary:
        return STATUS_NOT_RECOMMENDED, 'secondary_source'

    # Gate 4 — Recommended-probability threshold. Defaults to 55%.
    if probability is not None and probability < MIN_PROBABILITY_FOR_RECOMMENDED:
        return STATUS_NOT_RECOMMENDED, 'low_probability'

    # Gate 5 — minimum edge to bother (original rule, slightly lowered).
    if model_edge < MIN_EDGE:
        return STATUS_NOT_RECOMMENDED, 'low_edge'

    # Gate 6 — heavy-favorite juice. Edge must clear STRONG_EDGE when
    # the price is heavy. Keeps a 5pp edge against -200 from looking
    # like a pull-the-trigger pick.
    if odds_american is not None and odds_american <= HEAVY_FAVORITE_ODDS and model_edge < STRONG_EDGE:
        return STATUS_NOT_RECOMMENDED, 'high_juice'

    # All gates clear → Recommended.
    return STATUS_RECOMMENDED, ''


def top_play_reasons(model_edge, confidence_score, tier, status) -> list:
    """Bulleted explanation for why a recommendation qualifies as a top play.

    Used on the elite-tier banner so users see the *justification* — not just
    the conclusion. Pure presentation: derives from already-computed fields,
    never re-runs the decision rules.
    """
    bullets = []
    if model_edge is not None:
        try:
            bullets.append(f"+{float(model_edge):.1f}pp model edge over fair-value market")
        except (TypeError, ValueError):
            pass
    if model_edge is not None and float(model_edge) >= ELITE_EDGE:
        bullets.append("Market mispricing detected (≥8pp edge — elite threshold)")
    if tier == 'elite':
        bullets.append("Top-of-slate pick (capped at 2 elites per cycle)")
    if confidence_score is not None:
        try:
            cs = float(confidence_score)
            if cs >= 70:
                bullets.append(f"High projected win probability ({cs:.0f}%)")
        except (TypeError, ValueError):
            pass
    if status == STATUS_RECOMMENDED:
        bullets.append("Cleared decision rules: edge threshold + juice gate")
    return bullets


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, '')


def action_label(status: str) -> str:
    """Actionable CTA copy — 'Recommended Bet' vs 'Model Lean'."""
    return _STATUS_ACTION_LABELS.get(status, _STATUS_ACTION_LABELS[STATUS_RECOMMENDED])


def status_reason_label(status_reason: str) -> str:
    return _STATUS_REASON_LABELS.get(status_reason or '', '')


@dataclass
class Recommendation:
    sport: str
    game: object
    bet_type: str
    pick: str
    line: str
    odds_american: int
    confidence_score: float
    model_edge: float
    model_source: str
    tier: str = 'standard'
    status: str = STATUS_RECOMMENDED
    status_reason: str = ''
    # Market Movement integration. None / False when no signal is available
    # — UI and analytics treat that case as "no signal" without breaking.
    movement_class: Optional[str] = None
    movement_score: Optional[float] = None
    movement_supports_pick: bool = False
    market_warning: bool = False
    # Source-Aware Betting (trust-tier guardrails):
    #   is_secondary  → True iff the underlying snapshot is ESPN fallback
    #                   (odds_source='espn', is_derived=False). The
    #                   displayed_confidence property applies a 0.85
    #                   multiplier; the UI surfaces a yellow badge.
    #   blocked       → reflected via tier='blocked' and status='not_recommended'
    #                   with status_reason='derived_odds'. The model's edge
    #                   math is preserved untouched.
    is_secondary: bool = False

    # Two-Lane System (2026-04-28). Orthogonal to status/tier — drives
    # the Bet All filter and the Core/Qualified/Pass UI sections. lane
    # defaults to 'pass' so a recommendation that hasn't been classified
    # is never treated as bulk-bet eligible by accident.
    lane: str = LANE_PASS
    risk_flags: dict = field(default_factory=dict)
    risk_score: int = 0

    # 2026-05-03 calibration snapshot. Stored alongside the existing
    # confidence_score (= final_model_prob * 100) and model_edge so
    # downstream analytics can answer "what did we actually decide on?"
    # without re-running the model. raw_model_prob is None for v1: the
    # sport services don't yet return raw separately. Plumbing it requires
    # exposing the pre-calibration value from each compute_*_win_prob —
    # tracked as a follow-up in the changelog.
    raw_model_prob: Optional[float] = None        # decimal 0..1, pre-calibration
    final_model_prob: Optional[float] = None      # decimal 0..1, post-blend post-clamp
    market_prob: Optional[float] = None           # decimal 0..1, de-vigged picked side
    extreme_disagreement: bool = False            # |final - market| > 0.105 (post-blend)

    @property
    def market_implied_pct(self) -> Optional[float]:
        """De-vigged market implied probability for the picked side, in
        percent. Computed as confidence_score (model %) minus model_edge
        (pp). Used by the decision-first hub tiles to show Model | Market
        | Edge alongside each other. Returns None if either input is None.
        """
        if self.confidence_score is None or self.model_edge is None:
            return None
        return float(self.confidence_score) - float(self.model_edge)

    @property
    def tier_label(self) -> str:
        return _TIER_LABELS.get(self.tier, _TIER_LABELS['standard'])

    @property
    def status_label(self) -> str:
        return _STATUS_LABELS.get(self.status, '')

    @property
    def action_label(self) -> str:
        return _STATUS_ACTION_LABELS.get(self.status, _STATUS_ACTION_LABELS[STATUS_RECOMMENDED])

    @property
    def status_reason_label(self) -> str:
        return _STATUS_REASON_LABELS.get(self.status_reason or '', '')

    @property
    def top_play_reasons(self) -> list:
        return top_play_reasons(self.model_edge, self.confidence_score, self.tier, self.status)

    @property
    def is_recommended(self) -> bool:
        return self.status == STATUS_RECOMMENDED

    @property
    def market_implied_probability(self) -> Optional[float]:
        """Market's implied probability (0-100) for the picked side, from odds_american.
        Returns None if odds are missing or zero (defensive — shouldn't normally happen)."""
        if not self.odds_american:
            return None
        return _implied_prob(self.odds_american) * 100.0

    @property
    def explanation_rows(self):
        """Deterministic, scannable rows for the elite card explanation block.
        Skips any metric that can't be computed — never fabricates a number."""
        return _build_explanation_rows(self.confidence_score, self.odds_american, self.model_edge)

    # --- Market movement properties (parallel BettingRecommendation) ----
    @property
    def confidence_nudge_pp(self) -> float:
        from apps.core.services.odds_movement import confidence_nudge_pp
        return confidence_nudge_pp(self.movement_class, self.movement_supports_pick)

    @property
    def displayed_confidence(self):
        """Two adjustments composed in one property, in order:
            1. Movement nudge (additive, capped, +5pp max).
            2. Secondary-source multiplier (×0.85 when is_secondary).
        Then clamp at 99.

        The two are deliberately not factored into separate fields — they
        are both purely presentation. Edge math and base confidence_score
        stay untouched.
        """
        from apps.core.services.odds_movement import displayed_confidence as _dc
        from apps.core.services.odds_trust import secondary_confidence_multiplier
        nudged = _dc(self.confidence_score, self.movement_class, self.movement_supports_pick)
        if nudged is None:
            return None
        if self.is_secondary:
            nudged = nudged * secondary_confidence_multiplier()
        return min(99.0, nudged)

    @property
    def market_movement_chip(self):
        from apps.core.services.odds_movement import chip_label_for
        return chip_label_for(self.movement_class, self.movement_supports_pick, self.market_warning)

    def to_context(self):
        """Template-safe dict. Keeps the game object alive for related-field access."""
        d = asdict(self)
        d['game'] = self.game
        d['tier_label'] = self.tier_label
        d['explanation_rows'] = self.explanation_rows
        return d


def _build_explanation_rows(confidence_score, odds_american, model_edge):
    """Shared builder used by both the dataclass and the persisted DB model."""
    rows = []
    if confidence_score is not None:
        rows.append({'label': 'Win Probability', 'value': f'{float(confidence_score):.0f}%'})
    if odds_american:
        implied = _implied_prob(int(odds_american)) * 100.0
        rows.append({'label': 'Market Implied', 'value': f'{implied:.0f}%'})
    if model_edge is not None:
        sign = '+' if float(model_edge) > 0 else ''
        rows.append({'label': 'Edge', 'value': f'{sign}{float(model_edge):.1f}%'})
    return rows


def assign_tiers(recommendations: List['Recommendation']) -> List['Recommendation']:
    """Classify each recommendation and enforce slate-level caps.

    Mutates tier in place and returns the same list for convenience.

    Rules:
      - Raw tier from model_edge (_raw_tier). Edge in pp matches stored scale.
      - PRESERVE special tiers (blocked, value) that the candidate function
        already set — they're decided by stricter rules upstream and a
        re-classify here would erase the override.
      - If more than MAX_ELITE_PER_SLATE qualify as elite, only the top N by
        (model_edge desc, confidence_score desc) keep elite; the rest drop to
        strong.
      - Total Recommended count is INTENTIONALLY uncapped — per product
        direction (2026-04-28), every bet that clears the per-pick gates
        gets surfaced. The slate cap that briefly existed earlier was
        removed because the per-pick gates (probability ≥ 55%, |odds| ≤ 300,
        edge ≥ 3pp, primary source, no value-tier, no derived) are
        themselves the safety bar — there is no need for an additional
        ceiling on count.
    """
    # Preserve special-tier markers set upstream (blocked / value) — only
    # re-classify the standard / strong / elite tiers from edge math.
    SPECIAL_TIERS = {'blocked', 'value'}
    for rec in recommendations:
        if rec.tier not in SPECIAL_TIERS:
            rec.tier = _raw_tier(rec.model_edge)

    elites = [r for r in recommendations if r.tier == 'elite']
    if len(elites) > MAX_ELITE_PER_SLATE:
        elites.sort(
            key=lambda r: (r.model_edge, r.confidence_score),
            reverse=True,
        )
        for demoted in elites[MAX_ELITE_PER_SLATE:]:
            demoted.tier = 'strong'

    return recommendations


def _implied_prob(american: int) -> float:
    """American odds → implied probability in [0, 1].

    Kept as a thin wrapper so existing callers/tests don't break. The
    canonical implementation now lives in apps.core.utils.odds.
    """
    from apps.core.utils.odds import american_to_implied_prob
    return american_to_implied_prob(american)


def _format_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _lane_hard_gates_pass(
    *, probability: float, edge: float, odds_american: int, source_quality: str,
) -> bool:
    """All four lane hard gates must clear for any non-pass classification.

    Inputs are decimal: probability in [0,1], edge in [-1,1] (0.03 == 3pp),
    odds_american signed, source_quality the OddsSnapshot.source_quality
    value (typically 'primary' / 'fallback' / 'stale' / 'unavailable').
    """
    if probability is None or probability < LANE_HARD_GATES_PROBABILITY_MIN:
        return False
    if edge is None or edge < LANE_HARD_GATES_EDGE_MIN:
        return False
    if odds_american is None or abs(int(odds_american)) > LANE_HARD_GATES_MAX_ABS_ODDS:
        return False
    if source_quality != 'primary':
        return False
    return True


def _lane_compute_risk_flags(
    *, probability: float, odds_american: int, edge_decimal: float,
    movement_class: Optional[str], movement_supports_pick: bool,
    insight_conflicts: bool = False,
) -> dict:
    """Soft risk flags. Each is a boolean; the sum is the risk_score.

    Definitions (per master prompt):
      - market_conflict: market is moving STRONGLY or with SHARP action
            against our pick.
      - sanity_mismatch: model says a moderate favorite but the market
            prices it as a dog (or vice versa). Specifically:
              probability > 0.65 AND odds > +120  (we like a price-dog)
              OR
              probability < 0.55 AND odds < -150  (we shrug at a chalk)
            Both signal that something disagrees between our model and
            the market beyond just "we found edge".
      - thin_edge: post-vig edge against the picked side's RAW implied
            probability (not the de-vigged) is under 4pp. Different from
            `edge_decimal` (which is de-vigged) — this catches picks where
            the de-vig math made the edge look bigger than it really is.
      - insight_conflict: AI insight direction conflicts with our pick.
            Today insights are unstructured text and not reliably parsed
            for direction, so this is supplied by callers when known and
            defaults to False otherwise.
      - short_fav_thin: 2026-05-06. Short-favorite picks (-149 to +99)
            have historically underperformed when the de-vigged edge is
            below STRONG_EDGE (6pp). 30-day eval showed the segment
            losing money and dragging overall ROI. Flag downgrades the
            lane from core → qualified, which routes the bet from the
            Recommended section into Potential. The bet is NOT removed —
            the user still sees it, it just falls out of Bet All.
    """
    market_conflict = (
        movement_class in ('strong', 'sharp')
        and not movement_supports_pick
    )
    sanity_mismatch = False
    if odds_american is not None and probability is not None:
        if probability > 0.65 and odds_american > 120:
            sanity_mismatch = True
        elif probability < 0.55 and odds_american < -150:
            sanity_mismatch = True

    thin_edge = False
    if odds_american is not None and probability is not None:
        from apps.core.utils.odds import american_to_implied_prob
        raw_implied = american_to_implied_prob(int(odds_american))
        # Spec uses raw implied (with vig). When (prob - raw_implied) <
        # 0.04 the bet is informationally thin even if the de-vigged edge
        # looked larger.
        thin_edge = (probability - raw_implied) < 0.04

    # 2026-05-06 short-favorite discipline. Bucket boundary mirrors the
    # Moneyline Evaluation report's odds-type classifier (odds in
    # [-149, +99] is "short_favorite"). edge_decimal is the de-vigged
    # edge in decimal — STRONG_EDGE is in pp, so divide by 100.
    short_fav_thin = False
    if (
        odds_american is not None
        and edge_decimal is not None
        and -149 <= int(odds_american) <= 99
        and edge_decimal < (STRONG_EDGE / 100.0)
    ):
        short_fav_thin = True

    return {
        'market_conflict': bool(market_conflict),
        'sanity_mismatch': bool(sanity_mismatch),
        'thin_edge': bool(thin_edge),
        'insight_conflict': bool(insight_conflicts),
        'short_fav_thin': bool(short_fav_thin),
    }


def _lane_classify(
    *, probability: float, edge_decimal: float, odds_american: int,
    source_quality: str, movement_class: Optional[str],
    movement_supports_pick: bool, insight_conflicts: bool = False,
):
    """Run hard gates → risk flags → lane assignment.

    Returns (lane, risk_flags_dict, risk_score). Hard-gate failure
    short-circuits to ('pass', {}, 0) without computing risk flags.
    """
    if not _lane_hard_gates_pass(
        probability=probability, edge=edge_decimal,
        odds_american=odds_american, source_quality=source_quality,
    ):
        return LANE_PASS, {}, 0

    flags = _lane_compute_risk_flags(
        probability=probability, odds_american=odds_american,
        edge_decimal=edge_decimal,
        movement_class=movement_class,
        movement_supports_pick=movement_supports_pick,
        insight_conflicts=insight_conflicts,
    )
    score = sum(1 for v in flags.values() if v)

    if score == 0:
        return LANE_CORE, flags, score
    if 1 <= score <= LANE_RISK_FLAGS_MAX_FOR_QUALIFIED:
        return LANE_QUALIFIED, flags, score
    return LANE_PASS, flags, score


def partition_games_by_lane(tiles) -> dict:
    """Partition rendered game tiles into the three lane buckets.

    Each tile is expected to expose a `recommendation` attribute (the
    Recommendation dataclass returned by `get_recommendation`). Tiles
    without a recommendation default to LANE_PASS so they appear in the
    "not actionable" bucket rather than vanishing.

    Returns a dict with three lists keyed by 'core' / 'qualified' / 'pass'.
    Order within each list is preserved from the input.
    """
    out = {LANE_CORE: [], LANE_QUALIFIED: [], LANE_PASS: []}
    for tile in tiles:
        rec = getattr(tile, 'recommendation', None)
        lane = getattr(rec, 'lane', LANE_PASS) if rec is not None else LANE_PASS
        if lane not in out:
            lane = LANE_PASS
        out[lane].append(tile)
    return out


def _moneyline_candidate(game, data, model_source: str) -> Optional[Recommendation]:
    odds = data.get('latest_odds')
    if not odds or odds.moneyline_home is None or odds.moneyline_away is None:
        return None

    # The model_service has already run the model output through
    # finalize_win_prob (market blend at MARKET_BLEND_WEIGHT + soft clamp),
    # so home_prob below is the FINAL, post-calibration probability. That
    # is the right input for edge math — we want the post-shrink value vs
    # the de-vigged market.
    if model_source == 'user' and data.get('user_prob') is not None:
        home_prob = data['user_prob'] / 100.0
    else:
        home_prob = data['house_prob'] / 100.0
        model_source = 'house'
    away_prob = 1.0 - home_prob

    # De-vig the market BEFORE computing edge. The raw implied probabilities
    # sum to > 1.0 (the overround); using them directly inflates apparent
    # edge by the vig amount. Fair probs normalize to sum = 1 and give the
    # true EV-relevant baseline for the bet.
    from apps.core.utils.odds import devig_two_way
    raw_home_implied = _implied_prob(odds.moneyline_home)
    raw_away_implied = _implied_prob(odds.moneyline_away)
    fair_home, fair_away = devig_two_way(raw_home_implied, raw_away_implied)

    home_edge = home_prob - fair_home
    away_edge = away_prob - fair_away

    if home_edge >= away_edge:
        pick_name = game.home_team.name
        pick_odds = odds.moneyline_home
        edge = home_edge
        confidence = home_prob
        market_prob = fair_home
        pick_side = 'home'
    else:
        pick_name = game.away_team.name
        pick_odds = odds.moneyline_away
        edge = away_edge
        confidence = away_prob
        market_prob = fair_away
        pick_side = 'away'

    model_edge_pp = round(edge * 100, 1)

    # 2026-05-03 calibration: lightweight log of every candidate's
    # post-blend prob, the picked-side de-vigged market, and the resulting
    # edge. Helps diagnose recommendations after the fact without re-
    # running the model. Logged at INFO so production logs capture it.
    logger.info(
        'Calibration: sport=%s game=%s side=%s final=%.3f market=%.3f edge=%.3f',
        getattr(game, '_meta', None) and game._meta.app_label or '?',
        getattr(game, 'id', '?'),
        pick_side, confidence, market_prob, edge,
    )

    # 2026-04-27 strict correction: source-trust check moved BEFORE
    # compute_status so the source-quality flag can feed into the
    # decision rules (ESPN/secondary picks are never Recommended).
    from apps.core.services.odds_trust import get_odds_trust_tier
    trust = get_odds_trust_tier(odds)
    is_secondary = (trust == 'secondary')
    is_blocked_source = (trust == 'invalid')

    # Pass probability + secondary flag into compute_status so the new
    # gates fire (probability >= 55%, |odds| <= 300, source primary,
    # value-tier split for high-edge / low-prob picks).
    status, reason = compute_status(
        model_edge_pp, pick_odds,
        probability=confidence,        # decimal 0..1 — picked-side prob
        is_secondary=is_secondary,
    )
    tier = _raw_tier(model_edge_pp)

    # Tier overrides for blocked / value / secondary classifications.
    if is_blocked_source:
        # Derived-odds rows: forced to a separate Blocked tier so the
        # UI can hide them entirely from public surfaces.
        status = STATUS_NOT_RECOMMENDED
        reason = 'derived_odds'
        tier = 'blocked'
    elif reason == 'value':
        # 2026-04-27 correction: high-edge low-probability picks get
        # their own tier so the UI can render them in a "Value Plays"
        # section separate from the green Recommended cohort.
        tier = 'value'
    # is_secondary picks keep their natural tier (elite/strong/standard)
    # so the existing ESPN section's sorting still works — they just
    # carry status='not_recommended' with reason='secondary_source'
    # which the bulk-bet filter excludes.

    # 2026-05-03 calibration: extreme model-vs-market disagreement cap.
    # When the post-blend gap exceeds EXTREME_DISAGREEMENT_GAP we DO NOT
    # block the bet, but we downgrade the tier — elite/strong → standard
    # — so the pick falls out of "Top Plays" and into the regular slate.
    # The lane classifier below will independently re-evaluate via its
    # own risk flags. blocked / value tiers are exempt: they already have
    # their own UI treatment and bypassing the cap on them risks losing
    # important context for the staff diagnostic.
    extreme_disagreement = abs(confidence - market_prob) > EXTREME_DISAGREEMENT_GAP
    if extreme_disagreement and tier in ('elite', 'strong'):
        logger.info(
            'Calibration: extreme disagreement cap fired — gap=%.3f tier=%s→standard',
            abs(confidence - market_prob), tier,
        )
        tier = 'standard'

    # Movement signal — purely additive. The model still drives the pick,
    # the tier, and the recommendation status. Movement only affects the
    # *displayed* confidence (bounded nudge) and an optional warning chip.
    # type(odds) gives us the per-sport OddsSnapshot model class without
    # needing to thread the model through SPORT_REGISTRY.
    from apps.core.services.odds_movement import movement_signal_for_pick
    sig = movement_signal_for_pick(type(odds), game, pick_side)

    # Two-Lane classification — orthogonal to status/tier. Runs after the
    # existing decision rules so it reads the *resolved* status, but the
    # lane itself is derived from the spec's hard gates + risk flags. Hard
    # gates use OddsSnapshot.source_quality (added by Provider Health
    # Reliability work) — falls back to '' which fails the gate.
    source_quality = getattr(odds, 'source_quality', '') or ''
    lane, risk_flags, risk_score = _lane_classify(
        probability=confidence,
        edge_decimal=edge,
        odds_american=pick_odds,
        source_quality=source_quality,
        movement_class=sig['movement_class'],
        movement_supports_pick=sig['supports_pick'],
        insight_conflicts=False,  # AI insights aren't programmatically directional yet
    )
    # Defense in depth: a 'blocked' tier (synthesized odds) must never be
    # core, regardless of how the gates resolve. Same for is_secondary —
    # ESPN-fallback picks shouldn't reach Bet All even if their numbers
    # otherwise clear the gates.
    if tier == 'blocked' or is_secondary:
        if lane == LANE_CORE:
            lane = LANE_QUALIFIED

    return Recommendation(
        sport='',
        game=game,
        bet_type='moneyline',
        pick=pick_name,
        line=_format_american(pick_odds),
        odds_american=pick_odds,
        confidence_score=round(confidence * 100, 1),
        model_edge=model_edge_pp,
        model_source=model_source,
        tier=tier,
        status=status,
        status_reason=reason,
        movement_class=sig['movement_class'],
        movement_score=sig['movement_score'],
        movement_supports_pick=sig['supports_pick'],
        market_warning=sig['market_warning'],
        is_secondary=is_secondary,
        lane=lane,
        risk_flags=risk_flags,
        risk_score=risk_score,
        # 2026-05-03 calibration snapshot. raw_model_prob stays None until
        # the sport services expose pre-calibration values; final_model_prob
        # mirrors confidence_score in decimal form for direct comparison
        # against market_prob.
        raw_model_prob=None,
        final_model_prob=confidence,
        market_prob=market_prob,
        extreme_disagreement=extreme_disagreement,
    )


def get_recommendation(sport: str, game, user=None) -> Optional[Recommendation]:
    """Compute the single highest-edge pick for a game. Returns None if no odds exist.

    Preference order for model source:
      - If the user has a configured model AND its prob differs meaningfully from house,
        use the user model. Otherwise fall back to the house model.
    """
    entry = SPORT_REGISTRY.get(sport)
    if not entry:
        return None

    data = entry['compute_fn'](game, user)

    prefer_user = (
        user is not None
        and getattr(user, 'is_authenticated', False)
        and data.get('user_prob') is not None
    )
    source = 'user' if prefer_user else 'house'

    candidates = [
        _moneyline_candidate(game, data, source),
        # spread/total candidates require margin/runs models — not implemented in v1
    ]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None

    best = max(candidates, key=lambda c: c.model_edge)
    best.sport = sport
    return best


def persist_recommendation(sport: str, game, user=None):
    """Compute and save a BettingRecommendation row. Returns the saved model or None."""
    from apps.core.models import BettingRecommendation

    rec = get_recommendation(sport, game, user)
    if rec is None:
        return None

    game_fk_field = f"{sport}_game"
    return BettingRecommendation.objects.create(
        sport=sport,
        bet_type=rec.bet_type,
        pick=rec.pick,
        line=rec.line,
        odds_american=rec.odds_american,
        confidence_score=Decimal(str(rec.confidence_score)),
        model_edge=Decimal(str(rec.model_edge)),
        model_source=rec.model_source,
        status=rec.status,
        status_reason=rec.status_reason,
        movement_class=rec.movement_class or '',
        movement_score=rec.movement_score,
        movement_supports_pick=rec.movement_supports_pick,
        market_warning=rec.market_warning,
        is_secondary=rec.is_secondary,
        # Two-Lane snapshot — frozen at recommendation creation so
        # historical analytics can answer "was this rec bulk-eligible
        # at the time?" without re-running classification rules.
        lane=rec.lane,
        risk_flags=rec.risk_flags,
        risk_score=rec.risk_score,
        # 2026-05-03 calibration snapshot.
        raw_model_prob=rec.raw_model_prob,
        final_model_prob=rec.final_model_prob,
        market_prob=rec.market_prob,
        extreme_disagreement=rec.extreme_disagreement,
        **{game_fk_field: game},
    )
