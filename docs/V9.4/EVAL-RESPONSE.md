# V9.4 — Response to the V9.3 Independent Evaluation

Finding-by-finding. The evaluation raised **21 findings** across three
tiers. Every P0 is corrected, every P1 is either corrected or explicitly
disclosed, and the P2s are corrected except three that are scope, not
defects. Each correction is pinned by a test that **fails without it**.

The evaluation verified our archive hash independently
(`67a49a59…`) and reproduced 477 tests. Its reproductions are the tests
we now ship against.

---

## The finding that mattered most

> **All five V9.3 P0s were already reported in V9.2 and shipped unfixed.**

They survived a whole version because *nothing in the suite exercised
them*. The V9.3 cycle spent its effort on the model and data layers —
where four self-audits found four real defects — while the execution
layer went untouched and unre-checked. That is a process failure, not a
coding one, and it is the reason every fix below ships with a regression
test rather than a claim.

---

# P0 — blocked execution-grade paper results

## F1 — depth sorted on the rounded cent

Sort key was `int(round(float(price) * 100))`. With several levels inside
one cent, Python's stable sort left them in provider order and kept the
first ten. `0.5300 … 0.5311` all round to 53¢, so the **true best bid
0.5311 was discarded**.

**Fixed.** Sorts on the exact `Decimal`; display cents derived only after
selection. Their reproduction now reports `missing_input_best: false`.

## F2 — one fee at the blended VWAP

The general fee contains `P × (1 − P)` and is therefore non-linear, so a
single fee at the average price is not the sum of the per-fill fees.
50 @ $0.10 + 50 @ $0.90 was charged **$1.7500** instead of **$0.6300**.

**Fixed.** `simulate_fill` returns every per-level allocation;
`allocation_fees` charges each at its own price, ceil'd to the centicent,
and the breakdown plus fee-policy version is persisted on the fill.

## F3 — net edge never re-checked after the depth walk

The quoted edge authorised the order; the walk could pay a worse average
and nothing re-applied the policy. A fill with post-fill edge **0.0029**
stood against a **3%** threshold.

**Fixed.** Edge is recomputed from the actual average and allocation fees;
below threshold rejects `POST_FILL_EDGE_BELOW_THRESHOLD`. Both quoted and
realized edges are stored. Their DB reproduction now yields `fills: 0`.

## F4 — a fill with zero captured depth

`yes_buy_ladder` fell back to the top quote, and that fill entered the
same P&L as depth-backed ones.

**Fixed.** Classified `bounded_depth` vs `top_of_book_estimate`. Headline
P&L and ROI are **execution-grade only**; estimates are reported in a
separate block. A test asserts an estimate contributes **zero** to the
headline — labelling alone would have been cosmetic. `require_depth_for_fill`
can refuse them outright.

## F5 — quote linkage passed vacuously

`priced` was filtered to non-null `market_contract_id`, so `all([])` made
a canonical lock with **zero market links** pass the entire audit.

**Fixed.** Requires a non-empty set **and** all three game legs mapped to a
contract and a frozen quote by name (`three_way_market_linked`), and the
run writer now **refuses canonical completion** without them. Their
reproduction now returns `all_pass: false`.

> This broke seven existing tests — which *was* the finding: those
> fixtures modelled locks with no market links. The fixtures were
> corrected to model real locks; the gate was not weakened.

---

# P1 — research integrity and market completeness

## F6 — registry completeness recorded but not required ✅
Lock completeness was self-referential: *everything known locally was
captured*, even if discovery was truncated. A canonical lock now requires
a **complete** Kalshi `RegistryDiscovery` finished within
`REGISTRY_MAX_AGE_HOURS` (6 h; discovery runs every 10 min). Incomplete
**and** stale sweeps are both refused, each pinned by a test.

## F7 — approval not bound to what it approved ✅
The decision now records the exact selected parameters, engine signature,
**data cutoff**, and the published corpus manifest hash (null until one is
published — disclosed, not hidden). These sit **inside the hashed core**:
a test mutates `xg_shrink_games` and asserts the decision hash moves.

## F8 — tuning and evaluation on the same 162 matches ⚠️ **DISCLOSED**
Not fixed. Nested or held-out selection is a research redesign, and
running it hours before the first slate would change the approval on the
eve of the evidence it exists to authorise. Instead the approval record
now **states** the limitation in its own text: the interval is conditional
on the selected model and excludes model-selection uncertainty. The real
remedy is the prospective data the slate produces.

