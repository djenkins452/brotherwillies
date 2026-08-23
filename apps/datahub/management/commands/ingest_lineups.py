"""v3.4 SHADOW — pregame lineup ingestion.

Polls the MLB Stats API `/schedule?hydrate=lineups` for games starting
in a rolling window and writes append-only `ConfirmedLineup` rows.

Ingestion contract:
  * NEVER overwrite an existing row's `observed_at` — every observation
    that differs from the last stored one for (game, team) writes a
    NEW row.
  * If the observed lineup is identical to the last stored one for the
    same (game, team), skip (nothing changed).
  * `lineup_state` per row:
      confirmed                       — first non-empty observation
                                        AND observed_at < game.first_pitch
      updated_after_confirmation      — subsequent differing observation
                                        (regardless of pre/post first pitch)
      post_first_pitch                — first observation is at/after
                                        first_pitch (cannot be treated
                                        as legitimate pregame knowledge)

Cadence: designed to be invoked every 10-15 minutes from Railway cron.
One `/schedule?hydrate=lineups` call per invocation covers every game
scheduled today — cost ~1 request per poll (very cheap; ~100/day at
15-min cadence for the entire MLB slate).

Wrapped in cron_run_log so it appears alongside other cron jobs.

SHADOW-ONLY: writes only to `mlb.ConfirmedLineup`. Cannot touch
production recommendations. `USE_LINEUP_QUALITY` remains false
regardless of what this command does.
"""
import logging
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.datahub.providers.mlb.statsapi_client import (
    StatsApiError, fetch_json,
)
from apps.ops.services.cron_logging import cron_run_log


logger = logging.getLogger(__name__)


def _fingerprint(players_list) -> tuple:
    """Deterministic hash of a lineup for equality comparison. A tuple
    of (order, player_id) pairs — order-sensitive so a reshuffled
    lineup counts as a change even if the same 9 players play."""
    return tuple(
        (int(p.get('order') or 0), int(p.get('player_id') or 0))
        for p in players_list
    )


def _normalize_players(raw_players) -> list:
    """MLB Stats API returns a list of player dicts under
    `lineups.homePlayers` / `lineups.awayPlayers`. Order in the list
    IS the batting order (1-indexed after this normalization).

    We keep the minimal fields needed for downstream lineup-quality
    scoring: order, player_id, name, position."""
    out = []
    for i, p in enumerate(raw_players or [], 1):
        if not isinstance(p, dict) or not p.get('id'):
            continue
        pos = (p.get('primaryPosition') or {}).get('abbreviation', '')
        out.append({
            'order': i,
            'player_id': p['id'],
            'name': p.get('fullName', ''),
            'position': pos,
        })
    return out


def _last_stored_lineup(game, team):
    """Latest ConfirmedLineup row for this pair (any state)."""
    from apps.mlb.models import ConfirmedLineup
    return (
        ConfirmedLineup.objects
        .filter(game=game, team=team)
        .order_by('-observed_at')
        .first()
    )


def _classify_state(game_first_pitch, observed_at, prior_confirmed_exists: bool) -> str:
    """Decide the row's lineup_state given timing + prior state."""
    if prior_confirmed_exists:
        return 'updated_after_confirmation'
    if game_first_pitch is None or observed_at < game_first_pitch:
        return 'confirmed'
    return 'post_first_pitch'


