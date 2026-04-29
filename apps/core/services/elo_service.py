"""Auto-updating team ratings — Elo, sport-aware.

Replaces (when enabled) the static `Team.rating` floats that have been the
modeling foundation since v1. Static ratings drift from reality across a
season and have no update loop; Elo auto-updates from results, with a
configurable K-factor per sport and (for football/basketball) a margin
multiplier that dampens blowouts and boosts upsets.

This module is *math + persistence orchestration only*. It does not modify
the recommendation engine, decision rules, or thresholds. Integration
happens at one point: each sport's `model_service` calls
`team_rating_for_model(team, sport)` to fetch the rating that flows into
the existing win-prob formula. When `USE_DYNAMIC_RATINGS=False` (default)
or `team.elo_rating is None`, that helper returns the legacy `team.rating`
unchanged — no behavior change in production until the flag is flipped.

Sport-specific behavior (per requirements):
  - **MLB / college_baseball**: win/loss only. Margin of victory is NOT
    used because run differentials in baseball have wild variance driven
    by bullpen blow-ups, garbage-time scoring, and pitcher fatigue cycles
    that don't reliably reflect team strength.
  - **CFB / CBB**: margin-adjusted with the 538-style multiplier:
        ln(margin + 1) * (2.2 / (rating_diff_for_winner * 0.001 + 2.2))
    Margin is capped at MAX_MARGIN to prevent blowouts from breaking the
    rating system. The multiplier dampens the contribution from "obvious"
    wins (heavy favorite, big margin) while keeping informational value
    from upsets.

Defaults are documented inline. They were chosen to:
  - Keep K small enough for long seasons (MLB) so a single game can't
    swing a team rating by more than a few points.
  - Make HFA Elo points roughly equivalent to the existing legacy-rating
    HFA values once converted through the (elo - 1500) / 25 + 50 mapping.
  - Cap margins at sport-realistic upper bounds (4 TDs in CFB, ~20 pts in CBB).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from django.conf import settings


# Initial Elo rating for any team that has not been processed yet — the
# standard 1500 baseline (perfectly average team). Through the conversion
# (1500 - 1500) / 25 + 50 = 50, which exactly matches the legacy
# Team.rating default, so a freshly-rebuilt team produces the same model
# output as a never-rebuilt team.
INITIAL_RATING = 1500.0

# K-factor per sport. Higher K = more reactive ratings (more weight on
# recent results vs prior). College sports run shorter seasons and have
# bigger upsets, so K=20 captures movement without being too noisy.
# MLB/college_baseball play 60-162 games — K must be small or ratings
# whip around more than skill changes warrant.
K_FACTORS = {
    'cfb': 20.0,
    'cbb': 20.0,
    'mlb': 4.0,
    'college_baseball': 4.0,
}

# Home-field/court advantage in Elo points. Adds to the home team's
# expected-win-probability calculation when the game is not at a neutral
# site. Calibrated to the long-run home-win rates in each sport:
#   - CBB: ~62% home-win rate league-wide → larger HFA bump
#   - CFB: ~58% → moderate
#   - MLB: ~54% → small
#   - college_baseball: ~55% → small
HFA_ELO = {
    'cfb': 65.0,
    'cbb': 85.0,
    'mlb': 24.0,
    'college_baseball': 20.0,
}

# Cap the margin used in the multiplier to prevent rating runaway from
# extreme outcomes. A 70-7 CFB beating tells us the same thing as 35-7
# (one team is way better than the other); we don't want the rating to
# move 2× as much.
MAX_MARGIN = {
    'cfb': 24,   # ~4 TDs
    'cbb': 20,   # ~25% of typical scoring total
    # MLB/college_baseball ignore margin entirely (multiplier=1.0), but
    # we still set a cap defensively in case the multiplier function is
    # ever called with one of these sports.
    'mlb': 0,
    'college_baseball': 0,
}

# Sports that use margin-adjusted Elo. Others use plain win/loss (mult=1).
MARGIN_AWARE_SPORTS = {'cfb', 'cbb'}


# ---------------------------------------------------------------------------
# Core Elo math


def expected_win_prob(rating_a: float, rating_b: float, hfa_elo: float = 0.0) -> float:
    """Standard Elo expected score for player A.

    `hfa_elo` is added to A's effective rating before the calculation —
    so when A is at home, pass +HFA; when away, pass 0 (or pass -HFA to
    apply HFA from B's perspective). The caller decides; this function
    is unaware of who's at home.

    Returns a value in (0, 1). Symmetric: `expected(b, a, -hfa) == 1 - expected(a, b, hfa)`.
    """
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a - hfa_elo) / 400.0))


def margin_multiplier(margin: int, rating_diff_for_winner: float, sport: str) -> float:
    """538-style margin-of-victory multiplier for CFB/CBB.

    Args:
        margin: |home_score - away_score| — always non-negative.
        rating_diff_for_winner: pre-game (winner_elo - loser_elo). Positive
            when the favorite won; negative when the underdog won.
        sport: dispatch key. Returns 1.0 for sports that don't use MOV.

    The formula:

        ln(margin + 1) * (2.2 / (rating_diff_winner * 0.001 + 2.2))

    Why it works:
      - `ln(margin + 1)` rises sharply at first then plateaus → diminishing
        returns. Going from 1 to 7 points is more informative than 21 to 28.
      - The denominator (rating_diff_winner * 0.001 + 2.2) is BIGGER when
        the favorite wins by a lot, dampening the multiplier — we already
        knew the favorite was better, the result is unsurprising.
      - When the underdog wins, rating_diff_winner is negative, so the
        denominator shrinks → multiplier is amplified — upsets carry more
        information about the new ratings than chalk wins.

    Margin is capped at MAX_MARGIN[sport] so blowouts can't break the
    system. Returns 1.0 for sports outside MARGIN_AWARE_SPORTS.
    """
    if sport not in MARGIN_AWARE_SPORTS:
        return 1.0
    capped = min(abs(int(margin)), MAX_MARGIN[sport])
    if capped <= 0:
        # Tied game (impossible in practice for finalized CFB/CBB but
        # defensive). No information about ratings, mult = 1.0.
        return 1.0
    denom = (rating_diff_for_winner * 0.001) + 2.2
    # Defensive: if rating_diff_for_winner is hugely negative (a HUGE
    # upset by Elo standards) the denominator could approach zero. Floor
    # it at 0.5 to keep the multiplier from blowing up.
    denom = max(0.5, denom)
    return math.log(capped + 1) * (2.2 / denom)


def update_ratings(
    home_rating: float,
    away_rating: float,
    home_won: bool,
    margin: int,
    sport: str,
    neutral_site: bool = False,
) -> Tuple[float, float, float, float]:
    """Apply one game's Elo update.

    Returns: (new_home_rating, new_away_rating, delta_applied, margin_mult)

    `delta_applied` is the signed magnitude of rating change for the home
    team (positive when home won by more than expected, negative when
    home lost by more than expected). The away team's change is exactly
    -delta — Elo is conservative.

    The home team's expected score includes HFA when the game is not at
    a neutral site. HFA is sport-specific (HFA_ELO[sport]).
    """
    k = K_FACTORS[sport]
    hfa = HFA_ELO[sport] if not neutral_site else 0.0

    expected_home = expected_win_prob(home_rating, away_rating, hfa)
    actual_home = 1.0 if home_won else 0.0

    if sport in MARGIN_AWARE_SPORTS:
        # Sign of rating diff is from the winner's perspective for the
        # multiplier: bigger when underdog wins (rating_diff_for_winner < 0).
        if home_won:
            rating_diff_winner = home_rating - away_rating + (hfa if not neutral_site else 0)
        else:
            rating_diff_winner = away_rating - home_rating - (hfa if not neutral_site else 0)
        mult = margin_multiplier(margin, rating_diff_winner, sport)
    else:
        mult = 1.0

    delta = k * mult * (actual_home - expected_home)
    return home_rating + delta, away_rating - delta, delta, mult


# ---------------------------------------------------------------------------
# Rating accessors used by model_service integration


# Conversion factor between Elo (centered at 1500) and the legacy
# Team.rating scale (centered at 50).
#
# 2026-04-28 calibration tune: tightened 25 → 13. The original 25 was
# chosen to roughly match the static-rating spread, but it compressed
# Elo deltas too much — a 200-point Elo gap mapped to only 8 legacy
# points, then the sigmoid divisor (also flattened to 25 in this same
# tune) further damped the signal. With divisor 13, a 200-point Elo
# gap maps to ~15.4 legacy points — Elo changes now meaningfully
# affect probability output, while the flatter sigmoid keeps overall
# predictions less overconfident.
#
# Pairing: Elo path is now sigmoid(elo_diff / 13 / 25) = sigmoid(elo_diff / 325)
# vs the previous sigmoid(elo_diff / 25 / 15) = sigmoid(elo_diff / 375).
# Net: Elo signal is ~15% stronger; static signal is ~40% weaker —
# the desired calibration shift.
ELO_TO_LEGACY_DIVISOR = 13.0
ELO_BASELINE = 1500.0
LEGACY_BASELINE = 50.0


def elo_to_legacy_scale(elo_rating: float) -> float:
    """Project an Elo rating onto the legacy 50-centered scale.

    elo=1500 → legacy=50 (matches Team.rating default).
    elo=1600 → legacy=54 (good team).
    elo=1700 → legacy=58 (top tier).
    elo=1400 → legacy=46 (weak team).
    """
    return (elo_rating - ELO_BASELINE) / ELO_TO_LEGACY_DIVISOR + LEGACY_BASELINE


# Module-level override used by force_use_dynamic(). Set to None when no
# override is active — at that point team_rating_for_model reads the
# global Django setting. When set to True/False, the override wins.
#
# Concurrent-run safety: the analytics control page prevents two
# BacktestRun rows from being in 'running' status at once, so the
# override at most has one writer at a time. Tests that exercise both
# branches use the override or override_settings to keep their state
# isolated.
_force_use_dynamic_override = None


def force_use_dynamic(value: Optional[bool]):
    """Context manager that forces USE_DYNAMIC_RATINGS for nested calls.

    Used by the backtest analytics page to run Static vs Elo comparisons
    without flipping the global env-driven setting. The override is
    process-local and only safe under the analytics page's
    no-concurrent-runs guard.

    Pass `value=True` to force Elo, `value=False` to force static, or
    `value=None` to clear the override (defer to settings).
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        global _force_use_dynamic_override
        prev = _force_use_dynamic_override
        _force_use_dynamic_override = value
        try:
            yield
        finally:
            _force_use_dynamic_override = prev

    return _ctx()


def is_dynamic_active() -> bool:
    """Single source of truth for 'is dynamic Elo currently in effect?'.

    Reads the override first (set by `force_use_dynamic`), then falls
    back to the Django setting. Used by both `team_rating_for_model` and
    `run_backtest` so the rating mode is consistent across the run.
    """
    if _force_use_dynamic_override is not None:
        return bool(_force_use_dynamic_override)
    return bool(getattr(settings, 'USE_DYNAMIC_RATINGS', False))


def team_rating_for_model(team) -> float:
    """Return the rating that should flow into the win-prob formula for `team`.

    Branches on the active rating mode (override or `USE_DYNAMIC_RATINGS`)
    and per-team Elo availability. When dynamic AND the team has an
    `elo_rating` set, returns the Elo projected onto the legacy scale (so
    all the existing HFA/injury/pitcher math keeps working unchanged).
    Otherwise falls back to the legacy `team.rating` field.

    This is the single point of integration. By keeping the conversion
    here (instead of branching inside each sport's model_service), the
    flag flip is centralized and easy to audit.
    """
    if not is_dynamic_active():
        return team.rating
    elo = getattr(team, 'elo_rating', None)
    if elo is None:
        # Dynamic ratings on, but this team has no Elo yet (not rebuilt
        # after the flag flip, or new team). Fall back to legacy rating
        # so we never produce a weird hybrid.
        return team.rating
    return elo_to_legacy_scale(float(elo))


# ---------------------------------------------------------------------------
# Sport plumbing — used by the management commands


@dataclass
class _SportElo:
    """Per-sport plumbing: how to find games, teams, time, and FK fields.

    Keeps the rebuild/update commands sport-agnostic — they walk this
    descriptor instead of branching on sport strings in their bodies.
    """
    sport: str
    game_model_path: str       # 'cfb.Game' / 'cbb.Game' / etc.
    team_model_path: str       # 'cfb.Team' / etc.
    time_field: str            # 'kickoff' / 'tipoff' / 'first_pitch'
    history_team_fk: str       # field name on TeamEloHistory for this sport's team
    history_game_fk: str       # field name on TeamEloHistory for this sport's game


SPORT_ELO_REGISTRY = {
    'cfb': _SportElo(
        sport='cfb', game_model_path='cfb.Game', team_model_path='cfb.Team',
        time_field='kickoff', history_team_fk='cfb_team', history_game_fk='cfb_game',
    ),
    'cbb': _SportElo(
        sport='cbb', game_model_path='cbb.Game', team_model_path='cbb.Team',
        time_field='tipoff', history_team_fk='cbb_team', history_game_fk='cbb_game',
    ),
    'mlb': _SportElo(
        sport='mlb', game_model_path='mlb.Game', team_model_path='mlb.Team',
        time_field='first_pitch', history_team_fk='mlb_team', history_game_fk='mlb_game',
    ),
    'college_baseball': _SportElo(
        sport='college_baseball',
        game_model_path='college_baseball.Game', team_model_path='college_baseball.Team',
        time_field='first_pitch',
        history_team_fk='college_baseball_team',
        history_game_fk='college_baseball_game',
    ),
}


def get_sport_elo(sport: str) -> Optional[_SportElo]:
    return SPORT_ELO_REGISTRY.get(sport)


def get_team_model(sport: str):
    """Resolve `<app>.Team` to the actual model class lazily."""
    from django.apps import apps
    entry = SPORT_ELO_REGISTRY[sport]
    app_label, model_name = entry.team_model_path.split('.')
    return apps.get_model(app_label, model_name)


def get_game_model(sport: str):
    from django.apps import apps
    entry = SPORT_ELO_REGISTRY[sport]
    app_label, model_name = entry.game_model_path.split('.')
    return apps.get_model(app_label, model_name)


# ---------------------------------------------------------------------------
# Persistence-layer helpers (rebuild/update commands compose these)


def process_game(sport: str, game) -> bool:
    """Apply one game's Elo update + persist team ratings + history rows.

    Idempotent w.r.t. itself: a TeamEloHistory row already existing for
    this game causes a no-op. The caller (rebuild/update commands) is
    responsible for choosing which games to feed in.

    Returns True on update, False when skipped (already-processed, ties,
    missing scores).
    """
    from apps.analytics.models import TeamEloHistory

    if game.home_score is None or game.away_score is None:
        return False
    # Ties carry no Elo information and are vanishingly rare in our
    # sports — skip rather than half-credit, which adds noise.
    if game.home_score == game.away_score:
        return False

    entry = SPORT_ELO_REGISTRY[sport]

    # Idempotence guard: skip if this game already has history rows.
    already = TeamEloHistory.objects.filter(
        sport=sport,
        **{entry.history_game_fk: game},
    ).exists()
    if already:
        return False

    home = game.home_team
    away = game.away_team

    home_pre = home.elo_rating if home.elo_rating is not None else INITIAL_RATING
    away_pre = away.elo_rating if away.elo_rating is not None else INITIAL_RATING

    margin = abs(int(game.home_score) - int(game.away_score))
    home_won = game.home_score > game.away_score
    neutral = bool(getattr(game, 'neutral_site', False))

    new_home, new_away, delta, mult = update_ratings(
        home_pre, away_pre, home_won, margin, sport, neutral_site=neutral,
    )

    game_time = getattr(game, entry.time_field)
    home.elo_rating = new_home
    home.elo_last_updated = game_time
    home.save(update_fields=['elo_rating', 'elo_last_updated'])
    away.elo_rating = new_away
    away.elo_last_updated = game_time
    away.save(update_fields=['elo_rating', 'elo_last_updated'])

    # MLB/college_baseball don't use margin in their Elo update, so we
    # store None to make that explicit in the history (rather than the
    # raw run-differential, which could be misread as causal).
    margin_for_history = margin if sport in MARGIN_AWARE_SPORTS else None

    TeamEloHistory.objects.create(
        sport=sport,
        **{entry.history_team_fk: home, entry.history_game_fk: game},
        pre_rating=home_pre,
        post_rating=new_home,
        k_factor=K_FACTORS[sport],
        is_home=True,
        won=home_won,
        margin=margin_for_history,
        margin_multiplier=mult,
    )
    TeamEloHistory.objects.create(
        sport=sport,
        **{entry.history_team_fk: away, entry.history_game_fk: game},
        pre_rating=away_pre,
        post_rating=new_away,
        k_factor=K_FACTORS[sport],
        is_home=False,
        won=not home_won,
        margin=margin_for_history,
        margin_multiplier=mult,
    )
    return True


def reset_sport(sport: str) -> int:
    """Wipe all Elo state for a sport. Used by `rebuild_elo_ratings`.

    Returns the number of teams reset. TeamEloHistory rows for the sport
    are deleted; team `elo_rating` and `elo_last_updated` go back to None.
    """
    from apps.analytics.models import TeamEloHistory
    TeamEloHistory.objects.filter(sport=sport).delete()
    TeamModel = get_team_model(sport)
    return TeamModel.objects.update(elo_rating=None, elo_last_updated=None)
