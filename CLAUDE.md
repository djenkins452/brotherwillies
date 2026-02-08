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
    core/                  # Base layout, home page, help component, AI insights
      services/
        ai_insights.py     # AI-powered game explanation engine (OpenAI)
      templatetags/
        tz_extras.py       # {% tz_abbr %} template tag
    accounts/              # Auth, profile, preferences, My Model, My Stats
      timezone_lookup.py   # zip_to_timezone() via zipcodes library
    cfb/                   # College football: models, services, views
    cbb/                   # College basketball: models, services, views
    golf/                  # Golf MVP scaffolding
    parlays/               # Parlay builder/scoring (analytics only)
    analytics/             # Snapshots, CLV tracking, interaction logging
    datahub/               # Seed loader + live data ingestion
      providers/             # Multi-sport provider architecture
        base.py              # Abstract provider (fetch/normalize/persist)
        registry.py          # Provider lookup by sport/type
        client.py            # Shared HTTP client (rate limiting, retries)
        name_utils.py        # Team/player name normalization + aliases
        cbb/                 # CBB schedule, odds, injuries providers
        cfb/                 # CFB schedule, odds, injuries providers
        golf/                # Golf schedule, odds providers
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
| `datahub` | Seed loader, live data ingestion, multi-sport provider layer |

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
| `/accounts/register/` | Register (DISABLED — route removed) |
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
| `/api/ai-insight/<sport>/<uuid>/` | AI Insight AJAX endpoint (login required) |

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
  - `ensure_seed.py` — runs seed_demo if no Conference rows exist, then runs live data ingestion if enabled
  - `refresh_data.py` — refreshes schedule/odds/injuries + captures snapshots + resolves outcomes (designed for cron)
  - `capture_snapshots.py` — captures house model predictions for games within 24h of start
  - `resolve_outcomes.py` — resolves final_outcome + closing line for completed games

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
| 17 | Security hardening & registration disabled | COMPLETE |
| 18 | Live data ingestion (CBB → Golf → CFB) | COMPLETE |
| 19 | Analytics pipeline (snapshots, scores, outcomes, CLV, performance) | COMPLETE |
| 20 | AI Insight engine (OpenAI-powered game explanations + personas) | COMPLETE |

---

## Live Data Ingestion

**Architecture:** Multi-sport provider layer in `datahub/providers/`

**Data Sources:**
| Source | Purpose | Sports | Cost |
|--------|---------|--------|------|
| The Odds API | Odds (ML, spread, total) | CFB, CBB, PGA | Free (500 req/mo) |
| CBBD API | Schedules, scores, stats, lines | CBB | Free |
| CFBD API | Schedules, scores, stats, lines | CFB | Free (1K/mo) |
| ESPN Public API | Supplementary schedules, scores | All | Free, no key |

**Environment Toggles:**
```
LIVE_DATA_ENABLED=false          # Master switch (false = seed data only)
LIVE_CBB_ENABLED=false           # Per-sport toggles
LIVE_CFB_ENABLED=false
LIVE_GOLF_ENABLED=false
ODDS_API_KEY=                    # The Odds API key
CFBD_API_KEY=                    # CollegeFootballData.com key
CBBD_API_KEY=                    # CollegeBasketballData.com key
OPENAI_API_KEY=                  # OpenAI API key (for AI Insight feature)
OPENAI_MODEL=gpt-4.1-mini       # OpenAI model (default: gpt-4.1-mini)
```

**Management Commands:**
```bash
python manage.py ingest_schedule --sport=cbb|cfb|golf
python manage.py ingest_odds --sport=cbb|cfb|golf
python manage.py ingest_injuries --sport=cbb|cfb
python manage.py capture_snapshots --sport=cbb|cfb|all [--window-hours=24]
python manage.py resolve_outcomes --sport=cbb|cfb|all
python manage.py refresh_data          # Runs all of the above for enabled sports (cron use)
```

**Data Refresh:**
- `ensure_seed` runs live ingestion on every deploy (when `LIVE_DATA_ENABLED=true`)
- For ongoing refresh, set up a Railway cron job:
  1. Railway dashboard → project → "+ New" → "Cron Job"
  2. Same GitHub repo, start command: `python manage.py refresh_data`
  3. Schedule: `*/30 * * * *` (every 30 minutes)
  4. Copy all env vars from main service

