# WC26 → Multi-League Platform — Project Documentation (V9.5)

**V9.5 — July 26, 2026. THE FIRST PROSPECTIVE SLATE EDITION.** Every
edition before this one described a system that had never been tested
forward. On July 25 the platform locked its first 15 fixtures at T-10,
they kicked off, they settled, and there is finally an answer to the
only question that mattered: *does the machinery hold up when it is not
being watched?*

It did. The machinery passed every invariant it was built to satisfy.

The **evidence layer** did not. Reading the result honestly required
finding three defects, all of the same family — a number published
without the context that makes it readable — and one of them had
silently inverted the headline. The slate-producing baseline is now
**`mls-shadow-v1.6`**.

Docs live in `docs/V9.5/`; V9.4 is superseded. The whole-system
evaluation is [`SLATE-EVALUATION.md`](SLATE-EVALUATION.md).

---

## ⚡ CURRENT STATE — V9.5 SNAPSHOT

### One paragraph
One backend, two isolated planes. The **archive plane** is the completed
WC26 record: fail-closed read-only, self-healing at boot, 16/16 results
and 84/84 ledger positions restored on every redeploy. The **live plane**
is the MLS shadow platform on durable Railway PostgreSQL, which has now
produced and survived a full prospective slate. The model is unchanged
from V9.4 — provider expected goals, explicit calibration, every constant
re-swept under the model that ships. What changed in V9.5 is the layer
that *reports* on all of it: the paper ledger now publishes its
denominator, version strings can no longer silently destroy the rows
that carry them, a lock's validity is separated from the state of the
current deployment, and the research corpus is **published and immutable**
with the approval decision bound to it by hash. Real-money signals are
disabled and no code path can enable them.

### The frozen baseline
```text
release:            mls-shadow-v1.6
backend:            371d569  main   (15 commits past the V9.4 baseline 37ac74b)
frontend:           6833701  namson-dev main
migration head:     a1b2c3d4e5f6
input artifact:     model-input-v5    (unchanged)
lock policy:        mls-lock-v2       (unchanged)
paper execution:    paper-exec-v4     (unchanged)
corpus:             corpus-v2         PUBLISHED: mls-shadow-2026-07-25-slate-v1
evaluation:         model-eval-v1 / shadow-approval-v1
tests:              513 backend + 7 PostgreSQL integration + 13 e2e
money:              REAL_MONEY_SIGNALS_ENABLED=false — LOCKED
```

### The deployed model (unchanged since V9.3)
```text
ratings basis       provider xG        MLS_XG_RATING_ALPHA    = 1.0
xG prior            k = 6              MLS_XG_SHRINK_GAMES    = 6.0
goals prior         k = 24 (fallback only, when a fixture lacks xG)
recency             90-day half-life
3-way calibration   alpha 0.25 toward uniform
goal dispersion     MLS 0.0            (WC26 keeps 0.30 — archive intact)
ladder              M0 · M1 · M2 · M2C · M3     deployed = M3
```

**No model change shipped in V9.5.** The prospective slate is evidence
*about* the model; changing the model in the same edition would have
destroyed the only clean forward test the project has.

---

## THE FIRST SLATE — HEADLINE NUMBERS

```text
canonical T-10 locks      15 / 15      missed 0 · failed snapshots 0
lock audit                15 / 15 all_pass · clean: true   (29 checks each)
paper coverage            45 / 45 eligible legs  (100%, 18 recovered)
paper ledger              45 signals · 7 fills · 38 rejected
settled paper P&L         −$69.32 on $169.32 cost   ROI −40.94%
forecast vs baseline      +0.0027 WORSE   CI [−0.0755, +0.0770]
```

Every one of those numbers is too small to conclude anything from, and
[`SLATE-EVALUATION.md`](SLATE-EVALUATION.md) says so precisely rather
than gesturing at it. The verdicts are unchanged: **machinery GO,
profitability NO-GO, real money NO-GO.**

---

## WHAT V9.5 FIXED

Three defects, found in sequence — each one uncovered by the fix for the
one before it.

### 1. The paper ledger published a numerator with no denominator
15 locks × 3 quote-linked legs = **45 eligible** legs. The ledger held
**27**. Eighteen legs were never evaluated and nothing anywhere reported
the shortfall, so "27 signals, all rejected" was indistinguishable from a
complete examination that happened to find nothing.

`paper.paper_coverage()` now rides on `paper_summary`, `/api/mls/metrics`
and the lock-audit **summary**, using the same eligibility test as the
signal loop so coverage can never be satisfied by a leg the engine would
skip. `backfill_uncovered_locks()` recovers missing legs — faithful by
construction because every input is frozen on the lock, including the
quote age the staleness gate reads (`oldest_quote_age_seconds`, recorded
at capture, never computed against `now`). Recovered rows carry
`backfilled_at`; deterministic recovery is not the same evidence as a
signal that existed at lock time.

