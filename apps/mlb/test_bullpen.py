"""v3.3 SHADOW — bullpen feature tests.

Locks the load-bearing invariants of the shadow-only bullpen layer:

  1. LEAKAGE. A game G on date D never reads a `TeamBullpenSnapshot`
     row with `as_of >= G.first_pitch`. Strict `<` is enforced by
     `team_bullpen_signal`; this test verifies that a snapshot captured
     at first_pitch exactly is NOT returned (and one captured 1s
     before IS).

  2. FLAG OFF INVARIANCE. When `USE_BULLPEN_QUALITY=False` (default),
     `_score` produces IDENTICAL numeric score with and without bullpen
     data present. Same for `USE_BULLPEN_FATIGUE`.

  3. SHADOW CAPTURE. `_score(return_breakdown=True)` populates the
     `home_bullpen_quality_delta` / `away_bullpen_quality_delta` /
     `bullpen_quality_contribution` / etc. keys REGARDLESS of flag
     state — the audit trail is always captured.

  4. NO-DATA POSTURE. When TeamBullpenSnapshot is empty (production
     reality as of 2026-08-22), `team_bullpen_signal` returns
     `(0.0, 0.0, 'low', None)` and never raises.

  5. INGEST SCAFFOLD. The `ingest_bullpen_snapshots` management
     command runs as a no-op without hitting external APIs and
     without crashing.

  6. REPLAY EXPERIMENT. `run_bullpen_experiment` returns a well-formed
     result dict with coverage reporting; when coverage is 0 the
     renderer flags the run INFRASTRUCTURE-ONLY.

  7. VIEW SURFACES. `?experiment=bullpen` returns 200 for staff, 403
     for non-staff.
"""
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.mlb.models import (
    Conference, Game, OddsSnapshot, StartingPitcher, Team,
    TeamBullpenSnapshot,
)
from apps.mlb.services.bullpen import (
    LEAGUE_AVG_BULLPEN_ERA,
    QUALITY_ABS_CAP,
    QUALITY_SCALE_FACTOR,
    BullpenSignal,
    fatigue_delta,
    quality_delta,
    team_bullpen_signal,
)
from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score


def _mk_team(slug):
    c, _ = Conference.objects.get_or_create(
        slug=f'div-{slug}', defaults={'name': 'Div'},
    )
    return Team.objects.create(
        name=f'T-{slug}', slug=f't-{slug}', conference=c,
        rating=50.0, elo_rating=1500,
        source='mlb_stats_api', external_id=f't-{slug}',
    )


def _mk_pitcher(team, rating=50.0):
    return StartingPitcher.objects.create(
        team=team, name=f'P-{team.slug}', rating=rating,
        source='mlb_stats_api', external_id=f'p-{team.slug}',
    )


def _mk_game(home, away, *, first_pitch=None, home_pitcher=None,
             away_pitcher=None, neutral_site=False):
    fp = first_pitch or (timezone.now() + timedelta(days=1))
    return Game.objects.create(
        home_team=home, away_team=away, first_pitch=fp,
        home_pitcher=home_pitcher, away_pitcher=away_pitcher,
        status='scheduled', neutral_site=neutral_site,
        source='mlb_stats_api',
        external_id=f'g-{home.slug}-{away.slug}-{int(fp.timestamp())}',
    )


# ---------------------------------------------------------------------------
# 1. Leakage discipline (mandatory)


