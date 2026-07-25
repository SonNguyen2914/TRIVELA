# V9.3 — Project Report

**Period:** July 24–25, 2026. **38 commits** past the V9.2 baseline.

---

## What this cycle was

V9.1 and V9.2 were responses to *external evaluations*. V9.3 was not. It
began with the operator using his own product, noticing something that
could not be true, and asking about it:

> *"VAN is #1 and MIN is #9 right now, and my model argues that MIN have a
> better chance to win?"*

That single sanity check — a thing no test encoded, because no test knew
the league table — unspooled into four audit rounds and the largest set of
model corrections since the platform launched. It is the clearest evidence
so far that **a green test suite is not the same as a correct system**.

---

## The arc

**1. Give the model real data.** The official MLS (Sportec) stats API was
brought in as a first-class ingestion source: per-match team statistics
carrying the provider's own **expected goals**, plus per-match player
statistics. All 30 clubs mapped 1:1 by three-letter code; 238/238 matches
ingested; 9,481 player rows. An ESPN↔Sportec **player bridge** was built at
99.5 % accuracy on starters — after discovering that the obvious source
(the team-roster endpoint) is transfer-stale and tops out near 83 %.

**2. Rebuild the model on xG.** Ratings moved from goals to provider xG,
measured to help before shipping. This was the correct direction — and it
carried a defect: the xG ratings inherited the *goals* shrinkage prior.

**3. Four audits.** Detailed in [`AUDIT-FINDINGS.md`](AUDIT-FINDINGS.md).
Four defects, all in fully-green code, all the same shape: *a correct
implementation of an assumption nobody checked against reality.*

| # | defect | consequence if unfixed |
|---|---|---|
| 1 | xG ratings used the goals prior (k=24) | the league's best team rated near average |
| 2 | "win% blend" was damping under a false name | an unearned feature; benefit vanished under honest fitting |
| 3 | WC26's goal dispersion applied to MLS | BTTS 10 pp under actual, across 50 of 53 markets |
| 4 | lock freshness required a timestamp the venue does not publish | **the first slate collects zero execution evidence** |

**4. Two display defects and one production incident**, all found the same
way — the operator looking at the output. H2H silently vanished (ESPN
renamed a field at HTTP 200); the form block rendered **every defeat as a
win** (a provider string is winner-first); and the Postgres volume filled,
failing every prediction write behind a `{"created": 0}` that looked like
"nothing to do".

---

## Where the platform stands

```text
model        provider xG ratings + calibrated 3-way
edge         M3 vs baseline +0.0332  CI [−0.0015, +0.0674]  n=162
             (~4x the goals-only model; still NOT significant)
data         team 238/238 · player 238/238 · bridge 99.4% minutes-weighted
tests        477 backend + 6 e2e + 3 page-health
prod         ready · no blockers · paper_execution_ready true
storage      ~397 MB of a 5 GB volume (7.8%)
money        LOCKED — and there is no order-placement code path at all
```

---

## What was rejected

Four candidate improvements were **measured and not shipped**: an
XI-strength adjustment, a goalkeeper term, key-attacker availability (real
but not significant), and a λ-scaling correction for the residual goal-rate
bias. Two suspected defects were investigated and **cleared** as correct
behaviour (props excluded from calibration; the 3-way/props basis split).

This matters more than the features that did ship. The measurement is only
worth anything if it is allowed to say no.

---

## Lessons carried forward

**1. Provenance on tuned constants.** Three of four defects were a constant
measured in one context and inherited into another. `CALIBRATION.md` now
records, for every constant, *what it was swept under*. A constant without
that field is a defect waiting to happen.

**2. Verify what a third-party field means.** Two defects (the lock gate,
the scoreline inversion) were assumptions about provider semantics that
were never checked against provider data. Both were resolved in minutes by
simply fetching the payload and looking.

**3. Never render a provider's composite string next to a value derived
from different fields.** Compute both from the same primitives, so the
display is *structurally incapable* of contradicting itself.

**4. A failure that only prints to a log is invisible.** The DiskFull
incident hid for hours behind a swallowed exception. Every sweep now
reports *why* it produced nothing.

**5. A gate that can never pass is not fail-closed — it is broken-closed.**

---

## Open items

- **Goal-rate bias**: −0.174 goals/match, diagnosed, correction rejected.
- **Significance**: the edge CI still spans zero at n=162.
- **In-sample tuning**: every sweep selects on the same 162 matches.
- **Untestable until kickoff**: settlement against a real completed
  fixture; the in-play path during a live match.
- **Growth**: `market_depth_level` grows unbounded (152 MB, no retention).
  Not urgent at 5 GB, but it is the remaining structural growth risk.
- **Key-attacker availability**: the one rejected feature worth revisiting
  once more data tightens its interval.
