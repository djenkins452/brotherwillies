"""v3.3 SHADOW — deterministic bullpen state builder.

Consumes `RelieverAppearance` rows (populated from MLB Stats API
boxscores) and produces the bullpen state that would have been known
immediately BEFORE a given `reference_date` T.

DETERMINISM & IDEMPOTENCY

  Same (team, reference_date, appearance set) → identical output.
  Backfill and daily-update code paths share this same function so
  historical reconstruction and forward production computation cannot
  drift.

LEAKAGE

  Query filter is `game__first_pitch__lt=reference_date` — STRICT `<`.
  An appearance whose game started exactly at first_pitch is excluded.
  Locked by test.

METRICS

  Quality:
    * relief ERA        — 9 * ER / IP, rolling 30-day
    * relief WHIP       — (H + BB) / IP, rolling 30-day
    * relief K/9        — 9 * K / IP, rolling 30-day
    * relief BB/9       — 9 * BB / IP, rolling 30-day
    * relief HR/9       — 9 * HR / IP, rolling 30-day  (informational)
    * relief IP last 30d (used for confidence)

  Fatigue:
    * appearances_last_1_day / 2_days / 3_days — team total relief apps
    * top_reliever_available — heuristic: pitcher with the most
      season-to-date (saves + holds) has NOT appeared in the last day
      AND did not throw >= 25 pitches yesterday.

  Data confidence:
    * 'high' when relief IP last 30d >= 25 (roughly a normal month)
    * 'med' when 10 <= IP < 25
    * 'low' otherwise (including empty)

WHY IP-WEIGHTED, NOT SEASON-TO-DATE

  Rolling 30-day windows react to trades, roster changes, and midseason
  bullpen implosion / renaissance. Season-to-date aggregates lag those
  changes badly and would misprice a July snapshot with April data
  still in the average. The bullpen is a leverage feature — you want
  what's happening NOW, not what was happening in spring.

WHY 30 DAYS

  Design doc §4 anchors on 30 days. Sample size — ~120-150 relief IP
  per team over that window in a healthy season, which is enough for
  the ERA/WHIP components to stabilize. The 14-day window from the
  design doc is available too by passing `quality_days=14`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.db.models import Count, Q, Sum


# Data-confidence thresholds — relief IP over the rolling window.
CONFIDENCE_HIGH_IP_MIN = 25.0
CONFIDENCE_MED_IP_MIN = 10.0

# Top-reliever availability heuristic.
# A "top reliever" is the pitcher with the most (saves + holds)
# over `TOP_RELIEVER_WINDOW_DAYS` before the reference date.
# They are considered UNAVAILABLE if they appeared yesterday OR threw
# >= TOP_RELIEVER_HEAVY_PITCHES the day before yesterday.
TOP_RELIEVER_WINDOW_DAYS = 30
TOP_RELIEVER_HEAVY_PITCHES = 25


@dataclass
class BuiltBullpenSnapshot:
    """Values ready to be persisted onto a `TeamBullpenSnapshot` row."""
    bullpen_era: Optional[float]
    bullpen_whip: Optional[float]
    bullpen_k_per_9: Optional[float]
    bullpen_bb_per_9: Optional[float]
    bullpen_ip_last30: Optional[float]
    appearances_last_1_day: int
    appearances_last_2_days: int
    appearances_last_3_days: int
    top_reliever_available: Optional[bool]
    data_confidence: str
    notes: str


def _relief_qs(team, reference_date, *, window_days):
    """Reliever appearances for `team` in [reference_date - window_days,
    reference_date). STRICT `<` for the upper bound (leakage guard)."""
    from apps.mlb.models import RelieverAppearance
    cutoff = reference_date - timedelta(days=window_days)
    return (
        RelieverAppearance.objects
        .filter(
            team=team,
            is_starter=False,
            game__first_pitch__lt=reference_date,
            game__first_pitch__gte=cutoff,
        )
        .select_related('game', 'pitcher')
    )


def _team_appearances_between(team, *, start, end):
    """Reliever appearances for `team` in [start, end). Inclusive of
    start, exclusive of end — matches the leakage semantics."""
    from apps.mlb.models import RelieverAppearance
    return (
        RelieverAppearance.objects
        .filter(
            team=team,
            is_starter=False,
            game__first_pitch__gte=start,
            game__first_pitch__lt=end,
        )
    )


def _aggregate_metrics(appearances):
    """Sum IP/H/ER/BB/K/HR across an appearance queryset."""
    agg = appearances.aggregate(
        outs=Sum('outs_recorded'),
        hits=Sum('hits'),
        er=Sum('earned_runs'),
        bb=Sum('walks'),
        k=Sum('strikeouts'),
        hr=Sum('home_runs'),
        pitches=Sum('pitches'),
    )
    outs = int(agg.get('outs') or 0)
    ip = outs / 3.0
    return {
        'outs': outs,
        'ip': ip,
        'hits': int(agg.get('hits') or 0),
        'er': int(agg.get('er') or 0),
        'bb': int(agg.get('bb') or 0),
        'k': int(agg.get('k') or 0),
        'hr': int(agg.get('hr') or 0),
        'pitches': int(agg.get('pitches') or 0),
    }


def _rate_per_9(count: int, ip: float) -> Optional[float]:
    """Standard baseball rate stat: X per 9 innings.
    Returns None when IP is zero (no data)."""
    if ip <= 0:
        return None
    return round(9.0 * count / ip, 3)


def _classify_confidence(ip_last_30: float) -> str:
    if ip_last_30 >= CONFIDENCE_HIGH_IP_MIN:
        return 'high'
    if ip_last_30 >= CONFIDENCE_MED_IP_MIN:
        return 'med'
    return 'low'


def _top_reliever(team, reference_date):
    """Identify the team's top reliever as of reference_date.

    Definition (simple, historically reconstructable):
      Reliever on this team with the most (saves + holds) over the
      TOP_RELIEVER_WINDOW_DAYS window before reference_date. Ties
      broken by total appearances then pitcher id (deterministic).

    Returns the StartingPitcher instance, or None when the team has
    no reliever activity in the window.
    """
    from apps.mlb.models import RelieverAppearance
    from django.db.models import IntegerField
    from django.db.models.functions import Cast

    cutoff = reference_date - timedelta(days=TOP_RELIEVER_WINDOW_DAYS)
    rows = (
        RelieverAppearance.objects
        .filter(
            team=team,
            is_starter=False,
            game__first_pitch__lt=reference_date,
            game__first_pitch__gte=cutoff,
        )
        .values('pitcher_id')
        .annotate(
            leverage=Sum(Cast('is_save', IntegerField()))
                     + Sum(Cast('is_hold', IntegerField())),
            appearances=Count('id'),
        )
        .order_by('-leverage', '-appearances', 'pitcher_id')
    )
    if not rows:
        return None
    top_id = rows[0]['pitcher_id']
    from apps.mlb.models import StartingPitcher
    try:
        return StartingPitcher.objects.get(pk=top_id)
    except StartingPitcher.DoesNotExist:
        return None


def _top_reliever_available(team, reference_date):
    """Heuristic availability check for the team's top reliever.

    Returns:
      True  — top reliever exists and did not pitch yesterday AND
              did not throw >=25 pitches the day before yesterday.
      False — top reliever exists but recent workload indicates
              unavailability.
      None  — no top reliever identified (no relief activity in
              the lookback window).
    """
    from apps.mlb.models import RelieverAppearance

    top = _top_reliever(team, reference_date)
    if top is None:
        return None

    one_day_ago = reference_date - timedelta(days=1)
    two_days_ago = reference_date - timedelta(days=2)

    # Appeared yesterday? → unavailable.
    if RelieverAppearance.objects.filter(
        pitcher=top,
        game__first_pitch__gte=one_day_ago,
        game__first_pitch__lt=reference_date,
    ).exists():
        return False

    # Threw a heavy day-before-yesterday load? → unavailable.
    heavy = RelieverAppearance.objects.filter(
        pitcher=top,
        game__first_pitch__gte=two_days_ago,
        game__first_pitch__lt=one_day_ago,
        pitches__gte=TOP_RELIEVER_HEAVY_PITCHES,
    ).exists()
    if heavy:
        return False

    return True


def build_snapshot(team, reference_date, *, quality_days: int = 30) -> BuiltBullpenSnapshot:
    """Deterministically produce a bullpen snapshot as of `reference_date`.

    Idempotent: same inputs → same output. Never raises. When there are
    no reliever appearances at all in the lookback window, returns a
    zero-value snapshot with `data_confidence='low'` — same posture as
    `bullpen.team_bullpen_signal` on an empty table.
    """
    if reference_date is None:
        from django.utils import timezone
        reference_date = timezone.now()

    # Quality — rolling window aggregate.
    quality_qs = _relief_qs(team, reference_date, window_days=quality_days)
    q = _aggregate_metrics(quality_qs)

    era = _rate_per_9(q['er'], q['ip'])
    whip = round((q['hits'] + q['bb']) / q['ip'], 3) if q['ip'] > 0 else None
    k9 = _rate_per_9(q['k'], q['ip'])
    bb9 = _rate_per_9(q['bb'], q['ip'])

    # Fatigue — team-level relief appearance counts in the last 1/2/3 days.
    d1 = _team_appearances_between(
        team, start=reference_date - timedelta(days=1), end=reference_date,
    ).count()
    d2 = _team_appearances_between(
        team, start=reference_date - timedelta(days=2), end=reference_date,
    ).count()
    d3 = _team_appearances_between(
        team, start=reference_date - timedelta(days=3), end=reference_date,
    ).count()

    top_available = _top_reliever_available(team, reference_date)

    confidence = _classify_confidence(q['ip'])
    notes = (
        f'quality_window={quality_days}d, '
        f'relief_ip={q["ip"]:.1f}, '
        f'relief_apps={quality_qs.count()}'
    )

    return BuiltBullpenSnapshot(
        bullpen_era=era,
        bullpen_whip=whip,
        bullpen_k_per_9=k9,
        bullpen_bb_per_9=bb9,
        bullpen_ip_last30=round(q['ip'], 2) if q['ip'] > 0 else 0.0,
        appearances_last_1_day=d1,
        appearances_last_2_days=d2,
        appearances_last_3_days=d3,
        top_reliever_available=top_available,
        data_confidence=confidence,
        notes=notes,
    )


def persist_snapshot(team, reference_date, *, quality_days: int = 30, source='mlb_stats_api'):
    """Build + persist a TeamBullpenSnapshot for (team, reference_date).

    Append-only — every call inserts a NEW row. Caller controls idempotency
    by not calling twice for the same (team, reference_date); the
    `backfill_bullpen_snapshots` command handles that guard.
    """
    from apps.mlb.models import TeamBullpenSnapshot
    built = build_snapshot(team, reference_date, quality_days=quality_days)
    return TeamBullpenSnapshot.objects.create(
        team=team,
        as_of=reference_date,
        bullpen_era=built.bullpen_era,
        bullpen_whip=built.bullpen_whip,
        bullpen_k_per_9=built.bullpen_k_per_9,
        bullpen_bb_per_9=built.bullpen_bb_per_9,
        bullpen_ip_last30=built.bullpen_ip_last30,
        appearances_last_1_day=built.appearances_last_1_day,
        appearances_last_2_days=built.appearances_last_2_days,
        appearances_last_3_days=built.appearances_last_3_days,
        top_reliever_available=built.top_reliever_available,
        source=source,
        data_confidence=built.data_confidence,
        notes=built.notes,
    )
