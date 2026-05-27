"""Three-Population Audit — empirical proof harness for the Production Truth Audit.

Thin CLI wrapper over apps.mockbets.services.three_population_audit. Splits
the trailing-N-day MLB moneyline MockBet set into three populations:

    1. ALL ACTUAL BETS PLACED
    2. TRUE SYSTEM-APPROVED BETS  (production-equivalence filter set)
    3. MANUAL / CONTAMINATED BETS  ((1) minus (2))

Usage:
    python manage.py audit_three_populations
    python manage.py audit_three_populations --days 30
    python manage.py audit_three_populations --days 30 --user demo
    python manage.py audit_three_populations --days 30 --settled-only

READ-ONLY. No DB writes.

On Railway, prefer the staff-only HTTP view at
/mockbets/audit/three-populations/ since there is no interactive shell.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.mockbets.models import MockBet
from apps.mockbets.services.three_population_audit import build_audit, render_report


class Command(BaseCommand):
    help = (
        "Three-Population Audit for MLB moneyline MockBets. "
        "Read-only. Prints hard numbers for ALL ACTUAL / TRUE SYSTEM-APPROVED / "
        "MANUAL-CONTAMINATED populations over a date window."
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Trailing window in days (default: 30).')
        parser.add_argument('--user', type=str, default=None,
                            help='Restrict to a single username. Omit for all users.')
        parser.add_argument('--sport', type=str, default='mlb',
                            help='Sport filter (default: mlb).')
        parser.add_argument('--settled-only', action='store_true',
                            help='Exclude pending bets from the populations.')

    def handle(self, *args, **opts):
        now = timezone.now()
        cutoff = now - timedelta(days=opts['days'])
        audit = build_audit(
            MockBet.objects.all(),
            cutoff=cutoff,
            now=now,
            days=opts['days'],
            sport=opts['sport'],
            username=opts['user'],
            settled_only=opts['settled_only'],
        )
        self.stdout.write(render_report(audit))
