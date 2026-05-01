"""Why This Lost — post-bet classification of lost mock bets.

Answers the "was the model wrong, or was it variance?" question by grouping
each loss into one of four reasons based on the snapshot fields the MockBet
captured at bet placement.

Rules overlap by design; priority order is applied to resolve ambiguity:
  1. bad_edge        — edge was weak; we shouldn't have taken this regardless
  2. variance        — strong edge AND strong confidence; we had the right call
  3. market_movement — market implied higher than our confidence (we went
                       against the favorite and lost)
  4. model_error     — we had confidence but not the edge to back it up
  (fallback)          — model_error for any high-confidence loss; unknown
                        for bets placed before the snapshot migration landed

Only lost bets are analyzed; wins/pushes get no reason.
"""
from decimal import Decimal
from typing import Optional

from apps.core.services.recommendations import _implied_prob


# Reason keys persisted on MockBet.loss_reason.
REASON_VARIANCE = 'variance'
REASON_MODEL_ERROR = 'model_error'
REASON_MARKET_MOVEMENT = 'market_movement'
REASON_BAD_EDGE = 'bad_edge'
REASON_UNKNOWN = 'unknown'  # no snapshot data — pre-migration bets

REASON_CHOICES = [
    ('', ''),
    (REASON_VARIANCE, 'Bad Luck'),
    (REASON_MODEL_ERROR, 'Model Miss'),
    (REASON_MARKET_MOVEMENT, 'Market Misread'),
    (REASON_BAD_EDGE, 'Weak Edge'),
    (REASON_UNKNOWN, 'Unknown'),
]

_REASON_LABELS = dict(REASON_CHOICES)

_REASON_DETAILS = {
    REASON_VARIANCE: (
        "Model had a real edge and reasonable confidence. "
        "This was a solid bet that just didn't land."
    ),
    REASON_MODEL_ERROR: (
        "Model was confident but the edge was thin. "
        "Confidence outpaced the actual value signal."
    ),
    REASON_MARKET_MOVEMENT: (
        "Market implied a higher win probability than the model did. "
        "We bet against the market and it was right."
    ),
    REASON_BAD_EDGE: (
        "Edge was below the minimum threshold. "
        "This bet shouldn't have cleared the decision rules."
    ),
    REASON_UNKNOWN: (
        "No decision-layer snapshot available for this bet — "
        "placed before loss analysis was wired."
    ),
}

# Thresholds — units match the stored model_edge scale (percentage points).
_VARIANCE_MIN_CONF = Decimal('60')
_VARIANCE_MIN_EDGE = Decimal('5')
_MODEL_ERROR_MIN_CONF = Decimal('65')
_BAD_EDGE_MAX = Decimal('4')

# 2026-04-30 fallback path — when the snapshot fields aren't populated
# (pre-migration bets, or settlement that ran before the snapshot data
# was wired), we still classify based on what IS persisted on every
# MockBet: implied_probability + confidence_level. The classification
# is approximate, but vastly better than dumping ~half of all losses
# into "Unknown" just because the richer snapshot fields are missing.
#
# Confidence-level → assumed model probability map. Used to compute
# an approximate edge against the bet's implied_probability.
_FALLBACK_CONF_PROB = {
    'low':    Decimal('0.50'),
    'medium': Decimal('0.58'),
    'high':   Decimal('0.65'),
}


