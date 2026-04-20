"""Template filters for MLB signal/action rendering.

Maps structured reason keys (e.g. 'tight_spread') emitted by the signals
layer into human-readable labels at render time. Keeping UI strings out of
the signals layer lets the same reason key be rendered differently in
different contexts (tile chip vs. help modal vs. analytics) without
touching the service code.
"""
from django import template

register = template.Library()


REASON_LABELS = {
    'tight_spread':     'Tight spread',
    'moderate_spread':  'Close spread',
    'close_game_live':  'Close game',
    'high_injury':      'Key injury',
    'med_injury':       'Injury impact',
    'ace_matchup':      'Ace matchup',
    'line_value':       'Model edge vs. market',
    'late_game':        'Late game',
    'tbd_pitcher':      'Pitcher TBD',
}

ACTION_LABELS = {
    'watch_now': 'Watch Now',
    'best_bet':  'Best Bet',
}


@register.filter
def reason_label(key):
    """Render a structured reason key as a UI label.

    Unknown keys fall back to a prettified title case so a newly-added key
    still reads reasonably before the UI is updated.
    """
    if key in REASON_LABELS:
        return REASON_LABELS[key]
    return (key or '').replace('_', ' ').title()


@register.filter
def action_label(key):
    return ACTION_LABELS.get(key, (key or '').replace('_', ' ').title())
