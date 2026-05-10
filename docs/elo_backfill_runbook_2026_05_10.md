# Elo Backfill Runbook — 2026-05-10 (Phase 1B)

**Purpose:** the authoritative procedure for bringing Elo ratings online
for MLB. Step-by-step. Reversible. Each step is a one-liner; the
complexity is all in the documentation around what each step does and
when it's safe to skip.

**Audience:** an operator (current author or future Claude session)
following the Phase 1B plan.

---

## Properties of the backfill

The backfill is **deterministic, reproducible, and idempotent** — verified
by `apps/core/test_elo_service.py::RebuildIdempotenceTests`.

| Property | Mechanism |
|---|---|
| Deterministic | Output is a pure function of `(input games, K_FACTORS, HFA_ELO, MAX_MARGIN, INITIAL_RATING)`. No wall-clock, no randomness, no external API. |
| Reproducible | Source of truth is `mlb.Game` rows with `status='final'` and both scores populated. Same DB → same Elo state. |
| Idempotent | `rebuild_elo_ratings` wipes Elo state for the target sport before replaying; running it twice converges. `update_elo_ratings` skips games already in `TeamEloHistory`. |

**Default values:**
- Initial rating: `INITIAL_RATING = 1500.0` (standard Elo baseline). Through `elo_to_legacy_scale` this maps to legacy `50.0` — exactly the `Team.rating` default — so a freshly-rebuilt team produces identical model output to one that's never been rebuilt.
- K-factor (MLB): `K_FACTORS['mlb'] = 4.0`. Long season → small K so single-game movement is bounded.
- Home-field advantage (MLB): `HFA_ELO['mlb'] = 24.0`. Calibrated to ~54% league-wide home win rate.
- Margin: not used. `MARGIN_AWARE_SPORTS = {'cfb', 'cbb'}`. MLB ratings update on win/loss only.

**Regression strategy:** none. Ratings carry across seasons untouched. This is a deliberate Phase 1B simplification; cross-season regression-to-mean is a Phase 2 design question.

**Offseason handling:** none. If no games are played, no rows are processed and the elo_rating field stays at whatever it was at the last final game. No decay, no drift.

---

## Procedure — initial production backfill

The state we want to reach: every MLB team has an `elo_rating` set to the value implied by walking every season-to-date final game in chronological order.

