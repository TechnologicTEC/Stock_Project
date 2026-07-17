# Cross-sectional scoring — experiment plan

**Status:** planned, not started. Nothing below has been run against holdout data.
**Question:** can we raise the Screener's cross-sectional IC by changing *what it
measures against*, rather than by tuning numbers until the metric looks good?

---

## Where we're starting from

Measured 2026-07-17 on 499 S&P 500 names, 30,096 point-in-time reconstructions,
window 2021-04-14 → 2026-04-13, 4 of 6 factors (analyst/sentiment unavailable in
batch):

| | value |
|---|---|
| Composite cross-sectional IC | **+0.046** (t=2.17, 61 dates → 20 independent, hit rate 0.69) |
| Valuation / Momentum / Growth / Profitability | +0.034 / +0.034 / +0.032 / −0.025 — **none significant** (bar: \|t\|>2.5) |

The composite has a small edge; no individual factor does. That is why **factor
reweighting is rejected** — see `memory: screener-composite-works-parts-dont`.

### The result that shapes this plan

Rescoring a factor's 0–100 curve **cannot change its IC**. Proven on this data —
three different monotonic curves, identical IC to four decimals:

```
momentum IC as-is                : +0.0340
  squashed  (x^2/100)            : +0.0340
  stretched (10*sqrt(x))         : +0.0340
  logistic-ish                   : +0.0340
```

IC is a *rank* correlation; a monotonic remap preserves the ordering of stocks, so
the correlation is untouched. Curve tweaks only move the **composite** (0.046 →
0.049 squashed / 0.040 stretched) because it's a weighted **sum** — changing a
factor's spread changes its effective weight. **A curve tweak is a reweighting in
disguise**, and reweighting is exactly what the evidence says not to do.

**Therefore: the only way to move IC is to change the ORDER of stocks.** That means
changing what a factor is measured *relative to* — not the cosmetics of 0–100.

---

## What we already have (verified 2026-07-17)

Good news — most of the machinery exists:

| Thing | State |
|---|---|
| `FactorResult.raw` | already carries raw metrics (pe, pb, ps, …) |
| `_score_valuation` etc. | already **batch** functions: `dict[str, TickerRawData] -> dict[str, FactorResult]` |
| `_percentile_ranks(...)` | **already computes cross-sectional percentiles** — but is used *only for the explanation text*, not the score |
| `_curve_for("pe", sector_bucket, …)` | sector-specific curves already exist |
| 2016–2021 data | Alpaca has bars from Jan 2016; all 4 factors reconstruct at 2016-06-01 ✅ |
| `pinned_window`, per-ticker cache, Bonferroni | already built |

**The actual blocker:** the reconstruction scores **one ticker at a time**, so
`_percentile_ranks` sees a universe of 1 and is meaningless. The live Screener has
the same problem in reverse — it ranks against whatever small, biased peer set the
user happens to be screening.

---

## Hypotheses (pre-registered)

**Primary (H1) — one test, decided in advance.**
> Scoring each factor by its **cross-sectional percentile within its sector**, on
> each date, across the full universe — replacing the absolute curves — raises the
> composite cross-sectional IC versus the current scoring.

