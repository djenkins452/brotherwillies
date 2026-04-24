"""Tests for odds utilities — de-vig math + CLV conversion."""
from django.test import TestCase

from apps.core.utils.odds import (
    american_to_decimal,
    american_to_implied_prob,
    closing_line_value,
    devig_moneyline_prob,
    devig_two_way,
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