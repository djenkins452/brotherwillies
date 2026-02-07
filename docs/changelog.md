# Brother Willies - Changelog

---

## 2026-02-07 - Railway Deployment Setup

**Summary:** Production deployment configuration for Railway.com.

### Changes:
- Added `Procfile` with release phase (migrate, ensure_superuser, ensure_seed) and gunicorn web process
- Added `runtime.txt` pinning Python 3.11.11
- Added `dj-database-url` and `whitenoise` to requirements.txt
- Updated `settings.py`:
  - `dj_database_url.config()` for DATABASE_URL support (falls back to SQLite locally)
  - WhiteNoise middleware + compressed static file storage
  - CSRF_TRUSTED_ORIGINS from env var
  - Production security settings (SSL redirect, secure cookies, proxy header)
- Created `ensure_superuser` management command (idempotent, reads from env vars)
- Created `ensure_seed` management command (idempotent, seeds only if DB is empty)
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