**Dashboard Live Games:**
- Home page shows "Live Now" section for games with `status='live'`
- ESPN scoreboard fetches yesterday + today + 7 days to catch late-night games
- Game status lifecycle: `scheduled` → `live` → `final` (updated on each ingestion run)

---

## Analytics Pipeline

**Purpose:** Automatically capture model predictions, store game scores, resolve outcomes, and compute performance metrics.

**Cron pipeline order** (executed per sport in `refresh_data`):
1. `ingest_schedule` — updates game status (`scheduled` → `live` → `final`) and stores scores
2. `ingest_odds` — captures latest odds snapshots
3. `ingest_injuries` — updates injury reports
4. `capture_snapshots` — for games within 24h of start that have odds, creates `ModelResultSnapshot` with house model probability, market probability, and data confidence
5. `resolve_outcomes` — for completed games with scores, sets `final_outcome` (home win T/F) and `closing_market_prob` (last odds before kickoff/tipoff) on existing snapshots

**Key models:**

| Model | Location | Purpose |
|-------|----------|---------|
| `Game.home_score` / `Game.away_score` | `cfb/models.py`, `cbb/models.py` | Final scores (nullable, populated when game completes) |
| `ModelResultSnapshot` | `analytics/models.py` | Pre-game prediction capture + post-game outcome |

**ModelResultSnapshot fields:**
- `game` / `cbb_game` — FK to the game (one or the other)
- `market_prob` — market implied home win probability at capture time
- `house_prob` — house model's home win probability at capture time
- `house_model_version` — model version tag (currently `v1`)
- `data_confidence` — `high` / `med` / `low` based on odds freshness + injury data
- `closing_market_prob` — last market probability before game started (populated post-game)
- `final_outcome` — `True` = home won, `False` = away won (populated post-game)

**Score data sources:**
- **CFB**: CFBD API provides `home_points` / `away_points` directly
- **CBB**: ESPN API provides `competitor.score` per team

**Performance dashboard** (`/profile/performance/`) computes from snapshots:
- **Accuracy** — % of games where `house_prob > 0.5` matched `final_outcome`
- **Brier Score** — `mean((house_prob - actual)²)` — calibration quality (0 = perfect, 0.25 = coin flip)
- **By Sport** — separate accuracy/Brier for CFB and CBB
- **Trends** — last 7 days and 30 days
- **CLV** — did the market move toward the model? `abs(house_prob - market_prob) - abs(house_prob - closing_market_prob)`
- **Calibration** — bucket predictions by range (50-60%, 60-70%, etc.) and compare predicted vs actual win rates

**House model calculation** (`cfb/services/model_service.py`, `cbb/services/model_service.py`):
1. `rating_diff = (home_team.rating - away_team.rating) * rating_weight`
2. `hfa = 3.0 (CFB) or 3.5 (CBB) * hfa_weight` (0 if neutral site)
3. `injury_effect = Σ(impact_per_injury * injury_weight)` — LOW=0.01, MED=0.03, HIGH=0.06
4. `score = rating_diff + hfa + injury_effect`
5. `probability = 1 / (1 + exp(-score/15))` clamped to [0.01, 0.99]

**Data confidence rules:**
- `high` — odds < 2 hours old AND injuries exist
- `med` — odds < 12 hours old
- `low` — odds > 12 hours old or no odds

---

## AI Insight Engine

**Purpose:** Generate factual, expert-level AI summaries explaining why the house model and market agree or disagree on a game. The AI explains decisions — it does NOT make them.

**Architecture:**
- Service layer: `apps/core/services/ai_insights.py`
- AJAX endpoint: `GET /api/ai-insight/<sport>/<game_id>/` (login required)
- Triggered by "AI Insight" button on game detail pages (CFB + CBB)
- Returns JSON with `content` (formatted text) and `meta` (model, timing, prompt hash)

**How it works:**
1. `_build_context_from_game(game, data, sport)` — extracts structured facts from game object + computed data
2. `_build_system_prompt(persona)` — persona-specific tone + strict content rules
3. `_build_user_prompt(context)` — formats all facts as a structured text block
4. `generate_insight(game, data, sport, persona)` — calls OpenAI, returns result dict with error handling

