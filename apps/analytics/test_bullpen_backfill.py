"""v3.3 SHADOW — tests for the bullpen backfill operationalization.

Covers:
  1. BullpenBackfillRun model — states, elapsed_seconds, defaults.
  2. Staff-only access on status page, POST trigger, integrity audit.
  3. Trigger concurrency guard — 409 (soft-fail redirect) when a run
     is already running.
  4. Integrity audit runs cleanly on empty and populated tables.
  5. Stale-data safety in bullpen service — degrades to zero when the
     newest snapshot before reference_date is older than the threshold.
"""
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import BullpenBackfillRun
from apps.mlb.models import (
    Conference, Game, RelieverAppearance, StartingPitcher, Team,
    TeamBullpenSnapshot,
)


def _mk_team(slug):
    c, _ = Conference.objects.get_or_create(slug=f'div-{slug}', defaults={'name': 'Div'})
    return Team.objects.create(
        name=f'T-{slug}', slug=f't-{slug}', conference=c,
        rating=50.0, elo_rating=1500,
        source='mlb_stats_api', external_id=f'ext-{slug}',
        abbreviation=slug[:5].upper(),
    )


# ---------------------------------------------------------------------------
# 1. Model


class BullpenBackfillRunModelTests(TestCase):

    def test_defaults(self):
        r = BullpenBackfillRun.objects.create(
            date_from=date(2026, 5, 1), date_to=date(2026, 8, 21),
        )
        self.assertEqual(r.status, 'pending')
        self.assertEqual(r.kind, 'historical')
        self.assertEqual(r.phase, 'starting')
        self.assertEqual(r.appearances_created, 0)
        self.assertEqual(r.elapsed_seconds, 0)

    def test_elapsed_seconds_after_start(self):
        r = BullpenBackfillRun.objects.create(
            date_from=date(2026, 5, 1), date_to=date(2026, 8, 21),
            started_at=timezone.now() - timedelta(seconds=42),
        )
        self.assertGreaterEqual(r.elapsed_seconds, 41)


# ---------------------------------------------------------------------------
# 2. Access control


class AccessControlTests(TestCase):

    def _staff(self):
        return User.objects.create_user('s', 's@x.com', 'pw', is_staff=True)

    def _regular(self):
        return User.objects.create_user('r', 'r@x.com', 'pw')

    def test_status_page_requires_staff(self):
        c = Client()
        r = c.get(reverse('analytics:bullpen_backfill'))
        # Anon → login redirect (302) or forbidden.
        self.assertIn(r.status_code, (302, 403))
        c.force_login(self._regular())
        r = c.get(reverse('analytics:bullpen_backfill'))
        self.assertEqual(r.status_code, 403)
        c.force_login(self._staff())
        r = c.get(reverse('analytics:bullpen_backfill'))
        self.assertEqual(r.status_code, 200)

    def test_trigger_endpoint_requires_staff(self):
        c = Client()
        r = c.post(reverse('analytics:trigger_bullpen_backfill'))
        self.assertIn(r.status_code, (302, 403))
        c.force_login(self._regular())
        r = c.post(reverse('analytics:trigger_bullpen_backfill'))
        self.assertEqual(r.status_code, 403)

    def test_integrity_audit_requires_staff(self):
        c = Client()
        r = c.get(reverse('analytics:bullpen_integrity'))
        self.assertIn(r.status_code, (302, 403))
        c.force_login(self._staff())
        r = c.get(reverse('analytics:bullpen_integrity'))
        self.assertEqual(r.status_code, 200)
        # Plaintext body, always mentions the audit heading.
        self.assertIn(b'INTEGRITY AUDIT', r.content)


# ---------------------------------------------------------------------------
# 3. Concurrency guard


class ConcurrencyGuardTests(TestCase):

    def test_second_trigger_is_rejected_softly_when_run_is_active(self):
        staff = User.objects.create_user('s2', 's2@x.com', 'pw', is_staff=True)
        c = Client()
        c.force_login(staff)
        # Pretend a run is already in flight.
        BullpenBackfillRun.objects.create(
            kind='historical',
            status='running',
            phase='ingest_appearances',
            date_from=date(2026, 5, 1), date_to=date(2026, 8, 21),
            started_at=timezone.now(),
        )
        # Second trigger: redirect back with a warning (soft fail).
        r = c.post(
            reverse('analytics:trigger_bullpen_backfill'),
            data={'date_from': '2026-05-01', 'date_to': '2026-08-21'},
        )
        # Redirect back to the status page.
        self.assertEqual(r.status_code, 302)
        self.assertIn('bullpen-backfill', r.url)
        # No new row created.
        self.assertEqual(BullpenBackfillRun.objects.count(), 1)


