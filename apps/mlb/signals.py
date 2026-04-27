"""Auto-generate Phase 1 opportunity signals when a new OddsSnapshot lands.

Why a signal handler and not an explicit call from the persist code:
    - The Moneyline ingest pipeline (apps/datahub/providers/mlb/...,
      apps/datahub/management/commands/ingest_odds.py) is the project's
      Tier-1 reliability path. Touching it is a net-negative risk for a
      Phase 1 informational feature.
    - Multiple sources insert OddsSnapshot rows: primary Odds API,
      ESPN fallback, manual entry, future providers. A signal handler
      catches them all uniformly without each call site needing to know
      about opportunity signals.
    - Easy to disable for tests via @override_settings or
      post_save.disconnect(...).

Failure isolation: any exception inside the handler is swallowed (and
logged) so a malformed snapshot can never break the ingest write that
just succeeded.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='mlb.OddsSnapshot')
def generate_opportunities_on_snapshot_save(sender, instance, created, **kwargs):
    """Create SpreadOpportunity / TotalOpportunity rows on insert.

    Only fires on `created=True` — updates to an existing snapshot
    don't regenerate signals (the original snapshot already had its
    signal set by the first save). This matters for any future code
    path that mutates a snapshot post-insert.
    """
    if not created:
        return

    # Local import: app registry needs to be ready before service
    # imports resolve. Top-level import here would deadlock at startup.
    from apps.mlb.services.opportunity_signals import (
        generate_opportunities_for_snapshot,
    )

    try:
        generate_opportunities_for_snapshot(instance)
    except Exception:
        # Never propagate — the snapshot insert that triggered this
        # signal MUST stay successful. Log with traceback so we can
        # see anything that breaks downstream without losing data.
        logger.exception(
            'mlb_opportunity_signal_generation_failed snapshot_id=%s game_id=%s',
            getattr(instance, 'pk', None),
            getattr(instance.game, 'pk', None) if getattr(instance, 'game', None) else None,
        )
