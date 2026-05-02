"""Tests for apps/core/utils/multi_book.py — pure helpers, no engine impact.

The helpers operate on any Game whose model exposes `odds_snapshots`. We
exercise them against MLB Game/OddsSnapshot since that's the canonical
sport model and already has full source/derived/sportsbook fields.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.mlb.models import Conference, Team, Game, OddsSnapshot
from apps.core.utils.multi_book import (
    get_latest_snapshots_for_game,
    get_consensus_prob,
    get_best_price,
    is_odds_stale,
    get_odds_source_for_game,
    count_stale_games,
)


import uuid


def _game(start_offset_hours=2):
    """Build a fresh Game + Conf + Teams. UUID-suffixed slugs guarantee
    uniqueness even when multiple games are created in one test."""
    suffix = uuid.uuid4().hex[:8]
    league = Conference.objects.create(name=f'L-{suffix}', slug=f'l-{suffix}')
    home = Team.objects.create(name='H', slug=f'h-{suffix}', conference=league)
    away = Team.objects.create(name='A', slug=f'a-{suffix}', conference=league)
    return Game.objects.create(
        home_team=home, away_team=away,
        first_pitch=timezone.now() + timedelta(hours=start_offset_hours),
    )


def _snap(game, *, sportsbook='draftkings', captured_offset_minutes=0,
          ml_home=-110, ml_away=-110, prob=0.5,
          odds_source='odds_api', is_derived=False):
    return OddsSnapshot.objects.create(
        game=game,
        captured_at=timezone.now() - timedelta(minutes=captured_offset_minutes),
        sportsbook=sportsbook,
        market_home_win_prob=prob,
        moneyline_home=ml_home,
        moneyline_away=ml_away,
        odds_source=odds_source,
        is_derived=is_derived,
    )


# --- get_latest_snapshots_for_game -----------------------------------------

class LatestSnapshotsPerBookTests(TestCase):
    def test_one_snapshot_per_distinct_book(self):
        game = _game()
        _snap(game, sportsbook='draftkings', captured_offset_minutes=10, prob=0.55)
        _snap(game, sportsbook='draftkings', captured_offset_minutes=2, prob=0.58)
        _snap(game, sportsbook='fanduel', captured_offset_minutes=5, prob=0.60)
        latest = get_latest_snapshots_for_game(game)
        self.assertEqual(len(latest), 2)
        by_book = {s.sportsbook: s for s in latest}
        # Both books represented; per-book pick is the most recent capture.
        self.assertAlmostEqual(by_book['draftkings'].market_home_win_prob, 0.58)
        self.assertAlmostEqual(by_book['fanduel'].market_home_win_prob, 0.60)

    def test_excludes_derived_rows(self):
        game = _game()
        _snap(game, sportsbook='draftkings', prob=0.50)
        _snap(game, sportsbook='espn-derived', prob=0.99, is_derived=True)
        latest = get_latest_snapshots_for_game(game)
        books = {s.sportsbook for s in latest}
        self.assertEqual(books, {'draftkings'})

    def test_no_snapshots_returns_empty(self):
        self.assertEqual(get_latest_snapshots_for_game(_game()), [])

    def test_none_game_returns_empty(self):
        self.assertEqual(get_latest_snapshots_for_game(None), [])


# --- get_consensus_prob -----------------------------------------------------

class ConsensusProbTests(TestCase):
    def test_average_across_books(self):
        game = _game()
        _snap(game, sportsbook='draftkings', prob=0.50)
        _snap(game, sportsbook='fanduel', prob=0.60)
        _snap(game, sportsbook='caesars', prob=0.70)
        self.assertAlmostEqual(get_consensus_prob(game), 0.60, places=4)

    def test_uses_latest_per_book_not_all_rows(self):
        # Older 0.30 row on draftkings should be ignored — newest 0.50 wins.
        game = _game()
        _snap(game, sportsbook='draftkings', captured_offset_minutes=30, prob=0.30)
        _snap(game, sportsbook='draftkings', captured_offset_minutes=2, prob=0.50)
        _snap(game, sportsbook='fanduel', captured_offset_minutes=5, prob=0.60)
        # Mean of latest-per-book = (0.50 + 0.60) / 2 = 0.55, not (0.30+0.50+0.60)/3
        self.assertAlmostEqual(get_consensus_prob(game), 0.55, places=4)

    def test_single_book_returns_that_books_prob(self):
        game = _game()
        _snap(game, sportsbook='draftkings', prob=0.42)
        self.assertAlmostEqual(get_consensus_prob(game), 0.42, places=4)

    def test_no_snapshots_returns_none(self):
        self.assertIsNone(get_consensus_prob(_game()))


# --- get_best_price ---------------------------------------------------------

class BestPriceTests(TestCase):
    def test_returns_lowest_implied_prob_book(self):
        # Home side: -110 = 52.4% implied; +110 = 47.6%. Lower is better.
        game = _game()
        _snap(game, sportsbook='draftkings', ml_home=-110)
        _snap(game, sportsbook='fanduel', ml_home=110)
        _snap(game, sportsbook='caesars', ml_home=-105)
        odds, book = get_best_price(game, 'home')
        self.assertEqual(book, 'fanduel')
        self.assertEqual(odds, 110)

    def test_handles_sign_crossover(self):
        # +100 (50% implied) vs -100 (50% implied) — pure tie. The first
        # encountered "best" wins (deterministic by ordering); test that the
        # function picks something rather than crashing.
        game = _game()
        _snap(game, sportsbook='a', ml_home=100)
        _snap(game, sportsbook='b', ml_home=-100)
        result = get_best_price(game, 'home')
        self.assertIsNotNone(result)
        self.assertIn(result[1], ('a', 'b'))

    def test_away_side(self):
        game = _game()
        _snap(game, sportsbook='draftkings', ml_home=-200, ml_away=180)
        _snap(game, sportsbook='fanduel', ml_home=-180, ml_away=160)
        # Away: +180 (35.7%) is better for the bettor than +160 (38.5%).
        odds, book = get_best_price(game, 'away')
        self.assertEqual(book, 'draftkings')
        self.assertEqual(odds, 180)

    def test_returns_none_when_no_moneyline_for_side(self):
        game = _game()
        _snap(game, ml_home=None, ml_away=None)
        self.assertIsNone(get_best_price(game, 'home'))

    def test_invalid_side_raises(self):
        with self.assertRaises(ValueError):
            get_best_price(_game(), 'middle')


# --- get_odds_source_for_game ----------------------------------------------

class OddsSourceForGameTests(TestCase):
    def test_returns_latest_snapshots_source(self):
        game = _game()
        _snap(game, captured_offset_minutes=20, odds_source='odds_api')
        _snap(game, captured_offset_minutes=2, odds_source='espn',
              sportsbook='espn-feed')
        # Most recent wins regardless of book.
        self.assertEqual(get_odds_source_for_game(game), 'espn')

    def test_no_snapshots_returns_unknown(self):
        self.assertEqual(get_odds_source_for_game(_game()), 'unknown')

    def test_none_game_returns_unknown(self):
        self.assertEqual(get_odds_source_for_game(None), 'unknown')


# --- is_odds_stale ----------------------------------------------------------

class StaleOddsTests(TestCase):
    def test_inside_window_with_old_snapshot_is_stale(self):
        # Game starts in 10 min, latest snapshot is 45 min old — stale.
        game = _game(start_offset_hours=10 / 60)
        _snap(game, captured_offset_minutes=45)
        self.assertTrue(is_odds_stale(game))

    def test_inside_window_with_recent_snapshot_is_not_stale(self):
        # Game starts in 10 min, snapshot was 5 min ago — fresh.
        game = _game(start_offset_hours=10 / 60)
        _snap(game, captured_offset_minutes=5)
        self.assertFalse(is_odds_stale(game))

    def test_outside_window_never_stale(self):
        # Game starts in 4 hours, snapshot is 6 hours old — old, not stale.
        game = _game(start_offset_hours=4)
        _snap(game, captured_offset_minutes=360)
        self.assertFalse(is_odds_stale(game))

    def test_already_started_game_never_stale(self):
        game = _game(start_offset_hours=-1)  # started 1h ago
        _snap(game, captured_offset_minutes=120)
        self.assertFalse(is_odds_stale(game))

    def test_no_snapshots_returns_false(self):
        # "No data" is a separate signal from "stale" — helper does not
        # conflate them. Caller is expected to also check for absence.
        game = _game(start_offset_hours=10 / 60)
        self.assertFalse(is_odds_stale(game))

    def test_none_game_returns_false(self):
        self.assertFalse(is_odds_stale(None))

    def test_threshold_boundary(self):
        # Game in 30 min exactly; snapshot 31 min old → stale.
        # Game in 30 min exactly; snapshot 29 min old → fresh.
        # Use 25-min window so we land cleanly inside it.
        game_a = _game(start_offset_hours=20 / 60)
        _snap(game_a, captured_offset_minutes=31)
        self.assertTrue(is_odds_stale(game_a))

        game_b = _game(start_offset_hours=20 / 60)
        _snap(game_b, captured_offset_minutes=29)
        self.assertFalse(is_odds_stale(game_b))

    def test_count_stale_games(self):
        # Two stale, one fresh, one already-started.
        stale_a = _game(start_offset_hours=10 / 60)
        _snap(stale_a, captured_offset_minutes=60)
        stale_b = _game(start_offset_hours=15 / 60)
        _snap(stale_b, captured_offset_minutes=45)
        fresh = _game(start_offset_hours=10 / 60)
        _snap(fresh, captured_offset_minutes=2)
        started = _game(start_offset_hours=-1)
        _snap(started, captured_offset_minutes=120)
        self.assertEqual(count_stale_games(Game.objects.all()), 2)
