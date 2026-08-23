"""v3.3 SHADOW — bullpen quality + fatigue signal service.

STATUS

  SHADOW ONLY. The two feature flags:

    USE_BULLPEN_QUALITY   — controls whether the quality delta enters
                            the model score.
    USE_BULLPEN_FATIGUE   — controls whether the fatigue delta enters
                            the model score.

  BOTH DEFAULT FALSE. When absent or false, this module still runs on
  every scoring call — the delta values are computed and stored on
  `BettingRecommendation.feature_contributions` for research/audit —
  but they DO NOT enter the score. Production behavior is identical
  regardless of the presence of any bullpen data.

LEAKAGE DISCIPLINE (mandatory, locked by test)

  Every consumer MUST pass an explicit `reference_date`. This module
  returns state from the most-recent `TeamBullpenSnapshot` where
  `as_of < reference_date` (strict `<`, never `<=`). The strict
  inequality is what keeps a snapshot captured seconds after first
  pitch from bleeding into the pre-game decision.

  For the LIVE path, `reference_date=timezone.now()` is the natural
  choice. For REPLAY of historical game G, the caller MUST pass
  `reference_date=G.first_pitch`. Every call site (model_service._score,
  method_replay._simulate_recommendation) does exactly this.

DATA REALITY (2026-08-22)

  `TeamBullpenSnapshot` is empty. `ingest_bullpen_snapshots` is
  scaffolded but not wired to a live data source; the design's
  Phase 2A (team aggregate) requires a team-level pitching splits
  endpoint the codebase does not yet call, and Phase 2B (reliever
  fatigue) requires per-game boxscore ingestion the codebase does
  not yet call.

  Until real data lands:
    * team_bullpen_signal() returns BullpenSignal(0.0, 0.0, 'low', None)
    * feature_contributions captures those zeros (still useful for
      forward audit — every recommendation has the field populated
      from day one, ready to fill with real values on ingest).
    * The `?experiment=bullpen` replay endpoint compares baseline to
      baseline (no signal to attribute) and says so plainly in its
      plaintext output.

  This is the honest posture per the v3.3 brief:
    "If the available data cannot support historically correct
     reconstruction, STOP and report the limitation rather than build
     a misleading replay."
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# --- Empirical constants. Rating-scale so they compose with the existing
# pitcher_static term (~50 baseline) inside `_score` without needing new
# weight-normalization work.

# Anchor: MLB league-average bullpen ERA. A team below this is above
# average, above it is below average. The scale factor converts an ERA
# delta into rating-scale units.
LEAGUE_AVG_BULLPEN_ERA = 4.20
QUALITY_SCALE_FACTOR = 8.0  # rating units per 1.00 ERA gap
# Cap the quality delta to avoid a single elite/terrible pen dominating.
QUALITY_ABS_CAP = 12.0      # rating units

# --- Stale-data safety (v3.3 operationalization, 2026-08-22) ---
#
# Even after backfill + daily refresh, the ingest job can silently fall
# behind — API outage, bad deploy, missed cron. If we allow the shadow
# layer to keep using an old snapshot forever, a future activation
# would be scoring off stale bullpen state without any warning. The
# guard here degrades the signal to zero when the newest snapshot
# BEFORE reference_date is older than STALE_THRESHOLD_DAYS.
#
# Semantic: "as of reference_date T, is the latest available snapshot
# more than 3 days old?" Anchoring on reference_date (not now()) means
# replay of a historical game G is judged by data available IMMEDIATELY
# before G — a 5-day-old-at-G snapshot is stale, but "2-months-ago-from-
# today" is fine because the replay is scoring what would have been
# known at G's first_pitch.
STALE_THRESHOLD_DAYS = 3

# Fatigue: applied as a negative delta when the pen is over-used or its
# top arm isn't available. Kept small vs quality — fatigue is a
# tie-breaker, not a primary driver.
FATIGUE_UNAVAILABLE_PENALTY = 3.0        # rating units when top arm out
FATIGUE_OVERWORKED_PENALTY = 1.5         # rating units on >=4 apps in 2d
FATIGUE_OVERWORKED_THRESHOLD_2D = 4      # appearances in last 2 days


@dataclass(frozen=True)
class BullpenSignal:
    """Team-side bullpen shadow signal.

    quality_delta / fatigue_delta are in rating-scale units, signed so
    that POSITIVE values are BETTER for the team (better pen = positive
    quality; well-rested top arm available = fatigue >= 0).

    data_confidence is a coarse operator-readable tag ('low' when there
    is no snapshot at all, or when the underlying ingest flagged the
    row 'low'). snapshot_as_of is the timestamp of the row that fed the
    signal, or None when no snapshot exists.
    """
    quality_delta: float
    fatigue_delta: float
    data_confidence: str
    snapshot_as_of: Optional[datetime]


def _latest_snapshot_before(team, reference_date):
    """Most recent snapshot strictly before reference_date. None when the
    table is empty (no ingest yet) or when no row precedes the cutoff.

    When `reference_date is None`, defaults to `timezone.now()` — the
    live-scoring path. Replay/test callers should pass an explicit
    cutoff (e.g. `game.first_pitch`) so the strict `as_of__lt` guard
    can anchor on the correct historical moment; `None` is the natural
    default only for live scoring."""
    if team is None:
        return None
    if reference_date is None:
        from django.utils import timezone
        reference_date = timezone.now()
    # Local import: this module loads before mlb.models is ready during
    # Django's app-registration phase for some code paths.
    from apps.mlb.models import TeamBullpenSnapshot
    try:
        return (
            TeamBullpenSnapshot.objects
            .filter(team=team, as_of__lt=reference_date)
            .order_by('-as_of')
            .first()
        )
    except Exception:
        # Defensive: pre-migration state (table doesn't exist) OR any
        # transient DB issue must not crash a score computation. The
        # shadow layer degrades to zero cleanly.
        return None


def team_bullpen_signal(team, reference_date) -> BullpenSignal:
    """Return the team's bullpen signal as-of the reference date.

    Never raises. Returns zero deltas + 'low' confidence when no data
    is available. This is by design so `_score` can always call this
    function unconditionally without needing to know whether any data
    has been ingested.

    `reference_date=None` defaults to now (live-scoring path). Replay
    MUST pass `game.first_pitch` so the strict `as_of__lt` guard
    anchors on the correct historical cutoff.
    """
    snap = _latest_snapshot_before(team, reference_date)
    if snap is None:
        return BullpenSignal(0.0, 0.0, 'low', None)

    # v3.3 STALE-DATA SAFETY: if the newest snapshot BEFORE
    # reference_date is more than STALE_THRESHOLD_DAYS old, degrade
    # to zero. Prevents production from silently scoring against
    # arbitrarily old data if the daily refresh stops running.
    from datetime import timedelta as _td
    if reference_date is None:
        from django.utils import timezone as _tz
        reference_date_for_age = _tz.now()
    else:
        reference_date_for_age = reference_date
    if reference_date_for_age - snap.as_of > _td(days=STALE_THRESHOLD_DAYS):
        return BullpenSignal(0.0, 0.0, 'low', snap.as_of)

    # Quality: LOWER ERA is BETTER — signed so team-with-3.20-pen
    # yields positive quality delta.
    if snap.bullpen_era is not None:
        raw_quality = (LEAGUE_AVG_BULLPEN_ERA - snap.bullpen_era) * QUALITY_SCALE_FACTOR
        # Cap so a single outlier pen doesn't dominate the score.
        quality = max(-QUALITY_ABS_CAP, min(QUALITY_ABS_CAP, raw_quality))
    else:
        quality = 0.0

    # Fatigue: negative when the top arm isn't available OR the pen has
    # been used heavily in the last two days. Both signals require
    # data the reliever-appearance ingestion (Phase 2B) will populate;
    # both fields are nullable on the snapshot so partial data flows
    # cleanly.
    fatigue = 0.0
    if snap.top_reliever_available is False:
        fatigue -= FATIGUE_UNAVAILABLE_PENALTY
    if (
        snap.appearances_last_2_days is not None
        and snap.appearances_last_2_days >= FATIGUE_OVERWORKED_THRESHOLD_2D
    ):
        fatigue -= FATIGUE_OVERWORKED_PENALTY

    return BullpenSignal(
        quality_delta=float(quality),
        fatigue_delta=float(fatigue),
        data_confidence=snap.data_confidence or 'low',
        snapshot_as_of=snap.as_of,
    )


def quality_delta(team, *, reference_date) -> float:
    """Convenience — team-side quality delta only."""
    return team_bullpen_signal(team, reference_date).quality_delta


def fatigue_delta(team, *, reference_date) -> float:
    """Convenience — team-side fatigue delta only."""
    return team_bullpen_signal(team, reference_date).fatigue_delta
