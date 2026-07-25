# WC26 → Multi-League Platform — Project Documentation (V9.3)

**V9.3 — July 25, 2026. THE MEASURED-MODEL EDITION.** V9.2 froze an
execution-fidelity baseline whose model still priced matches from *goals*.
V9.3 records what happened when the model was given **real data** and then
**audited four times**: official MLS (Sportec) team + player statistics were
ingested, the model was rebuilt on **provider expected goals**, and a
sequence of audits found that three separate constants had been inherited
from contexts where they were measured and were wrong here — including one
gate that would have made the first slate collect **no execution evidence
at all**. The slate-producing baseline is now **`mls-shadow-v1.4`**.

Docs live in `docs/V9.3/`; V9.2 is superseded (frozen pre-slate snapshot).
The audit record is [`AUDIT-FINDINGS.md`](AUDIT-FINDINGS.md).

---

## ⚡ CURRENT STATE — V9.3 SNAPSHOT

### One paragraph
One backend, two isolated planes. The **archive plane** is the completed
WC26 record: fail-closed read-only, self-healing (16 results / 84 ledger /
6 lock bundles), 11/11 endpoints healthy. The **live plane** is the MLS
shadow platform on durable Railway PostgreSQL (now a 5 GB volume). The
model, `mls-2026-v0`, no longer rates teams by goals: attack and defence
come from the **official provider's expected goals**, carried by a
dedicated ingestion source with its own schema, identity bridge and
content-hashed evidence. Every tuned constant in the model has been
**re-swept under the model that actually ships**, and the 3-way carries an
explicit **calibration** term that is named for what it does. Each
canonical **T-10 lock** still satisfies the full V9.2 evidence contract,
with freshness now measured on a clock that exists at this venue.
**Real-money signals are disabled and no code path can enable them.**

### The frozen baseline
```text
release:            mls-shadow-v1.4
backend:            8fd791f  main   (38 commits past the V9.2 baseline f875c6f)
frontend:           950b04c
migration head:     c8d9e0f1a2b3
input artifact:     model-input-v5   (freezes the calibration term)
lock policy:        mls-lock-v2      (capture-clock freshness)
paper execution:    paper-exec-v3    (exact Decimal, centicent fees)
evaluation:         model-eval-v1 / shadow-approval-v1
tests:              477 backend + 6 e2e + 3 page-health   (5 PG skipped)
money:              REAL_MONEY_SIGNALS_ENABLED=false — LOCKED
```

### The deployed model
```text
ratings basis       provider xG            MLS_XG_RATING_ALPHA   = 1.0
xG prior            k = 6                  MLS_XG_SHRINK_GAMES   = 6.0
goals prior         k = 24 (fallback only, when a fixture lacks xG)
recency             90-day half-life
3-way calibration   alpha 0.25 toward uniform   MLS_CALIBRATION_ALPHA
goal dispersion     MLS 0.0                MLS_GOAL_DISPERSION_CV
                    WC26 0.30 (unchanged — the archive replays bit-for-bit)
ladder              M0 · M1 · M2 · M2C · M3      deployed = M3
```

---

## THE DATA LAYER (new in V9.3)

**Source.** `stats-api.mlssoccer.com` — public, no-auth, Sportec-powered.
Per-match **team** statistics (real `xG`, shots inside/outside the box, on
target, corners, passing) and per-match **player** statistics (`xG`,
minutes, goalkeeper flag, shots).

**Ingestion** (`src/live/mls_stats.py`). Throttled, idempotent, raw
responses content-hashed into `SourceObservation`. A stats match attaches
to our fixture by the two clubs' resolved ids plus kickoff date — Sportec's
`three_letter_code` equals our ESPN `abbrev` for all 30 clubs, verified 1:1
with zero unmapped. Wired into boot (gap-filling) and a 3-hourly refresh.

**Identity** (`src/live/player_bridge.py`). ESPN athlete ↔ Sportec player,
built by matching **per-match participant lists** — validated at **99.2 %**
of all participants and **99.5 %** of starters. The obvious approach (the
ESPN team-roster endpoint) reaches only ~83 %: it returns the *current*
squad, so mid-season transfers erase a club's own season top-scorers.

**Coverage (production).** Team stats 238/238 matches, player stats 238/238
(9,481 rows), bridge 98.9 % of players and **99.4 % minutes-weighted**.
Verifiable live at `GET /api/mls/stats-coverage`.

---

## WHAT CHANGED FROM V9.2

| area | V9.2 | V9.3 |
|---|---|---|
| ratings | goals | **provider xG** |
| xG prior | — | **k=6**, separate from the goals prior |
| 3-way post-processing | "win% blend" | **calibration** toward uniform (α 0.25) |
| goal dispersion | WC26's 0.30 everywhere | **MLS 0.0**, WC26 unchanged |
| lock freshness | provider timestamp | **capture clock** (`mls-lock-v2`) |
| artifact | `model-input-v4` | **`model-input-v5`** |
| ladder | M0/M1/M2/M2W | **M0/M1/M2/M2C/M3** |
| match page | form + H2H | **+ team news (XI, xG/90, absentees)** |

---

## ENDPOINT SURFACE (additions in V9.3)

```text
GET  /api/mls/stats-coverage        team + player stats coverage, bridge health
GET  /api/admin/mls/storage         operator: DB size + largest tables  (token)
POST /api/admin/mls/stats-backfill  operator: full-season stats backfill (token)
```
`GET /api/mls/match/{id}` gains a **`lineups`** section: per side the
formation, starting XI (jersey, position, GK, each player's own xG/90),
bench, and "not starting" for a club's top attackers — badged *bench* vs
*out*. It is **display context only**; an unreleased lineup reads
"awaiting team news" and asserts **no** absences.

All three admin endpoints reject a missing or wrong token with 403.

---

## THE DISCIPLINE, RESTATED

Every model change in V9.3 was measured on the 162-match rolling-origin
walk-forward **before** deployment, and two candidate features were
**rejected** by that measurement rather than shipped:

- an XI-strength adjustment (best +0.0008, degrading to −0.0094),
- a goalkeeper shot-stopping term (monotonically worse, −0.0055 → −0.0429),
- a λ-scaling correction for the residual goal-rate bias (fixes the mean,
  overshoots over-2.5).

The deployed edge remains **not statistically significant** at n=162. It is
shadow evidence, not an established executable edge, and the money gate
stays closed.
