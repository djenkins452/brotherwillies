# Brother Willies — Recommendation Quality Framework

**Promulgated:** 2026-05-14
**Status:** permanent governance document. Amendments follow the process at the end of `docs/architecture_laws.md`.

---

## Charter

> Prediction quality must improve through measurable evidence, not constant tuning.

Brother Willies is now mature enough that the dominant risk is not "missing intelligence" but "destabilizing tuning." This framework names the metrics we measure against, the discipline that governs when those metrics may justify a change, and the scoring system that tells us at a glance whether the engine is healthy without obsessing over daily W/L swings.

It does not change a single threshold. It defines the rules by which thresholds may be changed in the future.

The framework operates jointly with the four architecture laws (`docs/architecture_laws.md`):

- **Law 1** — signals are nudges, not drivers.
- **Law 2** — no signal without its evaluation slice.
- **Law 3** — analytics surfaces must be transparent about their scope.
- **Law 4** — do not overfit.

This framework is the operationalization of Laws 2 and 4.

---

## 1. Primary Success Metrics

### 1.1 The hierarchy

Eight metrics, ranked by their authority to drive tuning decisions:

| Rank | Metric | Type | Drives tuning? |
|---|---|---|---|
| 1 | **CLV trend** | leading | YES — primary tuning signal |
| 2 | **Calibration accuracy** | lagging but causal | YES — secondary tuning signal |
| 3 | **Edge-bucket performance** | segmentation | YES — for compression / gate decisions |
| 4 | **Favorite-size segmentation** | segmentation | YES — for sub-segment gates |
| 5 | **Long-term ROI** | lagging | YES — validation only, not target |
| 6 | **Market disagreement** | leading | NO — drives compression, not tuning |
| 7 | **Recommendation volume** | output | NO — diagnostic, never a target |
| 8 | **Win rate** | lagging, noisy | NO — display only |

The hierarchy is the contract. When two metrics conflict, the higher-ranked metric wins.

### 1.2 Per-metric definitions

#### #1 — CLV (Closing Line Value) trend

**What it measures.** The signed difference between the price we got at bet placement and the closing price for the same side. Positive CLV means the line moved toward our pick after we bet it — i.e., we got a better price than the market eventually settled on.

**Why it matters.** CLV is the **closest thing Brother Willies has to a leading indicator of edge**. It does not require games to settle. It correlates with positive expected value over time because beating the close means we systematically priced the game more accurately than the market at the moment of placement. Markets converge on truth as they mature; beating the close means we were closer to truth earlier.

**Leading or lagging?** **LEADING.** Observable per bet at game start. No outcome needed.

**Drives tuning?** **YES — the primary tuning signal.** When CLV+ rate decays over a meaningful window, that is the first place to investigate. Direction: declining CLV+ rate → model is misaligned with market → suspect rating staleness, calibration drift, or signal-overconfidence. Rising CLV+ rate → model is well-aligned → don't touch.

**Caveats.**
- Single-bet CLV is anecdote. Per-window CLV+ rate is signal.
- CLV is only meaningful when both opening and closing snapshots are from a trustworthy primary source (`odds_api`). ESPN-source CLV is intentionally suppressed because ESPN's "movement" is mostly capture artifact, not real market.
- A model that always defers to the market will have flat CLV near zero — not bad, not great. The goal is positive CLV at the slate level, which requires real edge generation, not consensus mimicry.

**Verdict:** confirms the user's hypothesis. CLV is the primary leading indicator.

#### #2 — Calibration Accuracy

**What it measures.** When the model predicts 60%, do actual outcomes hit 60% over a meaningful sample? The standard measure is the **Brier score** (mean squared error between predicted probability and binary outcome), per prediction-probability bucket.

**Why it matters.** A calibrated model can be trusted at any confidence level. An overconfident model predicts 80%-winners that only hit 65% — its edge math is wrong everywhere, not just on the obvious cases. Calibration drift is the second-most-likely cause when CLV trend goes south.

**Leading or lagging?** **Lagging but causal.** Requires settled outcomes. But once you have them, calibration tells you *which probability band* is broken — surgical signal.