def _analyze_loss_fallback(mock_bet) -> Optional[dict]:
    """Best-effort classification when snapshot fields are missing.

    Returns None if nothing usable is on the bet (no implied_probability
    AND no confidence_level) — caller falls through to UNKNOWN.
    """
    implied = mock_bet.implied_probability  # Decimal 0..1, set at placement
    conf_level = (mock_bet.confidence_level or '').lower()
    if implied is None and conf_level not in _FALLBACK_CONF_PROB:
        return None  # truly nothing — caller returns UNKNOWN

    # Default the model-side probability to medium (~58%) if not
    # specified. This biases the classifier toward neutral when we
    # have minimal info.
    assumed_conf = _FALLBACK_CONF_PROB.get(conf_level, _FALLBACK_CONF_PROB['medium'])

    if implied is not None:
        approx_edge_pp = (assumed_conf - implied) * Decimal('100')
    else:
        # Implied missing too (rare); call edge zero so we get bad_edge.
        approx_edge_pp = Decimal('0')

    if approx_edge_pp < _BAD_EDGE_MAX:
        primary = REASON_BAD_EDGE
    elif conf_level == 'high' and approx_edge_pp >= _VARIANCE_MIN_EDGE:
        primary = REASON_VARIANCE
    elif implied is not None and implied > assumed_conf:
        primary = REASON_MARKET_MOVEMENT
    elif conf_level == 'high':
        primary = REASON_MODEL_ERROR
    else:
        primary = REASON_MODEL_ERROR

    confidence_miss: Optional[Decimal] = None
    if implied is not None:
        confidence_miss = (assumed_conf - implied) * Decimal('100')
        confidence_miss = Decimal(str(round(float(confidence_miss), 2)))

    edge_miss = Decimal(str(round(float(approx_edge_pp), 2)))

    return {
        'primary_reason': primary,
        'details': (
            _REASON_DETAILS[primary]
            + ' (Approximate — classified from confidence level + odds; '
            'snapshot edge field was not captured for this bet.)'
        ),
        'confidence_miss': confidence_miss,
        'edge_miss': edge_miss,
    }


def reason_label(reason: str) -> str:
    """Human-readable label for the loss reason badge."""
    return _REASON_LABELS.get(reason or '', '')


def reason_details(reason: str) -> str:
    return _REASON_DETAILS.get(reason or '', '')


def analyze_loss(mock_bet) -> dict:
    """Classify a lost MockBet and return a structured dict.

    Non-fatal: if the snapshot fields are missing (bet placed pre-migration),
    the result's `primary_reason` is 'unknown' rather than raising.

    Callers should only invoke this for bets where result == 'loss'; calling
    it on a win/push/pending bet returns `primary_reason='unknown'` harmlessly.
    """
    if mock_bet.result != 'loss':
        return {
            'primary_reason': REASON_UNKNOWN,
            'details': 'Bet is not a loss — no analysis applied.',
            'confidence_miss': None,
            'edge_miss': None,
        }

    confidence = mock_bet.recommendation_confidence  # Decimal or None
    edge = mock_bet.expected_edge                    # Decimal or None
    odds = mock_bet.odds_american

    # No snapshot data — fall back to classifying with whatever WAS
    # persisted on the MockBet at placement (implied_probability is
    # always set; confidence_level low/medium/high too). The result
    # is approximate but vastly better than the catch-all "unknown"
    # that previously absorbed ~half of all losses just because the
    # snapshot fields were missing.
    if confidence is None or edge is None:
        fallback = _analyze_loss_fallback(mock_bet)
        if fallback is not None:
            return fallback
        # Truly nothing usable (no implied prob either) — last resort.
        return {
            'primary_reason': REASON_UNKNOWN,
            'details': _REASON_DETAILS[REASON_UNKNOWN],
            'confidence_miss': None,
            'edge_miss': None,
        }

    implied_pct = _implied_prob(odds) * 100 if odds else None

    # Priority-ordered resolution. The rules in the spec overlap (e.g. a bet
    # with conf=70, edge=6 satisfies both variance and model_error), so we
    # apply them in the order that best explains the loss.
    if edge < _BAD_EDGE_MAX:
        primary = REASON_BAD_EDGE
    elif edge >= _VARIANCE_MIN_EDGE and confidence >= _VARIANCE_MIN_CONF:
        primary = REASON_VARIANCE
    elif implied_pct is not None and implied_pct > float(confidence):
        primary = REASON_MARKET_MOVEMENT
    elif confidence >= _MODEL_ERROR_MIN_CONF:
        primary = REASON_MODEL_ERROR
    else:
        # Low-confidence loss that wasn't flagged as bad_edge — treat as
        # model_error since we still placed a bet on it.
        primary = REASON_MODEL_ERROR

    # confidence_miss: signed gap between our confidence and market implied.
    # Positive → we were more confident than the market (and were wrong).
    # Negative → market was more confident than us.
    # Null when odds can't produce an implied probability.
    confidence_miss: Optional[Decimal] = None
    if implied_pct is not None:
        confidence_miss = Decimal(str(round(float(confidence) - implied_pct, 2)))

    # edge_miss: the edge we claimed at bet time that didn't pay off.
    edge_miss = Decimal(str(round(float(edge), 2)))

    return {
        'primary_reason': primary,
        'details': _REASON_DETAILS[primary],
        'confidence_miss': confidence_miss,
        'edge_miss': edge_miss,
    }
