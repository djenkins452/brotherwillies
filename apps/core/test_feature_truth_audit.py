"""Regression locks for the Phase 1A Feature Truth Audit.

Each test asserts which weight keys a sport's score formula actually
consumes. Built by perturbing one key at a time and observing whether
the output changes — if a key is "used", changing its weight changes
the score; if it's a phantom, the score is identical regardless.

When a future change wires a previously-phantom key (or accidentally
disables a previously-used one), the corresponding test will flip and
demand an update — which is exactly the audit lock we want.

The truth captured here is documented in
`docs/feature_truth_audit_2026_05_10.md`. Updates to that doc and
this file go together.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone


def _key_affects_score(score_fn, base_weights, key, perturb=2.0):
    """Return True iff bumping `weights[key]` changes score_fn output.

    Tolerance is wider than float-eq because some sport formulas have
    HFA / injury terms that can collapse to 0 by coincidence — a clean
    "the key was actually consumed" check needs the perturbation to
    produce an observable delta in the realistic operating range.
    """
    weights_a = dict(base_weights)
    weights_b = dict(base_weights)
    weights_b[key] = perturb
    a = score_fn(weights_a)
    b = score_fn(weights_b)
    return abs(a - b) > 1e-9


class MLBHouseWeightsTruthTests(TestCase):
    """`apps.mlb.services.model_service._score` reads exactly: rating,
    pitcher, hfa. `injury` is a documented phantom (Phase 1A audit)."""

    def setUp(self):
        from apps.mlb.models import (
            Conference, Game, StartingPitcher, Team,
        )
        league = Conference.objects.create(
            name='AL', slug=f'al-{timezone.now().timestamp()}',
        )
        home = Team.objects.create(
            name='H', slug=f'h-{id(self)}', conference=league, rating=70.0,
        )
        away = Team.objects.create(
            name='A', slug=f'a-{id(self)}', conference=league, rating=40.0,
        )
        hp = StartingPitcher.objects.create(
            team=home, name='HP', rating=70.0,
        )
        ap = StartingPitcher.objects.create(
            team=away, name='AP', rating=40.0,
        )
        self.game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=2),
            home_pitcher=hp, away_pitcher=ap,
        )

    def _score_with(self, weights):
        from apps.mlb.services.model_service import _score
        return _score(self.game, weights)

    def test_rating_is_consumed(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'rating'))

    def test_pitcher_is_consumed(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'pitcher'))

    def test_hfa_is_consumed(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'hfa'))

    def test_injury_is_phantom(self):
        # If this test starts failing, the injury term has been wired up
        # — celebrate, then update docs/feature_truth_audit_2026_05_10.md.
        from apps.mlb.services.model_service import HOUSE_WEIGHTS
        self.assertFalse(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'injury'))


class CollegeBaseballHouseWeightsTruthTests(TestCase):
    """Mirror of MLB. Same expectations."""

    def setUp(self):
        from apps.college_baseball.models import (
            Conference, Game, StartingPitcher, Team,
        )
        league = Conference.objects.create(
            name='SEC', slug=f'sec-{timezone.now().timestamp()}',
        )
        home = Team.objects.create(
            name='H', slug=f'h-cb-{id(self)}', conference=league, rating=70.0,
        )
        away = Team.objects.create(
            name='A', slug=f'a-cb-{id(self)}', conference=league, rating=40.0,
        )
        hp = StartingPitcher.objects.create(
            team=home, name='HP', rating=70.0,
        )
        ap = StartingPitcher.objects.create(
            team=away, name='AP', rating=40.0,
        )
        self.game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=2),
            home_pitcher=hp, away_pitcher=ap,
        )

    def _score_with(self, weights):
        from apps.college_baseball.services.model_service import _score
        return _score(self.game, weights)

    def test_rating_is_consumed(self):
        from apps.college_baseball.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'rating'))

    def test_pitcher_is_consumed(self):
        from apps.college_baseball.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'pitcher'))

    def test_hfa_is_consumed(self):
        from apps.college_baseball.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'hfa'))

    def test_injury_is_phantom(self):
        from apps.college_baseball.services.model_service import HOUSE_WEIGHTS
        self.assertFalse(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'injury'))


class CFBHouseWeightsTruthTests(TestCase):
    """`_compute_win_prob` reads: rating, hfa, injury. recent_form and
    conference are documented phantoms."""

    def setUp(self):
        from apps.cfb.models import Conference, Team, Game
        conf = Conference.objects.create(
            name='SEC', slug=f'sec-{timezone.now().timestamp()}',
        )
        self.home = Team.objects.create(
            name='H', slug=f'h-cfb-{id(self)}', conference=conf, rating=70.0,
        )
        self.away = Team.objects.create(
            name='A', slug=f'a-cfb-{id(self)}', conference=conf, rating=40.0,
        )
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now() + timedelta(hours=2),
        )

    def _score_with(self, weights):
        # _compute_win_prob takes injuries; pass [] to keep the test
        # focused on weight-key plumbing rather than injury data.
        from apps.cfb.services.model_service import _compute_win_prob
        return _compute_win_prob(self.game, [], weights)

    def test_rating_is_consumed(self):
        from apps.cfb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'rating'))

    def test_hfa_is_consumed(self):
        from apps.cfb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'hfa'))

    def test_injury_is_consumed_with_real_injury_data(self):
        # CFB injury weight only affects the score when injuries exist.
        # Use the full pipeline with a real InjuryImpact row.
        from apps.cfb.models import InjuryImpact
        from apps.cfb.services.model_service import _compute_win_prob, HOUSE_WEIGHTS
        InjuryImpact.objects.create(
            game=self.game, team=self.home, impact_level='high',
        )
        injuries = [InjuryImpact.objects.first()]
        a = _compute_win_prob(self.game, injuries, HOUSE_WEIGHTS)
        b = _compute_win_prob(self.game, injuries, dict(HOUSE_WEIGHTS, injury=2.0))
        self.assertNotAlmostEqual(a, b, places=6)

    def test_recent_form_is_phantom(self):
        from apps.cfb.services.model_service import HOUSE_WEIGHTS
        self.assertFalse(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'recent_form'))

    def test_conference_is_phantom(self):
        from apps.cfb.services.model_service import HOUSE_WEIGHTS
        self.assertFalse(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'conference'))


class CBBHouseWeightsTruthTests(TestCase):
    """Mirror of CFB. Same expectations."""

    def setUp(self):
        from apps.cbb.models import Conference, Team, Game
        conf = Conference.objects.create(
            name='B12', slug=f'b12-{timezone.now().timestamp()}',
        )
        self.home = Team.objects.create(
            name='H', slug=f'h-cbb-{id(self)}', conference=conf, rating=70.0,
        )
        self.away = Team.objects.create(
            name='A', slug=f'a-cbb-{id(self)}', conference=conf, rating=40.0,
        )
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            tipoff=timezone.now() + timedelta(hours=2),
        )

    def _score_with(self, weights):
        from apps.cbb.services.model_service import _compute_win_prob
        return _compute_win_prob(self.game, [], weights)

    def test_rating_is_consumed(self):
        from apps.cbb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'rating'))

    def test_hfa_is_consumed(self):
        from apps.cbb.services.model_service import HOUSE_WEIGHTS
        self.assertTrue(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'hfa'))

    def test_recent_form_is_phantom(self):
        from apps.cbb.services.model_service import HOUSE_WEIGHTS
        self.assertFalse(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'recent_form'))

    def test_conference_is_phantom(self):
        from apps.cbb.services.model_service import HOUSE_WEIGHTS
        self.assertFalse(_key_affects_score(self._score_with, HOUSE_WEIGHTS, 'conference'))


class StaticTeamRatingHasNoUpdaterTests(TestCase):
    """Locks the audit finding that `Team.rating` has no provider updater.

    This is a static structural assertion — the test scans the providers
    directory for any line that assigns `team.rating = ...` (the form a
    real updater would take). Today: zero hits.

    If a future change adds a real Team.rating updater, this test will
    flip and demand an update to the audit doc + a re-evaluation of
    Phase 1B (because the Elo cutover assumes static-rating staleness
    is the problem to solve).
    """

    def test_no_team_rating_updaters_in_providers(self):
        import os
        import re

        # Match `team.rating = ...` or `<some_team_var>.rating = ...`
        # but exclude `elo_rating` (which DOES have updaters by design).
        pat = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\.rating\s*=')
        elo = re.compile(r'\.elo_rating\s*=')

        offenders = []
        # Scan provider files and ingestion commands.
        roots = [
            'apps/datahub/providers',
            'apps/datahub/management/commands',
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    if not f.endswith('.py'):
                        continue
                    path = os.path.join(dirpath, f)
                    with open(path) as fh:
                        for ln, line in enumerate(fh, start=1):
                            if elo.search(line):
                                continue  # elo_rating updaters are intentional
                            m = pat.search(line)
                            if not m:
                                continue
                            var = m.group(1)
                            # Skip pitcher-rating updaters — pitcher.rating
                            # IS legitimately written by the stats provider.
                            if 'pitcher' in var.lower():
                                continue
                            offenders.append((path, ln, line.rstrip()))

        # If this assertion ever fires, a code change has introduced a
        # real Team.rating updater. That's a Phase 1B-relevant event:
        # the dynamic-rating cutover plan assumed static-rating staleness
        # was the problem to solve. Update the audit doc and re-evaluate.
        self.assertEqual(
            offenders, [],
            f'Unexpected Team.rating updater(s) found: {offenders}',
        )
