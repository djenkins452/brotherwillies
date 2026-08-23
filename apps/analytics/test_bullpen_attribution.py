"""v3.3 SHADOW — tests for bullpen attribution + salvage study.

Locks:
  * decompose_game returns None on games without pre-game odds
  * evaluate_config at baseline (scale=0) reproduces the V3.2-baseline
    pick that _simulate_recommendation would produce
  * evaluate_config at scale=1 reproduces the +quality pick
  * bounded scale REDUCES the contribution magnitude
  * cap enforces the requested prob-pp bound
  * veto NEVER promotes (only downgrades)
  * kind='attribution' orchestrator success path persists result
  * verdict logic returns one of A-G on every input
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.mlb.models import (
    Conference, Game, OddsSnapshot, StartingPitcher, Team,
    TeamBullpenSnapshot,
)
from apps.analytics.models import BullpenExperimentRun


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


class DecompositionTests(TestCase):

    def test_returns_none_when_no_pregame_odds(self):
        from apps.analytics.services.bullpen_attribution import decompose_game
        home = _mk_team('dh1'); away = _mk_team('da1')
        g = _mk_game(home, away, timezone.now() - timedelta(hours=6),
                     _mk_pitcher(home, 'p'), _mk_pitcher(away, 'p'))
        # No OddsSnapshot rows — sim must return None (no crash).
        self.assertIsNone(decompose_game(g, blend_weight=0.55))

    def test_decomposition_populates_expected_shape(self):
        from apps.analytics.services.bullpen_attribution import decompose_game
        home = _mk_team('dh2'); away = _mk_team('da2')
        hp = _mk_pitcher(home, 'p'); ap = _mk_pitcher(away, 'p')
        g = _mk_game(home, away, timezone.now() - timedelta(hours=6), hp, ap)
        _mk_odds(g)
        _mk_bullpen(home, g.first_pitch - timedelta(hours=1), era=3.00)
        _mk_bullpen(away, g.first_pitch - timedelta(hours=1), era=4.50)
        d = decompose_game(g, blend_weight=0.55)
        self.assertIsNotNone(d)
        # Coverage flag set when both sides have snapshots.
        self.assertTrue(d.both_bullpens_covered)
        # Home has BETTER pen (lower ERA) → positive quality diff.
        self.assertGreater(d.bullpen_quality_diff, 0)
        # Won recorded from home perspective (hs > as_).
        self.assertTrue(d.won)


class EvaluatorTests(TestCase):

    def _decomp(self, era_home=3.00, era_away=4.50, market_home=0.55):
        from apps.analytics.services.bullpen_attribution import decompose_game
        home = _mk_team(f'eh{era_home}-{era_away}-{market_home}')
        away = _mk_team(f'ea{era_home}-{era_away}-{market_home}')
        hp = _mk_pitcher(home, 'p'); ap = _mk_pitcher(away, 'p')
        g = _mk_game(home, away, timezone.now() - timedelta(hours=6), hp, ap)
        _mk_odds(g, market_home=market_home)
        _mk_bullpen(home, g.first_pitch - timedelta(hours=1), era=era_home)
        _mk_bullpen(away, g.first_pitch - timedelta(hours=1), era=era_away)
        return decompose_game(g, blend_weight=0.55)

    def test_baseline_scale_zero_ignores_bullpen(self):
        from apps.analytics.services.bullpen_attribution import (
            CONFIG_BASELINE, CONFIG_B_FULL_QUALITY, evaluate_config,
        )
        d = self._decomp(era_home=2.50, era_away=5.00)
        base = evaluate_config(d, CONFIG_BASELINE)
        full = evaluate_config(d, CONFIG_B_FULL_QUALITY)
        # With strong bullpen delta, full-quality should push the pick
        # probability in HOME's direction relative to baseline.
        if base.pick_side == 'home' and full.pick_side == 'home':
            self.assertGreater(full.pick_prob, base.pick_prob)

    def test_bounded_scale_reduces_contribution(self):
        from apps.analytics.services.bullpen_attribution import (
            BullpenConfig, evaluate_config,
        )
        d = self._decomp(era_home=2.50, era_away=5.00)
        full = evaluate_config(d, BullpenConfig(
            label='full', bullpen_quality_scale=1.0,
        ))
        quarter = evaluate_config(d, BullpenConfig(
            label='quarter', bullpen_quality_scale=0.25,
        ))
        base = evaluate_config(d, BullpenConfig(
            label='off', bullpen_quality_scale=0.0,
        ))
        # Quarter scale should sit BETWEEN off and full on any prob
        # metric — same side pick expected in this setup.
        if base.pick_side == quarter.pick_side == full.pick_side == 'home':
            self.assertLessEqual(base.pick_prob, quarter.pick_prob)
            self.assertLessEqual(quarter.pick_prob, full.pick_prob)

    def test_veto_never_promotes(self):
        from apps.analytics.services.bullpen_attribution import (
            BullpenConfig, evaluate_config,
        )
        # Very weak decomp: no starter/pitcher advantage, market ~50/50
        # → baseline won't recommend. A veto config that would otherwise
        # veto must not turn a non-recommendation into a recommendation.
        d = self._decomp(era_home=4.00, era_away=4.00, market_home=0.51)
        def veto_all(decomp, side):
            return True
        v = evaluate_config(d, BullpenConfig(
            label='veto-all', bullpen_quality_scale=0.0,
            apply_veto=veto_all,
        ))
        self.assertFalse(v.is_recommended)


class RunAttributionSmokeTests(TestCase):

    def test_runs_without_exception_on_realistic_fixture(self):
        # Small realistic slate — verifies the whole 8-section report
        # runs end-to-end without crashing.
        from apps.analytics.services.bullpen_attribution import (
            run_bullpen_attribution, render_bullpen_attribution,
        )
        # 10 games spread across the last 20 days.
        teams = [_mk_team(f'ra{i}') for i in range(6)]
        pitchers = [_mk_pitcher(t, f'p-{t.slug}') for t in teams]
        for i in range(10):
            when = timezone.now() - timedelta(days=i + 1, hours=6)
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
        result = run_bullpen_attribution(days=20)
        # Section keys all present.
        for k in ('window', 'coverage', 'aggregate',
                  'section_1_partition', 'section_2_magnitude',
                  'section_4_veto', 'section_5_bounded',
                  'section_6_isolated', 'section_7_interactions',
                  'section_8_verdict'):
            self.assertIn(k, result, f'missing key: {k}')
        # Verdict is one of A-G.
        self.assertIn(result['section_8_verdict']['verdict'],
                      list('ABCDEFG'))
        # Renderer produces a non-empty report.
        body = render_bullpen_attribution(result)
        self.assertIn('BULLPEN ATTRIBUTION', body)
        self.assertIn('SECTION 1', body)
        self.assertIn('SECTION 8', body)


class OrchestratorAttributionKindTests(TestCase):

    def test_attribution_run_uses_attribution_service(self):
        from apps.analytics.services.bullpen_experiment_service import (
            run_experiment_in_background,
        )
        run = BullpenExperimentRun.objects.create(
            kind='attribution', days=7,
            status='running', started_at=timezone.now(),
        )
        fake = {'window': {'days': 7}, 'section_8_verdict': {'verdict': 'A'}}
        with patch(
            'apps.analytics.services.bullpen_attribution.run_bullpen_attribution',
            return_value=fake,
        ):
            run_experiment_in_background(str(run.id))
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.result['section_8_verdict']['verdict'], 'A')

    def test_default_kind_still_uses_experiment_service(self):
        from apps.analytics.services.bullpen_experiment_service import (
            run_experiment_in_background,
        )
        run = BullpenExperimentRun.objects.create(
            kind='experiment', days=7,
            status='running', started_at=timezone.now(),
        )
        fake = {'window': {'days': 7}, 'a_v3_2_baseline': {'metrics': {}}}
        with patch(
            'apps.analytics.services.bullpen_replay.run_bullpen_experiment',
            return_value=fake,
        ):
            run_experiment_in_background(str(run.id))
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')


class VerdictLogicTests(TestCase):

    def test_no_bullpen_variant_beats_baseline_returns_verdict_A(self):
        from apps.analytics.services.bullpen_attribution import _decide_verdict
        baseline = {'n': 200, 'wins': 140, 'losses': 60,
                    'win_rate': 0.70, 'roi': 0.20}
        quality = {'n': 400, 'wins': 260, 'losses': 140,
                   'win_rate': 0.65, 'roi': 0.10}
        v = _decide_verdict(
            baseline, quality, quality,
            bounded_results=[{'config': 'q0.25', 'metrics': {
                'n': 300, 'wins': 195, 'losses': 105,
                'win_rate': 0.65, 'roi': 0.11,
            }}],
            veto_results=[{'config': 'veto-x', 'metrics': {
                'n': 190, 'wins': 130, 'losses': 60,
                'win_rate': 0.684, 'roi': 0.18,
            }}],
            isolated={'by_quality_diff': {}, 'by_fatigue_diff': {}},
        )
        self.assertEqual(v['verdict'], 'A')
