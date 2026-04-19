"""College Baseball Schedule Provider — fetches games from ESPN public endpoints.

ESPN's site.api.espn.com exposes a college-baseball scoreboard that
includes schedule, status, and scores for D1. It does not expose
probable pitchers — pitchers remain TBD in our schema until a
richer source is wired (D1Baseball, NCAA, etc.).

Full D1 coverage is achieved by passing `groups=50` (D1 group code)
and a high `limit`; ESPN returns all D1 games for the requested date.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from apps.college_baseball.models import Conference, Game, Team
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.team_colors import get_team_color

logger = logging.getLogger(__name__)

SOURCE = 'espn'

# ESPN competition status -> our status
STATUS_MAP = {
    'pre': 'scheduled',
    'in': 'live',
    'post': 'final',
}


def _upsert_conference(name):
    if not name:
        name = 'Independent'
    slug = slugify(name) or 'independent'
    conference, _ = Conference.objects.get_or_create(
        slug=slug, defaults={'name': name},
    )
    return conference


def _upsert_team(team_payload, conference):
    """team_payload is ESPN competitor.team dict."""
    ext_id = str(team_payload.get('id', '') or '')
    name = (
        team_payload.get('displayName')
        or team_payload.get('name')
        or team_payload.get('location')
        or ''
    )
    abbr = team_payload.get('abbreviation', '') or ''
    if not ext_id or not name:
        return None

    slug = slugify(name)
    color = get_team_color(slug, 'college_baseball') or ''
    team, _ = Team.objects.update_or_create(
        source=SOURCE, external_id=ext_id,
        defaults={
            'name': name,
            'slug': slug,
            'conference': conference,
            'abbreviation': abbr,
            'primary_color': color,
        },
    )
    return team


class CollegeBaseballScheduleProvider(AbstractProvider):
    sport = 'college_baseball'
    data_type = 'schedule'

    def __init__(self):
        self.client = APIClient(
            base_url=settings.ESPN_BASEBALL_BASE_URL,
            rate_limit_delay=0.4,
        )

    def fetch(self):
        now = timezone.now()
        all_events = []
        # Previous day (catches late-night finals) + today + next 7 days
        for offset in range(-1, 8):
            date = now + timedelta(days=offset)
            date_str = date.strftime('%Y%m%d')
            try:
                data = self.client.get(
                    '/college-baseball/scoreboard',
                    params={'dates': date_str, 'groups': '50', 'limit': 300},
                )
                events = data.get('events', []) or []
                all_events.extend(events)
                logger.info(f"ESPN college_baseball {date_str}: {len(events)} events")
            except Exception as e:
                logger.warning(f"ESPN college_baseball {date_str} failed: {e}")
        return all_events

    def normalize(self, raw):
        normalized = []
        for event in raw or []:
            competitions = event.get('competitions') or []
            if not competitions:
                continue
            comp = competitions[0]

            home_payload = away_payload = None
            home_conf = away_conf = ''
            home_score = away_score = None
            for competitor in comp.get('competitors') or []:
                team = competitor.get('team') or {}
                score_raw = competitor.get('score')
                score = int(score_raw) if score_raw and str(score_raw).isdigit() else None
                groups = team.get('groups') or {}
                conf_name = ''
                if isinstance(groups, dict):
                    conf_name = groups.get('shortName') or groups.get('name') or ''
                if competitor.get('homeAway') == 'home':
                    home_payload = team
                    home_score = score
                    home_conf = conf_name
                else:
                    away_payload = team
                    away_score = score
                    away_conf = conf_name

            if not home_payload or not away_payload:
                continue

            start_date = event.get('date', '')
            if not start_date:
                continue

            status_state = (event.get('status') or {}).get('type', {}).get('state', '')
            status = STATUS_MAP.get(status_state, 'scheduled')

            neutral = bool(comp.get('neutralSite'))

            normalized.append({
                'external_id': str(event.get('id') or ''),
                'start_date': start_date,
                'status': status,
                'neutral_site': neutral,
                'home_payload': home_payload,
                'away_payload': away_payload,
                'home_conf': home_conf,
                'away_conf': away_conf,
                'home_score': home_score,
                'away_score': away_score,
            })
        return normalized

    def persist(self, normalized):
        created = updated = skipped = 0
        for item in normalized:
            if not item['external_id']:
                skipped += 1
                continue

            home_conf = _upsert_conference(item['home_conf'])
            away_conf = _upsert_conference(item['away_conf'])
            home = _upsert_team(item['home_payload'], home_conf)
            away = _upsert_team(item['away_payload'], away_conf)
            if not home or not away:
                skipped += 1
                continue

            first_pitch = parse_datetime(item['start_date'])
            if first_pitch is None:
                skipped += 1
                continue
            if timezone.is_naive(first_pitch):
                first_pitch = timezone.make_aware(first_pitch)

            defaults = {
                'home_team': home,
                'away_team': away,
                'first_pitch': first_pitch,
                'status': item['status'],
                'neutral_site': item['neutral_site'],
                'home_score': item.get('home_score'),
                'away_score': item.get('away_score'),
            }
            _, was_created = Game.objects.update_or_create(
                source=SOURCE, external_id=item['external_id'],
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return {'status': 'ok', 'created': created, 'updated': updated, 'skipped': skipped}
