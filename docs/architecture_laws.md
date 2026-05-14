# Brother Willies — Architecture Laws

Permanent design constraints. These are not aspirations or guidelines —
they are load-bearing rules that downstream architecture depends on. A
proposed change that violates one of these laws must either justify the
exception (in writing, in the same commit) or revise the law itself
(also in writing, in a separate commit ahead of the violating change).

These laws were promulgated as Brother Willies transitioned from
*"filtered betting opinions"* toward *"calibrated probabilistic market
reasoning"* — at the point where the model gained enough complexity that
hidden interactions, silent feature explosions, and undisciplined
calibration became the dominant risk to product quality.

---

## Law 1 — Signals Are Nudges, Not Drivers

**Statement.** No single predictive signal may dominate the model's
output. Every signal's contribution to the final probability is
bounded by a deliberate, named, named-in-code constant. The bound is
small enough that, with the signal at its maximum AND all other signals
at zero, the recommendation engine's downstream gates still make the
decision.

**What this means in practice.**
- Each new term in `_score()` has an explicit weight × explicit cap on
  its input range. The product gives a worst-case legacy-point
  contribution.
- The worst-case probability swing through the sigmoid (divisor 25),
  blend (weight 0.40), and clamp ([0.52, 0.85]) is reported in the
  signal's design doc.
- A signal whose worst-case prob swing exceeds 0.05 (5 percentage
  points) requires explicit justification. Most signals should land
  well under 0.02.

**Why this law exists.**
Without it, every new feature races to be "the signal that explains
everything." That race produces a model that is fragile (one bad data
input swings recommendations wildly), un-debuggable (no single signal
is interpretable in isolation), and over-fit (the dominant signal
captures noise as much as truth). Bounded nudges compose. Drivers
collide.

**Concrete examples (Phase 2 designs).**

| Signal | Input range | Weight | Worst legacy pts | Worst prob swing |
|---|---|---|---|---|
| Team rating (Elo) | ±50 | × 0.35 | ±17.5 | ±0.50 |
| Pitcher rating | ±50 | × 0.65 | ±32.5 | ±0.75 |
| Pitcher form (proposed) | ±1.0 | × 0.15 | ±0.15 | ±0.006 |
| Team form (proposed) | ±1.0 | × 0.10 | ±0.10 | ±0.004 |
| Bullpen (proposed) | ±1.0 | × 0.12 | ±0.12 | ±0.005 |
| HFA | constant 2.5 | × 1.0 | +2.5 | +0.10 |

Note: rating and pitcher terms are large *by design* — they are the
core thesis. Phase 2B additions are deliberately tiny because they are
context refinements on top of the core, not replacements for it.

---

## Law 2 — No Signal Without Its Evaluation Slice

**Statement.** Any commit that adds a new predictive signal to the
score formula must, in the same commit, add an evaluation breakdown
that segments recommendation outcomes by that signal. The evaluation
must:

1. Bucket recommendations into named bands derived from the signal's
   value at recommendation time (e.g., `hot` / `neutral` / `cold` for
   form scores).
2. Surface per-band sample count, win rate, ROI, average CLV, and
   positive-CLV rate — the same metrics every other breakdown in
   `BacktestRun.summary` carries.
3. Be readable from the existing analytics surface
   (`/analytics/backtest/` for full replays, `/analytics/shadow-review/`
   for live shadow comparisons) without operator code changes.

**What this means in practice.**
- New signal → new top-level key in `BacktestRun.summary`.
- New signal → new field on `GameEvaluation` carrying the band label.
- New signal → new buckets in `_BacktestAggregator`.
- New signal → tests asserting evaluations land in the right band.

**Why this law exists.**
Without it, signals accumulate faster than measurement. We end up
adding "intelligence" we can't audit. Within months, the model is a
pile of weights nobody can defend, and any post-hoc attempt to
attribute performance is guessing.

With it, every commit is a controlled experiment. The signal lands
behind a flag, runs in shadow for a defined window, and either
demonstrates measurable lift in its own evaluation slice or it gets
backed out cleanly. The model evolves scientifically.

**Compliance check.** A reviewer reading a "new signal" PR should be
able to answer "how do we know this signal helps?" by pointing at a
specific breakdown key in `BacktestRun.summary`. If they can't, the
PR violates Law 2.

