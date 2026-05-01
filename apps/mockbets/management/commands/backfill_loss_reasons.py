"""Re-classify settled losses whose loss_reason is empty or 'unknown'.

Why this command exists:
    The Loss Breakdown widget on /mockbets/analytics/ was showing ~48%
    of losses as "Unknown" because the original analyze_loss() bailed
    to UNKNOWN whenever the snapshot fields (recommendation_confidence
    + expected_edge) were missing — and a lot of legacy bets pre-date
    those fields being captured at settlement time.

    On 2026-04-30 analyze_loss gained a fallback path that classifies
    using the always-present implied_probability + confidence_level.
    This command sweeps existing rows so they pick up the new
    classification without waiting for a re-settle.

Idempotent: running twice on the same row is a no-op for already-
classified bets unless --force is passed.

Usage:
    python manage.py backfill_loss_reasons              # only blank/unknown
    python manage.py backfill_loss_reasons --force      # all losses
    python manage.py backfill_loss_reasons --dry-run    # preview only
"""
from django.core.management.base import BaseCommand

from apps.mockbets.models import MockBet
from apps.mockbets.services.loss_analysis import analyze_loss


class Command(BaseCommand):
    help = 'Re-classify settled losses whose loss_reason is missing or unknown.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Re-classify ALL losses, even those that already have a reason.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change without writing.',
        )

    def handle(self, *args, **options):
        qs = MockBet.objects.filter(result='loss')
        if not options['force']:
            # Only touch rows where the previous classification was
            # blank or the catch-all UNKNOWN.
            qs = qs.filter(loss_reason__in=['', 'unknown'])

        total = qs.count()
        self.stdout.write(f'Sweeping {total} loss row(s)...')

        # Counters keyed by (old_reason → new_reason) so we can show
        # exactly what shifted. Useful for ops + a sanity check that
        # the fallback isn't doing something wild.
        from collections import Counter
        transitions = Counter()
        no_change = 0
        updated = 0

        for bet in qs.iterator():
            result = analyze_loss(bet)
            new_reason = result['primary_reason']
            old_reason = bet.loss_reason or '(blank)'
            if new_reason == bet.loss_reason:
                no_change += 1
                continue
            transitions[(old_reason, new_reason)] += 1
            if not options['dry_run']:
                bet.loss_reason = new_reason
                # confidence_miss / edge_miss are nullable Decimal columns
                # that the existing analyze_loss return shape provides.
                bet.confidence_miss = result.get('confidence_miss')
                bet.edge_miss = result.get('edge_miss')
                bet.save(update_fields=[
                    'loss_reason', 'confidence_miss', 'edge_miss',
                ])
            updated += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{"Would update" if options["dry_run"] else "Updated"}: '
            f'{updated} · No change: {no_change}'
        ))
        if transitions:
            self.stdout.write('')
            self.stdout.write('Transitions:')
            for (old, new), count in sorted(transitions.items(), key=lambda x: -x[1]):
                self.stdout.write(f'  {old:20s} → {new:20s}  {count}')
