# API-Football — the one-month trial, pre-registered

Son bought **one month of API-Football PRO ($19, no auto-renewal)** to answer
a question that cannot be answered from outside the paywall. This document is
the pre-registration: what is being tested, the four criteria, how to run the
test, and what a pass does *not* prove.

> **THESE CRITERIA WERE FIXED BEFORE ANY DATA WAS SEEN.** They were written on
> 2026-07-29 into `scripts/verify_apifootball.py`, together with the frozen
> ESPN reference roster in `scripts/apifootball_espn_roster.json`, at a point
> when no API-Football key existed in this project and no paid response had
> ever been observed. That is the entire point of the file: a criterion
> invented after seeing the answer measures nothing.

---

## 1. Why a harness, and not just a look at the docs

Three facts forced this:

1. **The coverage claim is unverifiable from outside.** Nine of API-Football's
   documentation and dashboard URLs return HTTP 403 to `WebFetch` and to curl
   with a browser user-agent. Whether xG exists for EPL, La Liga, Liga MX and
   MLS could not be read; it had to be measured.
2. **The free tier cannot answer it.** The free plan was previously measured
   *season-blind for current seasons* in this project. A free key would have
   produced a confident answer about the wrong plan.
3. **This repository has an expensive history with hollow successes.** See
   §2.

TRIVELA fits its league rating models from goals alone for EPL, La Liga and
Liga MX because no public xG exists for them. MLS is the exception: real
Sportec xG is free at `stats-api.mlssoccer.com`, and it produced a measured
monotonic improvement. The trial asks whether the other three leagues can be
put on the same footing.

## 2. The governing rule

```text
ASSERT A NON-ZERO COUNT, NEVER A STATUS CODE.
A 200 WITH NOTHING IN IT IS A FAILURE, AND THE HARNESS SAYS SO IN THOSE WORDS.
```

The failure mode this rule exists for is the one that has cost this project
the most:

- `src/live_feed.py:221` records API-Football returning `results: 0` for a
  season-2026 fixture that was **live at the 45th minute**. HTTP 200. The free
  plan was season-blind and the empty array read as "no match on".
- A `{"created": 0}`-shaped success hid **every paper fill for a night** while
  a full production volume silently rejected each write.

So no check in the harness concludes anything from `r.status_code`, from
`errors: []`, or from the provider's own `results` field. Every check counts
the elements of `response`, and where the provider's own count disagrees with
the array it shipped, **the count wins and the disagreement is printed**.

There is exactly one deliberate exception, CHECK 4, which tests for the
*absence* of a value. There an empty payload is the desired result. It is
called out at the point it happens so nobody reads it as the rule being
relaxed.

### 2.1 The same rule applied to the provider's self-description

The harness records API-Football's own `coverage.fixtures.statistics_fixtures`
flag per league and prints it labelled **"context only — never a substitute
for the measurement"**. The first live run showed why that wording matters:
for EPL and La Liga the provider's own flag says `False` while the measured
payload carries xG for both teams. A harness that trusted the declaration
would have returned DROP on a provider that has the data.

## 3. The four criteria, as pre-registered

Each check is pass/fail and prints the measured numbers. `KEEP` requires all
four; anything else is `DROP` with the failing check named.

### CHECK 1 — xG exists for all four leagues

For each of EPL, La Liga, Liga MX and MLS: take a completed fixture from a
season the plan can see, call `fixtures/statistics`, and look for an
expected-goals statistic.

- **PASS requires a non-zero, non-null xG value present for BOTH teams in
  EVERY ONE of the four leagues.** One league short is a fail.
- A zero, an empty string, `-`, `null` or a non-numeric value is **absence**,
  not a value. Presence means parseable and strictly `> 0`.
- If the statistic *type* is absent from the response entirely, the harness
  says so explicitly — `THE EXPECTED-GOALS STATISTIC TYPE IS ABSENT FROM THE
  RESPONSE ENTIRELY` — and lists the statistic types the provider *did* offer.
  This was the most likely outcome and the most important one to report
  cleanly.
- xGA / expected-goals-conceded / goals-prevented do **not** count. Accepting
  them would let "xG coverage" be claimed on the strength of the opponent's
  number.
- Fixture selection is deterministic so it cannot be cherry-picked: the most
  recent fixture with status FT/AET/PEN whose kickoff is **at least 24h in the
  past**, so statistics have had time to be posted. If none, the most recent
  completed fixture, flagged. If the current season has no completed fixture,
  the harness falls back **once** to the most recent earlier season the plan
  lists, and prints which season it used.

### CHECK 2 — current-season visibility