class LeakageDisciplineTests(TestCase):

    def test_snapshot_captured_at_first_pitch_is_NOT_returned(self):
        """Strict `<` means a snapshot captured AT first_pitch is excluded.
        This is the leakage invariant every downstream layer depends on."""
        team = _mk_team('leak1')
        fp = timezone.now()
        # Snapshot exactly at first_pitch — must be excluded.
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=fp, bullpen_era=3.00,
            data_confidence='high',
        )
        # Reference date is first_pitch.
        sig = team_bullpen_signal(team, reference_date=fp)
        # No snapshot precedes → signal is empty (zero).
        self.assertEqual(sig.quality_delta, 0.0)
        self.assertIsNone(sig.snapshot_as_of)
        self.assertEqual(sig.data_confidence, 'low')

    def test_snapshot_one_second_before_first_pitch_IS_returned(self):
        team = _mk_team('leak2')
        fp = timezone.now()
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=fp - timedelta(seconds=1),
            bullpen_era=3.20, data_confidence='high',
        )
        sig = team_bullpen_signal(team, reference_date=fp)
        self.assertIsNotNone(sig.snapshot_as_of)
        # 3.20 vs league avg 4.20 → quality delta positive.
        self.assertGreater(sig.quality_delta, 0.0)

    def test_multiple_snapshots_picks_most_recent_before(self):
        team = _mk_team('leak3')
        fp = timezone.now()
        # Old snapshot: bad pen.
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=fp - timedelta(days=30),
            bullpen_era=5.00, data_confidence='high',
        )
        # Recent snapshot: good pen. THIS one should feed the signal.
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=fp - timedelta(hours=6),
            bullpen_era=3.00, data_confidence='high',
        )
        # Post-game snapshot: must be ignored.
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=fp + timedelta(hours=1),
            bullpen_era=2.00, data_confidence='high',
        )
        sig = team_bullpen_signal(team, reference_date=fp)
        # Should reflect the 3.00 snapshot, not 5.00 or 2.00.
        expected = (LEAGUE_AVG_BULLPEN_ERA - 3.00) * QUALITY_SCALE_FACTOR
        self.assertAlmostEqual(sig.quality_delta, expected, places=4)

    def test_reference_date_none_defaults_to_now_for_live_scoring(self):
        """When reference_date is None (live path), the service defaults
        to timezone.now(). A snapshot captured 1s ago is BEFORE now so
        it IS visible. This is the correct live-scoring behavior."""
        team = _mk_team('leak4')
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=timezone.now() - timedelta(seconds=1),
            bullpen_era=3.00, data_confidence='high',
        )
        sig = team_bullpen_signal(team, reference_date=None)
        # Signal IS available under live-scoring default.
        self.assertIsNotNone(sig.snapshot_as_of)
        self.assertGreater(sig.quality_delta, 0.0)


# ---------------------------------------------------------------------------
# 2. Flag OFF invariance


class FlagOffInvarianceTests(TestCase):
    """Even when bullpen data is present, the FLAG being OFF means the
    score is identical to the pre-v3.3 baseline. Locks the shadow-only
    posture — no accidental production activation via ingested data."""

    def _mk_game_with_pitchers(self):
        home = _mk_team('fi-h')
        away = _mk_team('fi-a')
        hp = _mk_pitcher(home, rating=60.0)
        ap = _mk_pitcher(away, rating=45.0)
        return _mk_game(home, away, home_pitcher=hp, away_pitcher=ap), home, away

    @override_settings(USE_BULLPEN_QUALITY=False, USE_BULLPEN_FATIGUE=False)
    def test_score_identical_with_and_without_snapshots(self):
        game, home, away = self._mk_game_with_pitchers()
        # Score with NO snapshots.
        score_no_data = _score(game, HOUSE_WEIGHTS)
        # Populate snapshots so the shadow signal WOULD be non-zero if
        # activated.
        past = timezone.now() - timedelta(hours=6)
        TeamBullpenSnapshot.objects.create(
            team=home, as_of=past, bullpen_era=2.50,
            data_confidence='high',
        )
        TeamBullpenSnapshot.objects.create(
            team=away, as_of=past, bullpen_era=5.50,
            data_confidence='high',
        )
        # With flag OFF, score MUST NOT change.
        score_with_data = _score(game, HOUSE_WEIGHTS)
        self.assertEqual(score_no_data, score_with_data)

    @override_settings(USE_BULLPEN_QUALITY=True, USE_BULLPEN_FATIGUE=False)
    def test_score_changes_when_flag_on_and_data_present(self):
        # Positive control — with flag ON and data present, score must
        # differ from baseline. If this test ever fails alongside the
        # flag_off test, the flag routing is broken.
        game, home, away = self._mk_game_with_pitchers()
        # Baseline with NO data.
        baseline = _score(game, HOUSE_WEIGHTS)
        # Add snapshots.
        past = timezone.now() - timedelta(hours=6)
        TeamBullpenSnapshot.objects.create(
            team=home, as_of=past, bullpen_era=2.50,
            data_confidence='high',
        )
        TeamBullpenSnapshot.objects.create(
            team=away, as_of=past, bullpen_era=5.50,
            data_confidence='high',
        )
        with_data = _score(game, HOUSE_WEIGHTS)
        self.assertNotEqual(baseline, with_data)
        # Home pen (2.50) is much better than away (5.50) → score up.
        self.assertGreater(with_data, baseline)

    @override_settings(USE_BULLPEN_QUALITY=False, USE_BULLPEN_FATIGUE=False)
    def test_bullpen_fatigue_flag_independently_gated(self):
        """The fatigue flag toggles independently. With quality False +
        fatigue True, only fatigue signal enters the score."""
        game, home, away = self._mk_game_with_pitchers()
        past = timezone.now() - timedelta(hours=6)
        # Snapshot with ONLY fatigue signal (top_reliever_available=False).
        TeamBullpenSnapshot.objects.create(
            team=home, as_of=past,
            top_reliever_available=False,
            data_confidence='high',
        )
        # Even a top-reliever-unavailable flag on the home team must not
        # change the score with both flags OFF.
        s_off = _score(game, HOUSE_WEIGHTS)
        with override_settings(USE_BULLPEN_QUALITY=False, USE_BULLPEN_FATIGUE=True):
            s_fatigue_on = _score(game, HOUSE_WEIGHTS)
        # With fatigue on, the home team suffers → score decreases.
        self.assertLess(s_fatigue_on, s_off)


