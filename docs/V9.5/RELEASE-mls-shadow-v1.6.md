# Release — `mls-shadow-v1.6`

**The V9.5 post-slate baseline.** Supersedes `mls-shadow-v1.5` (V9.4),
which produced the first prospective slate correctly but could not report
what it had produced.

---

## Identity

```text
release            mls-shadow-v1.6
backend            371d569   main   (code; docs on docs-v9.5)
frontend           6833701   namson-dev main
migration head     a1b2c3d4e5f6
date               2026-07-26
```

## Contract versions

```text
input artifact     model-input-v5    unchanged
lock policy        mls-lock-v2       unchanged
paper execution    paper-exec-v4     unchanged
fee policy         kalshi-fee-2026-07-general   (general taker only)
corpus             corpus-v2         FIRST PUBLISHED VERSION
evaluation         model-eval-v1 / shadow-approval-v1
audit              mls-lock-audit-v1   (+ summary paper_coverage,
                                         summary engine_provenance)
risk               risk-v1
```

## Model parameters (unchanged from v1.4 / v1.5)

```text
MLS_XG_RATING_ALPHA      1.0
MLS_XG_SHRINK_GAMES      6.0     goals keep k=24 as the no-xG fallback
MLS_CALIBRATION_ALPHA    0.25
MLS_GOAL_DISPERSION_CV   0.0     WC26 keeps 0.30
HALF_LIFE_DAYS           90
MIN_GAMES                5
REGISTRY_MAX_AGE_HOURS   6
```

**No model change.** The prospective slate is evidence about this model;
changing it in the same edition would destroy the only clean forward test
the project has.

## Approved-for-shadow decision (production)

```text
decision_id             199        (snapshot — drifts every deploy)
content_hash            c97432ca77c0086599526d8cc9db3946c919c0e791008cedab2bcea783a8439c
corpus_version          mls-shadow-2026-07-25-slate-v1     ← first ever bound
corpus_manifest_hash    7e0836818f6f25f9a08126140eca822c063574f1e50cf0ffe003d3827415a27c
n_scored                177        (was 162 — the slate folded in)
edge_vs_baseline        +0.0269    (was +0.0331)
ci95                    [−0.0052, +0.0609]
edge_significant        FALSE
approved_mode           shadow
```

## Published corpus

```text
version         mls-shadow-2026-07-25-slate-v1
schema          corpus-v2
size            13,437,156 bytes
manifest hash   7e0836818f6f25f9a08126140eca822c063574f1e50cf0ffe003d3827415a27c
sections        24, all independently re-hashed OK
served from     stored bytes (never rebuilt)
local copies    research_archive/corpus/*.json.gz  (928 KB)
                research_archive/corpus/*.verification.json
```

> Verify sections with `json.dumps(section, sort_keys=True,
> ensure_ascii=False)`. The default `ensure_ascii=True` produces six
> false mismatches on the non-ASCII sections.

---

## What changed since `mls-shadow-v1.5`

15 commits. Three defects and one provider break.

| area | change | commit |
|---|---|---|
| Paper ledger | `paper_coverage()` — the missing denominator; coverage on summary / metrics / audit; `backfill_uncovered_locks()`; `backfilled_at` provenance | `0df161d` |
| Schema | 10 version/policy columns widened; SQLite VARCHAR-length guard in the suite; constant-vs-column invariant tests | `89eca11` |
| Evidence chain | `engine_matches()` revision-only drift; engine-match moved out of lock `all_pass` into `engine_provenance` | `5a23c19`, `df36aa5` |
| Approval | `latest_published_version()`; corpus binding by default; `bind-corpus` endpoint; `corpus_manifest_hash` read from the decision document | `189679c`, `7ed0ab7` |
| Standings | ESPN conference-grouping drift — membership from strict subsets, statistics from the freshest row | `c2e3f00` |
| Frontend | kickoff date/time on fixture cards; unpinned the hard-coded decision-safety fixture | `6833701` |

### Migrations
```text
f1a2b3c4d5e6   paper_signal.backfilled_at
a1b2c3d4e5f6   widen 10 version/policy columns   ← head
```
Both round-trip empty → head → down → head on real PostgreSQL 18.

---

## Test evidence

```text
backend (SQLite)            513 passed,  7 skipped
backend (+ PostgreSQL)      520 passed
frontend e2e (Playwright)    12 passed,  1 skipped
tsc --noEmit                clean
next build                  clean
```

Two guards were verified to **fail without their fix**, because a guard
that cannot fail is decoration:

- the SQLite VARCHAR listener turned **10 green tests red** — precisely
  the fill-creating ones;
- the frontend kickoff test failed against the previous build (it still
  found "Scheduled").

The skipped e2e is correct: no upcoming fixture currently has both an
open book and a model run (the next fixture is the All-Star game, which
is excluded as a non-club match).

---

## Production verification (2026-07-26)

```text
ready                       true
archive_ready               true    16/16 results · 84/84 ledger
migrations_current          true
shadow_blockers             []
paper_kill_switches         []
real_money_signals          FALSE

lock audit                  15/15 all_pass · clean: true
paper coverage              45/45 legs · 100% · 18 backfilled
paper ledger                45 signals · 7 fills · 38 rejected
settled paper P&L           −$69.3237 on $169.3237   ROI −40.94%
engine_provenance           locks_engine_changed: 15  (disclosed, see below)
standings                   Eastern 15 · Western 15 · 30 rows · 30 unique
```

---

## Disclosed, not fixed

- **`locks_engine_changed: 15`.** The slate's locks cannot be replayed
  under *today's* engine, because `model_mls.py` genuinely changed in
  this release. Their evidence is intact and frozen in the published
  corpus; replay under the recorded revision (`37ac74b`) remains the
  claim, as it always was.
- **Corpus preview exceeds the public body cap** (13.4 MB vs 8 MB), so
  `?preview=1&full=1` returns 413. Publishing is the download path.
- Carried forward from V9.4: F8 in-sample selection, F15 single order
  type, F16 capture-clock freshness, F21 process-local scheduler.

---

## Verdicts

**Machinery GO** — 15/15 locks, clean audit, bit-exact replay proven on
real frozen evidence.

**Profitability NO-GO** — unchanged. n=15 fixtures and n=7 fills move
nothing; the edge estimate went down and its CI still spans zero.

**Real money NO-GO** — unchanged. `REAL_MONEY_SIGNALS_ENABLED=false`, no
code path can enable it.
