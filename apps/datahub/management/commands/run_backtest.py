"""Run the moneyline backtesting service and persist a BacktestRun.

Idempotent in the sense that it never mutates any source data; each
invocation creates a new BacktestRun row tagged with `created_at`.
Re-running the same range produces a new row rather than updating —
that's intentional so we keep a history of run results and can compare
how methodology changes affected aggregations.

Examples:
  python manage.py run_backtest                    # all sports, all history
  python manage.py run_backtest --sport mlb
  python manage.py run_backtest --start 2026-01-01 --end 2026-04-01
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.backtesting_service import run_backtest


SUPPORTED_SPORTS = ['all', 'cfb', 'cbb', 'mlb', 'college_baseball']


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise CommandError(f"Invalid date '{value}' (expected YYYY-MM-DD)")


class Command(BaseCommand):
    help = 'Reconstruct historical moneyline recommendations and persist a BacktestRun.'

    def add_arguments(self, parser):
        parser.add_argument('--sport', type=str, choices=SUPPORTED_SPORTS, default='all')
        parser.add_argument('--start', type=str, default=None,
                            help='Inclusive start date (YYYY-MM-DD).')
        parser.add_argument('--end', type=str, default=None,
                            help='Inclusive end date (YYYY-MM-DD).')

    def handle(self, *args, **options):
        sport = options['sport']
        start = _parse_date(options.get('start'))
        end = _parse_date(options.get('end'))

        run = run_backtest(sport=sport, start_date=start, end_date=end, persist=True)

        overall = run.summary.get('overall', {})
        self.stdout.write(self.style.SUCCESS(
            f"Backtest {run.id} complete: "
            f"{run.games_evaluated} evaluated, {run.games_skipped} skipped, "
            f"approximate={run.is_approximate}"
        ))
        if overall.get('sample'):
            self.stdout.write(
                f"  Overall: {overall['sample']} bets, "
                f"win_rate={overall['win_rate']}%, "
                f"roi_pct={overall['roi_pct']}%"
            )
        if run.notes:
            for line in run.notes.splitlines():
                self.stdout.write(self.style.WARNING(f"  {line}"))