## F9 — the evaluator did not score the deployed generator ✅
`evaluate_deployed()` walks the same rolling origin through the
production path (simulator, red-card sampling, calibration, production
seeds) and reports its metrics beside the analytic ones. Operator
endpoint `GET /api/admin/mls/deployed-eval`.

**Measured, n=162 — and the sim count matters:**

| simulations | deployed | vs analytic 1.0443 |
|---|---|---|
| 1,200 | 1.0453 | noise-dominated, not reproducible |
| 4,000 | 1.0453 | |
| **10,000 (production)** | **1.0444** | **−0.0001** |

At the production count the analytic ladder is a **faithful proxy**. A
cheap run makes the deployed path look worse purely through Monte Carlo
noise, so `n_sims` defaults to `config.N_SIMULATIONS`.

## F10 — corpus not self-contained for model development ✅
`corpus-v2` adds the research plane: approval decisions, registry sweeps,
official per-match team and player statistics, and `model_parameters.json`
carrying the exact deployed configuration **and the selection protocol**,
including the F8 limitation.

## F11 — raw responses truncated ✅
This was our own debt: the 8 KB cap shipped during the Jul 25 DiskFull
incident. **Compression removes the trade-off** — a real MLS response is
9,596 bytes raw and 2,760 gzip+base64 (28.8%), *smaller than the stub it
replaces*. All four observation sites now retain the complete body with
its true length and encoding, and `evidence.verify_payload()` re-hashes
it. Verified on a live ingest.

## F12 — price-grid metadata not stored ✅
`MarketQuote` freezes `price_level_structure` and `price_ranges` at
capture. Verified live: `linear_cent`, step `0.0100` — which is also why
the F1 subpenny defect was still latent.

---

# P2 — execution, operations, maintainability

| # | finding | status |
|---|---|---|
| **F13** | risk gating on rounded cents | ✅ exact `Decimal` price + fractional size; tests pin an 8.4¢ spread that rounds to 8¢ and a 9.4-contract depth that reads as 10 |
| **F14** | aggregates rounded | ✅ headline P&L sums exact `Decimal` over execution-grade fills only |
| **F15** | one narrow order type | ⚠️ **scope, disclosed** — maker orders, cancel/replace, latency, exit fills are genuine future work, not a labelling change |
| **F16** | capture-time freshness | ⚠️ **acknowledged** — the venue publishes no quote-update clock; the basis is recorded as `capture_time` and never dressed up as provider-confirmed |
| **F17** | dependency floors | ✅ exact pins at the tested versions |
| **F18** | frontend tests not deterministic | ✅ `contract-deterministic.spec.ts` serves recorded payloads; 4 hermetic invariants |
| **F19** | current team news vs frozen input | ✅ the section states it is current/as-played and names the frozen run it is **not** |
| **F20** | expensive public reads | ✅ rate-limited singleton routes, `MAX_PUBLIC_BODY_BYTES` ceiling, corpus preview cached (46s → 0.07s warm) |
| **F21** | process-local scheduler | ⚠️ **deferred** — only matters multi-replica, which is not deployed; the riskiest possible pre-slate change |

---

## Errors we made *during* this response

Recorded because the evaluation's central lesson is that unexercised code
hides defects:

- Adding the F10 research sections turned the corpus preview into a
  **46-second public read**. Caught in production verification, fixed with
  caching.
- Rate-limiting `/api/research` would have 429'd a user opening a second
  match — the limiter buckets per *prefix*, so only singleton routes
  belong in it. Caught by an existing test.
- Writing the F18 tests produced **two bugs in our own tests** (a click
  that closed an already-open section; a body read before render). The
  no-model case turned out to be handled correctly already, so the test
  now pins that behaviour instead of our assumption.

---

## What has not changed

Both readiness verdicts stand, and no fix above moves them:

> **Paper P&L: NO-GO for profitability interpretation.**
> **Manual real money: NO-GO.**

The execution layer is materially more faithful, but the model edge is
**+0.0331, CI [−0.0035, +0.0663], n=162**, with hyperparameters selected
on that same sample. Only prospective data changes that — which is
exactly what the first slate is for.
