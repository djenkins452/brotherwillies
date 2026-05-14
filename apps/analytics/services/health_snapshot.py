"""Health Score snapshot persistence — the governance ledger.

Writes `RecommendationHealthSnapshot` rows. Append-only. The
recommendation engine never reads this table; only analytics surfaces
do. The snapshot is a frozen receipt of "what did the engine look
like at this moment?".

Typical use:
  - Daily cron capture (idempotent — operator chooses cadence).
  - Manual capture before major model changes (pre-Elo baseline).
  - Manual capture after major model changes (post-cutover regression
    detection).

The capture is intentionally cheap (one Health Score compute + one
DB insert) so the operator can run it as often as needed.
"""
from __future__ import annotations

from typing import Optional

from .health_score import HealthScore, compute_health_score


def capture_snapshot(
    *, window_days: int = 14, notes: str = '',
    health: Optional[HealthScore] = None,
):
    """Compute (or accept) a Health Score and persist it.

    Args:
        window_days: passed through to compute_health_score if `health`
            is not supplied.
        notes: optional free-text tag (e.g. 'pre-elo baseline').
        health: optional pre-computed HealthScore. Pass when the caller
            already has one in hand to avoid duplicate computation.

    Returns: the saved `RecommendationHealthSnapshot` instance.

    No-op safeguards:
        - When health.overall_score is None (insufficient data across
          the board), the snapshot is still persisted with band=''.
          The historical ledger benefits from "we tried at this moment;
          no data was available" rows.
    """
    from apps.analytics.models import RecommendationHealthSnapshot

    if health is None:
        health = compute_health_score(window_days=window_days)

    snap = RecommendationHealthSnapshot.objects.create(
        overall_score=health.overall_score if health.overall_score is not None else 0.0,
        band=health.band or '',
        dimension_scores=health.dimension_scores,
        supporting_data=health.supporting_data,
        rating_mode_active=health.rating_mode_active,
        calibration_state=health.calibration_state,
        notes=notes,
    )
    return snap


def recent_snapshots(limit: int = 30):
    """Return the most recent snapshots, newest first."""
    from apps.analytics.models import RecommendationHealthSnapshot
    return list(RecommendationHealthSnapshot.objects.all()[:limit])


def latest_snapshot():
    """Return the most recent snapshot, or None."""
    from apps.analytics.models import RecommendationHealthSnapshot
    return RecommendationHealthSnapshot.objects.first()
