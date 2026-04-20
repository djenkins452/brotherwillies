# Brother Willies - Changelog

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