For each league, request current-season fixtures and assert a non-zero count.
**This is the check the free tier failed.**

- **PASS requires `len(response) > 0` for every league.** An HTTP 200 with an
  empty `response` array is a FAILURE.
- The season is **calendar-derived, not provider-declared**: split-year
  leagues (eng.1, esp.1, mex.1) use `year` if month ≥ 7 else `year - 1`;
  calendar-year leagues (usa.1) use `year`. The provider's `current: true`
  flag is *not* used to decide, because the season list is plan-filtered — a
  plan that cannot see the live season could flag an older one "current" and
  pass a check it actually fails.
- The provider's `current` season and its count are printed as context, and if
  the calendar season comes back empty the harness spends **one** extra
  request diagnosing whether the provider's own current season is populated.

### CHECK 3 — identity stability

Three parts, all of which must hold.

- **Team id.** One team, two different fixtures, fetched as two separate
  `/fixtures?id=` calls. The API-Football team id must be identical.
- **Player id.** One player present by name in both fixtures'
  `/fixtures/players` must carry an identical player id. If no player is
  shared between the two fixtures the check is INDETERMINATE, which is
  **pre-registered as FAIL** — missing evidence is never converted into
  confidence.
- **1:1 resolution against our ESPN names.** Each league's API-Football team
  list is resolved against the frozen ESPN reference in
  `scripts/apifootball_espn_roster.json`, in four ordered tiers: exact
  normalised → frozen alias → equal token set → unique containment. The
  per-tier counts are printed, so it is visible how much of a rate rests on
  fuzz.
  - **Any ambiguity FAILS EXPLICITLY and is named.** One API-Football team
    matching two ESPN names, or two API-Football teams claiming the same ESPN
    name, is the defect that would silently mis-join a fixture. Nothing is
    ever silently picked.
  - **Thresholds: MLS 100%, EPL / La Liga / Liga MX 75%.** The asymmetry is
    deliberate and was set from ESPN evidence alone. The MLS reference is the
    30 names the live plane already keys on, operator-curated and verified
    against the KXMLSGAME slates of 2026-07-22 and 2026-07-25 — it is current,
    so it is held to 100%. The eng.1 / esp.1 / mex.1 references were
    **measured on 2026-07-29** to carry clubs that are not in those top
    flights (`Coventry City`, `Hull City`, `Ipswich Town`; `Deportivo La
    Coruña`, `Málaga`, `Racing Santander`; `Atlante`) and to omit clubs that
    are. That staleness can account for roughly 20–25% of a roster by itself,
    so 75% is the line below which a failure can no longer be blamed on our
    own reference.
  - The unresolved names are **always printed in both directions** — API-
    Football side and ESPN side — so a human attributes each miss instead of
    reading a percentage.

### CHECK 4 — no pre-match xG (leakage)

Find a fixture that has not kicked off and assert that no xG value is
populated for it.

- The fixture must have `status.short == "NS"` **and** a kickoff timestamp
  strictly in the future. The status alone is a provider claim.
- **A populated pre-match xG is a CRITICAL FAIL** (exit code 3). It would mean
  an xG feature could silently carry the outcome, which this repo treats as a
  disqualifying defect: target leakage through a provider's post-hoc field.
  The harness says so in those terms.
- A `null` or exactly-zero placeholder carries no outcome information; it is
  reported as an observation, not failed.
- An empty statistics response is a **PASS** here. This is the one check where
  an absent payload is the desired result, which is why it is stated
  separately from the governing rule.
- The harness also scans the not-started fixture *object itself* for any
  xG-shaped key. An empty statistics endpoint only proves the statistics
  endpoint is empty; a post-hoc field on the fixture would leak just as hard.
- **If no not-started fixture can be found, the check is INDETERMINATE, which
  counts as FAIL.** A leakage check that could not run must never read as
  clean.

## 4. How the criteria are protected from being retuned

Pre-registration is worthless if the file can be edited between the
registration and the run. Two mechanisms:

- **`CRITERIA_FINGERPRINT`** — every threshold lives in one declarative
  `CRITERIA` block, and the harness computes its sha256 at startup and
  **refuses to run** if it does not match the literal committed in the file.
  Changing a threshold therefore forces a visible change to that literal,
  which puts the change in the diff. The fingerprint is printed in the verdict
  and stored in every archive.
