"""Command Center snapshot computation — every dial / card on the dashboard
materialises through this module. Single entry point: build_snapshot().

Design choices:
  - All calculations done in Python on bounded recent windows (last 24h
    for API, last 7 days for cron). The tables are append-only and
    indexed on -timestamp so this is cheap.
  - We never raise from this layer; an empty DB returns a snapshot with
    zero counts and `health='unknown'` so the dashboard renders a
    "No run history captured yet" state instead of a crash.
  - Health categories are explicitly tri-state — green / yellow / red /
    unknown — because the dashboard CSS keys colors off them.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from django.db.models import Avg, Count, Max, Q
from django.utils import timezone


# --- Health classification thresholds ----------------------------------------
# Tuned to be conservative: a single 4xx in the last hour drops API to red,
# because the symptom we are explicitly building for is "401 all day."
API_GREEN_FAILS_24H = 0     # any failure in 24h drops out of green
API_YELLOW_FAILS_1H = 0     # any failure in last hour → red

CRON_GREEN_FAILS_7D = 0     # any failure in 7d drops out of green
CRON_YELLOW_FAILS_24H = 0   # any failure in 24h → red
STUCK_RUNNING_MINUTES = 10  # a 'running' row older than this is "stuck"

QUOTA_RED_PCT = 90          # >=90% used → red
QUOTA_YELLOW_PCT = 70       # 70-90% used → yellow


# --- Snapshot dataclasses ----------------------------------------------------

@dataclass
class HealthCard:
    """Generic colored health card. health ∈ {green,yellow,red,unknown}."""
    health: str
    label: str
    detail: str = ''


@dataclass
class ApiUsageStats:
    last_24h: int = 0
    last_24h_failures: int = 0
    last_1h: int = 0
    last_1h_failures: int = 0
    last_7d: int = 0
    last_call_at: Optional['timezone.datetime'] = None
    last_status: Optional[int] = None
    last_success: Optional[bool] = None
    avg_latency_ms_24h: Optional[int] = None
    by_sport_24h: dict = field(default_factory=dict)
    last_failure_status: Optional[int] = None
    last_failure_at: Optional['timezone.datetime'] = None


@dataclass
class QuotaStats:
    used: Optional[int] = None
    remaining: Optional[int] = None
    pct_used: Optional[float] = None
    health: str = 'unknown'
    captured_at: Optional['timezone.datetime'] = None


@dataclass
class CronCommandStats:
    command: str
    last_run_at: Optional['timezone.datetime'] = None
    last_status: str = 'unknown'
    last_duration_seconds: Optional[float] = None
    last_summary: str = ''
    success_count_7d: int = 0
    failure_count_7d: int = 0
    is_running: bool = False
    is_stuck: bool = False


@dataclass
class CommandCenterSnapshot:
    overall: HealthCard
    api: HealthCard
    api_stats: ApiUsageStats
    quota: QuotaStats
    cron: HealthCard
    cron_commands: List[CronCommandStats] = field(default_factory=list)
    recent_failures: list = field(default_factory=list)
    recent_runs: list = field(default_factory=list)


# --- Public entry point ------------------------------------------------------

def build_snapshot() -> CommandCenterSnapshot:
    """Compose everything the /ops/command-center/ page needs in one shot."""
    api_stats = _api_usage_stats()
    quota = _quota_stats()
    cron_commands = _cron_command_stats()
    recent_failures = _recent_failures(limit=10)
    recent_runs = _recent_runs(limit=15)

    api_card = _classify_api_health(api_stats)
    cron_card = _classify_cron_health(cron_commands)
    overall_card = _classify_overall(api_card, cron_card, quota)

    return CommandCenterSnapshot(
        overall=overall_card,
        api=api_card,
        api_stats=api_stats,
        quota=quota,
        cron=cron_card,
        cron_commands=cron_commands,
        recent_failures=recent_failures,
        recent_runs=recent_runs,
    )


# --- API metrics -------------------------------------------------------------

def _api_usage_stats() -> ApiUsageStats:
    from apps.ops.models import OddsApiUsage

    now = timezone.now()
    cutoff_1h = now - timedelta(hours=1)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    qs_24h = OddsApiUsage.objects.filter(timestamp__gte=cutoff_24h)
    qs_1h = OddsApiUsage.objects.filter(timestamp__gte=cutoff_1h)

    stats = ApiUsageStats()
    stats.last_24h = qs_24h.count()
    stats.last_24h_failures = qs_24h.filter(success=False).count()
    stats.last_1h = qs_1h.count()
    stats.last_1h_failures = qs_1h.filter(success=False).count()
    stats.last_7d = OddsApiUsage.objects.filter(timestamp__gte=cutoff_7d).count()

    last = OddsApiUsage.objects.order_by('-timestamp').first()
    if last:
        stats.last_call_at = last.timestamp
        stats.last_status = last.status_code
        stats.last_success = last.success

    avg = qs_24h.aggregate(avg=Avg('response_time_ms'))['avg']
    if avg is not None:
        stats.avg_latency_ms_24h = int(round(avg))

    sport_rows = qs_24h.values('sport').annotate(n=Count('id'))
    stats.by_sport_24h = {r['sport']: r['n'] for r in sport_rows}

    last_fail = OddsApiUsage.objects.filter(success=False).order_by('-timestamp').first()
    if last_fail:
        stats.last_failure_at = last_fail.timestamp
        stats.last_failure_status = last_fail.status_code

    return stats


def _classify_api_health(stats: ApiUsageStats) -> HealthCard:
    if stats.last_24h == 0:
        return HealthCard(
            health='unknown',
            label='No API activity yet',
            detail='No outbound Odds API calls captured. Check that ingestion has run since this build.',
        )
    if stats.last_1h_failures > 0:
        return HealthCard(
            health='red',
            label=f'{stats.last_1h_failures} failure(s) in last hour',
            detail=(
                f'Most recent failure: HTTP {stats.last_failure_status or "?"}'
                f' at {_fmt(stats.last_failure_at)}'
            ),
        )
    if stats.last_24h_failures > 0:
        return HealthCard(
            health='yellow',
            label=f'{stats.last_24h_failures} failure(s) in last 24h',
            detail=(
                f'No failures in the last hour. Most recent: '
                f'HTTP {stats.last_failure_status or "?"} at {_fmt(stats.last_failure_at)}'
            ),
        )
    return HealthCard(
        health='green',
        label='All Odds API calls succeeding',
        detail=f'{stats.last_24h} calls in 24h · avg {stats.avg_latency_ms_24h or "—"}ms',
    )


# --- Quota -------------------------------------------------------------------

def _quota_stats() -> QuotaStats:
    """Read most recent row that captured x-requests-used / -remaining headers.

    The Odds API only ships those headers on successful responses, so a 401-
    storm produces no quota visibility. That's accurate to reality — if
    health is bad we'd rather say `unknown` than guess.
    """
    from apps.ops.models import OddsApiUsage

    row = (
        OddsApiUsage.objects
        .filter(credits_used__isnull=False, credits_remaining__isnull=False)
        .order_by('-timestamp')
        .first()
    )
    if not row:
        return QuotaStats(health='unknown')

    used = row.credits_used or 0
    remaining = row.credits_remaining or 0
    total = used + remaining if (used + remaining) > 0 else None
    pct = (100.0 * used / total) if total else None

    if pct is None:
        health = 'unknown'
    elif pct >= QUOTA_RED_PCT:
        health = 'red'
    elif pct >= QUOTA_YELLOW_PCT:
        health = 'yellow'
    else:
        health = 'green'

    return QuotaStats(
        used=used,
        remaining=remaining,
        pct_used=pct,
        health=health,
        captured_at=row.timestamp,
    )


# --- Cron --------------------------------------------------------------------

# Commands surfaced as first-class on the dashboard. Anything else still gets
# logged but only shows up in the recent-runs table at the bottom.
TRACKED_COMMANDS = ['refresh_data', 'refresh_scores_and_settle']


def _cron_command_stats() -> List[CronCommandStats]:
    from apps.ops.models import CronRunLog

    now = timezone.now()
    cutoff_7d = now - timedelta(days=7)
    stuck_cutoff = now - timedelta(minutes=STUCK_RUNNING_MINUTES)

    out = []
    for cmd in TRACKED_COMMANDS:
        cmd_qs = CronRunLog.objects.filter(command=cmd)
        last = cmd_qs.order_by('-started_at').first()

        recent = cmd_qs.filter(started_at__gte=cutoff_7d)
        success_n = recent.filter(status='success').count()
        failure_n = recent.filter(status__in=['failure', 'partial']).count()

        is_running = False
        is_stuck = False
        if last and last.status == 'running':
            is_running = True
            if last.started_at < stuck_cutoff:
                is_stuck = True

        out.append(CronCommandStats(
            command=cmd,
            last_run_at=last.started_at if last else None,
            last_status=last.status if last else 'unknown',
            last_duration_seconds=last.duration_seconds if last else None,
            last_summary=(last.summary or '') if last else '',
            success_count_7d=success_n,
            failure_count_7d=failure_n,
            is_running=is_running,
            is_stuck=is_stuck,
        ))
    return out


def _classify_cron_health(commands: List[CronCommandStats]) -> HealthCard:
    if all(c.last_run_at is None for c in commands):
        return HealthCard(
            health='unknown',
            label='No cron run history captured yet',
            detail='Run history starts once the next scheduled cron fires or you trigger a manual run.',
        )
    if any(c.is_stuck for c in commands):
        stuck = [c.command for c in commands if c.is_stuck]
        return HealthCard(
            health='red',
            label=f'Cron stuck running: {", ".join(stuck)}',
            detail=f'Row in `running` state for >{STUCK_RUNNING_MINUTES} min. Worker likely crashed mid-run.',
        )
    if any(c.last_status == 'failure' for c in commands):
        failed = [c.command for c in commands if c.last_status == 'failure']
        return HealthCard(
            health='red',
            label=f'Last run failed: {", ".join(failed)}',
            detail='See Recent Failures below for the traceback.',
        )
    if any(c.last_status == 'partial' for c in commands):
        return HealthCard(
            health='yellow',
            label='Last run partial — some sub-tasks failed',
            detail='Run completed but one or more sports/providers errored.',
        )
    if any(c.failure_count_7d > 0 for c in commands):
        return HealthCard(
            health='yellow',
            label='Recent failures in 7-day window',
            detail='Latest run is healthy but earlier runs failed. Investigate before next deploy.',
        )
    return HealthCard(
        health='green',
        label='All cron jobs healthy',
        detail=f'{sum(c.success_count_7d for c in commands)} successful runs in last 7 days',
    )


# --- Recent activity ---------------------------------------------------------

def _recent_failures(limit=10):
    """Combine API + cron failures into a single timeline, newest first."""
    from apps.ops.models import CronRunLog, OddsApiUsage

    api_fails = list(
        OddsApiUsage.objects
        .filter(success=False)
        .order_by('-timestamp')[:limit]
        .values('id', 'timestamp', 'sport', 'endpoint', 'status_code', 'error_message')
    )
    for r in api_fails:
        r['kind'] = 'api'
        r['when'] = r['timestamp']
        r['title'] = f'Odds API {r["status_code"] or "ERR"} · {r["sport"]}'
        r['detail'] = (r['error_message'] or '')[:200]

    cron_fails = list(
        CronRunLog.objects
        .filter(status__in=['failure', 'partial'])
        .order_by('-started_at')[:limit]
        .values('id', 'started_at', 'command', 'status', 'error_message', 'summary')
    )
    for r in cron_fails:
        r['kind'] = 'cron'
        r['when'] = r['started_at']
        r['title'] = f'Cron {r["status"]} · {r["command"]}'
        r['detail'] = (r['summary'] or r['error_message'] or '')[:200]

    combined = api_fails + cron_fails
    combined.sort(key=lambda r: r['when'], reverse=True)
    return combined[:limit]


def _recent_runs(limit=15):
    from apps.ops.models import CronRunLog
    rows = (
        CronRunLog.objects
        .order_by('-started_at')[:limit]
        .values(
            'id', 'command', 'trigger', 'started_at', 'completed_at',
            'status', 'duration_seconds', 'summary', 'stdout_tail',
        )
    )
    return list(rows)


# --- Overall -----------------------------------------------------------------

def _classify_overall(api: HealthCard, cron: HealthCard, quota: QuotaStats) -> HealthCard:
    """Aggregate worst-of with explicit precedence: red > yellow > unknown > green.

    Rationale: the home banner has to encode "should I be paged right now" at
    a glance, and red on any one panel is enough to warrant attention.
    """
    statuses = [api.health, cron.health, quota.health]
    if 'red' in statuses:
        return HealthCard(
            health='red',
            label='System needs attention',
            detail='One or more components are failing. Review the panels below.',
        )
    if 'yellow' in statuses:
        return HealthCard(
            health='yellow',
            label='Operational with recent warnings',
            detail='No active failures, but recent issues are worth a look.',
        )
    if all(s == 'unknown' for s in statuses):
        return HealthCard(
            health='unknown',
            label='No history captured yet',
            detail='The system is online; metrics will populate as ingestion runs.',
        )
    return HealthCard(
        health='green',
        label='All systems operational',
        detail='APIs healthy, cron jobs succeeding, quota in safe band.',
    )


# --- Helpers -----------------------------------------------------------------

def _fmt(dt):
    if not dt:
        return '—'
    return dt.strftime('%Y-%m-%d %H:%M:%S %Z') if dt.tzinfo else dt.strftime('%Y-%m-%d %H:%M:%S')
