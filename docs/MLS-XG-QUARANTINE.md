# Sportec's July xG is broken, and the live MLS ratings are eating it

**Date:** 2026-07-30 · **Branch:** `claude/hopeful-vaughan-b68ff7`
**Status:** detection and observability implemented; the ratings change is
staged behind a flag and **awaits Son's decision** (§6).

The xG source comparison found, incidentally, that Sportec's own expected
goals collapsed on the MLS restart slates while its own shot volume held
(`docs/XG-SOURCE-COMPARISON.md` §4 on branch
`research-xg-source-comparison`). This document is the follow-up: what was
independently reproduced, what was built, what it costs, and the one
decision left open.

---

## The verdict, first

| question | answer |
|---|---|
| Is the data still wrong? | **Yes.** 0 of 15 matches corrected on re-fetch, 8 days after kickoff. |
| Can a re-ingest fix it? | **No.** The provider is serving the same bytes. |
| Is there a provider flag to reject by? | **No.** `data_status` is `postmatch` — *final* — on all 253 matches, the broken ones included. |
| How many matches are affected? | **Seven**, not the four the original screen found. |
| Is the deployed model consuming it? | **Yes.** `MLS_XG_RATING_ALPHA=1.0`, and the M3 rung fits on this field. |
| Does the fix change model output? | **Not as shipped.** Detection lands inert; the exclusion is behind a flag defaulting to today's behaviour. |

---

## 1. What was independently reproduced

Per `AGENTS.md` §7 a review finding is evidence to investigate, not a patch
to apply. Three claims were checked before anything was written.

**The four flagged matches are unchanged.** Re-fetched the whole
2026-07-20 → 07-27 window from `stats-api.mlssoccer.com` (read-only,
throttled). Every value byte-identical to the 2026-07-29 capture:

```text
MLS-MAT-0009HE  Charlotte FC vs Atlanta United      xG 0.9591 -> 0.9591  unchanged
MLS-MAT-0009HJ  Sporting KC vs Minnesota United     xG 0.3338 -> 0.3338  unchanged
MLS-MAT-0009HG  Colorado Rapids vs San Diego FC     xG 0.1836 -> 0.1836  unchanged
MLS-MAT-0009HR  San Jose Earthquakes vs Orlando     xG 0.4316 -> 0.4316  unchanged
```

Extended to all 15 matches on the two affected slates: **0 of 15
corrected.** So the suggested "re-ingest plus a staleness policy" is ruled
out by measurement — there is nothing to re-ingest. A derived guard is
load-bearing, not belt-and-braces.

**`data_status` and `scope` exist, and are useless as a rejection signal.**
Both live on `match_statistics_list[0].match_statistics`, beside
`match_id`, `match_status`, `minute_of_play` and `result`. Every one of the
253 matches reads `data_status: "postmatch"`, `scope: "match"` — including
all seven broken ones. Recording them is worth doing so the provider's own
claim is *auditable*; it cannot be the guard.

**`mls_stats.py` is NOT inside the engine signature.** The task named
`model_mls.py` as off-limits. Checking `model_mls.engine_signature()`, the
source digest covers exactly:

```text
src.live.model_mls   src.models.simulator   src.models.xg_model   src.models.features
```

`mls_stats.py` is absent, so editing it does not change `signature_hash`
and does not invalidate the live approval. That is what makes this work
possible at all — but see §5: *not invalidating the approval is not the
same as not changing the model*, and that distinction drives the design.

---

## 2. The defect is wider than the original screen found

The comparison's screen (total match xG under 0.06 per own shot on target)
flagged 4 of 252. That threshold is far below the clean window's actual
floor, so it caught only the worst cases. Re-deriving the threshold *from
the clean window* raises the count to **seven**:

