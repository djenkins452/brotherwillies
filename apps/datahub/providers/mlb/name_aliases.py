"""MLB team-name normalization.

MLB Stats API uses official names like "New York Yankees"; The Odds API
uses the same canonical full names. This keeps the matcher defensive
against minor variations (e.g. "LA Angels" vs "Los Angeles Angels").
"""

MLB_TEAM_ALIASES = {
    'la angels': 'Los Angeles Angels',
    'ny yankees': 'New York Yankees',
    'ny mets': 'New York Mets',
    'chicago cubs': 'Chicago Cubs',
    'chicago white sox': 'Chicago White Sox',
    'sf giants': 'San Francisco Giants',
    'sd padres': 'San Diego Padres',
    'tampa bay': 'Tampa Bay Rays',
    'az diamondbacks': 'Arizona Diamondbacks',
    'd-backs': 'Arizona Diamondbacks',
    'redsox': 'Boston Red Sox',
    'white sox': 'Chicago White Sox',
    'red sox': 'Boston Red Sox',
}


def normalize_mlb_team_name(name):
    if not name:
        return name
    key = name.strip().lower()
    if key in MLB_TEAM_ALIASES:
        return MLB_TEAM_ALIASES[key]
    return name.strip()
