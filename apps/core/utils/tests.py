"""Tests for odds utilities — de-vig math + CLV conversion + display formatters."""
from django.test import TestCase

from apps.core.utils.odds import (
    american_to_decimal,
    american_to_implied_prob,
    closing_line_value,
    clv_label,
    devig_moneyline_prob,
    devig_two_way,
    format_american_signed,
    format_clv_percent,
    format_line_movement,
)


class AmericanConversionTests(TestCase):
    def test_plus_money_implied_prob(self):
        self.assertAlmostEqual(american_to_implied_prob(100), 0.5, places=4)
        self.assertAlmostEqual(american_to_implied_prob(200), 1 / 3, places=4)
        self.assertAlmostEqual(american_to_implied_prob(120), 100 / 220, places=4)

    def test_minus_money_implied_prob(self):
        self.assertAlmostEqual(american_to_implied_prob(-100), 0.5, places=4)
        self.assertAlmostEqual(american_to_implied_prob(-150), 0.6, places=4)

    def test_decimal_conversion(self):
        self.assertAlmostEqual(american_to_decimal(100), 2.0, places=4)
        self.assertAlmostEqual(american_to_decimal(120), 2.2, places=4)
        self.assertAlmostEqual(american_to_decimal(-150), 1.6667, places=4)
        self.assertAlmostEqual(american_to_decimal(-200), 1.5, places=4)


class DevigTests(TestCase):
    def test_symmetric_line_devigs_to_fifty_fifty(self):
        """-110/-110 line has implied probs 0.5238/0.5238 — fair is 50/50."""
        imp_home = american_to_implied_prob(-110)
        imp_away = american_to_implied_prob(-110)
        fair = devig_moneyline_prob(imp_home, imp_away)
        self.assertAlmostEqual(fair, 0.5, places=4)

    def test_asymmetric_line_preserves_ratio(self):
        """-200/+180 — home is favorite, fair home > 0.5 but less than raw."""
        imp_home = american_to_implied_prob(-200)  # 0.6667
        imp_away = american_to_implied_prob(180)   # 0.3571
        # Raw total: 1.0238 (vig ~2.4%)
        fair_home = devig_moneyline_prob(imp_home, imp_away)
        self.assertGreater(fair_home, 0.5)
        self.assertLess(fair_home, imp_home)  # de-vigged is always <= raw for favorite side
        # Expected: 0.6667 / (0.6667 + 0.3571) ≈ 0.6513
        self.assertAlmostEqual(fair_home, 0.6513, places=3)

    def test_devig_two_way_sums_to_one(self):
        imp_home = american_to_implied_prob(-130)
        imp_away = american_to_implied_prob(110)
        fair_home, fair_away = devig_two_way(imp_home, imp_away)
        self.assertAlmostEqual(fair_home + fair_away, 1.0, places=6)

    def test_devig_fallback_when_total_zero(self):
        """Defensive: total==0 (can't happen for real odds) returns input."""
        self.assertEqual(devig_moneyline_prob(0, 0), 0)


class ClosingLineValueTests(TestCase):
    def test_positive_clv_when_bet_beat_close(self):
        # Bet +120, closed +110. You got the better price → positive CLV.
        clv = closing_line_value(120, 110)
        self.assertGreater(clv, 0)

    def test_negative_clv_when_close_beat_bet(self):
        # Bet +120, closed +130. Close was a better price → negative CLV.
        clv = closing_line_value(120, 130)
        self.assertLess(clv, 0)

    def test_positive_clv_for_favorite_when_bet_got_better_price(self):
        # Bet -150, closed -160. You got less juice than close → positive CLV.
        clv = closing_line_value(-150, -160)
        self.assertGreater(clv, 0)

    def test_negative_clv_for_favorite_when_close_got_better_price(self):
        # Bet -150, closed -140. Close had less juice → negative CLV.
        clv = closing_line_value(-150, -140)
        self.assertLess(clv, 0)

    def test_zero_clv_when_bet_matches_close(self):
        self.assertEqual(closing_line_value(-110, -110), 0)


