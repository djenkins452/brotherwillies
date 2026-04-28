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

from apps.core.sport_registry import SPORT_REGISTRY


# Decision thresholds — units are percentage points (pp) to match the stored
# `model_edge` scale (e.g. 5.2 means 5.2 pp above market). Mixing units here
# with decimal probabilities would silently misclassify every recommendation.
#
# 2026-04-27 strict correction: lowered MIN_EDGE 4.0 → 3.0 per spec, and
# added new HARD probability + longshot + source gates. The previous
# elite-edge override let +1700 longshots with 21% probability sneak into
# Recommended via "edge >= 8pp short-circuits everything"; that was the
# bug the correction targets. Edge alone is no longer sufficient — the
# pick must also be high-probability, reasonably-priced, and primary-sourced.
MIN_EDGE = 3.0      # 3 pp — minimum edge for any recommended bet
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
HARD_MIN_PROBABILITY = 0.50              # absolute floor; below this → never recommended
MIN_PROBABILITY_FOR_RECOMMENDED = 0.55   # actual recommended threshold (configurable 0.55–0.60)
MAX_ABS_ODDS_FOR_RECOMMENDED = 300       # avoid extreme longshots / extreme favorites
                                          # (configurable; 300 keeps normal markets in range)

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


def _moneyline_candidate(game, data, model_source: str) -> Optional[Recommendation]:
    odds = data.get('latest_odds')
    if not odds or odds.moneyline_home is None or odds.moneyline_away is None:
        return None

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
        pick_side = 'home'
    else:
        pick_name = game.away_team.name
        pick_odds = odds.moneyline_away
        edge = away_edge
        confidence = away_prob
        pick_side = 'away'

    model_edge_pp = round(edge * 100, 1)

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

    # Movement signal — purely additive. The model still drives the pick,
    # the tier, and the recommendation status. Movement only affects the
    # *displayed* confidence (bounded nudge) and an optional warning chip.
    # type(odds) gives us the per-sport OddsSnapshot model class without
    # needing to thread the model through SPORT_REGISTRY.
    from apps.core.services.odds_movement import movement_signal_for_pick
    sig = movement_signal_for_pick(type(odds), game, pick_side)

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
        **{game_fk_field: game},
    )
