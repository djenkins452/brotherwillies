# Brother Willies - Changelog

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
