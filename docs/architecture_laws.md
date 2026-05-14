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
