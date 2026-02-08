# Brother Willies - Claude Code Context

**Project:** Django 5.x sports analytics web app (mobile-first)
**Directory:** /Users/dannyjenkins/Projects/brotherwillies
**Domain:** brotherwillies.com
**Stack:** Python 3.11+ / Django 5.x / SQLite (dev) / PostgreSQL (prod-ready)

---

## BEHAVIOR RULES

**Do NOT ask permission for:**
- Reading files, searching, grepping
- Running tests or migrations
- Making commits when task is complete

**Still ask permission for:**
- Destructive operations (deleting files, dropping tables)
- Genuinely ambiguous or risky actions

**Task Discussion Flow:**
1. Present the task
2. Discuss scope and approach
3. Wait for "go" before implementation

**Communication style:**
- Be direct - skip "Would you like me to..."
- Execute (after "go"), don't propose
- If something fails, fix it and move on
- Summarize results, not intentions

**Conserve limits:**
- Keep responses concise
- Don't re-read files already in conversation
- Batch related changes
- Use Task tool with Explore agent for broad searches

---

## Standing Instructions

**Always update these when features change:**
1. **Context-aware help** — `templates/includes/help_modal.html` (keyed by `help_key` per page)
2. **User Guide** — `templates/accounts/user_guide.html` (at `/profile/user-guide/`)
3. **Changelog** — `docs/changelog.md`

**On task completion:**
1. Update changelog
2. Commit changes
3. Push: `GIT_SSH_COMMAND="ssh -p 443" git push git@ssh.github.com:djenkins452/brotherwillies.git main`

---

## Quick Reference

```bash
# Run dev server
python manage.py runserver

# Run tests
python manage.py test -v 1

# Run specific app tests
python manage.py test apps.cfb.tests -v 1 --failfast

# Create/apply migrations
python manage.py makemigrations && python manage.py migrate

# Seed demo data
python manage.py seed_demo

# Django check
python manage.py check
```

