"""Final calibration step on the moneyline win-probability path.

Two operations applied in order to the home win probability after the
sport's `_compute_win_prob` returns:

  1. **Market blend** — anchor the model probability to the de-vigged
     market by mixing in `MARKET_BLEND_WEIGHT` (15%, capped at 20%)
     of `OddsSnapshot.market_home_win_prob`. Light-touch stabilizer
     that reduces cases where the model strongly disagrees with the
     consensus market — those disagreements historically correlate
     with negative CLV in the backtest data.

  2. **Soft clamp** — bound the picked side's probability to
     `[PROB_MIN, PROB_MAX]` (0.52 → 0.85). Sports betting outcomes
     rarely justify >85% confidence at the moneyline market; capping
     prevents the sigmoid from producing 90%+ predictions that are
     overconfident in the historical data.

Why a separate module: this is a final post-processing step that every
sport's house and user models share verbatim. Centralising it here:
  - Keeps the math in one place — backtest-driven tuning of weights
    or thresholds touches one constant set, not four.
  - Lets the calibration be unit-tested independently of the model
    services that consume it.
  - Makes it explicit at every call site that the model output goes
    through this calibration before becoming a recommendation input.

DOES NOT change recommendation thresholds, edge math, decision rules,
the lane system, or Elo update logic.
"""
from typing import Optional


# Light-touch market anchor. The spec is explicit that this is a
# stabilizer, not a replacement — so the weight is hard-capped at 0.20
# inside `blend_with_market` regardless of caller-supplied values.
MARKET_BLEND_WEIGHT = 0.15
MARKET_BLEND_WEIGHT_CAP = 0.20

# Soft caps on the picked side's probability. Picked-side prob is
# `max(home_prob, 1 - home_prob)`. From the home_prob perspective:
#   home_prob > 0.5  →  clamped to [PROB_MIN, PROB_MAX]
#   home_prob < 0.5  →  clamped to [1-PROB_MAX, 1-PROB_MIN] = [0.15, 0.48]
# A 0.92 home_prob clamps to 0.85; a 0.05 home_prob clamps to 0.15.
PROB_MIN = 0.52
PROB_MAX = 0.85


def blend_with_market(
    model_prob: float,
    market_prob: Optional[float],
    weight: float = MARKET_BLEND_WEIGHT,
) -> float:
    """Anchor model_prob toward market_prob.

    Returns model_prob unchanged when market_prob is None — there's no
    market signal to blend with on a no-odds slate.

    The weight is clamped to [0, MARKET_BLEND_WEIGHT_CAP] regardless of
    the caller-supplied value. The spec is explicit: "Do NOT exceed 20%
    market weight". Clamping in the helper means a future caller can't
    accidentally widen the anchor.
    """
    if market_prob is None:
        return model_prob
    weight = max(0.0, min(MARKET_BLEND_WEIGHT_CAP, weight))
    return model_prob * (1.0 - weight) + market_prob * weight


def clamp_probability(
    p: float,
    lo: float = PROB_MIN,
    hi: float = PROB_MAX,
) -> float:
    """Soft caps on the picked side's win probability.

    From the home_prob perspective:
      home > 0.5  → clamp to [lo, hi]
      home < 0.5  → clamp to [1-hi, 1-lo]
      home == 0.5 → unchanged (true coin flip; sigmoid practically
                    never lands here, and clamping it would create
                    a discontinuous step).

    Returns the clamped value. Always preserves the side of 0.5 the
    input was on — a 0.51 input never crosses to 0.49 just because
    `lo > 0.51`. (It's pushed up to 0.52 instead.)
    """
    if p > 0.5:
        return max(lo, min(hi, p))
    if p < 0.5:
        return max(1.0 - hi, min(1.0 - lo, p))
    return p


def finalize_win_prob(
    model_home_prob: float,
    market_home_prob: Optional[float],
) -> float:
    """Single entry point used by every sport's `compute_*_win_prob`.

    Blend → clamp, in that order. Blending first means the clamp acts on
    the *post-blend* probability, which is what we want — if the model
    says 0.92 and the market says 0.55, the blend lands at ~0.86 and
    the clamp pulls it to 0.85. Doing it in the opposite order would
    clamp the raw 0.92 to 0.85 first, then blend with 0.55 → 0.80,
    leaking the cap.
    """
    blended = blend_with_market(model_home_prob, market_home_prob)
    return clamp_probability(blended)
