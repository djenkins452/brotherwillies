"""
Seed upcoming golf major events with golfer fields and odds snapshots.

Idempotent: uses get_or_create on slug. Only creates events whose
end_date is in the future. Populates odds snapshots for the top 30
golfers per event using realistic outright odds tiers.

Usage:
    python manage.py seed_golf_events
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.golf.models import GolfEvent, Golfer, GolfOddsSnapshot


# Major tournaments with approximate 2026 dates (month, day, duration)
MAJOR_EVENTS = [
    ('The Masters', 'the-masters', 4, 10, 4),
    ('PGA Championship', 'pga-championship', 5, 21, 4),
    ('U.S. Open', 'us-open', 6, 18, 4),
    ('The Open Championship', 'the-open-championship', 7, 16, 4),
]

# Top golfers who should appear in every major field (subset of seed_golfers list)
FIELD_GOLFERS = [
    'Scottie Scheffler', 'Xander Schauffele', 'Rory McIlroy', 'Collin Morikawa',
    'Ludvig Aberg', 'Wyndham Clark', 'Patrick Cantlay', 'Viktor Hovland',
    'Sahith Theegala', 'Hideki Matsuyama', 'Shane Lowry', 'Tommy Fleetwood',
    'Russell Henley', 'Sam Burns', 'Sungjae Im', 'Tony Finau',
    'Matt Fitzpatrick', 'Brian Harman', 'Max Homa', 'Keegan Bradley',
    'Justin Thomas', 'Brooks Koepka', 'Jordan Spieth', 'Jon Rahm',
    'Bryson DeChambeau', 'Cameron Smith', 'Jason Day', 'Robert MacIntyre',
    'Tom Kim', 'Akshay Bhatia',
]

# Realistic outright odds tiers (American format) for 30-golfer field
ODDS_TIERS = [
    600, 700, 800, 900, 1000, 1100, 1200, 1400, 1600, 1800,
    2000, 2200, 2500, 2500, 2800, 3000, 3300, 3500, 4000, 4000,
    4500, 5000, 5000, 6000, 6500, 7000, 8000, 10000, 12500, 15000,
]


class Command(BaseCommand):
    help = 'Seed upcoming golf major events with fields and odds'

    def handle(self, *args, **options):
        random.seed(2026)
        now = timezone.now()
        today = now.date()
        year = today.year

        events_created = 0
        odds_created = 0

        for name, slug, month, day, duration in MAJOR_EVENTS:
            start = today.replace(month=month, day=day)
            end = start + timedelta(days=duration - 1)

            # Skip events that have already ended
            if end < today:
                continue

            event, created = GolfEvent.objects.get_or_create(
                slug=slug,
                defaults={
                    'name': name,
                    'start_date': start,
                    'end_date': end,
                },
            )
            if created:
                events_created += 1

            # Ensure golfers exist and build field
            field_golfers = []
            for golfer_name in FIELD_GOLFERS:
                golfer, _ = Golfer.objects.get_or_create(
                    name=golfer_name, defaults={},
                )
                field_golfers.append(golfer)

            # Create odds snapshots if none exist for this event
            if not GolfOddsSnapshot.objects.filter(event=event).exists():
                shuffled = list(field_golfers)
                random.shuffle(shuffled)
                for i, golfer in enumerate(shuffled):
                    if i >= len(ODDS_TIERS):
                        break
                    base_odds = ODDS_TIERS[i] + random.randint(-50, 50)
                    implied = 100.0 / (base_odds + 100)
                    GolfOddsSnapshot.objects.create(
                        event=event,
                        golfer=golfer,
                        captured_at=now - timedelta(hours=random.randint(2, 48)),
                        sportsbook='consensus',
                        outright_odds=base_odds,
                        implied_prob=round(implied * 100, 2),
                    )
                    odds_created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Golf events seeded: {events_created} created, {odds_created} odds snapshots'
        ))
