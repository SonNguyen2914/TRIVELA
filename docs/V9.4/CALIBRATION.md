# V9.4 — Model & Calibration

`mls-2026-v0`, as deployed for the `mls-shadow-v1.5` baseline. The
MODEL IS UNCHANGED from V9.3 — V9.4 corrected the execution and evidence
layers beneath it, not the forecast. Every
number here comes from the same 162-match rolling-origin walk-forward with
analytic (noise-free) 3-way scoring and match-cluster bootstrap CIs.

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

Everything above is frozen into a `model-input-v5` artifact, so a run
replays byte-for-byte from stored bytes alone.

---

## The ladder

| rung | log-loss | Brier | RPS | description |
|---|---|---|---|---|
| M0 | 1.0776 | 0.6530 | 0.2349 | league scoring + venue split |
| M1 | 1.1658 | 0.6954 | 0.2550 | ratings, equal-weighted, minimal pooling |
| M2 | 1.0697 | 0.6469 | 0.2322 | + recency + partial pooling (the old v0) |
| M2C | 1.0608 | 0.6404 | 0.2290 | + calibration toward uniform |
| **M3** | **1.0443** | **0.6286** | **0.2239** | **+ provider xG ratings — DEPLOYED** |

| edge | Δ log-loss | CI 95 % | significant |
|---|---|---|---|
| M2 vs M0 | +0.0079 | [−0.0123, +0.0274] | no |
| M2 vs M1 | +0.0961 | [+0.0245, +0.1773] | **yes** |
| M2C vs M2 | +0.0088 | [−0.0066, +0.0253] | no |
| M3 vs M2C | +0.0165 | [−0.0021, +0.0361] | no |
| **M3 vs M0** | **+0.0332** | **[−0.0015, +0.0674]** | **no** |

n = 162. M1 losing to M0 is the standing reminder that more signal can
hurt: raw ratings without pooling overfit.

**Does the ladder score what actually ships? (V9.3 eval F9 — now measured.)**
The ladder scores an analytic independent-Poisson representation; production
also samples red cards and calibrates. `evaluate_deployed()` walks the same
rolling origin through the production path:

| simulations | deployed log-loss | vs analytic 1.0443 |
|---|---|---|
| 1,200 | 1.0453 | noise-dominated, not reproducible |
| 4,000 | 1.0453 | |
| **10,000 (production)** | **1.0444** | **-0.0001** |

At the production count the analytic ladder is a faithful proxy. A cheap
run makes the deployed path look worse purely through Monte Carlo noise.

> These are one reference run (seed 12345, 2000 bootstrap resamples).
> Production recomputes the ladder on every deploy, so the CI bounds move
> in the third decimal and the point estimates by ~0.0001. Live at cut:
> M3 vs M0 **+0.0331**, CI [−0.0032, +0.0664].

---

## Calibration of the 3-way

Predicted vs actual over the scored fixtures:

| outcome | mean predicted | actual | bias |
|---|---|---|---|
| home win | 46.8 % | 45.7 % | +1.1 pp |
| draw | 23.5 % | 24.1 % | −0.6 pp |
| away win | 29.7 % | 30.2 % | −0.6 pp |

No systematic bias. The residual disagreement with the market (mean 5 pp
across the slate, 2 favourite-flips in 14, both near coin-flips) is a
difference of opinion, not a model bias.

**Why a uniform anchor and not the league base rate?** Measured: the
league-base anchor is *worse* (1.0501–1.0512 vs 1.0443–1.0454), because the
model already prices home advantage — anchoring to a base rate that also
contains it double-counts.

---

## Props are deliberately *not* calibrated

| market | mean predicted | actual | bias |
|---|---|---|---|
| over 1.5 | 83.1 % | 85.2 % | −2.1 pp |
| over 2.5 | 62.8 % | 64.8 % | −2.0 pp |
| over 3.5 | 41.0 % | 43.8 % | −2.8 pp |
| BTTS | 61.2 % | 67.3 % | −6.1 pp |

Props are slightly **under**-confident, the opposite of the 3-way, and
shrinking them toward 0.5 makes log-loss monotonically worse
(0.588 → 0.604). Applying the 3-way's correction to them would be the same
mistake the "win% blend" was.

---

## Parameter provenance

Every tuned constant, and **what it was measured against** — the field
whose absence caused three of the four V9.3 defects:

| constant | value | swept under | evidence |
|---|---|---|---|
| `MLS_XG_RATING_ALPHA` | 1.0 | xG ratings | monotonic; M3 vs M2C +0.0165 |
| `MLS_XG_SHRINK_GAMES` | 6.0 | **xG ratings** | interior optimum k=4–6; all of 3–8 beats k=24 |
| `SHRINK_GAMES` | 24.0 | **goals** | k=6 *lost* for goals; fallback path only |
| `HALF_LIFE_DAYS` | 90 | re-swept under xG | flat optimum 45–120 d |
| `MLS_CALIBRATION_ALPHA` | 0.25 | xG ratings | flat optimum 0.15–0.35 |
| `RESULT_SHRINK` | 8.0 | — | **no longer affects any probability** |
| `MLS_GOAL_DISPERSION_CV` | 0.0 | **MLS props** | monotonic; +0.0277 prop log-loss |
| `GOAL_DISPERSION_CV` | 0.30 | **WC26** | untouched — archive replays bit-for-bit |
| `MIN_GAMES` | 5 | re-swept under xG | best of 3/4/5/6/8 |

---

## Rejected features (measured, not shipped)

| candidate | best result | verdict |
|---|---|---|
| XI-strength adjustment | +0.0008, then −0.0094 | team xG already encodes who played |
| goalkeeper shot-stopping | −0.0055 → −0.0429 | monotonically worse; save% not persistent at this sample |
| key-attacker availability | +0.0034, CI [−0.0148, +0.0213] | real but not significant; revisit with more data |
| λ-scaling for the goal-rate bias | −0.0083 on over-2.5 | fixes the mean, overshoots |

---

## Known residuals

- **Goal rate.** Predicted mean total 3.264 vs actual 3.438 (−0.174/match).
  Diagnosed; the obvious correction was tested and rejected. Open.
- **Significance.** The deployed edge's CI still spans zero at n=162.
- **In-sample tuning.** Every sweep above selects a parameter on the same
  162 matches. The approval record now STATES this (V9.3 eval F8): the
  interval is conditional on the selected model and excludes
  model-selection uncertainty. Finding 2 in [`AUDIT-FINDINGS.md`](AUDIT-FINDINGS.md) shows
  precisely how much that can inflate an apparent edge (+0.0038 → −0.0007
  when fitted honestly). Read every gain here as optimistic.

**Shadow approval means "safe to collect prospective evidence". It has
never meant "edge established".**