# ---------------------------------------------------------------------------
# 3. Shadow capture: contributions stored regardless of flag


class ShadowCaptureTests(TestCase):

    @override_settings(USE_BULLPEN_QUALITY=False, USE_BULLPEN_FATIGUE=False)
    def test_breakdown_carries_bullpen_keys_when_flags_off(self):
        home = _mk_team('sc-h'); away = _mk_team('sc-a')
        hp = _mk_pitcher(home); ap = _mk_pitcher(away)
        game = _mk_game(home, away, home_pitcher=hp, away_pitcher=ap)
        _, breakdown = _score(game, HOUSE_WEIGHTS, return_breakdown=True)
        # Even with flags OFF, shadow contribution keys must be present.
        self.assertIn('home_bullpen_quality_delta', breakdown)
        self.assertIn('away_bullpen_quality_delta', breakdown)
        self.assertIn('home_bullpen_fatigue_delta', breakdown)
        self.assertIn('away_bullpen_fatigue_delta', breakdown)
        self.assertIn('bullpen_quality_contribution', breakdown)
        self.assertIn('bullpen_fatigue_contribution', breakdown)
        self.assertIn('home_bullpen_data_confidence', breakdown)
        self.assertIn('away_bullpen_data_confidence', breakdown)
        self.assertIn('use_bullpen_quality', breakdown)
        self.assertIn('use_bullpen_fatigue', breakdown)
        # Zero when no data.
        self.assertEqual(breakdown['bullpen_quality_contribution'], 0.0)
        self.assertEqual(breakdown['bullpen_fatigue_contribution'], 0.0)
        self.assertEqual(breakdown['home_bullpen_data_confidence'], 'low')

    def test_breakdown_reflects_populated_snapshots(self):
        home = _mk_team('scp-h'); away = _mk_team('scp-a')
        hp = _mk_pitcher(home); ap = _mk_pitcher(away)
        game = _mk_game(home, away, home_pitcher=hp, away_pitcher=ap)
        past = timezone.now() - timedelta(hours=3)
        TeamBullpenSnapshot.objects.create(
            team=home, as_of=past, bullpen_era=2.80,
            data_confidence='high',
        )
        _, breakdown = _score(game, HOUSE_WEIGHTS, return_breakdown=True)
        # Home has a good pen, away has none → home quality > 0, away = 0.
        self.assertGreater(breakdown['home_bullpen_quality_delta'], 0.0)
        self.assertEqual(breakdown['away_bullpen_quality_delta'], 0.0)
        self.assertEqual(breakdown['home_bullpen_data_confidence'], 'high')
        self.assertEqual(breakdown['away_bullpen_data_confidence'], 'low')


