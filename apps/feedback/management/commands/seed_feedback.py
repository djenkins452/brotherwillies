from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from apps.feedback.models import FeedbackComponent, PartnerFeedback


COMPONENTS = [
    'AI Explanations',
    'Website Design',
    'Value Board',
    'Model Accuracy',
    'Data Sources',
    'Performance Dashboard',
    'Parlay Builder',
    'Mobile Experience',
    'Navigation & UX',
    'Account & Profile',
]


class Command(BaseCommand):
    help = 'Seed feedback components and demo feedback items'

    def handle(self, *args, **options):
        # Create components
        created = 0
        for name in COMPONENTS:
            _, was_created = FeedbackComponent.objects.get_or_create(name=name)
            if was_created:
                created += 1
        self.stdout.write(f'Components: {created} created, {len(COMPONENTS) - created} already existed')

        # Create demo feedback if partner users exist
        partner_usernames = ['djenkins', 'jsnyder', 'msnyder']
        partners = User.objects.filter(username__in=partner_usernames)
        if not partners.exists():
            self.stdout.write(self.style.WARNING('No partner users found — skipping demo feedback'))
            return

        if PartnerFeedback.objects.exists():
            self.stdout.write('Demo feedback already exists — skipping')
            return

        demo_items = [
            {
                'username': 'djenkins',
                'component': 'AI Explanations',
                'title': 'AI persona tone feels inconsistent',
                'description': 'The New York Bookie persona sometimes slips into neutral analyst tone mid-response. Would be better if the persona voice stayed consistent throughout the entire explanation.',
                'status': 'NEW',
            },
            {
                'username': 'jsnyder',
                'component': 'Value Board',
                'title': 'Edge values hard to scan on mobile',
                'description': 'When looking at the value board on iPhone, the edge percentages are small and hard to quickly scan. Maybe make the edge number larger or add color-coded backgrounds to make positive/negative edges pop more.',
                'status': 'ACCEPTED',
                'reviewer_notes': '',
            },
            {
                'username': 'msnyder',
                'component': 'Model Accuracy',
                'title': 'House model seems to underweight home field advantage in CBB',
                'description': 'Watching CBB games this week, it seems like the house model is undervaluing home court in conference play. Teams at home are covering at a higher rate than the model suggests. Might want to bump the HFA weight for CBB conference games.',
                'status': 'READY',
                'reviewer_notes': 'Good observation. We should A/B test a higher HFA coefficient for conference games. Data supports this — home teams in conference play cover at ~55% vs the 51% the model implies.',
            },
            {
                'username': 'djenkins',
                'component': 'Navigation & UX',
                'title': 'Need a way to quickly compare two games',
                'description': 'Sometimes I want to compare the model output of two games side by side. Right now I have to flip back and forth. A compare mode or split view would be useful.',
                'status': 'DISMISSED',
                'reviewer_notes': 'Good idea but low priority for current phase. Revisit after we ship the core analytics pipeline improvements.',
            },
            {
                'username': 'jsnyder',
                'component': 'Performance Dashboard',
                'title': 'Calibration table needs more granularity',
                'description': 'The calibration table groups predictions in 10% buckets. Would be more useful to see 5% buckets, especially in the 50-70% range where most of our predictions land. The current bucketing hides interesting patterns.',
                'status': 'NEW',
            },
        ]

        for item in demo_items:
            user = partners.filter(username=item['username']).first()
            if not user:
                continue
            component = FeedbackComponent.objects.get(name=item['component'])
            PartnerFeedback.objects.create(
                user=user,
                component=component,
                title=item['title'],
                description=item['description'],
                status=item['status'],
                reviewer_notes=item.get('reviewer_notes', ''),
            )

        self.stdout.write(self.style.SUCCESS(f'Created {len(demo_items)} demo feedback items'))