**AI personas** (stored in `UserProfile.ai_persona`, configured in Preferences):

| Key | Tone |
|-----|------|
| `analyst` (default) | Neutral, professional, factual |
| `new_york_bookie` | Blunt, sharp, informal (profanity allowed) |
| `southern_commentator` | Calm, folksy, confident |
| `ex_player` | Direct, experiential |

**Fact variables passed to AI** (the AI ONLY uses these — no speculation):
- Game context: teams, sport, time, neutral site, status, ratings, conferences
- Market data: home/away win probabilities, spread, total, odds age
- House model: probabilities, edge, model version
- User model (optional): probability, edge
- Injuries: team, impact level, notes
- Line movement: direction
- Data confidence: level, missing data flags

**Response structure** (enforced by system prompt):
1. One-line summary
2. Market vs House snapshot
3. Key drivers (bullet list, ordered by impact)
4. Injury impact (if any)
5. Line movement context (if any)
6. What would change this view
7. Confidence & limitations

**Environment variables:**
```
OPENAI_API_KEY=           # Required (no key = graceful error message)
OPENAI_MODEL=gpt-4.1-mini  # Default (override with gpt-4.1 for higher quality)
```

**Logging:** Every request logs model used, prompt hash, response length, elapsed time. Errors log game ID, sport, and error message.

**IMPORTANT — Content rules:**
- AI uses ONLY facts provided in the prompt. No invented players, stats, or trends.
- Language must be neutral per legal guardrails: "analyzed", "modeled", "suggests" — never "guaranteed", "lock", "best bet"
- If data is missing or confidence is LOW, the AI states this explicitly
- AI output is NOT stored as fact and NOT cached (MVP)

---

## Context-Aware Help System

**Architecture:** Single modal template, string-keyed content, available on every page.

**Key file:** `templates/includes/help_modal.html` — all help content lives here in `{% if help_key == '...' %}` blocks.

**How it works:**
1. Each view passes `help_key` in its render context (e.g., `'help_key': 'performance'`)
2. `base.html` includes `help_modal.html` on every page
3. User taps "?" button → `toggleHelp()` JS function shows the modal
4. Modal renders the content block matching the current `help_key`

**Current help keys:**

| Key | Page | What it explains |
|-----|------|------------------|
| `home` | Dashboard | Live scores, edge, market %, house %, confidence, data sources |
| `value_board` | Value Board | House/user/delta edges, filters, confidence, data sources |
| `cfb_hub` | CFB Hub | Conferences, game cards, data sources |
| `cbb_hub` | CBB Hub | Same as CFB Hub for basketball |
| `game_detail` | Game Detail | Scores, status badges, probabilities, edge, line movement, injuries, confidence thresholds, data sources |
| `my_model` | My Model | Weight sliders, what each factor does, presets |
| `my_stats` | My Stats | Activity metrics, agreement rate, confidence distribution |
| `performance` | Performance | Accuracy, Brier score, sport breakdown, trends, CLV, calibration table, how snapshots work |
| `parlays` | Parlays | Combined probability, correlation risk, 10% haircut |
| `golf` | Golf | Tournament data sources |
| `profile` | Profile | Personal info, quick links |
| `preferences` | Preferences | Filters, favorite team, spread/odds/edge thresholds |
| (default) | Fallback | General site overview |

**IMPORTANT — When changing features, update the help content:**
- If you add/change a metric on the Performance page → update the `performance` block in `help_modal.html`
- If you add/change data displayed on Game Detail → update the `game_detail` block
- If you change how calculations work (model formula, confidence thresholds) → update ALL blocks that reference those calculations
- Help text should explain **where numbers come from** (which API, which formula), not just what they mean
- Keep language neutral per legal guardrails — "analyzed", "modeled", never "guaranteed"

---

## Reference Documentation

| Doc | Purpose |
|-----|---------|
| `docs/changelog.md` | Change history |
| `docs/master_prompt.md` | Full project specification (preserved for reference) |

---

*Last updated: 2026-02-08*
