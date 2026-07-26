# WC26 → Multi-League Platform — Project Documentation & Roadmap

> **⚠️ SUPERSEDED.** This is the V9 edition (2026-07-23). The current
> edition is **V9.5** on branch `docs-v9.5` — see [`docs/EDITIONS.md`](../EDITIONS.md)
> for the full map. V9 predates the first prospective slate, the
> published corpus, and the paper-coverage / VARCHAR-truncation /
> engine-signature fixes. Kept for the record.

**V9 — July 23, 2026. THE VALIDATION-READY EDITION.** V8 opened the MLS
expansion; V9 closes the arc that followed it — two independent
evaluations of the shadow platform and the full remediation-plus-roadmap
build they prescribed. Every step of that roadmap that is *code* is now
done: a two-phase, completeness-gated evidence chain; retrievable,
independently-reproducible model inputs; a self-contained research
corpus; a model-development ladder scored with noise-free analytics and
confidence intervals; depth-aware paper trading; a central risk
engine; operational observability; real-PostgreSQL CI; frontend
decision-safety E2E; and a slate scorecard standing by to grade the
first live matchday. The system does exactly what a serious shadow
research platform should: it collects provenance-complete evidence,
tells the truth about what it does and does not know, and keeps every
path to real money closed. Docs live in `docs/V9/`; V8 remains the
expansion snapshot; V7 the evaluation-hardening close; V6 the
tournament close; V5 on `docs-v5-handoff`.

> **V9 P0 remediation pass (July 23, 2026):** a *second* independent
> evaluation of the V9 zip raised 21 findings; the P0 set is now built.
> See [`docs/V9/P0-REMEDIATIONS.md`](P0-REMEDIATIONS.md) for the
> finding-by-finding response — and for the **precise, narrowed claims**
> it settles: the T-10 lock is *two-phase completeness-gated* (not one
> atomic transaction); replay is *bit-identical under the matching engine*
> (an engine signature is frozen and drift is refused, not silently
> replayed); the corpus is *immutable once published* (served from stored
> bytes); the book is the *top-10-depth required-family* book; paper fees
> are the *order-level general schedule, approximate* (no maker/taker or
> series overrides). Migration head is now `f9a1c0d2b3e4`.

---

## ⚡⚡⚡⚡ CURRENT STATE — V9 SNAPSHOT (July 23, 2026)

### The one-paragraph version
One backend, two planes. The **archive plane** is the completed WC26
research record: fail-closed read-only, self-healing at every deploy
(16 results / 84 ledger / 6 canonical lock bundles), and a live-plane
failure cannot touch it. The **live plane** is the MLS shadow platform
on durable Railway PostgreSQL: the full 2026 season ingested from ESPN,
approved-alias identity, integer-cent capture across all 12 per-match
Kalshi families, and a league-fitted model (`mls-2026-v0`) whose every
run is a status-gated evidence package — deterministic seed from stable
provider identity, a retrievable canonical input document (with a frozen
engine signature), a two-phase completeness-gated T-10 lock frozen
against a completeness-validated market snapshot with the lineup it saw
(recorded even on a provider fetch failure), and every priced contract
joined to its frozen quote. On top of that: paper trading with realistic depth-walk fills, a
central risk engine, a model-eval ladder with bootstrap CIs, a
self-contained corpus, observability, and a slate scorecard. Public
site at namson.dev/bet-suggester (`?league=mls`). **Real-money
recommendations are disabled and no code path can enable them.**

### Where things stand right now
- **Repos:** backend `~/dev/wc26-bet-suggester` (**441 tests green + 5
  real-PostgreSQL run in CI** — the one network-dependent lineup test is
  now hermetic, V9 eval F2/F9.8; ~13.6k LOC src+api+jobs); frontend
  `~/dev/namson-dev`. Local == remote == deployed.
- **CI (both repos, blocking):** backend runs the SQLite suite + real
  `postgres:16` migrations (empty→head and previous-release→head) +
  the PostgreSQL integration tests; frontend runs install + typecheck +
  lint + build + Playwright decision-safety E2E. Both green.