### 2. A 26-character constant in a 24-character column erased every fill
`FEE_POLICY["version"]` is `"kalshi-fee-2026-07-general"` — 26 chars.
`paper_fill.fee_policy_version` was `String(24)`. **SQLite ignores
VARCHAR length; PostgreSQL raises `StringDataRightTruncation`.** The
whole suite runs on SQLite, so 500 tests stayed green while every
`paper_fill` INSERT died in production.

The loss was **biased, not partial**, which is the serious part. One
transaction covers a whole lock: a lock whose signals were all rejections
wrote no fill and committed normally; a lock that produced a fill hit the
truncation and the rollback took its signals with it. The ledger retained
**100% of rejections and 0% of fills** and reported *"27 signals, 0
fills, all rejected NET_EDGE_TOO_LOW"* — which reads as *the model agreed
with the market everywhere*, when in fact every fixture where it
disagreed had been deleted. Six of fifteen.

Ten version/policy columns are widened (migration `a1b2c3d4e5f6`),
including the two holding **provider-controlled** strings that were never
ours to bound. The structural fix is in the suite: a `before_flush`
listener now enforces VARCHAR lengths on SQLite, giving PostgreSQL-grade
checking to every test that writes a row. Enabling it turned **10 green
tests red — precisely the fill-creating ones.**

### 3. An unrelated deploy voided the evidence chain
Deploying fix #1 took the lock audit from 15/15 to **0/15** and made
`/api/mls/replay` refuse every historical lock. `engine_signature()`
hashes `code_revision`, so a migration changed it exactly as a model
rewrite would.

Two-part fix. `engine_matches(stored, run_revision)` recomputes the
signature under the revision a run **recorded**; reproducing the hash
proves only the revision moved. And — the real fix — engine-match moved
**out** of a lock's `all_pass` into a summary `engine_provenance` block,
because *a canonical lock records what happened at T-10 and cannot be
retroactively invalidated by a later deploy.* `engine_signature_present`
remains a hard check; replay still refuses on a genuine mismatch, which
is the correct home for the strict guard.

### Also: the third ESPN break at HTTP 200
The child **named** `"Eastern Conference"` began carrying all 30 clubs
(its inner block is literally `standings.name: "overall"`), so every
Western club rendered twice and a Western club topped the Eastern table —
and the two blocks disagreed, one being a matchday fresher. `parse_standings`
now takes **membership** from any child listing a strict subset and
**statistics** from the freshest row per club. After the
`headToHeadGames`→`seasonseries` rename and the winner-first score
string, the rule is now explicit: *a provider's grouping is data, not a
guarantee.*

---

## THE PUBLISHED CORPUS

```text
version            mls-shadow-2026-07-25-slate-v1
schema             corpus-v2
size               13,437,156 bytes
manifest hash      7e0836818f6f25f9a08126140eca822c063574f1e50cf0ffe003d3827415a27c
sections           24, all independently re-hashed and verified
```

Served from stored bytes, never rebuilt. Downloaded and verified: the
manifest hash recomputes and all 24 sections match.

> **Verification gotcha.** Section hashes use
> `json.dumps(section, sort_keys=True, ensure_ascii=False)`. Verifying
> with the default `ensure_ascii=True` reports **six false mismatches** —
> exactly the sections carrying non-ASCII (player and team names, em
> dashes). The bytes were fine; the checker was wrong.

The approval decision is now **bound**: the active decision carries
`corpus_version` and `corpus_manifest_hash`, both inside its content
hash. Every prior decision recorded `corpus_version: null` — the plumbing
existed and was simply never connected, because boot called
`ensure_approval_decision()` with no argument.

---

## ENDPOINT SURFACE (additions in V9.5)

```text
POST /api/admin/mls/paper-backfill          recover uncovered locks
POST /api/admin/mls/approval/bind-corpus    bind approval to a published corpus
GET  /api/mls/metrics       → paper.coverage block
GET  /api/mls/paper         → coverage block
GET  /api/mls/audit         → summary.paper_coverage + summary.engine_provenance
GET  /api/mls/approval      → corpus_manifest_hash now populated from the
                              decision DOCUMENT (was hardcoded null)
```

---

## READINESS

```text
ready                       true
archive_ready               true    16/16 results · 84/84 ledger
shadow_collection_ready     true    blockers: []
paper_engine_operational    true    kill switches: []
migrations_current          true    head a1b2c3d4e5f6
real_money_signals          FALSE   — LOCKED
```

Verdicts: **machinery GO · profitability NO-GO · real money NO-GO.**