**Drives tuning?** **YES — secondary tuning signal.** Specifically: if the 0.70–0.80 bucket consistently underperforms its predicted rate by > 5pp over ≥ 50 bets and 4 weeks, the sigmoid divisor or the clamp ceiling may want a retune. The change must isolate to that band's mechanism, not blanket the whole formula.

**Caveats.**
- Per-bucket samples accumulate slowly. Calibration retunes are quarterly at best.
- Calibration evaluated on the same data used to fit it is meaningless — held-out windows only.

#### #3 — Edge-bucket performance

**What it measures.** ROI, CLV, and win rate segmented by predicted edge size (`0-4` / `4-6` / `6-8` / `8+` pp). The relationship between edge size and outcome should be monotonic — bigger edges should win more.

**Why it matters.** If the `8+` edge bucket doesn't outperform `4-6`, our edge math is producing noise rather than signal. The "fake giant edge" pathology (`docs/calibration_impact_review_2026_05_10.md`) is observable here.

**Leading or lagging?** Lagging (needs settled outcomes), but the edge-vs-CLV relationship is partially leading.

**Drives tuning?** **YES — for edge-compression and gate decisions.** Specifically: if the `8+` bucket underperforms `4-6`, that justifies edge-realism compression (Phase 2C Task 7). If `0-4` outperforms `4-6` on CLV (perverse), the `MIN_EDGE` gate may want a reconsideration.

**Sample minimum:** 30 bets in the bucket over a ≥ 4-week window.

#### #4 — Favorite-size segmentation

**What it measures.** ROI/CLV by favorite-size band (`heavy_fav`, `mid_fav`, `short_fav`, `short_dog`, `mid_dog`, `long_dog`). The Phase 1A Task 3 instrumentation already collects this.

**Why it matters.** Persistent underperformance in one band signals miscalibration *for that regime*, not a global problem. The short-favorite leak (-149..+99) is the canonical example.

**Drives tuning?** **YES — for sub-segment gate decisions, NOT blanket retunes.** A band-specific gate is justified when:
- The band has ≥ 20 settled bets,
- Over a ≥ 4-week window,
- The band's ROI/CLV is > 1σ below the slate average,
- A named mechanism explains *why* (e.g., "sigmoid flat in the middle; stale ratings amplify the cluster"),
- The proposed gate is bounded and reversible.

**Caveats.** A short window with one bad band is variance. Five consecutive losing bets in `short_fav` proves nothing. Don't retune from "this week the dogs lost."

#### #5 — Long-term ROI

**What it measures.** Net profit / total stake, over a long window (≥ 100 settled bets, ideally ≥ 4 weeks).

**Why it matters.** It's the outcome that matters. But it's *lagging* and *noisy* — it tells you whether the system worked, after enough time has passed that you can't easily change anything that would have helped.

**Drives tuning?** **YES, but only as VALIDATION** — never as a target. Tuning to improve ROI directly is outcome-chasing (Law 4 violation). Tuning to improve CLV and calibration produces ROI as a consequence.

**Caveats.**
- ROI is highly sensitive to stake-distribution. Equal-stake ROI and Kelly-stake ROI tell different stories.
- Single-month ROI is variance. Multi-month is signal.

#### #6 — Market disagreement

**What it measures.** `|model_prob - fair_market_prob|`, in probability points. Distribution: mean, percentile bands, count above thresholds (5/10/15/20 pp — already aggregated in `/analytics/shadow-review/`).

**Why it matters.** Large disagreement is usually wrong. The market is the most sophisticated continuous prediction system on Earth. Brother Willies competes with it by reading inputs the market reads, plus a few inputs the market may underweight. Disagreement that exceeds a few percentage points without a *named* reason is a red flag.

**Leading or lagging?** **LEADING.** Observable at recommendation time.

**Drives tuning?** **NO directly.** Market disagreement is an *input* to edge-realism compression (Phase 2C Task 7) and a diagnostic for calibration drift. It is not itself a tuning target. Tightening the system to reduce disagreement is consensus-mimicry — kills CLV by definition. The correct response to "disagreement is high" is "investigate which named inputs the model is reading differently."

