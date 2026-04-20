"""Recent W/L streak computation for MLB teams.

Single batched query across a set of team IDs. Returns a dict of
team_id -> {'kind': 'W'|'L', 'count': int, 'label': 'W3'} or None when
the team has no recent final games.

Only streaks of 2+ games are surfaced — a single-game streak is noise,
not a story.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..models import Game

# How far back to look for final games. Baseball seasons are long and dense;
# a 45-day window comfortably captures recent form without pulling the
# full season into memory.
LOOKBACK_DAYS = 45
MAX_GAMES = 500  # safety cap — extremely unlikely to be reached
MIN_STREAK = 2   # hide W1/L1; surface 2+


def compute_streaks(team_ids) -> dict:
    """Return {team_id: {'kind', 'count', 'label'} | None} for each team_id.

    One DB query. Iterates finals in reverse chronological order and builds
    a per-team outcome list in memory, then counts consecutive matching
    outcomes from the most recent game.
    """
    team_ids = set(t for t in team_ids if t)
    if not team_ids:
        return {}

    since = timezone.now() - timedelta(days=LOOKBACK_DAYS)
    finals = (
        Game.objects
        .filter(status='final', first_pitch__gte=since,
                home_score__isnull=False, away_score__isnull=False)
        .filter(Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids))
        .order_by('-first_pitch')
        .values('home_team_id', 'away_team_id', 'home_score', 'away_score')
        [:MAX_GAMES]
    )

    outcomes: dict = {tid: [] for tid in team_ids}
    for g in finals:
        home_won = g['home_score'] > g['away_score']
        if g['home_team_id'] in outcomes:
            outcomes[g['home_team_id']].append('W' if home_won else 'L')
        if g['away_team_id'] in outcomes:
            outcomes[g['away_team_id']].append('L' if home_won else 'W')

    result: dict = {}
    for tid, seq in outcomes.items():
        if not seq:
            result[tid] = None
            continue
        head = seq[0]
        count = 1
        for o in seq[1:]:
            if o == head:
                count += 1
            else:
                break
        if count < MIN_STREAK:
            result[tid] = None
        else:
            result[tid] = {'kind': head, 'count': count, 'label': f"{head}{count}"}
    return result


def format_record(team) -> str | None:
    """Team.wins / Team.losses are nullable — only render when both set."""
    if team is None or team.wins is None or team.losses is None:
        return None
    return f"{team.wins}-{team.losses}"


def format_pitcher_record(pitcher) -> str | None:
    if pitcher is None or pitcher.wins is None or pitcher.losses is None:
        return None
    return f"{pitcher.wins}-{pitcher.losses}"
