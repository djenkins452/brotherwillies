"""Starter recent-form proxy (v3.1, first new v3 feature).

KNOWN LIMITATION — DO NOT REMOVE THIS DOCSTRING:
The MLB schema does NOT currently ingest per-start pitcher stats
(per-start IP, ER, K, BB, FIP/xFIP, velocity). All we have is
season-aggregate stats on StartingPitcher + per-game team-level
outcomes via Game.home_score / away_score.

So "recent form" in v3.1 is computed from the proxy that IS available:
the pitcher's WIN-LOSS over their last N final starts. This is a
NOISY indicator — a pitcher can throw 7 shutout innings and still
lose 1-0 if their team doesn't score. The team's bullpen and offense
both contribute to W/L outcomes.

Despite the noise, W/L is the standard FIRST-LOOK signal in baseball,
and an A/B replay (production vs +form) is the only honest way to
test whether even this noisy proxy carries enough signal to ship. If
replay validation fails, we lose nothing — the path stays
shadow-only behind the USE_STARTER_RECENT_FORM flag.

Upgrade path when per-start data becomes available:
  - Replace _w_l_form_delta with FIP/xFIP/SIERA-based form
  - Keep the same public signature so callers don't break

Returns are on the same scale as StartingPitcher.rating (50-centered).
A delta of +5 means "performing 5 rating points above their season-
average rating, based on recent W/L over their last N starts."
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


# Configurable but stable defaults.
DEFAULT_LOOKBACK_STARTS = 5      # last N completed starts
MIN_DECISIONS_FOR_SIGNAL = 2     # below this, signal too thin → return 0
SCALE_FACTOR = 25.0              # 20pp win-rate deviation → +5 rating pts


def recent_form_delta(
    pitcher,
    *,
    reference_date: Optional[datetime] = None,
    n: int = DEFAULT_LOOKBACK_STARTS,
) -> float:
    """Return a rating-scale delta (positive = better-than-season form).

    Args:
        pitcher: a StartingPitcher instance (or None for graceful no-op).
        reference_date: cutoff — only games STRICTLY before this are
            considered. Defaults to "now" — but for replay must be the
            game's own first_pitch (leak guard).
        n: lookback window in completed starts.

    Returns:
        A float on the rating scale (50-centered). 0.0 means "no signal"
        (None pitcher, too few decisions, or no historical data).
    """
    if pitcher is None:
        return 0.0

    from django.utils import timezone
    from django.db.models import Q
    from apps.mlb.models import Game

    if reference_date is None:
        reference_date = timezone.now()

    qs = (
        Game.objects
        .filter(
            Q(home_pitcher_id=pitcher.id) | Q(away_pitcher_id=pitcher.id),
            status='final',
            first_pitch__lt=reference_date,
            home_score__isnull=False,
            away_score__isnull=False,
        )
        .order_by('-first_pitch')[:n]
    )
    games = list(qs)
    if len(games) < MIN_DECISIONS_FOR_SIGNAL:
        return 0.0

    wins = 0
    decisions = 0
    for g in games:
        # A tie counts as no decision (defensive — MLB regulation games
        # don't tie, but extra-inning suspensions exist).
        if g.home_score is None or g.away_score is None or g.home_score == g.away_score:
            continue
        decisions += 1
        if g.home_pitcher_id == pitcher.id:
            pitcher_team_won = g.home_score > g.away_score
        else:
            pitcher_team_won = g.away_score > g.home_score
        if pitcher_team_won:
            wins += 1

    if decisions < MIN_DECISIONS_FOR_SIGNAL:
        return 0.0

    recent_win_rate = wins / decisions
    delta = (recent_win_rate - 0.500) * SCALE_FACTOR
    return round(delta, 3)
