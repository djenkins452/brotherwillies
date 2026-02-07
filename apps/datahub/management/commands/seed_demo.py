import random
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth.models import User
from apps.cfb.models import Conference, Team, Game, OddsSnapshot, InjuryImpact
from apps.accounts.models import UserProfile, UserModelConfig, ModelPreset, UserSubscription
from apps.analytics.models import ModelResultSnapshot
from apps.parlays.models import Parlay, ParlayLeg
from apps.cfb.services.model_service import compute_house_win_prob


class Command(BaseCommand):
    help = 'Seed deterministic demo data for development'

    def handle(self, *args, **options):
        random.seed(42)
        self.stdout.write('Clearing existing demo data...')
        ParlayLeg.objects.all().delete()
        Parlay.objects.all().delete()
        ModelResultSnapshot.objects.all().delete()
        InjuryImpact.objects.all().delete()
        OddsSnapshot.objects.all().delete()
        Game.objects.all().delete()
        Team.objects.all().delete()
        Conference.objects.all().delete()
        ModelPreset.objects.filter(user__username='demo').delete()

        # Conferences
        conf_data = [
            ('SEC', 'sec'), ('Big Ten', 'big-ten'), ('ACC', 'acc'),
            ('Big 12', 'big-12'), ('Pac-12', 'pac-12'),
        ]
        conferences = {}
        for name, slug in conf_data:
            conferences[slug] = Conference.objects.create(name=name, slug=slug)
        self.stdout.write(f'  Created {len(conferences)} conferences')

        # Teams
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
                    name=name, slug=slug, conference=conferences[conf_slug], rating=rating
                )
                all_teams.append(team)
        self.stdout.write(f'  Created {len(all_teams)} teams')

        # Games - ~25 across next 7 days
        now = timezone.now()
        games = []
        used_pairs = set()
        for day in range(7):
            games_today = random.randint(3, 5)
            for _ in range(games_today):
                if len(games) >= 25:
                    break
                attempts = 0
                while attempts < 50:
                    home = random.choice(all_teams)
                    away = random.choice(all_teams)
                    if home != away and (home.id, away.id) not in used_pairs:
                        used_pairs.add((home.id, away.id))
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
        self.stdout.write(f'  Created {len(games)} games')

        # Odds snapshots
        for game in games:
            rating_diff = game.home_team.rating - game.away_team.rating
            base_prob = 0.5 + (rating_diff / 100.0) + (0.03 if not game.neutral_site else 0)
            base_prob = max(0.15, min(0.85, base_prob))
            noise = random.uniform(-0.05, 0.05)
            market_prob = max(0.10, min(0.90, base_prob + noise))

            spread = -(rating_diff + (3 if not game.neutral_site else 0)) * 0.5
            total = random.uniform(42, 62)

            # First snapshot (older)
            OddsSnapshot.objects.create(
                game=game,
                captured_at=now - timedelta(hours=random.randint(6, 48)),
                sportsbook='consensus',
                market_home_win_prob=round(market_prob, 3),
                spread=round(spread, 1),
                total=round(total, 1),
                moneyline_home=self._prob_to_ml(market_prob),
                moneyline_away=self._prob_to_ml(1 - market_prob),
            )

            # Second snapshot for some games (line movement)
            if random.random() < 0.5:
                move = random.uniform(-0.04, 0.04)
                new_prob = max(0.10, min(0.90, market_prob + move))
                OddsSnapshot.objects.create(
                    game=game,
                    captured_at=now - timedelta(hours=random.randint(1, 5)),
                    sportsbook='consensus',
                    market_home_win_prob=round(new_prob, 3),
                    spread=round(spread + move * 10, 1),
                    total=round(total + random.uniform(-1, 1), 1),
                    moneyline_home=self._prob_to_ml(new_prob),
                    moneyline_away=self._prob_to_ml(1 - new_prob),
                )
        self.stdout.write('  Created odds snapshots')

        # Injuries
        injury_notes = [
            'Starting QB questionable', 'Key WR out for season', 'Starting RB day-to-day',
            'Two OL starters limited', 'Star CB probable', 'Defensive end doubtful',
        ]
        injury_count = 0
        for game in random.sample(games, min(15, len(games))):
            team = random.choice([game.home_team, game.away_team])
            level = random.choice(['low', 'med', 'high'])
            InjuryImpact.objects.create(
                game=game, team=team, impact_level=level,
                notes=random.choice(injury_notes)
            )
            injury_count += 1
        self.stdout.write(f'  Created {injury_count} injury impacts')

        # Demo user
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

        # Set favorite team
        alabama = Team.objects.filter(slug='alabama').first()
        sec = Conference.objects.filter(slug='sec').first()
        if alabama:
            profile.favorite_team = alabama
            profile.favorite_conference = sec
            profile.always_include_favorite_team = True
            profile.preference_min_edge = 1.0
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

        # Model result snapshots
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

        # Demo parlay
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
            import math
            parlay.implied_probability = math.prod(implied_probs)
            parlay.house_probability = math.prod([l.house_prob for l in parlay.legs.all() if l.house_prob])
            parlay.save()
            self.stdout.write('  Created demo parlay')

        self.stdout.write(self.style.SUCCESS('Demo data seeded successfully!'))

    @staticmethod
    def _prob_to_ml(prob):
        if prob >= 0.5:
            return int(-prob / (1 - prob) * 100)
        else:
            return int((1 - prob) / prob * 100)
