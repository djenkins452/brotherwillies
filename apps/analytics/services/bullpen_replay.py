"""v3.3 SHADOW — bullpen replay experiment.

Compares three variants on the same historical MLB slate under v3.2
selection thresholds:

  A. v3.2 baseline               (no bullpen terms)
  B. v3.2 + bullpen quality      (quality delta enters the score)
  C. v3.2 + bullpen quality + fatigue

Uses the exact leakage-safe machinery from
`apps.analytics.services.method_replay._simulate_recommendation`
(reference_date = game.first_pitch; snapshot lookups use strict `<`
against first pitch). The bullpen toggles are threaded through the
existing sim without re-plumbing.

DATA COVERAGE REPORTING

  The report includes a bullpen-data-coverage summary: what fraction
  of the evaluable games had a `TeamBullpenSnapshot` for BOTH teams
  strictly before first pitch. When coverage is zero (the current
  state as of 2026-08-22 because ingestion is scaffolded but not yet
  wired), the report explicitly flags the run as INFRASTRUCTURE-ONLY
  and refuses to draw any conclusion — B and C will produce identical
  numbers to A, and reporting a "no-change" verdict as validation
  would be misleading.

This module DOES NOT toggle any feature flag or write anything to the
DB. Read-only. Callable programmatically or via the
`?experiment=bullpen` staff endpoint on
`apps/analytics/views.py::method_replay`.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional

from django.utils import timezone

logger = logging.getLogger(__name__)


def _simulate_variant(games, *, blend_weight, use_recent_form,
                      use_bullpen_quality, use_bullpen_fatigue, label,
                      progress_cb=None):
    """Run the leakage-safe sim across all games under a variant config.
    Returns (sims, error_report_dict).

    Never raises. Individual game failures are counted and categorized
    so ordinary incomplete historical data (e.g. a game with no
    pre-game odds snapshot) does NOT crash the whole experiment.

    progress_cb (optional) is called every 25 games with the current
    (i, total, sims_kept, errors) — used by the background-thread
    orchestrator to update BullpenExperimentRun progress rows live.
    """
    from apps.analytics.services.method_replay import _simulate_recommendation

    sims = []
    errors = 0
    error_categories: dict = {}
    none_returns = 0
    total = len(games)
    for i, g in enumerate(games, 1):
        try:
            sim = _simulate_recommendation(
                g, blend_weight, label,
                use_recent_form=use_recent_form,
                use_bullpen_quality=use_bullpen_quality,
                use_bullpen_fatigue=use_bullpen_fatigue,
            )
        except Exception as exc:
            errors += 1
            # Categorize by exception type so ops can see if failures
            # cluster on one root cause (e.g. TeamEloHistory missing
            # for a subset of games) vs random one-offs.
            cat = type(exc).__name__
            error_categories[cat] = error_categories.get(cat, 0) + 1
            logger.exception(
                'bullpen_replay: sim failed game=%s label=%s',
                getattr(g, 'id', None), label,
            )
            continue
        if sim is None:
            # sim returned None because pre-game odds were missing —
            # this is EXPECTED for some historical games and is NOT
            # an error. Counted separately from exceptions.
            none_returns += 1
            continue
        sims.append(sim)
        if progress_cb is not None and i % 25 == 0:
            progress_cb(i=i, total=total, sims_kept=len(sims), errors=errors)
    return sims, {
        'errors': errors,
        'categories': error_categories,
        'none_returns': none_returns,
        'total_games_attempted': total,
    }


def _bullpen_data_coverage(games) -> dict:
    """How many games in the window have a TeamBullpenSnapshot for BOTH
    teams strictly before first_pitch? This is the honest denominator
    for whether the bullpen experiment can say anything meaningful."""
    from apps.mlb.models import TeamBullpenSnapshot
    both_covered = 0
    home_only = 0
    away_only = 0
    neither = 0
    for g in games:
        h = TeamBullpenSnapshot.objects.filter(
            team=g.home_team, as_of__lt=g.first_pitch,
        ).exists()
        a = TeamBullpenSnapshot.objects.filter(
            team=g.away_team, as_of__lt=g.first_pitch,
        ).exists()
        if h and a:
            both_covered += 1
        elif h:
            home_only += 1
        elif a:
            away_only += 1
        else:
            neither += 1
    total = max(1, len(games))
    return {
        'total_games': len(games),
        'both_covered': both_covered,
        'home_only': home_only,
        'away_only': away_only,
        'neither': neither,
        'both_covered_pct': round(100.0 * both_covered / total, 2),
    }


def run_bullpen_experiment(
    *,
    days: int = 90,
    blend_weight: float = 0.55,
    reference_date=None,
    min_games_for_window: int = 20,
    coverage_ship_criterion_pct: float = 80.0,
    progress_cb=None,
) -> dict:
    """Compare A (v3.2 baseline) / B (+quality) / C (+quality+fatigue).

    Recent-form is ON in all three variants (v3.1 is already validated
    and part of the frozen v3.2 baseline). The window defaults to 90
    days — same as run_recent_form_experiment for comparability.

    `coverage_ship_criterion_pct` is the pre-registered gate for
    "enough data to draw any conclusion" (design doc §5, criterion 6).
    Default 80% of games with both teams' snapshots present.
    """
    ref = reference_date or timezone.localdate()
    date_to = ref - timedelta(days=1)
    date_from = ref - timedelta(days=days)

    from apps.mlb.models import Game
    from apps.analytics.services.method_replay import _compute_metrics

    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=date_from,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    coverage = _bullpen_data_coverage(games)
    coverage_ok = coverage['both_covered_pct'] >= coverage_ship_criterion_pct

    # Pluggable progress callback per variant — lets the background
    # orchestrator write live progress rows for the operator page.
    def _variant_progress(prefix):
        if progress_cb is None:
            return None
        def _cb(**kw):
            progress_cb(variant=prefix, **kw)
        return _cb

    a_sims, a_err = _simulate_variant(
        games, blend_weight=blend_weight, use_recent_form=True,
        use_bullpen_quality=False, use_bullpen_fatigue=False,
        label='A_v3_2_baseline',
        progress_cb=_variant_progress('A'),
    )
    b_sims, b_err = _simulate_variant(
        games, blend_weight=blend_weight, use_recent_form=True,
        use_bullpen_quality=True, use_bullpen_fatigue=False,
        label='B_v3_2_plus_quality',
        progress_cb=_variant_progress('B'),
    )
    c_sims, c_err = _simulate_variant(
        games, blend_weight=blend_weight, use_recent_form=True,
        use_bullpen_quality=True, use_bullpen_fatigue=True,
        label='C_v3_2_plus_quality_and_fatigue',
        progress_cb=_variant_progress('C'),
    )

    def _lc(sims):
        return [s for s in sims if s.is_lane_corrected_recommended]

    a_lc, b_lc, c_lc = _lc(a_sims), _lc(b_sims), _lc(c_sims)
    a_metrics = _compute_metrics(a_lc)
    b_metrics = _compute_metrics(b_lc)
    c_metrics = _compute_metrics(c_lc)

    # Comparability check: variants must share the same underlying
    # simulated-game population. Only the score decomposition (bullpen
    # terms) may differ. If sim populations diverge (e.g. one variant
    # hit an exception the other didn't), report it — the delta below
    # is not comparable.
    sim_populations = {
        'a': len(a_sims),
        'b': len(b_sims),
        'c': len(c_sims),
    }
    populations_match = (
        sim_populations['a'] == sim_populations['b'] == sim_populations['c']
    )

    return {
        'window': {
            'days': days, 'from': date_from, 'to': date_to,
            'blend_weight': blend_weight,
            'games_evaluable': len(games),
        },
        'coverage': coverage,
        'coverage_ship_criterion_pct': coverage_ship_criterion_pct,
        'coverage_ok': coverage_ok,
        'a_v3_2_baseline':      {'metrics': a_metrics, 'count': len(a_lc), 'sim_errors': a_err},
        'b_plus_quality':       {'metrics': b_metrics, 'count': len(b_lc), 'sim_errors': b_err},
        'c_plus_quality_and_fatigue': {'metrics': c_metrics, 'count': len(c_lc), 'sim_errors': c_err},
        'data_ok': len(games) >= min_games_for_window,
        'sim_populations': sim_populations,
        'populations_match': populations_match,
    }


def render_bullpen_experiment(exp: dict) -> str:
    """Plaintext staff renderer. Honest about no-data state."""
    w = exp['window']
    cov = exp['coverage']
    a = exp['a_v3_2_baseline']
    b = exp['b_plus_quality']
    c = exp['c_plus_quality_and_fatigue']

    lines = []
    lines.append('#' * 100)
    lines.append('#  BULLPEN EXPERIMENT — A: v3.2 baseline / B: +quality / C: +quality+fatigue')
    lines.append(f"#  Window: last {w['days']} days  ({w['from']} → {w['to']})")
    lines.append(f"#  Blend weight: {w['blend_weight']:.2f}    Games evaluable: {w['games_evaluable']}")
    lines.append('#' * 100)
    lines.append('')

    lines.append('NOTE: This sync endpoint is subject to gunicorn worker timeouts')
    lines.append('at production scale (~2700 games * 3 variants). For a full')
    lines.append('180-day production run, use the async page instead:')
    lines.append('  /analytics/bullpen-experiment/')
    lines.append('')

    lines.append('DATA COVERAGE — TeamBullpenSnapshot presence before first_pitch')
    lines.append('-' * 78)
    lines.append(f"  total games          : {cov['total_games']}")
    lines.append(f"  both teams covered   : {cov['both_covered']} ({cov['both_covered_pct']}%)")
    lines.append(f"  home only            : {cov['home_only']}")
    lines.append(f"  away only            : {cov['away_only']}")
    lines.append(f"  neither              : {cov['neither']}")
    lines.append(f"  ship criterion (design §5.6): >= {exp['coverage_ship_criterion_pct']}%")
    lines.append(f"  coverage_ok          : {exp['coverage_ok']}")
    lines.append('')

    if not exp['coverage_ok']:
        lines.append('*' * 78)
        lines.append('*  ⚠ INFRASTRUCTURE-ONLY RUN — coverage below the ship criterion.')
        lines.append('*  Bullpen deltas will be zero for uncovered games; B and C will')
        lines.append("*  reproduce A's numbers with statistical noise from any covered games.")
        lines.append('*  DO NOT interpret a "no change" verdict as validation. Populate')
        lines.append('*  TeamBullpenSnapshot via the ingest_bullpen_snapshots command and')
        lines.append('*  re-run once coverage reaches the ship criterion.')
        lines.append('*' * 78)
        lines.append('')

    if not exp['data_ok']:
        lines.append('⚠ THIN GAME COUNT — fewer than min_games_for_window; results directional only.')
        lines.append('')

    def _line(label, block):
        m = block['metrics']
        n = m.get('count', 0)
        win = f"{m['win_rate']:.2f}%" if m.get('win_rate') is not None else '   n/a'
        roi = f"{m['roi']:+.2f}%" if m.get('roi') is not None else '   n/a'
        clv = f"{m['positive_clv_rate']:.1f}%" if m.get('positive_clv_rate') is not None else '  n/a'
        return f"  {label:<32}  n={n:>4}  W-L {m['wins']:>3}-{m['losses']:<3}  win {win}  ROI {roi}  CLV+ {clv}"

    lines.append('AGGREGATE (lane-corrected recommendations)')
    lines.append('-' * 78)
    lines.append(_line('A — v3.2 baseline',            a))
    lines.append(_line('B — v3.2 + quality',           b))
    lines.append(_line('C — v3.2 + quality + fatigue', c))
    lines.append('')

    # v3.3 observability: sim-error + population comparability. Answers
    # "why is the evidence incomplete?" without the operator needing
    # to read logs.
    lines.append('SIM POPULATION + ERRORS')
    lines.append('-' * 78)
    sp = exp.get('sim_populations', {})
    lines.append(
        f'  simulated_sims: A={sp.get("a", "?")}  '
        f'B={sp.get("b", "?")}  C={sp.get("c", "?")}   '
        f'match={"yes" if exp.get("populations_match") else "NO — deltas below are not comparable"}'
    )
    for variant_key, block, letter in [
        ('a_v3_2_baseline', a, 'A'),
        ('b_plus_quality', b, 'B'),
        ('c_plus_quality_and_fatigue', c, 'C'),
    ]:
        er = block.get('sim_errors') or {}
        cat_str = ', '.join(f'{k}={v}' for k, v in (er.get('categories') or {}).items()) or 'none'
        lines.append(
            f'  {letter}: errors={er.get("errors", 0)}  '
            f'none_returns={er.get("none_returns", 0)}  '
            f'attempted={er.get("total_games_attempted", 0)}  '
            f'categories=[{cat_str}]'
        )
    lines.append('')

    if not exp['coverage_ok']:
        lines.append('VERDICT: NO EVIDENCE PRODUCED (data coverage below ship criterion).')
        lines.append("        The infrastructure works; the data pipe does not yet feed it.")
    else:
        # Only render a directional call when coverage clears.
        def _delta(x, y):
            if x is None or y is None:
                return None
            return round(y - x, 2)
        lines.append('DELTAS vs Baseline (informational — full ship criteria run separately)')
        lines.append('-' * 78)
        lines.append(f"  B vs A: ΔROI={_delta(a['metrics'].get('roi'), b['metrics'].get('roi'))}pp   Δwin={_delta(a['metrics'].get('win_rate'), b['metrics'].get('win_rate'))}pp")
        lines.append(f"  C vs A: ΔROI={_delta(a['metrics'].get('roi'), c['metrics'].get('roi'))}pp   Δwin={_delta(a['metrics'].get('win_rate'), c['metrics'].get('win_rate'))}pp")

    return '\n'.join(lines)
