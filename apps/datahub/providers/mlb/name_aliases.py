"""MLB team-name normalization — comprehensive variant coverage.

Two-stage match strategy used by the odds provider:

  Stage 1 — exact alias lookup
    A flat dict mapping every plausible variant (lowercased, punctuation-
    stripped) to the canonical full name we store in the DB. Constant time.

  Stage 2 — fuzzy fallback
    `fuzzy_match_to_canonical(name)` — used only when stage 1 misses. Walks
    the canonical list and returns the first match where:
      a) the input is a substring of the canonical, or
      b) the canonical's nickname is a substring of the input, or
      c) the input's nickname matches the canonical's nickname.
    No third-party libraries — pure string ops, deterministic, fast.

Why two stages and not just fuzzy: the alias dict catches well-known
formats with O(1) lookups and zero ambiguity ("d-backs" → Arizona). Fuzzy
catches the surprises that show up when an API rebrands or trims names,
without accidentally matching "Cardinals" to "Reds" because both contain
common letters.

ALL 30 MLB FRANCHISES are listed in CANONICAL_MLB_TEAMS. The Athletics
appear under both their nickname-only form (current branding after the
Oakland → Sacramento relocation) and the legacy "Oakland Athletics" form
because The Odds API has used both within the same season.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Single source of truth — every canonical MLB team name we expect to see
# in the DB. Used by fuzzy matching to walk candidates AND by tests to
# verify alias coverage. Order doesn't matter; lookups are linear and 30
# items is trivial.
CANONICAL_MLB_TEAMS = [
    'Arizona Diamondbacks',
    'Atlanta Braves',
    'Baltimore Orioles',
    'Boston Red Sox',
    'Chicago Cubs',
    'Chicago White Sox',
    'Cincinnati Reds',
    'Cleveland Guardians',
    'Colorado Rockies',
    'Detroit Tigers',
    'Houston Astros',
    'Kansas City Royals',
    'Los Angeles Angels',
    'Los Angeles Dodgers',
    'Miami Marlins',
    'Milwaukee Brewers',
    'Minnesota Twins',
    'New York Mets',
    'New York Yankees',
    'Athletics',  # Legacy "Oakland Athletics" also handled in alias dict.
    'Philadelphia Phillies',
    'Pittsburgh Pirates',
    'San Diego Padres',
    'San Francisco Giants',
    'Seattle Mariners',
    'St. Louis Cardinals',
    'Tampa Bay Rays',
    'Texas Rangers',
    'Toronto Blue Jays',
    'Washington Nationals',
]

# Mapping of (lowercased, punctuation-light) variant → canonical name.
# Generated programmatically below so each team gets a consistent set of
# variants (full, nickname, city, abbreviation), then augmented with
# hand-curated edge cases (St. Louis, "Athletics" rebrand, "D-backs", etc).
MLB_TEAM_ALIASES: dict = {}


def _add(*aliases: str, to: str):
    for a in aliases:
        # Normalize alias key the same way we'll normalize incoming API names.
        key = _normalize_key(a)
        if key:
            MLB_TEAM_ALIASES[key] = to


def _normalize_key(name: str) -> str:
    """Lowercased, punctuation-stripped, single-spaced. Used for both alias
    keys and incoming names so they hash the same way."""
    if not name:
        return ''
    # Remove periods (St. Louis → st louis), normalize whitespace.
    s = re.sub(r'[.\u2019\']', '', name).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    return s


# --- Per-team variant coverage ---------------------------------------------
# For each canonical team we register: full name, nickname-only, city-only
# (when unambiguous), 3-letter abbreviation, and common API quirks.

_add('Arizona Diamondbacks', 'arizona', 'diamondbacks', 'd-backs', 'dbacks',
     'd backs', 'arizona d-backs', 'az diamondbacks', 'ari',
     to='Arizona Diamondbacks')
_add('Atlanta Braves', 'atlanta', 'braves', 'atl',
     to='Atlanta Braves')
_add('Baltimore Orioles', 'baltimore', 'orioles', 'os', 'bal',
     to='Baltimore Orioles')
_add('Boston Red Sox', 'boston', 'red sox', 'redsox', 'bos',
     to='Boston Red Sox')
_add('Chicago Cubs', 'cubs', 'chi cubs', 'chicago cubs', 'chc',
     to='Chicago Cubs')
_add('Chicago White Sox', 'white sox', 'whitesox', 'chi white sox',
     'chicago white sox', 'cws', 'chw',
     to='Chicago White Sox')
_add('Cincinnati Reds', 'cincinnati', 'reds', 'cin',
     to='Cincinnati Reds')
_add('Cleveland Guardians', 'cleveland', 'guardians', 'cleveland indians',
     'indians', 'cle',
     to='Cleveland Guardians')
_add('Colorado Rockies', 'colorado', 'rockies', 'col',
     to='Colorado Rockies')
_add('Detroit Tigers', 'detroit', 'tigers', 'det',
     to='Detroit Tigers')
_add('Houston Astros', 'houston', 'astros', 'hou',
     to='Houston Astros')
_add('Kansas City Royals', 'kansas city', 'royals', 'kc royals', 'kc',
     to='Kansas City Royals')
_add('Los Angeles Angels', 'la angels', 'l.a. angels', 'angels',
     'los angeles angels', 'anaheim angels', 'laa',
     to='Los Angeles Angels')
_add('Los Angeles Dodgers', 'la dodgers', 'l.a. dodgers', 'dodgers',
     'los angeles dodgers', 'lad',
     to='Los Angeles Dodgers')
_add('Miami Marlins', 'miami', 'marlins', 'florida marlins', 'mia',
     to='Miami Marlins')
_add('Milwaukee Brewers', 'milwaukee', 'brewers', 'mil',
     to='Milwaukee Brewers')
_add('Minnesota Twins', 'minnesota', 'twins', 'min',
     to='Minnesota Twins')
_add('New York Mets', 'mets', 'ny mets', 'n.y. mets', 'nym',
     to='New York Mets')
_add('New York Yankees', 'yankees', 'ny yankees', 'n.y. yankees',
     'nyy', 'yanks',
     to='New York Yankees')
# Athletics rebrand — both legacy "Oakland Athletics" and current
# nickname-only "Athletics" map to the canonical "Athletics" we store.
_add('Athletics', 'oakland athletics', 'oakland', 'as', "a's", 'oak',
     'sacramento athletics',  # interim relocation branding
     to='Athletics')
_add('Philadelphia Phillies', 'philadelphia', 'phillies', 'phi',
     to='Philadelphia Phillies')
_add('Pittsburgh Pirates', 'pittsburgh', 'pirates', 'pit',
     to='Pittsburgh Pirates')
_add('San Diego Padres', 'san diego', 'padres', 'sd padres', 'sd',
     to='San Diego Padres')
_add('San Francisco Giants', 'san francisco', 'giants', 'sf giants', 'sf',
     to='San Francisco Giants')
_add('Seattle Mariners', 'seattle', 'mariners', 'sea',
     to='Seattle Mariners')
# St. Louis vs St Louis vs Saint Louis — all collapse to the canonical with
# a period (which is how the DB stores it).
_add('St. Louis Cardinals', 'st louis cardinals', 'saint louis cardinals',
     'cardinals', 'cards', 'st louis', 'stl', 'st. louis',
     to='St. Louis Cardinals')
_add('Tampa Bay Rays', 'tampa bay', 'rays', 'tampa', 'tb', 'tbr',
     to='Tampa Bay Rays')
_add('Texas Rangers', 'texas', 'rangers', 'tex',
     to='Texas Rangers')
_add('Toronto Blue Jays', 'toronto', 'blue jays', 'bluejays', 'jays', 'tor',
     to='Toronto Blue Jays')
_add('Washington Nationals', 'washington', 'nationals', 'nats', 'was',
     to='Washington Nationals')


# --- Public API ------------------------------------------------------------

def normalize_mlb_team_name(name: Optional[str]) -> str:
    """Stage 1: exact alias / canonical lookup.

    Returns the canonical full name when a match is found. Otherwise
    returns the input trimmed (defensive — the caller's DB lookup will
    then either succeed via iexact or fall through to fuzzy_match).
    """
    if not name:
        return name or ''
    key = _normalize_key(name)
    return MLB_TEAM_ALIASES.get(key, name.strip())


# Set of nicknames computed once at import time. Used for the "shared
# nickname" branch of fuzzy matching (the discriminating signal for most
# MLB names — "Yankees" alone disambiguates 1-of-30).
_NICKNAME_INDEX = {}
for canonical in CANONICAL_MLB_TEAMS:
    parts = canonical.split()
    nickname = parts[-1].lower()  # last word is almost always the nickname
    # Edge cases: "Red Sox", "White Sox", "Blue Jays" — last TWO words.
    if canonical in ('Boston Red Sox', 'Chicago White Sox', 'Toronto Blue Jays'):
        nickname = ' '.join(parts[-2:]).lower()
    _NICKNAME_INDEX.setdefault(nickname, canonical)


def fuzzy_match_to_canonical(name: Optional[str]) -> Optional[str]:
    """Stage 2: fuzzy fallback when neither alias lookup nor DB match worked.

    Strategy (return the first hit, in this order):
      1. Substring match — input contains a canonical's nickname,
         e.g. "ATH (Athletics)" contains "Athletics".
      2. Reverse substring — a canonical contains the input,
         e.g. input "Yankees" found inside "New York Yankees".
      3. Two-word nickname catch — "Red Sox" / "White Sox" / "Blue Jays"
         require checking the last two words.

    Returns the canonical full name on a hit; None on no match. Logs a
    debug line on every successful fuzzy match so we can build out the
    alias dict from real-world API outputs.
    """
    if not name:
        return None
    key = _normalize_key(name)
    if not key:
        return None

    # Strategy 1 + 3: input contains a known nickname.
    for nickname, canonical in _NICKNAME_INDEX.items():
        if nickname in key:
            logger.info(
                'mlb_alias_fuzzy_match strategy=nickname_in_input '
                'input=%r matched=%r', name, canonical,
            )
            return canonical

    # Strategy 2: input IS a substring of a canonical (rare but happens
    # when API trims to nickname-only and the DB stores full name).
    for canonical in CANONICAL_MLB_TEAMS:
        if key in canonical.lower() and len(key) >= 4:
            logger.info(
                'mlb_alias_fuzzy_match strategy=input_substring_of_canonical '
                'input=%r matched=%r', name, canonical,
            )
            return canonical

    return None