# ---------------------------------------------------------------------------
# 4. No-data posture (production reality on 2026-08-22)


class NoDataPostureTests(TestCase):

    def test_signal_when_snapshot_table_empty(self):
        team = _mk_team('nd1')
        sig = team_bullpen_signal(team, reference_date=timezone.now())
        self.assertEqual(sig, BullpenSignal(0.0, 0.0, 'low', None))

    def test_never_raises_on_missing_data(self):
        team = _mk_team('nd2')
        try:
            quality_delta(team, reference_date=timezone.now())
            fatigue_delta(team, reference_date=timezone.now())
        except Exception as e:
            self.fail(f'bullpen service raised on missing data: {e}')

    def test_quality_cap_applied(self):
        team = _mk_team('nd3')
        fp = timezone.now()
        # Absurd pen (ERA 0.10) — raw quality would be 32.8 rating units;
        # cap must clamp to QUALITY_ABS_CAP.
        TeamBullpenSnapshot.objects.create(
            team=team, as_of=fp - timedelta(hours=1),
            bullpen_era=0.10, data_confidence='high',
        )
        sig = team_bullpen_signal(team, reference_date=fp)
        self.assertLessEqual(sig.quality_delta, QUALITY_ABS_CAP)


# ---------------------------------------------------------------------------
# 5. Ingestion scaffold


class IngestScaffoldTests(TestCase):

    def test_no_op_ingest_runs_cleanly(self):
        out = StringIO()
        # No external API hit; no rows written; no exception.
        call_command('ingest_bullpen_snapshots', stdout=out)
        self.assertEqual(TeamBullpenSnapshot.objects.count(), 0)
        self.assertIn('scaffolded', out.getvalue().lower())

    def test_force_dummy_writes_zero_value_snapshots(self):
        _mk_team('dumm1'); _mk_team('dumm2')
        out = StringIO()
        call_command('ingest_bullpen_snapshots', '--force-dummy', stdout=out)
        self.assertEqual(TeamBullpenSnapshot.objects.count(), 2)
        for snap in TeamBullpenSnapshot.objects.all():
            self.assertEqual(snap.data_confidence, 'low')
            self.assertIsNone(snap.bullpen_era)


# ---------------------------------------------------------------------------
# 6. Replay experiment


class ReplayExperimentTests(TestCase):

    def test_experiment_runs_on_empty_slate(self):
        """No games in window → data_ok=False; coverage 0. Runs cleanly."""
        from apps.analytics.services.bullpen_replay import run_bullpen_experiment
        # No games created — window will be empty.
        exp = run_bullpen_experiment(days=7)
        self.assertFalse(exp['data_ok'])
        self.assertEqual(exp['coverage']['total_games'], 0)
        # Should still return well-formed structure.
        self.assertIn('a_v3_2_baseline', exp)
        self.assertIn('b_plus_quality', exp)
        self.assertIn('c_plus_quality_and_fatigue', exp)
        self.assertEqual(exp['a_v3_2_baseline']['count'], 0)

    def test_render_flags_no_data_as_infrastructure_only(self):
        from apps.analytics.services.bullpen_replay import (
            render_bullpen_experiment, run_bullpen_experiment,
        )
        exp = run_bullpen_experiment(days=7)
        body = render_bullpen_experiment(exp)
        self.assertIn('INFRASTRUCTURE-ONLY', body)
        self.assertIn('NO EVIDENCE PRODUCED', body)


# ---------------------------------------------------------------------------
# 7. View surface


class ViewSurfaceTests(TestCase):

    def test_staff_gets_200(self):
        staff = User.objects.create_user(
            'bstaff', 'bstaff@example.com', 'pw', is_staff=True,
        )
        c = Client()
        c.force_login(staff)
        r = c.get('/analytics/method-replay/?experiment=bullpen&days=7')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'BULLPEN EXPERIMENT', r.content)

    def test_non_staff_gets_403(self):
        user = User.objects.create_user('breg', 'breg@example.com', 'pw')
        c = Client()
        c.force_login(user)
        r = c.get('/analytics/method-replay/?experiment=bullpen&days=7')
        self.assertEqual(r.status_code, 403)