```text
2026-07-22  MLS-MAT-0009HP  Philadelphia Union:Red Bull New York   3:1
                xg 1.302 on 15 SoT = 0.0868
2026-07-22  MLS-MAT-0009HF  FC Cincinnati:Vancouver Whitecaps      4:3
                xg 1.371 on 12 SoT = 0.1143   P(7 goals | 1.371) = 0.00055
2026-07-23  MLS-MAT-0009HE  Charlotte FC:Atlanta United            2:2
                xg 0.959 on 16 SoT = 0.0599
2026-07-23  MLS-MAT-0009HJ  Sporting Kansas City:Minnesota United  2:1
                xg 0.334 on 15 SoT = 0.0223
2026-07-23  MLS-MAT-0009HG  Colorado Rapids:San Diego FC           1:0
                xg 0.184 on  4 SoT = 0.0459
2026-07-23  MLS-MAT-0009HD  Austin FC:Seattle Sounders FC          3:1
                xg 0.543 on  9 SoT = 0.0603
2026-07-23  MLS-MAT-0009HR  San Jose Earthquakes:Orlando City      0:4
                xg 0.432 on 14 SoT = 0.0308
```

The three the original screen missed are not marginal. **FC Cincinnati
4-3 Vancouver is seven goals on 1.37 total expected goals** — the single
most implausible match of the season by the goals test, and it sat at
ratio 0.114, just above a 0.06 cut. Austin 3-1 Seattle (0.0603) missed by
0.0003.

> A screen whose threshold is picked to catch the cases you already
> noticed will catch the cases you already noticed. The count was an
> artefact of the cut, not a property of the data.

### Counts differ from the comparison, for a stated reason

The comparison reports n=252; this analysis reports **253**. The
difference is one match — Orlando City vs Nashville, 2026-07-25 — which
**API-Football** had no xG for, so a *comparison* had to drop it. Sportec
carried xG on all 253 (`xg_absence.sportec.n_fixtures_with_absence: 0`),
and this analysis is Sportec-only, so 253 is the right denominator here.
The extra match falls on a clean slate and is not quarantined.

---

## 3. The guard, and why its thresholds are defensible

Two checks, both computed from **one payload's own internals** — the only
evidence available once the provider's status field is known to be
useless. `src/live/mls_stats.py xg_plausibility()`.

| check | signal | threshold | clean-window floor |
|---|---|---|---|
| `ratio` | total match xG per own shot on target | **< 0.12** | min **0.1443** (n=218) |
| `goals` | `P(N ≥ goals scored | total xG)`, Poisson | **< 0.02** | min **0.0228** (n=218) |

**The thresholds are derived from the clean window, not from the anomaly.**
Both sit *below* the tightest value the Feb–May data ever produced over 218
matches. This is why 0 false positives is a property rather than luck, and
`tests/test_mls_xg_guard.py` asserts both inequalities so that loosening
either past its clean-window floor fails the suite.

The split boundary is the **World Cup break** — MLS played no June fixture
— so it is external to the measured values, not a cut chosen from the
distribution.

Replaying the **shipped** guard over every archived payload:

| month | n | quarantined |
|---|---|---|
| 2026-02 | 19 | 0 |
| 2026-03 | 55 | 0 |
| 2026-04 | 70 | 0 |
| 2026-05 | 74 | 0 |
| **2026-07** | 35 | **7** |

All seven land on **2026-07-22 and 2026-07-23 exactly**. The slates either
side — 07-16/17/18 before, 07-25/26 after — are clean. That sharp
localisation is what distinguishes a bounded provider incident from a
screen that has started drifting into good data.

### Why keep two checks when one catches everything

`ratio` alone catches all seven; `goals` is currently redundant. It stays
because **it fails on a different input**: if the shot counts were ever
corrupted alongside the xG, the ratio would look normal and only the
scoreline would disagree. Each check records its own verdict, so if
`goals` ever becomes the one doing the work, that is a change in the
provider's failure mode and it will be readable rather than inferred.

`P(N ≥ 1 | xG = 0)` returns exactly `0.0`, not a small number — zero
expected goals producing a goal is a contradiction, not a tail event, and
no threshold should ever be able to sit under it.

### What the guard deliberately does not do

- **It does not rewrite the provider's number.** The raw `xg` is stored
  verbatim; the verdict is recorded beside it. Historical evidence is not
  edited to improve a result (`AGENTS.md` §3), and a later threshold change
  has to be re-derivable from what was actually received.
- **It does not screen sparse matches on the ratio.** Below 3 total shots
  on target the denominator carries no evidence. The clean window's sparse
  matches have *high* ratios, not low ones, so this closes a
  false-positive path rather than opening a hole.
