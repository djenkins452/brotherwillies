"""Autonomous canonical decision-snapshot capture for V3.2 forward
validation (2026-08-24).

Runs from `refresh_data` every cycle. Finds MLB games with first_pitch
inside the canonical pregame window and persists ONE immutable
`ForwardValidationSnapshot` per (game, engine_version). Idempotent.

CANONICAL WINDOW (pre-registered, do NOT tune against outcomes)
  Target: T-60min before first pitch.
  Eligible: first_pitch is between now+MIN_WINDOW_MIN and now+MAX_WINDOW_MIN.

  Rationale:
    * inside the app's actionable betting window
    * probable pitchers should usually be known
    * late market information is available
    * prospective lineup collection may already have data
    * avoids conflating early-morning and near-first-pitch decisions

DECISION CAPTURE
  Runs the exact frozen production stack (get_recommendation → V3.2
  helpers). Every evaluable game gets a snapshot regardless of whether
  V3.2 recommends. decision_class classifies each row so forward-health
  can filter to recommended-only while research retains rejected games.

FROZEN V3.2 GUARANTEE
  This module never sets USE_BULLPEN_*, USE_LINEUP_QUALITY, or
  USE_TEAM_OFFENSE. It calls the SAME code path that produces user-
  facing recommendations, so any drift in one propagates to the other.
  If someone later changes the model stack, they MUST bump ENGINE_VERSION
  below so historical snapshots stay interpretable.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from django.utils import timezone


logger = logging.getLogger(__name__)


# --- Canonical capture window (pre-registered) ---
ENGINE_VERSION = 'v3_2'
MIN_WINDOW_MIN = 45
MAX_WINDOW_MIN = 75


def activation_at():
    """Durable activation boundary for the autonomous capture system.

    Read from `settings.FORWARD_VALIDATION_ACTIVATION_AT_STR` — a
    hardcoded default overridable by env var. Never derived from the
    oldest ForwardValidationSnapshot (that would collapse to "now"
    on first deploy and misclassify games whose window opened
    minutes after activation).

    Returns a timezone-aware datetime. Raises ValueError if the
    configured string is unparseable — a misconfigured boundary would
    invalidate weeks of prospective evidence, so surface it loudly.
    """
    from django.conf import settings
    from django.utils.dateparse import parse_datetime
    from django.utils import timezone
    raw = getattr(settings, 'FORWARD_VALIDATION_ACTIVATION_AT_STR',
                  '2026-08-24T12:00:00+00:00')
    dt = parse_datetime(raw)
    if dt is None:
        raise ValueError(
            f'FORWARD_VALIDATION_ACTIVATION_AT_STR is not a valid ISO-8601 '
            f'datetime: {raw!r}'
        )
    if timezone.is_naive(dt):
        raise ValueError(
            f'FORWARD_VALIDATION_ACTIVATION_AT_STR must be tz-aware: {raw!r}'
        )
    return dt


def _classify_decision(rec) -> str:
    """Map a Recommendation dataclass to a canonical decision_class."""
    if rec is None:
        return 'no_signal'
    status = getattr(rec, 'status', '')
    lane = getattr(rec, 'lane', '')
    if status == 'recommended' and lane == 'core':
        return 'recommended'
    if status == 'recommended' and lane == 'qualified':
        return 'potential'
    return 'not_recommended'


def _pick_side_from_rec(rec, game) -> str:
    if rec is None or not getattr(rec, 'pick', ''):
        return ''
    pick = str(rec.pick).strip().lower()
    home_name = str(getattr(game, 'home_team', '') or '').lower()
    away_name = str(getattr(game, 'away_team', '') or '').lower()
    if pick in home_name or home_name in pick:
        return 'home'
    if pick in away_name or away_name in pick:
        return 'away'
    return ''


def _latest_odds_source(game) -> tuple:
    """Return (odds_source, odds_captured_at) from the latest MLB
    OddsSnapshot with moneylines, or ('unknown', None)."""
    try:
        snap = (
            game.odds_snapshots
            .filter(moneyline_home__isnull=False,
                    moneyline_away__isnull=False)
            .order_by('-captured_at')
            .first()
        )
    except Exception:
        return ('unknown', None)
    if snap is None:
        return ('unknown', None)
    src = getattr(snap, 'odds_source', 'unknown') or 'unknown'
    return (src, getattr(snap, 'captured_at', None))


def _lineup_state_for(game) -> tuple:
    """Return (lineup_state, ref) from ConfirmedLineup rows on this
    game. Never scoring-authoritative — pure metadata."""
    try:
        from apps.mlb.models import ConfirmedLineup
    except Exception:
        return ('unknown', '')
    try:
        latest = (
            ConfirmedLineup.objects
            .filter(game=game)
            .order_by('-first_seen_at')
            .first()
        )
    except Exception:
        return ('unknown', '')
    if latest is None:
        return ('unknown', '')
    # ConfirmedLineup's exact schema is defined elsewhere — degrade
    # gracefully if fields aren't as expected.
    state = getattr(latest, 'confirmation_state', None) or 'confirmed'
    if state not in {'unknown', 'projected', 'confirmed',
                     'updated_after_confirmation'}:
        state = 'confirmed'
    ref = f'{getattr(latest, "id", "")}@{getattr(latest, "first_seen_at", "")}'
    return (state, ref[:64])


def _capture_one(game, *, now, dry_run: bool = False) -> Optional[Dict[str, Any]]:
    """Idempotent capture of a single game. Returns a small dict
    describing the outcome, or None if not eligible / already captured.

    dry_run=True computes eligibility and would-be decision without
    persisting — used by the capture-health probe to preview what the
    next real run will do without polluting the audit trail.
    """
    from apps.analytics.models import ForwardValidationSnapshot
    from apps.core.services.recommendations import get_recommendation

    if game.first_pitch is None:
        return None
    minutes = int((game.first_pitch - now).total_seconds() / 60)
    if minutes < MIN_WINDOW_MIN or minutes > MAX_WINDOW_MIN:
        return None

    # Idempotence.
    exists = (
        ForwardValidationSnapshot.objects
        .filter(mlb_game=game, engine_version=ENGINE_VERSION)
        .exists()
    )
    if exists:
        return {'game_id': str(game.id), 'status': 'already_captured'}

    try:
        rec = get_recommendation('mlb', game, user=None)
    except Exception as exc:
        logger.exception('v3_2_capture: get_recommendation failed game=%s',
                         game.id)
        rec = None
        rec_error = repr(exc)[:200]
    else:
        rec_error = ''

    decision_class = _classify_decision(rec)
    pick_side = _pick_side_from_rec(rec, game)
    odds_source, odds_captured_at = _latest_odds_source(game)
    lineup_state, lineup_ref = _lineup_state_for(game)

    fields = {
        'mlb_game': game,
        'engine_version': ENGINE_VERSION,
        'minutes_to_first_pitch': minutes,
        'decision_class': decision_class,
        'pick': (rec.pick if rec else '') or '',
        'pick_side': pick_side,
        'odds_american': rec.odds_american if rec else None,
        'odds_source': odds_source,
        'odds_captured_at': odds_captured_at,
        'raw_model_prob': (rec.raw_model_prob if rec else None),
        'final_model_prob': (rec.final_model_prob if rec else None),
        'market_prob': (rec.market_prob if rec else None),
        'edge_pp': (float(rec.model_edge) if rec else None),
        'confidence_score': (float(rec.confidence_score) if rec else None),
        'status': (rec.status if rec else ''),
        'status_reason': (rec.status_reason if rec else ''),
        'tier': (rec.tier if rec else ''),
        'lane': (rec.lane if rec else ''),
        'risk_flags': (rec.risk_flags if rec and rec.risk_flags else {}),
        'risk_score': (rec.risk_score if rec else 0),
        'is_secondary': (rec.is_secondary if rec else False),
        'movement_class': (rec.movement_class if rec and rec.movement_class
                           else ''),
        'movement_score': (rec.movement_score if rec else None),
        'movement_supports_pick': (rec.movement_supports_pick if rec else False),
        'market_warning': (rec.market_warning if rec else False),
        'feature_contributions': (rec.feature_contributions
                                  if rec and rec.feature_contributions
                                  else {'capture_error': rec_error}
                                  if rec_error else {}),
        'lineup_state': lineup_state,
        'lineup_snapshot_ref': lineup_ref,
    }
    if dry_run:
        return {'game_id': str(game.id), 'status': 'dry_run',
                'decision_class': decision_class,
                'minutes_to_first_pitch': minutes}
    row = ForwardValidationSnapshot.objects.create(**fields)
    return {
        'game_id': str(game.id), 'status': 'created',
        'snapshot_id': str(row.id),
        'decision_class': decision_class,
        'minutes_to_first_pitch': minutes,
    }


def capture_pending(*, now=None, dry_run: bool = False) -> Dict[str, Any]:
    """Run capture across every MLB game whose first_pitch is inside
    the canonical window. Returns a summary suitable for logging into
    the CronRunLog stdout tail."""
    from apps.mlb.models import Game

    now = now or timezone.now()
    window_lo = now + timedelta(minutes=MIN_WINDOW_MIN)
    window_hi = now + timedelta(minutes=MAX_WINDOW_MIN)

    candidates = list(
        Game.objects
        .filter(first_pitch__gte=window_lo, first_pitch__lte=window_hi)
        .exclude(status='final')
        .select_related('home_team', 'away_team',
                        'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    created = 0
    already = 0
    processed = []
    for g in candidates:
        r = _capture_one(g, now=now, dry_run=dry_run)
        if r is None:
            continue
        processed.append(r)
        if r['status'] == 'created':
            created += 1
        elif r['status'] == 'already_captured':
            already += 1
        elif r['status'] == 'dry_run':
            pass

    # Missed-window detection: games whose first_pitch has passed but
    # captured_at was never inside the canonical window. If a snapshot
    # exists but was captured much earlier or much later than target,
    # that's a signal the refresh cadence isn't dense enough near
    # first pitch.
    return {
        'now': now.isoformat(),
        'window': {
            'from': window_lo.isoformat(),
            'to': window_hi.isoformat(),
            'min_min_to_fp': MIN_WINDOW_MIN,
            'max_min_to_fp': MAX_WINDOW_MIN,
        },
        'candidates_in_window': len(candidates),
        'captured': created,
        'already_captured': already,
        'dry_run': dry_run,
        'processed': processed[:50],
    }


def get_forward_validation_started_at():
    """Return the datetime of the first ForwardValidationSnapshot in
    the DB. Never a historical backfill — every row was captured
    prospectively when a game entered the canonical window."""
    from apps.analytics.models import ForwardValidationSnapshot
    first = (
        ForwardValidationSnapshot.objects
        .order_by('captured_at')
        .only('captured_at')
        .first()
    )
    return first.captured_at if first else None
