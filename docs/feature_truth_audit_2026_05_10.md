# Feature Truth Audit — 2026-05-10 (Phase 1A)

**Scope:** every weight declared in `HOUSE_WEIGHTS` and `compute_user_win_prob` across the four team-sport `model_service.py` files, plus a static-field audit of `Team.rating` updaters.

**Method:** read each `_score` / `_compute_win_prob` body and trace which keys are read off the `weights` dict; cross-reference declared `HOUSE_WEIGHTS` keys.

**Goal:** code-honesty. Make the model say what it does and do what it says. No new features added. No score formula changed.

---

## 1. Per-sport phantom weight inventory

### MLB (`apps/mlb/services/model_service.py`)

| Weight key | Declared in `HOUSE_WEIGHTS`? | Read by `_score`? | Status |
|---|---|---|---|
| `rating` | ✅ | ✅ (× 0.35) | active |
| `pitcher` | ✅ | ✅ (× 0.65) | active |
| `hfa` | ✅ | ✅ (× 2.5; zeroed when neutral) | active |
| `injury` | ✅ | ❌ | **PHANTOM** |

`compute_user_win_prob` also assembles `'injury': user_config.injury_weight` into the dict but `_score` never reads it. The `UserModelConfig.injury_weight` field exists on disk and is editable on the My Model page — moving the slider has zero effect on MLB output.

### College Baseball (`apps/college_baseball/services/model_service.py`)

| Weight key | Declared | Read by `_score`? | Status |
|---|---|---|---|
| `rating` | ✅ | ✅ (× 0.35) | active |
| `pitcher` | ✅ | ✅ (× 0.65) | active |
| `hfa` | ✅ | ✅ (× 2.0) | active |
| `injury` | ✅ | ❌ | **PHANTOM** |

Mirror of MLB — same phantom, same UX consequence.

### CFB (`apps/cfb/services/model_service.py`)

| Weight key | Declared | Read by `_compute_win_prob`? | Status |
|---|---|---|---|
| `rating` | ✅ | ✅ | active |
| `hfa` | ✅ | ✅ (× 3.0) | active |
| `injury` | ✅ | ✅ (real `_injury_adjustment`) | active |
| `recent_form` | ✅ | ❌ | **PHANTOM** |
| `conference` | ✅ | ❌ | **PHANTOM** |

`UserModelConfig.recent_form_weight` and `UserModelConfig.conference_weight` exist as DB fields and editable form sliders — both no-op the score formula.

### CBB (`apps/cbb/services/model_service.py`)

| Weight key | Declared | Read by `_compute_win_prob`? | Status |
|---|---|---|---|
| `rating` | ✅ | ✅ | active |
| `hfa` | ✅ | ✅ (× 3.5) | active |
| `injury` | ✅ | ✅ (real `_injury_adjustment`) | active |
| `recent_form` | ✅ | ❌ | **PHANTOM** |
| `conference` | ✅ | ❌ | **PHANTOM** |

Same shape as CFB.

---

## 2. Static field audit — `Team.rating`

**Models with the field:**
- `apps.cfb.Team.rating` — `FloatField(default=50.0)`
- `apps.cbb.Team.rating` — `FloatField(default=50.0)`
- `apps.mlb.Team.rating` — `FloatField(default=50.0)`
- `apps.college_baseball.Team.rating` — `FloatField(default=50.0)`

**Updaters across the codebase:**

```
$ grep -rn "team.rating\s*=\|\.rating\s*=" apps/datahub/providers/ apps/datahub/management/commands/
apps/datahub/providers/mlb/pitcher_stats_provider.py:187:    pitcher.rating = rating
```

That's it — and `pitcher.rating` is the **pitcher** rating (`StartingPitcher.rating`), not the team rating. **No provider, ingestion command, or scheduled job updates `Team.rating` for any sport.** The field is set once (presumably at seed) and never changes.

This is the foundational issue Phase 1B is set up to fix via Elo. `Team.elo_rating` has a real updater pipeline (`update_elo_ratings`, `rebuild_elo_ratings`, `process_game`); flipping `USE_DYNAMIC_RATINGS` swaps `Team.rating` out for `Team.elo_rating` projected onto the legacy scale.

---

## 3. Pitcher rating freshness

| Field | Updater | Notes |
|---|---|---|
| `StartingPitcher.rating` | `apps.datahub.providers.mlb.pitcher_stats_provider:187` | derived from ERA/WHIP/K-per-9 |
| `StartingPitcher.era`/`whip`/`k_per_9` | same provider | written before rating |

Pitcher ratings ARE updated when the provider runs. Default value is 50.0 — a pitcher with `rating == 50.0` is a strong signal that stats haven't been ingested yet (or the pitcher is new with no body of work). The Model Inventory page surfaces this with the "default — no stats yet" chip.

---

## 4. Decision

Phase 1A is "no behavior change." Two surgical, behavior-preserving moves are made:

1. **Comment phantom keys in place.** `HOUSE_WEIGHTS` retains every key for backward compatibility (admin pages, serialization, anything that introspects the dict). Each phantom gets an inline `# UNUSED — see docs/feature_truth_audit_2026_05_10.md` comment so a future reader sees the truth immediately.

2. **Lock the truth with a regression test.** A new test in `apps/core/test_feature_truth_audit.py` asserts which weight keys each sport's score formula actually consumes. Future code that adds a new key but forgets to wire it (or removes a key it secretly relied on) breaks the test.

Phantom-weight remediation in the score formula itself (i.e., actually wiring up an injury term, recent_form, or conference) is **out of scope for Phase 1A** — that's the Phase 1C "add the missing features that already have data" work that the engineering report sequenced explicitly *after* the dynamic-rating cutover (Phase 1B).

---

## 5. Follow-ups (not done in Phase 1A)

- **F2 — wire MLB injury term.** `InjuryImpact` rows are collected per game; the data is sitting there unused. Phase 1C in the roadmap.
- **F3 — pitcher recent form.** `era`/`whip` are populated but only feed into `pitcher.rating` (a season-aggregate). Phase 1C.
- **CFB/CBB recent_form / conference.** Defer to Phase 2; both involve provider work that doesn't exist today (no recent-form provider, no conference-strength feed).
- **My Model UX.** The `injury_weight` slider for MLB users is a UI lie until F2 lands. Same for `recent_form_weight` / `conference_weight` on CFB/CBB. Resolution: either implement the underlying logic (Phase 1C / Phase 2) or hide the slider per sport. Tracked here, not patched in this audit.
