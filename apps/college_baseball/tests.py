"""College Baseball tests: schema, prediction, provider normalization."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.college_baseball.models import Conference, Game, Team
from apps.college_baseball.services.model_service import (
    compute_data_confidence,
    compute_house_win_prob,
)


class CollegeBaseballSmokeTests(TestCase):
    def test_app_installed(self):
        from django.apps import apps
        self.assertTrue(apps.is_installed('apps.college_baseball'))


class CollegeBaseballPredictionModelTests(TestCase):
    def _mk_game(self, home_rating=50.0, away_rating=50.0, neutral=False):
        conf, _ = Conference.objects.get_or_create(slug='sec', defaults={'name': 'SEC'})
        home = Team.objects.create(
            name='H', slug='h', conference=conf, rating=home_rating,
            source='espn', external_id='1',
        )
        away = Team.objects.create(
            name='A', slug='a', conference=conf, rating=away_rating,
            source='espn', external_id='2',
        )
        return Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            neutral_site=neutral,
            source='espn', external_id='g1',
        )

    def test_missing_pitchers_gives_low_confidence(self):
        g = self._mk_game()
        self.assertEqual(compute_data_confidence(g), 'low')

    def test_hfa_is_lower_than_mlb(self):
        """CB HFA=2.0 is less aggressive than MLB HFA=2.5."""
        g = self._mk_game()  # equal teams, non-neutral
        p = compute_house_win_prob(g)
        # score = 0 + 0 + 2.0 -> sigmoid(2/15) ~= 0.533
        self.assertGreater(p, 0.52)
        self.assertLess(p, 0.55)

    def test_neutral_site_strips_hfa(self):
        g = self._mk_game(neutral=True)
        self.assertAlmostEqual(compute_house_win_prob(g), 0.5, places=3)


class CollegeBaseballScheduleProviderTests(TestCase):
    SAMPLE_EVENT = {
        'id': 'e789',
        'date': '2026-04-19T22:00:00Z',
        'status': {'type': {'state': 'pre'}},
        'competitions': [{
            'neutralSite': False,
            'competitors': [
                {
                    'homeAway': 'home',
                    'team': {'id': '99', 'displayName': 'LSU Tigers', 'abbreviation': 'LSU'},
                    'score': None,
                },
                {
                    'homeAway': 'away',
                    'team': {'id': '100', 'displayName': 'Texas Longhorns', 'abbreviation': 'TEX'},
                    'score': None,
                },
            ],
        }],
    }

    def test_normalize_extracts_expected_fields(self):
        from apps.datahub.providers.college_baseball.schedule_provider import (
            CollegeBaseballScheduleProvider,
        )
        with patch.object(CollegeBaseballScheduleProvider, '__init__', return_value=None):
            p = CollegeBaseballScheduleProvider()
            out = p.normalize([self.SAMPLE_EVENT])
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r['external_id'], 'e789')
        self.assertEqual(r['status'], 'scheduled')
        self.assertEqual(r['home_payload']['displayName'], 'LSU Tigers')
        self.assertEqual(r['away_payload']['displayName'], 'Texas Longhorns')

    def test_normalize_handles_missing_competitions(self):
        from apps.datahub.providers.college_baseball.schedule_provider import (
            CollegeBaseballScheduleProvider,
        )
        with patch.object(CollegeBaseballScheduleProvider, '__init__', return_value=None):
            p = CollegeBaseballScheduleProvider()
            out = p.normalize([{'id': 'x', 'competitions': []}])
        self.assertEqual(out, [])