- **`ROSTER_FINGERPRINT`** — the same mechanism applied to the frozen ESPN
  reference, `scripts/apifootball_espn_roster.json`, which records per league
  the reference names, the source file, the source branch and the source blob
  sha they were read from. Its sha256 is asserted against a literal in the
  harness, so editing the roster halts the harness until that literal is
  updated too.

  This guard was **added after the gap it closes was exploited on the day the
  harness first ran** — see amendment A2 in §7. The roster is half the
  instrument, and adding one alias to it turns a DROP into a KEEP without
  touching a single threshold, leaving `CRITERIA_FINGERPRINT` unchanged and the
  edit invisible. A tunable reference roster makes CHECK 3's match rate
  meaningless.

- **Non-clobbering archives.** A second run on the same UTC date gets a
  `.run2` suffix rather than overwriting the first. This too was added in
  response to A2: the pre-registered archive had already been destroyed in
  place by a re-run.

Both files also carry an **amendment rule**: a change made after a real run is
a *post-registration amendment*, must be recorded in §7 of this document with
its date and reason, and **does not retroactively turn an earlier FAIL into a
PASS**.

## 5. How to run it

```bash
cd ~/dev/TRIVELA/backend
python scripts/verify_apifootball.py            # no arguments required
```

The key is read from `APIFOOTBALL_KEY`, and if that is unset from
`~/.apifootball_key` — one line, **mode 600**; a group- or world-readable file
is refused rather than read. The file route is preferred because the key then
never crosses a shell history or an agent transcript.

```bash
install -m 600 /dev/null ~/.apifootball_key     # then paste the key in
```

The repo's older `API_FOOTBALL_KEY` variable is **deliberately not** a
fallback: it may still hold the free-tier key, and measuring the free plan
confidently is exactly the failure this harness exists to prevent.

The key travels only in a request header. It is never printed, never stored,
never placed in a URL or a parameter, and every byte bound for stdout or the
archive passes through `_redact()` first. `/status`'s `account` block (name,
email) is dropped at the record layer.

**Budget.** PRO is 300 requests/minute and 7,500/day. A run costs **27
requests**, hard-capped at 40, with a fixed delay between them and at most one
retry after a fixed sleep — never a tight loop. The harness aborts if the
provider reports zero daily requests remaining.

**Exit codes.**

| code | verdict | meaning |
|---|---|---|
| 0 | `KEEP` | all four checks passed |
| 1 | `DROP` | at least one check failed |
| 2 | `INDETERMINATE` | could not measure — no key, budget abort, transport failure. Not a verdict. |
| 3 | `DROP` (critical) | pre-match xG populated |

**Evidence.** Every response is archived to
`research_archive/apifootball_verification_<UTC-date>.json` with the key
redacted, alongside the criteria, the criteria fingerprint, the sha256 of the
harness and of the roster, the request budget used, and the full stdout. A run
on a date that already has an archive gets a non-clobbering suffix rather than
destroying the earlier evidence. The file is ~4 MB, mostly full-season fixture
lists — deliberately kept whole so a reviewer can recount the arrays
themselves rather than trusting the harness's counts.

## 6. Results

### 6.1 First pre-registered run — 2026-07-29, PRO, 27 requests

Criteria fingerprint `493393bac7efd620…`, matching the committed literal.
Archive: `research_archive/apifootball_verification_2026-07-29.json`.

| check | result | measured |
|---|---|---|
| 1 — xG all four leagues | **PASS** | epl 2025 f1379342 Fulham **1.81** / Newcastle **0.31** · laliga 2025 f1391198 Villarreal **2.48** / Atlético Madrid **1.16** · ligamx 2026 f1550911 Pachuca **1.22** / Querétaro **2.07** · mls 2026 f1490359 San Jose **3.27** / LA Galaxy **1.28** |
| 2 — current season | **PASS** | season 2026 fixtures: epl **380**, laliga **380**, ligamx **153**, mls **510** |
| 3 — identity | **FAIL** | team id stable (San Jose Earthquakes = 1596 in two fixtures) · player id stable (18 players shared by name, ids identical) · rosters epl 20/20, laliga 20/20, ligamx 17/18, **mls 29/30 = 96.7% against a 100% threshold** |
| 4 — pre-match xG | **PASS** | four NS fixtures with future kickoffs, statistics empty for all four, no xG-shaped key on any fixture object |

**Verdict: DROP — CHECK 3.** Exit 1.

Two things worth stating plainly about that run:

- **The purchase answered its question.** xG exists for all four leagues,
  including the three that have no public xG source. And for EPL and La Liga
  the provider's *own* `statistics_fixtures` coverage flag says `False` while
  the payload carries xG for both teams — the governing rule is the only
  reason this did not come back a false DROP.
