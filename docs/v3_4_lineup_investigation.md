# v3.4 — Confirmed Lineups + Lineup Quality: Empirical Investigation

**Status:** RESEARCH — historical replay NOT FEASIBLE without leakage risk. Recommendation: begin forward collection now; run a parallel historically-testable feature (park factors + weather) as immediate research.

**Date:** 2026-08-23
**Production impact:** ZERO. V3.2 remains frozen (0.62 / 7pp / blend 0.55 / Recent Form ON). Bullpen closed NO-GO.

---

## The critical distinction

A **confirmed pregame lineup** is what MLB Stats API publishes ~1-2 hours before first pitch, when the team announces the batting order. This is what Brother Willies could legitimately know when generating a recommendation.

A **boxscore batting order** is postgame truth — the players who actually batted. It is not, in general, identical to the confirmed pregame lineup:

- A late scratch (injury during warmup) replaces a player after the lineup card was posted.
- A pinch-hitter appears in the boxscore batting order even though the pregame card had the original starter.
- ~2-4% of games historically involve some form of last-minute lineup change.

**Treating a boxscore batting order as if it were the pregame confirmed lineup is a form of subtle leakage.** The magnitude is small but the discipline is what saved us from shipping bullpen — abandoning it here would defeat the same discipline.

## What MLB Stats API actually provides (empirical, tested 2026-08-23)

### Historical batting order — AVAILABLE

`GET /api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=lineups` returns for each Final game a `lineups.homePlayers` and `lineups.awayPlayers` array with 9 players each (batting order). Each player payload includes `id`, `fullName`, `primaryPosition.abbreviation`, etc.

Coverage: **10/10 Final games** for yesterday's slate had lineups populated. Extends to at least 2024-06-01 (verified earlier for bullpen). Confidence: high for coverage.

**But**: this is postgame truth, not the pregame confirmed lineup.

### Pregame confirmed lineup timestamp — NOT DIRECTLY EXPOSED

`GET /api/v1/schedule?date=2026-08-23&hydrate=lineups` on 2026-08-23 (today, before games start): **`lineups home/away = 0/0` for all 15 scheduled games**. Empty. The API does not publish forward-looking pregame lineups through this endpoint until close to or after game start.

The `/api/v1.1/game/{gamePk}/feed/live/timestamps` endpoint returns 450 sequential state timestamps for the historical game 822780 — from `20260810_192753` (~3.6 hours before first pitch) to `20260811_015631` (game end). This IS the mechanism by which one could reconstruct "at what timestamp did the lineup card first appear in the feed" — walk the timestamps sequentially, hit `/feed/live/diffPatch?startTimecode=X&endTimecode=Y`, watch for the moment `liveData.boxscore.teams.{side}.battingOrder` transitions from empty to populated.

Cost: ~450 timestamp checks per game × ~2700 games/6-months = ~1.2M API calls. Infeasible.

### Player offensive game logs — AVAILABLE

`GET /api/v1/people/{id}/stats?stats=gameLog&group=hitting&season=YYYY` returns per-appearance stats: AB, H, HR, BB, K, avg, obp, slg, ops, wOBA-adjacent components. Verified for Nathan Lukes (id 664770): 96 game log entries for 2026 season with full stat lines.

This is EXCELLENT for computing rolling as-of-T offensive stats — the same deterministic-builder pattern that worked for bullpen quality can produce lineup quality metrics.

## The stop condition fired

Per the v3.4 brief, stop conditions include:

> - lineup timestamps cannot distinguish pregame knowledge from postgame truth where that distinction is required

That distinction cannot currently be made from MLB Stats API for historical data without a 1.2M-call walk of feed/live timestamps. Building a historical replay on today's `/schedule?hydrate=lineups` payload would treat postgame batting orders as pregame knowledge — leakage.

**Do NOT run a historical replay on this data.**

## Recommended path forward (per brief Part 9)

### Immediate — begin forward collection

Build the minimum viable forward-collection foundation:

1. **`ConfirmedLineup` model** — one row per (game, side) with:
   - `game` FK, `team` FK
   - `players_json` — list of {batting_order, player_id, name, position}
   - `first_seen_at` — UTC timestamp when this lineup FIRST appeared in the schedule API poll
   - `source` (`'mlb_stats_api'`)
   - `is_confirmed` boolean (true when we successfully observed a non-empty lineup before first pitch)

2. **`lineup_snapshot_poll` management command** — polls `/schedule?hydrate=lineups` for today's scheduled games every 15 minutes in the ~3 hours preceding each game's first_pitch. Records the FIRST timestamp at which each lineup becomes non-empty. Creates/updates `ConfirmedLineup` row.

3. **Scheduling** — wire into the existing `cron_run_log` pattern, invoked from Railway cron every 15 min during game hours.

4. **Storage discipline**: `first_seen_at` is the truth marker. When we replay a historical game G, the eligible lineup is the one whose `first_seen_at < G.first_pitch`. If none exists (game happened before we started collecting), the game is UNCOVERED and excluded from lineup-experiment metrics.

Runtime cost: ~15 API calls/hour × ~6 hours/day × ~180 days = ~16k calls / 6 months of forward collection. Well within the free-tier envelope.

Coverage horizon: 30-90 days after activation before a legitimate lineup replay has meaningful sample size.

### Player offensive stats — deterministic historical builder

The player game-log endpoint IS historically reconstructable — same discipline as bullpen. Build a `PlayerOffensiveHistory` view (query wrapper) that returns as-of-T rolling stats:

- Last 30-day OPS, wOBA-lite, plate appearances
- Season-to-date pre-game stats

For each ConfirmedLineup's 9 players, aggregate their as-of-T offensive metrics into a **Team Lineup Quality Differential** at replay/scoring time.

This half CAN be reconstructed historically. Combined with future ConfirmedLineup rows, it feeds the eventual replay.

### Parallel — historically-testable feature to research NOW

Because forward lineup collection has a 30-90 day evidence horizon, keep the research pipeline productive by starting a parallel investigation on a feature that DOES have historical validity today.

**Recommended: park factors + weather.**

- **Historically safe**: venue is set at scheduling; weather forecast is available days ahead; actual game-time weather is in `gameData.weather` on `/feed/live` (verified in earlier investigation).
- **Low ingestion cost**: fields are already in the payloads we call for other purposes.
- **Well-established predictive edge**: park factors are one of the most robust context adjustments in baseball analytics (elevation, dimensions, wind).
- **Modest contribution magnitude**: unlike bullpen's over-powered contribution, park factors have small stable effects — less risk of the "artificial edge" pathology.

Alternative: **per-bucket isotonic calibration** — architectural, not new information, would address the calibration observation from Section 6 of the attribution study. But adds no new predictive signal.

### What is deliberately NOT shipped in this commit

- No `ConfirmedLineup` model. No `lineup_snapshot_poll` command. No lineup shadow contribution. No lineup replay experiment.

The forward-collection foundation is the natural next commit; this commit is the empirical investigation + closure of v3.3 + recommendation.

Reasoning: shipping the full lineup stack today is speculative until Danny confirms the recommended path (begin forward collection + start parallel park-factor/weather research). If Danny prefers a different alternative feature (per-bucket calibration, wOBA-based team rating, sharp-money detection), the lineup stack becomes wasted infrastructure.

## Explicit answers to the brief's questions

- **1. Confirmed V3.3 bullpen NO-GO** — see `docs/v3_3_bullpen_final_validation.md`. Full validation trail; walk-forward failed 2/6 criteria. Bullpen infrastructure preserved; production flags remain false.
- **4. Lineup data sources tested** — `/schedule?hydrate=lineups`, `/game/{gamePk}/feed/live`, `/game/{gamePk}/feed/live/timestamps`, `/people/{id}/stats?stats=gameLog&group=hitting`.
- **5. Historical fields available** — batting order (9 players/side), player IDs + names + positions, per-player hitting game logs with AB/H/HR/BB/K/avg/obp/slg/ops.
- **6. Pregame confirmation timing reconstructable?** — Not directly. Feed/live timestamps would allow it at ~1.2M API calls per 180-day backfill. Infeasible.
- **7. Historical player-stat feasibility** — YES. Per-game hitting logs are cheaply available.
- **8. Lineup quality methodology (proposed)** — For each side, sum rolling-30-day OPS × PA-weight across the 9 confirmed starters. Compute a differential (home - away). Optionally subtract a team-baseline expected value (using the roster's average starter) to produce "lineup vs expected" — captures the impact of a star being scratched.
- **9. Leakage risks** — the biggest: substituting current player statistics into historical games (fatal for any as-of-T metric). Second: treating boxscore batting orders as pregame confirmed. Third: using team-average stats when the specific starters differ materially. Mitigations: strict `<` reference_date filter on player game logs; forward-only ConfirmedLineup collection; explicit UNCOVERED tag for games without a valid pregame snapshot.
- **10. Historical backfill feasibility** — LINEUP: NO (requires feed/live timestamp walk, infeasible cost). PLAYER STATS: YES.
- **11. Forward collection architecture** — `ConfirmedLineup` append-only table populated by a 15-min poll of `/schedule?hydrate=lineups` for scheduled games; records `first_seen_at`; feeds all downstream lineup features.
- **12. Shadow feature architecture** — mirror the bullpen shadow pattern: compute lineup quality metrics on every scoring call, store on `BettingRecommendation.feature_contributions`, gate score inclusion behind `USE_LINEUP_QUALITY` env-var flag (default false).
- **13. Experiment design** — A: V3.2 baseline; B: V3.2 + lineup quality differential. Run only after forward-collection has produced ≥50 covered games (~30-45 days of forward accumulation).
- **14. Implementation completed if evidence supports proceeding** — NOT proceeding with lineup implementation this commit (see reasoning above). Awaiting operator decision.
- **15. Tests** — none yet; would be added with the ConfirmedLineup model.
- **16. Commit / deployment status** — this commit closes v3.3 bullpen formally and publishes this investigation. Lineup implementation deferred to a follow-up commit pending operator direction.
- **17. Exact next operator action** — See the "Operator decision" section below.

## Operator decision requested

Danny, pick one of:

- **A** — Approve building the forward-collection foundation NOW (`ConfirmedLineup` model + 15-min poll command + Railway cron wiring). Accept that legitimate lineup evidence will not exist for ~30-90 days. In parallel, I open a park-factor + weather investigation to keep the research pipeline productive.

- **B** — Skip lineup entirely for now. Move directly to park factors + weather as the next feature investigation. Lineup revisited later if that feature also fails to earn its place.

- **C** — Different priority — you name the next feature to investigate first.

Whatever you pick, the invariant holds: **every feature must earn the right to influence a recommendation, and V3.2 stays frozen until one does.**
