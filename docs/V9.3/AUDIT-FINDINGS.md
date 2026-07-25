# V9.3 — Audit Findings (four rounds, July 24–25 2026)

The V9.3 cycle was not driven by an external evaluation. It began with a
single question from the operator looking at the site:

> *"There is a big disagreement between my model and the community. VAN is
> #1 and MIN is #9 right now, and my model argues MIN has a better chance
> to win?"*

He was right and the model was wrong. Chasing that one observation
uncovered a class of defect that then repeated three more times, and four
audit rounds followed. **Every defect below was found in code where the
full test suite was green.**

---

## The recurring shape

> **A correct implementation of an assumption that was never checked
> against reality.**

Each constant had been honestly measured — in a *different* context — and
then inherited. Each gate had been honestly implemented — against an
assumption about a provider's data that nobody had verified. Tests passed
throughout, because the tests encoded the same assumption.

---

## Finding 1 — xG ratings inherited the goals prior (round 0, the trigger)

`SHRINK_GAMES = 24` was swept for **goals**, which are noisy enough to need
a heavy pull toward the mean (k=6 *lost* there). Provider xG is far
lower-variance, so the same prior discarded most of the signal: with ~7.4
recency-weighted games a team retained only **24 %** of its own rating.

Vancouver — the best xG differential in MLS at **+1.52/game**, unshrunk
attack 1.544 / defence 0.685 — was flattened to 1.128 / 0.926, and
Minnesota's home multiplier (×1.297) then outranked them.

**Fix.** `XG_SHRINK_GAMES = 6.0`, separate from the goals prior. Re-swept:
monotonic gain from k=24 down to a clean **interior** optimum at k=4–6
(turning back up below k=3, so not an edge artefact).
`M3 vs baseline +0.0235 → +0.0307`. The reported fixture became
MIN 33.9 % / VAN 42.0 % — Vancouver correctly favoured.

## Finding 2 — the "win% blend" was damping wearing a false name

Probing `RESULT_SHRINK` showed the deployed win/draw/loss blend carried
**no team information**:

| variant | log-loss |
|---|---|
| pure xG, no blend | 1.0506 |
| win% blend (deployed) | 1.0469 |
| **flat 1/3 prior, same weight** | **1.0445** |

A flat anchor beat the real prior, so the whole benefit was damping an
overconfident model. Worse, with α fitted **walk-forward** (as production
must) the win% blend scored **−0.0007 vs no blend at all** — the reported
+0.0038 was hindsight from choosing α on the same 162 matches.

**Fix.** Replaced by an explicit calibration term, α 0.25 toward uniform,
named for what it does. `M3 1.0469 → 1.0443`.