- **The CHECK 3 failure was ours, not the provider's.** The single MLS miss is
  `DC United` (API-Football) against `D.C. United` (our ESPN reference); Liga
  MX's single miss has the same shape, `U.N.A.M. - Pumas` against
  `Pumas UNAM`. Both are dotted initialisms that the harness's own normaliser
  split into single-letter tokens. That is a defect in the instrument, not an
  API-Football naming problem — and the pre-registered result stands as a FAIL
  regardless, which is what §7 is for.

## 7. Post-registration amendments

Recorded here as required by §4. Each entry states what changed, when, why,
and what it did to a previously reported result.

### A1 — 2026-07-29 · dotted-initialism normalisation

**What.** `norm_team()` now collapses a run of consecutive single-character
tokens into one token, so `D.C. United` normalises to `dc united` and
`U.N.A.M. - Pumas` to `unam pumas`. Added to `CRITERIA` as
`initialism_rule` so it falls inside `CRITERIA_FINGERPRINT`; the fingerprint
literal moved from `493393bac7efd620…` to `7b7106b0342d2c5d…` accordingly,
which is the mechanism working as intended — the amendment is unmissable in
the diff.

Note what this amendment did **not** do: it added no names and no aliases. The
frozen roster is byte-identical to its pre-data state
(`1c0f01745bcb72ff…`). `D.C. United` now resolves at the *exact* tier, and
`U.N.A.M. - Pumas` resolves through the `UNAM Pumas` alias that was already in
the table before any data existed.

**Why.** The first run's only CHECK 3 failures were `DC United` vs
`D.C. United` and `U.N.A.M. - Pumas` vs `Pumas UNAM`. Both are the same
punctuation defect on our side of the comparison. Nothing was learned about
API-Football that motivated this; what was learned is that the instrument
mis-tokenises initialisms.

**Effect on a reported result — stated because it is exactly what
pre-registration exists to expose.** This amendment turns CHECK 3 from FAIL
into PASS, and therefore the overall verdict from DROP into KEEP. It is a fix
to the measuring instrument, not a relaxation of a threshold: the 100% / 75%
lines and the zero-ambiguity rule are unchanged. But **the 2026-07-29
pre-registered run remains a DROP**, its archive is committed unmodified, and
the KEEP below is explicitly a post-amendment result.

Read the two together: the honest summary is *"three of four checks passed on
a pre-registered instrument; the fourth failed on our own name normalisation
and passes once that is fixed."* Not *"all four passed."*

### A2 — 2026-07-29 · the frozen roster was tuned to the data, and reverted

**This is the most important entry in this document.** Full record:
`research_archive/apifootball_tuning_incident_2026-07-29.json`.

**What happened.** Within an hour of the harness first running, two aliases
appeared in `scripts/apifootball_espn_roster.json` that this agent did not
write:

```text
mls     "DC United"        -> "D.C. United"
ligamx  "U.N.A.M. - Pumas" -> "Pumas UNAM"
```

Those are precisely — and only — the two team names that had just failed
CHECK 3. The harness was re-run on the edited roster, CHECK 3 went from FAIL
to PASS, the verdict went from **DROP to KEEP**, and the result was written
over the pre-registered archive at its canonical filename. The original
survived only because whoever did it also left a copy under a different name.

**Why `CRITERIA_FINGERPRINT` did not catch it.** It couldn't. Both runs carry
the *same* criteria fingerprint `493393bac7efd620…`, because no threshold was
touched. The roster is a separate file the fingerprint never covered. **That
gap, not the edit, is the defect.**

**How it was detected.** An untracked file appeared in the worktree that this
agent had not created. A structural diff against the canonical archive showed a
different `run_utc`, a different `espn_reference.sha256`, and CHECK 3 flipping
from FAIL to PASS.

**Remediation.**

- Both aliases removed. The roster now hashes to `1c0f01745bcb72ff…`, which is
  **byte-identical to the hash the pre-registered run recorded** — that match
  is the proof the restoration is exact and that those two aliases were the
  only difference.
- The pre-registered DROP archive restored to the canonical filename.
- `ROSTER_FINGERPRINT` added (§4), so this is now structurally impossible to do
  silently.
- `archive_path()` no longer overwrites an existing dated archive.
- This agent's own first amendment-A1 run was **discarded**, not reported: it
  had been launched while the tuned roster was still on disk, so its KEEP could
  not be attributed to A1 alone. A1 was re-measured against the restored
  roster, which is what §6.2 reports.