Pre-flight is local (or staging — we don't have a staging env, so prod-with-flag-off):

### 1. Verify data quality before backfill

```bash
# How many final games does the source-of-truth contain?
python manage.py shell -c "
from apps.mlb.models import Game
total = Game.objects.filter(status='final').count()
with_scores = Game.objects.filter(
    status='final', home_score__isnull=False, away_score__isnull=False,
).count()
print(f'Final games: {total}; with both scores: {with_scores}')
"
```

If `total != with_scores`, we have final games with missing scores — investigate and either patch the data or accept the backfill will silently skip them. Skip rate is reported in the rebuild output.

### 2. Run the rebuild (one-time)

```bash
python manage.py rebuild_elo_ratings --sport mlb
```

Expected output:
```
[mlb] Reset 30 teams; cleared TeamEloHistory rows.
[mlb] Rebuild complete — processed=N, skipped=M.
```

`processed` = games that produced an Elo update.
`skipped` = ties + games that lost to the idempotence guard (which can't fire here because we just reset).

### 3. Verify state

```bash
python manage.py shell -c "
from apps.mlb.models import Team
from apps.analytics.models import TeamEloHistory

teams_with_elo = Team.objects.filter(elo_rating__isnull=False).count()
total_teams = Team.objects.count()
history_count = TeamEloHistory.objects.filter(sport='mlb').count()

print(f'Teams with Elo set: {teams_with_elo} / {total_teams}')
print(f'EloHistory rows: {history_count}')

# Sanity: top 5 ratings, bottom 5
top = Team.objects.filter(elo_rating__isnull=False).order_by('-elo_rating')[:5]
bot = Team.objects.filter(elo_rating__isnull=False).order_by('elo_rating')[:5]
print('Top 5:', [(t.name, round(t.elo_rating, 1)) for t in top])
print('Bot 5:', [(t.name, round(t.elo_rating, 1)) for t in bot])
"
```

Spot checks:
- Total Elo history rows ≈ `2 × processed` from the rebuild output (two rows per game).
- Ratings are in a plausible range — for MLB after a partial season, expect roughly `1400–1600`.
- The top of the list should match the operator's eyeball view of the league standings; if it's wildly off, score data is suspect.

### 4. Done — flag remains OFF

`USE_DYNAMIC_RATINGS=False` stays unchanged. Production behavior is identical to before the backfill. The new Elo ratings sit on `Team.elo_rating` waiting to either:
- Power the shadow-mode comparison data on every new MLB recommendation (`BettingRecommendation.shadow_alt_data` populated by Phase 1B Task 5), or
- Become the live ratings the moment the flag is flipped (Phase 1B Task 8 cutover).

---

## Procedure — ongoing maintenance

Already wired. `apps/datahub/management/commands/refresh_data.py` (the cron entrypoint) calls `update_elo_ratings` after `resolve_outcomes` on every cycle. As new games finalize, their Elo updates land in `TeamEloHistory` and `Team.elo_rating` advances.

**While the flag is OFF:** `update_elo_ratings` runs silently in the background; nothing user-facing changes. The shadow-mode logging on `BettingRecommendation` reads the latest `Team.elo_rating` to compute the alt-mode pick.

**Failure handling:** the cron wraps `update_elo_ratings` in a try/except that marks the run as partial but does not abort the rest of `refresh_data`. Shadow data goes stale until the next successful cycle; live recommendations are unaffected.

---

## Procedure — emergency rebuild (when something looks wrong)

The standard reset:

```bash
python manage.py rebuild_elo_ratings --sport mlb
```

This wipes `mlb` Elo state and replays from scratch. Other sports' Elo (if any) are untouched — `reset_sport` is sport-scoped. Inside an atomic transaction, so a partial failure rolls everything back.

When to use:
- After a data correction that affects historical scores.
- After changing K-factor, HFA_ELO, or `ELO_TO_LEGACY_DIVISOR`.
- After investigating ratings that look off — rebuild to see if it converges to a sane state vs the suspicious one.

---

## Procedure — rollback

If Elo is causing problems and we need to disable it:

1. **If the flag is ON:** set `USE_DYNAMIC_RATINGS=False` (Railway env var). Effective immediately on next request — `team_rating_for_model` will read `team.rating` again. No restart needed.
2. **If we want to wipe Elo state entirely** (rare — only if we suspect bad data is poisoning shadow-mode comparisons):
   ```bash
   python manage.py shell -c "
   from apps.core.services.elo_service import reset_sport
   reset_sport('mlb')
   "
   ```
   This sets every MLB team's `elo_rating = None` and deletes the sport's `TeamEloHistory`. Subsequent calls to `team_rating_for_model` (with the flag on) fall back to static. Subsequent recommendations record `elo_available=False` in shadow data.

3. **To remove shadow logging temporarily:** set `shadow_active_mode=''` and `shadow_alt_data={}` would require code change — not a runbook step. The shadow data is best left in place; if `elo_available=False` it's already self-describing.

---

## Procedure — the cutover (Phase 1B Task 8)

Documented for completeness. NOT done yet. This step requires Phase 1B Task 7's calibration impact review to land first.

1. Run the latest rebuild (above) so ratings are current.
2. Run a full Static backtest from the analytics page (`/analytics/backtest/`).
3. Run a full Elo backtest from the same page.
4. Compare ROI / CLV / calibration in the side-by-side cards. Decision criteria documented in the production-readiness report.
5. If go: set `USE_DYNAMIC_RATINGS=True` in Railway env vars. Effective on next request.
6. Monitor for one slate. If anything looks wrong, the rollback above is one env-var change.

---

## Procedure — verification queries

Quick checks an operator can run anytime:

```bash
# Are Elo updates happening?
python manage.py shell -c "
from apps.analytics.models import TeamEloHistory
from django.utils import timezone
from datetime import timedelta
recent = TeamEloHistory.objects.filter(
    sport='mlb',
    captured_at__gte=timezone.now() - timedelta(hours=24),
).count()
print(f'EloHistory rows in last 24h: {recent}')
"

# Is shadow data being captured?
python manage.py shell -c "
from apps.core.models import BettingRecommendation
recent = BettingRecommendation.objects.filter(
    sport='mlb', shadow_active_mode__in=('static', 'elo'),
).order_by('-created_at')[:3]
for r in recent:
    print(r.created_at, r.shadow_active_mode, r.shadow_alt_data.get('elo_available'))
"
```

---

## Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-10 | Carry ratings across seasons (no regression) | Phase 1B simplicity; one-season scope |
| 2026-05-10 | Run `update_elo_ratings` every cron cycle (flag-agnostic) | Keeps shadow data current; zero flag-on startup gap |
| 2026-05-10 | MLB-only shadow logging | Phase 1B scope per spec |
| 2026-05-10 | `force_use_dynamic` context manager for alt-mode compute | Process-local override; safe under analytics-page no-concurrency guard |