class Command(BaseCommand):
    help = (
        'v3.4 SHADOW: poll MLB Stats API for pregame lineups and write '
        'append-only ConfirmedLineup rows. Safe to invoke every 10-15 min. '
        'Shadow-only — never affects production recommendations.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--lookback-hours', type=int, default=1,
            help=(
                'Also include games whose first_pitch was up to N hours '
                'in the past (default 1) so post_first_pitch observations '
                'are still captured for coverage analysis.'
            ),
        )
        parser.add_argument(
            '--lookahead-hours', type=int, default=8,
            help=(
                'Include games whose first_pitch is up to N hours in the '
                'future (default 8). MLB lineups typically post 2-3h '
                'pregame; 8h gives comfortable headroom.'
            ),
        )
        parser.add_argument(
            '--trigger', choices=['cron', 'manual', 'deploy'], default='cron',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
        )

    def handle(self, *args, **options):
        with cron_run_log('ingest_lineups', trigger=options.get('trigger', 'cron')) as log:
            summary = self._ingest(options)
            log.summary = summary
            self.stdout.write(summary)

    def _ingest(self, options) -> str:
        from apps.mlb.models import ConfirmedLineup, Game

        now = timezone.now()
        window_start = now - timedelta(hours=options['lookback_hours'])
        window_end = now + timedelta(hours=options['lookahead_hours'])

        # Pull today's + tomorrow's schedule (some late-night games span
        # date boundaries in UTC). fetch_schedule chunks internally so a
        # 2-day window is one API request in most cases.
        try:
            games_payload = fetch_json(
                '/v1/schedule',
                params={
                    'sportId': 1,
                    'startDate': window_start.date().isoformat(),
                    'endDate': window_end.date().isoformat(),
                    'hydrate': 'probablePitcher,lineups',
                },
            )
        except StatsApiError as e:
            logger.exception('ingest_lineups: schedule fetch failed')
            return f'FAILED schedule fetch: {e.human_summary()}'

        candidate_games = []
        for dblk in games_payload.get('dates', []) or []:
            for g in dblk.get('games', []) or []:
                candidate_games.append(g)

        created = 0
        skipped_unchanged = 0
        skipped_empty = 0
        skipped_no_local_game = 0
        candidates_in_window = 0

        for g in candidate_games:
            gpk = g.get('gamePk')
            if not gpk:
                continue

            # Only games within the [-lookback, +lookahead] window
            # around now — outside that, either too early to have
            # lineups or too old to care.
            game_start_iso = g.get('gameDate')
            if game_start_iso:
                try:
                    game_start = datetime.fromisoformat(
                        game_start_iso.replace('Z', '+00:00'),
                    )
                except (TypeError, ValueError):
                    game_start = None
            else:
                game_start = None
            if game_start is None or not (
                window_start <= game_start <= window_end
            ):
                continue
            candidates_in_window += 1

            # Match against our local Game row (source='mlb_stats_api',
            # external_id=str(gamePk)). Games not in our slate are
            # ignored — same behavior as reliever appearance ingestion.
            local_game = Game.objects.filter(
                source='mlb_stats_api', external_id=str(gpk),
            ).select_related('home_team', 'away_team').first()
            if local_game is None:
                skipped_no_local_game += 1
                continue

            lineups = g.get('lineups') or {}
            home_players = _normalize_players(lineups.get('homePlayers'))
            away_players = _normalize_players(lineups.get('awayPlayers'))

            for side, team, players in (
                ('home', local_game.home_team, home_players),
                ('away', local_game.away_team, away_players),
            ):
                if not players:
                    skipped_empty += 1
                    continue

                fingerprint = _fingerprint(players)
                prior = _last_stored_lineup(local_game, team)
                if prior is not None and _fingerprint(prior.players) == fingerprint:
                    # Identical to the last observation — nothing changed;
                    # do NOT write a new row.
                    skipped_unchanged += 1
                    continue

                # State decision — is this the first EVER row for this
                # (game, team) pair, or an update after a prior confirmation?
                prior_confirmed_exists = (
                    ConfirmedLineup.objects
                    .filter(
                        game=local_game, team=team,
                        lineup_state__in=('confirmed', 'updated_after_confirmation'),
                    )
                    .exists()
                )
                state = _classify_state(
                    game_first_pitch=local_game.first_pitch,
                    observed_at=now,
                    prior_confirmed_exists=prior_confirmed_exists,
                )
                if options.get('dry_run'):
                    created += 1
                    continue
                ConfirmedLineup.objects.create(
                    game=local_game, team=team,
                    observed_at=now,
                    players=players,
                    lineup_state=state,
                    source='mlb_stats_api',
                    data_confidence='high',
                    raw_snapshot={'lineups_side': side},
                )
                created += 1

        return (
            f'ingest_lineups: candidates_in_window={candidates_in_window} '
            f'created={created} skipped_unchanged={skipped_unchanged} '
            f'skipped_empty={skipped_empty} '
            f'skipped_no_local_game={skipped_no_local_game}'
        )