Rationale, stated *before* seeing any result: absolute curves ("P/E 15 → 80
points") ignore both context that matters. A P/E of 20 is cheap for a
semiconductor and dear for a utility; and on a date when everything is expensive,
every name scores low and the factor stops discriminating between names — which is
the only thing the IC measures.

**Secondary / exploratory — reported, never used to claim success.**
- **H2 (ablation):** universe-wide percentile vs sector-relative — which part does the work?
- **H3 (diagnostic):** IC against **sector-neutral residual returns** (return minus that date's sector mean). NOTE: this changes the *target*, not the model, so it is **not comparable to the baseline IC** and cannot be reported as an improvement. It answers a different question: is the Screener better at picking *within* a sector than it looks against raw returns (which are dominated by market/sector moves nothing stock-specific can predict)?

Only H1 decides adoption. H2/H3 are for understanding, and are subject to the same
Bonferroni bar if we ever quote them as findings.

---

## The split — and why this direction

| | window | role |
|---|---|---|
| **Development** | 2021-04-14 → 2026-04-13 | tune freely. Already contaminated — we've stared at it all session, so there's nothing left to spend. |
| **Holdout** | ~2016 → 2021 | **pristine.** Touched exactly once, at the end. |

Deliberately backwards from the usual "train on old, test on new". The point of a
holdout is data *nobody has looked at*, and 2021-2026 has been looked at
repeatedly (26-name pilot, 5-year run, 503-name run). Spending the already-spent
data on development preserves the only clean data we have for the one test that
counts.

**Regime caveat, named up front:** the holdout contains COVID (Feb–Mar 2020 sits
just inside a 2016-2021 window) and the development period contains the AI rally.
A method that transfers across those two is genuinely robust; one that doesn't was
regime-fitted. That's a feature of this split, not a bug — but it also means a
*failure* to replicate is ambiguous (real regime dependence vs overfitting).

---

## The test

**Paired, per-date.** For each date `d` in the holdout compute `IC_new(d)` and
`IC_old(d)`, then test whether `mean(IC_new − IC_old) > 0`.

Paired because both methods see the identical dates and names, so date-level noise
(the market's mood that quarter) cancels. That is far more powerful than comparing
two independent ICs, and it's the difference between a test that can conclude
something and one that can't.

- t-stat over **independent** dates (deflate by `step/horizon` — `effective_sample_size` already does this).
- Adoption bar: **t > 1.96 on the paired difference**, one test, pre-specified.
- Report the effect size, not just the verdict.

### Phase 0 gate: power analysis — RUN 2026-07-17, result below

From the real per-date composite ICs (61 dates → 20 independent, mean +0.046,
**sd 0.0952**), the minimum improvement the test could detect at t>1.96:

| design | min detectable improvement |
|---|---|
| **Unpaired** (run both, compare the two ICs) | **+0.0590** |
| Paired, methods correlated ρ=0.80 | +0.0264 |
| Paired, ρ=0.90 | **+0.0187** |
| Paired, ρ=0.95 | +0.0132 |
| Paired, ρ=0.98 | +0.0083 |

**Two conclusions, both load-bearing:**

1. **The obvious experiment is worthless.** Comparing two separately-measured ICs
   could only detect a +0.059 improvement — *larger than the entire effect we're
   studying* (+0.046). We could double the Screener's edge and the naive test would
   shrug. Anyone running "new IC vs old IC, is it bigger?" would be reading noise.
   The paired design isn't a refinement here; it's the only thing that makes the
   question answerable at all.
2. **Paired, the test is viable but not generous.** The new method re-normalizes
   the *same* factors over the *same* names, so per-date ICs should be highly
   correlated (ρ≈0.9+), giving a detection floor around **+0.019**.

**Pre-committed consequences:**
- We detect improvements of roughly **+0.02 or larger**. Anything smaller is
  invisible to us, and we will call it "not detected" and keep the current
  scoring — not "probably a small win".
- **Measure ρ on the development window first** (no holdout contact needed). If
  ρ < 0.8, the floor rises past the plausible effect and we **stop before Phase 5**
  rather than spend the pristine data on a test that cannot answer.

The gate passes — conditionally on that ρ check.

---

## Results log

Recorded as they happen — pre-registration only means anything if the misses get
written down too.

### ρ gate + H1 (universe-wide percentile) — development window, 2026-07-17

Full universe, 2021-04-14 → 2026-04-13, control vs candidate, paired per date:

```
composite IC: absolute +0.0460 | cross-sectional +0.0417
RHO between per-date ICs = 0.959          (gate: >= 0.80)  -> PASS
paired mean diff = -0.0043, sd 0.0303, t = -0.64 on 20 independent dates
min detectable improvement with this pairing: +0.0133
```

**Gate: PASS.** ρ=0.959 makes the pairing excellent — floor +0.0133, better than
the +0.019 assumed at ρ=0.9. The test is well powered.

**H1: FAILS.** Universe-wide percentile scoring is *slightly worse* (−0.0043),
comfortably inside noise (t=−0.64) — but the test could have seen a +0.0133
improvement and there isn't one. A well-powered null, on the **development** data,
i.e. the friendly case.

**Diagnosis — H1 was not a fair test.** The absolute control is *already
sector-aware*: `SECTOR_CURVE_OVERRIDES` supplies sector-specific P/E, P/B, P/S and
gross-margin curves across ~10 sector buckets. Ranking percentiles across the whole
index discards that — it ranks a utility's P/E against a semiconductor's. H1 didn't
*add* context, it **traded sector-awareness for cross-sectional context** and lost
slightly. That makes H2 (rank *within sector*) the real candidate: it keeps what
the control already had and adds what it lacks.

**Holdout: NOT touched.** Nothing here is worth spending the one clean test on —
you don't take a candidate that lost on the data you tuned it on to the pristine
window.

---

## Phases

| # | Phase | Done when |
|---|---|---|
| 0 | **Power analysis + pre-registration.** Compute minimum detectable effect. Commit the hypotheses/criteria to this file. | The bar is written down and we've confirmed the test can see the effect. |
| 1 | **Invert the loop to date-major.** Today: for each ticker → for each date. Needed: for each date → gather the whole universe's raw data → score as a batch. No behaviour change. | Baseline reproduces **+0.046 exactly**. If it doesn't, stop — something else changed. |
| 2 | **Cross-sectional percentile scorer**, behind an opt-in flag. Promote `_percentile_ranks` from explanation to score. Winsorize before ranking. | Absolute-curve path still default and green. |
| 3 | **Sector-relative variant** (rank within `sector_bucket`, fall back to universe-wide when a sector is too thin). | Both variants runnable on dev data. |
| 4 | **Develop on 2021-2026 only.** Iterate freely. The holdout is not touched, not peeked at, not "sanity checked". | A single frozen candidate. |
| 5 | **One holdout run.** 2016-2021. Run it once. | Result recorded, whatever it says. |
| 6 | **Adopt or discard** per the Phase 0 bar. Either way write the outcome into the Validation page's honesty text. | The app tells the truth about what was tested. |

Phase 1 is the real work; Phases 2–3 are small because the scorers are already
batch-shaped and `_percentile_ranks` already exists.

---

## Risks & known limitations

- **Sector look-ahead.** Finnhub gives *today's* sector; applying it to 2016 assumes no reclassification. Mild, but it is look-ahead. Document; don't pretend it's clean.
- **Survivorship bias.** The universe is today's index in both windows. Fine for comparing methods against each other on the same names (it hits both equally) — never for quoting return levels.
- **Alpaca coverage pre-2018.** AAPL has 2016 bars; breadth across 500 names is unverified. Phase 0 must check, or the "holdout" quietly becomes a smaller, different universe.
- **Live-screener UX change.** A cross-sectional score is *relative to the S&P 500 on a date*, not absolute. "68/100" would come to mean "better than 68% of the index", which is a different claim and must be said plainly in the UI. It also means the live score needs a stored recent universe distribution to rank against.
- **Regression risk.** Keep absolute curves as the default until the holdout earns the change.
- **Two factors missing.** Analyst/sentiment don't reconstruct in batch (Yahoo blocks datacenter IPs; GDELT needs BigQuery creds). This experiment tests a 4-factor composite; the live 6-factor score is not the thing being validated.

---

## Expectations

Set honestly, before we start: if H1 works, IC plausibly moves from ~0.046 to
~0.06–0.08. That is a *better* faint tilt — **not** a stock picker in the sense
the phrase implies. IC ~0.05 is already roughly what professional equity factor
models achieve; IC ~0.3 does not exist on public free-tier fundamentals. The
ceiling here is the **data**, which is the same data everyone else has, and no
amount of arithmetic on it changes that.

The plan is worth doing because cross-sectional/sector-relative ranking is
*principled* — it's what the metric actually measures and what every real factor
model does — not because we expect a transformation.

**A null result is a real result.** If H1 fails on the holdout, we keep the current
scoring, and we will have learned something true rather than manufactured a number
that looks better on data we'd already read.
