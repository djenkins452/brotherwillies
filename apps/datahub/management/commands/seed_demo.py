import random
import math
from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth.models import User
from apps.cfb.models import Conference, Team, Game, OddsSnapshot, InjuryImpact
from apps.cbb.models import (
    Conference as CBBConference, Team as CBBTeam, Game as CBBGame,
    OddsSnapshot as CBBOddsSnapshot, InjuryImpact as CBBInjuryImpact,
)
from apps.accounts.models import UserProfile, UserModelConfig, ModelPreset, UserSubscription
from apps.analytics.models import ModelResultSnapshot
from apps.parlays.models import Parlay, ParlayLeg
from apps.cfb.services.model_service import compute_house_win_prob
from apps.cbb.services.model_service import compute_house_win_prob as cbb_house_prob
from apps.datahub.team_colors import get_team_color


class Command(BaseCommand):
    help = 'Seed deterministic demo data for development'

    def handle(self, *args, **options):
        random.seed(42)
        now = timezone.now()

        from apps.mockbets.models import MockBet, MockBetSettlementLog

        self.stdout.write('Clearing existing demo data...')
        MockBetSettlementLog.objects.all().delete()
        MockBet.objects.all().delete()
        ParlayLeg.objects.all().delete()
        Parlay.objects.all().delete()
        ModelResultSnapshot.objects.all().delete()
        # CFB
        InjuryImpact.objects.all().delete()
        OddsSnapshot.objects.all().delete()
        Game.objects.all().delete()
        Team.objects.all().delete()
        Conference.objects.all().delete()
        # CBB
        CBBInjuryImpact.objects.all().delete()
        CBBOddsSnapshot.objects.all().delete()
        CBBGame.objects.all().delete()
        CBBTeam.objects.all().delete()
        CBBConference.objects.all().delete()
        ModelPreset.objects.filter(user__username='demo').delete()

        # ── CFB ──────────────────────────────────────────────────────
        self.stdout.write('Seeding CFB data...')

        conf_data = [
            ('SEC', 'sec'), ('Big Ten', 'big-ten'), ('ACC', 'acc'),
            ('Big 12', 'big-12'), ('Pac-12', 'pac-12'),
        ]
        conferences = {}
        for name, slug in conf_data:
            conferences[slug] = Conference.objects.create(name=name, slug=slug)
        self.stdout.write(f'  Created {len(conferences)} CFB conferences')

        teams_data = {
            'sec': [
                ('Alabama', 'alabama', 82), ('Georgia', 'georgia', 85), ('LSU', 'lsu', 75),
                ('Ole Miss', 'ole-miss', 72), ('Tennessee', 'tennessee', 70), ('Texas A&M', 'texas-am', 68),
            ],
            'big-ten': [
                ('Ohio State', 'ohio-state', 84), ('Michigan', 'michigan', 80), ('Penn State', 'penn-state', 76),
                ('Oregon', 'oregon', 78), ('Wisconsin', 'wisconsin', 65), ('Iowa', 'iowa', 62),
            ],
            'acc': [
                ('Florida State', 'florida-state', 73), ('Clemson', 'clemson', 71), ('Miami', 'miami', 69),
                ('North Carolina', 'north-carolina', 66),
            ],
            'big-12': [
                ('Texas', 'texas', 79), ('Oklahoma', 'oklahoma', 74), ('Kansas State', 'kansas-state', 67),
                ('TCU', 'tcu', 64), ('Baylor', 'baylor', 61),
            ],
            'pac-12': [
                ('USC', 'usc', 77), ('Washington', 'washington', 73), ('Utah', 'utah', 70),
                ('UCLA', 'ucla', 66),
            ],
        }
        all_teams = []
        for conf_slug, team_list in teams_data.items():
            for name, slug, rating in team_list:
                team = Team.objects.create(
                    name=name, slug=slug, conference=conferences[conf_slug], rating=rating,
                    primary_color=get_team_color(slug, 'cfb'),
                )
                all_teams.append(team)
        self.stdout.write(f'  Created {len(all_teams)} CFB teams')

        # CFB Games - ~25 across next 7 days (no team plays twice on same day)
        games = []
        used_pairs = set()
        teams_by_day = {}
        for day in range(7):
            teams_by_day[day] = set()
            games_today = random.randint(3, 5)
            for _ in range(games_today):
                if len(games) >= 25:
                    break
                attempts = 0
                while attempts < 50:
                    home = random.choice(all_teams)
                    away = random.choice(all_teams)
                    if (home != away
                            and (home.id, away.id) not in used_pairs
                            and home.id not in teams_by_day[day]
                            and away.id not in teams_by_day[day]):
                        used_pairs.add((home.id, away.id))
                        teams_by_day[day].add(home.id)
                        teams_by_day[day].add(away.id)
                        break
                    attempts += 1
                else:
                    continue

                hour = random.choice([12, 15, 17, 19, 20])
                kickoff = now + timedelta(days=day + 1, hours=hour - now.hour)
                neutral = random.random() < 0.1

                game = Game.objects.create(
                    home_team=home, away_team=away, kickoff=kickoff, neutral_site=neutral
                )
                games.append(game)
        self.stdout.write(f'  Created {len(games)} CFB games')

        # CFB Odds snapshots
        for game in games:
            self._create_odds_snapshot(game, now, OddsSnapshot, total_range=(42, 62))
        self.stdout.write('  Created CFB odds snapshots')

        # CFB Injuries
        cfb_injury_notes = [
            'Starting QB questionable', 'Key WR out for season', 'Starting RB day-to-day',
            'Two OL starters limited', 'Star CB probable', 'Defensive end doubtful',
        ]
        injury_count = 0
        for game in random.sample(games, min(15, len(games))):
            team = random.choice([game.home_team, game.away_team])
            level = random.choice(['low', 'med', 'high'])
            InjuryImpact.objects.create(
                game=game, team=team, impact_level=level,
                notes=random.choice(cfb_injury_notes)
            )
            injury_count += 1
        self.stdout.write(f'  Created {injury_count} CFB injury impacts')

        # ── CBB ──────────────────────────────────────────────────────
        self.stdout.write('Seeding CBB data...')

        cbb_conf_data = [
            ('Big 12', 'big-12'), ('SEC', 'sec'), ('Big Ten', 'big-ten'),
            ('ACC', 'acc'), ('Big East', 'big-east'), ('Pac-12', 'pac-12'),
        ]
        cbb_conferences = {}
        for name, slug in cbb_conf_data:
            cbb_conferences[slug] = CBBConference.objects.create(name=name, slug=slug)
        self.stdout.write(f'  Created {len(cbb_conferences)} CBB conferences')

        cbb_teams_data = {
            'big-12': [
                ('Kansas', 'kansas', 88), ('Houston', 'houston', 82), ('Iowa State', 'iowa-state', 80),
                ('Baylor', 'baylor', 76), ('BYU', 'byu', 73),
            ],
            'sec': [
                ('Auburn', 'auburn', 86), ('Tennessee', 'tennessee', 84), ('Kentucky', 'kentucky', 81),
                ('Alabama', 'alabama', 79), ('Florida', 'florida', 74),
            ],
            'big-ten': [
                ('Purdue', 'purdue', 85), ('Michigan State', 'michigan-state', 78),
                ('Illinois', 'illinois', 77), ('Wisconsin', 'wisconsin', 75), ('Indiana', 'indiana', 72),
            ],
            'acc': [
                ('Duke', 'duke', 87), ('North Carolina', 'north-carolina', 83), ('Virginia', 'virginia', 76),
                ('Wake Forest', 'wake-forest', 71), ('NC State', 'nc-state', 70),
            ],
            'big-east': [
                ('UConn', 'uconn', 84), ('Marquette', 'marquette', 81), ('Creighton', 'creighton', 79),
                ('Villanova', 'villanova', 75), ('Xavier', 'xavier', 73),
            ],
            'pac-12': [
                ('Arizona', 'arizona', 83), ('Colorado', 'colorado', 78), ('UCLA', 'ucla', 76),
                ('Oregon', 'oregon', 74), ('Utah', 'utah', 71),
            ],
        }
        all_cbb_teams = []
        for conf_slug, team_list in cbb_teams_data.items():
            for name, slug, rating in team_list:
                team = CBBTeam.objects.create(
                    name=name, slug=slug, conference=cbb_conferences[conf_slug], rating=rating,
                    primary_color=get_team_color(slug, 'cbb'),
                )
                all_cbb_teams.append(team)
        self.stdout.write(f'  Created {len(all_cbb_teams)} CBB teams')

        # CBB Games - ~30 across 14 days, realistic schedule (Tue/Thu/Sat pattern)
        cbb_game_days = [1, 3, 4, 6, 8, 10, 11, 13]
        cbb_games = []
        cbb_used_pairs = set()
        cbb_teams_by_day = {}
        for day in cbb_game_days:
            cbb_teams_by_day[day] = set()
            games_today = random.randint(3, 5)
            for _ in range(games_today):
                if len(cbb_games) >= 30:
                    break
                attempts = 0
                while attempts < 50:
                    home = random.choice(all_cbb_teams)
                    away = random.choice(all_cbb_teams)
                    if (home != away
                            and (home.id, away.id) not in cbb_used_pairs
                            and home.id not in cbb_teams_by_day[day]
                            and away.id not in cbb_teams_by_day[day]):
                        cbb_used_pairs.add((home.id, away.id))
                        cbb_teams_by_day[day].add(home.id)
                        cbb_teams_by_day[day].add(away.id)
                        break
                    attempts += 1
                else:
                    continue

                hour = random.choice([19, 20, 21])
                tipoff = now + timedelta(days=day, hours=hour - now.hour)
                neutral = random.random() < 0.05

                game = CBBGame.objects.create(
                    home_team=home, away_team=away, tipoff=tipoff, neutral_site=neutral
                )
                cbb_games.append(game)
        self.stdout.write(f'  Created {len(cbb_games)} CBB games')

        # CBB Odds snapshots
        for game in cbb_games:
            self._create_cbb_odds_snapshot(game, now)
        self.stdout.write('  Created CBB odds snapshots')

        # CBB Injuries
        cbb_injury_notes = [
            'Starting PG questionable', 'Key guard day-to-day', 'Center out 2-3 weeks',
            'Sixth man probable', 'Starting forward limited', 'Star wing doubtful',
        ]
        cbb_injury_count = 0
        for game in random.sample(cbb_games, min(15, len(cbb_games))):
            team = random.choice([game.home_team, game.away_team])
            level = random.choice(['low', 'med', 'high'])
            CBBInjuryImpact.objects.create(
                game=game, team=team, impact_level=level,
                notes=random.choice(cbb_injury_notes)
            )
            cbb_injury_count += 1
        self.stdout.write(f'  Created {cbb_injury_count} CBB injury impacts')

        # ── Demo User ────────────────────────────────────────────────
        self.stdout.write('Setting up demo user...')

        demo_user, created = User.objects.get_or_create(
            username='demo',
            defaults={'email': 'demo@brotherwillies.com'}
        )
        if created:
            demo_user.set_password('brotherwillies')
            demo_user.save()
            self.stdout.write('  Created demo user')
        else:
            self.stdout.write('  Demo user already exists')

        # Ensure profile exists
        try:
            profile = demo_user.profile
        except Exception:
            profile = UserProfile.objects.create(user=demo_user)

        # Set CFB favorite team
        alabama_cfb = Team.objects.filter(slug='alabama').first()
        sec_cfb = Conference.objects.filter(slug='sec').first()
        if alabama_cfb:
            profile.favorite_team = alabama_cfb
            profile.favorite_conference = sec_cfb
            profile.always_include_favorite_team = True
            profile.preference_min_edge = 1.0

        # Set CBB favorite team
        kansas = CBBTeam.objects.filter(slug='kansas').first()
        big12_cbb = CBBConference.objects.filter(slug='big-12').first()
        if kansas:
            profile.favorite_cbb_team = kansas
            profile.favorite_cbb_conference = big12_cbb

        profile.save()

        # Demo user model config (non-default)
        config = UserModelConfig.get_or_create_for_user(demo_user)
        config.rating_weight = 1.3
        config.hfa_weight = 0.8
        config.injury_weight = 1.5
        config.recent_form_weight = 1.1
        config.conference_weight = 0.9
        config.save()

        # Demo preset
        ModelPreset.objects.create(
            user=demo_user, name='Injury-Heavy',
            rating_weight=0.8, hfa_weight=1.0, injury_weight=2.0,
            recent_form_weight=1.0, conference_weight=1.0,
        )

        # Ensure subscription
        try:
            demo_user.subscription
        except UserSubscription.DoesNotExist:
            UserSubscription.objects.create(user=demo_user, tier='free')

        # CFB Model result snapshots
        for game in games[:10]:
            odds = game.odds_snapshots.first()
            if odds:
                house_prob = compute_house_win_prob(game)
                ModelResultSnapshot.objects.create(
                    game=game,
                    market_prob=odds.market_home_win_prob,
                    house_prob=house_prob,
                    house_model_version='v1',
                    data_confidence=random.choice(['low', 'med', 'high']),
                )

        # CBB Model result snapshots
        for game in cbb_games[:10]:
            odds = game.odds_snapshots.first()
            if odds:
                house_prob = cbb_house_prob(game)
                ModelResultSnapshot.objects.create(
                    cbb_game=game,
                    market_prob=odds.market_home_win_prob,
                    house_prob=house_prob,
                    house_model_version='v1',
                    data_confidence=random.choice(['low', 'med', 'high']),
                )

        # Demo parlay (CFB)
        if len(games) >= 3:
            parlay = Parlay.objects.create(user=demo_user, sportsbook='DraftKings')
            parlay_games = random.sample(games, 3)
            implied_probs = []
            for pg in parlay_games:
                odds = pg.odds_snapshots.first()
                mp = odds.market_home_win_prob if odds else 0.5
                ParlayLeg.objects.create(
                    parlay=parlay, game=pg, market_type='moneyline',
                    selection=f'{pg.home_team.name} ML',
                    market_prob=mp,
                    house_prob=compute_house_win_prob(pg),
                )
                implied_probs.append(mp)
            parlay.implied_probability = math.prod(implied_probs)
            parlay.house_probability = math.prod([l.house_prob for l in parlay.legs.all() if l.house_prob])
            parlay.save()
            self.stdout.write('  Created demo parlay')

        # ── Mock Bets ────────────────────────────────────────────────
        self.stdout.write('Seeding mock bets...')
        self._seed_mock_bets(demo_user, games, cbb_games, now, MockBet, MockBetSettlementLog)

        self.stdout.write(self.style.SUCCESS('Demo data seeded successfully!'))

    def _create_odds_snapshot(self, game, now, snapshot_model, total_range=(42, 62)):
        """Create odds snapshots for a CFB game."""
        rating_diff = game.home_team.rating - game.away_team.rating
        base_prob = 0.5 + (rating_diff / 100.0) + (0.03 if not game.neutral_site else 0)
        base_prob = max(0.15, min(0.85, base_prob))
        noise = random.uniform(-0.05, 0.05)
        market_prob = max(0.10, min(0.90, base_prob + noise))

        spread = -(rating_diff + (3 if not game.neutral_site else 0)) * 0.5
        total = random.uniform(*total_range)

        snapshot_model.objects.create(
            game=game,
            captured_at=now - timedelta(hours=random.randint(6, 48)),
            sportsbook='consensus',
            market_home_win_prob=round(market_prob, 3),
            spread=round(spread, 1),
            total=round(total, 1),
            moneyline_home=self._prob_to_ml(market_prob),
            moneyline_away=self._prob_to_ml(1 - market_prob),
        )

        if random.random() < 0.5:
            move = random.uniform(-0.04, 0.04)
            new_prob = max(0.10, min(0.90, market_prob + move))
            snapshot_model.objects.create(
                game=game,
                captured_at=now - timedelta(hours=random.randint(1, 5)),
                sportsbook='consensus',
                market_home_win_prob=round(new_prob, 3),
                spread=round(spread + move * 10, 1),
                total=round(total + random.uniform(-1, 1), 1),
                moneyline_home=self._prob_to_ml(new_prob),
                moneyline_away=self._prob_to_ml(1 - new_prob),
            )

    def _create_cbb_odds_snapshot(self, game, now):
        """Create odds snapshots for a CBB game."""
        rating_diff = game.home_team.rating - game.away_team.rating
        base_prob = 0.5 + (rating_diff / 100.0) + (0.035 if not game.neutral_site else 0)
        base_prob = max(0.15, min(0.85, base_prob))
        noise = random.uniform(-0.05, 0.05)
        market_prob = max(0.10, min(0.90, base_prob + noise))

        spread = -(rating_diff + (3.5 if not game.neutral_site else 0)) * 0.5
        total = random.uniform(130, 165)

        CBBOddsSnapshot.objects.create(
            game=game,
            captured_at=now - timedelta(hours=random.randint(6, 48)),
            sportsbook='consensus',
            market_home_win_prob=round(market_prob, 3),
            spread=round(spread, 1),
            total=round(total, 1),
            moneyline_home=self._prob_to_ml(market_prob),
            moneyline_away=self._prob_to_ml(1 - market_prob),
        )

        if random.random() < 0.5:
            move = random.uniform(-0.04, 0.04)
            new_prob = max(0.10, min(0.90, market_prob + move))
            CBBOddsSnapshot.objects.create(
                game=game,
                captured_at=now - timedelta(hours=random.randint(1, 5)),
                sportsbook='consensus',
                market_home_win_prob=round(new_prob, 3),
                spread=round(spread + move * 10, 1),
                total=round(total + random.uniform(-1, 1), 1),
                moneyline_home=self._prob_to_ml(new_prob),
                moneyline_away=self._prob_to_ml(1 - new_prob),
            )

    def _seed_mock_bets(self, user, cfb_games, cbb_games, now, MockBet, MockBetSettlementLog):
        """Seed 30 mock bets — mix of settled and pending, realistic outcomes."""
        bet_templates = []

        # CFB settled bets (15 bets, placed 1-14 days ago, settled)
        cfb_pool = cfb_games[:15] if len(cfb_games) >= 15 else cfb_games
        for i, game in enumerate(cfb_pool):
            odds = game.odds_snapshots.first()
            if not odds:
                continue
            is_home = random.random() < 0.6
            team = game.home_team if is_home else game.away_team
            ml = odds.moneyline_home if is_home else odds.moneyline_away
            prob = odds.market_home_win_prob if is_home else (1 - odds.market_home_win_prob)

            bet_type = random.choice(['moneyline', 'moneyline', 'spread', 'total'])
            if bet_type == 'spread':
                spread_val = abs(odds.spread) if odds.spread else 7
                selection = f'{team.name} {"-" if is_home else "+"}{spread_val}'
                ml = random.choice([-110, -105, -115])
                prob = 0.5238
            elif bet_type == 'total':
                total_val = odds.total if odds.total else 52
                over_under = random.choice(['Over', 'Under'])
                selection = f'{over_under} {total_val}'
                ml = random.choice([-110, -105, -115])
                prob = 0.5238
            else:
                selection = f'{team.name}'

            # Determine result based on implied probability + noise
            roll = random.random()
            if roll < prob * 0.95:
                result = 'win'
            elif roll < 0.98:
                result = 'loss'
            else:
                result = 'push'

            placed_days_ago = random.randint(1, 14)
            placed_at = now - timedelta(days=placed_days_ago, hours=random.randint(1, 12))
            settled_at = placed_at + timedelta(hours=random.randint(3, 8))

            stake = Decimal(str(random.choice([25, 50, 100, 100, 100, 150, 200])))
            if result == 'win':
                if ml > 0:
                    payout = stake * Decimal(ml) / Decimal(100)
                else:
                    payout = stake * Decimal(100) / Decimal(abs(ml))
            elif result == 'push':
                payout = stake
            else:
                payout = Decimal('0.00')

            edge = round(random.uniform(-3, 10), 1) if random.random() < 0.7 else None

            bet_templates.append({
                'sport': 'cfb',
                'cfb_game': game,
                'bet_type': bet_type,
                'selection': selection,
                'odds_american': ml,
                'implied_probability': Decimal(str(round(prob, 4))),
                'stake_amount': stake,
                'result': result,
                'simulated_payout': payout if result == 'win' else (stake if result == 'push' else Decimal('0.00')),
                'confidence_level': random.choice(['low', 'medium', 'medium', 'high']),
                'model_source': random.choice(['house', 'house', 'user']),
                'expected_edge': Decimal(str(edge)) if edge is not None else None,
                'placed_at': placed_at,
                'settled_at': settled_at,
            })

        # CBB settled bets (10 bets)
        cbb_pool = cbb_games[:10] if len(cbb_games) >= 10 else cbb_games
        for game in cbb_pool:
            odds = game.odds_snapshots.first()
            if not odds:
                continue
            is_home = random.random() < 0.55
            team = game.home_team if is_home else game.away_team
            ml = odds.moneyline_home if is_home else odds.moneyline_away
            prob = odds.market_home_win_prob if is_home else (1 - odds.market_home_win_prob)
            selection = f'{team.name}'

            roll = random.random()
            if roll < prob * 0.9:
                result = 'win'
            elif roll < 0.97:
                result = 'loss'
            else:
                result = 'push'

            placed_days_ago = random.randint(1, 10)
            placed_at = now - timedelta(days=placed_days_ago, hours=random.randint(1, 8))
            settled_at = placed_at + timedelta(hours=random.randint(2, 5))

            stake = Decimal(str(random.choice([50, 100, 100, 150])))
            if result == 'win':
                if ml > 0:
                    payout = stake * Decimal(ml) / Decimal(100)
                else:
                    payout = stake * Decimal(100) / Decimal(abs(ml))
            elif result == 'push':
                payout = stake
            else:
                payout = Decimal('0.00')

            bet_templates.append({
                'sport': 'cbb',
                'cbb_game': game,
                'bet_type': 'moneyline',
                'selection': selection,
                'odds_american': ml,
                'implied_probability': Decimal(str(round(prob, 4))),
                'stake_amount': stake,
                'result': result,
                'simulated_payout': payout if result == 'win' else (stake if result == 'push' else Decimal('0.00')),
                'confidence_level': random.choice(['low', 'medium', 'high']),
                'model_source': random.choice(['house', 'user']),
                'expected_edge': Decimal(str(round(random.uniform(-2, 8), 1))) if random.random() < 0.6 else None,
                'placed_at': placed_at,
                'settled_at': settled_at,
            })

        # Pending bets (5 bets — future games)
        pending_cfb = [g for g in cfb_games if g.status == 'scheduled'][:3]
        pending_cbb = [g for g in cbb_games if g.status == 'scheduled'][:2]
        for game in pending_cfb:
            odds = game.odds_snapshots.first()
            if not odds:
                continue
            team = game.home_team
            ml = odds.moneyline_home
            prob = odds.market_home_win_prob
            bet_templates.append({
                'sport': 'cfb',
                'cfb_game': game,
                'bet_type': 'moneyline',
                'selection': team.name,
                'odds_american': ml,
                'implied_probability': Decimal(str(round(prob, 4))),
                'stake_amount': Decimal('100.00'),
                'result': 'pending',
                'simulated_payout': None,
                'confidence_level': random.choice(['medium', 'high']),
                'model_source': 'house',
                'expected_edge': None,
                'placed_at': now - timedelta(hours=random.randint(1, 24)),
                'settled_at': None,
            })
        for game in pending_cbb:
            odds = game.odds_snapshots.first()
            if not odds:
                continue
            team = game.home_team
            ml = odds.moneyline_home
            prob = odds.market_home_win_prob
            bet_templates.append({
                'sport': 'cbb',
                'cbb_game': game,
                'bet_type': 'moneyline',
                'selection': team.name,
                'odds_american': ml,
                'implied_probability': Decimal(str(round(prob, 4))),
                'stake_amount': Decimal('100.00'),
                'result': 'pending',
                'simulated_payout': None,
                'confidence_level': 'medium',
                'model_source': 'user',
                'expected_edge': None,
                'placed_at': now - timedelta(hours=random.randint(1, 12)),
                'settled_at': None,
            })

        # Create all bets
        settled_count = 0
        for tmpl in bet_templates:
            cfb_game = tmpl.pop('cfb_game', None)
            cbb_game = tmpl.pop('cbb_game', None)
            bet = MockBet.objects.create(
                user=user,
                cfb_game=cfb_game,
                cbb_game=cbb_game,
                **tmpl,
            )
            if bet.result != 'pending':
                MockBetSettlementLog.objects.create(
                    mock_bet=bet,
                    settled_at=bet.settled_at,
                    result=bet.result,
                    payout=bet.simulated_payout or Decimal('0'),
                    reason=f'Demo seed: {bet.sport.upper()} game resolved',
                )
                settled_count += 1

        # Add review flags to some settled bets
        settled_bets = list(MockBet.objects.filter(user=user, result__in=['win', 'loss']))
        for bet in random.sample(settled_bets, min(8, len(settled_bets))):
            bet.review_flag = 'repeat' if bet.result == 'win' else random.choice(['repeat', 'avoid'])
            notes_pool = [
                'Good read on the matchup', 'Line moved my direction after placement',
                'Injury report was key factor', 'Model edge was accurate',
                'Should have waited for better line', 'Overreacted to recent form',
                'Confidence was too high for this spot', 'Solid fundamentals play',
            ]
            bet.review_notes = random.choice(notes_pool)
            bet.save(update_fields=['review_flag', 'review_notes'])

        total_bets = len(bet_templates)
        self.stdout.write(f'  Created {total_bets} mock bets ({settled_count} settled, {total_bets - settled_count} pending)')

    @staticmethod
    def _prob_to_ml(prob):
        if prob >= 0.5:
            return int(-prob / (1 - prob) * 100)
        else:
            return int((1 - prob) / prob * 100)