---

## Law 3 — Analytics Surfaces Must Be Transparent About Their Scope

**Statement.** Any analytics surface that filters the user's actual
data (bets, recommendations, games, etc.) must visibly disclose:

1. The chosen scope (named).
2. The total population observed *before* the scope filter (the
   denominator the user would expect).
3. The included count (the numerator the surface is actually using).
4. The excluded count, broken down by reason.

Silent exclusion is forbidden. If a page shows N records but the
underlying ledger has N + K for the same time/sport/user window, K
must appear on the page along with the reason it was filtered out.

**What this means in practice.**
- Every evaluation page carries a `scope` indicator at the top.
- The default scope on any user-facing surface is the one that
  matches the user's actual placed-bet ledger (`actual` in the
  Moneyline Evaluation), not a silently filtered subset.
- Specialized scopes (e.g., `recommended` for model evaluation) are
  one explicit click away and surface their exclusion counts.
- The same scope label propagates to derived artifacts — markdown
  copy-packets, JSON exports, AI-Insight prompts — so any downstream
  reader (human or LLM) sees the same scope context the page does.

**Why this law exists.**
The Phase 2A Task 3 → Task 4 workflow caught the canonical violation:
the operator saw 6 placed MLB bets on `My Bets` and 2 in `Moneyline
Evaluation` for the same date. The evaluation page silently dropped 4
manual bets (`is_system_generated=False`), with no count, no reason,
no operator-readable scope indicator. Every downstream conclusion
built on top of that 2-bet view was structurally untrustworthy.

Once the discrepancy is observable, the human can decide whether the
scope is the right one. When the discrepancy is invisible, the human
makes confident decisions on incomplete data — the worst possible
state.

**Concrete examples.**

| Surface | Default scope | Excluded counts visible? | Compliance |
|---|---|---|---|
| `/mockbets/moneyline-evaluation/` | `actual` (post-2026-05-14) | ✅ scope-summary box | ✅ |
| `/mockbets/` (My Bets) | user's full ledger | n/a — not a filtered evaluation | ✅ |
| `/analytics/shadow-review/` | last 14 days w/ `elo_available=True` | ✅ `sample` vs `sample_total` displayed | ✅ |
| `/analytics/backtest/` | full historical replay per rating mode | ✅ `validation.evaluated` + `validation.duplicates_dropped` + `validation.approximate_games` in summary | ✅ |

**Compliance check.** A reviewer reading any analytics-page PR should
be able to answer "what fraction of the user's actual data is this
page rendering?" by pointing at a visible UI element. If the answer
requires reading source, the PR violates Law 3.

---

## Law 4 — Do Not Overfit

**Statement.** Brother Willies does not chase outcomes. Constant
changes, threshold retunes, and recommendation-rule modifications
require evidence: sample size, time window, effect magnitude, and a
named mechanism. A single slate, week, or segment cannot trigger a
configuration change. Survivorship bias, selection bias, and silent
filtering are forbidden anti-patterns. Every tune is rolled back if
its supporting evidence disappears within a defined window.

**What this means in practice.**
- No constant changes without a written justification covering
  sample, window, effect, mechanism.
- The Recommendation Health Score (`docs/recommendation_quality_framework.md`
  §3) is the gating signal for whether action is warranted. Scores
  in `STRONG` or `HEALTHY` bands do not justify constant changes
  even when individual segments look "off."
- Changes that target a single subgroup require a *generalizable*
  mechanism — "this pitcher's last-3-start ERA was bad" is not a
  mechanism; "short-favorite picks with default-rated pitchers
  underperformed across 60 settled bets over 4 weeks" is.
- After any constant change ships, no other change in the same
  causal area ships within the next isolation window (4 weeks for
  calibration; 1 week for sub-segment gates) — Operating Principle
  "no optimization stacking" enforced harder.
- Every tune logs its supporting evidence. Tune logs are evaluation
  inputs themselves — the next person looking at "why is this number
  set to X?" reads the log entry, not the source-control blame.

**Specific anti-patterns this law forbids.**