#### #7 — Recommendation volume

**What it measures.** Recommendations per slate.

**Why it matters.** Too few = under-utilization (gates too tight or model too conservative). Too many = lack of discrimination (gates too loose or edges illusory). A stable volume that fluctuates with slate size and quality is healthy.

**Drives tuning?** **NO.** Volume is the *output* of other decisions, never the target. Tuning to a volume target is reverse-engineering — set the inputs you trust, then accept whatever volume falls out.

**Diagnostic use only.** Volume that swings > 2σ week-over-week is worth investigating, but the investigation looks at *upstream causes* (data ingestion, calibration drift, regime change), not at the gate constants.

#### #8 — Win rate

**What it measures.** Fraction of bets that won.

**Why it matters.** It's what users emotionally track. It's also misleading on its own — 50% win rate at -110 is breakeven; 50% win rate at +130 is wildly profitable. Without odds context, win rate tells you nothing about edge quality.

**Drives tuning?** **NO. Display only.** A model that achieves 55% win rate at -200 prices is losing money on every bet on average; a model that achieves 48% win rate at +120 prices is winning. Win rate is downstream of odds-pick selection, which is downstream of edge math.

**Compliance:** the Health Score (§3) does NOT include win rate as a dimension. Tuning decisions that cite win rate as evidence violate Law 4 unless they also cite the joint distribution of win rate × odds.

### 1.3 What this hierarchy does NOT do

- It does not say "ignore lagging metrics." ROI and calibration are essential for *validating* that the leading-metric signals correspond to real edge.
- It does not say "tune aggressively to maximize CLV." CLV is the diagnostic; tuning is governed by §2.
- It does not say "every dimension is independent." Most dimensions are correlated (better calibration → better CLV → better ROI). The hierarchy ranks which to *act on* when they conflict, not which to *measure*.

---

## 2. Tuning Governance Rules

### 2.1 Sample size requirements

No tuning change ships until **all** applicable sample-size requirements are met for the supporting evidence.

| Decision type | Minimum settled bets | Minimum window | Notes |
|---|---|---|---|
| Calibration retune (sigmoid divisor, clamp, blend) | 100 | 4 weeks | Per affected probability bucket; bucket-specific evidence required |
| Global gate change (e.g., MIN_EDGE) | 100 | 4 weeks | Effect must be visible across multiple segments |
| Sub-segment gate (e.g., short_fav band) | 20 in segment | 4 weeks | Named mechanism required |
| Risk-flag rule addition / removal | 50 firings of the flag | 4 weeks | Flag's downstream effect must be measurable |
| Signal weight tuning (within a signal) | 50 with the signal active | 4 weeks | Signal must already be in production at the prior weight |
| New signal activation (flip flag from False to True) | n/a | shadow-mode demonstrated lift | See Phase 1B Task 8 production-readiness criteria |
| New signal deactivation | 30 with signal active showing degradation | 2 weeks | Lower bar because we're removing risk |

### 2.2 What counts as "statistically meaningful"

A signal is **meaningful** when:
- Sample meets the minimum above, AND
- Effect size exceeds the bucket's natural variance band (≥ 1σ for sub-segment moves; ≥ 2σ for global moves), AND
- The signal is consistent across the entire window (no single-week spike driving the average), AND
- The mechanism is named (no "the system feels off").

A signal is **noise** when:
- Sample below the minimum, OR
- Window < 4 weeks (calibration) / < 2 weeks (sub-segment), OR
- Effect inside the variance band, OR
- The signal is concentrated in one slate / one weekend / one team, OR
- No mechanism can be named.

The default response to noise is **wait**.

### 2.3 The four guardrail rules

Codified separately because they're invoked often:

1. **No tuning from one slate.** A single day cannot trigger a constant change, regardless of how bad or good it looked.
2. **No tuning from outcome variance alone.** Negative slates inside a positive CLV trend are variance, not signal. The Health Score (§3) is the gate.
3. **Calibration changes require evidence.** A specific calibration retune requires per-bucket evidence in the affected probability band, not blanket "the model feels off" reasoning.
4. **No optimization stacking.** When more than one change is in flight (e.g., signal addition + threshold tightening + clamp retune), attribution becomes impossible. One change at a time, with a stabilization window between changes (4 weeks for calibration; 1 week for sub-segment gates).

