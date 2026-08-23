"""v3.4 SHADOW — team offensive-strength signal.

Answers the Track B question: does an explicit offensive-strength
signal, computed leakage-safely from historical runs scored, contain
predictive information beyond what Elo (which captures aggregate
W-L strength) already encodes?

DATA SOURCE — no new ingestion required

  Every historical MLB Game row already contains `home_score` and
  `away_score` for status='final' rows. For a target team T's offense
  at reference_date R, we can sum `runs scored` across every final
  game T played strictly before R and divide by n_games. This is:

    * Leakage-safe by construction (strict `first_pitch < R` filter).
    * Zero API cost (uses local DB).
    * Deterministic (same inputs → same output).
    * Historically reproducible back to whenever we ingested MLB games.

  We could also derive OPS-style hitting metrics from per-player game
  logs, but that requires substantial ingestion (~30 teams × ~15
  hitters × 96 games/season = ~43k player-game rows). Runs-per-game
  is the crudest metric but works with what we already have and
  answers the primary question ("does explicit offense help?") before
  we invest in richer per-player stats.

WHY THIS IS DIFFERENT FROM ELO

  Elo captures aggregate W-L outcome. A team that wins 6-5 gets the
  same rating bump as one that wins 15-2. Runs-per-game separates
  offensive volume from win outcomes — a lineup that scores 6 runs
  in a loss is invisible to Elo but visible here. If offense adds
  predictive value beyond Elo, it should show as a moneyline signal
  in the offense_replay experiment; if not, the feature is subsumed
  by Elo and we drop it.

FLAG DISCIPLINE

  `USE_TEAM_OFFENSE=false` (code default). Shadow computation runs on
  every score call; contribution enters the score only when the flag
  is on. Same pattern as bullpen and lineup — the ONLY change in
  production behavior when this ships without activation is that
  `feature_contributions` gets a new set of zero fields populated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


# --- Empirical constants (documented for auditability). ---

# Rolling window default. 30 days is the "recent form" horizon — same
# convention used for bullpen quality. Longer windows dilute recency;
# shorter windows are too noisy on a MLB slate.
DEFAULT_WINDOW_DAYS = 30

# MLB league-average runs per game (per team). Roughly 4.5 in the
# modern era — used as the anchor for the differential.
LEAGUE_AVG_RUNS_PER_GAME = 4.50

# Rating-scale conversion. Runs-per-game delta of ~1.0 corresponds
# roughly to a 5-unit rating delta on the legacy Team.rating scale
# (empirical starting point — reviewed in the offense_replay ship
# criteria).
RUNS_TO_RATING_UNITS = 5.0

# Cap so a fluky small-sample high scores doesn't dominate the signal.
QUALITY_ABS_CAP_UNITS = 10.0

# Minimum games required in the window for a signal to be trusted.
# Below this we return zero with data_confidence='low'.
MIN_GAMES_FOR_SIGNAL = 8

# Stale-data safety — if the newest final game the team played in the
# lookback is more than 21 days before reference_date, treat as stale.
STALE_THRESHOLD_DAYS = 21


@dataclass(frozen=True)
class OffenseSignal:
    """Team-side offensive strength shadow signal.

    quality_delta is in rating-scale units, signed so a higher-scoring
    team yields a positive delta. When insufficient data exists,
    quality_delta=0.0 with data_confidence='low'."""
    quality_delta: float
    runs_per_game: float
    n_games: int
    data_confidence: str    # 'low' / 'med' / 'high'
    latest_game_first_pitch: Optional[datetime]


def _aggregate_runs(team, reference_date, window_days: int):
    """Return (n_games, total_runs_scored) for team over
    [reference_date - window_days, reference_date) strict."""
    from django.db.models import Case, F, IntegerField, Sum, When
    from apps.mlb.models import Game

    cutoff = reference_date - timedelta(days=window_days)
    games_qs = Game.objects.filter(
        status='final',
        home_score__isnull=False,
        away_score__isnull=False,
        first_pitch__lt=reference_date,
        first_pitch__gte=cutoff,
    ).filter(
        # Team was either home or away in this game.
        # OR-combined via Q.
    )
    from django.db.models import Q
    games_qs = games_qs.filter(Q(home_team=team) | Q(away_team=team))

    # Aggregate: for each game, runs scored BY THIS TEAM is home_score
    # if team was home, else away_score. Compute via a Case/When
    # annotated sum.
    agg = games_qs.aggregate(
        runs=Sum(Case(
            When(home_team=team, then=F('home_score')),
            When(away_team=team, then=F('away_score')),
            default=0, output_field=IntegerField(),
        )),
    )
    total_runs = int(agg.get('runs') or 0)
    n = games_qs.count()
    return n, total_runs


def _latest_game_first_pitch(team, reference_date):
    from django.db.models import Q
    from apps.mlb.models import Game
    latest = (
        Game.objects
        .filter(status='final', first_pitch__lt=reference_date)
        .filter(Q(home_team=team) | Q(away_team=team))
        .order_by('-first_pitch')
        .first()
    )
    return latest.first_pitch if latest else None


def team_offense_signal(team, reference_date,
                        *, window_days: int = DEFAULT_WINDOW_DAYS) -> OffenseSignal:
    """Return the team's offensive-strength signal as-of reference_date.

    Never raises. Returns zero + 'low' confidence when:
      * team is None
      * reference_date is None (defaults to timezone.now() safely)
      * fewer than MIN_GAMES_FOR_SIGNAL games in the window
      * newest game in the window is older than STALE_THRESHOLD_DAYS
    """
    if team is None:
        return OffenseSignal(0.0, 0.0, 0, 'low', None)
    if reference_date is None:
        from django.utils import timezone
        reference_date = timezone.now()

    try:
        n, total_runs = _aggregate_runs(team, reference_date, window_days)
    except Exception:
        # Defensive: any transient DB / schema issue must not crash a
        # score computation.
        return OffenseSignal(0.0, 0.0, 0, 'low', None)

    latest = _latest_game_first_pitch(team, reference_date)
    if n < MIN_GAMES_FOR_SIGNAL:
        return OffenseSignal(
            quality_delta=0.0, runs_per_game=0.0, n_games=n,
            data_confidence='low', latest_game_first_pitch=latest,
        )

    if latest is not None and (
        reference_date - latest > timedelta(days=STALE_THRESHOLD_DAYS)
    ):
        return OffenseSignal(
            quality_delta=0.0, runs_per_game=0.0, n_games=n,
            data_confidence='low', latest_game_first_pitch=latest,
        )

    runs_per_game = total_runs / n
    raw_delta = (runs_per_game - LEAGUE_AVG_RUNS_PER_GAME) * RUNS_TO_RATING_UNITS
    quality = max(-QUALITY_ABS_CAP_UNITS, min(QUALITY_ABS_CAP_UNITS, raw_delta))

    return OffenseSignal(
        quality_delta=float(quality),
        runs_per_game=round(runs_per_game, 3),
        n_games=n,
        data_confidence='high' if n >= 20 else 'med',
        latest_game_first_pitch=latest,
    )


def quality_delta(team, *, reference_date) -> float:
    """Convenience — team-side offensive quality delta only."""
    return team_offense_signal(team, reference_date).quality_delta
