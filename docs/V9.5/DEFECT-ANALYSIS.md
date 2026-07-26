# V9.5 — Defect Analysis

Three defects found in sequence, each uncovered by the fix for the one
before it. This document is the finding-by-finding record, in the same
spirit as V9.4's `EVAL-RESPONSE.md` — except these were not reported by
an external evaluator. They were found by building the instrument that
could see them.

---

## D1 — The paper ledger published a numerator with no denominator

**Severity:** P0 (evidence integrity)
**Found by:** reconciling lock count against ledger count

### Claim
15 canonical locks × 3 quote-linked game legs = **45 eligible** legs. The
ledger held **27**. Eighteen eligible legs (40%) produced no signal and
no metric anywhere reported the shortfall.

### Proof
The lock audit reports `three_way_market_linked: true` on all 15 locks —
which requires both `market_contract_id` and `market_quote_id` on all
three legs. That is *exactly* the eligibility condition in the paper
signal loop (`paper.py`), so all 45 legs were eligible by the engine's
own test.

### Mechanism
`runs.py` calls `paper_trade_lock` inside a `try/except` so a paper
failure cannot undo a lock. **That isolation is correct and stays.** The
defect was the silence: `paper_summary()` emitted `signals` with nothing
stating what the number should have been, so a 40% shortfall was
indistinguishable from a complete examination that found nothing.

### Fix
- `paper.paper_coverage()` — per-lock eligible legs vs signalled, using
  the **same** eligibility test as the signal loop, so coverage can never
  be satisfied by a leg the engine would skip.
- Coverage block on `paper_summary`, `/api/mls/metrics`, and the
  lock-audit **summary** — deliberately *not* inside a lock's `checks`,
  because a lock whose paper engine never ran is still a valid lock.
- `backfill_uncovered_locks()` + `POST /api/admin/mls/paper-backfill`.
- Migration `f1a2b3c4d5e6`: `paper_signal.backfilled_at`.

### Why the backfill is legitimate
Every input the decision reads is frozen on the lock — the model
probability on the prediction contract, the ask on the linked quote, and
critically the quote age the staleness gate tests, which is
`snapshot.oldest_quote_age_seconds` **recorded at capture**, not computed
against `now`. A recomputation therefore yields the lock-time decision.
Pinned by a test that wipes the ledger and asserts the recovered signal
matches the inline one field-for-field: decision, reason, `net_edge`,
exact ask, exact fee.

### Family
Identical in shape to the DiskFull `{"created": 0}`: a count that reads
as success purely because nothing states what it should have been.

---

## D2 — A 26-character constant in a 24-character column erased every fill

**Severity:** P0 (biased evidence destruction)
**Found by:** running D1's backfill, which reproduced the original error

### Claim
`FEE_POLICY["version"]` is `"kalshi-fee-2026-07-general"` — 26 characters.
`paper_fill.fee_policy_version` was `String(24)`.

```
(psycopg.errors.StringDataRightTruncation)
value too long for type character varying(24)
[SQL: INSERT INTO paper_fill ...]
```

### Why 500 green tests missed it
**SQLite ignores `VARCHAR(n)` entirely. PostgreSQL enforces it.** The
whole suite runs on SQLite; production is Railway PostgreSQL. The gap is
invisible precisely because nothing fails.

### Why the loss was BIASED, not partial
This is the part that matters. `paper_trade_lock` wraps one lock in one
transaction:

| lock produced | paper_fill INSERT | outcome |
|---|---|---|
| rejections only | none attempted | commits normally — signals survive |
| at least one fill | attempted → truncation | rollback — **signals lost too** |

So the ledger retained **100% of rejections and 0% of fills**, and the
six lost locks were not a random 40% — they were precisely the fixtures
where the model disagreed with the market enough to trade.

The reported headline — *"27 signals, 0 fills, all rejected
NET_EDGE_TOO_LOW"* — therefore read as *the model agreed with the market
everywhere*, the exact inverse of what happened. `paper.py`'s docstring
claims the ledger "has no survivorship bias"; it had the maximum
possible amount.

### Blast radius
Confirmed by enabling the new guard: **10 previously-green tests failed,
precisely the fill-creating ones.**

### Fix
- Migration `a1b2c3d4e5f6` widens 10 version/policy columns, including
  the two holding **provider-controlled** strings that were never ours to
  bound: `market_quote.fee_schedule_version` (Kalshi `fee_type`) and
  `lineup_snapshot.parser_version`. Batch mode so the chain still
  replays on SQLite.
- **Structural:** the test suite installs a `before_flush` listener that
  enforces VARCHAR lengths, giving PostgreSQL-grade checking to every
  test that writes a row.