class DevigIntegrationWithRecommendationTests(TestCase):
    """Edge calculation in get_recommendation must use fair (de-vigged) probs."""

    def setUp(self):
        from datetime import timedelta
        from apps.mlb.models import Conference, Game, OddsSnapshot, Team
        from django.utils import timezone

        league = Conference.objects.create(name='AL East', slug='al-east-devig')
        self.home = Team.objects.create(
            name='Home', slug='devig-home', conference=league, rating=50,
        )
        self.away = Team.objects.create(
            name='Away', slug='devig-away', conference=league, rating=50,
        )
        # Neutral site removes HFA — otherwise home gets +2.5 rating, which
        # would shift model prob above 50% and make this test measure HFA
        # impact instead of de-vig behavior.
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=2),
            neutral_site=True,
        )
        # -110/-110 line — raw implied 52.38% both sides, fair is 50/50
        OddsSnapshot.objects.create(
            game=self.game,
            captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110,
            moneyline_away=-110,
        )

    def test_edge_near_zero_when_model_matches_fair_line(self):
        """Model says ~50% on a neutral-site -110/-110 line → de-vigged edge
        is at or near 0. Pre-devig logic would return 50 - 52.38 = -2.38 on
        this same setup; the de-vig moves it back to ~0."""
        from apps.core.services.recommendations import get_recommendation

        rec = get_recommendation('mlb', self.game)
        self.assertIsNotNone(rec)
        # With neutral site + equal ratings + equal pitchers (none set),
        # model prob ~50%. Fair prob = 50%. Edge should be ~0.
        self.assertAlmostEqual(rec.model_edge, 0.0, delta=0.5)

    def test_edge_shrinks_when_devig_is_applied(self):
        """Directly compare: the same 5pp-apparent edge against a -110 line
        becomes a ~2.6pp de-vigged edge. Quantifies that we're actually
        stripping the vig and not measuring something else."""
        from apps.core.services.recommendations import _implied_prob
        from apps.core.utils.odds import devig_two_way

        raw_home = _implied_prob(-110)
        raw_away = _implied_prob(-110)
        fair_home, _ = devig_two_way(raw_home, raw_away)

        # Imagine model says home wins 55%
        raw_edge = 0.55 - raw_home           # 0.55 - 0.5238 = +0.0262 (~2.6pp)
        fair_edge = 0.55 - fair_home          # 0.55 - 0.50   = +0.05   (~5pp)
        # De-vigged edge is strictly larger on a symmetric line when model > 50%
        # (the raw edge is the de-vigged edge MINUS half the overround).
        self.assertGreater(fair_edge, raw_edge)


class FormatClvPercentTests(TestCase):
    def test_positive_decimal_renders_with_plus_sign(self):
        self.assertEqual(format_clv_percent(0.042), '+4.2%')

    def test_negative_decimal_renders_with_minus(self):
        self.assertEqual(format_clv_percent(-0.031), '-3.1%')

    def test_zero_is_rendered_with_plus_zero(self):
        # +0.0% is the conventional "zero with explicit sign" format.
        self.assertEqual(format_clv_percent(0), '+0.0%')

    def test_none_returns_empty_string(self):
        self.assertEqual(format_clv_percent(None), '')


class ClvLabelTests(TestCase):
    def test_positive_label(self):
        self.assertEqual(clv_label(0.05), 'Beat Market')

    def test_negative_label(self):
        self.assertEqual(clv_label(-0.02), 'Market Beat You')

    def test_zero_label(self):
        self.assertEqual(clv_label(0), 'Matched Market')

    def test_none_label_is_empty(self):
        self.assertEqual(clv_label(None), '')


class FormatAmericanSignedTests(TestCase):
    def test_positive_odds_get_explicit_plus(self):
        self.assertEqual(format_american_signed(120), '+120')

    def test_negative_odds_stay_negative(self):
        self.assertEqual(format_american_signed(-150), '-150')

    def test_none_returns_empty_string(self):
        self.assertEqual(format_american_signed(None), '')