### 2.4 When the correct action is "wait"

| Situation | Correct action |
|---|---|
| Sample < minimum | Wait. Re-evaluate at sample = minimum. |
| Multiple changes in flight | Wait for the prior change's stabilization window. |
| Effect inside variance band | Wait. Re-evaluate at +2 weeks. |
| No mechanism identified | Investigate the *cause*. Do not patch the *symptom*. |
| Health Score (§3) ≥ 75 | Wait. Strong systems don't need cosmetic changes. |
| Operator emotionally distressed by recent results | Wait. Per Law 4, "looks bad" is not evidence. |

"Wait" is not laziness. "Wait" is the default response that prevents Brother Willies from accumulating fragile tunes that don't survive their own evidence.

---

## 3. Recommendation Health Score

A composite 0–100 score that compresses the system's state into a single signal an operator can scan in seconds.

### 3.1 Dimensions and weights

Seven dimensions, weights chosen to reflect the metric hierarchy in §1.

| Dimension | Weight | Healthy range | Source |
|---|---|---|---|
| **CLV+ rate** (last 30 settled, `odds_api` source only) | **25%** | ≥ 45% = healthy; ≥ 55% = strong | `BacktestRun.summary.clv_metrics.positive_clv_rate` |
| **Calibration accuracy** (Brier score, all settled in window) | **20%** | < 0.22 = healthy; < 0.20 = strong | computed from `calibration_curve` |
| **Edge realism** (sign of `8+` vs `4-6` ROI delta) | **15%** | `8+` ROI ≥ `4-6` ROI | `BacktestRun.summary.by_edge_bucket` |
| **Recommendation stability** (week-over-week volume variance) | **10%** | within 2σ of rolling mean | volume time series |
| **Market alignment** (avg `|final_prob − fair_market|`) | **10%** | < 0.08 mean = healthy; < 0.05 = strong | `shadow_review.avg_disagreement_active` |
| **Stale-odds rate** (% settled with no closing snapshot) | **10%** | < 5% = healthy | `BacktestRun.summary` stale-odds count |
| **Volume vs target** (rolling mean ± 2σ band) | **10%** | inside band | volume time series |

### 3.2 Per-dimension scoring formula

Each dimension scores 0–100 deterministically:

- **CLV+ rate:** linear from 0 (at 30% rate) to 100 (at 60% rate); clip outside.
- **Calibration:** linear from 0 (Brier = 0.30) to 100 (Brier = 0.18); inverted because lower is better.
- **Edge realism:** binary on the *sign* (100 if `8+` ≥ `4-6`, 0 if not), then scaled by the magnitude of the gap (the closer the bands are, the lower the score even when the sign is right).
- **Recommendation stability:** 100 inside 2σ, decaying linearly to 0 at 4σ.
- **Market alignment:** linear from 0 (mean disagreement = 0.20) to 100 (mean = 0.05).
- **Stale-odds rate:** linear from 100 (0%) to 0 (15%); clipped.
- **Volume vs target:** 100 inside 2σ, decaying linearly to 0 at 4σ.

Composite score = weighted average of the seven dimension scores.

### 3.3 Score bands and what they authorize

| Band | Score | Authorized action |
|---|---|---|
| **STRONG** | ≥ 75 | **Do not touch.** No constant changes regardless of individual segment fluctuation. Investigate degradations in any single dimension only if a named mechanism is identified. |
| **HEALTHY** | 50–74 | **Monitor.** Sub-segment changes allowed only with full §2 evidence. No global retunes. |
| **WATCH** | 25–49 | **Investigate.** Identify the binding dimension. Plan a targeted change. Do NOT ship changes from this band without the §2 evidence block. |
| **INTERVENE** | < 25 | **Act, narrowly.** Specific dimension has degraded; targeted action is justified. The action must isolate to the binding dimension; do not retune everything. |

