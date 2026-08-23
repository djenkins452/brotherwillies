"""v3.4 team-offense PHASE 2 — OPS/OBP/SLG-based offensive signal.

Second-generation offense signal replacing the FAILED runs/game
formulation (v3.4 phase 1). Uses MLB team hitting stats from stored
`TeamBattingSnapshot` rows (season-to-date raw counts, ingested
daily from /v1/teams/{id}/stats?stats=byDateRange).

WHAT PHASE 1 FOUND

  Rolling 30-day runs/game × 0.50 blended into V3.2 produced:
    A (V3.2 baseline)         n=236  72.03% win  +22.23% ROI
    B (V3.2 + 30d runs/game)  n=247  70.85% win  +20.13% ROI
  → NO-GO. Runs/game is a crude outcome metric — it conflates
  opportunity, situational hitting, and opponent quality with
  underlying offensive ability.

WHAT PHASE 2 TESTS

  OPS/OBP/SLG are rate stats that measure per-plate-appearance
  offensive quality — less noise from opportunity, no built-in
  correlation to game outcome (unlike runs). They also lag Elo
  and market probability in different ways, so they may add
  independent information.

  Three candidates (pre-declared before seeing any results):
    B_v2. Rolling 30-day OPS
    C_v2. Rolling 30-day OBP + SLG as separate components
    D_v2. Season-to-date OPS blended 50/50 with rolling 30-day OPS

  A (the failed runs/game reference) is retained for continuity.
  No parameter grid — evaluate the pre-declared set on a single
  isolated-predictive-value pass, select at most ONE candidate,
  bounded integration if warranted.

LEAKAGE DISCIPLINE

  For every consumer:
    * Query `TeamBattingSnapshot.as_of_date < game.first_pitch.date()`
      — strict `<`.
    * "Rolling 30-day OPS as of D" = derived from
      `snapshot(D-1) MINUS snapshot(D-31)` — two snapshots, one
      subtraction, no in-flight aggregation.
    * When either snapshot is missing OR minimum sample not met,
      return zero + 'low' confidence. Never fabricate.

CANDIDATE VALUES ARE DIAGNOSTIC UNTIL INTEGRATION

  The service returns raw OBP/SLG/OPS + a differential in
  rate-scale units. Only the model integration layer converts
  that to a bounded contribution — the signal itself has no
  built-in cap here. That deliberately mirrors bullpen's design
  (the signal has natural bounds; the caller decides the
  contribution weight).

FLAG DISCIPLINE

  `USE_TEAM_OFFENSE=false` (code default) unchanged. This module
  is invoked only by the isolated-analysis service and (if
  warranted) the offense_v2 replay experiment. Neither can
  influence production recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Tuple


# --- Minimum-sample gates (per candidate). Chosen conservatively so
# that a snapshot with 15 PA doesn't dominate. Match the "rolling ~30d"
# horizon; MLB teams get ~25-30 games in that window with ~100+ PA
# per game.
MIN_ROLLING_PA = 200          # ~10 games' worth of team PA
MIN_SEASON_PA = 400
LEAGUE_AVG_OPS = 0.720        # Roughly modern MLB league average
LEAGUE_AVG_OBP = 0.320
LEAGUE_AVG_SLG = 0.400

DEFAULT_ROLLING_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Signal dataclass


@dataclass(frozen=True)
class TeamHittingWindow:
    """Team hitting aggregate over a specific date window.

    All rate stats computed from raw counts using standard formulas:
      AVG = H / AB
      OBP = (H + BB + HBP) / (AB + BB + HBP + SF)
      SLG = (1B + 2*2B + 3*3B + 4*HR) / AB
      OPS = OBP + SLG
    """
    pa: int
    ab: int
    hits: int
    doubles: int
    triples: int
    home_runs: int
    walks: int
    hbp: int
    sac_flies: int
    strikeouts: int
    runs: int
    games: int

    @property
    def singles(self) -> int:
        return self.hits - self.doubles - self.triples - self.home_runs

    @property
    def total_bases(self) -> int:
        return (self.singles + 2 * self.doubles + 3 * self.triples
                + 4 * self.home_runs)

    @property
    def obp(self) -> Optional[float]:
        denom = self.ab + self.walks + self.hbp + self.sac_flies
        if denom == 0:
            return None
        return (self.hits + self.walks + self.hbp) / denom

    @property
    def slg(self) -> Optional[float]:
        if self.ab == 0:
            return None
        return self.total_bases / self.ab

    @property
    def ops(self) -> Optional[float]:
        o, s = self.obp, self.slg
        if o is None or s is None:
            return None
        return o + s

    @property
    def bb_rate(self) -> Optional[float]:
        if self.pa == 0:
            return None
        return self.walks / self.pa

    @property
    def k_rate(self) -> Optional[float]:
        if self.pa == 0:
            return None
        return self.strikeouts / self.pa

    @property
    def hr_rate(self) -> Optional[float]:
        if self.pa == 0:
            return None
        return self.home_runs / self.pa

    @property
    def runs_per_game(self) -> Optional[float]:
        if self.games == 0:
            return None
        return self.runs / self.games


def _empty_window() -> TeamHittingWindow:
    return TeamHittingWindow(
        pa=0, ab=0, hits=0, doubles=0, triples=0, home_runs=0,
        walks=0, hbp=0, sac_flies=0, strikeouts=0, runs=0, games=0,
    )


def _subtract(a, b) -> TeamHittingWindow:
    """Return counts for the rolling window represented by (b - a).

    b is the later (or equal) snapshot; a is the earlier. Subtracting
    a's cumulative counts from b's yields the counts contributed by
    the interval (a.as_of_date, b.as_of_date].

    If a is None (no earlier snapshot found), return b's counts as-is
    (equivalent to a "since season start" window).
    """
    if a is None:
        return TeamHittingWindow(
            pa=b.plate_appearances, ab=b.at_bats, hits=b.hits,
            doubles=b.doubles, triples=b.triples, home_runs=b.home_runs,
            walks=b.walks, hbp=b.hit_by_pitch, sac_flies=b.sac_flies,
            strikeouts=b.strikeouts, runs=b.runs, games=b.games_played,
        )
    return TeamHittingWindow(
        pa=b.plate_appearances - a.plate_appearances,
        ab=b.at_bats - a.at_bats,
        hits=b.hits - a.hits,
        doubles=b.doubles - a.doubles,
        triples=b.triples - a.triples,
        home_runs=b.home_runs - a.home_runs,
        walks=b.walks - a.walks,
        hbp=b.hit_by_pitch - a.hit_by_pitch,
        sac_flies=b.sac_flies - a.sac_flies,
        strikeouts=b.strikeouts - a.strikeouts,
        runs=b.runs - a.runs,
        games=b.games_played - a.games_played,
    )


# ---------------------------------------------------------------------------
# Snapshot lookup (leakage-safe)


def _latest_snapshot_strictly_before(team, cutoff_date):
    """Return the newest TeamBattingSnapshot with as_of_date < cutoff_date.

    Strict `<`. Never returns a same-day snapshot (which would carry
    the day's still-in-progress games and constitute leakage).
    """
    from apps.mlb.models import TeamBattingSnapshot
    return (
        TeamBattingSnapshot.objects
        .filter(team=team, as_of_date__lt=cutoff_date)
        .order_by('-as_of_date')
        .first()
    )


def _snapshot_at_or_before(team, target_date):
    """Return the newest TeamBattingSnapshot with as_of_date <= target_date.

    Used ONLY for the "earlier bound" of a rolling window when computing
    the difference — never for the "as of" date itself (that must use
    the strict variant). Same-day snapshots are fine on this side
    because they represent the state on an EARLIER date than the game.
    """
    from apps.mlb.models import TeamBattingSnapshot
    return (
        TeamBattingSnapshot.objects
        .filter(team=team, as_of_date__lte=target_date)
        .order_by('-as_of_date')
        .first()
    )


# ---------------------------------------------------------------------------
# Public API — leakage-safe windows


def season_to_date_window(team, reference_date) -> TeamHittingWindow:
    """Team's season-to-date hitting aggregate strictly before `reference_date`.

    reference_date is treated as a date (game.first_pitch.date() when
    called from a game-time consumer). Returns empty window when no
    snapshot exists yet for this team.
    """
    ref = _as_date(reference_date)
    if ref is None or team is None:
        return _empty_window()
    snap = _latest_snapshot_strictly_before(team, ref)
    if snap is None:
        return _empty_window()
    return _subtract(None, snap)


def rolling_window(
    team, reference_date, *,
    window_days: int = DEFAULT_ROLLING_WINDOW_DAYS,
) -> TeamHittingWindow:
    """Team's hitting aggregate over the rolling window ending strictly
    before `reference_date`, computed by subtracting two snapshots.

    Definition:
      end_snap = latest snapshot with as_of_date < reference_date
      start_snap = latest snapshot with as_of_date <= end_snap.as_of_date - window_days
      window = end_snap - start_snap

    If start_snap is None (no snapshot old enough), the window degrades
    to season-to-date (equivalent to subtracting zero counts). This is
    honest: for early-season dates there IS no 30-day-prior state.
    """
    ref = _as_date(reference_date)
    if ref is None or team is None:
        return _empty_window()
    end_snap = _latest_snapshot_strictly_before(team, ref)
    if end_snap is None:
        return _empty_window()
    lookback_target = end_snap.as_of_date - timedelta(days=window_days)
    start_snap = _snapshot_at_or_before(team, lookback_target)
    return _subtract(start_snap, end_snap)


def _as_date(x):
    if x is None:
        return None
    if isinstance(x, date):
        return x
    if hasattr(x, 'date'):
        try:
            return x.date()
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Candidate signals — one function per pre-declared candidate


@dataclass(frozen=True)
class OffenseSignalV2:
    """Team-side offensive signal from a specific candidate formulation.

    Field semantics:
      delta_units: signed contribution in rate-scale units (OPS-like).
        Positive → team hits above league average on this metric.
      raw_value: the underlying rate stat for logging/interpretation.
      sample_size: PA (rolling) or PA (season) — used for gating.
      confidence: 'low' if sample_size below threshold, else 'med'/'high'.
      candidate: 'B_v2_rolling_ops' / 'C_v2_rolling_obp_slg' /
                 'D_v2_blend_season_recent_ops'
    """
    delta_units: float
    raw_value: Optional[float]
    sample_size: int
    confidence: str
    candidate: str


def candidate_b_rolling_ops(team, reference_date, *,
                            window_days: int = DEFAULT_ROLLING_WINDOW_DAYS) -> OffenseSignalV2:
    w = rolling_window(team, reference_date, window_days=window_days)
    ops = w.ops
    if ops is None or w.pa < MIN_ROLLING_PA:
        return OffenseSignalV2(
            delta_units=0.0, raw_value=ops, sample_size=w.pa,
            confidence='low', candidate='B_v2_rolling_ops',
        )
    return OffenseSignalV2(
        delta_units=round(ops - LEAGUE_AVG_OPS, 6),
        raw_value=round(ops, 4),
        sample_size=w.pa,
        confidence='high' if w.pa >= 600 else 'med',
        candidate='B_v2_rolling_ops',
    )


def candidate_c_rolling_obp_slg(team, reference_date, *,
                                window_days: int = DEFAULT_ROLLING_WINDOW_DAYS
                                ) -> Tuple[OffenseSignalV2, OffenseSignalV2]:
    """C variant returns TWO signals — OBP and SLG separately — so the
    model integration can weight them independently if warranted."""
    w = rolling_window(team, reference_date, window_days=window_days)
    obp, slg = w.obp, w.slg
    ok = (obp is not None and slg is not None and w.pa >= MIN_ROLLING_PA)
    if not ok:
        low = OffenseSignalV2(
            delta_units=0.0, raw_value=None, sample_size=w.pa,
            confidence='low', candidate='C_v2_rolling_obp_slg',
        )
        return (
            OffenseSignalV2(**{**low.__dict__, 'raw_value': obp,
                               'candidate': 'C_v2_rolling_obp'}),
            OffenseSignalV2(**{**low.__dict__, 'raw_value': slg,
                               'candidate': 'C_v2_rolling_slg'}),
        )
    conf = 'high' if w.pa >= 600 else 'med'
    return (
        OffenseSignalV2(
            delta_units=round(obp - LEAGUE_AVG_OBP, 6),
            raw_value=round(obp, 4), sample_size=w.pa,
            confidence=conf, candidate='C_v2_rolling_obp',
        ),
        OffenseSignalV2(
            delta_units=round(slg - LEAGUE_AVG_SLG, 6),
            raw_value=round(slg, 4), sample_size=w.pa,
            confidence=conf, candidate='C_v2_rolling_slg',
        ),
    )


def candidate_d_blend_ops(team, reference_date, *,
                          rolling_days: int = DEFAULT_ROLLING_WINDOW_DAYS,
                          blend: float = 0.5) -> OffenseSignalV2:
    """D — season-to-date OPS blended (blend) with rolling 30-day OPS
    (1-blend). Rationale: rolling captures form; season anchors on
    the larger sample when a team's roster has been stable. Blend
    reduces early-season variance without giving up recency."""
    rw = rolling_window(team, reference_date, window_days=rolling_days)
    sw = season_to_date_window(team, reference_date)
    r_ops = rw.ops
    s_ops = sw.ops
    if (r_ops is None or rw.pa < MIN_ROLLING_PA
            or s_ops is None or sw.pa < MIN_SEASON_PA):
        return OffenseSignalV2(
            delta_units=0.0,
            raw_value=(r_ops if r_ops is not None else s_ops),
            sample_size=rw.pa + sw.pa,
            confidence='low', candidate='D_v2_blend_season_recent_ops',
        )
    blended = s_ops * blend + r_ops * (1.0 - blend)
    total_pa = rw.pa + sw.pa
    conf = 'high' if total_pa >= 1500 else 'med'
    return OffenseSignalV2(
        delta_units=round(blended - LEAGUE_AVG_OPS, 6),
        raw_value=round(blended, 4),
        sample_size=total_pa,
        confidence=conf, candidate='D_v2_blend_season_recent_ops',
    )
