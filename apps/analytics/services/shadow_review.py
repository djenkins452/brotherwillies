"""Shadow-mode review aggregator.

Reads BettingRecommendation rows that carry Phase 1B shadow data and
produces a side-by-side comparison: how does the active rating mode
differ from the alt mode on the slate of recommendations actually
emitted?

This is the cheap, real-time complement to the backtest harness:
- Backtest: replays historical games end-to-end against either rating
  mode and measures actual outcome / CLV / ROI. Authoritative but
  needs settled games.
- Shadow review: reads live recommendation snapshots that already
  carry both modes' picks side-by-side. Available the moment a
  recommendation is persisted; doesn't need games to settle.

The review answers Phase 1B Task 7's questions DIRECTLY:
  - probability distribution: compare active vs alt final_prob means + variance
  - edge distribution: compare active vs alt edge_pp distributions
  - recommendation count: how many flipped status / tier / lane between modes
  - pick agreement: how often did the two modes pick the same side?

Outcome / CLV deltas need games to settle and are the backtest's job.
This service is best for "what does the slate look like under both
modes RIGHT NOW".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone


# ---------------------------------------------------------------------------
# Data shape


@dataclass
class _StatTuple:
    """Mean/min/max/n for a numeric field, formatted to display tolerance."""
    n: int
    mean: Optional[float]
    min_val: Optional[float]
    max_val: Optional[float]


@dataclass
class ShadowReview:
    sample: int                      # rows with usable shadow data (elo_available=True)
    sample_total: int                # rows scanned (incl. elo_available=False)
    active_mode: Optional[str]       # 'static' / 'elo' / 'mixed' / None
    # Side-by-side distributions
    active_final_prob: _StatTuple
    alt_final_prob: _StatTuple
    active_edge_pp: _StatTuple
    alt_edge_pp: _StatTuple
    # Pick agreement
    pick_same_side: int
    pick_different_side: int
    pick_agreement_rate: Optional[float]   # decimal share, None when sample==0
    # Status flips
    status_recommended_active_only: int
    status_recommended_alt_only: int
    status_recommended_both: int
    status_recommended_neither: int
    # Tier breakdown
    tier_counts_active: dict
    tier_counts_alt: dict
    # Lane flips
    lane_core_active_only: int
    lane_core_alt_only: int
    lane_core_both: int
    # Disagreement examples — first ~5 rows where modes disagree on side
    disagreement_examples: list

    def to_dict(self):
        d = asdict(self)
        # Dataclass nested fields → already dicts via asdict
        return d


# ---------------------------------------------------------------------------
# Helpers


def _stat(values) -> _StatTuple:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return _StatTuple(n=0, mean=None, min_val=None, max_val=None)
    return _StatTuple(
        n=len(cleaned),
        mean=round(sum(cleaned) / len(cleaned), 4),
        min_val=round(min(cleaned), 4),
        max_val=round(max(cleaned), 4),
    )


def _pick_side_from_rec(rec) -> Optional[str]:
    """Active-mode pick side. We stored only the team name, so map back
    via the related game. Returns None if the game can't be resolved."""
    game = rec.game  # uses BettingRecommendation.game property
    if game is None:
        return None
    return 'home' if rec.pick == game.home_team.name else 'away'


# ---------------------------------------------------------------------------
# Public API


