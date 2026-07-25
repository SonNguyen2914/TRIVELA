# WC26 → Multi-League Platform — Project Documentation (V9.4)

**V9.4 — July 25, 2026. THE EXECUTION-MEASUREMENT EDITION.** V9.3 rebuilt
the *model* on official expected goals and audited it four times. The
independent V9.3 evaluation then found that the layer we had **not**
touched — the economic transformation from a frozen order book into a
simulated trade — still carried all five P0s it had reported against
V9.2. V9.4 is the answer: every P0 corrected, every P1 corrected or
explicitly disclosed, each pinned by a test that fails without its fix.
The slate-producing baseline is **`mls-shadow-v1.5`**.

Docs live in `docs/V9.4/`; V9.3 is superseded. The finding-by-finding
answer is [`EVAL-RESPONSE.md`](EVAL-RESPONSE.md).

---

## ⚡ CURRENT STATE — V9.4 SNAPSHOT

### One paragraph
One backend, two isolated planes. The **archive plane** is the completed
WC26 record: fail-closed read-only, self-healing, 11/11 endpoints healthy.
The **live plane** is the MLS shadow platform on durable Railway
PostgreSQL. The model rates teams from the official provider's expected
goals; the 3-way carries an explicit calibration term; every tuned
constant has been re-swept under the model that ships. What changed in
V9.4 is beneath that: depth is ordered by **exact price**, fees are
charged on **actual per-level allocations**, the strategy policy is
**re-applied to the economics a fill really achieved**, depth-backed
fills are separated from top-of-book estimates, a canonical lock must
prove **all three market legs** and a **complete registry sweep**, and
every raw provider response is retained **whole**. Real-money signals are
disabled and no code path can enable them.

### The frozen baseline
```text
release:            mls-shadow-v1.5
backend:            37ac74b  main   (13 commits past the V9.3 baseline 8fd791f)
frontend:           df31113
migration head:     e0f1a2b3c4d5
input artifact:     model-input-v5
lock policy:        mls-lock-v2
paper execution:    paper-exec-v4     (exact ordering, allocation fees,
                                       post-fill edge, execution classes)
corpus:             corpus-v2         (adds the research plane)
evaluation:         model-eval-v1 / shadow-approval-v1
tests:              492 backend + 10 e2e   (5 PG skipped)
money:              REAL_MONEY_SIGNALS_ENABLED=false — LOCKED
```

### The deployed model (unchanged from V9.3)
```text
ratings basis       provider xG        MLS_XG_RATING_ALPHA    = 1.0
xG prior            k = 6              MLS_XG_SHRINK_GAMES    = 6.0
goals prior         k = 24 (fallback only, when a fixture lacks xG)
recency             90-day half-life
3-way calibration   alpha 0.25 toward uniform
goal dispersion     MLS 0.0            (WC26 keeps 0.30 — archive intact)
ladder              M0 · M1 · M2 · M2C · M3     deployed = M3
```

---

## WHAT CHANGED FROM V9.3

| area | V9.3 | V9.4 |
|---|---|---|
| depth ordering | rounded cent | **exact `Decimal`** |
| fees | one fee at the VWAP | **per-level allocations** |
| edge policy | checked at the quote only | **re-checked after the fill** |
| no-depth fills | mixed into P&L | **classified + excluded from headline** |
| lock market linkage | vacuously satisfiable | **all three legs required** |
| lock registry | recorded, unused | **complete sweep required (6 h)** |
| raw payloads | truncated at 8 KB | **complete, gzip+base64, re-verifiable** |
| price grid | not stored | **frozen with the quote** |
| risk gating | rounded cents | **exact price + fractional size** |
| corpus | run replay only | **`corpus-v2` + research plane** |
| approval record | metrics + engine | **+ parameters, cutoff, corpus hash (hashed)** |
| deps | floors (`>=`) | **exact pins** |
| frontend tests | live-data smoke only | **+ 4 hermetic contract tests** |

---

## ENDPOINT SURFACE (additions in V9.4)

```text
GET /api/admin/mls/deployed-eval    operator: score the EXACT deployed
                                    probability generator (F9)
```
`GET /api/mls/paper` now separates **`execution_grade`** P&L from an
**`estimate_only`** block. `GET /api/mls/corpus?preview=1` is cached
(300 s) and size-capped; published versions are unaffected and remain the
real download path.

Expensive public reads (`model-eval`, `corpus`, `audit`, `refresh-all`)
are rate-limited. The limiter buckets per **prefix**, so per-match routes
are deliberately excluded — listing one would 429 a user opening a second
match.

---

## THE EVIDENCE CONTRACT (a canonical T-10 lock)

A lock must now satisfy, as enforced invariants:

1. a persisted CI-based **approval decision** whose hash recomputes and
   whose engine signature matches the current engine — and which now
   records the exact parameters, data cutoff and bound corpus;
2. a `model-input-v5` artifact fingerprinting model/simulator **source +
   runtime**, replayable byte-for-byte;
3. a completeness-gated **market snapshot** with best-10-per-side depth
   ordered by **exact price**, at exact provider precision, freshness on a
   clock that exists at this venue, and the **active price grid**;
4. **all three game legs** mapped to a market contract and a frozen quote;
5. a **complete registry sweep** within 6 hours;
6. the **lineup it saw**, or an explicit fetch-failure snapshot;
7. raw provider responses retained **whole** and re-verifiable.

Any lock failing any of these is a defect, not a curiosity — and the
slate report says so per fixture.

---

## THE DISCIPLINE, RESTATED

Nothing here improves the forecast. It improves whether the *measurement*
of execution can be believed. The model's standing result is unchanged
and unproven:

```text
M3 vs M0   +0.0331   CI [-0.0035, +0.0663]   n = 162   NOT significant
```

Shadow approval means "safe to collect prospective evidence". It has never
meant "edge established", and V9.4 does not move it.
