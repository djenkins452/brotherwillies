# Brother Willies - Changelog

---

## 2026-02-08 - Profile picture on header/nav icons + more visible icon buttons

**Summary:** Show the user's profile picture in the header profile button and bottom nav Profile tab (falls back to SVG person icon when no picture). Made the `?` help and profile icon buttons more visible with a `2px solid` border that highlights on hover.

### Changes:
- `templates/base.html` — header & bottom nav conditionally render `<img>` for profile picture
- `apps/accounts/context_processors.py` — new `user_profile` context processor (safe `get_or_create`)
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
