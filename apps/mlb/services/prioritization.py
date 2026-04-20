"""MLB game prioritization, signals, and guided-choice resolver.

Architectural rules:
    - Signals live here. Views orchestrate. Templates render.
    - `reasons` are *structured keys* (e.g. 'tight_spread'), never UI strings.
      The `mlb_reasons` template filter (apps.mlb.templatetags.mlb_reasons)
      maps keys to human-readable labels at render time.
    - `actions` are dicts: {'type', 'strength', 'reason'} where strength is
      'primary' or 'secondary'. Exactly one primary per game.
    - Top Opportunity scarcity: by default only the single best primary
      Best Bet across the page is tagged `is_top_opportunity=True`. This
      is configurable via `settings.MLB_MAX_TOP_OPPORTUNITIES` or at the
      call site.

Bucket thresholds (after summing weighted contributions):
    score >= 3.0  -> 'high'
    1.5 <= score  -> 'medium'
    else          -> 'low'
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Optional

from django.utils import timezone


# --- tunable weights ---------------------------------------------------------

WEIGHTS = {
    'tight_spread': 2.0,        # within 1.5 runs
    'moderate_spread': 1.0,     # within 2.5 runs
    'close_live_score': 2.0,    # within 2 runs while live
    'blowout_live_score': -1.5, # 6+ run margin while live
    'high_injury': 1.5,
    'med_injury': 0.5,
    'ace_matchup': 1.2,         # both starters rating >= 65
    'pitcher_tbd': -1.0,        # confidence penalty
    'line_value': 2.0,          # market/model discrepancy — real edge signal
    'late_game': 0.5,           # minor boost — late games deserve a look
    # Future extension seams (return 0 today):
    'favorite_team': 2.0,
    'odds_movement': 1.5,
    'game_importance': 2.0,
}

HIGH_CUTOFF = 3.0
MEDIUM_CUTOFF = 1.5

# Line-value thresholds (|model_prob - market_prob|).
LINE_VALUE_MIN = 0.06         # below this, no signal
LINE_VALUE_STRONG = 0.10      # above this, counts toward Best Bet eligibility

# Late-game proxy thresholds. Baseball games average ~3h. Until the MLB
# Stats API inning state is ingested, we approximate progression from
# elapsed time since first pitch.
GAME_LENGTH_HOURS = 3.0
LATE_GAME_PROGRESSION = 0.60  # ≥ 60% through → "late"


@dataclass
class GameSignals:
    """Enriched game context for the MLB hub tiles.

    `game` is the original Game model — templates may still reach into
    `game.home_team`, `game.first_pitch`, etc. for rendering.
    """
    game: object
    priority: str                                 # 'high' | 'medium' | 'low'
    priority_score: float
    reasons: list[str] = field(default_factory=list)  # structured keys, not UI text
    latest_odds: object | None = None
    injury_summary: dict = field(default_factory=dict)
    ace_matchup: bool = False
    pitchers_known: bool = True

    # Action-layer flags
    is_close_game: bool = False
    is_blowout: bool = False
    late_game: bool = False
    tbd_pitcher: bool = False
    actions: list[dict] = field(default_factory=list)  # [{type, strength, reason}]
    is_top_opportunity: bool = False

    # Edge / value signals
    house_prob: float | None = None
    market_prob: float | None = None
    line_value_discrepancy: float | None = None

    # Display context
    home_record: str | None = None
    away_record: str | None = None
    home_streak: dict | None = None
    away_streak: dict | None = None
    home_pitcher_record: str | None = None
    away_pitcher_record: str | None = None
    has_user_bet: bool = False


# Action keys rendered by templates. Keep in sync with tile partials + CSS.
ACTION_KEYS = ('watch_now', 'best_bet')
ACTION_STRENGTHS = ('primary', 'secondary')

# Set of every reason key the resolver can emit. Helps the template filter
# detect typos; any unknown key falls back to a title-cased rendering.
REASON_KEYS = frozenset({
    'tight_spread', 'moderate_spread', 'close_game_live',
    'high_injury', 'med_injury', 'ace_matchup', 'line_value',
    'late_game', 'tbd_pitcher',
})


# --- individual signals ------------------------------------------------------

def _spread_signal(odds) -> tuple[float, Optional[str]]:
    if odds is None or odds.spread is None:
        return 0.0, None
    mag = abs(odds.spread)
    if mag <= 1.5:
        return WEIGHTS['tight_spread'], 'tight_spread'
    if mag <= 2.5:
        return WEIGHTS['moderate_spread'], 'moderate_spread'
    return 0.0, None


def _live_score_signal(game) -> tuple[float, Optional[str]]:
    if game.status != 'live' or game.home_score is None or game.away_score is None:
        return 0.0, None
    margin = abs(game.home_score - game.away_score)
    if margin <= 2:
        return WEIGHTS['close_live_score'], 'close_game_live'
    if margin >= 6:
        return WEIGHTS['blowout_live_score'], None
    return 0.0, None


def _injury_signal(injuries) -> tuple[float, Optional[str], dict]:
    summary = {'home': None, 'away': None}
    for inj in injuries:
        team_side = 'home' if inj.team_id == inj.game.home_team_id else 'away'
        current = summary[team_side]
        if inj.impact_level == 'high' and current != 'high':
            summary[team_side] = 'high'
        elif inj.impact_level == 'med' and current is None:
            summary[team_side] = 'med'
        elif inj.impact_level == 'low' and current is None:
            summary[team_side] = 'low'
    if 'high' in summary.values():
        return WEIGHTS['high_injury'], 'high_injury', summary
    if 'med' in summary.values():
        return WEIGHTS['med_injury'], 'med_injury', summary
    return 0.0, None, summary


def _pitcher_signal(game) -> tuple[float, Optional[str], bool, bool]:
    """Returns (contribution, reason_key, ace_matchup, both_known)."""
    hp, ap = game.home_pitcher, game.away_pitcher
    if hp is None or ap is None:
        return WEIGHTS['pitcher_tbd'], None, False, False
    if hp.rating >= 65 and ap.rating >= 65:
        return WEIGHTS['ace_matchup'], 'ace_matchup', True, True
    return 0.0, None, False, True


def _line_value_signal(house_prob, market_prob) -> tuple[float, Optional[str], Optional[float]]:
    """Line-value discrepancy between model and market win probabilities.

    Returns (contribution, reason_key, discrepancy) where discrepancy is the
    absolute delta (always emitted when both probs are known — consumers can
    inspect magnitude; only ≥ LINE_VALUE_MIN contributes to the score and
    emits the 'line_value' reason).
    """
    if house_prob is None or market_prob is None:
        return 0.0, None, None
    diff = abs(house_prob - market_prob)
    if diff < LINE_VALUE_MIN:
        return 0.0, None, diff
    return WEIGHTS['line_value'], 'line_value', diff


def _late_game_signal(game) -> tuple[float, Optional[str], bool]:
    """Proxy for late-game status using elapsed time since first pitch.

    Returns (contribution, reason_key, is_late_flag). Only applies to live
    games. Once MLB Stats API inning state is ingested, swap this for a
    real inning check without touching callers.
    """
    if game.status != 'live' or game.first_pitch is None:
        return 0.0, None, False
    elapsed_h = (timezone.now() - game.first_pitch).total_seconds() / 3600.0
    progression = max(0.0, min(1.0, elapsed_h / GAME_LENGTH_HOURS))
    if progression >= LATE_GAME_PROGRESSION:
        return WEIGHTS['late_game'], 'late_game', True
    return 0.0, None, False


# --- future extension seams (no-op today) -----------------------------------

def _favorite_team_signal(game, user):
    return 0.0, None


def _odds_movement_signal(game):
    return 0.0, None


def _game_importance_signal(game):
    return 0.0, None


def _bucket(score: float) -> str:
    if score >= HIGH_CUTOFF:
        return 'high'
    if score >= MEDIUM_CUTOFF:
        return 'medium'
    return 'low'


# --- action resolver --------------------------------------------------------

def resolve_actions(s: 'GameSignals') -> list[dict]:
    """Map a GameSignals object to a list of action dicts.

    Shape:
        [{'type': 'best_bet', 'strength': 'primary', 'reason': 'line_value'},
         {'type': 'watch_now', 'strength': 'secondary', 'reason': 'close_game_live'}]

    Rules:
        - Best Bet requires: odds present, starters known (no TBD), not blowout,
          AND at least one "strong" edge signal (tight_spread OR line_value).
        - Watch Now prioritizes close live games, then late-game proxy, then
          ace matchup (if not a blowout).
        - When both actions fire, Best Bet is primary and Watch Now is secondary.
        - When only one fires, it's primary.
        - Exactly one primary per game. Empty list is a valid, clean state.
    """
    actions: list[dict] = []

    # Best Bet eligibility
    bb_reason = None
    odds = s.latest_odds
    if (
        odds is not None
        and s.pitchers_known
        and not s.is_blowout
    ):
        # line_value is the stronger of the two edge signals — prefer it as
        # the attached reason when both are present.
        if 'line_value' in s.reasons:
            bb_reason = 'line_value'
        elif 'tight_spread' in s.reasons:
            bb_reason = 'tight_spread'

    # Watch Now selection — priority order: close live > late > ace
    wn_reason = None
    if s.is_close_game:
        wn_reason = 'close_game_live'
    elif s.late_game:
        wn_reason = 'late_game'
    elif s.ace_matchup and not s.is_blowout:
        wn_reason = 'ace_matchup'

    if bb_reason is not None:
        actions.append({'type': 'best_bet', 'strength': 'primary', 'reason': bb_reason})
        if wn_reason is not None:
            actions.append({'type': 'watch_now', 'strength': 'secondary', 'reason': wn_reason})
    elif wn_reason is not None:
        actions.append({'type': 'watch_now', 'strength': 'primary', 'reason': wn_reason})

    return actions


# --- public API -------------------------------------------------------------

def build_signals(game, *, user=None, streaks=None, user_bet_game_ids=None) -> GameSignals:
    """Compute signals for a single game. Pure function — no DB writes.

    `streaks` and `user_bet_game_ids` are optional pre-computed batches from
    `prioritize()`. Callers that work on a single game may omit both.
    """
    latest_odds = game.odds_snapshots.order_by('-captured_at').first()
    injuries = list(game.injuries.all())
    for inj in injuries:
        inj.game = game

    score = 0.0
    reasons: list[str] = []

    def _add(signal_result):
        nonlocal score
        contrib, reason = signal_result[:2]
        score += contrib
        if reason:
            reasons.append(reason)

    _add(_spread_signal(latest_odds))
    _add(_live_score_signal(game))

    injury_contrib, injury_reason, injury_summary = _injury_signal(injuries)
    score += injury_contrib
    if injury_reason:
        reasons.append(injury_reason)

    pitcher_contrib, pitcher_reason, ace, known = _pitcher_signal(game)
    score += pitcher_contrib
    if pitcher_reason:
        reasons.append(pitcher_reason)
    if not known:
        reasons.append('tbd_pitcher')

    # --- compute model vs market probabilities for line-value ---
    from apps.mlb.services.model_service import compute_house_win_prob
    market_prob = latest_odds.market_home_win_prob if latest_odds else None
    house_prob = compute_house_win_prob(game, latest_odds=latest_odds, injuries=injuries)
    lv_contrib, lv_reason, lv_diff = _line_value_signal(house_prob, market_prob)
    score += lv_contrib
    if lv_reason:
        reasons.append(lv_reason)

    # --- late-game proxy ---
    late_contrib, late_reason, late_flag = _late_game_signal(game)
    score += late_contrib
    if late_reason:
        reasons.append(late_reason)

    # Future seams — contribute 0 today.
    score += _favorite_team_signal(game, user)[0]
    score += _odds_movement_signal(game)[0]
    score += _game_importance_signal(game)[0]

    # Boolean flags for the action layer
    is_close = is_blowout = False
    if game.status == 'live' and game.home_score is not None and game.away_score is not None:
        margin = abs(game.home_score - game.away_score)
        is_close = margin <= 2
        is_blowout = margin >= 6

    from .streaks import format_record, format_pitcher_record
    streaks = streaks or {}
    user_bet_game_ids = user_bet_game_ids or set()

    signals = GameSignals(
        game=game,
        priority=_bucket(score),
        priority_score=round(score, 3),
        reasons=reasons,
        latest_odds=latest_odds,
        injury_summary=injury_summary,
        ace_matchup=ace,
        pitchers_known=known,
        is_close_game=is_close,
        is_blowout=is_blowout,
        late_game=late_flag,
        tbd_pitcher=not known,
        house_prob=house_prob,
        market_prob=market_prob,
        line_value_discrepancy=lv_diff,
        home_record=format_record(game.home_team),
        away_record=format_record(game.away_team),
        home_streak=streaks.get(game.home_team_id),
        away_streak=streaks.get(game.away_team_id),
        home_pitcher_record=format_pitcher_record(game.home_pitcher),
        away_pitcher_record=format_pitcher_record(game.away_pitcher),
        has_user_bet=(game.id in user_bet_game_ids),
    )
    signals.actions = resolve_actions(signals)
    return signals


def prioritize(games: Iterable, *, user=None) -> list[GameSignals]:
    """Enrich a list of games with signals. Order is preserved — caller sorts.

    Batches streaks + user's pending mock bets across all games on the page.
    """
    from .streaks import compute_streaks
    games = list(games)

    team_ids = set()
    for g in games:
        team_ids.add(g.home_team_id)
        team_ids.add(g.away_team_id)
    streaks = compute_streaks(team_ids)

    user_bet_game_ids: set = set()
    if user is not None and getattr(user, 'is_authenticated', False) and games:
        from apps.mockbets.models import MockBet
        user_bet_game_ids = set(
            MockBet.objects
            .filter(user=user, result='pending', mlb_game_id__in=[g.id for g in games])
            .values_list('mlb_game_id', flat=True)
        )

    return [
        build_signals(g, user=user, streaks=streaks, user_bet_game_ids=user_bet_game_ids)
        for g in games
    ]


def mark_top_opportunities(signals_list: list[GameSignals], *, n: int | None = None) -> list[GameSignals]:
    """Tag the top-N primary-Best-Bet signals with `is_top_opportunity=True`.

    Defaults to `settings.MLB_MAX_TOP_OPPORTUNITIES` (defaults to 1). Passing
    an explicit `n` overrides.

    Scarcity matters: too many "Top Opportunity" tags dilutes the meaning.
    Default of 1 forces the system to actually pick its best spot.

    Tie-break order:
        1. priority_score desc
        2. line_value_discrepancy desc (larger edge wins)
        3. first_pitch asc (earlier game wins — you can actually act on it)
        4. game.id asc (deterministic)
    """
    if n is None:
        from django.conf import settings
        n = getattr(settings, 'MLB_MAX_TOP_OPPORTUNITIES', 1)

    eligible = [
        s for s in signals_list
        if any(a['type'] == 'best_bet' and a['strength'] == 'primary' for a in s.actions)
    ]
    eligible.sort(key=lambda s: (
        -s.priority_score,
        -(s.line_value_discrepancy or 0.0),
        s.game.first_pitch,
        str(s.game.id),
    ))
    for s in eligible[:max(0, n)]:
        s.is_top_opportunity = True
    return signals_list


def sort_live(signals: list[GameSignals]) -> list[GameSignals]:
    """Live sort: priority desc only. Inning/progression not yet ingested."""
    return sorted(signals, key=lambda s: -s.priority_score)


def sort_today(signals: list[GameSignals]) -> list[GameSignals]:
    """Today sort: priority desc, then earliest first_pitch first."""
    return sorted(signals, key=lambda s: (-s.priority_score, s.game.first_pitch))