**What this does not establish.** Who made the edit. Another process wrote to
this worktree while it was in use, which `AGENTS.md` §7 and §8.1 forbid, and
both aliases are individually *correct* mappings — the edit looks
well-intentioned. The intent is not the point. The point is that a
well-intentioned edit to a frozen reference silently converted a DROP into a
KEEP, unrecorded, and destroyed the evidence that would have shown it. That is
the exact failure this harness was commissioned to be immune to, and it
happened to the harness itself on day one.

### 6.2 Post-amendment run — 2026-07-29, PRO, 27 requests

Criteria fingerprint `7b7106b0342d2c5d…` (amendment A1), roster
`1c0f01745bcb72ff…` (pre-data, restored). Archive:
`research_archive/apifootball_verification_2026-07-29.run2.json`.

| check | result | measured |
|---|---|---|
| 1 — xG all four leagues | **PASS** | unchanged from 6.1 |
| 2 — current season | **PASS** | unchanged from 6.1 |
| 3 — identity | **PASS** | rosters epl 20/20, laliga 20/20, ligamx **18/18**, mls **30/30**; zero ambiguous anywhere; team id stable (San Jose Earthquakes = 1596); player id stable (18 shared by name) |
| 4 — pre-match xG | **PASS** | unchanged from 6.1 |

**Verdict: KEEP** (post-amendment). Exit 0.

Resolution tiers, so it is visible how much rests on fuzz: EPL 14 exact / 6
alias, La Liga 20 exact, Liga MX 14 exact / 4 alias, MLS 27 exact / 3 alias.
**Zero** resolutions came from the token-set or containment tiers, and zero
were ambiguous — every match is either an exact normalised hit or an alias
frozen before the data.

> **Read §6.1 and §6.2 together, not §6.2 alone.** The honest one-line summary
> is: *three of four checks passed on the pre-registered instrument; the fourth
> failed on our own name normalisation and passes once that is fixed.* Not
> "all four passed".

## 8. What this harness will NOT tell Son, even if every check passes

This is the section to read before treating a `KEEP` as a decision.

1. **It says nothing about whether xG improves the model.** It measures that a
   number exists, is non-zero, and arrives for both teams. Whether an
   xG-derived feature beats the goals-only baseline for EPL, La Liga or Liga
   MX is a walk-forward evaluation that has not been run. MLS's own xG feature
   measured `+0.0235` — real, monotonic, **and not significant at n=162**.
   Expect the same standard of proof here, and expect it to take a season's
   worth of fixtures, not a month's.
2. **It says nothing about whether the xG is any good.** Presence is not
   accuracy. It does not check the values against Sportec's for the same MLS
   matches — which is the one league where that comparison is possible and
   would be the sharpest available test of the provider's xG quality. That
   comparison is not built.
3. **A clean CHECK 4 is four fixtures, not a guarantee.** It proves that four
   specific not-started fixtures had no populated xG at one moment. It cannot
   prove the provider never backfills, never populates during a match in a way
   that a later pull would treat as pre-match, and it does not audit any of
   the other endpoints for post-hoc fields. Leakage must still be prevented
   structurally by the T-10 lock, not by this check having passed.
4. **It measures one day.** Coverage, naming and latency can all drift, and
   this repo has been broken at HTTP 200 by exactly that twice in one week
   (ESPN killed H2H by renaming a field, and served a winner-first score
   string that rendered every defeat as a win). One green run is not a
   contract.
5. **It says nothing about the 2027 renewal price, the rate limits under real
   load, or the latency near kickoff.** 27 requests spaced half a second apart
   is not a T-10 sweep across a full slate.
6. **CHECK 3 does not prove we can join fixtures.** It resolves *team names*.
   Fixture-level identity — matching an API-Football fixture to our ESPN
   fixture and to a Kalshi market — is a separate mapping, and the repo's rule
   that fixture identity must never come from names plus a date still applies.
7. **A 75% roster threshold means up to a quarter of a league is unjoinable
   until our own ESPN reference is refreshed.** That is our work, not the
   provider's, but it is work that has to happen before any of this reaches a
   model. The measured run happened to resolve 100% everywhere, but the
   *criterion* still permits 75%, and the ESPN reference is still stale — the
   clubs listed in §3 under CHECK 3 are still wrong in our copy.
8. **"Pre-registered" is only as strong as the guards, and one was already
   breached.** §7/A2 records the frozen roster being edited to contain the two
   names that had just failed, flipping DROP to KEEP under an unchanged
   criteria fingerprint, on day one. It was caught, reverted with a hash proof,
   and structurally closed — but the lesson generalises: the value of this
   harness is not that it produced a KEEP, it is that its inputs are hashed and
   its archives are immutable, so a future flip can be *seen*. Verify the
   fingerprints in the archive against the literals in the script before
   believing any verdict, including this one.
