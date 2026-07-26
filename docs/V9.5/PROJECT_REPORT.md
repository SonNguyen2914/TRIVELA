# Project Report — V9.5

**The edition where the system was finally tested forward.**

---

## Movement one — the slate happens

Fifteen MLS fixtures, July 25. The scheduler locked each one at T-10
against a frozen Kalshi book, wrote a canonical run with its immutable
approval decision, captured the lineup state it saw, and paper-traded
the result. Then it went quiet and the matches were played.

Nothing needed intervention. 15/15 locks, 0 missed, 0 failed snapshots,
395 complete runs with zero failures, and — days later — a locked run
replayed **bit-exactly** from its stored artifact alone. The DiskFull
failure that nearly cost the project its evidence in July did not recur.

Four independent evaluations built those invariants. This is the first
time they were all exercised at once, unattended, on data nobody could
adjust afterwards. They held.

---

## Movement two — the forecast says nothing, loudly

Scored against the 15 locked predictions, the model came in at 0.9263
log loss against a 0.9236 baseline: **0.0027 worse**, with a bootstrap
CI of [−0.0755, +0.0770].

The temptation with a number like that is to explain it. The honest
reading is that it cannot be explained, because the interval is five
times wider than the effect anyone is looking for. A 15-match slate was
never going to resolve a 0.03 edge, and saying so is the whole
contribution of this section.

There is one thing worth noting about the slate itself: **11 of 15 home
wins**, 73% against a 45.7% league rate, P(≥11) = 0.029. The model
averaged 43% home. It lost, mostly, by not predicting a 1-in-35 skew.

Folding the fixtures into the scored sample moved the standing estimate
from **+0.0331 (n=162)** to **+0.0269 (n=177)**, CI [−0.0052, +0.0609].
Down, and still not significant. That is what a prospective sample is
supposed to do to an in-sample estimate.

---

## Movement three — the ledger was lying

The paper engine reported *27 signals, 0 fills, all rejected
NET_EDGE_TOO_LOW*. Read plainly: the model agreed with the market
everywhere, and the slate produced no execution evidence at all. That is
what the first draft of the evaluation said.

It was wrong, and finding out required building something the ledger had
never had: a **denominator**. Fifteen locks each carried three
quote-linked legs — 45 eligible. The ledger held 27. Eighteen legs had
never been evaluated, and no metric anywhere reported it.

The backfill written to recover them reproduced the original production
failure word for word:

```
(psycopg.errors.StringDataRightTruncation)
value too long for type character varying(24)
```

`FEE_POLICY["version"]` had grown to 26 characters against a `String(24)`
column. SQLite — which the entire test suite runs on — ignores VARCHAR
length. PostgreSQL does not. Five hundred green tests, and every single
`paper_fill` INSERT dying in production.

Then the part that matters. One transaction covers one lock. A lock whose
signals were all *rejections* inserted no fill and committed cleanly. A
lock that produced a *fill* hit the truncation, and the rollback took its
signals with it. The ledger kept **100% of the rejections and 0% of the
fills** — and the six vanished locks were not a random 40%, they were
exactly the fixtures where the model disagreed with the market enough to
trade. The module's own docstring promises the ledger "has no
survivorship bias."

Recovered, the slate reads: 45 signals, 7 fills, 38 rejections, and a
settled paper P&L of **−$69.32 on $169.32 — ROI −40.94%**, one of seven
fills hitting at an average entry of 24¢. Expected hits at the market's
own price: 1.69. Observed: 1. One more winner and the ROI is +18.1%.
The sign is decided by a single match, so it is not evidence of edge —
but it is a real number where there had been a fabricated zero.

---

## Movement four — the fix that broke the evidence

Deploying the coverage fix took the lock audit from 15/15 to **0/15**,
and `/api/mls/replay` began refusing every historical lock.

Nothing about the model had changed; the four modules that compute the
numbers were byte-identical. But `engine_signature()` hashes
`code_revision`, so a migration and some observability changed the
fingerprint exactly as a model rewrite would. Bit-exact replay of frozen
locks — the strongest property the system has — had just been voided by
a commit that could not touch a single probability.

The first fix was to make revision-only drift *provable*: every run
records the revision it ran under, so recomputing the signature under
that revision either reproduces the stored hash (only the revision moved)
or does not (something real changed). It worked, and it was not enough —
because the fix itself edits `model_mls.py`, whose source digest the
signature covers. The guard was telling the truth.

Which exposed the actual error, one level up. `all_pass` required *today's
code* to still reproduce a lock. But a canonical lock is a record of what
happened at T-10; its validity is fixed by facts frozen at that moment. A
lock cannot become retroactively invalid because someone shipped a fix.
Engine-match moved out of the lock's checks into a summary
`engine_provenance` block — the same separation already applied to paper
coverage — while `engine_signature_present` stayed a hard check and
replay kept refusing on genuine mismatch, which is where a strict guard
belongs.

The honest residue, recorded in the audit rather than hidden:
`locks_engine_changed: 15`. The slate's locks cannot be replayed under
today's engine, because `model_mls.py` genuinely changed. Their evidence
is intact and frozen in the published corpus; replay under the recorded
revision remains the claim it always was.

---

## Movement five — freezing the record

The corpus was published: **`mls-shadow-2026-07-25-slate-v1`**,
13,437,156 bytes, served from stored bytes and never rebuilt. Downloaded
and verified independently — the manifest hash recomputes, all 24
sections re-hash to their recorded values.

The first verification attempt reported six mismatches. The corpus hashes
sections with `ensure_ascii=False`; the checker used Python's default.
The six failures were exactly the sections holding non-ASCII — player
names, team names, em dashes. The bytes were fine; the checker was wrong.
Worth recording, because the next person will hit it.

The approval decision is now bound to that corpus by hash. Every decision
before it recorded `corpus_version: null` — not because the binding was
unbuilt, but because boot called `ensure_approval_decision()` with no
argument and nothing else ever called it. The plumbing had been there
since V9.3.

---

## What this edition is really about

Three defects, one shape: **a correct implementation of an assumption
never checked against reality.**

- *A signal count is readable on its own.* It is not, without its
  denominator.
- *The test database behaves like the production database.* It does not,
  and the gap is invisible precisely because nothing fails.
- *A valid lock is one today's code can reproduce.* It is not; that
  conflates a historical record with the state of a deployment.

Every line of code involved did exactly what it said. What went untested
was the sentence underneath it. That is the same lesson as the Kalshi
`updated_time` gate that could never pass, the winner-first ESPN score
string, and the DiskFull `{"created": 0}` — and it is now three editions
in a row.

The countermeasure that actually worked here was not review. It was
building the thing that makes the assumption checkable: a denominator, a
VARCHAR guard that turns 10 green tests red, a provenance block that
separates two claims. Each of those failed *first*, against the real
defect, before being trusted.

---

## Where it stands

**Machinery GO. Profitability NO-GO. Real money NO-GO.**

The model is untouched and must stay untouched: these 15 fixtures are now
part of the scored sample, and the moment a constant is chosen with them
in view they stop being prospective evidence. What the verdict needs is
not a better model but a larger forward sample — hundreds of scored
fixtures and enough *fills* to tell an edge from seven longshots.

That is a season of Saturdays, collected without interference. The system
has now demonstrated it can do exactly that unattended.
