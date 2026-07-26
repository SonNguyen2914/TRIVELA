# CALIBRATION — V9: `mls-2026-v0` Validation (July 23, 2026)

> **⚠️ SUPERSEDED.** This is the V9 edition (2026-07-23). The current
> edition is **V9.5** on branch `docs-v9.5` — see [`docs/EDITIONS.md`](../EDITIONS.md)
> for the full map. V9 predates the first prospective slate, the
> published corpus, and the paper-coverage / VARCHAR-truncation /
> engine-signature fixes. Kept for the record.

*Supersedes `docs/V8/CALIBRATION.md`, which documented the model's first
Monte-Carlo walk-forward. The definitive evaluation is now the analytic
model-development ladder (`src/live/model_eval.py`, `GET /api/mls/model-eval`),
which fixes the two flaws the independent review named: simulation noise
masquerading as improvement, and a point estimate with no uncertainty.
The WC26 numbers remain in `docs/V7/CALIBRATION.md` and are unchanged.*

## The question

Not "is the model good," but the launch gate's narrow one: **does a
league-fitted model beat what "every team is identical" already knows,
and by how much, with what confidence?**

## Why the old backtest wasn't enough

The V8 walk-forward compared model vs baseline using DIFFERENT simulation
seeds, so their difference carried independent Monte Carlo noise — and it
reported a bare point estimate (+0.007 log loss). Both are fixed here.

## Protocol (analytic, rolling-origin, bootstrap)

- **Analytic scoring, zero MC noise.** The 3-way outcome is scored with
  exact independent-Poisson probabilities on a goal grid — `P(home/draw/
  away)` from `Poisson(λ_home)`, `Poisson(λ_away)`. No sampling, so every
  variant is compared on identical ground and a difference is signal.
- **Rolling-origin.** Each fixture is predicted only from fixtures that
  kicked off before it. No leakage by construction.
- **Ladder ablation.** M0 (league scoring + venue split, no team info) /
  M1 (team attack-defence, equal-weighted, minimal pooling) / M2
  (recency weighting + partial pooling = `mls-2026-v0`).
- **Match-cluster bootstrap.** Resample fixtures with replacement (1,000×),
  recompute each variant's mean and every pairwise edge, report the
  2.5/97.5 percentiles — so each edge is a claim with a 95% interval.
- Eligibility: both teams ≥ 5 prior completed games → **n = 162**.

## Headline result (live, prod-verified)

| variant | log loss | brier | rps | |
|---|---:|---:|---:|---|
| M0 league + venue | 1.078 | 0.653 | 0.235 | |
| M1 raw ratings | 1.166 | 0.695 | 0.255 | **overfits — worse than baseline** |
| M2 recency + pooling | 1.070 | 0.647 | 0.232 | best |

| edge (Δ log loss, +ve = better) | point | 95% CI | significant |
|---|---:|---:|---:|
| **M2 vs M0** (vs naive baseline) | **+0.008** | **[−0.012, +0.029]** | **No** |
| M2 vs M1 (vs raw ratings) | +0.096 | [+0.021, +0.177] | **Yes** |
| M1 vs M0 | −0.088 | [−0.186, +0.007] | No |

## What it means

- **Raw team ratings OVERFIT** MLS's noisy small samples and *lose* to a
  league-average baseline (M1 worse than M0).
- **Recency + partial pooling RESCUES them, decisively** (M2 beats M1,
  significant) — the model's two headline features earn their place.
- **But M2's edge over the naive baseline is within noise** (CI spans
  zero). The model's *construction* is validated; a durable forecasting
  edge is **not established**.

This is the number the money gate rests on. A confidence interval that
spans zero is not an executable edge, and the approval record says so:
shadow approval means "safe to collect prospective evidence," never
"edge established."

## Caveats, all of them

1. **In-sample.** The rolling-origin is not a prospective holdout, and
   the hyperparameters were chosen on this same sample — so +0.008 is
   mildly optimistic. The honest expectation is "positive, smaller, and
   quite possibly zero." Saturday begins the prospective test.
2. **n = 162, one league, one half-season.** No claim of significance
   for M2-vs-baseline is made; the CI is the point.
3. **Forecast quality only.** Market-relative and execution performance
   are evaluated separately, after settlement (the paper ledger and the
   frozen T-10 books already capture what's needed).
4. **M3–M5 not built.** Rest/travel, lineup, and goalkeeper effects are
   declared rungs; the lineup data is captured (Phase 5) but the model
   does not yet consume it — precisely because this evaluation has to
   show a feature helps before it goes in.

## The prospective plan (begins Saturday July 25)

Every fixture gets a two-phase, completeness-gated T-10 lock: ~35
contract probabilities frozen beside the top-10-depth integer-cent book
(exact fixed-point values retained alongside). As results settle:
- the 3-way ladder re-run on POST-LAUNCH fixtures only (no selection
  effect) — the real out-of-sample test;
- per-family Brier: locked model vs the frozen executable ask;
- the paper ledger's net-of-fee execution economics;
- calibration curves once n permits.

Promotion past shadow gets decided **in writing, on that prospective
evidence**, in a future edition of this document — never in a config
change.

## Evidence hierarchy (carried from V6/V7)

MEASURED (frozen pre-event, scored on outcome) > REPLAYED (seeded
backtest over recorded data) > PILOT (small-n live anecdote). Everything
here is REPLAYED. Nothing MLS is MEASURED yet — that begins Saturday.