The Health Score is the **gate** described in Law 4 — "Scores in `STRONG` or `HEALTHY` bands do not justify constant changes."

### 3.4 What the Health Score does NOT do

- It does not include win rate, ROI, or short-term outcomes. Those are downstream of the dimensions it does include.
- It does not auto-trigger changes. It authorizes investigation; humans propose changes; evidence justifies them; Law 4 governs them.
- It does not predict future performance. It describes the system's *current* observable state.
- It is not a leaderboard. There is no "win the day" signal — only "the system is operating within its design envelope."

### 3.5 Implementation note

Not implemented in this commit. Design is finalized; the implementation step (production service + view at `/analytics/health-score/` + tests) is gated behind an explicit operator authorization, separate from this governance document. The composite formula above is the contract that implementation must honor.

---

## 4. Segmentation Framework

### 4.1 Tier 1 — primary segments (drive tuning when §2 evidence applies)

| Segment | Boundary | Use |
|---|---|---|
| **Favorite-size band** | `heavy_fav` ≤ -200; `mid_fav` -199..-150; `short_fav` -149..+99; `short_dog` +100..+150; `mid_dog` +151..+250; `long_dog` +251..+ | Sub-segment gate decisions |
| **Edge bucket** | `0-4`, `4-6`, `6-8`, `8+` pp | Edge-realism compression decisions; calibration of MIN_EDGE |
| **Recommendation tier** | `elite`, `strong`, `standard` | Tier-cap policy (e.g., MAX_ELITE_PER_SLATE); edge math vs probability math attribution |
| **Pitcher data completeness** (MLB / college baseball) | `both_real`, `one_default`, `both_default`, `tbd`, `n_a` | Confidence policy; whether to recommend at all when pitcher data is degraded |

Each Tier-1 segment may have **its own gate** subject to §2 evidence. Independence is by design: a tightening that helps `short_fav` should not change `heavy_fav` behavior.

### 4.2 Tier 2 — secondary segments (analysis only, no independent tuning)

| Segment | Use |
|---|---|
| Home / away | Diagnostic for HFA mis-calibration |
| Favorite / underdog (binary) | Coarse alternative to favorite-size; redundant with Tier-1; do not introduce new gates |
| Model source (house / user) | Validation that user models aren't overfitting |
| Odds source quality (primary / fallback / stale) | Source-Aware Betting guardrails |
| Sport (when more than one is active) | Cross-sport calibration audit |

Tier-2 segments are slices for *analysis*. New gates on Tier-2 segments are not authorized by this framework — they require explicit promotion to Tier 1 first (i.e., a sample-size + mechanism justification + amendment to this doc).

### 4.3 Tier 3 — diagnostic only (debugging, never tuning)

- Day of week
- Time of day (slate start hour)
- Specific team
- Specific pitcher
- Sportsbook
- Captured-at recency

Tier-3 slices are useful for *investigation* (e.g., "why is Saturday's slate so different from Tuesday's?") but never appropriate as tuning targets. Persistent "this team underperforms" patterns are survivorship bias by construction — every season has teams that underperform expectations.

### 4.4 Independent tuning rules

When a Tier-1 segment shows ≥ 4 weeks of ≥ 20 in-band samples diverging by ≥ 1σ from slate average **AND** a named mechanism explains the divergence:

1. The proposed change applies *only* to that segment.
2. The change ships behind its own flag (Operating Principle: per-feature flags).
3. The change includes a `by_<segment>_band` evaluation slice in `BacktestRun.summary` (Law 2).
4. The change is reviewed against Law 4 anti-patterns.
5. The change includes a rollback trigger ("if the in-band CLV+ rate drops below X for Y weeks, revert").

Any one of those missing → the change does not ship.

---

## 5. Calibration Discipline

A formal calibration philosophy. Concrete enough to dispute. The phrase "trust the market" is too vague to be load-bearing — what follows is specific.

### 5.1 Philosophy

> The market is the most sophisticated continuous prediction system on Earth. Brother Willies competes with it by reading the same inputs the market reads, plus a small number of inputs the market may underweight. Disagreement should be **small in mean** and have a **named basis**. Confident model overrides without basis bleed CLV; consensus mimicry kills edge generation. The right state is *informed humility* — disagree when we have a reason, agree by default.

