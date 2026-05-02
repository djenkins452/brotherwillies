"""Tests for the MONEYLINE_ONLY_MODE master switch.

Covers:
  - The flag itself + its AND-composition with the per-feature flags
  - Server-side gates at the bulk_actions service entry points
  - View-level gates (place_bet manual, bulk_place_recommended dispatch)
  - Analytics + System Tuning queryset filters
  - Reversibility — flipping the master flag OFF restores full behavior
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from apps.core.config import (
    is_moneyline_only_mode,
    is_spread_total_enabled,
    is_spread_total_leans_enabled,
    is_spread_total_recommendations_enabled,
)


# --- Flag composition --------------------------------------------------------

class FlagCompositionTests(TestCase):
    """Master switch wins regardless of per-feature flag values."""

    @override_settings(MONEYLINE_ONLY_MODE=True)
    def test_master_on_silences_all_per_feature_flags(self):
        with override_settings(
            SPREAD_TOTAL_SIGNALS_ENABLED=True,
            SPREAD_TOTAL_LEANS_ENABLED=True,
            SPREAD_TOTAL_RECOMMENDATIONS_ENABLED=True,
        ):
            self.assertTrue(is_moneyline_only_mode())
            self.assertFalse(is_spread_total_enabled())
            self.assertFalse(is_spread_total_leans_enabled())
            self.assertFalse(is_spread_total_recommendations_enabled())

    @override_settings(
        MONEYLINE_ONLY_MODE=False,
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=False,
        SPREAD_TOTAL_RECOMMENDATIONS_ENABLED=False,
    )
    def test_master_off_respects_per_feature_flags(self):
        self.assertFalse(is_moneyline_only_mode())
        self.assertTrue(is_spread_total_enabled())
        self.assertFalse(is_spread_total_leans_enabled())
        self.assertFalse(is_spread_total_recommendations_enabled())

    @override_settings(
        MONEYLINE_ONLY_MODE=False,
        SPREAD_TOTAL_SIGNALS_ENABLED=False,
    )
    def test_master_off_per_feature_off_remains_off(self):
        self.assertFalse(is_spread_total_enabled())


# --- Service-level gates -----------------------------------------------------

@override_settings(MONEYLINE_ONLY_MODE=True)
class BulkServicesGatedTests(TestCase):
    """place_bulk_proven_spread_bets / _total_bets must short-circuit
    BEFORE any DB work and return the structured 'blocked' summary."""

    def setUp(self):
        self.user = User.objects.create_user('bulkguard', password='x')

    def test_proven_spread_returns_blocked_summary(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_spread_bets
        summary = place_bulk_proven_spread_bets(self.user)
        self.assertEqual(summary['placed'], 0)
        self.assertEqual(summary['blocked'], 'moneyline_only_mode')
        self.assertEqual(summary['bet_type'], 'spread')

    def test_proven_total_returns_blocked_summary(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_total_bets
        summary = place_bulk_proven_total_bets(self.user)
        self.assertEqual(summary['placed'], 0)
        self.assertEqual(summary['blocked'], 'moneyline_only_mode')
        self.assertEqual(summary['bet_type'], 'total')


@override_settings(MONEYLINE_ONLY_MODE=False)
class BulkServicesUngatedTests(TestCase):
    """When the master switch is OFF, the gate disappears — services
    proceed to their normal eligibility-scan path. Confirms reversibility."""

    def setUp(self):
        self.user = User.objects.create_user('bulkfree', password='x')

    def test_proven_spread_no_block_key(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_spread_bets
        summary = place_bulk_proven_spread_bets(self.user)
        # Empty DB — placed=0 but NOT a blocked summary.
        self.assertNotIn('blocked', summary)

    def test_proven_total_no_block_key(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_total_bets
        summary = place_bulk_proven_total_bets(self.user)
        self.assertNotIn('blocked', summary)


# --- View-level gates --------------------------------------------------------

@override_settings(
    MONEYLINE_ONLY_MODE=True,
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
)
class PlaceBetViewGateTests(TestCase):
    """The manual placement endpoint rejects non-moneyline bet_type with 400."""

    def setUp(self):
        self.user = User.objects.create_user('placeguard', password='x')
        self.client = Client()
        self.client.force_login(self.user)

    def _post(self, bet_type):
        import json
        return self.client.post(
            '/mockbets/place/',
            json.dumps({
                'sport': 'cfb', 'bet_type': bet_type,
                'selection': 'X', 'odds_american': -110, 'stake_amount': '100',
            }),
            content_type='application/json',
        )

    def test_spread_blocked_with_400(self):
        resp = self._post('spread')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get('blocked'), 'moneyline_only_mode')

    def test_total_blocked_with_400(self):
        resp = self._post('total')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get('blocked'), 'moneyline_only_mode')

    def test_moneyline_not_blocked(self):
        # Validation may still fail downstream (no real game), but the
        # block-by-bet-type check must NOT fire on moneyline.
        resp = self._post('moneyline')
        # Either 200 success or a different validation error — but never
        # the moneyline_only_mode block.
        body = resp.json()
        self.assertNotEqual(body.get('blocked'), 'moneyline_only_mode')


@override_settings(
    MONEYLINE_ONLY_MODE=True,
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
)
class BulkPlaceRecommendedViewGateTests(TestCase):
    """The bulk-placement view rejects bet_type=spread/total with 400."""

    def setUp(self):
        self.user = User.objects.create_user('bulkviewguard', password='x')
        self.client = Client()
        self.client.force_login(self.user)

    def test_bulk_spread_blocked(self):
        resp = self.client.post('/mockbets/bulk/place-recommended/?bet_type=spread')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get('blocked'), 'moneyline_only_mode')

    def test_bulk_total_blocked(self):
        resp = self.client.post('/mockbets/bulk/place-recommended/?bet_type=total')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get('blocked'), 'moneyline_only_mode')

    def test_bulk_moneyline_not_blocked_at_view_layer(self):
        # No games in DB → service returns placed=0, but the moneyline
        # path must NOT hit the gate.
        resp = self.client.post('/mockbets/bulk/place-recommended/?bet_type=moneyline')
        body = resp.json()
        self.assertNotEqual(body.get('blocked'), 'moneyline_only_mode')


# --- Analytics queryset filter ----------------------------------------------

@override_settings(MONEYLINE_ONLY_MODE=True)
class SystemTuningQuerysetFilterTests(TestCase):
    """system_tuning_view filters bets to bet_type='moneyline' at the
    query layer when the master switch is on."""

    def setUp(self):
        self.user = User.objects.create_user('tuner', password='x', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def _bet(self, bet_type, result='loss'):
        from apps.mockbets.models import MockBet
        return MockBet.objects.create(
            user=self.user, sport='cfb', bet_type=bet_type,
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            result=result,
        )

    @override_settings(STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    })
    def test_system_tuning_excludes_spread_and_total(self):
        # 1 ML bet, 1 spread bet, 1 total bet.
        self._bet('moneyline')
        self._bet('spread')
        self._bet('total')
        resp = self.client.get('/mockbets/system-tuning/')
        self.assertEqual(resp.status_code, 200)
        # Overall settled count reflects only the moneyline row.
        self.assertEqual(resp.context['overall']['total_bets'], 1)


# --- Reversibility -----------------------------------------------------------

@override_settings(MONEYLINE_ONLY_MODE=False)
class ReversibilityTests(TestCase):
    """Flipping the master flag OFF restores full behavior — the central-
    helper architecture is the contract that makes this safe."""

    def setUp(self):
        self.user = User.objects.create_user('rev', password='x', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def _bet(self, bet_type):
        from apps.mockbets.models import MockBet
        return MockBet.objects.create(
            user=self.user, sport='cfb', bet_type=bet_type,
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            result='loss',
        )

    @override_settings(STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    })
    def test_system_tuning_includes_spread_total_when_master_off(self):
        self._bet('moneyline')
        self._bet('spread')
        self._bet('total')
        resp = self.client.get('/mockbets/system-tuning/')
        self.assertEqual(resp.status_code, 200)
        # Master OFF → all 3 bets reach the page.
        self.assertEqual(resp.context['overall']['total_bets'], 3)


# --- Context processor -------------------------------------------------------

@override_settings(
    MONEYLINE_ONLY_MODE=True,
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
)
class ContextProcessorTests(TestCase):
    """The feature_flags context processor exposes MONEYLINE_ONLY_MODE
    to every template — verify by hitting a rendering view."""

    def test_flag_in_template_context(self):
        user = User.objects.create_user('ctx', password='x', is_staff=True)
        c = Client()
        c.force_login(user)
        resp = c.get('/mockbets/system-tuning/')
        self.assertEqual(resp.status_code, 200)
        # The processor injects MONEYLINE_ONLY_MODE; assert it's truthy.
        self.assertTrue(resp.context['MONEYLINE_ONLY_MODE'])
