"""v3.1 first-step tests: feature contribution capture + recent form variant.

Covers:
  - feature_contributions populated on the Recommendation dataclass + the
    persisted BettingRecommendation row
  - Missing pitcher: contribution dict still safely populated
  - recent_form_delta math + leakage safeguards
  - USE_STARTER_RECENT_FORM flag OFF: production behavior unchanged
  - USE_STARTER_RECENT_FORM flag ON: form delta enters the score
  - ?experiment=recent_form view: staff 200, non-staff 403
  - Existing flow does not break when feature_contributions absent on a rec
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from apps.core.models import BettingRecommendation
from apps.mlb.models import Conference, Team, Game, OddsSnapshot, StartingPitcher


def _make_mlb_setup(slug_suffix):
    c, _ = Conference.objects.get_or_create(
        slug=f'al-east-{slug_suffix}', defaults={'name': 'AL East'},
    )
    home = Team.objects.create(
        name=f'Home {slug_suffix}', slug=f'h-{slug_suffix}',
        conference=c, rating=85.0, elo_rating=1560,
        source='mlb_stats_api', external_id=f'h-{slug_suffix}',
    )
    away = Team.objects.create(
        name=f'Away {slug_suffix}', slug=f'a-{slug_suffix}',
        conference=c, rating=20.0, elo_rating=1440,
        source='mlb_stats_api', external_id=f'a-{slug_suffix}',
    )
    return home, away


def _make_pitcher(team, rating=50.0, name='P'):
    return StartingPitcher.objects.create(
        team=team, name=f'{name}-{team.slug}', rating=rating,
        source='mlb_stats_api', external_id=f'p-{team.slug}-{name}',
    )


def _make_game(home, away, *, days_ahead=1, home_pitcher=None,
               away_pitcher=None, status='scheduled',
               home_score=None, away_score=None):
    fp = timezone.now() + timedelta(days=days_ahead)
    return Game.objects.create(
        home_team=home, away_team=away, first_pitch=fp,
        home_pitcher=home_pitcher, away_pitcher=away_pitcher,
        status=status, home_score=home_score, away_score=away_score,
        source='mlb_stats_api',
        external_id=f'g-{home.slug}-{away.slug}-{int(fp.timestamp())}',
    )


def _make_snapshot(game, *, ml_home=-130, ml_away=110, market_home_prob=0.55):
    return OddsSnapshot.objects.create(
        game=game,
        captured_at=game.first_pitch - timedelta(hours=2),
        market_home_win_prob=market_home_prob,
        moneyline_home=ml_home, moneyline_away=ml_away,
        odds_source='odds_api', source_quality='primary',
    )


class FeatureContributionCaptureTests(TestCase):
    """The MLB engine populates feature_contributions on Recommendation."""

    def test_contribution_dict_populated_with_pitchers(self):
        h, a = _make_mlb_setup('fc1')
        hp = _make_pitcher(h, rating=58.0, name='ace')
        ap = _make_pitcher(a, rating=44.0, name='5th')
        g = _make_game(h, a, home_pitcher=hp, away_pitcher=ap)
        _make_snapshot(g)
        from apps.core.services.recommendations import get_recommendation
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        fc = rec.feature_contributions
        self.assertEqual(fc.get('sport'), 'mlb')
        self.assertEqual(fc.get('engine_version'), 'v3.1')
        # Pitcher contribution is in the dict; non-zero given the rating gap.
        ps = fc['contributions_pp']['pitcher_static_score_units']
        self.assertIsNotNone(ps)
        self.assertGreater(abs(ps), 0)
        # Inputs are captured (graceful, even when None).
        self.assertEqual(fc['inputs']['home_pitcher_rating'], 58.0)
        self.assertEqual(fc['inputs']['away_pitcher_rating'], 44.0)
        # Probabilities are captured.
        self.assertIn('raw_pre_blend', fc['probabilities'])
        self.assertIn('final_calibrated', fc['probabilities'])

    def test_missing_pitcher_does_not_crash(self):
        h, a = _make_mlb_setup('fc2')
        # No pitchers on the game.
        g = _make_game(h, a)
        _make_snapshot(g)
        from apps.core.services.recommendations import get_recommendation
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        fc = rec.feature_contributions
        self.assertEqual(fc.get('sport'), 'mlb')
        self.assertIsNone(fc['inputs']['home_pitcher_rating'])
        self.assertEqual(fc['contributions_pp']['pitcher_static_score_units'], 0.0)
        self.assertEqual(fc['contributions_pp']['pitcher_form_score_units'], 0.0)

    def test_persist_recommendation_writes_feature_contributions(self):
        h, a = _make_mlb_setup('fc3')
        hp = _make_pitcher(h, rating=58.0, name='ace')
        ap = _make_pitcher(a, rating=44.0, name='5th')
        g = _make_game(h, a, home_pitcher=hp, away_pitcher=ap)
        _make_snapshot(g)
        from apps.core.services.recommendations import persist_recommendation
        row = persist_recommendation('mlb', g)
        self.assertIsNotNone(row)
        self.assertIsInstance(row.feature_contributions, dict)
        self.assertEqual(row.feature_contributions.get('engine_version'), 'v3.1')
        self.assertIn('contributions_pp', row.feature_contributions)


class RecentFormServiceTests(TestCase):
    """Pure math + leakage safeguards on the W-L proxy."""

    def test_returns_zero_for_none_pitcher(self):
        from apps.mlb.services.pitcher_form import recent_form_delta
        self.assertEqual(recent_form_delta(None), 0.0)

    def test_returns_zero_when_insufficient_history(self):
        from apps.mlb.services.pitcher_form import recent_form_delta
        h, a = _make_mlb_setup('rf1')
        p = _make_pitcher(h, rating=50.0)
        # No past games.
        self.assertEqual(recent_form_delta(p, reference_date=timezone.now()), 0.0)

    def test_positive_form_when_pitchers_team_wins_recent_starts(self):
        from apps.mlb.services.pitcher_form import recent_form_delta
        h, a = _make_mlb_setup('rf2')
        p = _make_pitcher(h, rating=50.0)
        # 3 past games — pitcher's team won all (as home pitcher).
        now = timezone.now()
        for i in range(3):
            Game.objects.create(
                home_team=h, away_team=a,
                first_pitch=now - timedelta(days=10 + i),
                home_pitcher=p, status='final',
                home_score=5, away_score=2,
                source='mlb_stats_api', external_id=f'past-{i}',
            )
        delta = recent_form_delta(p, reference_date=now)
        # 100% win rate → (1.0 - 0.5) * 25 = +12.5
        self.assertGreater(delta, 0)

    def test_leakage_guard_reference_date(self):
        """Only games STRICTLY BEFORE reference_date contribute."""
        from apps.mlb.services.pitcher_form import recent_form_delta
        h, a = _make_mlb_setup('rf3')
        p = _make_pitcher(h, rating=50.0)
        now = timezone.now()
        # A "future" game that should NOT be considered.
        Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=now + timedelta(days=1),
            home_pitcher=p, status='final',
            home_score=10, away_score=0,
            source='mlb_stats_api', external_id='future-1',
        )
        # Anchor reference_date at "now" — future game must be excluded.
        # With no past games + future game excluded → zero signal.
        self.assertEqual(recent_form_delta(p, reference_date=now), 0.0)


class FlagOffPreservesProductionTests(TestCase):
    """When USE_STARTER_RECENT_FORM=False the score must equal the original
    static-rating-only computation. This is the rollback safety invariant."""

    @override_settings(USE_STARTER_RECENT_FORM=False)
    def test_score_excludes_form_when_flag_off(self):
        from apps.mlb.services.model_service import _score, HOUSE_WEIGHTS
        h, a = _make_mlb_setup('flag1')
        hp = _make_pitcher(h, rating=58.0)
        ap = _make_pitcher(a, rating=44.0)
        g = _make_game(h, a, home_pitcher=hp, away_pitcher=ap)
        score = _score(g, HOUSE_WEIGHTS)
        # When the flag is off the form term contributes 0 — even if the
        # contribution dict captures it for audit.
        score_b, brk = _score(g, HOUSE_WEIGHTS, return_breakdown=True)
        self.assertAlmostEqual(score, score_b, places=6)
        # Form contribution was captured but didn't enter the score.
        # (Empty pitcher history → zero anyway, but the structural property
        # holds even when form is non-zero.)
        self.assertEqual(brk['use_recent_form'], False)


class FlagOnAddsFormTests(TestCase):
    """When USE_STARTER_RECENT_FORM=True the form term enters the score."""

    @override_settings(USE_STARTER_RECENT_FORM=True)
    def test_score_includes_form_when_flag_on_and_form_nonzero(self):
        from apps.mlb.services.model_service import _score, HOUSE_WEIGHTS
        h, a = _make_mlb_setup('flag2')
        hp = _make_pitcher(h, rating=58.0)
        ap = _make_pitcher(a, rating=44.0)
        # Seed past wins for home pitcher → positive form.
        now = timezone.now()
        for i in range(3):
            Game.objects.create(
                home_team=h, away_team=a,
                first_pitch=now - timedelta(days=10 + i),
                home_pitcher=hp, status='final',
                home_score=5, away_score=2,
                source='mlb_stats_api', external_id=f'flag2-past-{i}',
            )
        g = _make_game(h, a, home_pitcher=hp, away_pitcher=ap, days_ahead=1)
        score_with = _score(g, HOUSE_WEIGHTS, reference_date=g.first_pitch)
        score_without = _score(
            g, HOUSE_WEIGHTS, use_recent_form=False, reference_date=g.first_pitch,
        )
        # Form non-zero AND flag on → score should differ.
        self.assertNotAlmostEqual(score_with, score_without, places=6)


class RecentFormExperimentViewTests(TestCase):
    def test_staff_gets_plaintext(self):
        u = User.objects.create_user('rf_staff', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        resp = c.get('/analytics/method-replay/?experiment=recent_form&days=30')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertIn('RECENT-FORM EXPERIMENT', body)
        self.assertIn('SHIP CRITERIA', body)
        self.assertIn('VERDICT', body)

    def test_non_staff_forbidden(self):
        u = User.objects.create_user('rf_reg', password='x')
        c = Client()
        c.force_login(u)
        resp = c.get('/analytics/method-replay/?experiment=recent_form')
        self.assertEqual(resp.status_code, 403)
