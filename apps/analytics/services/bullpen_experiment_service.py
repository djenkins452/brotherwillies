"""v3.3 SHADOW — bullpen A/B/C experiment orchestrator (background thread).

Runs `bullpen_replay.run_bullpen_experiment` off the request thread so
the ~2700-game production replay can complete without hitting
gunicorn's 30s worker timeout (which is what caused the initial
`/analytics/method-replay/?experiment=bullpen&days=180` HTTP 500).

Mirrors the `BullpenBackfillRun` orchestration pattern. Never raises
into the caller — any exception is captured as
`BullpenExperimentRun.status='failed'` with structured failure_summary
+ full traceback preserved in error_message.

Never writes bullpen feature flags. Never touches production
recommendation code paths. Reads only the historical MLB data
already ingested by the backfill.
"""
from __future__ import annotations

import logging
import traceback
from dataclasses import asdict, is_dataclass

from django.utils import timezone


logger = logging.getLogger(__name__)


LOG_TAIL_LINE_CAP = 40
LOG_TAIL_CHAR_CAP = 4000


def _append_log(run, line: str) -> None:
    stamp = timezone.now().strftime('%H:%M:%S')
    payload = f'[{stamp}] {line}'
    lines = (run.log_tail or '').splitlines()
    lines.append(payload)
    lines = lines[-LOG_TAIL_LINE_CAP:]
    tail = '\n'.join(lines)
    if len(tail) > LOG_TAIL_CHAR_CAP:
        tail = tail[-LOG_TAIL_CHAR_CAP:]
    run.log_tail = tail
    run.save(update_fields=['log_tail'])


def _json_safe(obj):
    """Recursively coerce dataclasses, sets, dates to JSONable types
    so the full experiment result dict can round-trip through
    BullpenExperimentRun.result (a JSONField)."""
    from datetime import date, datetime
    if is_dataclass(obj) and not isinstance(obj, type):
        return _json_safe(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, set):
        return sorted(_json_safe(x) for x in obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def run_experiment_in_background(run_id: str) -> None:
    """Background-thread body. Loads the run row, executes the
    experiment (kind='experiment' = A/B/C replay; kind='attribution'
    = full salvage study) with a progress callback that writes back
    into the row, persists the result. Wrapped in try/except so
    failure ends up as status='failed' with a human-readable summary."""
    from apps.analytics.models import BullpenExperimentRun
    from apps.analytics.services.bullpen_replay import (
        run_bullpen_experiment,
    )
    from apps.analytics.services.bullpen_attribution import (
        run_bullpen_attribution,
    )
    from apps.analytics.services.bullpen_veto_walkforward import (
        run_veto_walkforward,
    )
    from apps.analytics.services.offense_replay import (
        run_offense_experiment,
    )

    try:
        run = BullpenExperimentRun.objects.get(id=run_id)
    except BullpenExperimentRun.DoesNotExist:
        logger.exception('bullpen_experiment: run row missing id=%s', run_id)
        return

    try:
        run.status = 'running'
        run.started_at = timezone.now()
        run.save()
        _append_log(run, f'Starting {run.kind} days={run.days} blend={run.blend_weight}')

        # Progress callback shape depends on kind:
        #   experiment  → (variant, i, total, sims_kept, errors)
        #   attribution → (phase, current, total)
        def _experiment_progress(*, variant, i, total, sims_kept, errors):
            run.progress_variant = variant
            run.progress_current = i
            run.progress_total = total
            run.save(update_fields=[
                'progress_variant', 'progress_current', 'progress_total',
            ])
            if i % 100 == 0:
                _append_log(
                    run,
                    f'variant {variant}: {i}/{total} sims, kept={sims_kept}, err={errors}',
                )

        def _attribution_progress(*, phase, current, total):
            run.progress_variant = phase
            run.progress_current = current
            run.progress_total = total
            run.save(update_fields=[
                'progress_variant', 'progress_current', 'progress_total',
            ])
            if current % 300 == 0:
                _append_log(run, f'{phase}: {current}/{total}')

        if run.kind == 'attribution':
            result = run_bullpen_attribution(
                days=run.days,
                blend_weight=run.blend_weight,
                progress_cb=_attribution_progress,
            )
        elif run.kind == 'veto_walkforward':
            # Same progress-callback shape as attribution — the veto
            # walk-forward publishes {phase, current, total} tuples.
            result = run_veto_walkforward(
                days=run.days,
                blend_weight=run.blend_weight,
                progress_cb=_attribution_progress,
            )
        elif run.kind == 'offense_replay':
            # v3.4 offense replay — same {phase, current, total} shape.
            result = run_offense_experiment(
                days=run.days,
                blend_weight=run.blend_weight,
                progress_cb=_attribution_progress,
            )
        else:
            result = run_bullpen_experiment(
                days=run.days,
                blend_weight=run.blend_weight,
                progress_cb=_experiment_progress,
            )
        run.result = _json_safe(result)
        run.status = 'completed'
        run.finished_at = timezone.now()
        run.save()
        _append_log(run, f'Completed in {run.elapsed_seconds}s')
    except Exception as exc:  # noqa: BLE001
        logger.exception('bullpen_experiment_failed run_id=%s', run_id)
        try:
            run = BullpenExperimentRun.objects.get(id=run_id)
            run.status = 'failed'
            run.failure_summary = repr(exc)[:500]
            run.error_message = ''.join(traceback.format_exception(exc))[:6000]
            run.finished_at = timezone.now()
            run.save()
            _append_log(run, f'FAILED: {repr(exc)[:250]}')
        except Exception:
            logger.exception('bullpen_experiment_failed_save run_id=%s', run_id)
