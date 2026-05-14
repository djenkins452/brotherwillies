"""Capture a Recommendation Health Snapshot — manual or cron.

Usage:
    # Standard daily cron capture (14-day window, no notes).
    python manage.py capture_health_snapshot

    # Custom window.
    python manage.py capture_health_snapshot --window 30

    # Tagged baseline capture (e.g., before flipping USE_DYNAMIC_RATINGS).
    python manage.py capture_health_snapshot --notes "pre-elo baseline"

    # Inspect without persisting.
    python manage.py capture_health_snapshot --dry-run

Reads live aggregates from MockBet + BettingRecommendation. Writes one
RecommendationHealthSnapshot row. Cannot modify recommendation
behavior. Cannot modify calibration constants. Safe to run as often
as desired.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Capture a Recommendation Health Snapshot. Read-only with respect '
        'to recommendation engine state; writes one RecommendationHealthSnapshot '
        'row. Use --notes to tag baselines or post-change captures.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--window', type=int, default=14,
            help='Window in days for aggregations (default 14).',
        )
        parser.add_argument(
            '--notes', type=str, default='',
            help='Free-text tag for the snapshot (e.g. "pre-elo baseline").',
        )
        parser.add_argument(
            '--dry-run', action='store_true', default=False,
            help='Compute the score and print it; do not persist.',
        )

    def handle(self, *args, **options):
        from apps.analytics.services.health_score import (
            compute_health_score, detect_warnings,
        )
        from apps.analytics.services.health_snapshot import capture_snapshot

        window = options['window']
        notes = options['notes']
        dry_run = options['dry_run']

        health = compute_health_score(window_days=window)
        warnings = detect_warnings(health)

        # Pretty-print the result either way so cron / operator can see it.
        self.stdout.write(f'Health Score window={window}d')
        self.stdout.write(
            f'  overall: '
            f'{health.overall_score if health.overall_score is not None else "n/a"} '
            f'(band: {health.band or "n/a"})'
        )
        self.stdout.write(f'  rating mode active: {health.rating_mode_active}')
        for key, info in health.dimension_scores.items():
            score = info.get('score')
            status = info.get('status', 'n/a')
            value = info.get('value')
            self.stdout.write(
                f'    {key}: score={score} status={status} value={value}'
            )
        if warnings:
            self.stdout.write('  warnings:')
            for w in warnings:
                self.stdout.write(
                    f"    [{w['severity']}] {w['dimension']}: {w['message']}"
                )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Dry run — snapshot NOT persisted.'
            ))
            return

        snap = capture_snapshot(window_days=window, notes=notes, health=health)
        self.stdout.write(self.style.SUCCESS(
            f'Snapshot persisted: {snap.id}'
            + (f' (notes: "{notes}")' if notes else '')
        ))