- **Prod:** `/api/ready` reports both planes healthy, mode-specific
  readiness (`archive_ready` / `shadow_collection_ready` /
  `paper_execution_ready`, V9 eval F17), `real_money_signals: false`,
  migrations at head `f9a1c0d2b3e4`.
- **Frozen baseline:** tag `mls-shadow-v1` on both repos
  (`docs/V9/RELEASE-mls-shadow-v1.md`) — the version that collects the
  first slate.
- **Next real event:** Saturday July 25, a 15-fixture slate — the first
  live T-10 locks and the first prospective evidence. The slate
  scorecard reads all PENDING and will classify itself as the day runs.

### The launch-level model (do not collapse into "production ready")
| Level | Meaning | Status |
|---|---|---|
| 1 Historical archive | WC26 evidence preserved and served honestly | **Ready** |
| 2 MLS shadow collection | T-10 model + market evidence frozen prospectively | **Ready** |
| 3 Research reproducibility | Another machine reconstructs every result | **Ready** |
| 4 Execution-quality paper | Realistic fills: price, depth, fees, slippage | Machinery ready, evidence pending |
| 5 Manual real-money | Human-reviewed signals under validated controls | Not ready (by design) |
| 6 Automated execution | Safe order placement + reconciliation | Not ready (separate final project) |

---

## PART A — ARCHITECTURE (the two-plane law)

One FastAPI process; two isolated storage planes. A live-plane failure
disables the live plane (records `LIVE_BOOT_ERROR`, surfaced by
`/api/ready`) and the archive keeps serving — proven the hard way by a
25-minute self-inflicted outage that became the plane-isolation law.

| | Archive (WC26) | Live (MLS) |
|---|---|---|
| Storage | SQLite in-container, ephemeral | Railway **PostgreSQL + volume**, durable |
| Schema | `src/db.py`, created at boot | `src/live/models.py`, **Alembic** (head `f9a1c0d2b3e4`) |
| On deploy | Self-heals from committed artifacts | Persists; migrations run once at boot |
| Writes | Operator-token only (fail-closed) | Scheduler jobs only |

### Live-plane modules (`src/live/`)
- `db.py` — `live_enabled` / `plane_ready` / `LIVE_BOOT_ERROR` /
  `migrate_and_seed` (catch-all; never kills the archive).
- `models.py` — 22 tables (identity, evidence chain, market snapshot +
  quotes + depth, model-input artifact, lineups, paper ledger).
- `identity.py` — ESPN seeding + curated `KALSHI_BRIDGES`; approved
  aliases only attach markets.
- `ingest.py` — season schedules (played + `?fixture=true`) + rolling
  window; reschedules as history; hashed observations.
- `markets.py` — 12-family discovery (suffix-join), current-schema
  capture (`*_fp` / `orderbook_fp`), cursor pagination, the
  completeness-gated **lock snapshot**.
- `lineups.py` — ESPN roster → provenance-complete lineup snapshot +
  the input-quality states.
- `model_mls.py` — `mls-2026-v0`; the retrievable input artifact +
  deterministic replay.
- `runs.py` — status-gated prediction runs, the two-phase T-10 lock, the
  approval gate, the public/hub payloads.
- `paper.py` — depth-aware paper trading, order-level general fees (Part E).
- `risk.py` — the central risk engine (Part F).
- `audit.py` / `slate.py` / `corpus.py` / `model_eval.py` /
  `observability.py` — the evidence + evaluation surfaces (Part G).

### Scheduler (`jobs/scheduler.py`)
MLS jobs are lazy-imported and `plane_ready`-gated: `mls_window` 15m
(refresh + paper settlement), `mls_markets` 10m, `mls_runs` 15m,
`mls_t10` 60s. `mls_boot` is its own one-shot, never chained into the
archive boot.

---

## PART B — THE MODEL (`mls-2026-v0`) & ITS HONEST EVALUATION