def build_shadow_review(qs: QuerySet) -> ShadowReview:
    """Aggregate a queryset of MLB BettingRecommendation rows.

    Caller is responsible for filtering — this function does NOT apply
    a date window or sport filter so it stays composable. The default
    expected input is `BettingRecommendation.objects.filter(sport='mlb',
    shadow_active_mode__in=('static','elo'))` over a recent window.
    """
    rows = list(qs.select_related('mlb_game__home_team', 'mlb_game__away_team'))
    sample_total = len(rows)

    # Filter to rows where the alt comparison is actually meaningful.
    usable = [
        r for r in rows
        if r.shadow_alt_data and r.shadow_alt_data.get('elo_available') is True
    ]
    sample = len(usable)

    if sample == 0:
        # Empty shape — every field present so the template never has to
        # defensively .get().
        empty = _StatTuple(n=0, mean=None, min_val=None, max_val=None)
        return ShadowReview(
            sample=0, sample_total=sample_total, active_mode=None,
            active_final_prob=empty, alt_final_prob=empty,
            active_edge_pp=empty, alt_edge_pp=empty,
            pick_same_side=0, pick_different_side=0, pick_agreement_rate=None,
            status_recommended_active_only=0, status_recommended_alt_only=0,
            status_recommended_both=0, status_recommended_neither=0,
            tier_counts_active={}, tier_counts_alt={},
            lane_core_active_only=0, lane_core_alt_only=0, lane_core_both=0,
            disagreement_examples=[],
        )

    # Determine active mode (rows can be a mix if the flag was flipped
    # mid-window; report 'mixed' in that case).
    modes = {r.shadow_active_mode for r in usable}
    if len(modes) == 1:
        active_mode = next(iter(modes))
    else:
        active_mode = 'mixed'

    # Probability distributions (active confidence_score is in percent;
    # convert to decimal to match alt's final_prob which is decimal).
    active_probs = [float(r.confidence_score) / 100.0 for r in usable]
    alt_probs = [r.shadow_alt_data.get('final_prob') for r in usable]

    # Edge distributions (active model_edge in pp; alt edge_pp in pp).
    active_edges = [float(r.model_edge) for r in usable]
    alt_edges = [r.shadow_alt_data.get('edge_pp') for r in usable]

    # Pick agreement.
    same = different = 0
    disagreement_examples = []
    for r in usable:
        active_side = _pick_side_from_rec(r)
        alt_side = r.shadow_alt_data.get('pick_side')
        if active_side is None or alt_side is None:
            continue
        if active_side == alt_side:
            same += 1
        else:
            different += 1
            if len(disagreement_examples) < 5:
                game = r.mlb_game
                disagreement_examples.append({
                    'game_id': str(game.id) if game else None,
                    'game_label': (
                        f"{game.away_team.name} @ {game.home_team.name}"
                        if game else r.pick
                    ),
                    'active_pick': r.pick,
                    'active_pick_side': active_side,
                    'active_edge_pp': float(r.model_edge),
                    'alt_pick': r.shadow_alt_data.get('pick'),
                    'alt_pick_side': alt_side,
                    'alt_edge_pp': r.shadow_alt_data.get('edge_pp'),
                })

    pick_total = same + different
    agreement_rate = round(same / pick_total, 4) if pick_total else None

    # Status flips.
    rec_active_only = rec_alt_only = rec_both = rec_neither = 0
    for r in usable:
        a_rec = (r.status == 'recommended')
        x_rec = (r.shadow_alt_data.get('status') == 'recommended')
        if a_rec and x_rec:
            rec_both += 1
        elif a_rec and not x_rec:
            rec_active_only += 1
        elif x_rec and not a_rec:
            rec_alt_only += 1
        else:
            rec_neither += 1

    # Tier counts.
    tier_active = Counter(r.tier for r in usable)
    tier_alt = Counter(
        r.shadow_alt_data.get('tier') for r in usable
        if r.shadow_alt_data.get('tier') is not None
    )

    # Lane flips.
    lane_core_active_only = lane_core_alt_only = lane_core_both = 0
    for r in usable:
        a_core = (r.lane == 'core')
        x_core = (r.shadow_alt_data.get('lane') == 'core')
        if a_core and x_core:
            lane_core_both += 1
        elif a_core and not x_core:
            lane_core_active_only += 1
        elif x_core and not a_core:
            lane_core_alt_only += 1

    return ShadowReview(
        sample=sample,
        sample_total=sample_total,
        active_mode=active_mode,
        active_final_prob=_stat(active_probs),
        alt_final_prob=_stat(alt_probs),
        active_edge_pp=_stat(active_edges),
        alt_edge_pp=_stat(alt_edges),
        pick_same_side=same,
        pick_different_side=different,
        pick_agreement_rate=agreement_rate,
        status_recommended_active_only=rec_active_only,
        status_recommended_alt_only=rec_alt_only,
        status_recommended_both=rec_both,
        status_recommended_neither=rec_neither,
        tier_counts_active=dict(tier_active),
        tier_counts_alt=dict(tier_alt),
        lane_core_active_only=lane_core_active_only,
        lane_core_alt_only=lane_core_alt_only,
        lane_core_both=lane_core_both,
        disagreement_examples=disagreement_examples,
    )


def recent_mlb_shadow_review(days: int = 14) -> ShadowReview:
    """Convenience: read the most recent MLB recommendations and build a review."""
    from datetime import timedelta
    from apps.core.models import BettingRecommendation

    cutoff = timezone.now() - timedelta(days=days)
    qs = BettingRecommendation.objects.filter(
        sport='mlb',
        shadow_active_mode__in=('static', 'elo'),
        created_at__gte=cutoff,
    )
    return build_shadow_review(qs)