- **NULL is not False.** A row written before the guard existed reads
  `NULL` — never screened. `False` means screened and passed. The report
  keeps them in separate fields; conflating them would convert missing
  evidence into confidence.
- **Disabling it does not stop it measuring.** `MLS_XG_GUARD_ENABLED=false`
  withholds the *verdict* only: the checks still run and the failure is
  still recorded in the reason string. An off switch that also stopped
  measuring would hide the next incident the way the last one hid behind
  `{"created": 0}`.

---

## 4. Observability

A guard that rejects silently is indistinguishable from a provider that
went quiet.

```text
row columns        xg_quarantined, xg_quarantine_reason (with the measured
                   numbers), data_status, scope        <- the durable record
log line           [mls_stats] xG QUARANTINE <match> <H>-<A> <score>:
                   <reason> (provider data_status=postmatch)
ingest response    xg_quarantined: N  (reported even when 0 — an absent key
                   reads as "the guard did not run")
coverage()         matches_xg_quarantined, matches_with_usable_xg
                   (coverage that counts a wrong xG as covered is coverage
                   that lies)
GET /api/admin/mls/stats/xg-quarantine
                   operator-only, read-only: the rejected rows with their
                   measured values, the provider's own claim beside them,
                   the active thresholds, and whether the ratings are
                   actually excluding anything
corpus             the new columns export through corpus.py's generic
                   _dump, so the verdict travels with the published bytes
```

---

## 5. What excluding the quarantined xG would cost

`model_mls.fit()` is **imported and parameterized, never edited** — it is
inside the engine signature. It is already a pure function of its inputs,
so three fits over the same 253 fixtures with three different
`xg_by_fixture` maps isolate exactly this effect, at `xg_alpha=1.0`, the
deployed setting.

```text
xg_coverage   1.000  ->  0.949
league_xg     1.5348 ->  1.5976   (+4.09%)

|Δ attack|    mean 0.0267   max 0.0458
|Δ defence|   mean 0.0266   max 0.0459

for scale, |xG rating - goals-only| attack:  mean 0.0669   max 0.1750
```

Two findings here matter more than the headline number.

**The contamination is league-wide, not confined to 14 teams.**
`league_xg` is the shared denominator every rating divides by, and the
corrupt matches drag it down 4%. So the 16 teams that never played in an
affected match still shift (mean |Δattack| 0.0241) — nearly as much as the
14 that did (0.0296). A defect in 7 of 253 matches perturbs all 30 teams'
ratings.

**The shift is material against the right yardstick.** A mean |Δattack| of
0.0267 is meaningless in the abstract; measured against the 0.0669 that
the entire xG rung buys over goals-only ratings, it is **~40% of the whole
xG effect**. League ordering moves too: 10 of 30 teams shift ≥3 places in
attack (2 shift ≥5, largest 8). Sub-1-place shuffles among near-ties are
noise and are excluded from that count.

**NOT_RUN: the M3 walk-forward ladder.** It scores locked predictions
against frozen market data in the live plane, which an archive replay
cannot reach. This is a *ratings* delta, not a ladder result, and must not
be reported as one.

---

## 6. The open decision — Son's, not an agent's

**Everything above is shipped inert.** `MLS_XG_QUARANTINE_EXCLUDE` defaults
to `false`, which reproduces exactly what the deployed M3 rung consumes
today; a test asserts that default so it cannot drift silently. Detection,
recording and observability all land without changing a single model
output.

That default is deliberate, and it is a genuine trade-off rather than a
safe choice:

- **For `false` (as shipped):** flipping it changes what the ratings are
  fitted on, which is a modelling change owed an evaluation and sequenced
  between slates — `AGENTS.md` §12 and the task's own constraint. It also
  keeps this branch mergeable at any time.
- **Against `false`:** provably wrong data continues to feed the deployed
  ratings until someone acts. The guard makes that visible instead of
  invisible, which is the valuable half, but visibility is not a fix.

**Recommendation:** flip it to `true`, between slates, and re-measure the
ladder afterwards. The data is confirmed wrong, confirmed uncorrected by
the provider, and the fallback is the goals rating that `fit()` already
degrades to — the mechanism is the same one a fixture with no xG at all
already takes. The measured cost in §5 is the argument *for* acting, not
against: a 40%-of-the-xG-effect distortion is too large to leave in
deliberately.