### 5.2 Market trust spectrum

The `MARKET_BLEND_WEIGHT` (currently 0.40) expresses how much the final probability defers to the market. The current default is uniform across all picks. The right state (Phase 1D candidate, not in scope here) is **dynamic** — varying with observable conditions:

| Condition | Blend weight bias | Rationale |
|---|---|---|
| Line just opened (< 2 hours old) | LOWER trust (e.g., 0.30) | Markets haven't fully discovered the price yet; we may be early |
| Mature line (≥ 6 hours, near first pitch) | HIGHER trust (e.g., 0.50) | Market has incorporated late information |
| Multiple books agree tightly (< 5 cent spread across 3+ books) | HIGHER trust | Market consensus is strong |
| Single book or wide book disagreement | LOWER trust | Market signal is noisy |
| Recent strong movement *against* our pick | HIGHER trust | Sharp money disagrees with us; we should listen |
| Recent strong movement *toward* our pick | LOWER trust | Sharp money agrees with us; we may have edge that's diffusing |
| Derived odds (synthesized from one-sided ESPN data) | LOWEST trust | Synthetic line; treat as missing |
| Source quality = `primary` (Odds API) | NORMAL trust | The baseline |
| Source quality = `fallback` (ESPN) | LOWER trust | Single-bookmaker, single-capture; not a real market |

**Bounds:** even under maximum bias, the blend weight never goes below 0.20 or above 0.65. Hard caps prevent any single condition from collapsing the framework into pure consensus or pure model.

### 5.3 What level of disagreement is healthy

- **< 5 pp mean disagreement:** strong alignment. The model is essentially shadow-pricing the market with small differences. CLV will be near zero on average; that's fine — edge generation lives in the right tail of disagreement.
- **5–10 pp mean disagreement:** healthy. We disagree often enough to generate edge, infrequently enough that our disagreements have basis.
- **10–15 pp mean disagreement:** investigate. Either we have real edge the market doesn't see (rare), or our model is reading stale inputs (more likely). The investigation is driven by §1 metric hierarchy and §2 evidence — not by reflex compression.
- **> 15 pp mean disagreement:** dangerous. The most likely explanation is that the model has structural blindness (stale ratings, missing context, broken provider). Calibration retune candidates surface here. Sub-segment investigation surfaces here.

### 5.4 When the market should override the model

