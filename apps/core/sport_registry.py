"""Sport registry — single source of truth for team-sport metadata.

Each entry describes how the lobby (and other shared code) should
interact with a given sport: which Game model to query, which field
holds the start time, which compute function to use, etc.

Keeping this data-driven instead of sprinkling `if sport == 'cbb'`
branches throughout views.py means adding a fifth team sport in the
future is a single entry here, not a sweep across files.

The UI-facing "label" is what appears on lobby tabs and in empty-state
copy. `season_months` is inclusive and used by `is_in_season` to decide
whether a tab should surface even when no games are in the DB yet
(off-season placeholder).
"""
from apps.cfb.models import Game as CFBGame
from apps.cbb.models import Game as CBBGame
from apps.mlb.models import Game as MLBGame
from apps.college_baseball.models import Game as CBaseballGame
from apps.cfb.services.model_service import compute_game_data as cfb_compute
from apps.cbb.services.model_service import compute_game_data as cbb_compute
from apps.mlb.services.model_service import compute_game_data as mlb_compute
from apps.college_baseball.services.model_service import compute_game_data as cb_compute


SPORT_REGISTRY = {
    'cfb': {
        'label': 'CFB',
        'game_model': CFBGame,
        'time_field': 'kickoff',
        'compute_fn': cfb_compute,
        'season_months': [8, 9, 10, 11, 12, 1],
    },
    'cbb': {
        'label': 'CBB',
        'game_model': CBBGame,
        'time_field': 'tipoff',
        'compute_fn': cbb_compute,
        'season_months': [11, 12, 1, 2, 3, 4],
    },
    'mlb': {
        'label': 'MLB',
        'game_model': MLBGame,
        'time_field': 'first_pitch',
        'compute_fn': mlb_compute,
        'season_months': [3, 4, 5, 6, 7, 8, 9, 10],
    },
    'college_baseball': {
        'label': 'College Baseball',
        'game_model': CBaseballGame,
        'time_field': 'first_pitch',
        'compute_fn': cb_compute,
        'season_months': [2, 3, 4, 5, 6],
    },
}

# Ordered tuple for deterministic lobby-tab display
SPORT_ORDER = ['cbb', 'cfb', 'mlb', 'college_baseball']


def get_sport(key):
    """Return the registry entry for a sport, or None if unknown."""
    return SPORT_REGISTRY.get(key)


def all_team_sports():
    """Iterate (key, entry) tuples in the display order."""
    for key in SPORT_ORDER:
        if key in SPORT_REGISTRY:
            yield key, SPORT_REGISTRY[key]
