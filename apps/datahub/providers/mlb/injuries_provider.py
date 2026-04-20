"""MLB Injuries Provider — ESPN team-level injury list.

ESPN exposes a clean JSON injury list per team at
    /apis/site/v2/sports/baseball/mlb/teams/{teamExternalId}/injuries
Fields per entry:
    athlete.fullName, athlete.position.abbreviation
    status          — "Day-To-Day", "7-Day IL", "10-Day IL", "15-Day IL", "60-Day IL"
    type.description — e.g. "Elbow"
    details.returnDate — ISO date (may be absent)

We aggregate each team's current injuries into a single InjuryImpact row
per upcoming game within a 7-day window. Impact level is derived from the
*most severe* listed status (adjusted for position — a starting pitcher on
any meaningful IL is high impact). Affected players are enumerated in the
`notes` field so the UI can show *who* is out without extra queries.

Idempotent: `update_or_create` on (game, team) replaces the prior snapshot
each run — we want fresh state, not an audit trail of injury changes.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.mlb.models import Game, InjuryImpact, Team

logger = logging.getLogger(__name__)

UPCOMING_WINDOW_DAYS = 7       # attach injury state to games within this window
TOP_N_NOTES = 5                # include top-N most severe injuries in notes


def _il_length_days(status: str) -> int | None:
    """Normalize ESPN status string → approximate IL length in days.

    Returns None for unrecognized statuses (safer to ignore than
    misclassify). Day-To-Day maps to 1; 7/10/15/60 map literally.
    """
    s = (status or '').lower().strip()
    if 'day-to-day' in s or 'day to day' in s:
        return 1
    if '7-day' in s or '7 day' in s:
        return 7
    if '10-day' in s or '10 day' in s:
        return 10
    if '15-day' in s or '15 day' in s:
        return 15
    if '60-day' in s or '60 day' in s:
        return 60
    return None


def _impact_from_status(status: str, position: str) -> str | None:
    """Map status + position → InjuryImpact.IMPACT_CHOICES value.

    Starting pitchers get a boost: any 10+ day IL is `high` for them,
    since losing an SP materially changes the game-level forecast. For
    other positions, 15-Day or 60-Day → high, 10-Day → med, shorter → low.
    Returns None when status is unrecognized.
    """
    length = _il_length_days(status)
    if length is None:
        return None
    pos = (position or '').upper()
    is_sp = pos in ('SP', 'P')  # some feeds use plain 'P' for starters

    if length >= 60:
        return 'high'
    if length >= 15:
        return 'high'
    if length >= 10:
        return 'high' if is_sp else 'med'
    if length >= 7:
        return 'med' if is_sp else 'low'
    return 'low'  # day-to-day


SEVERITY_RANK = {'high': 3, 'med': 2, 'low': 1}


def _pick_max(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b


class MLBEspnInjuriesProvider(AbstractProvider):
    sport = 'mlb'
    data_type = 'injuries'

    def __init__(self):
        self.client = APIClient(
            base_url=settings.ESPN_BASEBALL_BASE_URL,
            rate_limit_delay=0.5,
        )

    def fetch(self):
        """One request per team. 30 MLB teams + 0.5s rate-limit → ~15s.

        Teams lacking `external_id` are skipped; we rely on the schedule
        provider to have seeded MLB Stats API team IDs which match ESPN's.
        """
        all_raw = []
        teams = list(Team.objects.filter(source='mlb_stats_api').exclude(external_id=''))
        logger.info(f"mlb_injuries_fetch_start teams={len(teams)}")
        for team in teams:
            try:
                data = self.client.get(f'/mlb/teams/{team.external_id}/injuries')
                injuries = (data or {}).get('injuries') or []
                all_raw.append({
                    'team_id': team.id,
                    'team_name': team.name,
                    'injuries': injuries,
                })
            except Exception as e:
                logger.warning(
                    f"mlb_injuries_fetch_team_failed team={team.name} "
                    f"external_id={team.external_id} err={e}"
                )
        return all_raw

    def normalize(self, raw):
        """Per-team: collapse raw ESPN injury dicts into a compact shape.

        Output:
            [{
                'team_id': <db id>,
                'team_name': ...,
                'impact_level': 'high'|'med'|'low'|None,
                'notes': 'SP Spencer Strider (15-Day IL, return May 1)\\n...',
                'player_count': int,
             }, ...]
        """
        out = []
        for team_block in raw or []:
            injuries = team_block.get('injuries') or []
            if not injuries:
                continue
            # Score each injury and keep the worst per team + a notes list.
            scored: list[tuple[int, str]] = []  # (severity_rank, note)
            team_level: str | None = None
            for inj in injuries:
                athlete = inj.get('athlete') or {}
                name = athlete.get('fullName') or 'Unknown'
                position = ((athlete.get('position') or {}).get('abbreviation')) or ''
                status = inj.get('status') or ''
                impact = _impact_from_status(status, position)
                if impact is None:
                    continue
                team_level = _pick_max(team_level, impact)
                return_date = ((inj.get('details') or {}).get('returnDate')) or ''
                note_pieces = [f"{position} {name}".strip(), f"({status}"]
                if return_date:
                    note_pieces[-1] += f", return {return_date}"
                note_pieces[-1] += ')'
                scored.append((SEVERITY_RANK[impact], ' '.join(note_pieces)))
            if team_level is None:
                continue
            scored.sort(key=lambda x: -x[0])
            notes = '\n'.join(n for _, n in scored[:TOP_N_NOTES])
            out.append({
                'team_id': team_block['team_id'],
                'team_name': team_block['team_name'],
                'impact_level': team_level,
                'notes': notes,
                'player_count': len(scored),
            })
        logger.info(f"mlb_injuries_normalize teams_with_injuries={len(out)}")
        return out

    def persist(self, normalized):
        """Attach each team's aggregated injury state to every upcoming
        game (within UPCOMING_WINDOW_DAYS). Idempotent via update_or_create.
        """
        now = timezone.now()
        window_end = now + timedelta(days=UPCOMING_WINDOW_DAYS)
        created = updated = 0

        for item in normalized:
            team_id = item['team_id']
            upcoming = Game.objects.filter(
                first_pitch__gte=now,
                first_pitch__lte=window_end,
            ).filter(
                # home OR away for this team
            )
            # Union of home + away filters:
            home_games = upcoming.filter(home_team_id=team_id)
            away_games = upcoming.filter(away_team_id=team_id)
            for game in list(home_games) + list(away_games):
                _, was_created = InjuryImpact.objects.update_or_create(
                    game=game,
                    team_id=team_id,
                    defaults={
                        'impact_level': item['impact_level'],
                        'notes': item['notes'],
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        logger.info(
            f"mlb_injuries_persist_summary created={created} updated={updated} "
            f"teams={len(normalized)}"
        )
        return {
            'status': 'ok' if (created + updated) > 0 else 'empty',
            'created': created,
            'updated': updated,
        }