Interpretable goals-rate baseline through the shared Monte Carlo engine:
recency-weighted (90-day half-life), shrunk (k=24) attack/defence
ratings; fitted league gpg + venue split; set-piece neutral; honest
zeros for form/fatigue. Deterministic seed from the **ESPN event id**
(not a mutable row id), masked to 31 bits for PG's signed integer.
Team totals and all marginals come from the **full simulation arrays**,
never a truncated scoreline list.

**The evaluation is the honest part** (`src/live/model_eval.py`,
`GET /api/mls/model-eval`; full tables in `docs/V9/CALIBRATION.md`). The
ladder M0 (league+venue) / M1 (raw ratings) / M2 (recency+pooling =
mls-2026-v0) is scored with **analytic independent-Poisson 3-way
probabilities** — zero Monte Carlo noise — under rolling-origin
validation, with **match-cluster bootstrap 95% CIs** on every edge.
Result on 162 fixtures: raw ratings *overfit* (M1 worse than baseline);
recency+pooling *rescues* them decisively (M2 vs M1 significant); but
**M2's edge over the naive baseline is within noise** (CI spans 0). The
model's construction is validated; a durable forecasting edge is not.
`approved_for_shadow` is earned and means "safe to collect prospective
evidence," never "edge established" — the approval record says so.

---

## PART C — THE EVIDENCE CHAIN (per run)

Every batch is a `prediction_run`: UUID, status gating (readers see only
`complete`; no time-window reconstruction anywhere), stored seed + sim
count + git revision, **frozen approval record** (`model_approved_at_run`),
a link to the **retrievable input artifact** (the exact canonical
document it simulated from — replay it via `GET /api/mls/replay/{id}`,
verified `max_delta 0.0`), the **market snapshot** it priced against, the
**lineup snapshot** it saw, the frozen **input-quality states**, and one
contract per priced outcome each joined to its frozen quote.

**The two-phase, completeness-gated T-10 lock** (`runs.t10_locks`, 60s):
fixtures 0–11 min from kickoff without a canonical lock get (1) a
completeness-gated market snapshot — every event fetched and the game
3-way priced, else it stays `failed` and no lock happens; (2) a lineup
snapshot, recorded even when the provider fetch fails (V9 eval F2), so
the lock never references a null lineup; (3) the transactional run frozen
against both; (4) paper trading; (5) a PAPER-labeled alert. Two-phase,
not one transaction: the completed snapshot commits before the run
(V9 eval F11) — a crash can orphan a snapshot but never fabricate a lock. One canonical complete t10 per fixture, enforced by
a partial unique index proven on PostgreSQL. A missing lock stays
visibly missing.

---

## PART D — MARKET COVERAGE

17 MLS series exist; 12 are per-match. All 12 are discovered, mapped by
exact ticker-suffix join to the game event, captured in integer cents
with sizes + `orderbook_fp` depth, and shown. Outcome keys are parsed
from machine-readable ticker tails, never label text. The model prices
the 3-way, totals ladder, BTTS, margins, first-goal, team totals, and
scorelines (~35 contracts/run); MOV + 1st-half are market-only.

---

## PART E — PAPER TRADING (execution evidence, never real orders)

`src/live/paper.py` (`GET /api/mls/paper`). Each lock's positive-net-edge
3-way contracts become paper decisions, gated by the risk engine (Part
F). A fill walks the **real** book — buying YES consumes the NO-bid
ladder (`yes_ask = 100 − no_bid`) level by level, partial fills when
depth runs out, net-of-fee, net-of-slippage — and is fully referenced
for deterministic replay. Rejections keep their reason (no survivorship
bias). `settle_paper` pays 100¢/contract on a hit as results land.
`PAPER_TRADING_ENABLED` is a kill switch, **not** a money gate: it has
zero coupling to `REAL_MONEY_SIGNALS_ENABLED`.

---

## PART F — THE RISK ENGINE (one server-side authority)

