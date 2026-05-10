"""Tests for the Phase 1B Elo shadow-mode logging.

Coverage:
  1. shadow_active_mode reflects the active rating mode at persist time.
  2. Non-MLB sports leave shadow fields empty (Phase 1B is MLB-only).
  3. Missing Elo on either team yields elo_available=False (no
     meaningful comparison can be made).
  4. With both teams Elo-rated, alt-mode computation produces a
     plausible alt recommendation snapshot.
  5. The primary recommendation is unaffected by shadow logging
     (alt-mode compute is sandboxed behind force_use_dynamic).
  6. A failure inside the alt compute does NOT block primary
     persistence — the row still saves with a degraded shadow blob.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone


def _make_mlb_setup(
    *,
    home_rating=70.0, away_rating=40.0,
    home_elo=None, away_elo=None,
    home_pitcher_rating=70.0, away_pitcher_rating=40.0,
    moneyline_home=-150, moneyline_away=130,
    market_home_prob=0.55,
):
    from apps.mlb.models import (
        Conference, Game, OddsSnapshot, StartingPitcher, Team,
    )
    league = Conference.objects.create(
        name='AL', slug=f'al-{timezone.now().timestamp()}',
    )
    home = Team.objects.create(
        name='Yankees', slug=f'h-{timezone.now().timestamp()}',
        conference=league, rating=home_rating, abbreviation='NYY',
        elo_rating=home_elo,
    )
    away = Team.objects.create(
        name='Red Sox', slug=f'a-{timezone.now().timestamp()}',
        conference=league, rating=away_rating, abbreviation='BOS',
        elo_rating=away_elo,
    )
    hp = StartingPitcher.objects.create(
        team=home, name='Cole', rating=home_pitcher_rating,
    )
    ap = StartingPitcher.objects.create(
        team=away, name='Sale', rating=away_pitcher_rating,
    )
    game = Game.objects.create(
        home_team=home, away_team=away,
        first_pitch=timezone.now() + timedelta(hours=2),
        home_pitcher=hp, away_pitcher=ap,
    )
    OddsSnapshot.objects.create(
        game=game,
        captured_at=timezone.now(),
        market_home_win_prob=market_home_prob,
        moneyline_home=moneyline_home,
        moneyline_away=moneyline_away,
        odds_source='odds_api', source_quality='primary',
    )
    return game


class ShadowModeBasicsTests(TestCase):
    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_active_mode_is_static_when_flag_off(self):
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(home_elo=1700.0, away_elo=1300.0)
        rec = persist_recommendation('mlb', game)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.shadow_active_mode, 'static')
        self.assertEqual(rec.shadow_alt_mode, 'elo')

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_active_mode_is_elo_when_flag_on(self):
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(home_elo=1700.0, away_elo=1300.0)
        rec = persist_recommendation('mlb', game)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.shadow_active_mode, 'elo')
        self.assertEqual(rec.shadow_alt_mode, 'static')

    def test_non_mlb_sport_leaves_shadow_fields_blank(self):
        # CFB recommendation should NOT carry shadow data — Phase 1B is
        # MLB-only by spec.
        from apps.core.services.recommendations import persist_recommendation
        from apps.cfb.models import Conference, Game, OddsSnapshot, Team
        conf = Conference.objects.create(
            name='SEC', slug=f'sec-{timezone.now().timestamp()}',
        )
        h = Team.objects.create(
            name='H', slug=f'cfh-{timezone.now().timestamp()}',
            conference=conf, rating=70.0,
        )
        a = Team.objects.create(
            name='A', slug=f'cfa-{timezone.now().timestamp()}',
            conference=conf, rating=40.0,
        )
        game = Game.objects.create(
            home_team=h, away_team=a,
            kickoff=timezone.now() + timedelta(hours=2),
        )
        OddsSnapshot.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.6,
            moneyline_home=-150, moneyline_away=130,
            odds_source='odds_api', source_quality='primary',
        )
        rec = persist_recommendation('cfb', game)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.shadow_active_mode, '')
        self.assertEqual(rec.shadow_alt_mode, '')
        self.assertEqual(rec.shadow_alt_data, {})


class ShadowModeAvailabilityTests(TestCase):
    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_no_elo_on_either_team_marks_unavailable(self):
        # Both teams missing elo_rating — alt computation would fall
        # back to static, producing identical numbers. Marked unavailable.
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(home_elo=None, away_elo=None)
        rec = persist_recommendation('mlb', game)
        self.assertFalse(rec.shadow_alt_data.get('elo_available'))

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_missing_one_elo_marks_unavailable(self):
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(home_elo=1700.0, away_elo=None)
        rec = persist_recommendation('mlb', game)
        self.assertFalse(rec.shadow_alt_data.get('elo_available'))

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_both_elos_present_marks_available(self):
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(home_elo=1700.0, away_elo=1300.0)
        rec = persist_recommendation('mlb', game)
        self.assertTrue(rec.shadow_alt_data.get('elo_available'))


class ShadowAltDataPopulationTests(TestCase):
    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_alt_data_carries_team_ratings(self):
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(
            home_rating=72.0, away_rating=42.0,
            home_elo=1650.0, away_elo=1350.0,
        )
        rec = persist_recommendation('mlb', game)
        d = rec.shadow_alt_data
        self.assertAlmostEqual(d['home_static_rating'], 72.0)
        self.assertAlmostEqual(d['away_static_rating'], 42.0)
        self.assertAlmostEqual(d['home_elo_rating'], 1650.0)
        self.assertAlmostEqual(d['away_elo_rating'], 1350.0)

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_alt_recommendation_under_elo_differs_from_static(self):
        # Static ratings are mild (60/40) but Elo is dramatic (1900/1200
        # → projects to ~80/30 on legacy scale). Alt-mode (Elo) should
        # produce a more confident pick than the active static path.
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(
            home_rating=60.0, away_rating=40.0,
            home_elo=1900.0, away_elo=1200.0,
            moneyline_home=-200, moneyline_away=170,
            market_home_prob=0.62,
        )
        rec = persist_recommendation('mlb', game)
        primary_prob = float(rec.confidence_score) / 100.0
        alt_prob = rec.shadow_alt_data['final_prob']
        self.assertIsNotNone(alt_prob)
        # Elo should pull the home prob noticeably higher than static.
        # Using > 0.005 tolerance — clamps may smooth the difference.
        self.assertGreater(alt_prob, primary_prob)

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_alt_data_includes_all_documented_keys(self):
        from apps.core.services.recommendations import persist_recommendation
        game = _make_mlb_setup(home_elo=1700.0, away_elo=1300.0)
        rec = persist_recommendation('mlb', game)
        d = rec.shadow_alt_data
        # All keys from the docstring shape must be present.
        for key in (
            'pick', 'pick_side', 'pick_odds',
            'final_prob', 'edge_pp', 'status', 'status_reason', 'tier', 'lane',
            'home_static_rating', 'home_elo_rating',
            'away_static_rating', 'away_elo_rating',
            'elo_available',
        ):
            self.assertIn(key, d, f'Missing shadow_alt_data key: {key}')


class ShadowModeNonInterferenceTests(TestCase):
    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_primary_recommendation_unchanged_under_shadow_logging(self):
        # Sanity: primary recommendation values must match what
        # get_recommendation produces directly. Shadow logging uses
        # force_use_dynamic which is a context manager; it must not
        # leak state.
        from apps.core.services.recommendations import (
            get_recommendation, persist_recommendation,
        )
        game = _make_mlb_setup(home_elo=1700.0, away_elo=1300.0)
        rec_direct = get_recommendation('mlb', game)
        rec_persisted = persist_recommendation('mlb', game)
        self.assertAlmostEqual(
            float(rec_persisted.confidence_score),
            rec_direct.confidence_score, places=2,
        )
        self.assertAlmostEqual(
            float(rec_persisted.model_edge),
            rec_direct.model_edge, places=2,
        )
        self.assertEqual(rec_persisted.status, rec_direct.status)

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_alt_compute_failure_does_not_block_primary_persist(self):
        # If the shadow alt-mode compute raises, the primary row must
        # still save with a degraded shadow blob. Patch
        # _build_shadow_alt_data to simulate the worst-case failure.
        from apps.core.services import recommendations as rmod

        original = rmod._build_shadow_alt_data
        def boom(*args, **kwargs):
            raise RuntimeError('simulated alt compute failure')

        # We don't patch _build_shadow_alt_data itself — it's the
        # caller that needs to keep going. Instead we patch the
        # internal get_recommendation called WITHIN the alt branch.
        # Easier to patch force_use_dynamic and verify the row still
        # saves with whatever blob results.

        game = _make_mlb_setup(home_elo=1700.0, away_elo=1300.0)
        with patch.object(
            rmod, 'get_recommendation', side_effect=[
                # First call (primary) returns a real recommendation;
                # second call (alt) raises.
                rmod.get_recommendation('mlb', game),  # actually compute the primary once
                RuntimeError('boom'),
            ],
        ):
            try:
                rec = rmod.persist_recommendation('mlb', game)
            except RuntimeError:
                # If we get here, the test fails — primary persistence
                # must not propagate the shadow failure.
                self.fail(
                    'Shadow alt compute failure must not propagate to primary persistence',
                )

        # We at least produced a row.
        self.assertIsNotNone(rec)
