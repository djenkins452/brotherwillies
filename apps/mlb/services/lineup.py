"""v3.4 SHADOW — team lineup quality signal.

STATUS

  SHADOW ONLY. USE_LINEUP_QUALITY defaults false.

  Reads ConfirmedLineup rows written by `ingest_lineups`. Requires:
    (1) a lineup observed with `observed_at < reference_date`
        (strict `<` for leakage safety)
    (2) rolling as-of-reference_date player OPS for each of the 9
        players — NOT YET CACHED (requires PlayerHittingHistory
        infrastructure to be built in a follow-up commit).

  Until player-stat caching ships, the quality signal returns 0.0 for
  every team even when a confirmed lineup exists — the signal is
  structurally wired but numerically inert. Storing zeros for now
  populates the feature_contributions schema so any future run of
  the ingestion / replay pipeline works against the same field shape.

LEAKAGE DISCIPLINE

  ConfirmedLineup lookup uses strict `observed_at < reference_date`.
  This prevents a lineup posted AT first pitch (or later) from being
  treated as legitimate pregame knowledge.

  Rows with `lineup_state='post_first_pitch'` are explicitly EXCLUDED
  from consumption — those were captured AFTER game start and cannot
  represent pregame decision-input state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# Same stale threshold pattern as bullpen: a lineup snapshot must be
# recent relative to reference_date. For lineups specifically, this
# only rules out "we ran a poll a month ago but nothing since" — the
# usual pattern is one snapshot per game so this is defense-in-depth.
STALE_THRESHOLD_HOURS = 24


@dataclass(frozen=True)
class LineupSignal:
    """Team-side lineup shadow signal.

    quality_delta is in rating-scale units (same convention as bullpen
    quality). Positive = better lineup. When player stats aren't yet
    cached, quality_delta is 0.0 with data_confidence='low'.

    lineup_state exposes the underlying ConfirmedLineup.lineup_state
    so downstream diagnostics (coverage report, feature contributions)
    can distinguish confirmed / updated / post_first_pitch / no_data.
    """
    quality_delta: float
    data_confidence: str        # 'low' / 'med' / 'high'
    lineup_state: str           # 'no_data' or ConfirmedLineup.STATE_CHOICES
    observed_at: Optional[datetime]
    n_players: int              # 0 when no lineup found


def _latest_lineup_before(team, reference_date):
    """Most recent ConfirmedLineup for `team` with observed_at strictly
    before `reference_date`, EXCLUDING post_first_pitch state. Returns
    None when no legitimate pregame observation exists."""
    if team is None:
        return None
    if reference_date is None:
        from django.utils import timezone
        reference_date = timezone.now()
    from apps.mlb.models import ConfirmedLineup
    try:
        return (
            ConfirmedLineup.objects
            .filter(
                team=team,
                observed_at__lt=reference_date,
            )
            # Exclude observations that were captured AT OR AFTER first
            # pitch — those cannot represent pregame decision state.
            .exclude(lineup_state='post_first_pitch')
            .order_by('-observed_at')
            .first()
        )
    except Exception:
        # Defensive — pre-migration or any transient DB issue must not
        # crash a score computation.
        return None


def team_lineup_signal(team, reference_date) -> LineupSignal:
    """Return the team's lineup signal as-of the reference date.

    Never raises. Returns zero delta + 'low' confidence when no
    legitimate pregame lineup exists OR when player-stat caching
    isn't ready yet."""
    snap = _latest_lineup_before(team, reference_date)
    if snap is None:
        return LineupSignal(
            quality_delta=0.0, data_confidence='low',
            lineup_state='no_data', observed_at=None, n_players=0,
        )

    # Stale-data safety — same shape as bullpen. If the newest legit
    # pregame observation is older than the threshold, degrade.
    from datetime import timedelta as _td
    ref_for_age = reference_date
    if ref_for_age is None:
        from django.utils import timezone as _tz
        ref_for_age = _tz.now()
    if ref_for_age - snap.observed_at > _td(hours=STALE_THRESHOLD_HOURS):
        return LineupSignal(
            quality_delta=0.0, data_confidence='low',
            lineup_state=snap.lineup_state,
            observed_at=snap.observed_at,
            n_players=len(snap.players or []),
        )

    # Player-stat caching is not yet built — return the shell of a
    # signal so feature_contributions has the field shape but the
    # numeric contribution is zero. When PlayerHittingHistory ships,
    # this function computes the real quality_delta from cached OPS.
    return LineupSignal(
        quality_delta=0.0,        # will be non-zero once player stats cached
        data_confidence='med' if len(snap.players or []) == 9 else 'low',
        lineup_state=snap.lineup_state,
        observed_at=snap.observed_at,
        n_players=len(snap.players or []),
    )


def quality_delta(team, *, reference_date) -> float:
    """Convenience — team-side lineup quality delta only."""
    return team_lineup_signal(team, reference_date).quality_delta
