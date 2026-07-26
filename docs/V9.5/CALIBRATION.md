# V9.5 — Model & Calibration

`mls-2026-v0`, as deployed for the `mls-shadow-v1.6` baseline. **The
MODEL IS UNCHANGED** from V9.3 — V9.4 corrected the execution layer,
V9.5 corrected the evidence layer. Neither touched the forecast.

What *is* new in V9.5: the numbers below are no longer purely
retrospective. Fifteen prospectively-locked fixtures have been scored and
folded into the sample.

---

## How a probability is produced

```text
1. ratings      attack_i, defence_i from the provider's per-match xG,
                recency-weighted (90-day half-life), shrunk k=6.
                A fixture without xG falls back to goals ratings (k=24).
2. lambdas      lam_home = league_gpg × atk_h × def_a × venue_home
                lam_away = league_gpg × atk_a × def_h × venue_away
3. simulate     shared Monte Carlo engine, MLS dispersion cv = 0.0,
                deterministic seed from the ESPN event id (31-bit masked)
4. calibrate    3-way only:  (1-0.25) × simulated  +  0.25 × (1/3,1/3,1/3)
                props and scorelines are NOT calibrated — measured correct
```

Frozen into a `model-input-v5` artifact, so a run replays byte-for-byte
from stored bytes alone — **demonstrated on this slate**, not asserted:
a lock written days earlier replayed with `max_delta: 0.0`.

---

## The approved decision (production)

```text
decision_id             199        (snapshot — see the note below)
content_hash            c97432ca77c0086599526d8cc9db3946c919c0e791008cedab2bcea783a8439c
corpus_version          mls-shadow-2026-07-25-slate-v1
corpus_manifest_hash    7e0836818f6f25f9a08126140eca822c063574f1e50cf0ffe003d3827415a27c
n_scored                177
edge_vs_baseline        +0.0269
ci95                    [−0.0052, +0.0609]
edge_significant        FALSE
approved_mode           shadow
```

> **Do not pin the decision id or content hash.** The engine signature
> includes `code_revision`, so every deploy invalidates the active
> decision and boot recomputes one — from whatever the mutable database
> holds at that moment. The id advances and the bootstrap CI can move in
> the fourth decimal place even when `n_scored` and the point estimate do
> not. This snapshot was 199 / `c97432ca…`; it was 197 / `58382fc9…` an
> hour earlier, for a docs-only deploy. What is stable and citable is the
> **corpus binding** and the point estimate.

First decision in the project's history that is **bound to a published
corpus**. Every prior one recorded `corpus_version: null`.

---

## The estimate moved — down

| sample | n | edge vs baseline | CI 95% | significant |
|---|---|---|---|---|
| V9.4 (retrospective only) | 162 | +0.0331 | [−0.0035, +0.0663] | no |
| **V9.5 (+15 prospective)** | **177** | **+0.0269** | **[−0.0052, +0.0609]** | **no** |

This is the expected direction. An in-sample estimate meeting genuinely
out-of-sample fixtures should shrink, and it did. The CI still spans
zero; nothing here is an edge claim.

---

## The prospective slate, scored on its own

|  | log loss | Brier |
|---|---|---|
| Model (locked T-10) | 0.9263 | 0.5462 |
| Baseline (league base rates) | 0.9236 | — |

```text
model − baseline    +0.0027   (positive = WORSE)
bootstrap CI 95%    [−0.0755, +0.0770]   (20,000 resamples)
favourite hit rate  10 / 15
```

**The CI is ~5× wider than the effect being chased.** Fifteen matches
cannot resolve an edge of 0.03, and this slate does not.

Context that matters more than the point estimate: the slate produced
**11 home wins in 15** (73%) against a 45.7% league rate, P(≥11) = 0.029.
The model averaged 43% home. It underperformed principally by not
forecasting a 1-in-35 home skew.

---

## The ladder (retrospective, unchanged from V9.4)

| rung | log-loss | Brier | RPS | description |
|---|---|---|---|---|
| M0 | 1.0776 | 0.6530 | 0.2349 | league scoring + venue split |
| M1 | 1.1658 | 0.6954 | 0.2550 | ratings, equal-weighted, minimal pooling |
| M2 | 1.0697 | 0.6469 | 0.2322 | + recency + partial pooling (the old v0) |
| M2C | 1.0608 | 0.6404 | 0.2290 | + calibration toward uniform |
| **M3** | **1.0443** | **0.6286** | **0.2239** | **+ provider xG ratings — DEPLOYED** |

M1 losing to M0 remains the standing reminder that more signal can hurt:
raw ratings without pooling overfit.

---

## Execution economics — the first real measurement

V9.4 could report no paper P&L because there were no fills. V9.5 found
out why: a VARCHAR truncation had destroyed every fill while preserving
every rejection (see [`SLATE-EVALUATION.md`](SLATE-EVALUATION.md) §3).
Recovered:

```text
signals                45     (45/45 eligible legs — 100% coverage)
fills                   7     all execution-grade (bounded_depth)
rejected               38     all NET_EDGE_TOO_LOW
settled cost      $169.3237
settled P&L       −$69.3237
ROI                −40.94%
fills that hit          1 of 7
average entry        ~$0.24
```

### Why −40.9% is not a result

```text
expected hits at the MARKET's own price   1.61      (sum of fill prices)
observed                                  1
P(≤1)                                     0.49244   Poisson-binomial

ROI as settled        −40.9%
ROI, one more hit     +18.1%     ← the sign turns on a single match
ROI, one fewer hit   −100.0%
```

Seven longshot fills. The sign of the ROI is decided by one match. This
replaces a fabricated zero with an honestly bounded number; it is not
evidence of edge in either direction.

---

## Standing discipline

**Do not tune on this slate.** These 15 fixtures are now inside the
scored sample (n=177). The moment a constant is selected with them in
view they stop being prospective evidence and become another in-sample
fit — which is precisely how the M1 overfit and the "win% blend" both
happened.

What would move the verdict is a larger *forward* sample: hundreds of
scored fixtures, and enough **fills** — not signals — to separate a real
edge from seven longshots. At this slate's rate that is a season of
Saturdays collected without interference.

**Paper P&L NO-GO for profitability. Real money NO-GO.** Unchanged.