- Invariant tests mapping every versioned constant to the column it is
  persisted in, plus a real PostgreSQL write using the actual constants
  (`test_postgres_integration` #7).

### Recovered truth
45 signals · 7 fills · 38 rejections · settled **−$69.32 on $169.32**
(ROI −40.94%), 1 of 7 hitting at ~23¢ average entry.

---

## D3 — An unrelated deploy voided the evidence chain

**Severity:** P0 (evidence availability)
**Found by:** verifying the D1 deploy

### Claim
Deploying D1 took the lock audit from **15/15 to 0/15** and made
`/api/mls/replay` refuse every historical lock:
`"engine signature mismatch — refusing to replay under a different
engine"`.

Nothing about the model had changed. `git diff` over `model_mls.py`,
`simulator.py`, `xg_model.py`, `features.py` was **empty**.
`engine_signature()` hashes `code_revision`, so a migration plus some
observability changed the fingerprint exactly as a model rewrite would.

### Why it matters
Bit-exact replay of a frozen lock is the strongest property the system
has. Losing it to an unrelated commit is a serious regression — and an
audit that reads `clean: false` forever is an audit nobody reads.

### Fix, part 1 — make revision-only drift provable
`engine_matches(stored_hash, run_revision)` recomputes the signature
under the revision the run **recorded** (`PredictionRun.git_revision`).
Reproducing the stored hash proves the constants, module source digests,
python and numpy are all identical and only the revision moved. A real
change still fails, because substituting a revision cannot recover the
hash.

### Fix, part 2 — the actual error, one level up
Part 1 worked and was **not sufficient**, because the fix itself edits
`model_mls.py`, whose source digest the signature covers. The guard was
telling the truth.

Which exposed the real mistake: `all_pass` required *today's code* to
still reproduce a lock. But a canonical lock records what happened at
T-10; its validity is fixed by facts frozen at that moment — written
before kickoff, inside the window, from a complete snapshot, under an
approved decision, with its engine signature recorded and artifact
retained. **A lock cannot become retroactively invalid because someone
shipped a fix.**

- engine-match moved **out** of a lock's `checks` into a summary
  `engine_provenance` block (matching / revision-only drift / engine
  changed, plus the current hash);
- `engine_signature_present` stays a hard check — a lock must document
  the engine that produced it;
- the recorded revision is *reported*, not gated — an environment
  without git metadata must not turn every lock red;
- **replay still refuses** on a genuine mismatch. That is the correct
  home for the strict guard: replay claims to reproduce numbers *now*,
  and should decline when it cannot.

Same lock-validity-vs-deployment-state separation already applied to
paper coverage in D1.

### Disclosed residue
`locks_engine_changed: 15`. The slate's locks cannot be replayed under
today's engine because `model_mls.py` genuinely changed. Their evidence
is intact and frozen in the published corpus; replay under recorded
revision `37ac74b` remains the claim, as it always was.

---

## D4 — ESPN standings: the third HTTP-200 break

**Severity:** P1 (display correctness)
**Found by:** user report from the live hub

### Claim
The child **named** `"Eastern Conference"` carried all 30 clubs — its
inner block is literally `standings.name: "overall"` — while
`"Western Conference"` carried the correct 15. Every Western club
rendered twice, every place number doubled (1,1,2,2,…), and Vancouver, a
Western club, topped the Eastern table.

Additionally the two blocks **disagreed**: every Western club's row in
the 30-row block was one matchday fresher, so the same club showed
different points depending on which table you read.

### Key insight
The 30-row block carries each club's **conference** rank (1..15 twice),
not a league rank. So it partitions cleanly.

### Fix
- **Membership** from any child listing a *strict subset* of clubs. A
  child listing every club is the league table wearing a conference's
  name and is never treated as a roster; its members are whatever no
  genuine roster claimed.
- **Statistics** from the **freshest** row per club across all blocks
  (most games played — standings only move forward), which resolves the
  disagreement deterministically.
- Ranks kept from ESPN (they encode the league's own tiebreakers),
  recomputed only when they collide.
- No strict subset anywhere → one honest combined table rather than two
  invented ones.

### Standing rule
After `headToHeadGames` → `seasonseries` and the winner-first `score`
string: **a provider's grouping is data, not a guarantee** — same as its
field names and its composite strings.

---

## The common shape

| # | Untested assumption |
|---|---|
| D1 | "a signal count is readable on its own" |
| D2 | "the test database behaves like the production database" |
| D3 | "a valid lock is one today's code can reproduce" |
| D4 | "a provider's grouping tells you which bucket a row is in" |

Every line of code involved did exactly what it said. What went untested
was the sentence underneath it — **a correct implementation of an
assumption never checked against reality**, three editions running.

The countermeasure that worked was not review. It was building the
instrument that makes the assumption checkable, and then confirming the
instrument **fails against the real defect** before trusting it:

- the coverage denominator exposed D1 and led directly to D2;
- the VARCHAR listener turned 10 green tests red;
- the frontend kickoff test failed against the previous build.

A guard that cannot fail is decoration.
