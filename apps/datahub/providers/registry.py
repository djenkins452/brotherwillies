from apps.datahub.providers.cbb.schedule_provider import CBBScheduleProvider
from apps.datahub.providers.cbb.odds_provider import CBBOddsProvider
from apps.datahub.providers.cbb.injuries_provider import CBBInjuriesProvider
from apps.datahub.providers.cfb.schedule_provider import CFBScheduleProvider
from apps.datahub.providers.cfb.odds_provider import CFBOddsProvider
from apps.datahub.providers.cfb.injuries_provider import CFBInjuriesProvider
from apps.datahub.providers.golf.schedule_provider import GolfScheduleProvider
from apps.datahub.providers.golf.odds_provider import GolfOddsProvider

_PROVIDERS = {
    ('cbb', 'schedule'): CBBScheduleProvider,
    ('cbb', 'odds'): CBBOddsProvider,
    ('cbb', 'injuries'): CBBInjuriesProvider,
    ('cfb', 'schedule'): CFBScheduleProvider,
    ('cfb', 'odds'): CFBOddsProvider,
    ('cfb', 'injuries'): CFBInjuriesProvider,
    ('golf', 'schedule'): GolfScheduleProvider,
    ('golf', 'odds'): GolfOddsProvider,
}


def get_provider(sport, data_type):
    """Look up and instantiate a provider by sport and data type."""
    key = (sport, data_type)
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ValueError(f"No provider registered for {sport}/{data_type}")
    return cls()
