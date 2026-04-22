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

    # No snapshot data → unknown. Bets placed before the snapshot fields
    # migration landed fall here. We return a non-None confidence_miss /
    # edge_miss only when we actually have data.
    if confidence is None or edge is None:
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
