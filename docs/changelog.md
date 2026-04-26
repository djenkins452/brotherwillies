# Brother Willies - Changelog

---

## 2026-04-26 - ESPN single-side moneyline: derive opposite via symmetric inversion (v1)

**Summary:** Earlier today's fix made the ESPN provider parse the moneyline out of `details` strings like `"NYY -136"`. But ESPN often emits only ONE side's moneyline — never publishing the opponent's number — so the parser still returned `away_ml=None` and downstream skipped the row. This fix fills the missing side by symmetric inversion (`-136 → +136`).

### What changed
In `_pick_best_odds_entry`, after Pass 3 accumulates whatever sides the `details` strings supplied, an additional v1 step runs:
- `details_home != None and details_away == None` → `details_away = -details_home`
- `details_away != None and details_home == None` → `details_home = -details_away`

The provider docstring acknowledges the imperfection up front: real markets carry vig, so the true opposite of -136 is closer to +120, not +136. This is acceptable in v1 because:
- The model still owns the true win probability — ESPN's number only feeds `market_home_win_prob` for display.
- "Approximate moneyline" is strictly better than "no moneyline" — the alternative was skipping the game entirely and showing the user no odds at all.

A follow-up can swap inversion for a vig-aware estimate using the spread or a default vig assumption; the schema and rest of the pipeline don't change.

### New / updated logs (per spec)
- `mlb_odds_espn_parsed_details details=… home_ml=… away_ml=…` — fires when details parsing produces moneylines (with or without inversion).
- `mlb_odds_espn_no_match_for_details details=… home_abbr=… away_abbr=… value=…` — fires when a parseable details string carries an abbreviation matching neither home nor away. Lets us extend the alias map from real production data.

The earlier `mlb_odds_espn_parsed source=details …` log line was replaced by `mlb_odds_espn_parsed_details` to match the spec exactly.

### Tests (7 new, 1 updated in `apps.datahub.tests`)
- `MlbEspnSingleSideDerivationTests`:
  - Home team in details → away derived (`NYY -136` with NYY=home → home=-136, away=+136).
  - Away team in details → home derived (NYY=away → away=-136, home=+136).
  - Underdog in details derived correctly (`TOR +120` with TOR=home → home=+120, away=-120).
  - `mlb_odds_espn_parsed_details` log fires with full numbers and the original details string.
  - `mlb_odds_espn_no_match_for_details` log fires when abbreviation matches neither side.
  - Two-sided details still uses reported values, NOT inversion (regression guard for the case where ESPN actually does publish both sides).
- `MlbEspnSingleSideEndToEndTests` — full normalize→persist run on a single-side ESPN payload produces a complete `OddsSnapshot` instead of being skipped, with `odds_source='espn'` / `source_quality='fallback'`.
- Updated `test_falls_back_when_only_home_details_present` → renamed to `test_single_side_details_derives_opposite_via_inversion` (now asserts the away side is filled at +143 instead of None).

535 total tests pass (pre-existing `feedback.tests` ImportError unchanged).

### Files
- Edited: `apps/datahub/providers/mlb/odds_espn_provider.py`, `apps/datahub/tests.py`. No schema, no Odds API path, no recommendation engine, no UI, no ingest flow changes.

---

## 2026-04-26 - ESPN fallback fixes: details parsing + per-row freshness

**Summary:** Two production bugs in the ESPN fallback that, together, made the fallback fill 0 of 23 gap games. Both fixed with narrow changes — no schema, no recommendation engine, no UI.

### Bug 1: ESPN moneyline lived in `details`, not `homeTeamOdds.moneyLine`
ESPN payloads observed in prod put the moneyline inside `odds[].details` as a string like `"BAL -143"` instead of (or in addition to) the previously-supported `homeTeamOdds.moneyLine` numeric field. The provider read only the numeric field, missed the moneyline, and skipped the row with `mlb_odds_espn_no_moneyline`.

**Fix:** new `_parse_moneyline_from_details(details, home_abbr, away_abbr)` parser. Added as a third pass in `_pick_best_odds_entry` after the standard shape-A/B/C/D extraction:
- Recognizes `"BAL -143"` (abbreviation + integer ≥ 100), `"+120"`, `"-115"`.
- Rejects spreads (decimals like `"BAL -1.5"`, small integers like `"-7"`).
- Disambiguates side via team abbreviation lookup against the event's competitors.
- Combines partial details across multiple entries (one entry's `details` carries the home moneyline, another's the away).
- Emits `mlb_odds_espn_parsed` log on success, `mlb_odds_espn_parse_error` with the raw string when it fails so we can grow the parser from real-world data.

### Bug 2: `target_game_ids` filter was rejecting good matches
The per-game gap-fill landed earlier today passed `target_game_ids = set(gap_pks)` to ESPN's persist as a post-match filter. In prod, ESPN events were matching against **yesterday's already-played game** (same teams, ±36h window), and yesterday's game.pk was correctly NOT in the gap set — so every ESPN row was rejected with `reason=not_in_target_set`. Real symptom: `api_filled=1, espn_filled=0, still_missing=23`.

**Fix:** removed `target_game_ids` entirely. Replaced with two changes inside ESPN's persist itself:
1. **Restrict candidate Games to the upcoming window** (`first_pitch in [now-2h, now+72h]`). Yesterday's finished game is no longer a match candidate.
2. **Per-row freshness check**: if the matched game already has a fresh primary `OddsSnapshot` (within `FRESH_ODDS_MAX_AGE_MINUTES`), skip with `reason=has_fresh_primary`. No more double-writes; no caller cooperation needed.

Result: ESPN is now self-filtering, idempotent, and impossible to confuse with stale matchups. The `not_in_target_set` reason no longer exists.

### New structured logs (per spec)
- `mlb_odds_espn_match_success game_id=… home=… away=… ml_home=… ml_away=…` — one per persisted row, naming the matched game.
- `mlb_odds_espn_no_match stage=team|game …` — replaces the old `no_team_match` / `no_game_match` reasons with a single contract.
- `mlb_odds_espn_parsed source=details ml_home=… ml_away=…` — fires when details-parsing produces moneylines.
- `mlb_odds_espn_parse_error raw_details=…` — fires when neither standard shapes nor details parsing yielded a moneyline. Captures the raw string so we can extend the parser without guesswork.
- `mlb_odds_espn_persist_skip reason=has_fresh_primary` — replaces `not_in_target_set`.

### Tests (28 new in `apps.datahub.tests`)
- `MlbEspnDetailsParserTests` (7) — parses abbrev+ML, parses `+`/`-` values, rejects decimals (spreads), rejects small integers, parses value-only without abbreviation, unknown abbreviation returns value but None side, garbage returns `(None, None)`.
- `MlbEspnPickBestEntryDetailsFallbackTests` (3) — combines details from two entries, partial coverage (only home parsed), Pass 1 standard-shape still wins over details when both present.
- `MlbEspnNormalizeWiresParserAndLogsTests` (2) — normalize() recovers moneylines from details and emits `mlb_odds_espn_parsed`; emits `mlb_odds_espn_parse_error` when details are unrecognized.
- `MlbEspnPersistMatchSuccessLogTests` (2) — `match_success` log fires per persisted row with game_id; `no_match stage=team` fires on team failure.
- `MlbEspnFreshnessSelfFilterTests` (4) — replaces `MlbEspnTargetFilterTests`. Skips when game has fresh primary, doesn't skip when primary is stale, doesn't skip on existing ESPN row (only primary blocks fallback), ignores yesterday's finished game with same matchup.

528 total tests pass (pre-existing `feedback.tests` ImportError unchanged).

### Files
- Edited: `apps/datahub/providers/mlb/odds_espn_provider.py`, `apps/datahub/management/commands/ingest_odds.py`, `apps/datahub/tests.py`. No model changes, no UI changes, no recommendation-engine changes.

### What you should see post-deploy
On the next refresh, the log block should look like:
```
mlb_odds_persist_summary created=A skipped=B coverage_pct=…
ESPN per-game gap-fill triggered: K of N upcoming games have no fresh primary odds
mlb_odds_espn_parsed source=details ml_home=… ml_away=…   (one per game with details-style moneylines)
mlb_odds_espn_match_success game_id=… home=… away=… ml_home=…   (one per persisted row)
mlb_odds_espn_persist_summary created=K skipped=…
mlb_odds_ingest_summary upcoming_games=N api_filled=A espn_filled=K still_missing=Z
```
**Success criterion: `still_missing` approaches 0.** If non-zero, the per-line `mlb_odds_espn_no_match` / `mlb_odds_espn_parse_error` entries name exactly which event and why.

---

## 2026-04-26 - Per-game ESPN gap-fill (every MLB game gets odds)

**Summary:** Replaced the all-or-nothing whole-slate ESPN trigger with a per-game gap-fill: after the primary Odds API runs, identify upcoming MLB games that still have no fresh snapshot, and fall back to ESPN to fill exactly those games (no double-writes for games primary already covered).

### Behavior
- After primary persist, query upcoming MLB games (next 36h, starting up to 2h ago) that have no `OddsSnapshot` captured within `FRESH_ODDS_MAX_AGE_MINUTES` (180min default).
- If gaps exist, run ESPN scoreboard ingest with a `target_game_ids` filter — only persists for the gap games.
- If no gaps, ESPN call is skipped entirely (saves the wasted scoreboard fetch on healthy days).
- Old whole-slate trigger (`primary_created == 0` OR `today_count == 0`) is subsumed by the new logic — when primary returns 0, ALL upcoming games are gaps, ESPN fills them all. Same end state, zero regression.

### Source metadata is now tagged on write
- Primary path: `odds_source='odds_api'`, `source_quality='primary'`
- ESPN path: `odds_source='espn'`, `source_quality='fallback'`
- Both were silently defaulting to the model defaults before — ESPN snapshots were misclassified as "primary." This is now fixed at write time so the UI and recommendation engine can read source metadata directly off the row.

### Required debug summary (the "no more silent fails" line)
On every MLB ingest run, one log line answers the spec's contract:
```
mlb_odds_ingest_summary upcoming_games=N api_filled=X espn_filled=Y still_missing=Z
```
- `still_missing > 0` → logged at **ERROR** level (surfaces in the Ops Command Center recent-failures panel automatically).
- `still_missing == 0` → logged at INFO. Healthy day, healthy log.

### Tests (13 new in `apps.datahub.tests`)
- `MlbGapDetectionTests` (4) — empty DB, fresh snapshot removes from gaps, stale doesn't count, far-future games excluded from upcoming window.
- `MlbEspnTargetFilterTests` (2) — filter skips non-target games (no double-writes), `None` filter preserves whole-slate behavior.
- `MlbSourceMetadataTests` (2) — ESPN tagged `espn`/`fallback`, primary tagged `odds_api`/`primary`.
- `MlbIngestOddsCommandGapFillTests` (5) — mixed slate fills only gaps; all-primary skips ESPN call entirely (mock asserts `fetch` not called); primary returns 0 → ESPN fills all (whole-slate behavior preserved); summary log emitted with correct counts; missing games trigger ERROR-level summary.

512 total tests pass (pre-existing `feedback.tests` ImportError unchanged).

### Files
- Edited: `apps/datahub/management/commands/ingest_odds.py`, `apps/datahub/providers/mlb/odds_provider.py`, `apps/datahub/providers/mlb/odds_espn_provider.py`, `apps/datahub/tests.py`.
- No model changes. No UI changes. No scheduling changes.

---

## 2026-04-26 - MLB odds: silent-data-loss fix + comprehensive alias coverage

**Summary:** A prod incident showed only 1 of 16 MLB games getting odds. The cause was almost certainly team-name variants the alias dict didn't recognize, and the silence was because skipped rows were logged at INFO with API-only names — easy to miss in deploy logs. This commit makes that class of failure visible AND much harder to trigger.

### Alias coverage — all 30 MLB franchises
`apps/datahub/providers/mlb/name_aliases.py` rebuilt with:
- A `CANONICAL_MLB_TEAMS` list of 30 active franchises (single source of truth).
- ~150 alias entries covering: full names, nickname-only, city-only, "NY/LA/SF/SD" short forms, three-letter abbreviations (NYY/LAD/STL/…), legacy variants (Cleveland Indians → Guardians), Athletics relocation (Oakland → Sacramento → Athletics-only).
- Punctuation-tolerant key normalization (`St. Louis` ≡ `St Louis` ≡ `Saint Louis`).

### Fuzzy fallback (`fuzzy_match_to_canonical`)
Runs only when alias lookup AND DB lookup both miss:
- Substring match: input contains a known nickname.
- Reverse substring: input is a substring of a canonical (e.g. API trims to "Yankees" only).
- Two-word nicknames handled correctly (Red Sox / White Sox / Blue Jays).
- Pure string ops, no fuzzy-match library needed.
- Logs every successful fuzzy recovery so we can mine the deploy log and grow the alias dict toward zero fuzzy hits.

### Persist logging — no more silent fails
`apps/datahub/providers/mlb/odds_provider.py::persist`:
- Per-skip logs now include both the raw API names AND the normalized canonical attempt — so a log reader can immediately tell alias-miss from DB-miss.
- Summary logs gained `matchups_seen` / `matchups_matched` / `coverage_pct`.
- **Coverage alert**: when `matchups_matched < 50%` of `matchups_seen` AND we saw at least 4 matchups, the summary is logged at **ERROR** level. This surfaces in the Ops Command Center "Recent Failures" panel automatically — no extra wiring needed.
- Result dict carries the same coverage figures so callers can act on them.

### Debug mode (`DEBUG_ODDS_MATCHING`)
New env-var-backed setting. When `true`, every API team name is echoed at INFO during persist — designed to be flipped on briefly via Railway, harvest the names, and flipped back off.

### Tests (22 new in `apps.datahub.tests`)
- `MlbAliasCoverageTests` — all 30 franchises present, every nickname resolves, common short forms (NY/LA/Chi), 3-letter abbreviations, Athletics relocation variants, defensive cases (empty/None/unknown).
- `MlbFuzzyMatchTests` — substring match, reverse substring, two-word nicknames, garbage input returns None, short-input doesn't false-match.
- `MlbOddsPersistLoggingTests` — skip log includes normalized names, summary log fields present, coverage <50% logs ERROR, normal coverage doesn't log ERROR, small-slate exemption (under 4 matchups), result dict carries coverage stats.
- `MlbDebugMatchingFlagTests` — flag on emits per-item INFO, flag off emits nothing extra.
- `MlbFuzzyRecoveryEndToEndTests` — _find_team recovers a Team via fuzzy fallback when alias dict misses.

499 total tests pass (pre-existing `feedback.tests` ImportError unchanged).

### Files
- Rewritten: `apps/datahub/providers/mlb/name_aliases.py`.
- Edited: `apps/datahub/providers/mlb/odds_provider.py`, `brotherwillies/settings.py`, `apps/datahub/tests.py`.

---

## 2026-04-26 - Auto-failover Foundation: ProviderHealth + Circuit Breaker (Commit 1)

**Summary:** First of three commits building an auto-failover + degraded-mode reliability layer. This one is the foundation — durable provider state, circuit-breaker logic, and snapshot source-tagging schema. No user-visible behavior change yet (no router, no UI gating); that lands in Commits 2 and 3.

### New `ProviderHealth` model
One row per provider (`odds_api`, `espn`), mutated in place:
- `last_success_at`, `last_failure_at`, `consecutive_failures`
- `last_status_code`, `last_error_message`
- `circuit_open_until` (when set in the future, calls are skipped)
- `last_open_reason` (surfaced on the Ops dashboard in Commit 3)
- Computed `state` property: `healthy` / `degraded` / `failed` / `circuit_open`

### `apps/ops/services/provider_health.py`
- `record_success(provider)` — clears failures and any open circuit (a single success closes the breaker).
- `record_failure(provider, status_code, error_message)` — increments consecutive_failures; auto-opens the circuit on:
  - HTTP 401 (key invalid / expired) — first occurrence
  - HTTP 429 (quota exhausted) — first occurrence
  - 3+ consecutive failures
- `is_circuit_open(provider)` — read-only, cheap, used by the upcoming router.
- `open_circuit(provider, reason)` and `reset_circuit(provider)` — manual controls (Commit 3 wires the dashboard buttons).
- All mutating calls swallow exceptions and log warnings — telemetry can never take down the upstream provider.

### Snapshot source-tagging schema (all 5 sports)
Additive, default-safe migrations on `mlb`/`cfb`/`cbb`/`college_baseball`/`golf` snapshot models:
- `odds_source` — `odds_api` / `espn` / `manual` / `cached` (default `odds_api`)
- `source_quality` — `primary` / `fallback` / `stale` / `unavailable` (default `primary`)