`src/live/risk.py` (`GET /api/mls/risk`). Every order path — paper now,
any future executor — passes through it. Two gate classes with named
reasons: **market** (MODEL_NOT_APPROVED, NOT_EXECUTION_READY,
QUOTE_STALE, NO_EXECUTABLE_ASK, INSUFFICIENT_SIZE, SPREAD_TOO_WIDE,
NET_EDGE_TOO_LOW) and **exposure** (MAX_POSITIONS, TOTAL_RISK_LIMIT,
MATCH/CORRELATED/TEAM_EXPOSURE_LIMIT, BANKROLL_RESERVE). **Kill
switches** sit above both (config `GLOBAL_/COMPETITION_TRADING_DISABLED`
+ data-driven `DAILY_LOSS_LIMIT`); the safest state is no new orders.
**Correlation grouping** collapses home_win / home_margin / home_team_
over / home_first_goal into one match-direction budget, so the same
opinion can't be stacked across families. Versioned `RISK_POLICY` —
explicit settings, no hidden constants.

---

## PART G — EVIDENCE & EVALUATION SURFACES (all public, read-only)

- `/api/mls/slate?date=` — the matchday scorecard: every fixture in
  exactly one state (PASS / MISSED / CAPTURE_FAILED / LOCK_FAILED /
  EXECUTION_NOT_READY / SETTLEMENT_FAILED / LEGACY_UNSCORABLE / PENDING
  / INTEGRITY_FAILED) + operational-qualification invariants.
- `/api/mls/audit` — every lock's integrity checks; missed locks +
  failed snapshots retained; content-hashed.
- `/api/mls/replay/{run_id}` — reproduce a run from its stored artifact.
- `/api/mls/corpus` (`?full=1`) — the self-contained research bundle;
  `scripts/analyze_corpus.py` reproduces metrics with no database.
- `/api/mls/model-eval` — the ladder + bootstrap CIs + approval record.
- `/api/mls/paper` — the paper P&L ledger.
- `/api/mls/risk` — policy, active kill switches, open exposure.
- `/api/mls/metrics` — operational observability.
- `/api/ready` — both planes, `shadow_ready` + named blockers.
- Operator: `POST /api/admin/mls/sweep?force=1` (token-gated).

---

## PART H — THE SITE (namson.dev/bet-suggester)

Board with league deep-linking (`?league=mls`), shadow odds chips, real
KXMLSGAME books. Match hub (`/bet-suggester/mls/{eventId}`): compact
info card, xG duel, folded ratings + ESPN scouting, **aligned three-way
market-vs-model bars in club colours**, the input-quality row, the
every-market table with a **fee-aware Net edge** column (never a bare
"edge" or a generic "TAKE"), scenario engine, state-aware live stats.
Everything model-made is labelled shadow / not advice. Playwright E2E
pins these decision-safety invariants.

---

## PART I — OPS & THE MONEY GATE

Runbook: `docs/V9/RUNBOOK.md` (a procedure for every incident the
evaluations named + the kill-switch reference). Modes are fail-safe
parsed (unknown → safer). The money gate is layered and unconditional:
`REAL_MONEY_SIGNALS_ENABLED=false` with no code path that sets or reads
it to act, `approved_for_real_money` a column no path sets, the frontend
saying "not advice" on every model surface, and the model's own
evaluation recording that the edge is not established. Nothing here is a
recommendation.

---

## PART J — WHAT REMAINS (honest, and mostly not code)

Every *buildable* roadmap step is done. What's left are gates:
1. **Prospective paper period** — needs Saturday onward to elapse and
   produce settled evidence (the slate/audit/paper/model-eval endpoints
   grade it automatically).
2. **Formal approval review** — a written decision *on* that evidence.
3. **Manual real-money** — gated on (2) and on judging the existing
   data/risk/operator controls sufficient.
4. **Automated execution** — a separate project: real Kalshi
   credentials, the demo environment, authenticated order/fill
   reconciliation, and legal/compliance review of the developer
   agreement and jurisdiction.

Smaller open items: prospective forecast scoring vs the locks (automatic
once Saturday settles), a 1st-half model (simulator halves data exists,
unused), prop settlement in paper trading, API-Football id discovery,
and an automated backup-restore rehearsal. **The next thing that moves
this project is not a commit — it is the first live slate.**