# ---------------------------------------------------------------------------
# 4. Integrity audit runs cleanly


class IntegrityAuditTests(TestCase):

    def test_audit_runs_on_empty_database(self):
        from apps.analytics.services.bullpen_backfill_service import integrity_audit
        report = integrity_audit(sample_size=10)
        # No data → no duplicates → passes the "no duplicates" check.
        checks = {f['check']: f['result'] for f in report['findings']}
        self.assertEqual(checks.get('no_duplicate_reliever_appearances'), 'PASS')
        self.assertEqual(checks.get('no_duplicate_snapshots'), 'PASS')
        self.assertIn(report['overall'], ('PASS', 'FAIL'))

    def test_audit_populates_all_expected_checks(self):
        from apps.analytics.services.bullpen_backfill_service import integrity_audit
        report = integrity_audit(sample_size=10)
        expected = {
            'no_duplicate_reliever_appearances',
            'no_duplicate_snapshots',
            'snapshot_determinism_sample',
            'each_team_has_one_starter_per_game_sample',
            'all_teams_represented_in_appearances',
            'coverage_last_60_days',
            'data_confidence_distribution',
        }
        actual = {f['check'] for f in report['findings']}
        missing = expected - actual
        self.assertEqual(missing, set(), msg=f'audit missing checks: {missing}')


# ---------------------------------------------------------------------------
# 5. Stale-data safety in bullpen service


class StaleDataSafetyTests(TestCase):

    def test_snapshot_older_than_threshold_degrades_to_zero(self):
        from apps.mlb.services.bullpen import (
            STALE_THRESHOLD_DAYS, team_bullpen_signal,
        )
        team = _mk_team('sd1')
        ref = timezone.now()
        # Snapshot is well-past the stale threshold (5 days > default 3d).
        TeamBullpenSnapshot.objects.create(
            team=team,
            as_of=ref - timedelta(days=STALE_THRESHOLD_DAYS + 2),
            bullpen_era=2.50, data_confidence='high',
        )
        sig = team_bullpen_signal(team, ref)
        # Even though the snapshot has strong signal (2.50 pen), the
        # stale-data guard must zero it out.
        self.assertEqual(sig.quality_delta, 0.0)
        self.assertEqual(sig.fatigue_delta, 0.0)
        self.assertEqual(sig.data_confidence, 'low')
        # snapshot_as_of is preserved for operator debug — tells them a
        # snapshot exists but was rejected as stale.
        self.assertIsNotNone(sig.snapshot_as_of)

    def test_snapshot_within_threshold_produces_signal(self):
        from apps.mlb.services.bullpen import (
            STALE_THRESHOLD_DAYS, team_bullpen_signal,
        )
        team = _mk_team('sd2')
        ref = timezone.now()
        TeamBullpenSnapshot.objects.create(
            team=team,
            as_of=ref - timedelta(days=STALE_THRESHOLD_DAYS - 1),
            bullpen_era=2.50, data_confidence='high',
        )
        sig = team_bullpen_signal(team, ref)
        self.assertGreater(sig.quality_delta, 0.0)

    def test_stale_guard_uses_reference_date_not_now(self):
        """Replay of a historical game G must judge staleness relative
        to G.first_pitch, not to today. Otherwise every historical
        replay would think all snapshots are stale."""
        from apps.mlb.services.bullpen import team_bullpen_signal
        team = _mk_team('sd3')
        # Simulate a game 60 days ago.
        game_time = timezone.now() - timedelta(days=60)
        # Snapshot captured 1 day BEFORE the game — fresh from G's POV.
        TeamBullpenSnapshot.objects.create(
            team=team,
            as_of=game_time - timedelta(days=1),
            bullpen_era=3.00, data_confidence='high',
        )
        sig = team_bullpen_signal(team, game_time)
        # Should be treated as fresh (relative to game_time), not stale.
        self.assertGreater(sig.quality_delta, 0.0)