The fields are inert this commit (every new row defaults to `odds_api`/`primary`, matching today's behavior). Commit 2 starts using them when the router lands.

### Settings
- `ODDS_PROVIDER_CIRCUIT_COOLDOWN_MINUTES` (default 60) — read on every breaker-open so test overrides take effect immediately.
- `FRESH_ODDS_MAX_AGE_MINUTES` (default 180) — for Commit 2's freshness gating.
- `STALE_ODDS_MAX_AGE_MINUTES` (default 720).

### Tests (21 new in `apps.ops.tests`)
- State transitions: get_or_create idempotent, default healthy, success resets failures, failure increments.
- Circuit triggers: 401 opens immediately, 429 opens immediately, 3 consecutive failures opens, 1–2 failures don't open, success resets the counter.
- Cooldown: stays open during, auto-closes after, success after cooldown returns to healthy.
- Manual controls: reset clears state, force-open works, state_summary returns dict shape.
- Exception safety: record_success / record_failure / is_circuit_open all swallow DB errors without raising.
- Settings override: cooldown is read dynamically so override_settings works in tests.
- Schema: snapshot defaults to `odds_api`/`primary`, can explicitly set `espn`/`fallback`.

477 total tests pass (pre-existing `feedback.tests` ImportError unchanged).

### Files
- New: `apps/ops/services/provider_health.py`, `apps/ops/migrations/0002_providerhealth.py`, plus 5 sport-snapshot migrations.
- Edited: `apps/ops/models.py`, `apps/ops/tests.py`, `brotherwillies/settings.py`, all 5 sport `models.py`.

**Out of scope (Commits 2 + 3):** the explicit router, freshness gating, recommendation safety (Elite/Top Play suppression), MLB hub degraded banner, Ops dashboard provider cards, header dot integration with provider state.

---

## 2026-04-26 - System status indicator in the header (superuser-only)

**Summary:** A small colored dot in the top-right of the header (next to the help/profile icons) gives superusers a constant at-a-glance read on system health. Click navigates to the Ops Command Center; hover shows a 3-line summary tooltip (overall status, last Odds API status, last cron status).

- **Color mapping:** 🟢 green (all systems operational) / 🟡 yellow (warnings) / 🔴 red (needs attention) / ⚪ unknown (no telemetry yet).
- **Data source:** existing `apps.ops.services.command_center.build_snapshot()` — same source the Ops dashboard reads, so the dot and the page can never disagree.
- **Implementation:** new context processor `apps.ops.context_processors.ops_status`, wired in `settings.TEMPLATES`. The processor returns an empty dict for non-superusers — so the snapshot is never computed for the 99%+ of requests that wouldn't show the dot. Wrapped in a broad try/except so a snapshot failure can never take down the header (falls back to "unknown").
- **Note on audience:** the **dropdown link** (yesterday's work) is `is_staff or is_superuser`. The **status dot** is strict superuser only — system-health red flags should only surface to people who can act on them.

**Tests:** 8 new in `apps.ops.tests.HeaderStatusIndicatorTests` — hidden for anonymous, regular users, and staff-only; visible for superusers; correct color for empty/healthy/failure states; clicks navigate to `/ops/command-center/`; context processor survives a snapshot exception (header stays alive). 456 total tests pass.

**Bug-fix tucked in:** I introduced (and immediately fixed) another multi-line `{# ... #}` comment in `base.html` — same Django gotcha as Apr 25. Switched to `{% comment %}…{% endcomment %}`. The codebase scan now reports zero remaining multi-line `{# #}` blocks.

**Files:** new `apps/ops/context_processors.py`; edited `brotherwillies/settings.py`, `templates/base.html`, `static/css/style.css`, `apps/ops/tests.py`.

---

## 2026-04-26 - Profile dropdown link to Ops Command Center

**Summary:** Adds a "⚙️ Command Center" entry to the header profile dropdown for staff/superusers, slotted into the existing staff-tools cluster (between MLB Diagnostic and Admin Console). The Ops view's auth gate was also broadened from `is_superuser` only to `is_staff or is_superuser` so the link doesn't dead-end for staff users — matching the dropdown convention used by every other staff item there.

- Active state: when the user is on `/ops/`, the dropdown item gets a `--active` modifier with the brand accent color so it reads as "you are here" rather than a hover state.
- The previously-added Ops button on the Profile page (`/profile/`) was updated to use the same broader gate.

**Tests:** 6 new in `apps.ops.tests.ProfileDropdownLinkTests` — link hidden from anonymous users, hidden from regular users, visible for staff, visible for superusers, active-state class present on `/ops/`. Plus a positive `test_staff_user_can_access` confirming the broadened view gate.

**Files:** `templates/base.html`, `apps/ops/views.py`, `templates/accounts/profile.html`, `static/css/style.css`, `apps/ops/tests.py`.

---

## 2026-04-26 - Odds Intelligence — decision integration + UI + analytics (Commit 2)

**Summary:** Movement signals now flow into the recommendation engine, the MLB hub tile, the MLB game detail page, and a new analytics panel on `/profile/performance/`. The provider hook also rolled out to CFB/CBB/college_baseball. Strictly additive — recommendations are never downgraded by movement.

### Recommendation integration
- `BettingRecommendation` gained four fields, populated at recommendation time and frozen there for historical analytics: `movement_class`, `movement_score`, `movement_supports_pick`, `market_warning`.
- New properties on both `Recommendation` (dataclass) and `BettingRecommendation` (model): `confidence_nudge_pp`, `displayed_confidence`, `market_movement_chip`.
- Confidence nudge math (capped, additive only):
  - `sharp` + supports → +5pp
  - `strong` + supports → +3pp
  - `moderate` + supports → +1pp
  - All other cases → 0pp
- `displayed_confidence` clamps at 99 so the UI never reads "100% confident."
- `market_warning` fires only at strong/sharp **against** the picked side. The recommendation status, tier, and base confidence are unchanged — the warning surfaces as a chip, not a downgrade.

### UI
- **MLB hub tile** — new chip slot in `_tile_actions.html` rendering `📈 Market Support` / `📉 Market Against You` / `↗ Market Moving`. Three CSS variants in `mlb.css` (`.mlb-action--movement{,-support,-warn}`). Tooltip explains the score.
- **MLB game detail** — new "Market Movement" card between the probability table and the Odds Snapshot card. Status-tinted using new generic `.card-success` / `.card-warning` classes.

### Analytics
- `compute_market_movement_agreement(bets)` in `apps/mockbets/services/recommendation_performance.py` buckets settled bets into `agreed` / `disagreed` / `no_signal` based on the linked `BettingRecommendation`'s movement flags.
- New "Market Movement Agreement" section on `/profile/performance/` — three rows × five metrics (bets / win rate / ROI / avg CLV / +CLV %) so users can see whether market agreement actually improves outcomes.
- `compute_all()` now includes `by_market_movement`.

### Provider rollout
- `apps/datahub/providers/{cfb,cbb,college_baseball}/odds_provider.py` — each `OddsSnapshot.objects.create(...)` block now invokes `apply_movement_intelligence()` immediately after creation (same shape as MLB).
- **Golf is intentionally NOT wired** — the schema has `outright_odds` + `implied_prob` per golfer (no two-sided market) and the provider also dedupes to one row per event per day. Adding golf cleanly would require both an outright-aware significance/score path and a relaxed dedup. Documented in `apps/datahub/providers/golf/odds_provider.py` with the work that's needed.

### Tests
22 new tests in `apps.core.tests` cover:
- Confidence nudge math (table, no-nudge cases, clamp at 99, base=None passthrough)
- Chip label precedence (warning > support > raw movement; noise → None)
- `movement_signal_for_pick` (too-few snapshots, supports home, warns away pick on same data, invalid side, None game)
- `get_recommendation` carries the new movement fields end-to-end
- Analytics: bucket logic for agreed / disagreed / no_signal, including "no recommendation FK" case; pending bets excluded
- MLB hub tile: chip renders when present, absent when not
- Provider hook smoke tests for CFB / CBB / college_baseball

442 total tests pass (pre-existing `feedback.tests` ImportError unchanged).

### Files
- New: `apps/core/migrations/0004_bettingrecommendation_market_warning_and_more.py`.
- Edited: `apps/core/models.py`, `apps/core/services/odds_movement.py`, `apps/core/services/recommendations.py`, `apps/core/tests.py`, `apps/mockbets/services/recommendation_performance.py`, `apps/accounts/views.py`, `apps/datahub/providers/{cfb,cbb,college_baseball,golf}/odds_provider.py`, `templates/mlb/_tile_actions.html`, `templates/mlb/game_detail.html`, `templates/accounts/performance.html`, `static/css/mlb.css`, `static/css/style.css`.

---

## 2026-04-25 - Ops Command Center + template comment fix

**Summary:** New superuser-only `/ops/command-center/` page that gives an at-a-glance read on Odds API health, Odds API quota, and the cron pipeline — built so we never again have to read deploy logs to understand "is something broken right now."

### What it shows
- **Overall banner** — green / yellow / red / unknown, computed from the worst component.
- **Odds API health dial** — calls in the last 24h, failure count, average latency. Goes red on any failure in the last hour, yellow on any older 24h failure. Empty DB renders "No API activity yet" rather than a misleading green.
- **Quota dial** — `x-requests-used` / `x-requests-remaining` headers from the most recent successful call, plus a percent-used meter. ≥90% = red, ≥70% = yellow.
- **Cron pipeline dial** — last status of `refresh_data` and `refresh_scores_and_settle` plus 7-day success/failure counts. Detects rows stuck in `running` for >10 min as crashed workers.
- **Recent failures + recent runs tables** — durable history pulled from the new tables.

### Foundation
- New app `apps.ops` with `OddsApiUsage` (one row per outbound Odds API call) and `CronRunLog` (one row per management-command run).
- `apps.datahub.providers.client.APIClient.get()` now logs every Odds API call (success or failure) to `OddsApiUsage`. Logging never raises — telemetry can't break ingestion.
- `refresh_data` and `refresh_scores_and_settle` are wrapped in a `cron_run_log()` context manager that records start/finish, captures stdout tail, and marks `partial` when sub-tasks fail.
- All commands accept `--trigger=cron|manual|deploy` and `--triggered-by-user-id=N` so manual runs from the dashboard credit the right user.

### Manual controls (superuser-only, POST + confirm)
- **Run Full Data Refresh** — spawns `refresh_data` in a subprocess. Anti-overlap guard: blocked while another `refresh_data` is in flight.
- **Run Score Refresh + Settle** — spawns `refresh_scores_and_settle`. Same overlap guard.
- **Test Odds API** — synchronous one-shot probe of `/v4/sports`. On 401/429 the dashboard surfaces actionable copy ("Rotate the key in Railway → Variables").

### Tests
32 new tests in `apps.ops.tests` cover URL detection, sport extraction, success/failure logging, the context manager's success/partial/exception paths, the stuck-running guard, all health classifications (green/yellow/red/unknown for empty/healthy/failure DBs), and the manual-trigger views (auth gating, anti-overlap, GET rejection, missing-key path, 401 path).

### Template comment fix (visible bug)
Two multi-line `{# ... #}` comments were being rendered as literal text on the MLB hub page (Section 4 — Unrated) and the MLB focus banner. Django's `{# #}` syntax only works on a single physical line — switched both to `{% comment %}…{% endcomment %}`. Added a check (script in `docs/`) so this doesn't sneak back.

### Files
- New: `apps/ops/{__init__.py, apps.py, models.py, admin.py, views.py, urls.py, tests.py}`, `apps/ops/services/{__init__.py, api_logging.py, cron_logging.py, command_center.py}`, `apps/ops/migrations/0001_initial.py`, `templates/ops/command_center.html`, `static/css/ops.css`.
- Edited: `apps/datahub/providers/client.py`, `apps/datahub/management/commands/refresh_data.py`, `apps/datahub/management/commands/refresh_scores_and_settle.py`, `brotherwillies/settings.py`, `brotherwillies/urls.py`, `templates/accounts/profile.html`, `templates/mlb/hub.html`, `templates/mlb/_focus_banner.html`.

---

## 2026-04-23 - De-vigged edge + Closing Line Value tracking

**Summary:** Two precision math upgrades that directly affect bet quality and how we measure it.

### De-vig the market
The prior edge calculation was `model_prob - raw_implied_prob`, where `raw_implied_prob` included the sportsbook's overround. On a -110/-110 line, raw implied is 52.38% each side — summing to 104.76%. Against a 55% model prob, that reported edge as +2.62pp when the fair (de-vigged) edge is +5pp. A 50% model call on the same line reported -2.38pp (fade) when the real edge is 0.

- New module `apps/core/utils/odds.py` — `american_to_implied_prob`, `american_to_decimal`, `devig_moneyline_prob`, `devig_two_way`, `closing_line_value`.
- `apps/core/services/recommendations.py::_moneyline_candidate` normalizes the two implied probs to sum=1 before computing per-side edge.
- Existing `_implied_prob` kept as a thin alias to avoid breaking other callers.

Expected impact: a subset of currently "recommended" bets with thin raw edge will drop to "not_recommended" as the vig is stripped. A few currently "not_recommended" bets may move up. Smaller but honest edges.

### CLV tracking per bet
Professional bettors track CLV because it resolves at game start (not settlement) and beats win rate as a leading indicator of real edge. Every positive-CLV bet validates selection quality even if it loses.

New fields on `MockBet` (migration `mockbets.0006`):
- `closing_odds_american` — snapshot of the pre-first-pitch moneyline on the bet's chosen side.
- `clv_cents` — `bet_decimal - close_decimal`. Positive = bet beat the close.
- `clv_direction` — `'positive'` / `'negative'` / empty.

New service `apps/mockbets/services/clv.py`:
- `capture_bet_clv(bet)` finds the latest `OddsSnapshot` with `captured_at < first_pitch`, matches the bet's selection to the home/away side, computes CLV, writes atomically. Idempotent (skips already-populated bets).
- `capture_closing_odds(game)` walks all pending-CLV bets on a game.

`_apply_settlement` now calls `capture_bet_clv(bet)` as a final step — the closing snapshot is already in the past by settlement time.

Analytics surface:
- `compute_performance_by_status` / `compute_performance_by_tier` emit `clv_sample`, `avg_clv`, `positive_clv_rate`.
- Analytics dashboard tables gained a `CLV %+` column. Missing-data buckets show `—`.
- `/mockbets/` bet row shows `CLV: +0.075` (green) / `-0.042` (red) when populated.

### Correctness note
The task spec wrote `clv = close_dec - bet_dec`, which would flip the sign under the standard "positive = beat the market" definition. Implemented the semantically correct `bet_dec - close_dec` and flagged the reversal in the utility's docstring.

### Tests (23 new, all pass)
Full app suite: 271/272 (pre-existing `feedback.tests` import error unchanged).

---

## 2026-04-22 - MLB hub is now a decision board (elite / recommended / not recommended)

**Summary:** The `/mlb/` page stopped being a schedule and started being a decision board. Today's games (live + scheduled today) are now partitioned into three color-coded sections — 🔥 Top Plays Today (elite), 👍 Recommended Bets, ⚠️ Not Recommended — each using the shared 3-max `.tiles-container` grid. CTAs adapt per status: "Bet This" (green), "Not Recommended" (grey, still clickable), or "Mock Bet" when no recommendation exists. Every game still renders.

### Service
- `apps/mlb/services/prioritization.py`:
  - `GameSignals` gains a `recommendation` field (full Recommendation dataclass, or `None` when odds aren't in yet).
  - `build_signals` stores the recommendation alongside `pick_text` / `pick_action_label` (it was already being fetched — just hold onto it).
  - New `partition_games_by_decision(signals)` → `{elite, recommended, not_recommended}`. Elite = `rec.tier == 'elite'`. Recommended = `status == 'recommended' AND tier != 'elite'`. Not recommended = `status == 'not_recommended'` OR `recommendation is None`. Sort within each section: edge DESC, confidence DESC; null-recommendation games sort last.

### View
- `apps/mlb/views.py::mlb_hub`:
  - After `mark_top_opportunities`, calls `assign_tiers(all_recs)` across the combined live + today slate so the Phase-4 guardrail (`MAX_ELITE_PER_SLATE=2`) is enforced **before** partitioning — Top Plays never shows more than 2.
  - Replaces the raw `live_tiles` / `today_tiles` context with `elite_games` / `recommended_games` / `not_recommended_games` (plus the existing context keys kept for the focus banner + upcoming list).

### Template (`templates/mlb/hub.html`)
- Replaced Live Now / Today rails with three decision sections using `tiles-container`.
- Each tile still auto-picks the right partial (`_tile_live.html` for live games, `_tile_upcoming.html` otherwise) — the dimension changed, not the tile rendering.
- Empty-state card when no elite plays qualify: "No high-confidence plays right now. Check recommended bets below."
- Upcoming (future days) kept as a separate context section at the bottom.

### CTA copy (`templates/mlb/_tile_actions.html`)
- `Bet This` — green solid button — when `s.recommendation.status == 'recommended'`.
- `Not Recommended` — grey outlined button, **still clickable** — when `s.recommendation.status == 'not_recommended'`.
- `Mock Bet` — unchanged — when no recommendation exists.

### Styling (`static/css/mlb.css`)
- `.mlb-section--elite .mlb-section__title` gold, `.mlb-section--recommended` green, `.mlb-section--not-recommended` muted grey — header accents match the section meaning.
- `.mlb-rail--dimmed` applies a 0.7 opacity to the whole not-recommended row (hover restores full); still readable, just stepped back.
- `.mlb-bet-btn--bet-this` and `.mlb-bet-btn--not-recommended` variants.
- `.mlb-empty-state` placeholder so the three-section rhythm doesn't collapse when elites are empty.

### Sample rendered output (abbreviated)
```
🔥 Top Plays Today                                  2
  Yankees ML (-110)  · +12% edge · 78% conf  [Bet This]
  Dodgers ML (-120)  · +10% edge · 72% conf  [Bet This]

👍 Recommended Bets                                 4
  Cardinals ML (+120) · +7% edge  · 55% conf [Bet This]
  Mets ML (-105)      · +6% edge  · 54% conf [Bet This]
  ...

⚠️ Not Recommended                                  6
  Phillies ML (-200)  · +3% edge · 70% conf  [Not Recommended]  ← juice gate
  Rockies ML (+400)   · +1% edge · 23% conf  [Not Recommended]  ← low edge
  ...
```

### Tests (11 new in `apps/mlb/tests.py`)
- `MLBHubDecisionPartitionTests` (8):
  - Elite/recommended/not-recommended/null-rec each land in the right section
  - Every input game appears in exactly one section (no dupes, no drops)
  - Within-section sort is edge DESC, confidence DESC
  - Null-recommendation games sort last within not-recommended
  - `assign_tiers` cap of 2 is honored end-to-end — demoted would-be-elites fall to recommended
- `MLBHubCTATests` (3): CTA text adapts to status — "Bet This" / "Not Recommended" / fallback "Mock Bet".

Full app suite: 239/240 (pre-existing `feedback.tests` import unrelated). Django check clean. No new migrations.

### No games removed
Every signal that `prioritize()` produced still renders — just in a different section. Games without a recommendation appear in the Not Recommended list (not a fourth bucket) with the dimmed treatment. The Upcoming section retains future-day games.

---

## 2026-04-22 - Actionable language + visual hierarchy + Why-This-Lost engine

**Summary:** Three paired upgrades that make the system actively coach decisions instead of just describing them: actionable "Recommended Bet:" / "Model Lean:" CTA copy replacing passive "Model Pick" language, a sharper visual hierarchy between elite / strong / standard / not-recommended cards, and a full Why-This-Lost analysis engine that runs on every lost bet and aggregates into a Loss Breakdown widget on the analytics dashboard.

### Phase 1 — Actionable language
- `action_label(status)` helper + `Recommendation.action_label` property + `BettingRecommendation.action_label` DB-side property. Returns `'Recommended Bet'` when `status='recommended'`, `'Model Lean'` otherwise.
- Templates updated:
  - `templates/core/includes/model_pick_banner.html` — detail-page banner headline.
  - `templates/core/value_board.html` — lobby tile inline pick line.
  - `templates/mlb/_focus_banner.html` — MLB hub focus banner pick row (reads `focus.pick_action_label`).
  - `templates/mlb/_tile_actions.html` — Best Bet chip now reads the recommendation's action label; the bet_placed chip is labeled "Your Bet".
- `apps/mlb/services/prioritization.py::GameSignals` gains `pick_action_label` populated from the recommendation.

### Phase 2 — Visual hierarchy
- `.game-card-tier-elite` — 2px gold border + soft glow (`0 0 12px rgba(255,215,0,0.4)`).
- `.game-card-tier-strong` — 2px accent-cyan border.
- `.game-card-tier-standard` — 1px faint white border.
- `.game-card-not_recommended` — opacity 0.7 + dashed border (still fully readable, restored on hover).
- Status chip colors — `.gc-pick-status-recommended` solid `#16a34a` + `.gc-pick-status-not_recommended` solid `#6b7280`. Strong contrast for fast scan.
- New `.gc-pick-action` / `.model-pick-action` accent on the "Recommended Bet:" prefix.

### Phase 3-7 — Why This Lost engine
**Service** — `apps/mockbets/services/loss_analysis.py`:
- `analyze_loss(mock_bet)` classifies into one of: `variance` (Bad Luck), `model_error` (Model Miss), `market_movement` (Market Misread), `bad_edge` (Weak Edge), `unknown` (no snapshot).
- Priority-ordered rule resolution (rules in the spec overlap):
  1. `bad_edge` — edge < 4pp. The bet shouldn't have cleared decision rules.
  2. `variance` — edge ≥ 5pp AND confidence ≥ 60. Strong call, just unlucky.
  3. `market_movement` — market implied % > model confidence %. We bet against a correct market.
  4. `model_error` — confidence ≥ 65 but not a variance case. Overconfident without edge.
  5. fallback `model_error` for low-confidence losses that cleared the bad_edge check.
- Returns `{primary_reason, details, confidence_miss, edge_miss}`. `confidence_miss` is signed: positive means we were more confident than market.
- Never raises; missing snapshot → `unknown`.

**Persistence** — `MockBet` gains three fields (migration `0005_mockbet_confidence_miss_mockbet_edge_miss_and_more`):
- `loss_reason` — one of the reason keys.
- `confidence_miss` — Decimal, signed pp gap between our confidence and market implied.
- `edge_miss` — Decimal, pp edge we claimed.

**Settlement hook** — `apps/mockbets/services/settlement.py::_apply_settlement` runs `analyze_loss` whenever the resolved result is `loss` and writes the three fields atomically with the rest of the settlement. Non-fatal — analyzer exceptions are logged; the settlement itself always succeeds.

**UI**:
- `templates/mockbets/my_bets.html` — each lost bet card now has a "Loss Reason: Bad Luck" badge plus confidence/miss/edge metrics inline.
- `templates/mockbets/bet_detail.html` — "Why This Lost" section with the full explanation text and a data table (confidence, miss vs market, edge claimed). Color-coded left-stripe per reason.
- `templates/mockbets/analytics.html` — new "Loss Breakdown" widget listing each reason with count + share, above the cumulative P/L chart.

**Aggregate** — `compute_loss_breakdown(bets)` in `recommendation_performance.py` returns `{total_losses, rows: [{reason, label, count, pct}, ...]}` in a stable display order so the widget doesn't reshuffle between renders. Wired into `compute_all` so the existing dashboard view gets it for free.

### Example loss analysis output
```
Loss Reason: Bad Luck (variance)
  Model Confidence:    72%
  Confidence vs Market: +22pp
  Edge Claimed:        +7.0pp

Loss Breakdown (across 18 settled losses):
  Bad Luck        8  44.4%
  Model Miss      5  27.8%
  Market Misread  3  16.7%
  Weak Edge       2  11.1%
  Unknown         0   0.0%
```

### All games still visible
Per the non-negotiable rules, nothing filters games out. `.game-card-not_recommended` reduces opacity and applies a dashed border, and the hover restores full opacity — the card is always clickable and readable.

### Tests (14 new in `apps/mockbets/tests.py`)
- `LossAnalysisRuleTests` (7): bad_edge wins over everything, variance beats model_error when edge strong, market_movement triggered when implied > confidence, model_error for high-confidence-no-edge, confidence_miss / edge_miss math, unknown when snapshot missing, non-loss safely returns unknown.
- `SettlementLossHookTests` (2): settlement populates loss_reason + edge_miss on a loss; win leaves loss fields empty.
- `LossBreakdownAggregateTests` (2): percentages + stable display order; empty losses returns zeros.
- `ActionLabelTests` (3): recommended → "Recommended Bet", not_recommended → "Model Lean", unknown/empty → fallback.

Full app suite: 228/229 (pre-existing `feedback.tests` import error unchanged). `python manage.py check` clean. Migration applied cleanly.

---

## 2026-04-21 - MLB hub fixes: 3-up wrap grid, actual pick on banner + Bet Placed

**Summary:** Fixed three defects on the MLB hub page spotted live:

1. **Horizontal scroll carousel replaced with a 3-up wrapping grid.** `.mlb-rail` was `display: flex; overflow-x: auto; scroll-snap-type: x proximity` with 288px-fixed tiles, which rendered 4+ tiles across with hidden scroll on wider viewports. Switched to `display: grid; grid-template-columns: repeat(3, minmax(0, 1fr))` with the same 1024/640 breakpoints as the rest of the app. Tiles now wrap to new rows instead of scrolling.
2. **Focus banner + Bet Placed chip answer "who did I bet on?"** Added `pick_text`, `pick_selection`, `pick_odds`, `user_bet_selection`, `user_bet_odds`, `user_bet_bet_type` fields to `GameSignals`. `prioritize()` fetches the decision-layer recommendation per game (via `get_recommendation`) and expands the pending-bet query to surface selection + odds. The focus banner shows "Your bet: Yankees (+120)" if the user has a bet on that game, or "Model pick: Yankees Moneyline (-150)" otherwise. The Bet Placed action chip now renders `· Yankees (+120)` inline. The Best Bet chip shows the model pick inline instead of the generic "Model edge vs market".
3. **Template comment leak.** `templates/mlb/_focus_banner.html` had `{# ... #}` spanning multiple lines — Django only supports single-line `{# #}` comments. Replaced with `{% comment %}...{% endcomment %}` so it no longer renders as visible text.

### Files
- `static/css/mlb.css` — `.mlb-rail` is now a 3-max wrapping grid; `.mlb-tile` width is `100%` (grid cell) instead of `288px`; new `.mlb-action__pick` + `.mlb-focus__pick` styles.
- `apps/mlb/services/prioritization.py` — `GameSignals` gains pick + user-bet fields; `build_signals` calls `get_recommendation('mlb', game, user)` (non-fatal on failure); `prioritize()` bet query pulls selection/odds/bet_type.
- `templates/mlb/_focus_banner.html` — replaces multi-line `{# #}` with `{% comment %}`; adds the pick row between matchup and action meta.
- `templates/mlb/_tile_actions.html` — Bet Placed chip shows `· {selection} ({odds})`; Best Bet chip shows the model pick text inline.

### Tests
Full MLB app test suite: 81/81 pass. Full app suite: 213/215 (two pre-existing unrelated failures unchanged). Django check clean.

---

## 2026-04-21 - Selection engine + recommendation performance feedback loop

**Summary:** Upgraded the recommendation layer to classify each pick as Recommended or Not Recommended using edge-aware decision rules, switched tier classification from confidence-based to edge-based, denormalized snapshot fields onto MockBet so analytics don't depend on future rule/model changes, and shipped a Recommendation Performance widget on the mock-bet analytics page with a 0-100 system confidence score. All games still visible — the new status is a label, not a filter.

### Decision rules (new)
Constants in `apps/core/services/recommendations.py` — units are percentage points to match the stored `model_edge` scale:
- `MIN_EDGE = 4.0` — below this, any pick is Not Recommended (`low_edge`)
- `STRONG_EDGE = 6.0` — edge required to clear heavy-favorite juice
- `ELITE_EDGE = 8.0` — overrides the juice rule; always Recommended
- `HEAVY_FAVORITE_ODDS = -150` — odds threshold for the juice gate

Rule evaluation order (first match wins):
1. Elite override — `model_edge >= ELITE_EDGE` → Recommended
2. Min edge — `model_edge < MIN_EDGE` → Not Recommended (`low_edge`)
3. Juice gate — `odds_american <= -150 AND edge < STRONG_EDGE` → Not Recommended (`high_juice`)
4. Default — Recommended

### Tier migration (confidence → edge)
Tier classification now reads `model_edge`, not `confidence_score`:
- `elite` — edge ≥ 8 pp
- `strong` — edge ≥ 6 pp
- `standard` — otherwise

Rationale: "strength of the opportunity" is a better mental model than "model confidence" — 92% confidence against -900 odds is not a strong edge, the market already priced it in. `assign_tiers` still caps elite at `MAX_ELITE_PER_SLATE=2` and now ranks by (edge desc, confidence desc). `_partition_elite` uses the same ordering.

### Snapshot fields
- `apps/core/models.py::BettingRecommendation` — new persisted `status`, `status_reason` fields.
- `apps/mockbets/models.py::MockBet` — new denormalized snapshot fields: `recommendation_status`, `recommendation_tier`, `recommendation_confidence`, `status_reason`. Captured in `place_bet` at bet creation so "what the system believed at bet time" is preserved forever.
- Migrations: `core/0003_bettingrecommendation_status_and_more.py`, `mockbets/0004_mockbet_recommendation_confidence_and_more.py`.

### Performance service
`apps/mockbets/services/recommendation_performance.py`:
- `compute_performance_by_status(bets)` — wins/losses/pushes/win_rate/roi/net_pl grouped by recommended vs not_recommended.
- `compute_performance_by_tier(bets)` — same metrics grouped by elite/strong/standard.
- `compute_system_confidence_score(bets)` — 0-100 score = `(win_rate * 0.5 + roi_term * 0.3 + sample_term * 0.2) * 100`, where `roi_term = ((clamp(roi/20, -1, 1) + 1) / 2)` and `sample_term = min(1, n/50)`. Sample penalty prevents small lucky streaks from maxing the score.
- `compute_all(bets)` — bundle for the analytics widget.

### UI
- Lobby tile shows status chip next to the tier label (`Recommended` green / `Not Recommended` muted grey, optional reason italic).
- Detail-page banner shows the same.
- `.game-card-not_recommended` applies reduced opacity (0.72) and softer border — card is still fully readable, just de-emphasized. **Never hidden.**
- Mock Bet Analytics page: new "Recommendation Performance" widget at the top with the system confidence score tile and two tables (by status, by tier). Framed around: "Recommended should outperform Not Recommended. Elite should outperform Strong. If not, the rules are wrong — update them."

### Sample analytics output
```
System Confidence Score: 62.4
  — 18 settled bets, 61.1% win, 12.3% ROI

By Status
  Recommended       13 bets   69.2% win   +18.5% ROI   +$240.50
  Not Recommended    5 bets   40.0% win   -8.0% ROI    -$40.00

By Tier
  Elite              2 bets  100.0% win   +75.0% ROI   +$150.00
  Strong             8 bets   62.5% win   +14.0% ROI   +$112.00
  Standard           8 bets   50.0% win    -2.5% ROI    -$20.00
```

### Tests (17 new / updated in `apps/core/tests.py`)
- `DecisionRuleTests` — 7 tests covering low_edge, boundary, high_juice, elite-override, favorite-boundary, and status propagation into the dataclass.
- `TierThresholdTests` — rewritten for edge-based boundaries.
- `AssignTiersGuardrailTests` — rewritten for edge-first ranking.
- `LobbySortByTierTests` — reframed around elite-beats-strong and within-tier tiebreak.
- `ElitePartitionTests` — updated to reflect edge-based sorting.
- `PlaceBetSnapshotsRecommendationTests` — new `test_place_bet_denormalizes_status_tier_confidence`.
- `RecommendationPerformanceTests` — 6 new tests covering status grouping, tier grouping, pending exclusion, sample-size penalty, empty-bets safety, compute_all bundle.

Full app suite: 213/215 (pre-existing `feedback.tests` import + `GolfOddsProviderPersistGateTests` flake unchanged).

### All games still visible
The `not_recommended` state is a visual label only. `_partition_elite` and `_sort_games_by_tier_then_edge` still include every game in `games_data`. Verified in existing lobby tests.

---

## 2026-04-21 - Observability, configurable score window, strict 3-column tile grid

**Summary:** Made the 15-minute score-refresh cycle observable (structured per-provider + cycle-summary logs, plus per-miss warnings for games the API returns but we haven't ingested), made the score-update window env-configurable, and enforced a shared 3-max tile grid across all boards.

### Settings
New env vars (defaults match prior hardcoded behavior):
- `SCORE_UPDATE_LOOKBACK_HOURS` (default `24`) — how far back the lightweight cycle considers games.
- `SCORE_UPDATE_LOOKAHEAD_HOURS` (default `12`) — how far forward.
Read at call time so `self.settings(...)` overrides take effect immediately in tests.

### Observability
- `apps/datahub/providers/base.py`:
  - Each `not_found` miss now emits `logger.warning('Score update skipped — game not found in DB', extra={sport, external_id})` so operators see drift between the provider feed and our DB.
  - Cycle summary log (`logger.info('Score update summary', ...)`) carries `updated` / `unchanged` / `skipped` / `out_of_window` / `not_found` / `window_hours` — grepable as one line.
  - New `_normalized_external_id` hook so each provider surfaces its own identifier in the miss log.
- `apps/datahub/services/scores.py`:
  - Per-provider success log after each run (`<sport> score update success`).
  - Per-provider failure log (`<sport> score update failed`, `exc_info`).
  - Cycle summary log (`Score refresh cycle complete`) with `providers_run` / `providers_failed` / `providers_disabled` / `total_updated` / `total_not_found`.
  - Provider exceptions no longer propagate out of the dispatcher — one sport's outage can't block settlement for the others.

### Strict 3-column tile grid
- New shared CSS class `.tiles-container` in `static/css/style.css`:
  - Desktop (>1024px): `repeat(3, 1fr)`
  - Tablet (≤1024px): `repeat(2, 1fr)`
  - Phone (≤640px): `1fr`
  - Gap: 16px
- Applied to:
  - `templates/core/value_board.html` (`.vb-section-body` — both MLB/CBB/CFB tile container and golf)
  - `templates/core/includes/elite_plays_section.html` (elite grid now inherits the 3-max from the shared class)
- Removed per-component grid rules on `.elite-plays-grid` and `.vb-section.open .vb-section-body` so the ceiling lives in exactly one place.
- Layout never exceeds 3 columns regardless of viewport.

### Tests (5 new in `apps/datahub/tests.py`)
- `test_not_found_emits_warning_log_with_external_id` — per-miss warning fires with `external_id`.
- `test_summary_log_includes_counts_and_window` — provider emits the cycle summary info log.
- `test_window_settings_respected` — overriding `SCORE_UPDATE_LOOKAHEAD_HOURS` via `self.settings(...)` flips a previously out-of-window game into `updated`.
- `test_dispatcher_emits_cycle_summary_log` — dispatcher emits both per-provider success log and cycle-complete summary.
- `test_dispatcher_continues_past_provider_failure` — a raising provider produces `results[sport] = {'status': 'error', ...}` without blocking the cycle.

Full app suite: 199/201 (pre-existing `feedback.tests` import + `GolfOddsProviderPersistGateTests` flake unchanged).

### Railway env (ops note)
Optional overrides — set in Railway Variables if tuning:
```
SCORE_UPDATE_LOOKBACK_HOURS=24
SCORE_UPDATE_LOOKAHEAD_HOURS=12
```
Defaults stay equivalent to the previous hardcoded window, so no operator action is required.

---

## 2026-04-21 - Dual-speed pipeline: lightweight scores + settle every 15 minutes

**Summary:** Introduced a narrow 15-minute cron that updates scores/status and settles pending mock bets without pulling odds or recomputing models. The heavy `refresh_data` pipeline (6-hour) is untouched and still owns schedule rebuilds, odds, injuries, pitcher stats, and snapshot capture.

### Why
After the previous stale-pending fix, settlement ran once per 6-hour cycle. That was enough to clear stragglers but still meant up to 6 hours between a game finalizing and a user's bet showing Win/Loss. The new lightweight path closes that gap to ~15 minutes without multiplying API cost or model recompute.

### Files
- `apps/datahub/providers/base.py` — `AbstractProvider` gains an opt-in score-only path (`update_scores_only` + three small hooks: `_find_existing_game`, `_normalized_game_time`, `_extract_score_fields`). Default for non-opted-in providers is a no-op returning `status='not_supported'`.
- `apps/datahub/providers/mlb/schedule_provider.py` — opts in; reuses the existing `fetch()` (MLB Stats API) and `normalize()` shape.
- `apps/datahub/providers/college_baseball/schedule_provider.py` — opts in; reuses the ESPN scoreboard payload.
- `apps/datahub/services/scores.py` — new dispatcher `update_scores_only(sport='all')` that respects per-sport `LIVE_*_ENABLED` toggles and swallows provider-level errors so one sport's outage can't block others.
- `apps/datahub/management/commands/refresh_scores_and_settle.py` — new command chaining: score-only update → `resolve_outcomes` → `settle_mockbets`.

### What's narrow about the lightweight path
- **No writes outside status/home_score/away_score** — pitcher FKs, odds, neutral_site, first_pitch timestamps, etc. are never touched.
- **Dirty check** — unchanged rows skip the write entirely (zero DB traffic when nothing moved).
- **Live window** — only games in `[now-1d, now+12h]` are eligible. Everything else is counted as `out_of_window` and skipped.
- **No row creation** — if the API returns a game we haven't ingested yet, it's counted as `not_found` and skipped. The 6-hour heavy path owns creation.
- **Sport-scoped + error-isolated** — provider failures are logged and reported per sport; other sports still run.

### Tests (8 new in `apps/datahub/tests.py`)
- `ScoreOnlyProviderTests` — updates only status+scores on real change; idempotent skip on re-run; skips games outside the live window; never creates unknown games.
- `RefreshScoresAndSettleCommandTests` — end-to-end: scheduled game → mock bet placed → API reports Final → command flips game + settles bet to `win` with correct payout; running twice produces exactly one settlement log; in-progress games leave the bet pending.

Full app suite: 194/196 (pre-existing `feedback.tests` import + `GolfOddsProviderPersistGateTests` flake unchanged).

### Railway cron setup (ops note)
Add a second cron job alongside the existing heavy one:
- Every **15 minutes**: `python manage.py refresh_scores_and_settle`
- Every **6 hours** (existing): `python manage.py refresh_data`

The 15-minute command is safe to co-run with the 6-hour command — both settle idempotently and neither touches odds/models in the fast path.

### Unsupported sports
CBB and CFB schedule providers do not implement the score-only hooks in this PR. Rationale: their game cadence (weekday evenings for CBB, weekend-heavy for CFB) is already well-served by the 6-hour heavy cron, and their schedule providers key by `(home_slug, away_slug, date)` which would need a different lookup shape. Adding them later is ~10 lines per provider — the base class is structured for it.

---

## 2026-04-21 - Elite Plays section + why-this-is-elite explanation

**Summary:** The lobby now surfaces the day's strongest model picks in a dedicated "🔥 High Confidence Plays" section above the main board, with a short deterministic explanation (Win Probability / Market Implied / Edge) on each elite card. The full slate still renders below — this is a visual hierarchy, not a filter.

### View
- `_partition_elite(games_data, live_data)` in `apps/core/views.py` splits elite-tier games out of the main board. Runs AFTER `assign_tiers` so the slate-level cap (≤2 elites per view) is already applied.
- Returned elite list is sorted by (confidence desc, edge desc) so the strongest pick leads.
- Elites are removed from both upcoming and live lists before they flow into `_group_games_by_timeframe` — no duplication below.
- Template context gains `elite_games`.

### Service
- `Recommendation.explanation_rows` (new property) returns a list of `{label, value}` dicts: Win Probability (from `confidence_score`), Market Implied (from `odds_american` via the existing `_implied_prob` helper), Edge (from `model_edge`).
- `_build_explanation_rows()` is the shared builder used by both the dataclass and the persisted `BettingRecommendation` DB model — no template-side math.
- Metrics that can't be computed (e.g. missing odds) are skipped, never fabricated.

### UI
- New partial `templates/core/includes/elite_plays_section.html` renders the featured section:
  - Header `🔥 High Confidence Plays` + subtitle
  - One card per elite rec: sport badge, matchup, tier label, pick + line, 3-row explanation block, "View Analysis" link + "Place Mock Bet" button (authed users).
- Lobby now includes `mockbets/includes/place_bet_modal.html` so the elite CTA places bets in one tap without a detail-page hop.
- Styling: gold border + soft glow panel. Cards stay 2-up on desktop (natural max since `MAX_ELITE_PER_SLATE = 2`), responsive down to 1-up on phone.

### Tests (9 new)
`apps/core/tests.py`:
- `ExplanationRowsTests` — full-row formatting, negative-edge sign handling, zero-odds skips Market Implied
- `ElitePartitionTests` — upcoming partition, live partition, sort by (confidence, edge), duplicate-safety, no-rec games stay put, end-to-end with `assign_tiers` showing the cap flows through

Full app suite: 188/189 (pre-existing `feedback.tests` import error unchanged).

---

## 2026-04-20 - Confidence tiers + visual prioritization on recommendations

**Summary:** The BettingRecommendation layer now classifies each pick into elite / strong / standard based on confidence, labels it accordingly in the UI, applies a slate-level cap of 2 elites so the signal stays meaningful, and sorts the lobby so highlighted picks surface first. Architecture is unchanged — tier is a computed property, not a new column.

### Logic
- Thresholds: **elite ≥ 80**, **strong 65–79.99**, **standard <65** (`ELITE_THRESHOLD`, `STRONG_THRESHOLD` in `apps/core/services/recommendations.py`).
- Labels: elite → `🔥 High Confidence`, strong → `Strong Edge`, standard → `Model Pick`.
- `Recommendation.tier` is a dataclass field; `tier_label` is a property. The persisted `BettingRecommendation` model exposes the same via computed properties.
- `assign_tiers(recs)` — slate-level pass that sets each rec's tier from its raw confidence, then enforces `MAX_ELITE_PER_SLATE = 2` by demoting extra elites (ordered by confidence desc, edge desc) to strong.

### Sort (lobby)
- `_sort_games_by_tier_then_edge` sorts games by `(tier_priority, -edge_magnitude)`. Elite always beats strong, strong always beats standard, standard always beats "no recommendation". Within a tier the existing edge-based ordering is preserved as a tiebreaker — same `sort=` query param as before.
- `value_board` view runs `assign_tiers` across the union of live + upcoming games so the elite cap applies to the whole lobby, not per-section.

### Visual
- `.game-card-tier-elite` — gold border + soft glow + subtle gradient (clear at a glance without changing card size, so the 3-up grid stays aligned).
- `.game-card-tier-strong` — accent-blue border ring, moderate emphasis.
- `.game-card-tier-standard` — default card styling.
- `.model-pick-banner-{tier}` mirrors the same treatment on game-detail pages.
- Tier label (🔥 High Confidence / Strong Edge / Model Pick) renders inline on each lobby tile's pick line and as the header on the detail-page banner.

### Tests
- 9 new tests in `apps/core/tests.py`:
  - Boundary checks for raw tier thresholds (80/65 boundaries)
  - Label mapping
  - `assign_tiers` caps elites at 2
  - Edge breaks ties when confidence is equal
  - Small slates keep all elites
  - Empty list is safe
  - Lobby sort: tier beats edge magnitude, edge is tiebreaker within tier, no-recommendation games land at the bottom
- Full app suite: 178/180 (two pre-existing unrelated failures).

---

## 2026-04-20 - Stale pending bug fix + bankroll summary + 3-up tile grid

**Summary:** Closed the loop on mock bets. Pending bets no longer sit forever after games finalize, the mock bets page now surfaces a proper bankroll summary, each bet row shows its stake/payout/net clearly, and lobby tiles render in a capped 3-per-row responsive grid.

### Root cause — stale pending bets
`apps/mockbets/management/commands/settle_mockbets.py` was written to be cron-friendly but **was never actually invoked** by any cron path. `refresh_data` ran schedule → odds → injuries → pitcher-stats → team-records → capture-snapshots → resolve-outcomes, then stopped. `ensure_seed` (deploy path) also skipped it. Because CLAUDE.md confirms Railway has no shell access, `python manage.py settle_mockbets` never ran in production — pending bets sat forever even though the MLB schedule provider correctly flipped `status='final'` with scores on every refresh.

### Fix
Two independent layers so the page is never stale even if one path fails:
- **Cron path** — `refresh_data` now calls `settle_mockbets` after `resolve_outcomes`. `ensure_seed` also calls it on every deploy so stragglers clear on the first boot after this release.
- **View path (defense-in-depth)** — new `settle_user_pending_bets(user)` in `services/settlement.py` is called by `my_bets` before render, scoped to the viewer. Idempotent; filters to `status='final'` with scores populated so it's cheap.

### UI changes
- **Lobby tile grid** — `.vb-section.open .vb-section-body` is now CSS grid: 1-up phone / 2-up tablet (481–768px) / **3-up desktop (≥769px)**. Matches the CLAUDE.md breakpoints.
- **Bankroll summary** (`templates/mockbets/my_bets.html`) — always-visible header card with Total Wagered, Total Won, Total Lost, Net Profit, Record (W–L–P), and Pending count. Math comes from `compute_kpis` — same source as the analytics dashboard (no duplicate accounting).
- **Per-bet card** — each row now has a left-color stripe keyed by result (green win / red loss / grey push / blue pending), a bold result badge, and an explicit Stake / Payout or Loss / Net row.
- **compute_kpis** extended with `total_won` and `total_lost` keys (additive — existing callers unaffected).

### Tests
- 4 new regression tests in `StalePendingRegressionTests` in `apps/mockbets/tests.py`:
  - `test_settle_mockbets_command_clears_stale_pending` — proves the cron-level fix
  - `test_my_bets_view_settles_stale_pending_on_read` — proves the view-level fix (the page no longer shows PENDING for a final game)
  - `test_settle_user_pending_does_not_touch_other_users` — per-user scoping
  - `test_full_pipeline_place_to_final_to_display` — end-to-end: POST bet → flip game final → GET `/mockbets/` → assert WIN badge + $120.00 payout rendered
- `BankrollKPIsTests` locks the `total_won` / `total_lost` math with a mixed-result fixture.
- Full app suite: 169/171 (2 pre-existing failures — `feedback.tests` import error + `GolfOddsProviderPersistGateTests` — unrelated and unchanged).

---

## 2026-04-20 - Decision Layer: BettingRecommendation engine

**Summary:** New thin decision layer converts existing model edge into a single actionable pick per game. No rebuild — reuses every sport's `compute_game_data` via the existing SPORT_REGISTRY; settlement, bankroll, and analytics are untouched.

### Added
- `BettingRecommendation` model (`apps/core/models.py`) — sport-agnostic via nullable per-sport FKs, mirroring MockBet's pattern.
- `apps/core/services/recommendations.py` with `get_recommendation(sport, game, user)` returning a Recommendation dataclass and `persist_recommendation()` that writes a DB row.
- `MockBet.recommendation` FK — snapshots the active model pick at bet placement (set automatically in `place_bet` view, non-fatal on failure).
- Reusable `templates/core/includes/model_pick_banner.html` partial rendered on MLB / CFB / CBB / College Baseball game detail pages.
- Explicit pick line on every lobby game card: `🎯 Cardinals Moneyline (+120) · 6.3% edge`.
- "Place Mock Bet on Model Pick" one-tap button that pre-fills the modal with the model's pick/odds/bet_type.
- Admin registration for `BettingRecommendation`.

### Design
- v1 emits **moneyline picks only** — that's the market where the existing sport model services produce comparable win probabilities. Spread/total picks would require a margin-of-victory or runs model that doesn't yet exist; returning None is more honest than fabricating edge.
- Model source selection: uses the user's configured model if authenticated and a user prob exists; otherwise house model.
- Respects the "neutral language" guardrail — UI copy is "MODEL PICK" / "House Model" / "Your Model", never "best bet" or "lock".

### Tests
- 11 new tests in `apps/core/tests.py` covering implied-prob math, side selection under edge asymmetry, missing-odds no-op, unknown-sport no-op, confidence-score shape, persistence FK correctness, and end-to-end place-bet → recommendation snapshot.
- Full app suite: 76 passing (only pre-existing `feedback.tests` import error remains, unchanged).

---

## 2026-04-19 - MLB injury ingestion (ESPN) + tile display

**Summary:** MLB was the only team sport without injury ingestion. ESPN exposes a clean per-team injuries endpoint; we now consume it, aggregate per team, attach to every upcoming game within a 7-day window, and surface the most-severe player on each tile.

### What's new
- **MLBEspnInjuriesProvider** ([apps/datahub/providers/mlb/injuries_provider.py](apps/datahub/providers/mlb/injuries_provider.py)) — calls `site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{teamId}/injuries` for each MLB team (rate-limited). Maps ESPN status strings to our `InjuryImpact` severity:
  - **Day-To-Day / 7-Day IL** → low (medium for SP)
  - **10-Day IL** → med (high for SP — losing a starter changes the forecast)
  - **15-Day IL / 60-Day IL** → high
  Unknown statuses are ignored (safer than guessing).
- **Aggregation** — one `InjuryImpact` per (team, game) with impact = most severe among the team's listed injuries. `notes` field carries the top-5 most severe players for the tile, newline-separated. Idempotent via `update_or_create` (no duplicate rows on re-ingest).
- **Deploy wiring** — `has_injuries` flipped to `True` for MLB in both `ensure_seed.py` and `refresh_data.py` sports configs. Auto-runs on every Railway deploy; also on the cron-driven refresh path.
- **Registry entry** — `('mlb', 'injuries'): MLBEspnInjuriesProvider`.
- **Tile surface** — the most severe injury per side is rendered below the team rows on live and upcoming tiles. Red-tinted, dashed top border, ⚕ glyph. Example: `ATL  ⚕ SP Spencer Strider (15-Day IL, return 2026-05-01)`.
- **Signal-layer integration** — `_injury_signal` continues to feed the priority + confidence calculation via `high_injury` / `med_injury` reasons (existing behavior), but the `injury_summary` dict now also carries `home_notes` / `away_notes` so templates don't need to re-query.

### Tests: 81/81 MLB green (+8 new)
- Status→impact mapping for SP (10-Day → high) and position players (15-Day → high, 10-Day → med)
- Unknown / empty status returns None
- Normalize aggregates a team to its worst impact; notes carry most-severe first
- Team with no recognized status is dropped entirely
- `persist` creates one row per (team, upcoming game) in the 7-day window
- `persist` is idempotent — re-running with escalated severity updates in place

### Architecture preserved
- Provider is a pure `AbstractProvider` subclass (fetch → normalize → persist).
- Signals layer reads `InjuryImpact` rows the same way it always has.
- No view-level changes; no template-level logic — just two new `mlb-injury` spans per tile.

---

## 2026-04-19 - Confidence, Focus Engine, user state + ESPN odds fallback

### Odds pipeline (hardened + ESPN fallback)
- **ESPN DraftKings feed as fallback** — new `MLBEspnOddsProvider` fetches the public `site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard` JSON, extracts each event's DraftKings odds (spread, total, home/away ML), and persists through the same `OddsSnapshot` model. No API key, no quota overlap with The Odds API. Triggered automatically by `ingest_odds mlb` when the primary produces 0 rows or raises.
- **Widened matching** — primary window bumped from ±1 day (`__date` lookup prone to TIME_ZONE mismatch) to ±36 hours direct datetime compare. Two fallbacks: (1) no-commence → nearest upcoming same-teams game within 7 days; (2) windowed miss → ±4 day widen, pick nearest-by-delta in Python. Portable across SQLite + Postgres.
- **Sample payload + structured skip logs** — first normalized record sampled at INFO; every persist-skip emits `reason=no_team_match|no_game_match|no_moneyline_home`; end-of-run summary includes a `skip_reasons` counter dict.
- **Deploy-log diagnostic** — `ensure_seed` ends with a per-sport odds health summary (total, today-window snapshots, games with coverage). No more guessing from deploy logs whether ingestion worked.
- **`diagnose_odds` management command** — on-demand dump of latest snapshot + per-day coverage for the next N days. Surfaces `ZERO coverage` warnings when any day has games but no odds.

### Confidence scoring (new)
- `compute_confidence(signals) -> [0, 1]` — deterministic pure function blending:
  - line_value_strength (40%) — line value discrepancy mapped linearly 0.06 → 0.15
  - is_close_game (15%), ace_matchup (10%), late_game (5%), tight_spread (10%)
  - data completeness: odds_present (10%), pitchers_known (10%)
  - Blowout clamps the ceiling at 0.4 — the game's decided.
- `GameSignals.confidence` + `confidence_pct` (int) attached before actions are resolved so primary action dicts carry it.

### Focus Engine (new)
- `get_focus_game(signals) -> GameSignals | None` — single "do this right now" pick.
  - Requires a PRIMARY action (best_bet or watch_now; bet_placed excluded — focus surfaces new opportunities, not restates the user's own action).
  - Prefers Best Bet over Watch Now; higher confidence wins; live tiebreaks.
  - Returns `None` when no game qualifies — the hub banner is simply omitted, never faked.
- New `templates/mlb/_focus_banner.html` renders at the hub top: flame kicker, teams matchup, action + reason, confidence mini-bar with percentage label. Gold-tinted card, accent from the home team's primary color.

### User state awareness (new)
- When the user has a pending MockBet on an MLB game, the signals layer inserts a **Bet Placed** primary action — the original Best Bet / Watch Now actions are demoted to secondary (context preserved, not hidden).
- Tile CTA swaps from "Mock Bet" to "View Bet" linking to `/mockbets/<bet_id>/`. The signals layer now carries `user_bet_id` (UUID string), batched in `prioritize()` with the existing pending-bet query.

### UI
- Primary action chips now show a thin confidence bar along the bottom edge (subtle, colored by the chip). Secondary chips stay subdued (no bar).
- `bet_placed` action: gold-family filled pill matching the pending-bet indicator.
- "View Bet" CTA: gold-outlined button replacing "Mock Bet" when the user already has a pending bet.

### Tests: 73/73 MLB green (15 new)
- Confidence: low floor without data, TBD caps ceiling, blowout clamps at 0.4, large line-value saturates, confidence_pct matches confidence * 100, primary actions carry confidence.
- Focus engine: returns None when no primary exists, picks highest-confidence best_bet, Best Bet preferred over Watch Now, bet_placed is excluded from focus.
- Bet Placed override: pending bet → primary `bet_placed`, existing actions demoted to secondary, `user_bet_id` = MockBet UUID.
- ESPN provider: normalize shape, DraftKings preference, empty-odds skip, end-to-end persist creates an OddsSnapshot when teams + game match.
- Existing test `IngestOddsFailFastTests` now mocks the ESPN fallback to stay hermetic.

### Architecture preserved
- Signals layer owns decisions (confidence, focus, bet_placed override).
- Views orchestrate (attach focus to context, run mark_top_opportunities).
- Templates render only (no dict-keyed conditionals, no string comparisons that hide logic).

---

## 2026-04-19 - Odds integrity + structured signals + guided choice

**Summary:** Harden the odds ingestion pipeline against silent failure, restructure the signals layer so UI text never leaks upstream, add a real edge signal (line-value discrepancy), and introduce scarcity-constrained "Top Opportunity" highlighting.

### Odds integrity (ships first)
- **APIClient guardrail** ([apps/datahub/providers/client.py](apps/datahub/providers/client.py)) — every response is now validated before `.json()`. New `NonJSONResponseError` is raised on non-JSON `Content-Type` or on bodies starting with `<html`/`<!doctype`/`<?xml`. The first 200 chars of the body plus URL + status are included in the message. Not retried — an HTML landing page rarely fixes itself within seconds.
- **Fail-fast on zero creates** ([apps/datahub/management/commands/ingest_odds.py](apps/datahub/management/commands/ingest_odds.py)) — after `provider.run()`, if `created == 0` we raise `RuntimeError` in `DEBUG` and log a `logger.error(...)` high-severity event in prod (fires alerting). Post-ingestion sanity check also counts today's OddsSnapshot rows and logs `{sport}_odds_empty_after_ingest` when the user-visible window is empty even if some rows were written.
- **MLB persist diagnostics** — structured skip-reason logs (`no_team_match`, `no_game_match`, `no_moneyline_home`) counted and summarized at the end of every run. Return payload now includes `skip_reasons` dict and a `status` of `'empty'` when nothing was created (previously always reported `'ok'`).

### Structured signals
- **`reasons` are structured keys, not UI strings** — e.g. `'tight_spread'`, `'ace_matchup'`, `'line_value'`. A new template filter [apps/mlb/templatetags/mlb_reasons.py](apps/mlb/templatetags/mlb_reasons.py) (`{{ key|reason_label }}`) maps keys to human-readable labels at render time. Unknown keys fall back to title case so a newly-added key is never a stacktrace.
- **Actions are structured dicts** — `{'type', 'strength', 'reason'}`. Exactly one primary per game. Primary gets a filled pill; secondary is outlined.
- **`ACTION_KEYS`, `ACTION_STRENGTHS`, `REASON_KEYS`** exported for static checks.

### New signals
- **`line_value_discrepancy`** — `|house_prob - market_prob|` computed from [apps/mlb/services/model_service.py](apps/mlb/services/model_service.py) + the latest odds snapshot. Populates `signals.line_value_discrepancy` (always when both probs known), emits `'line_value'` reason + score contribution when ≥ `LINE_VALUE_MIN` (0.06).
- **`late_game` proxy** — elapsed-time approximation until inning state is ingested: `progression = clamp((now - first_pitch) / 3h)`; `late` when ≥ 0.60. Contributes a small score boost and a `'late_game'` reason for live games in the back half.

### Action quality (Objective 6)
- **Best Bet** now requires: odds present AND starters known AND not blowout AND at least one strong edge signal (`tight_spread` OR `line_value`). Reason attaches the strongest available signal (`line_value` preferred over `tight_spread`).
- **Watch Now** priority: close-live > late-game > ace matchup (when not a blowout).
- **Empty actions is a valid state.** Templates render nothing — no placeholder chips, no clutter.

### Top Opportunity scarcity (Objective 4)
- New `mark_top_opportunities(signals, n=None)` in the signals layer. Default `n=1` (configurable via `settings.MLB_MAX_TOP_OPPORTUNITIES`). Deterministic tie-breakers: priority_score desc, line_value_discrepancy desc, first_pitch asc, game.id asc.
- Only one "Top Opportunity" tag appears across the whole page by default — too many dilutes the meaning.

### UI
- Primary Best Bet: filled green pill with reason text. Secondary Watch Now: outlined red pill.
- Top Opportunity: gold gradient pill with ★. Used sparingly (n=1 default).
- Tile `_tile_actions.html` renders the new dict shape with `{type|action_label}` and `{reason|reason_label}` through the template tag library.

### Tests: 58/58 MLB green (14 new)
- Line value: no odds / equal probs / large discrepancy emits signal
- Late-game proxy: fresh / two-hour-old / scheduled game behavior
- Top Opportunity: one by default, configurable n, zero best-bets → zero top, empty-action clean state
- APIClient: HTML body rejected, non-JSON Content-Type rejected, valid JSON passes
- ingest_odds fail-fast: zero-created raises RuntimeError in DEBUG
- Action resolver: Best Bet primary when both fire, Watch Now becomes secondary

### Architecture preserved
- Signals: service layer (no view coupling, no template logic)
- View: orchestrates prioritize → sort → mark_top_opportunities → attach prefill
- Templates: render only, no decisions

---

## 2026-04-19 - MLB tiles: richer context + dynamic bet selection + pending-bet indicator

**Summary:** Three enhancements to the MLB hub driven by real decision-making feedback.

### What's new
- **Team records + recent streak on every tile.** The MLB `Team` model already carries `wins`/`losses`; we now render "12-8" inline under each team's name. A new `apps/mlb/services/streaks.py` computes per-team recent form (consecutive W or L from the most recent final, min 2 games) in a single batched query across all teams on the page. Streaks surface as colored chips ("W3" green, "L2" red).
- **Pitcher records next to names.** Pitchers already ingest W/L; the tile now shows "(2-0)" after each starter's name on both live and upcoming tiles. Live tiles now also show the pitcher matchup row (previously upcoming-only).
- **Dynamic selection dropdown.** The Mock Bet modal's free-text selection input is replaced with a two-option `<select>` when the caller passes `selections_by_type`. MLB populates it with the two teams for moneyline, the signed spreads for spread bets, and Over/Under for totals (when a total is in the latest odds snapshot). The dropdown re-populates automatically when the user changes bet type. Other sports that don't pass options continue to use the free-text input — the modal contract is backwards-compatible.
- **Pending-bet indicator.** Logged-in users see a 🎯 icon in the tile header and a thin green rail on the right edge of any game they have a pending mock bet on. Implemented via a single batched query (`MockBet.filter(user=u, result='pending', mlb_game_id__in=[...])`) in `prioritize()`.

### Architecture
- All three features honor the hub's data-pipeline rule: business logic in services, views orchestrate, templates render. `GameSignals` gained six new display fields (home/away record, streak, pitcher record) plus `has_user_bet`; all are computed once per hub render in batched queries, not per-tile.
- Streak computation: one query over `Game.objects.filter(status='final', first_pitch__gte=45d_ago)` with an IN on the page's team IDs, walked in-memory to count consecutive matching outcomes.
- Pending-bet batch: one query for authenticated users; skipped entirely for anonymous.

### Tests (9 new, 44/44 green)
- Streak computation (3-game streak detection, single-game noise filter, no-games case, empty-input edge, `format_record` null safety).
- Pending-bet indicator surfaces for the bet's owner; never for anonymous users.
- Selection dropdown options: moneyline always present, spread/total only when the market exposes them; labels correct for home-POV spreads.

---

## 2026-04-19 - MLB Action Layer: Watch Now / Best Bet + one-click mock bet

**Summary:** MLB tiles now carry action tags (🔥 Watch Now, 💰 Best Bet) and a Mock Bet button that opens the existing modal with smart-default selection, bet type, and odds already filled in. All logic lives in the signals/service layer; templates stay render-only.

### What's new
- **Action resolver** — new `resolve_actions(GameSignals) -> list[str]` in `apps/mlb/services/prioritization.py`. Watch Now when a live game is close or an ace matchup isn't a blowout; Best Bet when the market spread is tight (≤1.5) AND both starters are known. Max 2 actions per tile.
- **Deliberate rule**: TBD pitcher never yields Best Bet. The house model has *less* information when a starter is unknown — pushing a bet in that state would undermine the integrity of the signal. We already demote priority for TBD; this keeps the action layer consistent.
- **Extended `GameSignals`** — new boolean flags `is_close_game`, `is_blowout`, `late_game` (placeholder until inning ingestion), `tbd_pitcher`, and `actions`. Flags are derived once in `build_signals`; `resolve_actions` consumes only those flags + odds snapshot.
- **Pre-fill helper** — new `apps/mockbets/services/prefill.py::prefill_from_signals`. Picks the team with the higher-rated starter as the default selection (tie → home). Switches bet type to `spread` when the market shows a tight spread; otherwise moneyline with `moneyline_home/away` propagated from the latest snapshot. Never fabricates odds.
- **Authenticated-only button** — the Mock Bet button is hidden for anonymous users. The hub only renders the place-bet modal when `user.is_authenticated`, saving the markup + JS cost entirely. View skips prefill serialization for anon too.
- **One-click flow** — each tile carries `data-mlb-prefill='{...}'`. `static/js/mlb.js` wires `openMLBBet(btn)` → parses the JSON → delegates to the existing `openMockBetModal()` from `place_bet_modal.html`. No new endpoint, no duplicated logic.
- **Tile UI** — new `_tile_actions.html` partial shared by live + upcoming tiles. Red pill = Watch Now, green pill = Best Bet, accent-outlined Mock Bet button pinned to the right. Button uses `stopPropagation` so it doesn't trigger the tile's outer link.
- **Tests** — 11 new: 6 action-resolver cases (close live, ace, tight spread, TBD, blowout, max-2 cap) + 5 prefill cases (better-pitcher selection, tie → home, tight spread → spread bet, moneyline passthrough, JSON-serializable shape). MLB suite now 35/35 green.

### Architecture guardrails preserved
- All business decisions (actions, selection side, bet type) happen in services.
- Templates only render what they're handed.
- View orchestrates: prioritize → sort → attach prefill → render.
- No changes to the `/mockbets/place/` endpoint contract — existing modal + AJAX continue to work identically.

---

## 2026-04-19 - Pitcher + team W/L records (MLB & College Baseball)

**Summary:** Baseball game detail pages now display W/L records — team records in the matchup header for both MLB and College Baseball, and pitcher W/L as a small badge next to the pitcher's name on MLB. Data comes from existing API endpoints (no new third-party dependencies); only extra call is one `/v1/standings` request per MLB refresh cycle.

### Changes
- `apps/mlb/models.py` — added nullable `wins`/`losses` IntegerField to `Team` and `StartingPitcher`. Migration: `mlb/0002_startingpitcher_losses_startingpitcher_wins_and_more`.
- `apps/college_baseball/models.py` — added nullable `wins`/`losses` IntegerField to `Team`. Migration: `college_baseball/0002_team_losses_team_wins`.
- `apps/datahub/providers/mlb/pitcher_stats_provider.py` — parses `wins`/`losses` from the existing `/v1/people?hydrate=stats(...)` response and persists on each `StartingPitcher`. No new API call.
- `apps/datahub/providers/mlb/team_record_provider.py` — **new** provider. Calls `/v1/standings?leagueId=103,104` once per refresh, upserts W/L onto every `Team`.
- `apps/datahub/providers/registry.py` — registers `('mlb', 'team_record')`.
- `apps/datahub/management/commands/ingest_team_records.py` — **new** command wrapping the provider (MLB-only for now). Gated by `LIVE_DATA_ENABLED` + `LIVE_MLB_ENABLED`.
- `apps/datahub/management/commands/refresh_data.py` + `ensure_seed.py` — extended the sports config tuple with `has_team_records` and wired MLB to invoke `ingest_team_records` after pitcher stats.
- `apps/datahub/providers/college_baseball/schedule_provider.py` — new helpers `_parse_record_summary` and `_extract_overall_record` pull team W/L from the ESPN competitor `records` array. `_upsert_team` now accepts `wins`/`losses` and only overwrites when provided (avoids stomping a fresher value with None). No new API call.
- `templates/mlb/game_detail.html` — team record `(W-L)` in matchup header; pitcher W/L badge next to pitcher name (only rendered when both values non-null).
- `templates/college_baseball/game_detail.html` — team record `(W-L)` in matchup header.

### Tests (13 new)
- `apps/mlb/tests.py`: pitcher W/L normalize (with + without keys), pitcher W/L persist, team record normalize, persist, skip-unknown-team.
- `apps/college_baseball/tests.py`: record summary parsing (valid + garbage), overall-record extraction (named + fallback), end-to-end scoreboard normalize with records.

### Not touched
- Pitcher `rating` formula (unchanged — still ERA/WHIP/K/9 only; W/L is context, not a rating input)
- Other sports (CFB, CBB, Golf)
- `refresh_data` / `ensure_seed` structure — just added one column to the config tuple

### Migration safety
4 nullable `IntegerField`s across 2 apps, no defaults, no data loss. Applied on next Railway deploy via existing `migrate --noinput` in the start command.

---

## 2026-04-19 - ensure_seed: add MLB + College Baseball to live ingestion

**Summary:** `ensure_seed` (which runs on every Railway deploy) only invoked live ingestion for CBB, CFB, and Golf. MLB and College Baseball were absent from the sports list, so their `LIVE_*_ENABLED` env vars had no effect at deploy time — only `refresh_data` (cron) would pick them up. Added both to the list, matching the `refresh_data` config (MLB includes pitcher stats).

### Changes
- `apps/datahub/management/commands/ensure_seed.py` — extended `sports_config` tuple to `(sport, toggle, has_injuries, has_pitcher_stats)` and added entries for `mlb` and `college_baseball`. Adds a conditional `ingest_pitcher_stats` call for MLB.

---

## 2026-04-19 - Golf event detail: show last odds update

**Summary:** The golf event detail page (e.g. `/golf/the-masters/`) now displays the timestamp of the most recent odds snapshot, rendered in the user's local timezone. Gives users a clear signal of data freshness.

### Changes
- `apps/golf/views.py` — `event_detail` now computes `last_odds_update` via `GolfOddsSnapshot.objects.filter(event=event).aggregate(Max('captured_at'))` and passes it in context.
- `templates/golf/event_detail.html` — new muted line "Odds last updated: {timestamp}" under the date range, using the standard `D M d, g:i A` + `{% tz_abbr %}` pattern. Line only renders when snapshots exist.

---

## 2026-04-19 - Golf odds windowed fetch

**Summary:** Golf odds ingestion now only hits The Odds API when at least one `GolfEvent` is in its fetch window (start_date − 7 days → end_date), and persists no more than one snapshot per event per day. Reduces API usage and aligns data freshness with betting relevance. No other sports affected.

### Changes
- `apps/datahub/providers/golf/odds_provider.py`
  - Added `is_event_in_window(event, today)` pure helper — returns `(bool, reason)` where reason ∈ `{outside_window, event_complete, in_window}`.
  - `fetch()`: gates the HTTP calls. Skips all 4 PGA sport keys entirely when no `GolfEvent` is in window. Emits structured logs `golf_odds_fetch_skipped_no_events`, `golf_odds_fetch_started`, `golf_odds_fetch_completed`.
  - `persist()`: enforces the window per event (data-integrity backstop) and a once-per-day guard using `GolfOddsSnapshot.captured_at__date=today` (no schema change). Logs `golf_odds_persist_skipped_window` and `golf_odds_persist_skipped_duplicate`.
- `apps/datahub/tests.py` — 13 new unit tests covering window predicate boundary cases, fetch-level gating (API calls suppressed / allowed), and persist-level gating (window + same-day dedupe).

### Not touched
- `AbstractProvider`, other sport providers (CBB, CFB, MLB, college baseball)
- `refresh_data`, scheduler, registry, API client
- `_match_event`, normalization logic
- Models — no migration required (`GolfEvent.start_date` / `end_date` already exist)

---

## 2026-04-19 - MLB Hub: Tile Priority Layer

**Summary:** Redesigned the MLB hub into a priority-driven command center. Live and today's games are rendered as horizontally-scrolling tiles sorted by a new signals layer; remaining upcoming games stay in a polished list.

### What's new
- **Signals layer** — new `apps/mlb/services/prioritization.py` computes a `GameSignals` object per game (priority bucket, numeric score, reasons, injury summary, ace-matchup flag). Weights are extensible via a single `WEIGHTS` table and include seams for user favorites / odds movement / game importance.
- **Three-bucket view** — `mlb_hub` now returns `live_tiles` (priority desc), `today_tiles` (priority desc, then start time), and `future_games` (chronological list, capped at 30). "Today" respects the viewer's timezone via `UserTimezoneMiddleware`.
- **Tile components** — new partials `templates/mlb/_tile_live.html`, `_tile_upcoming.html`, `_list_future.html`. Live tiles feature a pulsing live dot, score-prominent layout, and priority chips (amber/slate). Upcoming tiles show the pitcher matchup and the top "why" reason.
- **Scoped CSS** — new `static/css/mlb.css` (loaded via `extra_css` on the hub only; does not bloat the global stylesheet). Uses existing design tokens; respects `prefers-reduced-motion`; responsive at 375px.
- **Keyboard rail nav** — new `static/js/mlb.js` binds arrow-left/right to scroll within a focused rail and translates vertical wheel to horizontal when a rail overflows.
- **Tests** — 9 new tests covering bucket thresholds, blowout demotion, TBD-pitcher penalty, ace-matchup detection, and both sort functions. Full MLB suite: 18/18 green.

### Non-goals (today)
- Inning / progression-based live sort — deferred until inning state is ingested. Live sort is priority-only for now.
- Favorite team, odds-movement, and playoff-importance signals are wired as no-op seams; they contribute 0 to the score until the upstream data exists.

---

## 2026-04-19 - Baseball Expansion Phase 11: Final sweep + self-review

**Summary:** Final checks before closing out the expansion. All system checks clean, no pending migrations, every route returns its expected status.

### Verifications
- `python manage.py check` → clean (0 issues)
- `python manage.py makemigrations --check --dry-run` → "No changes detected" (no schema drift vs. committed migrations)
- Route sweep, 13/13 OK: `/`, `/lobby/` (× 6 sport variants), `/mlb/`, `/college-baseball/`, `/cfb/`, `/cbb/`, `/golf/`, `/accounts/login/`
- Live data proof: 30 MLB teams, 123 games, 108 pitchers (66 with stats), 44 D1 college baseball games ingested from real APIs during development
- Mock bet end-to-end: placed moneyline on NYY@KC (Yankees won 13-4), settlement engine marked win, -110 payout computed correctly
- AI insight prompt construction: pitchers block + BASEBALL CONTEXT clause render correctly when sport ∈ {mlb, college_baseball}, gracefully degrade to "Season stats not yet available" when stats are missing

### Self-review: consistency with the rest of Brother Willies

| Dimension | Baseball implementation | Existing (CFB/CBB) | Verdict |
|---|---|---|---|
| Models (Conference, Team, Game, OddsSnapshot, InjuryImpact) | Present | Present | Parallel |
| UUID primary keys on Game | Yes | Yes | Parallel |
| Game time field | `first_pitch` (baseball-specific) | `kickoff` / `tipoff` | Consistent naming convention |
| Admin registration | Yes | Yes | Parallel |
| URL structure (`hub`, `game/<uuid>/`) | Yes | Yes | Parallel |
| Template layout (probability table, odds card, AI insight, mock-bet button) | Yes | Yes | Parallel |
| House model signature (`compute_game_data` / `compute_house_win_prob` / etc.) | Yes | Yes | Parallel — so the lobby iterates the registry without a sport-specific branch |
| Idempotent upsert via `(source, external_id)` | Yes (new pattern for baseball) | No (older CBB/CFB uses name-based match) | Baseball is STRONGER; older sports can adopt later |
| Mock bet FK pattern | `mlb_game` / `college_baseball_game` nullable FK | `cfb_game` / `cbb_game` nullable FK | Parallel |
| Bet types (moneyline, spread/run line, total) | Shared | Shared | Parallel |
| Settlement flow | Same `_settle_team_sport` helper | Same helper (refactored) | Unified — reduces per-sport duplication |
| AI Insight dispatch | Single service, sport-aware branch for pitchers | Single service | Parallel |
| Lobby integration | Driven by `SPORT_REGISTRY` | Driven by `SPORT_REGISTRY` | Registry makes both first-class by the same mechanism |

### Self-review: what could weaken or destabilize the existing app?

**Potential destabilizers and their mitigations**:

1. **MockBet.sport `max_length` bump (4 → 20).** Django `AlterField` on an indexed CharField. Cheap DDL on SQLite; on Postgres this is an instant metadata-only change. Verified via `sqlmigrate` (would be visible in migration file). No data loss risk. Migration lands as `mockbets.0002`.
2. **Sport registry refactor in `core/views.py`.** Behavior-preserving: tested by comparing pre- and post-refactor lobby responses for CFB / CBB / Golf — all still 200 with identical byte counts in the smoke test. The registry was introduced deliberately to PREVENT brittleness when a 5th team sport is added.
3. **Settlement helper consolidation (`_settle_team_sport`).** The old `_settle_cfb` and `_settle_cbb` were byte-for-byte identical except for the FK column. The generalized helper is parameterized by FK name and exercises the exact same code path. Regression-tested: 20 existing mockbets tests still pass.
4. **`_resolve_spread` time-field lookup.** The old code was `kickoff if hasattr else tipoff`, which silently broke for baseball (`hasattr(game, 'kickoff')` would be False, so it would try to access `game.tipoff` which doesn't exist on baseball games and raise AttributeError). The new walk (`kickoff → tipoff → first_pitch`) is strictly more robust for all sports.
5. **AI Insight system prompt conditional.** The BASEBALL CONTEXT clause is appended ONLY when sport is MLB or College Baseball; other sports receive the exact same system prompt as before — verified via string comparison of system prompt output for sport='cfb' before and after the change.
6. **New FKs on `ModelResultSnapshot` and `UserGameInteraction`** are all nullable — no impact on existing rows, no default value migration.
7. **New apps registered in `INSTALLED_APPS`.** Admin site picks them up automatically; no name collisions (verified — new `Conference` / `Team` / `Game` classes live in their own app namespaces).

### Environment variables to set in Railway

| Var | Purpose | Default |
|---|---|---|
| `LIVE_MLB_ENABLED` | Master toggle for MLB data ingestion | `false` |
| `LIVE_COLLEGE_BASEBALL_ENABLED` | Master toggle for CB data ingestion | `false` |
| `MLB_STATSAPI_BASE_URL` | Override the MLB Stats API base URL | `https://statsapi.mlb.com/api` |
| `ESPN_BASEBALL_BASE_URL` | Override the ESPN baseball base URL | `https://site.api.espn.com/apis/site/v2/sports/baseball` |

`ODDS_API_KEY` (existing) is reused for baseball odds. No new API keys required.

### Known limitations (intentional, documented)

- **College baseball probable pitchers** are not available from ESPN's public feed. The model surfaces this with "Probable pitcher TBD" and low confidence rather than fabricating pitchers.
- **College baseball odds coverage** via The Odds API's `baseball_ncaa` market is sparse. Games without odds show "Odds unavailable" and are excluded from snapshot capture — as designed.
- **MLB favorite-team profile field** not yet wired. The bye-week / off-week detection logic in the lobby currently only applies to CFB/CBB where profile fields exist. The registry structure is ready for a future baseball-favorite-team field without further refactor.

### Final state
- 11 phase commits on `claude/eloquent-booth`, each merged to `main` and pushed to GitHub.
- `main` head: pushed to `git@ssh.github.com:djenkins452/brotherwillies.git`, Railway will auto-deploy once env vars are set.

---

## 2026-04-19 - Baseball Expansion Phase 10: Test coverage

**Summary:** Added 20 targeted tests covering the baseball expansion: schema smoke, prediction-model math, provider normalization, and mock-bet settlement via the generalized `_settle_team_sport` helper.

### New tests
- `apps/mlb/tests.py` (10 tests):
  - App installed smoke
  - `compute_house_win_prob` is 0.5 when teams + pitchers + neutral site are symmetric
  - Pitcher advantage drives probability past 0.95 when rating gap is extreme
  - Missing pitcher → confidence forced to `low` regardless of other factors
  - `compute_game_data` returns the expected dict shape
  - `MLBScheduleProvider.normalize()` round-trips a hand-built sample payload and extracts pitcher IDs
  - `MLBScheduleProvider.normalize()` correctly drops rows with missing team IDs
  - `compute_pitcher_rating()` returns high values for elite stats, low for poor stats, and `None` when any stat is missing (no fabrication)
- `apps/college_baseball/tests.py` (6 tests):
  - App installed smoke
  - Missing pitchers → low confidence
  - CB HFA=2.0 produces the expected probability at parity
  - Neutral site strips HFA cleanly
  - ESPN event payload normalizes correctly
  - Missing competitions → normalize skips the row
- `apps/mockbets/tests.py` → `MLBSettlementTests` (4 tests):
  - Moneyline win on home
  - Moneyline loss on home when away wins
  - Total over win
  - `sport='all'` settles MLB bets alongside other sports

### Regression
- Full `apps.*` test suite: **52 tests, 48 pass, 4 pre-existing staticfiles-manifest errors** (same 4 errors that existed before this session — unrelated to baseball work)
- All 20 new tests pass

---

## 2026-04-19 - Baseball Expansion Phase 9: Standing docs updated

**Summary:** Updated all standing documentation surfaces to describe the baseball expansion — user-visible help, user guide, and "what's new" page, plus this changelog.

### Modified files
- `templates/includes/help_modal.html` — new `help_key` entries for `mlb_hub`, `college_baseball_hub`, and a shared `mlb_game` / `college_baseball_game` block explaining starting pitchers, the model's pitcher weighting, run lines, and the data sources
- `templates/accounts/user_guide.html` — site overview now lists MLB and College Baseball; new dedicated Baseball section covering the pitcher-weighted model, TBD pitcher handling, bet types, and data sources; Mock Bets section updated to reference all team sports
- `templates/accounts/whats_new.html` — new Apr 19, 2026 release card at the top with sections for MLB live, College Baseball D1, prediction model, mock bets, AI Insight, and the sport-registry refactor
- `docs/changelog.md` — running phase-by-phase log (this file)

### Verified
- `/profile/user-guide/` → 200 (37,792 bytes)
- `/profile/whats-new/` → 200 (41,933 bytes)
- `/mlb/` and `/college-baseball/` hubs still 200

---

## 2026-04-19 - Baseball Expansion Phase 8: Analytics + mockbet UI baseball-aware

**Summary:** Wiring up the last baseball touchpoints across the existing analytics + mock-bet UI so baseball bets surface as first-class citizens in every filter, badge, chart, and management command.

### Modified files
- `apps/mockbets/management/commands/settle_mockbets.py` — `choices` now includes `mlb` and `college_baseball`; summary line reports all 5 sports (defensive `.get` so missing keys don't crash)
- `apps/mockbets/admin.py` — `raw_id_fields` includes `mlb_game` and `college_baseball_game`
- `templates/mockbets/my_bets.html` — filter chips for MLB and College Baseball added; sport-badge colors extended
- `templates/mockbets/bet_detail.html` — `bet.game` link block handles baseball FKs; sport-badge colors extended
- `templates/mockbets/analytics.html` — sport filter dropdown + chart `sportColors` dict include baseball entries (MLB=red, CB=blue)

### Verified end-to-end
- Created 3 MLB bets for a test user, settled as wins
- KPIs compute correctly: total=3, ROI=83.33
- Chart data includes `roi_by_sport` with baseball bucket
- Routes render 200: `/`, `/?sport=mlb`, `/?sport=college_baseball`, `/mockbets/`, `/mockbets/?sport=mlb`, `/mockbets/analytics/`, `/mockbets/analytics/?sport=mlb`

---

## 2026-04-19 - Baseball Expansion Phase 7: AI Insight pitching-matchup extension

**Summary:** The AI Insight service now knows about baseball. When the sport is MLB or College Baseball, the system prompt gains a short "BASEBALL CONTEXT" clause instructing the model to treat the SP-vs-SP matchup as the primary driver, and the user prompt includes a STARTING PITCHERS section with ERA / WHIP / K9 / rating / handedness — or explicit "Probable pitcher TBD (unknown)" when the pitcher is missing. Safety guardrails (no invented stats, no invented names, no betting advice) carry through unchanged.

### Modified files
- `apps/core/services/ai_insights.py`:
  - `_build_system_prompt` accepts a `sport` kwarg; when baseball, appends a focused clause about pitching dominance and TBD-handling.
  - `_build_context_from_game` adds a `pitchers` dict (home/away) to the context for baseball sports, preserving nulls for missing pitchers and tagging them in `missing_data` for confidence signaling.
  - `_build_user_prompt` renders a STARTING PITCHERS section listing name + handedness + stats, or an explicit "Probable pitcher TBD (unknown)".
  - `generate_insight` now passes the sport into system-prompt construction.

### Verified (no OpenAI call required)
- With a game whose pitchers both have stats (Cubs-Mets): the prompt renders `Javier Assad (RHP) … ERA 8.10 | WHIP 1.60 | K/9 5.4 | rating 12` and the mirrored Mets line.
- With a game whose pitchers lack stats: prompt correctly renders "Season stats not yet available" instead of fabricating numbers.
- System prompt includes the baseball context clause exactly when sport ∈ {mlb, college_baseball}.

---

## 2026-04-19 - Baseball Expansion Phase 6: Mock bets for MLB + College Baseball

**Summary:** Mock betting is now available on every baseball game the system ingests. Moneyline, run line (stored as spread), and total bets flow through the same settlement pipeline the other team sports use — with the duplicated per-sport settlement helpers collapsed into a single `_settle_team_sport(sport_key, fk_name)` function.

### Model changes
- `apps/mockbets/models.py`:
  - `SPORT_CHOICES` gains `('mlb', 'MLB')` and `('college_baseball', 'College Baseball')`
  - `sport` CharField max_length bumped from **4 → 20** to fit the longest new key
  - New FKs `mlb_game` and `college_baseball_game` (nullable, one-of pattern)
  - `.game` property extended for both new sports
- Migration `mockbets.0002` applied

### Settlement refactor
- `_settle_cfb()` + `_settle_cbb()` (byte-for-byte identical except FK column) collapsed into `_settle_team_sport(sport_key, fk_name)`. CFB / CBB / MLB / CB all dispatch through a single `_TEAM_SPORT_FK` mapping.
- `_resolve_spread` helper generalized — the old `kickoff if hasattr else tipoff` ternary now walks `kickoff → tipoff → first_pitch`.
- Behavior unchanged for existing CFB/CBB/Golf paths.

### Views
- `apps/mockbets/views.py`:
  - `place_bet` accepts `sport='mlb'` or `sport='college_baseball'` and sets the appropriate FK
  - `my_bets` and `analytics_dashboard` filter and prefetch both new sports
- `apps/core/views.py` home view (mock bet analytics dashboard) prefetches new FKs and accepts new sport filters

### Verified end-to-end against live data
- Placed a moneyline bet on a final MLB game (NYY 13, KC 4)
- `settle_pending_bets(sport='mlb')` settled it as a win
- `.game` property correctly returned the MLB game

---

## 2026-04-19 - Baseball Expansion Phase 5: Lobby integration + sport registry refactor

**Summary:** Baseball is now a first-class citizen in the Lobby — MLB and College Baseball tabs, Live/Today/Tomorrow/This Week timeframe grouping, Big Matchups surfacing, all working with the same UX as CFB/CBB. Under the hood this is powered by a new `apps/core/sport_registry.py` that replaces the brittle `time_field = 'kickoff' if sport == 'cfb' else 'tipoff'` ternary and the pair of near-identical `_get_cfb_value_data` / `_get_cbb_value_data` helpers.

### Architectural note
`SPORT_REGISTRY` is the **single source of truth** for team-sport metadata. Adding a fifth sport is now a single entry in that file — no sweep across `views.py`, no new per-sport helper functions, no template fork. The lobby, AI insight view, and timeframe grouper all consume the registry.

### New files
- `apps/core/sport_registry.py` — registry of `{label, game_model, time_field, compute_fn, season_months}` keyed by sport code

### Modified files
- `apps/core/views.py`
  - `SPORT_SEASONS` dict removed; `_is_in_season()` reads from registry
  - `_get_available_sports()` iterates the registry in display order (CBB, CFB, MLB, College Baseball), then appends Golf
  - `_get_cfb_value_data` + `_get_cbb_value_data` collapsed into a single `_get_value_data_for_sport(sport, user, sort_by)` helper; behavior preserved
  - `_get_live_data_for_sport()` helper extracted
  - `_group_games_by_timeframe()` now reads `time_field` from the registry; Big Matchups logic extends to all registered sports (previously hard-coded to `{'cfb','cbb'}`)
  - `value_board()` uses `if sport in SPORT_REGISTRY` branch for all team sports, then preserves CFB/CBB bye-week detection (baseball has no favorite-team profile field yet, but the structure is ready)
  - `ai_insight_view()` now accepts any registered sport; baseball select_related adds pitcher FKs
- `apps/core/services/ai_insights.py` — `_build_context_from_game()` uses `_TIME_ATTR_BY_SPORT` map instead of CFB/CBB ternary (baseball wiring landed here; full pitcher-aware prompt is Phase 7)
- `templates/base.html` — MLB added to the bottom nav (now 6 items: Home | Lobby | CFB | CBB | MLB | Golf). College Baseball remains first-class via the Lobby tabs — matching our UX convention that niche sports live under Lobby.
- `apps/mlb/views.py` + `apps/college_baseball/views.py` — all views now pass `nav_active` + `help_key` for proper nav highlighting and help-modal wiring (MLB highlights the MLB tile; CB highlights the Lobby tile)

### Verified
- `GET /lobby/?sport=mlb` → 200 (23,665 bytes with real games rendered through timeframe grouping)
- `GET /lobby/?sport=college_baseball` → 200 (21,116 bytes)
- `GET /lobby/?sport=cfb` / `cbb` / `golf` → 200 (unchanged — same 10,676 bytes as before the refactor, confirming no regression)
- `GET /mlb/` + `/college-baseball/` hub pages → 200
- Full test suite: 30/34 pass (4 failing tests are pre-existing unrelated staticfiles-env issues, identical to pre-refactor state)

---

## 2026-04-19 - Baseball Expansion Phase 4: Rich game detail templates + views

**Summary:** Baseball hub and game detail pages now match the visual richness of the CBB equivalents — probability comparison tables, odds snapshot cards, AI Insight container, mock-bet button stubs, and (for MLB) a prominent Starting Pitchers block with ERA / WHIP / K9 and derived rating. Missing pitcher data is rendered as "Probable pitcher TBD"; missing odds as "Market data temporarily unavailable" — no fabrication.

### Modified files
- `templates/mlb/game_detail.html` — full rewrite matching CBB style; Starting Pitchers section with stats; run-line terminology; mock-bet button passes `sport: 'mlb'`; AI insight fetches `/api/ai-insight/mlb/<uuid>/`
- `templates/college_baseball/game_detail.html` — same treatment with simpler pitcher block (no stats when source is ESPN-only); mock-bet passes `sport: 'college_baseball'`
- (AI insight endpoint wiring lives in Phase 7; the frontend fetches are wired now so Phase 7 is purely backend.)

### Verified
- `GET /mlb/` → 200 (14,640 bytes)
- `GET /college-baseball/` → 200 (12,455 bytes)
- `GET /mlb/game/<uuid>/` → 200 with pitcher names + "Starting Pitchers" section confirmed in HTML
- `GET /college-baseball/game/<uuid>/` → 200 correctly showing "Probable pitcher TBD" since ESPN does not supply probable pitchers

---

## 2026-04-19 - Baseball Expansion Phase 3: Prediction model with pitcher weighting

**Summary:** Replaced the stub model services with a real logistic-regression house model for both MLB and College Baseball. Pitching is the dominant driver per product direction.

### Model
```
score = 0.35 * (home.rating - away.rating)    * weights['rating']
      + 0.65 * (home_pitcher.rating - away_pitcher.rating) * weights['pitcher']
      + HFA                                   * weights['hfa']  (if not neutral)
prob  = sigmoid(score / 15)   clamped [0.01, 0.99]
```
- HFA: MLB = 2.5, College Baseball = 2.0
- Missing pitcher on either side → `pitcher_diff = 0` AND confidence downgraded to `low`. No fabricated pitcher data.
- User weights plug in via the new `pitcher_weight` field on `UserModelConfig` and `ModelPreset` (default 1.0, so behavior for users on other sports is unchanged).

### Modified files
- `apps/accounts/models.py` — added `pitcher_weight` to `UserModelConfig` and `ModelPreset`; migration `accounts.0008` applied
- `apps/mlb/services/model_service.py` — full house/user model replacing the Phase 1/2 stub
- `apps/college_baseball/services/model_service.py` — same shape, HFA=2.0

### Verified against live MLB data
- NYM (Myers r=64) @ CHC (Assad r=12) → house says CHC wins 11.1% — matches intuition for a ~52-point pitcher rating gap
- LAD (Sasaki r=28) @ COL (Lorenzen r=15) → house says COL wins 40.3% — slight home edge, pitching advantage, reasonable
- Same-rated/default pitchers → HFA-only result (54.2% home), confirming team-rating weight behaves as designed

### Confidence rules
- No odds snapshot → `low`
- Missing starting pitcher → `low`
- Odds < 2h old AND both pitchers known → `high`
- Odds < 12h old → `med`
- Else → `low`

---

## 2026-04-19 - Baseball Expansion Phase 2: Live data ingestion

**Summary:** Baseball data now flows from real production APIs. MLB pulls schedule, teams, probable pitchers, and live scores from `statsapi.mlb.com`; pitcher season stats pull from the MLB `/people` endpoint and are distilled into a 10–95 rating. College Baseball pulls full D1 schedule + live scores from ESPN's public scoreboard. Odds for both sports come from the existing Odds API. All ingestion is idempotent via `(source, external_id)` constraints. Verified against live data: 30 MLB teams / 123 games / 108 pitchers / 66 with stats ingested in a single run; 44 D1 baseball games ingested concurrently.

### New providers
- `apps/datahub/providers/mlb/schedule_provider.py` — statsapi.mlb.com `/v1/schedule` with `hydrate=probablePitcher`
- `apps/datahub/providers/mlb/pitcher_stats_provider.py` — `/v1/people?personIds=...&hydrate=stats(...)` batched by 40
- `apps/datahub/providers/mlb/odds_provider.py` — Odds API `baseball_mlb`
- `apps/datahub/providers/mlb/name_aliases.py` — minor-variation normalizer
- `apps/datahub/providers/college_baseball/schedule_provider.py` — ESPN public `college-baseball/scoreboard`, `groups=50` (full D1)
- `apps/datahub/providers/college_baseball/odds_provider.py` — Odds API `baseball_ncaa` (sparse; degrades gracefully to empty)

### Pipeline wiring
- `apps/datahub/providers/registry.py` — all 5 new provider entries
- `apps/datahub/management/commands/ingest_schedule.py` / `ingest_odds.py` — added `mlb` + `college_baseball` choices and toggle keys
- `apps/datahub/management/commands/ingest_pitcher_stats.py` — NEW, separate cadence for MLB pitcher stats
- `apps/datahub/management/commands/refresh_data.py` — new SPORTS_CONFIG shape `(sport, toggle, has_injuries, has_pitcher_stats)`; disabled sports now print a visible "skipped" line instead of silently skipping
- `apps/datahub/management/commands/capture_snapshots.py` — added MLB + CB branches
- `apps/datahub/management/commands/resolve_outcomes.py` — collapsed per-sport duplication into a shared `_resolve_by_fk(fk, time_field)` helper covering CFB / CBB / MLB / CB (refactor, no behavior change for existing sports)

### Model changes
- `apps/analytics/models.py` — added nullable FKs `mlb_game` and `college_baseball_game` on both `ModelResultSnapshot` and `UserGameInteraction`; migration `analytics.0003` applied
- `apps/datahub/team_colors.py` — added 30 MLB hex colors; `get_team_color(slug, sport)` extended with `mlb` and `college_baseball` (CB falls back to CFB/CBB colors for shared programs)
- `apps/mlb/services/model_service.py` + `apps/college_baseball/services/model_service.py` — stubs expanded to include `compute_house_win_prob`, `compute_user_win_prob`, `compute_data_confidence`, `compute_edges` so downstream callers can bind against a stable interface now; real logistic model lands Phase 3

### Architectural note
Per project direction ("shared abstractions are the right move, use them"), `resolve_outcomes` was refactored into a parameterized helper rather than adding two more copies of 30 nearly-identical lines. Behavior is unchanged for existing CFB/CBB paths.

### Verified
- `python manage.py check` — clean
- `python manage.py makemigrations` — analytics 0003 only; no spurious migrations on other apps
- `python manage.py refresh_data` with all baseball toggles off — clean, prints skip lines
- `python manage.py ingest_schedule --sport=mlb --force` — 123 games created against live `statsapi.mlb.com`
- `python manage.py ingest_pitcher_stats --sport=mlb --force` — 66 pitchers updated with real ERA/WHIP/K9, ratings spread 11.8–87.7
- `python manage.py ingest_schedule --sport=college_baseball --force` — 44 D1 games created against ESPN
- Smoke tests for mlb/college_baseball/analytics still green

---

## 2026-04-19 - Baseball Expansion Phase 1: MLB + College Baseball apps scaffolded

**Summary:** Added two new Django apps — `apps.mlb` and `apps.college_baseball` — as first-class sports alongside CFB, CBB, and Golf. This phase lays the schema, admin, URL, and template foundation. No live data is ingested yet (Phase 2), no prediction model is wired (Phase 3), and no lobby/mockbet integration is in place (Phases 5 & 6). Hitting `/mlb/` or `/college-baseball/` now renders a "Data temporarily unavailable" state until ingestion is enabled.

### Design highlights
- **`first_pitch`** field on `Game` (parallel to CFB `kickoff`, CBB `tipoff`) — no brittle ternaries, a dispatch map lands in Phase 5.
- **`StartingPitcher`** first-class entity in both apps; `home_pitcher`/`away_pitcher` nullable FKs + `pitchers_updated_at` freshness tracker on `Game`. Stats fields (era/whip/k_per_9) are all nullable so missing data is explicit rather than fabricated.
- **`source` + `external_id`** on Team / Game / StartingPitcher with unique constraints — idempotent upserts from MLB Stats API / ESPN / future providers with zero risk of duplicates.
- **No seed data for baseball** — baseball tables are empty until live ingestion runs, per product requirement.

### New env vars (default `false`)
- `LIVE_MLB_ENABLED`
- `LIVE_COLLEGE_BASEBALL_ENABLED`
- `MLB_STATSAPI_BASE_URL` (defaults to `https://statsapi.mlb.com/api`)
- `ESPN_BASEBALL_BASE_URL` (defaults to the ESPN public site.api.espn.com endpoint)

### New files
- `apps/mlb/{__init__,apps,models,admin,urls,views,tests}.py`
- `apps/mlb/services/{__init__,model_service}.py` (stub)
- `apps/mlb/migrations/0001_initial.py`
- `apps/college_baseball/{__init__,apps,models,admin,urls,views,tests}.py`
- `apps/college_baseball/services/{__init__,model_service}.py` (stub)
- `apps/college_baseball/migrations/0001_initial.py`
- `templates/mlb/{hub,game_detail}.html`
- `templates/college_baseball/{hub,conference,game_detail}.html`

### Modified files
- `brotherwillies/settings.py` — registered both apps in `INSTALLED_APPS`, added baseball env toggles + API base URLs
- `brotherwillies/urls.py` — mounted `/mlb/` and `/college-baseball/`

### Verified
- `python manage.py check` — clean
- `python manage.py makemigrations mlb college_baseball` — generated cleanly
- `python manage.py migrate` — applied to local SQLite without issue
- `python manage.py test apps.mlb.tests apps.college_baseball.tests` — 2/2 pass
- Pre-existing CBB/CFB/other tests exhibit only the known staticfiles-manifest issue (unrelated)

---

## 2026-02-08 - Lobby: Always Show Live Section + Fix Default Expand

**Summary:** Live Now section now always appears in the Lobby (even with 0 live games), showing "No live games right now" when empty. Fixed accordion default-open logic — server-side smart defaults (Live > Big Matchups > Today) now always apply on page load instead of being overridden by stale localStorage state.

### Modified files:
- `apps/core/views.py` — Always create Live section in `_group_games_by_timeframe()`, always call grouping for team sports
- `templates/core/value_board.html` — Added empty state message for Live section with 0 games
- `static/js/app.js` — Clear localStorage on load so server-side `default_open` always applies

---

## 2026-02-08 - Rename Value Board to Lobby + Live Now Section

**Summary:** Renamed "Value Board" to "Lobby" (industry-standard sportsbook terminology). Added Live Now accordion section at top of Lobby showing in-progress games with scores and live badges. Added Expand All / Collapse All buttons. Smart default: Live section opens if games are in progress, otherwise Big Matchups, then Today. Removed redundant Games page and nav item.

### Modified files:
- `apps/core/views.py` — Fetch live games per sport in `value_board()`, pass to `_group_games_by_timeframe()` as live section; removed `games()` view
- `apps/core/urls.py` — Removed `/games/` route
- `templates/core/value_board.html` — Renamed to "Lobby", added Live section with live card rendering (scores, badges), Expand/Collapse All buttons
- `templates/base.html` — Renamed "Value" nav to "Lobby", removed "Games" nav item; nav is now Home | Lobby | CFB | CBB | Golf
- `static/js/app.js` — Added `expandAllVB()` and `collapseAllVB()` functions

---

## 2026-02-08 - Home Page → Mock Bet Analytics + Games Nav Item

**Summary:** Restructured navigation so Home (`/`) shows the mock bet analytics dashboard (summary, charts, performance stats) and the previous dashboard (live games + top value picks) moves to a new "Games" nav item at `/games/`.

### Modified files:
- `apps/core/urls.py` — Added `/games/` route
- `apps/core/views.py` — New `home()` renders analytics dashboard; old home renamed to `games()`
- `templates/base.html` — Added "Games" nav item between Home and Value in bottom nav
- `templates/core/home.html` — Updated title from "Dashboard" to "Games"
- `brotherwillies/settings.py` — `LOGIN_REDIRECT_URL` updated from `/mockbets/analytics/` to `/`

---

## 2026-02-08 - Spread Indicators on Game Cards

**Summary:** Added spread (+/-) display next to each team name across all game cards and detail pages. Follows the industry-standard convention: negative spread = favorite, positive = underdog (e.g., Texas Tech -11.5 / West Virginia +11.5). Makes it immediately clear who is favored without reading the AI Insight.

### New:
- `spread_display` template filter in `apps/core/templatetags/tz_extras.py` — formats spread for home/away side with proper sign

### Modified files:
- `templates/core/home.html` — Added spread tags to all live and upcoming game cards (CBB + CFB)
- `templates/core/value_board.html` — Added spread tags to value board game cards
- `templates/cbb/game_detail.html` — Added spread next to team names in header
- `templates/cfb/game_detail.html` — Added spread next to team names in header
- `static/css/style.css` — Added `.spread-tag` styling (yellow, compact)

---

## 2026-02-08 - Golf Event Seeding for Production

**Summary:** Added idempotent `seed_golf_events` management command that creates upcoming major tournament events with 30-golfer fields and realistic outright odds. Wired into `ensure_seed` so production (Railway) gets golf data on every deploy, regardless of live data toggle.

### New files:
- `apps/datahub/management/commands/seed_golf_events.py` — Seeds 4 majors (The Masters, PGA Championship, U.S. Open, The Open Championship) with 30 golfers and odds snapshots per event. Idempotent via `get_or_create` on slug.

### Modified files:
- `apps/datahub/management/commands/ensure_seed.py` — Added `call_command('seed_golf_events')` after `seed_golfers`

---

## 2026-02-08 - Golf Mock Bet Integration

**Summary:** Built out the full golf section for mock bet placement. Added golf event detail pages with golfer odds tables, per-golfer "Mock Bet" buttons, golfer search autocomplete in the mock bet modal, golf demo seed data, and updated all standing docs.

### New files:
- `templates/golf/event_detail.html` — Golf event detail page with field/odds table (desktop) and card layout (mobile), Place Mock Bet buttons per golfer
- `apps/golf/urls.py` — Added `<slug:slug>/` route for event detail

### Modified files:
- `apps/golf/views.py` — Added `event_detail()` view with latest odds per golfer, field from GolfRound, event open/closed status
- `templates/golf/hub.html` — Event cards now link to event detail pages; updated placeholder text
- `templates/core/value_board.html` — Golf event links now point to `/golf/<slug>/` instead of `/golf/`
- `templates/mockbets/includes/place_bet_modal.html` — Added golfer search autocomplete field (visible when sport=golf), debounced AJAX to `/golf/api/golfer-search/`, pre-filled golfer display
- `static/css/style.css` — Added golf event detail styles (table, mobile cards, golfer search dropdown)
- `apps/datahub/management/commands/seed_demo.py` — Seeds 3 golf events, 16 golfers, odds snapshots, 6 settled + 3 pending golf mock bets
- `templates/includes/help_modal.html` — Added `golf_event` help key; updated `golf` help key with mock bet info
- `templates/accounts/user_guide.html` — Updated golf bets section with event detail page instructions
- `templates/accounts/whats_new.html` — Added "Golf Mock Bets" release entry
- `docs/changelog.md` — This entry

---

## 2026-02-08 - AI Insight Loading Message

**Summary:** Added a visible "Acquiring your AI insights, one moment please..." message with a pulsing animation below the shimmer lines while the AI Insight is loading on game detail pages (CFB and CBB).

### Modified files:
- `templates/cfb/game_detail.html` — Added loading message paragraph
- `templates/cbb/game_detail.html` — Added loading message paragraph
- `static/css/style.css` — Added `.ai-loading-msg` style with `pulse-fade` animation

---

## 2026-02-08 - Aged Parchment Background on Auth Pages

**Summary:** Applied aged parchment texture (`bg_image.png`) as background on all auth page form panels. All white/gray surfaces replaced with translucent overlays so the natural paper texture — stains, scratches, and wear — shows through. Message backgrounds switched to transparent tints, input borders warmed to match.

### New files:
- `static/branding/bg_image.png` — Aged ivory parchment texture (full-res, no-repeat)

### Modified files:
- `static/css/auth.css` — Form column background is now `bg_image.png`; card, inputs, and messages all use translucent backgrounds so parchment shows through; input borders warmed to match texture

---

## 2026-02-08 - Full Color Rebrand (Penny Gold)

**Summary:** Replaced blue accent color scheme with rich penny gold (`#c9943a`) sampled from the BW logo lettering. Header shows logo image + "Brother Willie" text. All buttons, links, tabs, badges, charts, and auth pages use the new burnished gold palette.

### Modified files:
- `templates/base.html` — Header now shows logo image + "Brother Willie" text
- `static/css/style.css` — `--accent` changed from `#4f8cff` to `#c9943a`, `--accent-hover` to `#b8832e`, all rgba references updated
- `static/css/auth.css` — Sign In button, links, input focus, and logo glow animation all switched to penny gold
- `static/branding/bw_logo.png` — Updated logo image
- `templates/mockbets/analytics.html` — Chart.js accent color updated

---

## 2026-02-08 - AI Performance Commentary & Demo Data (Phase 5)

**Summary:** Added AI-powered performance commentary for mock bet analytics using the user's chosen persona, plus seeded ~30 demo mock bets for the demo user so the analytics dashboard has data out of the box.

### New files:
- `apps/mockbets/services/ai_commentary.py` — AI commentary service with persona system, structured prompts, OpenAI integration (same pattern as game AI Insight)

### New route:
- `/mockbets/ai-commentary/` — AJAX POST endpoint for generating AI performance commentary

### Features:
- **AI Performance Commentary** — "Generate Commentary" button on analytics dashboard. AI reviews KPIs, calibration, edge analysis, and variance data. Uses the user's AI persona preference (Analyst, NY Bookie, Southern Commentator, Ex-Player). Requires 5+ settled bets. Model/temperature/max tokens configurable via Admin Console → Site Configuration.
- **Demo Mock Bets** — `seed_demo` command now seeds ~30 mock bets for the demo user (15 CFB, 10 CBB, 5 pending) with realistic odds, varied results, settlement logs, and review flags. Analytics dashboard populates immediately after seeding.

### Modified files:
- `apps/mockbets/views.py` — Added ai_commentary view
- `apps/mockbets/urls.py` — Added ai-commentary/ route
- `templates/mockbets/analytics.html` — Added AI Commentary panel with generate button, loading state, error handling
- `apps/datahub/management/commands/seed_demo.py` — Added _seed_mock_bets method with 30 deterministic bets
- `templates/includes/help_modal.html` — Added AI commentary info to mock_analytics help
- `templates/accounts/user_guide.html` — Added AI commentary docs to Section 11

---

## 2026-02-08 - Mock Bet Analytics Dashboard (Phase 2-4)

**Summary:** Full analytics dashboard for mock bet simulation with interactive Chart.js charts, House vs User comparison, confidence calibration, edge analysis, variance/stress testing, and flat-bet what-if simulation.

### New files:
- `apps/mockbets/services/analytics.py` — Analytics computation engine with 7 functions: compute_kpis, compute_chart_data, compute_comparison, compute_confidence_calibration, compute_edge_analysis, compute_flat_bet_simulation, compute_variance_stats
- `templates/mockbets/analytics.html` — Full analytics dashboard template with Chart.js charts

### New views & routes:
- `/mockbets/analytics/` — Analytics dashboard with filters (sport, bet type, confidence, model source, date range)
- `/mockbets/flat-bet-sim/` — AJAX endpoint for flat-bet what-if simulation

### Features:
- **KPI Cards** — Total bets, W-L-P record, win rate, simulated net P/L, ROI, avg odds, avg implied probability
- **Filters** — Sport, bet type, confidence level, model source, date range (from/to)
- **5 Chart.js Charts** — Cumulative P/L (line), Rolling Win % with 50% reference (line), ROI by Sport (bar), Performance by Confidence (grouped bar), Odds Distribution (scatter)
- **House vs User Comparison** — Head-to-head table: count, win rate, ROI, net P/L, avg odds, implied probability, volatility
- **Confidence Calibration** — Expected vs actual win rate by confidence level
- **Edge Analysis** — Win rate and ROI by edge bucket (negative, 0-3%, 3-7%, 7%+)
- **Variance & Stress Testing** — Longest win/loss streaks, max drawdown, volatility, best/worst N-bet stretches
- **Flat-Bet Simulation** — What-if with custom stake, recalculated P/L/ROI/drawdown + cumulative chart

### Modified files:
- `apps/mockbets/views.py` — Added analytics_dashboard and flat_bet_sim views
- `apps/mockbets/urls.py` — Added analytics/ and flat-bet-sim/ routes
- `templates/mockbets/my_bets.html` — Added "Analytics Dashboard" button link
- `templates/includes/help_modal.html` — Added mock_analytics help key
- `templates/accounts/user_guide.html` — Added Section 11 (Mock Bet Analytics), renumbered Glossary to 12

---

## 2026-02-08 - Session closeout command + CLAUDE.md updates

**Summary:** Added `/closeout` slash command for Claude Code that reviews all documentation, help systems, and tracking before ending a coding session. Updated CLAUDE.md Standing Instructions to include What's New page and parallel session safety notes.

### New files:
- `.claude/commands/closeout.md` — session closeout skill (changelog, What's New, help system, User Guide, CLAUDE.md review, safe git commit/push with rebase for parallel sessions)

### Modified files:
- `CLAUDE.md` — added What's New to Standing Instructions (#3), added `/closeout` reference, added parallel sessions note

---

## 2026-02-08 - Mock Bet Simulation System (Phase 1)

**Summary:** Added a comprehensive Mock Bet Simulation system for tracking simulated betting decisions, evaluating outcomes, and analyzing decision quality over time. Covers CFB, CBB, and Golf with sport-specific bet types. No real money — strictly for analytics and learning.

### New app: `apps/mockbets/`

**Models:**
- `MockBet` — UUID primary key, user FK, sport-specific game/event FKs, bet type (moneyline/spread/total for games; outright/top_5/top_10/top_20/make_cut/matchup for golf), American odds, implied probability, simulated stake/payout, result (pending/win/loss/push), confidence level, model source, expected edge, decision review fields
- `MockBetSettlementLog` — Audit trail for settlement decisions

**Settlement Engine:**
- `apps/mockbets/services/settlement.py` — Auto-settles pending bets when games finalize
- Sport-specific resolution: moneyline, spread (with line parsing), total (over/under), golf positional finishes
- Atomic transactions with audit logging

**Management Command:**
- `settle_mockbets --sport=cfb|cbb|golf|all` — Idempotent settlement command for cron integration

**Views & API:**
- `/mockbets/` — My Mock Bets dashboard with KPI cards (total bets, win rate, net P/L, ROI), sport and result filters
- `/mockbets/place/` — AJAX endpoint for placing mock bets (validates all inputs, calculates implied probability)
- `/mockbets/<uuid>/` — Bet detail page with full parameters, settlement log, and decision review
- `/mockbets/<uuid>/review/` — AJAX endpoint for flagging bets (would repeat/avoid) with reflection notes

**Placement Modal:**
- Reusable `place_bet_modal.html` partial included on game detail pages
- Dynamic bet type options based on sport (game vs golf types)
- Pre-fills odds from game data when available
- Real-time implied probability calculation
- Safety disclaimer on every modal

**Game Detail Integration:**
- "Place Mock Bet" button added to CFB and CBB game detail pages (authenticated users, non-final games only)
- Placement modal auto-populates sport, game ID, and odds

**Navigation:**
- "My Mock Bets" link added to profile dropdown menu

**Safety Language:**
- "Simulated analytics only. No real money involved." disclaimer on My Bets, Bet Detail, and placement modal
- All financial figures labeled as "Simulated" (Sim. Net P/L, Sim. ROI, Simulated Stake, Simulated Payout)

**Help System:**
- Added `mock_bets` and `mock_bet_detail` help keys to help modal
- Added Section 10 (Mock Bets) to User Guide with full feature documentation
- Glossary renumbered to Section 11

**Tests:**
- 23 tests covering model calculations, payout logic, API validation, settlement engine, review endpoints

### New files:
- `apps/mockbets/` — full app (models, views, urls, admin, apps, services, management command, tests)
- `templates/mockbets/` — my_bets.html, bet_detail.html, includes/place_bet_modal.html

### Modified files:
- `brotherwillies/settings.py` — added `apps.mockbets` to INSTALLED_APPS
- `brotherwillies/urls.py` — added `/mockbets/` URL include
- `templates/base.html` — added "My Mock Bets" to profile dropdown
- `templates/cfb/game_detail.html` — added Place Mock Bet button + modal include
- `templates/cbb/game_detail.html` — added Place Mock Bet button + modal include
- `templates/includes/help_modal.html` — added mock_bets and mock_bet_detail help keys
- `templates/accounts/user_guide.html` — added Mock Bets section, renumbered Glossary

### Migration:
- `mockbets.0001_initial` — MockBet and MockBetSettlementLog tables

### Verified:
- `manage.py check` (0 issues), migration applied, 23 tests passing

---

## 2026-02-08 - What's New page (human-readable release history)

**Summary:** Added a "What's New" page at `/profile/whats-new/` that presents the full changelog as a human-readable product evolution story. Grouped by date/time releases with friendly descriptions instead of technical file lists. Accessible from the profile dropdown and linked from the User Guide TOC. No login required.

### New files:
- `templates/accounts/whats_new.html` — full release history with TOC, 5 release sections

### Modified files:
- `apps/accounts/views.py` — added `whats_new_view`
- `apps/accounts/profile_urls.py` — added `/profile/whats-new/` route
- `templates/base.html` — added "What's New" link to profile dropdown
- `templates/includes/help_modal.html` — added `whats_new` help key
- `templates/accounts/user_guide.html` — added What's New link to TOC

---

## 2026-02-08 - User Guide page + CLAUDE.md trim

**Summary:** Added comprehensive User Guide at `/profile/user-guide/` with 10 sections covering every feature, accessible from profile dropdown and quick links. Trimmed CLAUDE.md from ~585 lines to ~284 lines by removing implementation details that belong in code/help system, and added Standing Instructions section.

### New files:
- `templates/accounts/user_guide.html` — comprehensive user guide with TOC, 10 sections, glossary

### Modified files:
- `apps/accounts/views.py` — added `user_guide_view`
- `apps/accounts/profile_urls.py` — added `/profile/user-guide/` route
- `templates/base.html` — added User Guide link to profile dropdown
- `templates/accounts/profile.html` — added User Guide to quick links
- `templates/includes/help_modal.html` — added `user_guide` help key
- `CLAUDE.md` — trimmed to concise project reference, added Standing Instructions, removed implementation details (Build Progress, Model Services formulas, Analytics Pipeline field docs, AI Insight Engine internals, Live Data Ingestion details, Help System docs)

---

## 2026-02-08 - AI Insight: fix injury language (no injuries ≠ missing data)

**Summary:** Fixed AI Insight treating "no injuries reported" as "missing data." When ESPN/CBBD report no injuries for a game, that's normal — not a data gap. Previously, the AI would flag this as missing data, degrade confidence, and overstate the significance. Now: (1) empty injuries no longer added to `missing_data` list, (2) system prompt instructs AI to briefly note "no injuries reported by the source" and move on.

### Modified files:
- `apps/core/services/ai_insights.py` — removed `injury reports` from `missing_data` when injuries list is empty; updated INJURY IMPACT instruction in system prompt

---

## 2026-02-08 - Value Board: sport icons, accordion sections, favorite team colors

**Summary:** Overhauled the Value Board with three enhancements: (1) SVG sport icons next to CBB/CFB/Golf tabs, (2) collapsible accordion sections grouping games by timeframe (Today, Tomorrow, This Week, Coming Up) with a "Big Games" section for CFB showing top-rated matchups, and (3) favorite team color highlighting with a school-colored accent bar on game cards. Added `primary_color` field to both Team models with a comprehensive color dictionary covering ~133 FBS and ~360+ D1 basketball teams.

### Model changes:
- `cfb.Team` — added `primary_color` CharField (hex color, e.g. `#9E1B32`)
- `cbb.Team` — added `primary_color` CharField (hex color, e.g. `#0051BA`)

### New files:
- `apps/datahub/team_colors.py` — comprehensive team color dictionaries (`CFB_TEAM_COLORS`, `CBB_TEAM_COLORS`) keyed by slug, plus `get_team_color()` helper

### Modified files:
- `apps/cfb/models.py` — added `primary_color` field
- `apps/cbb/models.py` — added `primary_color` field
- `apps/core/views.py` — added `_group_games_by_timeframe()` helper, passes `game_sections` and `favorite_team_color` to template context
- `templates/core/value_board.html` — rewritten with SVG sport icons, accordion sections, and school-color border-top on favorite team cards
- `static/css/style.css` — added `.sport-tab-icon`, `.vb-section` accordion styles, `.game-card-favorite`
- `static/js/app.js` — added `toggleVBSection()` with localStorage persistence for expand/collapse state
- `templates/includes/help_modal.html` — updated Value Board help with sections and color bar explanations
- `apps/datahub/management/commands/seed_demo.py` — passes `primary_color` when creating teams
- `apps/datahub/providers/cfb/schedule_provider.py` — sets `primary_color` on team creation + backfills existing teams
- `apps/datahub/providers/cbb/schedule_provider.py` — same color population logic

### Migrations:
- `apps/cfb/migrations/0003_team_primary_color.py`
- `apps/cbb/migrations/0003_team_primary_color.py`

---

## 2026-02-08 - Merge favorites into unified section + reorder preferences

**Summary:** Combined CFB, CBB, and Golf favorites into a single "Favorites" accordion section with sport sub-groups (🏈 College Football, 🏀 College Basketball, ⛳ Golf). Reordered preferences sections to: AI Persona → Favorites → Value Board Filters → Location. Badge on Favorites header dynamically shows all selected favorites. Golfer select/clear now rebuilds the combined badge correctly.

### Modified files:
- `templates/accounts/preferences.html` — merged three favorites sections into one with `.fav-sport-group` sub-sections, reordered accordion sections, added `favBadge` ID for dynamic badge updates, improved `rebuildFavBadge()` JS function

---

## 2026-02-08 - Favorite golfer with autocomplete search

**Summary:** Added favorite golfer selection to preferences. Users can search ~200 PGA Tour players by typing any part of their name (first, last, or full) with instant AJAX autocomplete. The Golfer model now stores first/last name split for better search. Data stored as FK on UserProfile, relatable to future golf odds/results/analytics.

### Model changes:
- `Golfer` — added `first_name`, `last_name` (indexed, auto-split from `name` on save)
- `UserProfile` — added `favorite_golfer` FK to `golf.Golfer`

### New files:
- `apps/datahub/management/commands/seed_golfers.py` — seeds ~200 top PGA Tour players (idempotent, backfills first/last on existing rows)

### Modified files:
- `apps/golf/models.py` — Golfer fields + auto-split save()
- `apps/golf/views.py` — added `golfer_search` AJAX endpoint (login required, icontains on name/first/last, returns top 15 JSON)
- `apps/golf/urls.py` — added `/golf/api/golfer-search/`
- `apps/accounts/models.py` — added `favorite_golfer` FK
- `apps/accounts/forms.py` — added `favorite_golfer` as HiddenInput (autocomplete JS sets value)
- `templates/accounts/preferences.html` — new Golf Favorites accordion section with search input, dropdown results, keyboard nav, selected-state chip with clear button
- `apps/datahub/management/commands/ensure_seed.py` — calls `seed_golfers` on deploy

### Search features:
- Debounced AJAX (250ms) — no excess API calls
- Keyboard navigation (arrow keys + Enter + Escape)
- Click-to-select from dropdown
- Selected golfer shows as chip with X to clear
- Badge updates in real-time on section header

---

## 2026-02-08 - Preferences page redesign (accordion + persona tiles)

**Summary:** Rebuilt the preferences page with a collapsible accordion layout inspired by WLJ. Each settings group (Location, CFB Favorites, CBB Favorites, Value Board Filters, AI Persona) is a card with icon, title, subtitle, and current-value badge. Sections collapse/expand on tap. AI persona selection uses visual tile cards instead of a dropdown. Expand All / Collapse All controls at top. Toggle switch for the "always include favorite" checkbox. Sections auto-open when they contain validation errors.

### Changes:
- `templates/accounts/preferences.html` — fully rewritten with accordion sections, persona tile grid, toggle switch, scoped CSS + JS
- No backend changes — same form fields, same POST handling, same view logic

---

## 2026-02-08 - Admin-configurable AI settings (SiteConfig)

**Summary:** Added a `SiteConfig` singleton model editable from Django admin (`/bw-manage/`). AI temperature and max tokens are now configurable at runtime without redeploying. Temperature defaults to 0 (deterministic/most factual).

### Changes:
- **`apps/core/models.py`** — new `SiteConfig` singleton with `ai_temperature` (default 0.0) and `ai_max_tokens` (default 800), enforced pk=1, `SiteConfig.get()` class method
- **`apps/core/admin.py`** — registered with fieldset, description, no-delete, single-row enforcement
- **`apps/core/services/ai_insights.py`** — reads temperature/max_tokens from `SiteConfig.get()` with fallback defaults
- **`apps/core/migrations/0001_initial.py`** — creates SiteConfig table

### Admin usage:
1. Go to `/bw-manage/` → Core → Site Configuration
2. Click "Add" (first time) or edit the existing row
3. Change AI Temperature (0 = factual, 0.3 = slight variation, 1.0+ = creative)
4. Change Max Tokens if needed
5. Save — takes effect on next AI Insight request (no restart needed)

---

## 2026-02-08 - AI Insight: general knowledge enrichment

**Summary:** Updated AI system prompt to allow supplementing analysis with well-established general sports knowledge (conference history, program prestige, rivalries, coaching records, championship counts). Previously the AI was limited to ONLY the data we passed, which meant it couldn't correct bad data (e.g., Clemson listed as "Independent" instead of ACC) or add widely-known context.

### Changes:
- **`apps/core/services/ai_insights.py`** — rewrote CRITICAL RULES section with 3-tier data hierarchy:
  1. PRIMARY DATA — our structured numbers (always source of truth for quantitative analysis)
  2. GENERAL KNOWLEDGE — well-known verifiable facts about teams/programs (allowed)
  3. DATA CORRECTIONS — flag and correct clearly wrong data (e.g., wrong conference)
- Hard limits remain: no invented current-season stats, no player names unless certain, no betting advice
- Temperature lowered 0.4 → 0.3 (tighter, less hallucination risk)
- Max tokens raised 600 → 800 (richer context needs more space)
- Word limit raised 300 → 350

---

## 2026-02-08 - Unified Value Board with sport tabs

**Summary:** Consolidated the separate CFB and CBB Value Boards into a single unified `/value/` page with sport tabs. The tab bar auto-detects which sports have upcoming games or events and shows only those. CBB appears first during basketball season (Nov-Apr). Golf events appear when available.

### Changes:
- **`apps/core/views.py`** — new `value_board()` view with `_get_available_sports()`, `_get_cfb_value_data()`, `_get_cbb_value_data()`, `_get_golf_events()`, shared `_apply_filters()` helper, and `cbb_value_redirect()`
- **`templates/core/value_board.html`** — new unified template with sport tabs, conditional game/event rendering per sport
- **`apps/core/urls.py`** — added `/value/` route
- **`brotherwillies/urls.py`** — removed old `/value/` (CFB) route, kept `/cbb/value/` as redirect to `/value/?sport=cbb`
- **`templates/core/home.html`** — updated dashboard links to use `?sport=` params
- **`static/css/style.css`** — new `.sport-tabs`, `.sport-tab`, `.sport-tab-count` styles

### Behavior:
- Default sport = first available (CBB in Feb, CFB in Sep, etc.)
- `?sport=cbb|cfb|golf` query param selects tab; `?sort=` preserved per-tab
- Old `/cbb/value/` redirects to `/value/?sport=cbb`
- Golf tab shows upcoming events (links to Golf Hub)
- Tab shows game count badge per sport
- Anonymous users still see top 3 games (login gate)

---

## 2026-02-08 - Branded auth pages (2-column split layout)

**Summary:** Redesigned all authentication pages with a bold, modern 2-column split layout. Left column (66%) features the BW logo on a dark background with entrance animation; right column (34%) contains the form in a clean white card on light gray. Fully responsive — stacks vertically on mobile with logo on top. Added password reset flow using Django's built-in views.

### Updated pages:
- **Sign In** (`/accounts/login/`) — standalone layout with logo, username/email + password, "Forgot your password?" link
- **Password Reset** (`/accounts/password-reset/`) — email input, sends reset link
- **Password Reset Confirm** (`/accounts/password-reset/<uidb64>/<token>/`) — new password + confirm
- **Password Reset Done** (`/accounts/password-reset/done/`) — confirmation message
- **Password Reset Complete** (`/accounts/password-reset/complete/`) — success with sign-in link

### New/modified files:
- `static/css/auth.css` — full 2-column layout CSS with responsive breakpoints and logo animations
- `static/branding/bw_logo.png` — logo asset (renamed from double extension)
- `templates/accounts/login.html` — standalone auth layout (no longer extends base.html)
- `templates/registration/password_reset_form.html` — new template
- `templates/registration/password_reset_confirm.html` — new template
- `templates/registration/password_reset_done.html` — new template
- `templates/registration/password_reset_complete.html` — new template
- `apps/accounts/urls.py` — added password reset URL patterns with namespaced success URLs

### Design:
- No registration links or sign-up messaging (per security policy)
- 16px min font on inputs (prevents iOS auto-zoom)
- Logo entrance animation with subtle glow effect
- Mobile: stacks vertically, logo scales down
- All pages are self-contained (no header/footer/nav chrome)

---

## 2026-02-08 - Partner feedback system

**Summary:** Added a private, partner-only feedback system for internal product operations. Three authorized partners (djenkins, jsnyder, msnyder) can submit structured feedback targeting specific site components, review it through a status pipeline (New → Accepted → Ready → Dismissed), and manage everything via a custom admin console. The system is future-safe for AI-driven action — feedback marked as READY exposes a structured `is_ready_for_ai` property. No public visibility, no Django Admin usage, no auto-modifications.

### New app: `apps/feedback/`
- **Models:** `FeedbackComponent` (categorization) + `PartnerFeedback` (UUID primary key, status workflow, reviewer notes)
- **Access control:** `is_partner()` helper + `@partner_required` decorator — returns 404 for non-partners
- **Submission form:** Component dropdown, title, description — available at `/feedback/new/`
- **Custom admin console:** Dashboard with status counts, filters (status/component/user), full CRUD — at `/feedback/console/`
- **Validation:** Status change to READY or DISMISSED requires reviewer notes
- **Seed command:** `seed_feedback` populates 10 components + demo feedback items (runs via `ensure_seed` on deploy)
- **Tests:** 16 tests covering access control, CRUD, form validation, filtering
- **Help content:** Added `feedback` help key to help modal
- **Nav integration:** "Feedback" link in profile dropdown (partner-only)

### Routes:
| Route | Purpose |
|-------|---------|
| `/feedback/new/` | Submit feedback |
| `/feedback/console/` | Feedback dashboard |
| `/feedback/console/<uuid>/` | Feedback detail |
| `/feedback/console/<uuid>/update/` | Edit feedback |

### New/modified files:
- `apps/feedback/` — new app (models, views, forms, urls, access, tests, seed command)
- `templates/feedback/` — new templates (new, console, detail, edit)
- `templates/includes/help_modal.html` — added feedback help key
- `templates/base.html` — added Feedback link in profile dropdown
- `brotherwillies/settings.py` — added `apps.feedback` to INSTALLED_APPS
- `brotherwillies/urls.py` — added feedback URL include
- `apps/datahub/management/commands/ensure_seed.py` — calls `seed_feedback` on deploy
- `docs/changelog.md` — this entry

---

## 2026-02-08 - AI Insight engine (OpenAI-powered game explanations)

**Summary:** Added an AI-powered explanation engine to game detail pages. Logged-in users can tap "AI Insight" to get a factual, structured summary of why the house model and market agree or disagree on a game. The AI uses ONLY data already shown on the page (team ratings, injuries, odds, model probabilities) — no speculation, no invented facts. Users can choose from 4 AI personas (Analyst, New York Bookie, Southern Commentator, Ex-Player) in Preferences.

### Architecture:
- **Service layer:** `apps/core/services/ai_insights.py` — prompt construction, OpenAI Chat Completions call, structured context builder
- **AJAX endpoint:** `GET /api/ai-insight/<sport>/<game_id>/` — returns JSON with `content` and `meta`
- **Login required** — anonymous users see the existing login gate
- **Strict fact-only prompts** — system prompt enforces no speculation, no betting advice, no invented data
- **Fail-safe** — graceful error messages when API key is missing, data is incomplete, or API call fails

### Persona system:
| Persona | Tone |
|---------|------|
| `analyst` (default) | Neutral, professional, factual |
| `new_york_bookie` | Blunt, sharp, informal (profanity allowed) |
| `southern_commentator` | Calm, folksy, confident |
| `ex_player` | Direct, experiential (profanity controlled) |

Persona affects tone only — content and facts remain identical.

### New/modified files:
- `apps/core/services/__init__.py` — new (package init)
- `apps/core/services/ai_insights.py` — new (AI service layer: prompt builder, context builder, OpenAI caller, logging)
- `apps/core/urls.py` — added `/api/ai-insight/` route
- `apps/core/views.py` — added `ai_insight_view` AJAX endpoint
- `apps/accounts/models.py` — added `ai_persona` field to UserProfile (4 choices, default: analyst)
- `apps/accounts/forms.py` — added `ai_persona` to PreferencesForm with help text
- `apps/accounts/migrations/0006_userprofile_ai_persona.py` — new migration
- `templates/cfb/game_detail.html` — added AI Insight button, loading spinner, result container, inline JS
- `templates/cbb/game_detail.html` — same as CFB
- `templates/includes/help_modal.html` — added AI Insight explanation to `game_detail` help section
- `static/css/style.css` — `.ai-insight-card`, `.ai-insight-header`, `.ai-insight-body`, `.ai-insight-error`, `.badge-ai`, `.spinner`, `@keyframes spin`
- `requirements.txt` — added `openai>=1.0`
- `brotherwillies/settings.py` — added `OPENAI_API_KEY`, `OPENAI_MODEL` settings

### Environment variables:
```
OPENAI_API_KEY=           # Required for AI Insight to work
OPENAI_MODEL=gpt-4.1-mini  # Default model (override with gpt-4.1 for higher quality)
```

### Logging:
- Logs model used, prompt hash, response length, and elapsed time for every AI insight request
- Logs errors with game ID, sport, and error message

### Verified:
- `manage.py check` (0 issues), migration applied, all imports clean
- URL resolution works, prompt construction tested with real game data
- Graceful error when OPENAI_API_KEY is not set

---

## 2026-02-08 - Help content update + CLAUDE.md analytics docs

**Summary:** Updated context-aware help to explain exactly where every number comes from — model formulas, API sources, confidence thresholds, snapshot lifecycle. Added Analytics Pipeline and Context-Aware Help System sections to CLAUDE.md so future changes always keep help content in sync.

### Changes:
- `templates/includes/help_modal.html` — Rewrote `performance`, `game_detail`, and `home` help keys with detailed explanations of scores, status badges, CLV, calibration, model formula, confidence thresholds, data sources
- `CLAUDE.md` — Added Analytics Pipeline section (cron order, model formula, snapshot fields, score sources, performance metrics), Context-Aware Help System section (all help keys, architecture, update rules), updated build progress and command lists

---

## 2026-02-08 - Productive analytics pipeline + score tracking

**Summary:** Analytics system now captures model predictions automatically, resolves game outcomes with real scores, and displays comprehensive performance metrics including accuracy by sport, calibration analysis, and closing line value (CLV). Game scores are ingested from APIs and displayed throughout the UI.

### Changes:
- `apps/cfb/models.py`, `apps/cbb/models.py` — added `home_score`, `away_score` fields to Game models
- `apps/datahub/providers/cfb/schedule_provider.py` — persist `home_points`/`away_points` from CFBD API
- `apps/datahub/providers/cbb/schedule_provider.py` — extract and persist scores from ESPN API
- `apps/datahub/management/commands/capture_snapshots.py` — new: captures house model predictions for upcoming games (24h window)
- `apps/datahub/management/commands/resolve_outcomes.py` — new: resolves final_outcome + closing_market_prob for completed games
- `apps/datahub/management/commands/refresh_data.py` — integrated capture_snapshots + resolve_outcomes into cron cycle
- `apps/accounts/views.py` — enhanced performance_view with sport breakdown, time trends, calibration, and CLV metrics
- `templates/accounts/performance.html` — rebuilt with full analytics dashboard (overall, by sport, trends, CLV, calibration table, recent results)
- `templates/cfb/game_detail.html`, `templates/cbb/game_detail.html` — display scores and status badges for live/final games
- `templates/core/home.html` — display live scores in dashboard
- `static/css/style.css` — `.game-score`, `.badge-gray`, `.text-green`, `.text-red`, `.table-wrap` styles

### Cron pipeline order:
1. `ingest_schedule` (updates status + scores)
2. `ingest_odds` (fresh market lines)
3. `ingest_injuries`
4. `capture_snapshots` (pre-game predictions)
5. `resolve_outcomes` (post-game results + CLV)

---

## 2026-02-08 - Live games dashboard + cron refresh + data source docs

**Summary:** Home dashboard now shows live/in-progress games in a dedicated "Live Now" section with pulsing red badge and dot. ESPN fetch window expanded to include yesterday's scoreboard for late-night games. Added `refresh_data` management command for Railway cron job. Help system now documents data sources on every page.

### Changes:
- `apps/core/views.py` — home view queries `status='live'` games separately, renders in "Live Now" section
- `templates/core/home.html` — "Live Now" section at top with red pulsing LIVE badge, game cards with red left-border
- `static/css/style.css` — `.badge-live`, `.game-card-live`, `.live-dot`, `.section-title-live`, `@keyframes live-pulse`
- `apps/datahub/providers/cbb/schedule_provider.py` — ESPN fetch range changed to `range(-1, 8)` (includes yesterday)
- `apps/datahub/management/commands/refresh_data.py` — new command for Railway cron (refreshes all enabled sports)
- `apps/datahub/management/commands/ensure_seed.py` — runs live ingestion on deploy when env toggles enabled
- `templates/includes/help_modal.html` — "Where does this data come from?" on Home, Value Board, CFB/CBB Hub, Game Detail, Golf

### Railway cron setup:
1. Railway dashboard → "+ New" → "Cron Job"
2. Start command: `python manage.py refresh_data`
3. Schedule: `*/30 * * * *`
4. Copy env vars from main service

---

## 2026-02-08 - Store profile picture as base64 in DB (Railway-safe)

**Summary:** Replaced `ImageField` (filesystem-based, breaks on Railway's ephemeral disk) with a `TextField` storing the image as a base64 data URI. Uploaded images are center-cropped to square, resized to 200×200, and JPEG-compressed (~5-15 KB). Profile picture now shows in the header profile button and bottom nav.

### Changes:
- `apps/accounts/models.py` — replaced `profile_picture` ImageField with `profile_picture_data` TextField
- `apps/accounts/views.py` — `_process_profile_picture()` resizes/compresses uploads to base64 data URI
- `apps/accounts/context_processors.py` — new `user_profile` context processor (safe `get_or_create`)
- `apps/accounts/migrations/0005_*` — removes old field, adds new one
- `templates/base.html` — header & bottom nav render profile picture from data URI
- `templates/accounts/profile.html` — profile page uses `profile_picture_data`
- `brotherwillies/settings.py` — registered context processor
- `static/css/style.css` — `.icon-btn` border, `.header-avatar`, `.nav-avatar` styles

---

## 2026-02-08 - Fix 500 error on /profile/ for users missing UserProfile row

**Summary:** Replaced `request.user.profile` (which crashes with `RelatedObjectDoesNotExist` if no UserProfile row exists) with `UserProfile.objects.get_or_create(user=request.user)` in all affected views.

### Files changed:
- `apps/accounts/views.py` — `profile_view`, `preferences_view`, `my_stats_view`
- `apps/cfb/views.py` — `value_board` (preference filters + bye-week check)

### Root cause:
Users created before the `post_save` signal was wired (or via paths that bypass it) had no `UserProfile` row, causing a 500 on any page that accessed `request.user.profile`.

---

## 2026-02-08 - Live Data Ingestion (Step 18)

**Summary:** Multi-sport live data ingestion for CBB, PGA Golf, and CFB. Provider architecture fetches from external APIs and normalizes into existing models. Entirely optional — controlled by environment toggles. Seed data still works when live data is disabled.

### Data Sources:
- **The Odds API** (free tier, 500 req/mo) — odds for all 3 sports
- **CBBD API** (free) — CBB schedules, scores, stats
- **CFBD API** (free, 1K req/mo) — CFB schedules, scores, stats
- **ESPN Public API** (free, no key) — supplementary schedules/injuries, golf fields

### New management commands:
- `ingest_schedule --sport=cbb|cfb|golf` — fetch and upsert games
- `ingest_odds --sport=cbb|cfb|golf` — fetch and append odds snapshots
- `ingest_injuries --sport=cbb|cfb` — fetch and upsert injury impacts
- All commands respect `LIVE_DATA_ENABLED` + per-sport toggles (use `--force` to override)

### Architecture:
- `apps/datahub/providers/` — multi-sport provider layer
  - `base.py` — AbstractProvider (fetch → normalize → persist)
  - `client.py` — APIClient with rate limiting, retries, exponential backoff
  - `registry.py` — `get_provider(sport, data_type)` lookup
  - `name_utils.py` — team/player name normalization with alias table
  - `cbb/` — CBBScheduleProvider (CBBD), CBBOddsProvider (Odds API), CBBInjuriesProvider (ESPN)
  - `cfb/` — CFBScheduleProvider (CFBD), CFBOddsProvider (Odds API), CFBInjuriesProvider (ESPN)
  - `golf/` — GolfScheduleProvider (ESPN), GolfOddsProvider (Odds API)

### Golf model additions:
- `GolfOddsSnapshot` model (event, golfer, outright_odds, implied_prob)
- `external_id` field on GolfEvent and Golfer
- `slug` field on GolfEvent

### Environment toggles (settings.py):
- `LIVE_DATA_ENABLED` — master switch
- `LIVE_CBB_ENABLED`, `LIVE_CFB_ENABLED`, `LIVE_GOLF_ENABLED` — per-sport
- `ODDS_API_KEY`, `CFBD_API_KEY`, `CBBD_API_KEY`

### New files (19):
- `apps/datahub/providers/__init__.py`, `base.py`, `client.py`, `registry.py`, `name_utils.py`
- `apps/datahub/providers/cbb/__init__.py`, `schedule_provider.py`, `odds_provider.py`, `injuries_provider.py`
- `apps/datahub/providers/cfb/__init__.py`, `schedule_provider.py`, `odds_provider.py`, `injuries_provider.py`
- `apps/datahub/providers/golf/__init__.py`, `schedule_provider.py`, `odds_provider.py`
- `apps/datahub/management/commands/ingest_schedule.py`, `ingest_odds.py`, `ingest_injuries.py`

### Modified files:
- `apps/golf/models.py` — added GolfOddsSnapshot, external_id, slug fields
- `apps/golf/admin.py` — registered GolfOddsSnapshot
- `brotherwillies/settings.py` — added live data toggles and API key settings
- `.env.example` — added live data env vars

### Migration:
- `golf.0002` — GolfOddsSnapshot model, external_id + slug fields

### Verified:
- `manage.py check` (0 issues), migrations applied, seed_demo works, all commands registered

---

## 2026-02-08 - Security Hardening & Registration Disabled

**Summary:** Disabled public registration, hardened login against brute-force bots, obscured admin URL, and added HSTS headers.

### Changes:
- **Registration disabled:** Removed `/accounts/register/` URL route and all Register links/buttons from home page, login page, CFB Value Board, and CBB Value Board. View/form/template left in place for easy re-enable.
- **Login rate limiting:** Added `django-axes` — locks out after 5 failed attempts per username+IP, 1-hour cooloff, resets on success.
- **Admin URL obscured:** Changed `/admin/` to `/bw-manage/` to avoid bot scanners. Updated profile dropdown link.
- **Admin password from env var:** `ensure_superuser` now reads `ADMIN_PASSWORD` env var (falls back to default for local dev). Set a strong password on Railway.
- **HSTS headers:** Added `SECURE_HSTS_SECONDS` (1 year), `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` in production.

### Files changed:
- `apps/accounts/urls.py` — removed register route
- `templates/core/home.html` — removed Register button
- `templates/cfb/value_board.html` — removed Register button
- `templates/cbb/value_board.html` — removed Register button
- `templates/accounts/login.html` — removed "Don't have an account?" link
- `templates/base.html` — admin link updated to `/bw-manage/`
- `brotherwillies/urls.py` — admin path changed to `bw-manage/`
- `brotherwillies/settings.py` — added axes app/middleware/backend, HSTS settings
- `apps/datahub/management/commands/ensure_superuser.py` — reads ADMIN_PASSWORD env var
- `requirements.txt` — added `django-axes>=8.0`

### Migration:
- `axes.0001` through `axes.0010` (auto-applied)

### Action required on Railway:
- Set `ADMIN_PASSWORD` env var to a strong password
- Since admin user already exists, manually change password via Django shell or recreate

### Verified:
- `manage.py check` (0 issues), migrations applied

---

## 2026-02-08 - User Timezone Support via Zip Code

**Summary:** Users can set their zip code on the Preferences page to automatically resolve their timezone. All game times display in the user's local timezone with an abbreviation (CST, EST, etc.). Uses the `zipcodes` Python library for per-zip-code accuracy in split-timezone states (Indiana, Tennessee, Florida panhandle).

### Changes:
- Added `zip_code` and `timezone` fields to UserProfile model
- Added `zipcodes>=1.3` to requirements.txt for per-zip-code timezone resolution
- Created `apps/accounts/timezone_lookup.py` — `zip_to_timezone()` wrapper around `zipcodes.matching()`
- Zip code field on Preferences page with 5-digit validation; timezone resolved on save and displayed in green
- Created `brotherwillies/middleware.py` with `UserTimezoneMiddleware` — activates user's timezone per-request
- Added middleware to settings after `AuthenticationMiddleware`
- Created `apps/core/templatetags/tz_extras.py` with `{% tz_abbr %}` tag (outputs CST, EST, etc.)
- All game time displays across 10 templates now append the timezone abbreviation
- Anonymous users and users without a zip code see times in the server default (America/Chicago)

### Templates updated (10):
- `cfb/hub.html`, `cfb/value_board.html`, `cfb/game_detail.html`, `cfb/conference.html`
- `cbb/hub.html`, `cbb/value_board.html`, `cbb/game_detail.html`, `cbb/conference.html`
- `core/home.html`, `parlays/new.html`

### New files:
- `apps/accounts/timezone_lookup.py`
- `brotherwillies/middleware.py`
- `apps/core/templatetags/__init__.py`
- `apps/core/templatetags/tz_extras.py`

### Migration:
- `accounts.0004` — zip_code, timezone fields

### Verified:
- `manage.py check` (0 issues), migration applied, all key pages return 200

---

## 2026-02-08 - Season-Aware Dashboard & Offseason Banners

**Summary:** Home dashboard only shows in-season sports. CFB pages show an offseason/demo data banner when out of season.

### Changes:
- Home view now checks sport seasons by month (CFB: Aug-Jan, CBB: Nov-Apr)
- Dashboard only queries and displays games for sports currently in season
- CFB hub and value board show a yellow "Offseason" banner when CFB is not in season, explaining the data below is sample/demo data
- Added `.offseason-banner` CSS style

---

## 2026-02-08 - College Basketball (CBB) App

**Summary:** Full CBB app added alongside existing CFB, with realistic seed data scheduling.

### Changes:
- Created `apps/cbb/` Django app with models (Conference, Team, Game, OddsSnapshot, InjuryImpact)
- Game model uses `tipoff` field instead of CFB's `kickoff`; `neutral_site` retained for tournament games
- CBB model service (`apps/cbb/services/model_service.py`) with HFA=3.5 (vs CFB's 3.0)
- CBB views: hub, conference detail, game detail, value board — all mirroring CFB patterns
- CBB templates: `templates/cbb/` (hub, conference, game_detail, value_board)
- Added CBB to bottom navigation (basketball SVG icon, 6 nav items total)
- Updated home dashboard to show both "Top CBB Value Games" and "Top CFB Value Games" sections
- CFB Value Board renamed to "CFB Value Board" with cross-link to CBB Value Board
- CBB Value Board at `/cbb/value/` with cross-link back to CFB
- Added `favorite_cbb_conference` and `favorite_cbb_team` fields to UserProfile
- Added nullable `cbb_game` FK to analytics (UserGameInteraction, ModelResultSnapshot) and parlays (ParlayLeg)
- CBB seed data: 6 conferences, 30 teams, ~30 games on realistic Tue/Thu/Sat schedule
- Basketball-specific: evening tipoffs, totals 130-165, basketball injury notes
- **Fixed seed data realism**: teams no longer double-booked on same day (both CFB and CBB)
- Demo user favorites: Kansas / Big 12 (CBB), Alabama / SEC (CFB)
- Updated `ensure_seed.py` to check for both CFB and CBB conferences
- Updated help modal with CBB hub content
- Updated CLAUDE.md with CBB app documentation

### New files (15):
- `apps/cbb/__init__.py`, `apps.py`, `models.py`, `admin.py`, `views.py`, `urls.py`, `value_urls.py`, `tests.py`
- `apps/cbb/services/__init__.py`, `model_service.py`
- `apps/cbb/migrations/__init__.py`, `0001_initial.py`
- `templates/cbb/hub.html`, `conference.html`, `game_detail.html`, `value_board.html`

### Migrations:
- `cbb.0001_initial` — all CBB models
- `accounts.0003` — favorite_cbb_conference, favorite_cbb_team fields
- `analytics.0002` — cbb_game FK + nullable game FK
- `parlays.0002` — cbb_game FK + nullable game FK

### Verified:
- `manage.py check` (0 issues), all migrations applied, seed data loaded
- All key pages return 200: /, /cfb/, /cbb/, /cbb/value/, /value/, /cbb/conference/<slug>/, /cbb/game/<uuid>/

---

## 2026-02-07 - Comprehensive Help Content Overhaul

**Summary:** Rewrote all context-aware help content to be beginner-friendly with detailed explanations, examples, and a sample preferences setup.

### Changes:
- Every help section rewritten with "What is this page?" introductions
- All sports analytics terms explained from scratch (edge, spread, moneyline, Brier score, CLV, etc.)
- Added concrete examples using Alabama throughout (team the demo user follows)
- Preferences help includes detailed field-by-field explanations with sample values
- New `preferences` help_key for the split preferences page
- Added CBB Hub help section
- Profile help updated for new personal info fields
- Default/fallback help section expanded with platform overview

---

## 2026-02-07 - Header, Profile Dropdown, Profile/Preferences Split

**Summary:** Updated header branding, added profile dropdown menu, split profile into personal info and preferences pages, added profile picture upload support.

### Changes:
- Header logo changed from "BW" to "Brother Willies Predictions"
- Added profile dropdown menu in header (Profile, Preferences, Admin Console for staff, Log Out)
- Dropdown closes on outside click and Escape key
- Split `/profile/` into two pages:
  - `/profile/` — Personal info (first name, last name, email, profile picture)
  - `/profile/preferences/` — Filter preferences (favorite team/conference, spread, odds, edge)
- Added `profile_picture` ImageField to UserProfile model
- Added Pillow dependency for image handling
- Configured MEDIA_ROOT/MEDIA_URL in settings for uploaded files
- New forms: `PersonalInfoForm`, `PreferencesForm` (replaced single `ProfileForm`)
- New view: `preferences_view`
- New template: `templates/accounts/preferences.html`
- Updated CSS: profile dropdown styles, avatar section, responsive logo sizing
- Updated JS: `toggleProfileDropdown()` with click-outside-to-close

---

## 2026-02-07 - Railway Deployment Setup

**Summary:** Production deployment configuration for Railway.com.

### Changes:
- Added `runtime.txt` pinning Python 3.11.11
- Added `dj-database-url` and `whitenoise` to requirements.txt
- Updated `settings.py`:
  - `dj_database_url.config()` for DATABASE_URL support (falls back to SQLite locally)
  - WhiteNoise middleware + compressed static file storage
  - CSRF_TRUSTED_ORIGINS from env var
  - Production security settings (SSL redirect, secure cookies, proxy header)
- Created `ensure_superuser` management command (idempotent, hardcoded creds)
- Created `ensure_seed` management command (idempotent, seeds only if DB is empty)
- Custom start command set in Railway dashboard (no Procfile — Railpack's static secret scanner causes build failures)
- Updated CLAUDE.md with Railway constraints and deployment details

---

## 2026-02-07 - Initial Build (Steps 0-12)

**Summary:** Full Django project built from scratch. All core functionality implemented and verified.

### What was built:

**Project Foundation (Steps 0-1)**
- Python 3.11 virtualenv, requirements.txt (Django 5.x, python-dotenv, requests, gunicorn, psycopg2-binary)
- Django project `brotherwillies` with 7 apps: core, accounts, cfb, golf, parlays, analytics, datahub
- Settings with SQLite dev / PostgreSQL prod-ready config
- Base template with dark theme, fixed bottom nav, header with help icon
- Global CSS (mobile-first, responsive breakpoints) and vanilla JS

**Authentication & Accounts (Step 2)**
- Register/login/logout with Django built-in auth
- UserProfile (favorite team/conference, preference filters)
- UserModelConfig (5 tunable weights)
- ModelPreset (save/load weight configurations)
- UserSubscription (free/pro/elite tiers, no payments)
- Auto-creation signals on User save

**CFB Core (Steps 3-5)**
- Models: Conference, Team, Game (UUID pk), OddsSnapshot, InjuryImpact
- House model service (versioned "v1", logistic probability)
- User model service (recomputes with user weights)
- Edge/delta calculations, data confidence scoring
- Value Board with preference filtering, favorite team override, sorting
- Anonymous gating (top 3 rows only)
- CFB hub, conference dashboard, game detail pages

**User Features (Steps 6-8)**
- My Model tuning page with sliders + reset to house defaults
- Preset save/load with free tier 1-preset limit
- Personal Statistics (summary tiles + history, period filtering)
- Model Performance page (accuracy, Brier score from snapshots)
- Context-aware help system (per-page help_key, bottom-sheet modal)

**Additional Apps (Steps 9-11)**
- Golf MVP scaffolding (GolfEvent, Golfer, GolfRound models + placeholder page)
- Parlays app (builder, scoring, correlation detection with 10% haircut)
- Analytics models (UserGameInteraction, ModelResultSnapshot)
- Interaction logging on game detail views

**Seed Data (Step 12)**
- `python manage.py seed_demo` creates deterministic demo data
- 5 conferences, 25 teams, 25 games, odds snapshots with line movement
- 15 injury impacts, demo user with Alabama favorite, non-default model weights
- 1 preset, 10 model result snapshots, 1 demo parlay with 3 legs

**Files created:**
- `manage.py`, `brotherwillies/settings.py`, `brotherwillies/urls.py`, `brotherwillies/wsgi.py`, `brotherwillies/asgi.py`
- `apps/*/models.py`, `apps/*/views.py`, `apps/*/urls.py`, `apps/*/admin.py`, `apps/*/apps.py`
- `apps/accounts/forms.py`, `apps/cfb/services/model_service.py`
- `apps/datahub/management/commands/seed_demo.py`
- `templates/base.html`, `templates/includes/help_modal.html`
- All page templates (16 total across all apps)
- `static/css/style.css`, `static/js/app.js`
- `.env`, `.env.example`, `.gitignore`, `requirements.txt`
- `CLAUDE.md`, `docs/changelog.md`

**Migrations:** 0001_initial for cfb, accounts, analytics, golf, parlays

**Verified:** `manage.py check` (0 issues), all migrations applied, seed data loaded, all key pages return 200.

---
