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
**Admin superuser:** `admin` / `brotherwillies` (http://localhost:8000/admin/)

---

## Tech Stack

- Django 5.x with built-in auth (sessions, no OAuth)
- SQLite (dev) / PostgreSQL (prod-ready)
- Django templates + CSS + vanilla JS (no React)
- Dark theme, card-based, mobile-first UI
- No external APIs yet (deterministic seed data)

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
    core/                  # Base layout, home page, help component
      templatetags/
        tz_extras.py       # {% tz_abbr %} template tag
    accounts/              # Auth, profile, preferences, My Model, My Stats
      timezone_lookup.py   # US zip prefix → IANA timezone mapping
    cfb/                   # College football: models, services, views
    cbb/                   # College basketball: models, services, views
    golf/                  # Golf MVP scaffolding
    parlays/               # Parlay builder/scoring (analytics only)
    analytics/             # Snapshots, CLV tracking, interaction logging
    datahub/               # Seed loader, future data ingestion
  static/
    css/style.css          # Global dark theme + responsive styles
    js/app.js              # Minimal vanilla JS (help modal, nav, etc.)
  templates/
    base.html              # Base layout with header, bottom nav, footer
    includes/              # Reusable partials (help modal, nav, etc.)
```

---

## Django Apps

| App | Purpose |
|-----|---------|
| `core` | Base layout, home page, global help component |
| `accounts` | Register/login/logout, profile, preferences, My Model tuning, presets, My Stats, performance |
| `cfb` | Conferences, teams, games, odds, injuries, house/user model services, CFB Value Board |
| `cbb` | College basketball: conferences, teams, games, odds, injuries, model services, CBB Value Board |
| `golf` | MVP scaffold (models + placeholder pages) |
| `parlays` | Parlay builder/scoring, correlation detection (analytics only) |
| `analytics` | ModelResultSnapshot, UserGameInteraction, CLV tracking |
| `datahub` | seed_demo command, future data ingestion |

---

## Key URLs

| Route | Description |
|-------|-------------|
| `/` | Home (dashboard preview) |
| `/value/` | Value Board (top 3 for anon, full for auth) |
| `/cfb/` | CFB hub (conferences + upcoming) |
| `/cfb/conference/<slug>/` | Conference dashboard |
| `/cfb/game/<uuid>/` | CFB game detail |
| `/cbb/` | CBB hub (conferences + upcoming) |
| `/cbb/value/` | CBB Value Board |
| `/cbb/conference/<slug>/` | CBB conference dashboard |
| `/cbb/game/<uuid>/` | CBB game detail |
| `/golf/` | Golf hub (placeholder) |
| `/accounts/register/` | Register |
| `/accounts/login/` | Login |
| `/accounts/logout/` | Logout |
| `/profile/` | Profile (personal info) |
| `/profile/preferences/` | Preferences (favorites, filters, zip code/timezone) |
| `/profile/my-model/` | My Model tuning |
| `/profile/presets/` | Model presets |
| `/profile/my-stats/` | Personal Statistics |
| `/profile/performance/` | Model Performance |
| `/parlays/` | Parlay hub |
| `/parlays/new/` | Build parlay |
| `/parlays/<uuid>/` | Parlay detail |

---

## Legal / Trust Guardrails

- **Analytics only** - NO betting advice, picks, "best bets", "locks"
- **Neutral language** - analyzed, evaluated, modeled (never guarantee/profit)
- **Footer disclaimer on every page:** "For informational and entertainment purposes only. No guarantees. Check local laws."
- **Stats pages disclaimer:** "Past performance does not predict future outcomes."
- **No storage of:** bet amounts, profit, winnings

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
- No fixed widths - use `max-width`, `%`, `vw`
- Bottom nav padding so content isn't hidden
- Verify layout at 375px width (iPhone SE)

---

## Model Services (cfb/services/ and cbb/services/)

1. **House model** (fixed, versioned "v1")
   - `compute_house_win_prob(game, latest_odds, injuries, context) -> float`
2. **User model** (recompute with user weights)
   - `compute_user_win_prob(game, user_config) -> float`
3. **Edge calculations**
   - `house_edge = house_prob - market_prob`
   - `user_edge = user_prob - market_prob`
   - `delta = user_prob - house_prob`
4. **Data confidence** (low/med/high based on odds recency + injury completeness)

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

**On Task Completion:**
1. Update changelog (`docs/changelog.md`)
2. Commit changes
3. Push to GitHub:
   ```bash
   GIT_SSH_COMMAND="ssh -p 443" git push git@ssh.github.com:djenkins452/brotherwillies.git main
   ```

**Production hosts:** `brotherwillies.com`, `www.brotherwillies.com`
**Hosting:** Railway.com (auto-deploys from `main` branch)

### Railway Constraints

- **No shell/CLI access** — cannot run `manage.py` commands directly on Railway
- **Everything runs via custom start command** (set in Railway Settings → Deploy):
  ```
  python manage.py migrate --noinput && python manage.py ensure_superuser && python manage.py ensure_seed && python manage.py collectstatic --noinput && gunicorn brotherwillies.wsgi --bind 0.0.0.0:$PORT
  ```
- **No Procfile** — Railpack scans Procfile commands and treats env var references as required build-time secrets, causing build failures. Custom start command avoids this.
- **No `DJANGO_SUPERUSER_*` env vars** — Railpack's static scanner detects these and fails the build. Superuser credentials are hardcoded in `ensure_superuser.py` (`admin` / `brotherwillies`).
- **Idempotent commands live in:** `apps/datahub/management/commands/`
  - `ensure_superuser.py` — creates superuser if not exists (hardcoded creds)
  - `ensure_seed.py` — runs seed_demo only if no CFB and CBB Conference rows exist

---

## Build Progress

| Step | Description | Status |
|------|-------------|--------|
| 0 | Initialize project (venv, requirements) | COMPLETE |
| 1 | Django project + apps + settings + base templates + static | COMPLETE |
| 2 | Accounts/auth (register, login, profile, models) | COMPLETE |
| 3 | CFB models + migrations + admin | COMPLETE |
| 4 | Model services (house, user, edge, confidence) | COMPLETE |
| 5 | CFB pages + Value Board | COMPLETE |
| 6 | My Model tuning + presets | COMPLETE |
| 7 | Personal Statistics + Model Performance | COMPLETE |
| 8 | Context-aware help system | COMPLETE |
| 9 | Golf scaffolding | COMPLETE |
| 10 | Parlays app | COMPLETE |
| 11 | Analytics snapshots | COMPLETE |
| 12 | Seed demo data | COMPLETE |
| 13 | Final polish + verification | COMPLETE |
| 14 | CBB app (college basketball) | COMPLETE |
| 15 | Season-aware dashboard + offseason banners | COMPLETE |
| 16 | User timezone via zip code | COMPLETE |

---

## Reference Documentation

| Doc | Purpose |
|-----|---------|
| `docs/changelog.md` | Change history |
| `docs/master_prompt.md` | Full project specification (preserved for reference) |

---

*Last updated: 2026-02-08*