**The same honest test was then applied to Finding 1's fix**, which
survived for a real reason: *every* k in 3–8 beats k=24 by ~0.007 — the
whole region, not a lucky peak — with an independent structural
explanation (the league's best team flattened to average).

## Finding 3 — WC26's goal dispersion suppressed every MLS prop

`GOAL_DISPERSION_CV = 0.30` is sourced for WC26. Widening the per-match
goal spread inflates **P(0 goals) for each side**, which suppresses both
BTTS and the overs — while leaving the mean total untouched, so the
expected-goals figure on the page looked perfectly correct.

| market | predicted | actual | bias |
|---|---|---|---|
| **BTTS** | 57.3 % | 66.0 % | **−10.0 pp** |
| over 1.5 | — | — | −4.5 pp |
| over 2.5 | — | — | −4.2 pp |
| over 3.5 | — | — | −3.7 pp |

Mechanism at λ 1.72/1.55: cv 0.30 raises P(home=0) 18.0 → 20.2 % and
P(away=0) 21.2 → 23.3 %, dropping BTTS 64.6 → 61.2 %.

**Fix.** `MLS_GOAL_DISPERSION_CV = 0.0`, injected per-simulator; WC26 keeps
0.30 so the frozen archive replays bit-for-bit. Prop log-loss improves
monotonically as dispersion falls: **+0.0277** total, roughly double the
entire xG gain. The engine signature records the MLS value, so replay
rebuilds the same simulator.

## Finding 4 — the lock freshness gate could never pass (the critical one)

Execution-readiness required `freshness_basis == 'provider'` and
`age <= 600 s` (V9.1 eval F6 — faithful to the requirement). But **Kalshi
publishes no quote-update timestamp.** Measured on the live book:

```text
KXMLSGAME-26JUL25MINVAN-VAN   status ACTIVE
  two-sided prices 0.26 / 0.25, volume ~43k, open interest ~39k
  updated_time = 2026-07-23T19:02:29Z   →  ~108,880 s (~30 hours) old
```

Every KXMLSGAME market showed the same ~30 h; no other time field exists.
`updated_time` tracks the market **definition**, not the order book.

**Blast radius.** `execution_ready` was permanently False →
`risk.market_gate` rejected **every** paper signal `NOT_EXECUTION_READY` →
`slate.py` classified **every** fixture `EXECUTION_NOT_READY`. The first
slate would have produced locks and predictions but **zero paper-trading
evidence**, looking exactly like correct fail-closed behaviour. A gate that
can never pass protects nothing.

**Fix.** Lock policy **`mls-lock-v1` → `mls-lock-v2`**: freshness is the age
of *our* fetch, recorded honestly as `capture_time`. It does not claim to
know when the book last changed — no venue field permits that — and each
quote still stores the provider timestamp for audit. Verified against the
real book: `execution_ready` **False → True**.

**Coverage gap.** 475 tests passed with the platform's core purpose
disabled; nothing pinned the v1 rule. Three regression tests now do, one of
which fails without the fix.

---

## Operational findings

**A dated failure, four days out.** ESPN's MLS feed carries the All-Star
game (MLS All-Stars vs Liga MX All-Stars, event 401864004, Jul 30). Its
sides are not clubs, so identity correctly leaves them unmapped — but the
readiness invariant counted it as an unmapped upcoming fixture, i.e. a
**blocker**. On Jul 28 it would have entered the 48 h window and flipped
`shadow_ready` to false with nothing actually wrong. Non-club fixtures are
now excluded; a regression test reproduces the exact fixture.

**Production DiskFull incident.** The Railway PostgreSQL volume filled and
**every prediction write failed**, while the force sweep reported only
`{"created": 0}`. Cause: `SourceObservation` stored up to 200 KB of raw
response per row and the season-schedule ingest writes ~60 of them **on
every boot** — ~10 deploys in a day is ~100 MB. `payload_json` has **no
reader anywhere**; the `content_hash` is the evidence anchor. Payloads are
now capped at 8 KB, the sweep reports `no_prediction` and `failures`
instead of swallowing them, and `GET /api/admin/mls/storage` exists.
Operator resized the volume to 5 GB rather than delete evidence.

**Two display defects** (both reported by the operator, both self-
contradicting output):
- H2H vanished — ESPN dropped `headToHeadGames` and moved the data to
  `seasonseries` at HTTP 200. Parser reads both shapes.
- The form block showed **every defeat as a win** — ESPN's `score` string
  is *winner-first*, so a 0-1 loss rendered as "L 1-0". Wins and draws
  looked right, which is why it survived. The W/L/D letter is now derived
  from the same two numbers displayed beside it, so label and scoreline
  are **structurally incapable** of disagreeing.

---

## What was investigated and cleared

Not every suspicion was a defect. Recorded because negative results are
part of the evidence:

- **Props excluded from calibration** — suspected inconsistency (3 markets
  calibrated, 50 raw). *Measured:* props are slightly **under**-confident
  and shrinking them makes log-loss monotonically worse. Excluding them is
  **correct**.
- **λ-scaling for the residual −0.174 goals/match bias** — fixes the mean,
  **overshoots** over-2.5 (−0.0083). Rejected.
- **`INTEGRITY_FAILED` on 4 locks** — stale artifacts captured 2026-07-23
  in a DB with zero approval decisions, predating the feature.
- **Bracket resolver alert spam** — idempotent; 0 repeat announcements.
- **`model_cache` with zero test references** — used in 8 call sites.
- **Six "unguarded" scheduler jobs** — APScheduler's `job_defaults` already
  supply `max_instances=1`.

## Verified clean

Replay integrity (v5 *and* legacy v4 artifacts reproduce byte-for-byte) ·
money lock at code level (`approved_for_real_money` never set; **no
order-placement path exists**; `paper.py` makes zero HTTP calls) · admin
auth (403 on missing and wrong token) · Kalshi fee maths exact to the
centicent across 2,000 fuzzed orders · datetime normalisation · all
probability invariants (3-way sums, margin nesting, totals monotonicity,
first-goal partition) · session hygiene · cross-layer fee agreement to
0.0075 pp · settlement for win/loss/draw with exact Decimal P&L · all 12
scheduler jobs · all three frontend pages with zero console errors.

---

## Honest limits

Round 4 found no new defects, which is what diminishing returns look like
— **not** proof of correctness. Two surfaces remain untestable until
Saturday's matches kick off and resolve: **settlement against a real
completed fixture**, and **the in-play path during a live match**.

And one methodological caveat applies to every number in this edition:
these sweeps are tuned in-sample on 162 matches. Finding 2 demonstrated
exactly how that inflates an apparent edge. Treat all reported gains as
optimistic — which is one more reason the money gate stays closed.
