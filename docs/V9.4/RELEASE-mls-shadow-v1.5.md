# Release — `mls-shadow-v1.5`

**The V9.4 slate-producing baseline.** Supersedes `mls-shadow-v1.4`
(V9.3), which carried all five execution P0s the independent evaluation
reported.

---

## Identity

```text
release            mls-shadow-v1.5
backend            37ac74b   main   (code; docs on docs-v9.4)
frontend           df31113   namson-dev main
migration head     e0f1a2b3c4d5
date               2026-07-25
```

## Contract versions

```text
input artifact     model-input-v5
lock policy        mls-lock-v2
paper execution    paper-exec-v4     exact ordering · allocation fees ·
                                     post-fill edge · execution classes
fee policy         kalshi-fee-2026-07-general   (general taker only)
corpus             corpus-v2         run replay + research plane
evaluation         model-eval-v1 / shadow-approval-v1
audit              mls-lock-audit-v1
risk               risk-v1           (exact Decimal gating)
```

## Model parameters (unchanged from v1.4)

```text
MLS_XG_RATING_ALPHA      1.0
MLS_XG_SHRINK_GAMES      6.0     goals keep k=24 as the no-xG fallback
MLS_CALIBRATION_ALPHA    0.25
MLS_GOAL_DISPERSION_CV   0.0     WC26 keeps 0.30
HALF_LIFE_DAYS           90
MIN_GAMES                5
REGISTRY_MAX_AGE_HOURS   6
```

## Approved-for-shadow decision (production)

```text
decision           191 at cut
edge vs baseline   +0.0331
CI 95%             [-0.0035, +0.0663]
n_scored           162
mode               shadow
significant        NO
corpus_manifest    null — no corpus published yet (disclosed, not hidden)
```

> The decision **recomputes on every deploy** (its engine signature
> includes the code revision), so the id moves and the bootstrap CI shifts
> in the third decimal. Hold onto the point estimate and the verdict, not
> the id.

## Verification at cut

```text
tests              492 backend + 10 e2e   (5 PG skipped)
ready              true, no blockers, migrations current
paper              paper-exec-v4, execution_grade separated
corpus preview     cold 25s -> warm 0.07s (cached)
REAL_MONEY_SIGNALS false — no order-placement path exists in the repo
```

## What changed since v1.4

Every item is a V9.3-evaluation finding; see
[`EVAL-RESPONSE.md`](EVAL-RESPONSE.md).

| finding | change |
|---|---|
| F1 | depth ordered by exact `Decimal`, not the rounded cent |
| F2 | fees charged on actual per-level allocations |
| F3 | strategy policy re-applied after the fill (`POST_FILL_EDGE_BELOW_THRESHOLD`) |
| F4 | `bounded_depth` vs `top_of_book_estimate`; headline P&L excludes estimates |
| F5 | all three market legs required; audit no longer vacuous |
| F6 | complete registry sweep required within 6 h |
| F7 | approval records parameters, cutoff, corpus hash — inside the hash |
| F9 | `evaluate_deployed()` scores the production generator |
| F10 | `corpus-v2` research plane |
| F11 | complete raw payloads (gzip+base64), re-verifiable |
| F12 | active price grid frozen with the quote |
| F13/F14 | exact `Decimal` risk gating and aggregates |
| F17 | dependencies pinned |
| F18/F19 | hermetic frontend tests; current-vs-frozen labelling |
| F20 | rate limits, body ceiling, cached corpus preview |

**Disclosed, not fixed:** F8 (in-sample selection), F15 (single order
type), F16 (capture-clock freshness), F21 (process-local scheduler).

## Rollback

```bash
git checkout mls-shadow-v1.4
```
The trade: v1.4 reintroduces all five execution P0s — inverted depth
selection on subpenny books, VWAP fees, unchecked post-fill economics,
unlabelled no-depth fills, and a vacuously passable lock audit. Prefer
fixing forward.

## Known limits at cut

- model edge **not significant**; hyperparameters selected in-sample
- paper execution models one order type only (taker entry, hold to
  settlement, general fee approximation)
- freshness is capture-time by necessity — the venue publishes no
  quote-update clock
- settlement and the in-play path remain unexercised against a real
  resolved fixture until the first slate completes
- no corpus published yet, so the approval is not yet corpus-bound
