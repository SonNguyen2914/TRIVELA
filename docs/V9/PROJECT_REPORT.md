# Project Report — V9: From Shadow Prototype to Validation-Ready (July 22–23, 2026)

> **⚠️ SUPERSEDED.** This is the V9 edition (2026-07-23). The current
> edition is **V9.5** on branch `docs-v9.5` — see [`docs/EDITIONS.md`](../EDITIONS.md)
> for the full map. V9 predates the first prospective slate, the
> published corpus, and the paper-coverage / VARCHAR-truncation /
> engine-signature fixes. Kept for the record.

*Successor to `docs/V8/PROJECT_REPORT.md` (the MLS expansion + the first
remediation addenda). This report tells the whole arc as one story: a
shadow platform, two independent evaluations, and the disciplined build
that took it from "plausible prototype" to "provenance-complete,
independently-reproducible, honestly-evaluated, and ready for its first
prospective slate — with money still locked."*

> **A THIRD review, and its P0 build (July 23, 2026).** After this report
> was first written, a third independent evaluation of the V9 zip raised
> 21 findings. The P0 set is now built — persisted CI-based model
> approval, lineup-failure snapshots, provenance foreign keys,
> cursor-complete market discovery, exact fixed-point capture, explicit
> quote-freshness basis, the correct order-level Kalshi fee, immutable
> published corpus artifacts, a frozen-engine replay guard, mode-specific
> readiness, and frontend temporal labeling — with several claims
> narrowed to exactly what the code proves. The finding-by-finding
> response is [`docs/V9/P0-REMEDIATIONS.md`](P0-REMEDIATIONS.md).

## The one-liner

Extended a completed-tournament research archive into a multi-league
live platform, then — under three rounds of independent technical
review — rebuilt its MLS evidence chain until every canonical lock is a
two-phase, completeness-gated, reproducible research artifact; proved the
model's construction while
honestly measuring that its edge over a naive baseline is *not yet
established*; and built the execution, risk, observability, and
audit machinery that a serious shadow study needs — all without ever
opening a path to real money.

## The arc in four movements

**1 — The expansion (V8, Jul 22–23).** The WC26 archive became two
planes: the immutable archive plus a durable PostgreSQL MLS live plane.
Identity, season ingest, a league-fitted model that *earned* its shadow
badge in a walk-forward backtest, full 12-family Kalshi coverage, and a
match hub rebuilt to the operator's own design. Born through a three-bug
PostgreSQL chain — including a 25-minute self-inflicted outage that
became the plane-isolation law: *a subordinate plane must never be able
to kill the primary.*

**2 — The first evaluation (V8 eval).** An independent review found the
architecture sound but the evidence chain not yet atomic — provably: our
own test accepted a canonical lock with zero captured quotes. Twelve P0
findings, all fixed the same day: a completeness-gated `MarketSnapshot`
so no lock forms without a validated book; full provider-schema capture
(sizes + `orderbook_fp` depth, where the old parser silently stored
zero); cursor pagination; team totals from full simulation arrays (the
old truncated-scoreline method understated them by ~2pp, far more at
high scoring); enforced model approval; a stable seed from provider
identity; DST-correct dates; lock-as-primary display; and a "Net edge"
that subtracts the fee. The verdict moved T-10 evidence integrity from
3.5 to 8.4.

**3 — The re-evaluation and its roadmap (V8.1).** The second review
confirmed the central objection closed and laid out a six-level,
sixteen-step path to real-money readiness. Every step that is *code* was
built, in order:
- **Phase 2 — retrievable input artifact.** The last provenance gap: a
  hash proves integrity but not reproducibility unless the bytes it
  hashed are kept. Now every run stores its exact canonical input
  document — including a frozen ENGINE signature (V9 eval F4) — and
  `GET /api/mls/replay/{id}` reproduces it, verified `max_delta 0.0`,
  bit-identical *under the matching engine*; if the engine constants or
  runtime have drifted, replay refuses rather than silently diverging.
  *Level 3 reached.*
- **Phase 3 — the corpus.** A self-contained, hashed, immutable export of
  every research entity, and a one-command analyzer that reproduces
  metrics with **no database** — proven end-to-end against production.
- **Step 5 — PostgreSQL in CI.** The partial index that killed migration
  #1, outcome-key uniqueness, restart readback, and concurrent lock
  contention, all tested against a real `postgres:16` service on every
  push.