**Demo user:** `demo` / `brotherwillies`
**Admin superuser:** `admin` / `ADMIN_PASSWORD` env var (http://localhost:8000/bw-manage/)

---

## Tech Stack

- Django 5.x with built-in auth (sessions, no OAuth)
- SQLite (dev) / PostgreSQL (prod-ready)
- Django templates + CSS + vanilla JS (no React)
- Dark theme, card-based, mobile-first UI
- `zipcodes` library for per-zip-code timezone resolution
- `django-axes` for login brute-force protection
- Live data via The Odds API, CFBD, CBBD, ESPN (optional, env-driven)
- `requests` for API calls, `cfbd`/`cbbd` Python SDKs
- `openai` for AI Insight generation (OpenAI Chat Completions API)

## Project Structure

```
brotherwillies/
  manage.py
  brotherwillies/          # Django project settings
    settings.py
    urls.py
    wsgi.py
    middleware.py           # UserTimezoneMiddleware (zip-code-based TZ)
  apps/
    core/                  # Base layout, home page, help, AI insights, SiteConfig
      services/
        ai_insights.py     # AI-powered game explanation engine (OpenAI)
      templatetags/
        tz_extras.py       # {% tz_abbr %} template tag
    accounts/              # Auth, profile, preferences, My Model, My Stats, User Guide, What's New
      timezone_lookup.py   # zip_to_timezone() via zipcodes library
    cfb/                   # College football: models, services, views
    cbb/                   # College basketball: models, services, views
    golf/                  # Golf: models, golfer search, placeholder pages
    parlays/               # Parlay builder/scoring (analytics only)
    analytics/             # Snapshots, CLV tracking, interaction logging
    datahub/               # Seed loader + live data ingestion
      providers/             # Multi-sport provider architecture
        cbb/                 # CBB schedule, odds, injuries providers
        cfb/                 # CFB schedule, odds, injuries providers
        golf/                # Golf schedule, odds providers
      team_colors.py       # CFB + CBB team primary colors by slug
    mockbets/              # Mock bet simulation (no real money, decision analytics)
    feedback/              # Partner-only feedback system
  static/
    css/style.css          # Global dark theme + responsive styles
    css/auth.css           # Standalone 2-column auth layout
    js/app.js              # Vanilla JS (help modal, nav, accordions, etc.)
  templates/
    base.html              # Base layout with header, bottom nav, footer
    includes/              # Reusable partials (help modal, nav, etc.)
```

---

## Django Apps

| App | Purpose |
|-----|---------|
| `core` | Base layout, home page, Value Board, help component, AI Insight, SiteConfig |
| `accounts` | Auth, profile, preferences, My Model, presets, My Stats, performance, User Guide, What's New |
| `cfb` | Conferences, teams, games, odds, injuries, house/user model services |
| `cbb` | College basketball: same structure as CFB |
| `golf` | Golf events, golfers, odds, golfer search API |
| `parlays` | Parlay builder/scoring, correlation detection (analytics only) |
| `analytics` | ModelResultSnapshot, UserGameInteraction, CLV tracking |
| `mockbets` | Mock bet simulation — place/track/review simulated bets, settlement engine, decision analytics |
| `feedback` | Partner-only feedback system (submit, review, status pipeline) |
| `datahub` | Seed loader, live data ingestion, multi-sport provider layer |

---

## Key URLs

| Route | Description |
|-------|-------------|
| `/` | Home (dashboard) |
| `/value/` | Unified Value Board with sport tabs |
| `/value/?sport=cbb` | Value Board — CBB tab |
| `/value/?sport=cfb` | Value Board — CFB tab |
| `/value/?sport=golf` | Value Board — Golf tab |
| `/cfb/` | CFB hub |
| `/cfb/conference/<slug>/` | Conference dashboard |
| `/cfb/game/<uuid>/` | CFB game detail |
| `/cbb/` | CBB hub |
| `/cbb/game/<uuid>/` | CBB game detail |
| `/golf/` | Golf hub |
| `/accounts/login/` | Login |
| `/accounts/logout/` | Logout |
| `/accounts/password-reset/` | Password reset flow |
| `/profile/` | Profile |
| `/profile/preferences/` | Preferences |
| `/profile/my-model/` | My Model tuning |
| `/profile/presets/` | Model presets |
| `/profile/my-stats/` | Personal Statistics |
| `/profile/performance/` | Model Performance |
| `/profile/user-guide/` | User Guide |
| `/profile/whats-new/` | What's New (release history) |
| `/parlays/` | Parlay hub |
| `/parlays/new/` | Build parlay |
| `/parlays/<uuid>/` | Parlay detail |
| `/mockbets/` | My Mock Bets dashboard |
| `/mockbets/place/` | Place mock bet (AJAX) |
| `/mockbets/<uuid>/` | Mock bet detail |
| `/feedback/console/` | Feedback dashboard (partner-only) |
| `/api/ai-insight/<sport>/<uuid>/` | AI Insight AJAX endpoint |

---

## Legal / Trust Guardrails

- **Analytics only** — NO betting advice, picks, "best bets", "locks"
- **Neutral language** — analyzed, evaluated, modeled (never guarantee/profit)
- **Footer disclaimer on every page:** "For informational and entertainment purposes only. No guarantees. Check local laws."
- **Stats pages disclaimer:** "Past performance does not predict future outcomes."
- **Mock bets are simulated** — no real money, all figures labeled "Simulated"
- **No storage of:** real bet amounts, profit, winnings

---

## Responsive Design (REQUIRED)

**Breakpoints:**
- Phone: 1 column (`max-width: 480px`)
- Tablet: 2 columns (`max-width: 768px`)
- Desktop: 3 columns (`min-width: 769px`)

**Rules:**
- Mobile-first defaults
- Tap targets >= 44px
- `font-size: 16px` min on inputs (prevents iOS auto-zoom)
- No fixed widths — use `max-width`, `%`, `vw`
- Bottom nav padding so content isn't hidden
- Verify layout at 375px width (iPhone SE)

---

## Feature Gating (Monetization Scaffold)

Tiers: `free`, `pro`, `elite` (no payments implemented)

| Feature | Free | Pro | Elite |
|---------|------|-----|-------|
| Value Board | Auth-gated (top 3 for anon) | Full | Full |
| Presets | 1 max | Multiple | Multiple |
| Parlay Scoring | Yes | Yes | Yes |

Helper: `user_has_feature(user, feature_key) -> bool`

---

## Git / Deployment

**GitHub repo:** `djenkins452/brotherwillies`
**Branch:** `main`
**Remote:** `git@ssh.github.com:djenkins452/brotherwillies.git` (SSH port 443)

**Production hosts:** `brotherwillies.com`, `www.brotherwillies.com`
**Hosting:** Railway.com (auto-deploys from `main` branch)

### Railway Constraints

- **No shell/CLI access** — cannot run `manage.py` commands directly on Railway
- **Everything runs via custom start command** (set in Railway Settings → Deploy):
  ```
  python manage.py migrate --noinput && python manage.py ensure_superuser && python manage.py ensure_seed && python manage.py collectstatic --noinput && gunicorn brotherwillies.wsgi --bind 0.0.0.0:$PORT
  ```
- **No Procfile** — Railpack treats env var references as required build-time secrets
- **No `DJANGO_SUPERUSER_*` env vars** — Railpack's static scanner fails the build
- **Idempotent commands in:** `apps/datahub/management/commands/`
  - `ensure_superuser.py` — creates superuser if not exists
  - `ensure_seed.py` — seeds data + runs live ingestion if enabled
  - `refresh_data.py` — refreshes all data (designed for cron)
  - `capture_snapshots.py` — captures pre-game predictions
  - `resolve_outcomes.py` — resolves outcomes for completed games
- **Mock bet settlement:** `apps/mockbets/management/commands/`
  - `settle_mockbets.py` — settles pending mock bets for completed games

---

## Environment Variables

```
SECRET_KEY=                      # Django secret key
DEBUG=true                       # Debug mode
ALLOWED_HOSTS=                   # Comma-separated hostnames
DATABASE_URL=                    # PostgreSQL URL (prod)
LIVE_DATA_ENABLED=false          # Master switch for live data
LIVE_CBB_ENABLED=false           # Per-sport toggles
LIVE_CFB_ENABLED=false
LIVE_GOLF_ENABLED=false
ODDS_API_KEY=                    # The Odds API key
CFBD_API_KEY=                    # CollegeFootballData.com key
CBBD_API_KEY=                    # CollegeBasketballData.com key
OPENAI_API_KEY=                  # OpenAI API key (for AI Insight)
OPENAI_MODEL=gpt-4.1-mini       # OpenAI model
```

AI temperature and max tokens are admin-configurable at `/bw-manage/` → Core → Site Configuration (no env var needed).

---

## Reference Documentation

| Doc | Purpose |
|-----|---------|
| `docs/changelog.md` | Change history |
| `docs/master_prompt.md` | Full project specification (preserved for reference) |
| `/profile/user-guide/` | User-facing guide (template: `templates/accounts/user_guide.html`) |
| `/profile/whats-new/` | Release history (template: `templates/accounts/whats_new.html`) |

---

*Last updated: 2026-02-08*
