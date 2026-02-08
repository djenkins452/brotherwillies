"""Team and player name normalization utilities."""

import re

# Common aliases: API name -> canonical name used in our DB
# Extend these as new mismatches are discovered during ingestion.
TEAM_ALIASES = {
    # CBB / CFB shared
    'uconn': 'Connecticut',
    'connecticut huskies': 'Connecticut',
    'smu': 'SMU',
    'southern methodist': 'SMU',
    'southern methodist mustangs': 'SMU',
    'lsu': 'LSU',
    'louisiana state': 'LSU',
    'louisiana state tigers': 'LSU',
    'ole miss': 'Ole Miss',
    'mississippi rebels': 'Ole Miss',
    'pitt': 'Pittsburgh',
    'pittsburgh panthers': 'Pittsburgh',
    'ucf': 'UCF',
    'central florida': 'UCF',
    'central florida knights': 'UCF',
    'usc': 'USC',
    'southern california': 'USC',
    'southern california trojans': 'USC',
    'miami (fl)': 'Miami',
    'miami hurricanes': 'Miami',
    'miami (oh)': 'Miami (OH)',
    'nc state': 'NC State',
    'north carolina state': 'NC State',
    'north carolina state wolfpack': 'NC State',
    'tcu': 'TCU',
    'texas christian': 'TCU',
    'texas christian horned frogs': 'TCU',
    'byu': 'BYU',
    'brigham young': 'BYU',
    'brigham young cougars': 'BYU',
    'vt': 'Virginia Tech',
    'virginia tech hokies': 'Virginia Tech',
    "hawai'i": "Hawai'i",
    'hawaii': "Hawai'i",
    'umass': 'UMass',
    'massachusetts': 'UMass',
    'utsa': 'UTSA',
    'ut san antonio': 'UTSA',
}

# Common mascot suffixes to strip for matching
_MASCOT_PATTERN = re.compile(
    r'\s+(Crimson Tide|Bulldogs|Tigers|Wildcats|Jayhawks|Sooners|Longhorns|'
    r'Seminoles|Hurricanes|Cavaliers|Tar Heels|Blue Devils|Cardinals|'
    r'Wolverines|Buckeyes|Spartans|Hawkeyes|Hoosiers|Boilermakers|'
    r'Volunteers|Razorbacks|Commodores|Rebels|Gamecocks|Gators|'
    r'Aggies|Bears|Horned Frogs|Red Raiders|Mountaineers|Cyclones|'
    r'Cougars|Knights|Mustangs|Huskies|Ducks|Beavers|Sun Devils|'
    r'Buffaloes|Utes|Golden Bears|Cardinal|Bruins|Trojans|Panthers|'
    r'Wolfpack|Demon Deacons|Yellow Jackets|Fighting Irish|Hokies|'
    r'Orange|Terrapins|Nittany Lions|Badgers|Cornhuskers|Golden Gophers|'
    r'Illini|Scarlet Knights|Owls|Bearcats|Musketeers|Bluejays|'
    r'Friars|Pirates|Hoyas|Red Storm|Peacocks|Gaels)$',
    re.IGNORECASE,
)


def normalize_team_name(name):
    """Normalize an API team name to match our DB records.

    1. Check alias table (case-insensitive)
    2. Strip common mascot suffixes
    3. Return cleaned name
    """
    if not name:
        return name

    name = name.strip()

    # Check alias table
    alias_key = name.lower()
    if alias_key in TEAM_ALIASES:
        return TEAM_ALIASES[alias_key]

    # Strip mascot suffix
    cleaned = _MASCOT_PATTERN.sub('', name).strip()
    if cleaned:
        return cleaned

    return name


def normalize_golfer_name(name):
    """Normalize golfer name: strip whitespace, title-case."""
    if not name:
        return name
    return ' '.join(name.strip().split())
