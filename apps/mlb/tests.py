"""MLB tests — expanded in Phase 10."""
from django.test import TestCase


class MLBSmokeTests(TestCase):
    def test_app_installed(self):
        from django.apps import apps
        self.assertTrue(apps.is_installed('apps.mlb'))