- **Always, by default, in the absence of a named model signal** that disagrees with it.
- **Specifically:** when `|model_prob − fair_market_prob| > 15 pp` AND no specific signal in the score breakdown explains the divergence, the recommendation should be auto-downgraded to a lower tier or removed from `core` lane. (The existing `EXTREME_DISAGREEMENT_GAP` rule is the binary version; Phase 2C Task 7's edge-realism compression is the continuous version.)
- **When the market is sharp and we are not:** the short-favorite band -149..+99 is the canonical case where the market reads the game better than our model does because the band is structurally hard.

### 5.5 When the model should override the market

- **Rarely, deliberately, with a named basis.** The named basis must be:
  - A specific recent input the market may not have fully priced (late lineup change, weather, injury announcement),
  - A historical bucket where the model has demonstrated edge over enough sample,
  - A Source-Aware-Betting trust signal (e.g., the market line is from ESPN fallback, which is single-bookmaker / single-capture and lower-quality).
- **Never on instinct.** "The model just sees this one better" is not a basis. The signal_breakdown (Phase 2B Task 6, deferred) is what makes "named basis" inspectable.

### 5.6 Failure modes

Documented so they can be detected and resisted.

| Failure mode | Symptom | Detection | Remediation |
|---|---|---|---|
| **Confident overrides without basis** | CLV+ rate < 35% sustained; disagreement > 12 pp | Health Score CLV+ dimension drops to `WATCH`; `disagreement_gt_15pp.active` rises | Investigate signal_breakdown; tighten compression; do NOT retune calibration globally |
| **Consensus mimicry** | CLV+ rate near 50% (no signal); recommendation count drops; edges all in `0-4` bucket | Volume drops; edge distribution flattens | Investigate gates: are they too tight? Are signals being overweighted toward market? |
| **Recent-CLV chasing** | Constants change in response to short-term CLV swings | Tune log shows changes < 4 weeks apart in same causal area | Law 4 violation; revert the chase |
| **Survivorship-bias tuning** | Per-team or per-pitcher adjustments shipped | Code review flags hardcoded names | Strict prohibition; remove the adjustment |
| **Silent filtering** | Analytics surface shows fewer rows than ledger | Operator catches; or Law 3 audit | Add scope summary; default to inclusive scope |

### 5.7 Guardrails (in-code)

These are properties of the system, not aspirations. Most already exist; some are Phase 1D candidates.

| Guardrail | Status |
|---|---|
| `MARKET_BLEND_WEIGHT_CAP = 0.40` (current; raises to 0.65 dynamic cap as Phase 1D) | ✅ in code |
| `PROB_MIN = 0.52`, `PROB_MAX = 0.85` soft clamp | ✅ in code |
| `EXTREME_DISAGREEMENT_GAP = 0.12 post-blend` tier downgrade | ✅ in code |
| `MIN_PROBABILITY_FOR_RECOMMENDED = 0.60` | ✅ in code |
| `MIN_EDGE = 6.0` pp | ✅ in code |
| `MAX_ABS_ODDS_FOR_RECOMMENDED = 300` | ✅ in code |
| Source-trust block on derived odds | ✅ in code |
| Audit trail on overrides (signal_breakdown) | ⏳ Phase 2B Task 6 |
| Sub-segment gates per Tier-1 segment | ⏳ Phase 2C |

---

## 6. How this framework prevents overfitting

By construction:

1. **The metric hierarchy (§1)** prevents the most common form of overfitting — chasing win rate or single-window ROI swings. Win rate is display-only; ROI is validation-only.
2. **The governance rules (§2)** force every proposed change through a sample-size + window + mechanism filter. Reflexive tunes from short samples cannot pass.
3. **The Health Score (§3)** is the gate. Strong systems don't get touched; weak systems get *targeted* action, not broad retunes.
4. **The segmentation framework (§4)** keeps tunes scoped. Tier-1 segments may have independent gates; Tier-2 and Tier-3 may not.
5. **The calibration philosophy (§5)** prevents two opposite failure modes simultaneously: confident overrides without basis (CLV bleed) and consensus mimicry (no edge).
6. **Law 4 (`docs/architecture_laws.md`)** codifies the "no overfit" principle and lists specific anti-patterns to reject.

The combination is more important than any single piece. A team that follows §2 but ignores §3 will still ship justifiable-but-unnecessary changes that destabilize the system. A team that watches §3 but skips §2's mechanism requirement will respond to true degradations with mis-aimed fixes. The framework is the joint discipline.

---

## 7. Implementation roadmap (gated)

This document is governance only. The implementation steps below require explicit operator authorization and ship one at a time, each behind §2 governance.

| Step | Description | Effort | Risk |
|---|---|---|---|
| 1 | Build the Recommendation Health Score service + view (`/analytics/health-score/`) | medium | low (additive observation) |
| 2 | Wire the Health Score into the existing system-tuning surface as the gating signal | small | low |
| 3 | Build a constant-change audit log (tune log) | small | low |
| 4 | Auto-link tune log entries to a `BacktestRun` showing the supporting evidence | small | low |
| 5 | Add Health Score and tune log entries to the Phase 2B / 2C readiness reports | small | low |

None of these change recommendation behavior. All ship under Law 2 (evaluation surface) and Law 3 (transparency).

---

## 8. The single sentence

If the framework had to fit on one line:

> **Brother Willies improves by measuring CLV, calibration, and edge-bucket performance against the Recommendation Health Score, and tunes only when sample, window, mechanism, and isolation all permit.**

That sentence is the operating discipline. Everything in this document is its specification.

---

*Promulgated 2026-05-14. Brother Willies transitions from "filtered betting opinions" to "calibrated probabilistic market reasoning with scientific governance."*