- **Step 6 — the input plane.** Lineup / availability / goalkeeper
  snapshots with provenance, attached to each lock, exposing five
  input-quality states — so missing data reads as *pending*, never as
  silent confidence. The model does not consume them yet, by design.
- **Step 7 — the model ladder.** The scientifically decisive step:
  analytic (noise-free) scoring and match-cluster bootstrap CIs replace
  the noisy backtest. The finding is the honest one — raw ratings
  overfit, pooling rescues them significantly, but the edge over the
  baseline is within noise. *This is why the money gate stays shut, now
  with a confidence interval behind it.*
- **Step 8 — execution-quality paper trading.** Realistic depth-walk
  fills against frozen books, net of fees and slippage, a fully-
  referenced deterministic ledger, and settlement — PAPER only, zero
  real-money coupling.
- **Steps 9–11 — risk engine, observability, frontend E2E.** One
  server-side risk authority with correlation grouping and kill
  switches; machine-readable metrics and a full runbook; and the
  frontend's first automated tests (Playwright decision-safety, in CI).

**4 — Step 2's audit half.** The slate scorecard: every fixture on a
matchday classified into exactly one state, with operational-
qualification invariants — so Saturday's live slate grades itself. The
*running* of the slate is the one thing that genuinely waits for real
kickoffs (forcing a lock early would capture stale books and replace the
real one).

## The two lessons worth keeping

**SQLite forgives; PostgreSQL doesn't; local passes, prod fails.** Every
one of the hardest bugs — `canonical IS 1` DDL, a 32-bit seed overflow,
a partial unique index — was invisible on SQLite. The answer wasn't
caution; it was making real PostgreSQL a blocking CI gate.

**The operator's eye and the reviewer's eye both beat the process.** A
phone screenshot caught a data bug (a wrong ESPN name baked into code
*and* its test); a review caught that a "canonical" flag could coexist
with zero evidence. Both found real defects faster than any internal
pass. The response to both was the same: fix it, and add the test that
makes it impossible to reintroduce.

## By the numbers

- **441 backend tests** (+5 real-PostgreSQL run in CI; the one
  network-dependent lineup test is now hermetic, V9 eval F2/F9.8);
  ~13.6k src + ~6.9k test LOC.
- **9 clean production PostgreSQL migrations** (the ninth is the V9 P0
  remediation, head `f9a1c0d2b3e4`, round-tripped empty→head→down on
  real `postgres:16`); the archive never blinked through ~15 deploys;
  **1 outage (25 min)**, converted into a law + test.
- **Model ladder, n=162:** M2 vs baseline +0.008, **CI [−0.012, +0.029],
  not significant** — the honest headline the whole gate rests on.
- **Three independent evaluations**, T-10 integrity 3.5 → 8.4 → P0
  remediations closed; central objection closed.

## Resume bullets (ready to paste)

- Designed a two-plane architecture where a live-plane failure provably
  cannot degrade an immutable research archive; validated by regression
  tests and a real-PostgreSQL CI matrix after a production incident.
- Built a provenance-complete evidence chain — completeness-gated market
  snapshots, retrievable and independently-reproducible model input
  artifacts (engine-signature-guarded replay), and two-phase
  completeness-gated pre-kickoff locks enforced by partial unique
  indexes — under three rounds of independent review.
- Evaluated a forecasting model with analytic (noise-free) scoring and
  match-cluster bootstrap confidence intervals; reported that its edge
  over a naive baseline was not statistically established and kept
  real-money features disabled on that basis.
- Implemented execution-quality paper trading (depth-aware fills, fees,
  slippage, deterministic replay) and a central correlated-exposure risk
  engine with kill switches, shared server-side by every order path.

## Links

- Live: https://namson.dev/bet-suggester?league=mls → any match hub
- Prod evidence: `/api/ready` · `/api/mls/slate` · `/api/mls/audit` ·
  `/api/mls/replay/{id}` · `/api/mls/corpus` · `/api/mls/model-eval` ·
  `/api/mls/paper` · `/api/mls/risk` · `/api/mls/metrics`
- Docs: `docs/V9/` (this arc), `docs/V8/` (expansion + first addenda),
  `docs/V7/` (evaluation hardening), `docs/V6/` (tournament)