| Anti-pattern | Why it's forbidden |
|---|---|
| "We lost three in a row — tighten the edge gate." | Variance, not signal. Three-bet samples carry no statistical content. |
| "This edge bucket lost five bets this week — suppress it." | Insufficient sample (< 20 in bucket per Tuning Governance §2). |
| "Today's slate was bad — drop the market blend weight." | Single-slate noise. Calibration changes require ≥ 4 weeks. |
| "The model agreed with the market on the close losses — increase divergence." | Outcome-driven; the model agreeing with the sharp market is *good* CLV behavior even when individual bets lose. |
| "Add a per-team adjustment because the Yankees keep underperforming." | Per-team adjustments are survivorship-biased by definition. |
| "Tweak the gate so yesterday's winners would have qualified and yesterday's losers wouldn't." | Look-back overfitting. Cannot be done on the data the change is justified by. |

**How to identify a Law 4 violation in a proposed change.**

A change violates Law 4 if ANY of the following hold:
1. The supporting sample is < 30 settled bets, OR
2. The supporting window is < 4 weeks (for calibration) or < 1 week (for sub-segment gates), OR
3. The change targets a subgroup without a generalizable mechanism, OR
4. Another change in the same causal area was made within the isolation window, OR
5. The change's evidence section contains the phrase "felt off," "looked bad," or "we should probably" — these are emotional, not evidence-based.

**Compliance check.** Every commit that modifies a recommendation
constant, calibration parameter, gate threshold, blend weight, clamp
bound, sigmoid divisor, lane rule, or any other tuning surface must
include:

```
## Evidence
- Sample: <n> settled bets
- Window: <date_from> to <date_to> (≥ 4 weeks for calibration)
- Segment: <if applicable>
- Effect measured: <metric> changed from <X> to <Y> (Δ <Z>)
- Mechanism: <generalizable causal explanation>
- Health Score before: <score>
- Health Score after (predicted): <score>
- Rollback trigger: <metric> below <threshold> for <window>
```

A PR / commit message without that block, for a tuning change,
violates Law 4. The reviewer should request the evidence or close
the PR.

---

## Operating principles derived from the laws

These follow from the two laws but are worth stating explicitly so
they don't get re-litigated.

- **Per-feature flags, default False.** Every new signal ships behind
  its own flag (e.g., `MLB_PITCHER_FORM_ENABLED`). The flag is the
  shadow / production toggle. Two signals are never entangled in one
  flag.

- **Determinism only.** No randomness, no ML, no LLM in the prediction
  loop. Every signal is a deterministic function of observable inputs.
  Same DB state → same output, every time.

- **Reversibility.** Every change must have a documented rollback that
  is one command, one env-var flip, or one revert commit. Migrations
  must be reversible or accompanied by a stated reason for irreversibility.

- **Diagnostic visibility.** Every signal's contribution to a specific
  recommendation must be inspectable on the Model Input Inventory
  page (`/analytics/model-inventory/`). If a number changes in the UI
  but nothing changes on the inventory page, that is a bug.

- **Evaluation tooling is product.** The backtest harness, the shadow
  review, the model inventory, the segment instrumentation — these
  are not engineering scaffolding. They are core product assets. They
  evolve with the model; they get tests; they survive feature work.

- **CLV is the primary signal of edge realism.** Win rate is noisy;
  CLV is the leading indicator. Calibration decisions are evaluated
  against long-term CLV trend, not short-term win-rate spikes.

- **No emotional tuning.** A bad slate is data, not a crisis. Constants
  do not move in response to a single week's outcomes. Re-tunes are
  driven by ≥ 4 weeks of post-cutover data in a documented analysis.

- **No optimization stacking.** When more than one change is in flight
  (e.g., Elo cutover + signal addition + calibration retune), we cannot
  attribute effects. Changes are sequenced; each gets a stabilization
  window before the next ships.

---

## Amendment process

To change one of the laws:

1. Open a separate commit that updates this doc with the new wording
   and a written justification.
2. Cross-reference the commit in the changelog.
3. Wait at least one full slate / cycle before the first commit that
   relies on the amended law.

Amendments are explicit. Silent erosion is not allowed.

---

*First codified: 2026-05-14 (Phase 2A direction-setting).*
*Foundation: the Phase 1 + Phase 2 architecture work that established
the shadow-mode framework, segment instrumentation, and bounded-signal
design principle.*
