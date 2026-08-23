"""v3.3 SHADOW — tests for the final bullpen veto walk-forward.

Locks:
  * _apply_veto NEVER promotes (only downgrades a baseline-recommended pick)
  * _picked_side_bullpen_diff mirrors home/away convention
  * run_veto_walkforward returns well-formed dict on realistic fixture
  * ship criteria produce PASS when all 6 conditions met
  * ship criteria produce NO-GO when any condition fails
  * orchestrator dispatches on kind='veto_walkforward'
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import BullpenExperimentRun
from apps.mlb.models import (
    Conference, Game, OddsSnapshot, StartingPitcher, Team,
    TeamBullpenSnapshot,
)


def _mk_team(slug):
    c, _ = Conference.objects.get_or_create(
        slug=f'div-{slug}', defaults={'name': 'Div'},
    )
    return Team.objects.create(
        name=f'T-{slug}', slug=f't-{slug}', conference=c,
        rating=50.0, elo_rating=1500,
        source='mlb_stats_api', external_id=f'ext-{slug}',
        abbreviation=slug[:5].upper(),
    )


def _mk_pitcher(team, name):
    return StartingPitcher.objects.create(
        team=team, name=name, rating=50.0,
        source='mlb_stats_api', external_id=f'p-{team.slug}-{name}',
    )


def _mk_game(home, away, when, hp=None, ap=None, hs=4, as_=2):
    return Game.objects.create(
        home_team=home, away_team=away, first_pitch=when,
        home_pitcher=hp, away_pitcher=ap,
        home_score=hs, away_score=as_,
        status='final', source='mlb_stats_api',
        external_id=f'g-{home.slug}-{int(when.timestamp())}',
    )


def _mk_odds(game, ml_home=-120, ml_away=+100, market_home=0.55):
    OddsSnapshot.objects.create(
        game=game,
        captured_at=game.first_pitch - timedelta(hours=2),
        market_home_win_prob=market_home,
        moneyline_home=ml_home, moneyline_away=ml_away,
        odds_source='odds_api', source_quality='primary',
    )


def _mk_bullpen(team, as_of, era=3.50, top_avail=True):
    return TeamBullpenSnapshot.objects.create(
        team=team, as_of=as_of, bullpen_era=era,
        bullpen_whip=1.20, bullpen_k_per_9=9.0, bullpen_bb_per_9=3.0,
        bullpen_ip_last30=30.0,
        appearances_last_1_day=0, appearances_last_2_days=1,
        appearances_last_3_days=2,
        top_reliever_available=top_avail,
        source='mlb_stats_api', data_confidence='high',
    )


class VetoConstraintTests(TestCase):

    def test_veto_never_promotes_a_non_recommendation(self):
        from apps.analytics.services.bullpen_attribution import (
            EvaluatedVariant,
        )
        from apps.analytics.services.bullpen_veto_walkforward import (
            _apply_veto,
        )
        # Fabricated decomp — doesn't matter, the not-recommended check
        # fires before any bullpen inspection.
        class _D:
            bullpen_quality_diff = -20.0  # extremely bad pen
        base = EvaluatedVariant(
            pick_side='home', pick_odds=-110, pick_prob=0.55,
            edge_pp=3.0, status='not_recommended', lane='pass',
            is_recommended=False, tier='standard',
        )
        # Even a massive negative bullpen diff must NOT flip a
        # non-recommendation to True.
        self.assertFalse(_apply_veto(_D(), base, threshold=-6.0))

    def test_picked_side_bullpen_diff_mirrors_home_away(self):
        from apps.analytics.services.bullpen_veto_walkforward import (
            _picked_side_bullpen_diff,
        )
        class _D:
            bullpen_quality_diff = -8.0  # home 8 units WORSE than away
        # Picked home → -8 (worse). Picked away → +8 (better).
        self.assertEqual(_picked_side_bullpen_diff(_D(), 'home'), -8.0)
        self.assertEqual(_picked_side_bullpen_diff(_D(), 'away'), 8.0)

    def test_veto_fires_when_picked_side_diff_at_or_below_threshold(self):
        from apps.analytics.services.bullpen_attribution import (
            EvaluatedVariant,
        )
        from apps.analytics.services.bullpen_veto_walkforward import (
            _apply_veto,
        )
        class _D:
            bullpen_quality_diff = -6.5  # picked-home diff -6.5
        base = EvaluatedVariant(
            pick_side='home', pick_odds=-110, pick_prob=0.68,
            edge_pp=8.0, status='recommended', lane='core',
            is_recommended=True, tier='strong',
        )
        self.assertTrue(_apply_veto(_D(), base, threshold=-6.0))

    def test_veto_does_not_fire_above_threshold(self):
        from apps.analytics.services.bullpen_attribution import (
            EvaluatedVariant,
        )
        from apps.analytics.services.bullpen_veto_walkforward import (
            _apply_veto,
        )
        class _D:
            bullpen_quality_diff = -5.5  # picked-home diff -5.5 (above -6)
        base = EvaluatedVariant(
            pick_side='home', pick_odds=-110, pick_prob=0.68,
            edge_pp=8.0, status='recommended', lane='core',
            is_recommended=True, tier='strong',
        )
        self.assertFalse(_apply_veto(_D(), base, threshold=-6.0))


class RunWalkforwardSmokeTests(TestCase):

    def test_runs_end_to_end_on_realistic_fixture(self):
        from apps.analytics.services.bullpen_veto_walkforward import (
            render_veto_walkforward, run_veto_walkforward,
        )
        teams = [_mk_team(f'wf{i}') for i in range(6)]
        pitchers = [_mk_pitcher(t, f'p-{t.slug}') for t in teams]
        # 30 games over the past 90 days so folds have data.
        for i in range(30):
            when = timezone.now() - timedelta(days=i * 3 + 2, hours=6)
            home, away = teams[i % 3], teams[3 + (i % 3)]
            hp, ap = pitchers[i % 3], pitchers[3 + (i % 3)]
            g = _mk_game(home, away, when, hp, ap,
                         hs=(4 if i % 2 == 0 else 2),
                         as_=(2 if i % 2 == 0 else 4))
            _mk_odds(g)
            _mk_bullpen(home, g.first_pitch - timedelta(hours=1),
                        era=3.00 + (i % 3) * 0.4)
            _mk_bullpen(away, g.first_pitch - timedelta(hours=1),
                        era=4.20 - (i % 3) * 0.3)
        result = run_veto_walkforward(days=90, train_days=14, holdout_days=14,
                                      step_days=14)
        for k in ('window', 'fold_config', 'aggregate', 'folds',
                  'fold_classification_counts', 'ship_criteria',
                  'overall_verdict'):
            self.assertIn(k, result, msg=f'missing key: {k}')
        # Verdict is one of PASS / NO-GO.
        self.assertIn(result['overall_verdict'], ('PASS', 'NO-GO'))
        # Renderer succeeds.
        body = render_veto_walkforward(result)
        self.assertIn('VETO WALK-FORWARD', body)
        self.assertIn('SHIP CRITERIA', body)


class ShipCriteriaTests(TestCase):

    def _agg(self, n, wins, roi, clv=0.55):
        losses = n - wins
        return {
            'n': n, 'wins': wins, 'losses': losses,
            'win_rate': wins / n if n else None, 'roi': roi,
            'net_pl': 0.0,
            'wilson_ci_95': (0.0, 1.0),
            'positive_clv_rate': clv, 'clv_sample': n,
        }

    def test_all_pass_criteria_produces_PASS(self):
        from apps.analytics.services.bullpen_veto_walkforward import (
            _evaluate_ship_criteria,
        )
        agg_a = self._agg(200, 140, 0.20)          # baseline
        agg_b = self._agg(160, 116, 0.22)          # veto helps
        agg_v = self._agg(40, 24, -0.05)           # vetoed bets worse
        fold_results = [{'classification': 'helped'}] * 5 + \
                       [{'classification': 'neutral'}] * 2 + \
                       [{'classification': 'hurt'}] * 1
        criteria = _evaluate_ship_criteria(
            agg_a, agg_b, agg_v, fold_results, 80.0,
        )
        self.assertTrue(all(c['pass'] for c in criteria),
                        msg=f'failed: {[c for c in criteria if not c["pass"]]}')

    def test_low_retained_volume_fails(self):
        from apps.analytics.services.bullpen_veto_walkforward import (
            _evaluate_ship_criteria,
        )
        agg_a = self._agg(200, 140, 0.20)
        agg_b = self._agg(80, 60, 0.30)  # only 40% retained
        agg_v = self._agg(120, 80, -0.10)
        fold_results = [{'classification': 'helped'}] * 6
        criteria = _evaluate_ship_criteria(
            agg_a, agg_b, agg_v, fold_results, 40.0,
        )
        # Criterion 4 must fail.
        crit_4 = next(c for c in criteria if c['name'].startswith('4.'))
        self.assertFalse(crit_4['pass'])

    def test_win_rate_regression_fails(self):
        from apps.analytics.services.bullpen_veto_walkforward import (
            _evaluate_ship_criteria,
        )
        agg_a = self._agg(200, 140, 0.20)  # 70%
        agg_b = self._agg(160, 100, 0.10)  # 62.5%
        agg_v = self._agg(40, 40, 0.30)
        fold_results = [{'classification': 'neutral'}] * 6
        criteria = _evaluate_ship_criteria(
            agg_a, agg_b, agg_v, fold_results, 80.0,
        )
        crit_1 = next(c for c in criteria if c['name'].startswith('1.'))
        self.assertFalse(crit_1['pass'])

    def test_fewer_helped_than_hurt_fails(self):
        from apps.analytics.services.bullpen_veto_walkforward import (
            _evaluate_ship_criteria,
        )
        agg_a = self._agg(200, 140, 0.20)
        agg_b = self._agg(160, 116, 0.22)
        agg_v = self._agg(40, 24, -0.05)
        fold_results = [{'classification': 'helped'}] * 2 + \
                       [{'classification': 'neutral'}] * 1 + \
                       [{'classification': 'hurt'}] * 5
        criteria = _evaluate_ship_criteria(
            agg_a, agg_b, agg_v, fold_results, 80.0,
        )
        crit_5 = next(c for c in criteria if c['name'].startswith('5.'))
        self.assertFalse(crit_5['pass'])


class OrchestratorKindDispatchTests(TestCase):

    def test_veto_walkforward_kind_routes_to_veto_service(self):
        from apps.analytics.services.bullpen_experiment_service import (
            run_experiment_in_background,
        )
        run = BullpenExperimentRun.objects.create(
            kind='veto_walkforward', days=30,
            status='running', started_at=timezone.now(),
        )
        fake = {
            'window': {'days': 30, 'veto_threshold_units': -6.0,
                       'from': '2026-07-24', 'to': '2026-08-22',
                       'games_evaluable': 0, 'decomps_generated': 0,
                       'decomp_errors': 0},
            'fold_config': {'train_days': 30, 'holdout_days': 14,
                            'step_days': 14, 'n_folds': 0},
            'aggregate': {
                'a_baseline': {'n': 0},
                'b_with_veto': {'n': 0},
                'vetoed': {'n': 0},
                'retained_volume_pct': None,
                'delta_win_rate': None,
                'delta_roi': None,
                'delta_positive_clv_rate': None,
            },
            'folds': [],
            'fold_classification_counts': {'helped': 0, 'neutral': 0,
                                           'hurt': 0, 'no_data': 0},
            'ship_criteria': [],
            'overall_verdict': 'NO-GO',
        }
        with patch(
            'apps.analytics.services.bullpen_veto_walkforward.run_veto_walkforward',
            return_value=fake,
        ):
            run_experiment_in_background(str(run.id))
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.result['overall_verdict'], 'NO-GO')