Sequence:

```text
1. between slates (no fixture inside the T-10 window)
2. set MLS_XG_QUARANTINE_EXCLUDE=true
3. re-ingest the affected range so the verdicts are written to existing
   rows (they currently read NULL — never screened):
      POST /api/admin/mls/stats-backfill?skip_existing=false
4. confirm:  GET /api/admin/mls/stats/xg-quarantine
5. re-run the M3 ladder and record the result
6. POST /api/admin/mls/approval/activate if the boot fell closed
```

Step 3 is not optional. The migration adds nullable columns and backfills
nothing, so **every existing row reads NULL and no fixture is excluded
until it is re-screened** — the flag alone changes nothing.

---

## 7. Scope: this covers the Sportec/MLS path only

The guard sits in `mls_stats.py` and screens `MlsTeamMatchStat`. The other
leagues' xG arrives by a **different route** — `apifootball.py
bridge_fixture_xg()` reading `ApiFootballFixtureXg` — which produces the
same `{fixture_id: {side: {...}}}` contract for the ladder but never passes
through this code. **EPL, La Liga and Liga MX xG is currently unscreened.**

That matters more there than here, not less: MLS is the one competition
with a second source, which is why this defect was catchable at all. The
comparison found API-Football's MLS xG internally consistent (0 of 252
flagged), but its own §5.1 is explicit that this says little about the
other leagues, where providers may aggregate different suppliers per
competition and there is no cross-check.

`xg_plausibility()` is a pure function over plain dicts, so it is directly
reusable there — **partially**. `ApiFootballFixtureXg` stores `xg`,
`xg_against`, `goals` and `goals_conceded` but **no shot counts**, so:

- the `goals` check applies unchanged, and
- the `ratio` check reports `applicable: false` rather than passing
  vacuously — verified, and pinned by a test.

So the follow-up is real but not free: it would give those leagues the
weaker of the two checks. Extending it is left undone deliberately — it
would change what three other ladders consume, which is a modelling change
owed its own evaluation, and this branch's scope is the MLS plane.

---

## 8. Reproduction

```bash
git show research-xg-source-comparison:\
research_archive/xg_source_comparison_2026-07-29.json > /tmp/xg.json
.venv/bin/python scripts/verify_xg_quarantine.py --archive /tmp/xg.json
```

Add `--refetch` to re-read the affected slates from the provider (15
throttled read-only GETs); omit it and the run makes no network request at
all.

| artefact | detail |
|---|---|
| harness | `scripts/verify_xg_quarantine.py` — calibrate, verify, cost |
| guard | `src/live/mls_stats.py xg_plausibility()` |
| tests | `tests/test_mls_xg_guard.py` — 27 tests |
| migration | `live_migrations/versions/c4e8a91f27b6_mls_xg_plausibility_guard.py` |
| evidence | `research_archive/mls_xg_quarantine_2026-07-30.json` |
| source evidence | `xg_source_comparison_2026-07-29.json`, sha256 `0834ebe7…3c45f059` — verified, hash recorded, bodies not duplicated |
| secrets | none: the MLS stats API needs no key; the archive was scanned |

**Validation.** `.venv/bin/python -m pytest tests/ -q` →
**1608 passed, 14 skipped, exit code 0**, run unpiped so the exit code is
real. With `PG_TEST_URL` against a throwaway local database (created and
dropped for the run): **1622 passed, exit 0**. Excluding the new file:
1581 passed, 14 skipped — so the 27 additions are purely additive and no
existing outcome changed.

> The local interpreter is Python 3.14; CI pins 3.12 (`CLAUDE.md` §2). A
> green local run is evidence about *this* interpreter, not proof CI passes.

`AGENTS.md` §9 documents "~530 passed, 7 skipped" as of 2026-07-26. That
figure is stale — reporting what was observed, per `CLAUDE.md` §5.
`AGENTS.md` was not edited from this worktree because concurrent branches
are touching it.

Migration verified up **and** down on throwaway SQLite and PostgreSQL
databases; the four columns land after `observed_at` in both, matching the
model's declared order, and `downgrade` drops exactly those four.

**No CI ran.** Both repos' workflows trigger on push to `main` and on
`pull_request`; this is a feature branch with no PR, so local runs are the
only signal.
