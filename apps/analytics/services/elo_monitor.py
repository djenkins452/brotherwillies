"""Elo Activation Monitor service.

Read-only diagnostic for the 2-3 week observation window following the
Phase 2A Task 4 Elo activation (2026-05-16). Surfaces:

  - Activation state (USE_DYNAMIC_RATINGS) + pre-Elo baseline snapshot.
  - Latest Health Score + delta from the pre-Elo baseline.
  - Rollback-trigger status indicators per the GO doc.

Cannot influence recommendation behavior. The recommendation engine
does not query this service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from django.utils import timezone


# Rollback-trigger thresholds. Hardcoded constants tied to the
# 2026-05-16 GO recommendation doc. Changing these values requires
# an amendment to docs/phase_2a_task4_elo_activation_2026_05_16.md
# in the same commit (architecture law continuity).
ROLLBACK_CLV_DROP_PP = 5.0          # CLV+ rate drop ≥ 5pp vs baseline
ROLLBACK_VOLUME_DROP_PCT = 0.50     # > 50% drop in recommendation volume
ROLLBACK_HEALTH_DROP_POINTS = 10.0  # Health Score drop ≥ 10 points
ROLLBACK_INTERVENE_THRESHOLD = 25.0 # Health Score in INTERVENE band


@dataclass
class TriggerStatus:
    """Single rollback trigger evaluation."""
    name: str
    threshold_label: str
    current_value: Optional[str]
    fired: bool
    severity: str  # 'ok' / 'watch' / 'critical'
    detail: str = ''


@dataclass
class EloActivationMonitor:
    """Composite monitor state. JSON-friendly via asdict."""
    activation_status: dict          # USE_DYNAMIC_RATINGS, mode, env override
    pre_elo_baseline: Optional[dict] # snapshot summary or None
    current_health: Optional[dict]   # current snapshot summary or None
    score_delta: Optional[float]     # current - baseline
    rollback_triggers: list          # list of TriggerStatus dicts
    observation_window_days: int     # days since activation


def _snapshot_summary(snap) -> dict:
    """Extract the operator-relevant fields from a snapshot row."""
    return {
        'captured_at': snap.captured_at.isoformat() if snap.captured_at else None,
        'overall_score': snap.overall_score,
        'band': snap.band,
        'notes': snap.notes,
        'rating_mode_active': snap.rating_mode_active,
        # Dimension scores for delta computation.
        'dimension_scores': snap.dimension_scores or {},
    }


def _find_pre_elo_baseline():
    """Find the most recent snapshot tagged as the pre-Elo baseline.

    Matching is by `notes` containing 'pre-elo' (case-insensitive) — the
    convention documented in docs/health_score_operations.md and
    docs/phase_2a_task4_elo_activation_2026_05_16.md. Multiple matches:
    take the most recent.
    """
    from apps.analytics.models import RecommendationHealthSnapshot
    return (
        RecommendationHealthSnapshot.objects
        .filter(notes__icontains='pre-elo')
        .order_by('-captured_at')
        .first()
    )


def _activation_status() -> dict:
    """Capture the current USE_DYNAMIC_RATINGS state + the source."""
    import os
    from django.conf import settings
    env_value = os.environ.get('USE_DYNAMIC_RATINGS')
    return {
        'active': bool(getattr(settings, 'USE_DYNAMIC_RATINGS', False)),
        'env_var_set': env_value is not None,
        'env_var_value': env_value,
        'rating_mode': 'elo' if getattr(settings, 'USE_DYNAMIC_RATINGS', False) else 'static',
    }


def _evaluate_rollback_triggers(baseline, current) -> list:
    """Per the GO doc, the rollback-trigger conditions.

    Each returns a TriggerStatus dict so the template can render
    them uniformly. Conditions evaluate against the most-recent
    snapshot's data.

    A trigger that fires does NOT auto-rollback. Per Law 4 and the
    GO doc's monitoring section, the operator inspects, investigates,
    and decides. Auto-rollback would be a self-modifying behavior
    forbidden by Law 4's spirit.
    """
    triggers = []

    # --- Trigger 1: CLV deterioration ---
    baseline_clv = (
        baseline.dimension_scores.get('clv_trend', {}).get('value')
        if baseline else None
    )
    current_clv = (
        current.dimension_scores.get('clv_trend', {}).get('value')
        if current else None
    )
    fired = False
    detail = ''
    current_str = (
        f'{current_clv:.1%}' if current_clv is not None else '—'
    )
    if baseline_clv is not None and current_clv is not None:
        drop_pp = (baseline_clv - current_clv) * 100.0
        if drop_pp >= ROLLBACK_CLV_DROP_PP:
            fired = True
            detail = (
                f'CLV+ dropped {drop_pp:.1f}pp vs baseline '
                f'({baseline_clv:.1%} → {current_clv:.1%}). '
                f'Threshold: ≥{ROLLBACK_CLV_DROP_PP}pp.'
            )
        else:
            detail = (
                f'CLV+ change vs baseline: {-drop_pp:+.1f}pp '
                f'({baseline_clv:.1%} → {current_clv:.1%}). Within band.'
            )
    triggers.append({
        'name': 'CLV deterioration',
        'threshold_label': f'Drop ≥ {ROLLBACK_CLV_DROP_PP}pp from baseline',
        'current_value': current_str,
        'fired': fired,
        'severity': 'critical' if fired else 'ok',
        'detail': detail,
    })

    # --- Trigger 2: Health Score collapse (composite) ---
    baseline_score = baseline.overall_score if baseline else None
    current_score = current.overall_score if current else None
    fired = False
    detail = ''
    if baseline_score is not None and current_score is not None:
        drop_points = baseline_score - current_score
        if drop_points >= ROLLBACK_HEALTH_DROP_POINTS:
            fired = True
            detail = (
                f'Health Score dropped {drop_points:.1f} points '
                f'({baseline_score:.1f} → {current_score:.1f}). '
                f'Threshold: ≥{ROLLBACK_HEALTH_DROP_POINTS} points.'
            )
        else:
            detail = (
                f'Score change vs baseline: {-drop_points:+.1f} points '
                f'({baseline_score:.1f} → {current_score:.1f}). Within band.'
            )
    triggers.append({
        'name': 'Health Score collapse',
        'threshold_label': f'Drop ≥ {ROLLBACK_HEALTH_DROP_POINTS} points from baseline',
        'current_value': (
            f'{current_score:.1f}' if current_score is not None else '—'
        ),
        'fired': fired,
        'severity': 'critical' if fired else 'ok',
        'detail': detail,
    })

    # --- Trigger 3: INTERVENE band reached ---
    fired = False
    detail = ''
    if current_score is not None:
        if current_score < ROLLBACK_INTERVENE_THRESHOLD:
            fired = True
            detail = (
                f'Composite Health Score {current_score:.1f} is in the '
                f'INTERVENE band (< {ROLLBACK_INTERVENE_THRESHOLD}). '
                f'Per Law 4 targeted action is justified; rollback is '
                f'the simplest targeted action.'
            )
        else:
            detail = f'Score {current_score:.1f} is above INTERVENE threshold.'
    triggers.append({
        'name': 'Composite in INTERVENE band',
        'threshold_label': f'Composite score < {ROLLBACK_INTERVENE_THRESHOLD}',
        'current_value': (
            f'{current_score:.1f}' if current_score is not None else '—'
        ),
        'fired': fired,
        'severity': 'critical' if fired else 'ok',
        'detail': detail,
    })

    # --- Trigger 4: Edge-realism perverse (8+ ROI < 4-6 ROI by a lot) ---
    # The fake-edge pathology Elo was supposed to remove. If we're seeing
    # the perverse outperformance AFTER Elo activation, the cutover
    # didn't address the cause.
    fired = False
    detail = ''
    current_edge = (
        current.dimension_scores.get('edge_realism', {})
        if current else {}
    )
    edge_value = current_edge.get('value')
    edge_sample_8plus = current_edge.get('sample_8plus', 0)
    edge_sample_4to6 = current_edge.get('sample_4to6', 0)
    current_value_str = '—'
    if edge_value is not None:
        current_value_str = f'{edge_value:.4f}'
        if (
            edge_sample_8plus >= 20 and edge_sample_4to6 >= 20
            and edge_value < -0.05
        ):
            fired = True
            detail = (
                f'8+ edge bucket ROI is {edge_value:.1%} BELOW 4-6 bucket '
                f'(8+ n={edge_sample_8plus}, 4-6 n={edge_sample_4to6}). '
                'Fake-edge pathology persists post-Elo. Investigate; '
                'rollback if persistent.'
            )
        else:
            detail = (
                f'8+ vs 4-6 ROI delta: {edge_value:+.4f}. '
                f'Within band (8+ n={edge_sample_8plus}, '
                f'4-6 n={edge_sample_4to6}).'
            )
    triggers.append({
        'name': 'Edge realism inversion (fake-edge persists)',
        'threshold_label': '8+ ROI ≥ 5pp BELOW 4-6 ROI with sample ≥ 20 each',
        'current_value': current_value_str,
        'fired': fired,
        'severity': 'critical' if fired else 'ok',
        'detail': detail,
    })

    return triggers


def build_monitor() -> EloActivationMonitor:
    """Compose the monitor state.

    Live computation — reads the latest Health Score snapshot and the
    most recent pre-Elo-tagged snapshot. Does not write anything.
    """
    from apps.analytics.services.health_snapshot import latest_snapshot

    baseline = _find_pre_elo_baseline()
    current = latest_snapshot()

    # Guard: if baseline IS the most-recent snapshot (because no
    # post-Elo snapshot has been captured yet), we have no signal yet.
    if current and baseline and current.id == baseline.id:
        # Effectively no post-Elo snapshot yet.
        current = None

    score_delta = None
    if baseline and current and current.overall_score and baseline.overall_score:
        score_delta = round(current.overall_score - baseline.overall_score, 2)

    triggers = _evaluate_rollback_triggers(baseline, current)

    observation_window_days = 0
    if baseline:
        delta = timezone.now() - baseline.captured_at
        observation_window_days = max(0, delta.days)

    return EloActivationMonitor(
        activation_status=_activation_status(),
        pre_elo_baseline=_snapshot_summary(baseline) if baseline else None,
        current_health=_snapshot_summary(current) if current else None,
        score_delta=score_delta,
        rollback_triggers=triggers,
        observation_window_days=observation_window_days,
    )