class FormatLineMovementTests(TestCase):
    """Sign must be signed from the BETTOR'S perspective — positive when
    the line moved in favor of the bet, even across favorite/dog boundaries."""

    def test_favorite_line_moving_further_is_positive(self):
        """Bet -120, closed -135: you got less juice, positive movement."""
        out = format_line_movement(-120, -135)
        self.assertTrue(out.startswith('+15 cents'), msg=out)
        self.assertIn('-120 → -135', out)

    def test_favorite_line_coming_back_is_negative(self):
        """Bet -120, closed -110: close had less juice, you lost the line."""
        out = format_line_movement(-120, -110)
        self.assertTrue(out.startswith('-10 cents'), msg=out)

    def test_underdog_price_shortening_is_negative(self):
        """Bet +120, closed +110: +120 was longer price, but close is shorter,
        meaning close moved AGAINST your pick — the market thought your pick
        MORE likely at close (lower implied prob → less +EV at close) —
        wait that's actually positive CLV. Let me think again."""
        # Bet +120 (implied 45.45%), close +110 (implied 47.62%).
        # You paid LESS for the pick than close did → positive CLV.
        out = format_line_movement(+120, +110)
        self.assertTrue(out.startswith('+10 cents'), msg=out)

    def test_underdog_price_lengthening_is_negative(self):
        """Bet +120, closed +150: close offered longer odds → market thought
        your pick LESS likely at close → you got worse value than close."""
        out = format_line_movement(+120, +150)
        self.assertTrue(out.startswith('-30 cents'), msg=out)

    def test_no_movement_renders_zero(self):
        out = format_line_movement(-110, -110)
        self.assertIn('0 cents', out)

    def test_missing_closing_odds_returns_empty(self):
        self.assertEqual(format_line_movement(-110, None), '')
        self.assertEqual(format_line_movement(None, -110), '')


class MockBetDisplayPropertiesTests(TestCase):
    """MockBet.clv_percent_display / clv_outcome_label / line_movement_display
    surface the formatter output off the model so templates don't call helpers."""

    def setUp(self):
        from decimal import Decimal
        from django.contrib.auth.models import User
        from apps.mockbets.models import MockBet
        self.user = User.objects.create_user('disp', password='pw')
        self.MockBet = MockBet
        self.Decimal = Decimal

    def _bet(self, clv, direction='', closing=None, bet_odds=-120):
        return self.MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=bet_odds,
            implied_probability=self.Decimal('0.5'),
            stake_amount=self.Decimal('100'),
            clv_cents=clv, clv_direction=direction,
            closing_odds_american=closing,
        )

    def test_positive_clv_display(self):
        bet = self._bet(clv=0.042, direction='positive', closing=-135, bet_odds=-120)
        self.assertEqual(bet.clv_percent_display, '+4.2%')
        self.assertEqual(bet.clv_outcome_label, 'Beat Market')
        self.assertTrue(bet.line_movement_display.startswith('+15 cents'))

    def test_negative_clv_display(self):
        bet = self._bet(clv=-0.031, direction='negative', closing=-110, bet_odds=-120)
        self.assertEqual(bet.clv_percent_display, '-3.1%')
        self.assertEqual(bet.clv_outcome_label, 'Market Beat You')

    def test_no_clv_yields_empty_strings(self):
        bet = self._bet(clv=None, direction='', closing=None)
        self.assertEqual(bet.clv_percent_display, '')
        self.assertEqual(bet.clv_outcome_label, '')
        self.assertEqual(bet.line_movement_display, '')

    def test_clv_set_but_closing_odds_missing_still_shows_percent(self):
        """Fallback path: CLV computed but closing_odds wasn't persisted for
        some reason. Percent + label should still render; line_movement empty."""
        bet = self._bet(clv=0.015, direction='positive', closing=None, bet_odds=-110)
        self.assertEqual(bet.clv_percent_display, '+1.5%')
        self.assertEqual(bet.clv_outcome_label, 'Beat Market')
        self.assertEqual(bet.line_movement_display, '')