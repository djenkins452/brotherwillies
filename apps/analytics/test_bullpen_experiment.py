"""v3.3 SHADOW — tests for the async bullpen experiment infrastructure.

Locks:
  * BullpenExperimentRun model + elapsed_seconds
  * Access control on status page + POST trigger
  * Concurrency guard
  * Orchestrator: successful path writes result + status='completed'
  * Orchestrator: exception path writes failure_summary + status='failed'
  * _json_safe coerces dates/dataclasses/sets so the JSONField survives
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import BullpenExperimentRun


class BullpenExperimentRunModelTests(TestCase):

    def test_defaults(self):
        r = BullpenExperimentRun.objects.create()
        self.assertEqual(r.status, 'pending')
        self.assertEqual(r.days, 180)
        self.assertEqual(r.blend_weight, 0.55)
        self.assertEqual(r.progress_current, 0)
        self.assertEqual(r.elapsed_seconds, 0)

    def test_elapsed_seconds_running(self):
        r = BullpenExperimentRun.objects.create(
            started_at=timezone.now() - timedelta(seconds=17),
        )
        self.assertGreaterEqual(r.elapsed_seconds, 16)


class AccessControlTests(TestCase):

    def _staff(self):
        return User.objects.create_user('sx', 'sx@x.com', 'pw', is_staff=True)

    def _regular(self):
        return User.objects.create_user('rx', 'rx@x.com', 'pw')

    def test_status_page_requires_staff(self):
        c = Client()
        r = c.get(reverse('analytics:bullpen_experiment'))
        self.assertIn(r.status_code, (302, 403))
        c.force_login(self._regular())
        self.assertEqual(
            c.get(reverse('analytics:bullpen_experiment')).status_code, 403,
        )
        c.force_login(self._staff())
        self.assertEqual(
            c.get(reverse('analytics:bullpen_experiment')).status_code, 200,
        )

    def test_trigger_requires_staff_and_post(self):
        c = Client()
        c.force_login(self._regular())
        r = c.post(reverse('analytics:trigger_bullpen_experiment'))
        self.assertEqual(r.status_code, 403)
        c.force_login(self._staff())
        # GET is 405 (require_POST decorator).
        r = c.get(reverse('analytics:trigger_bullpen_experiment'))
        self.assertEqual(r.status_code, 405)


class ConcurrencyGuardTests(TestCase):

    def test_second_trigger_rejected_when_run_active(self):
        staff = User.objects.create_user('sc', 'sc@x.com', 'pw', is_staff=True)
        c = Client()
        c.force_login(staff)
        # Simulate an already-running row.
        BullpenExperimentRun.objects.create(
            days=180, status='running', started_at=timezone.now(),
        )
        r = c.post(reverse('analytics:trigger_bullpen_experiment'),
                   data={'days': '60', 'blend': '0.55'})
        # Redirect back with warning; no new row.
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BullpenExperimentRun.objects.count(), 1)


class OrchestratorSuccessTests(TestCase):

    def test_successful_run_completes_and_persists_result(self):
        from apps.analytics.services.bullpen_experiment_service import (
            run_experiment_in_background,
        )
        run = BullpenExperimentRun.objects.create(
            days=7, status='running', started_at=timezone.now(),
        )
        # Patch run_bullpen_experiment to a fast deterministic result.
        fake = {
            'window': {
                'days': 7, 'from': date(2026, 8, 15), 'to': date(2026, 8, 21),
                'blend_weight': 0.55, 'games_evaluable': 0,
            },
            'coverage': {'total_games': 0, 'both_covered': 0,
                         'home_only': 0, 'away_only': 0, 'neither': 0,
                         'both_covered_pct': 0.0},
            'coverage_ok': False,
            'coverage_ship_criterion_pct': 80.0,
            'a_v3_2_baseline': {'metrics': {'count': 0, 'wins': 0, 'losses': 0,
                                            'win_rate': None, 'roi': None,
                                            'positive_clv_rate': None},
                                'count': 0, 'sim_errors': {'errors': 0,
                                                           'categories': {},
                                                           'none_returns': 0,
                                                           'total_games_attempted': 0}},
            'b_plus_quality': {'metrics': {'count': 0, 'wins': 0, 'losses': 0,
                                           'win_rate': None, 'roi': None,
                                           'positive_clv_rate': None},
                               'count': 0, 'sim_errors': {'errors': 0,
                                                          'categories': {},
                                                          'none_returns': 0,
                                                          'total_games_attempted': 0}},
            'c_plus_quality_and_fatigue': {'metrics': {'count': 0, 'wins': 0,
                                                       'losses': 0,
                                                       'win_rate': None,
                                                       'roi': None,
                                                       'positive_clv_rate': None},
                                           'count': 0, 'sim_errors': {'errors': 0,
                                                                       'categories': {},
                                                                       'none_returns': 0,
                                                                       'total_games_attempted': 0}},
            'data_ok': False,
            'sim_populations': {'a': 0, 'b': 0, 'c': 0},
            'populations_match': True,
        }
        with patch(
            'apps.analytics.services.bullpen_replay.run_bullpen_experiment',
            return_value=fake,
        ):
            run_experiment_in_background(str(run.id))
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertIsNotNone(run.finished_at)
        # result JSONField must have persisted the fake payload (dates
        # coerced to isoformat strings by _json_safe).
        self.assertEqual(run.result['window']['days'], 7)
        self.assertEqual(run.result['window']['from'], '2026-08-15')


class OrchestratorFailureTests(TestCase):

    def test_exception_marks_run_failed_with_summary(self):
        from apps.analytics.services.bullpen_experiment_service import (
            run_experiment_in_background,
        )
        run = BullpenExperimentRun.objects.create(
            days=7, status='running', started_at=timezone.now(),
        )
        with patch(
            'apps.analytics.services.bullpen_replay.run_bullpen_experiment',
            side_effect=RuntimeError('simulated boom'),
        ):
            run_experiment_in_background(str(run.id))
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('simulated boom', run.failure_summary)
        self.assertIn('RuntimeError', run.error_message)


class ProgressVariantLengthTests(TestCase):
    """Regression lock for the 2026-08-23 Railway failure.

    BullpenExperimentRun.progress_variant was CharField(max_length=8)
    — sized only for the A/B/C experiment variant labels. The
    attribution run writes phase names like 'decompose' (9 chars) to
    it, which threw psycopg2 DataError('value too long for type
    character varying(8)') on Railway Postgres and killed the run at
    the first progress-save.

    These tests round-trip the actual production values through the
    database so the field can never silently drift too narrow again."""

    def test_saves_attribution_progress_variant_decompose(self):
        """The exact value that produced the Railway DataError."""
        from apps.analytics.models import BullpenExperimentRun
        run = BullpenExperimentRun.objects.create(
            kind='attribution', days=7,
            status='running', started_at=timezone.now(),
        )
        # The failing production write:
        run.progress_variant = 'decompose'
        run.save(update_fields=['progress_variant'])
        run.refresh_from_db()
        self.assertEqual(run.progress_variant, 'decompose')

    def test_saves_experiment_variant_labels(self):
        """The A/B/C values previously supported."""
        from apps.analytics.models import BullpenExperimentRun
        for label in ('A', 'B', 'C'):
            run = BullpenExperimentRun.objects.create(
                kind='experiment', days=7,
                status='running', started_at=timezone.now(),
                progress_variant=label,
            )
            run.refresh_from_db()
            self.assertEqual(run.progress_variant, label)

    def test_field_width_accommodates_reasonable_phase_names(self):
        """Future diagnostic run types may publish descriptive phase
        names like 'baseline_sim', 'compute_metrics', 'render_report'.
        Field should have room without another migration."""
        from apps.analytics.models import BullpenExperimentRun
        for phase in ('baseline_sim', 'compute_metrics', 'render_report',
                      'evaluate_configs', 'aggregating_results'):
            run = BullpenExperimentRun.objects.create(
                kind='attribution', days=7,
                status='running', started_at=timezone.now(),
                progress_variant=phase,
            )
            run.refresh_from_db()
            self.assertEqual(run.progress_variant, phase)


class ChoiceFieldWidthInvariantTests(TestCase):
    """Invariant: for every choice-backed CharField on the bullpen
    operational models, max_length must accommodate every legal choice
    value. This catches the exact class of defect that killed the
    initial attribution run (a field sized for one call site but
    written to by another with longer values).

    NOTE: `progress_variant` is NOT choice-backed — it's a
    free-form CharField whose values come from progress callbacks.
    Its length is exercised by `ProgressVariantLengthTests` above."""

    def _assert_field_fits_all_choices(self, model, field_name):
        f = model._meta.get_field(field_name)
        if not f.choices:
            return
        longest = max(len(str(c[0])) for c in f.choices)
        self.assertGreaterEqual(
            f.max_length, longest,
            msg=(
                f'{model.__name__}.{field_name}: max_length='
                f'{f.max_length} < longest choice value ({longest} chars)'
            ),
        )

    def test_bullpen_backfill_run_choice_widths(self):
        from apps.analytics.models import BullpenBackfillRun
        for name in ('kind', 'status', 'phase'):
            self._assert_field_fits_all_choices(BullpenBackfillRun, name)

    def test_bullpen_experiment_run_choice_widths(self):
        from apps.analytics.models import BullpenExperimentRun
        for name in ('kind', 'status'):
            self._assert_field_fits_all_choices(BullpenExperimentRun, name)


class JsonSafeTests(TestCase):

    def test_coerces_dates_and_sets(self):
        from apps.analytics.services.bullpen_experiment_service import _json_safe
        payload = {
            'when': date(2026, 8, 15),
            'tags': {'a', 'b'},
            'nested': {'deeper': [date(2026, 1, 1), 'x']},
        }
        safe = _json_safe(payload)
        self.assertEqual(safe['when'], '2026-08-15')
        self.assertEqual(safe['tags'], ['a', 'b'])
        self.assertEqual(safe['nested']['deeper'], ['2026-01-01', 'x'])
