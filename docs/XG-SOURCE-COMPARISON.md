# Is API-Football's xG accurate, or merely present?

**Measured 2026-07-29 against Sportec's xG for the same 252 MLS matches.**

`docs/APIFOOTBALL-TRIAL.md` §8.2 named this gap in its own words: *"It says
nothing about whether the xG is any good. Presence is not accuracy."* MLS is
the only competition in this platform where two independent xG sources exist,
so it is the only place the check is possible. What is measured here is the
entire basis for trusting API-Football's xG in EPL, La Liga and Liga MX, where
no second source exists.

---

## The verdict, first

> **API-Football's xG is FIT to fit ratings on.** On 252 paired MLS fixtures it
> tracks Sportec closely (Pearson **r = 0.86** over all fixtures, **0.90** over
> the clean window), agrees with Sportec on **which team out-created in 88.9%**
> of fixtures, and when substituted into the MLS model it produces fitted
> ratings that rank the league near-identically (attack Spearman **0.86**,
> defence **0.89**, same best attack and best defence side) and an M3 rung that
> is **not significantly different** from Sportec's (Δ log loss −0.00305, 95% CI
> [−0.01118, +0.00530], n=176). It does not degrade the model. It nominally
> *slightly* improves it.
>
> **Two material caveats, neither of which changes that verdict but both of
> which bound it:**
>
> 1. **The agreement is not tight.** Mean |difference| is **0.35 xG per
>    team-observation** — 22% of the mean value — and the 90th percentile is
>    0.80. On 28 of 252 fixtures the two sources disagree about *which team was
>    the better side*. These are different xG models, not two reads of one
>    number. Anything that depends on a single match's xG being right is not
>    supported by this result; ratings averaged over a season are.
> 2. **This tells you much less about EPL and Liga MX than it looks like it
>    does.** See "What this does not establish" — this is measured agreement
>    with *one* competition's vendor on *one* season. It is evidence that
>    API-Football ships a competent shot model, not evidence that its EPL feed
>    is the same pipeline or the same quality.

**The most important incidental finding is not about API-Football at all:**
**Sportec's own xG is corrupted for the 2026-07-22/23 MLS restart slate**, and
Sportec labels those rows `data_status: "postmatch"` — final, not provisional.
The live MLS plane ingests that field and fits M3 on it. See
[Sportec's July defect](#the-finding-nobody-was-looking-for-sportecs-july-xg-is-broken).

---

## 1. Pairing — every fixture accounted for

Fixture identity never comes from names plus a date (`AGENTS.md` §5, §13).
There is no shared fixture id between these providers, so the join is two
stages and only the first touches a name.

### Stage 1 — team entities, from frozen references

| leg | mechanism | result |
|---|---|---|
| Sportec club id → ESPN team id | `three_letter_code == ESPN abbrev`, the mechanism `src/live/identity.py::resolve_mls_club` already requires | **30/30** clubs, 0 ambiguous |
| API-Football team id → ESPN displayName | the **frozen roster resolver**, read out of `feat-apifootball-probe`'s object store and sha256-asserted, not re-implemented | **30/30** teams = 100%, tiers 27 exact / 3 frozen alias, **0** token-set, **0** containment, **0** ambiguous |
| both sides bridged | intersection | **30/30** ESPN clubs |

- Frozen roster sha256 **`1c0f01745bcb72ff8621abdb9cf6420b0180f8400bff631817dbb15b26ab8a3c`**,
  asserted equal to the pre-data literal `docs/APIFOOTBALL-TRIAL.md` §7-A2
  publishes. The harness **refuses to run** on any other value — the identity
  bridge must not be built on a roster that may have been tuned.
- Resolver harness sha256 `d461a8762187d5e5c67579326fadd7d7942d28974527d0444b1349b027421c47`,
  criteria fingerprint `7b7106b0342d2c5d…` committed **and** recomputed equal.
- **Zero new aliases were introduced.** An unresolved or ambiguous team is
  dropped and named, never guessed. Nothing needed dropping.
- The frozen roster's 30 ESPN names were checked against the **live** ESPN
  `/teams` payload: no drift.

### Stage 2 — fixtures, from provider-stable ids only

Key = (home ESPN team id, away ESPN team id) → then exactly one candidate
within a **36-hour** window → then the **final score must agree exactly**.
Goals are not derived from xG by either provider, so the scoreline is an
independent corroboration. More than one candidate fails explicitly.

| | count | denominator |
|---|---|---|
| API-Football fixtures, MLS season 2026 | 510 | — (253 FT, 257 NS) |
| API-Football completed (FT/AET/PEN) | **253** | 510 |
| Sportec completed (`finalWhistle`) | **253** | 253 schedule rows |
| **paired** | **253** | 253 / 253 |
| dropped — team unbridged | 0 | 253 |
| dropped — no temporal match | 0 | 253 |
| dropped — ambiguous temporal match | 0 | 253 |
| dropped — score mismatch | 0 | 253 |
| dropped — unpaired Sportec match | 0 | 253 |
| **excluded — xG absent in one source** | **1** | 253 |
| **comparable fixtures** | **252** | 253 |
| **comparable team-observations** | **504** | 2 × 252 |

**Every completed fixture in the season paired, with zero drops.** The one
exclusion is `Orlando City SC vs Nashville SC`, 2026-07-25
(API-Football fixture `1490349`, Sportec `MLS-MAT-0009I1`): API-Football
returned two team entries but **the expected-goals statistic type was absent
from the response entirely** — not null, not zero, absent. Sportec had it.
So API-Football's MLS xG coverage is **252/253 = 99.6%**.

> A null / empty / non-numeric xG is treated as **ABSENT, never zero**. No
> observation in either source parsed to a literal 0 (`0 of 504` both sides),
> so no zero was silently admitted as a value.

**The join is corroborated by a field neither xG model touches.** Both
providers publish shot counts:

| field | API-Football mean | Sportec mean | Pearson r | exact match |
|---|---|---|---|---|
| shots on target | 4.833 | 4.974 | **+0.9752** | 74.2% |
| total shots | 12.837 | 13.054 | **+0.9836** | 48.0% |

Two payloads that agree to r≈0.98 on shots are describing the same matches.
The pairing is sound independently of the quantity being measured.

---

## 2. The comparison

Primary, pre-specified: **all 252 comparable fixtures / 504 team-observations.**

| statistic | value |
|---|---|
| **Pearson r** | **+0.8615** (R² = 0.7422) |
| **Spearman ρ** | **+0.8357** |
| API-Football mean xG | 1.4813 (sd 0.8657) |
| Sportec mean xG | 1.5960 (sd 0.9234) |
| **mean signed difference (API-F − Sportec)** | **−0.1147**, 95% CI **[−0.1617, −0.0649]** |
| — interval excludes zero | **yes** — API-Football is systematically **lower** |
| **mean absolute difference** | **0.3509**, 95% CI [0.3194, 0.3847] |
| OLS | API-F = **0.8076** × Sportec + 0.1923 |

CIs are **fixture-clustered bootstraps** (10,000 resamples, seed 20260729).
The two team-observations inside a fixture are the same match, the same
provider run and often the same disagreement, so an observation-level interval
would be too narrow.

**API-Football runs about 0.11 xG per team lower and compresses the spread**
(slope 0.81). For a ratings model that works on relatives, the slope matters
more than the level: a compressed spread pulls every team toward average.

### The distribution, not just the mean

|diff| per team-observation, n=504:

| p0 | p5 | p10 | p25 | **p50** | p75 | p90 | p95 | p99 | p100 |
|---|---|---|---|---|---|---|---|---|---|
| 0.000 | 0.020 | 0.039 | 0.109 | **0.253** | 0.469 | 0.795 | 1.002 | 1.553 | **2.248** |

```
|diff| in [0, 0.1)      117   23.2%  ###########
|diff| in [0.1, 0.2)     87   17.3%  ########
|diff| in [0.2, 0.3)     75   14.9%  #######
|diff| in [0.3, 0.5)    112   22.2%  ###########
|diff| in [0.5, 0.75)    57   11.3%  #####
|diff| in [0.75, 1.0)    30    6.0%  ###
|diff| in [1.0, 1.5)     19    3.8%  #
|diff| in [1.5, 2.25]     7    1.4%
```

The mean of 0.35 hides a real tail: **11.2% of team-observations differ by
more than 0.75 xG**, and 1.4% by more than 1.5. Signed differences run
p5 = −0.858 to p95 = +0.664 — the disagreement is two-sided, not a constant
offset that could be calibrated away.

### The worst disagreements, named

| \|diff\| | API-F | Sportec | team | fixture | date |
|---|---|---|---|---|---|
| **2.248** | 0.99 | 3.2384 | D.C. United | New England Revolution 1-0 D.C. United | 2026-04-11 |
| **2.125** | 2.72 | 4.8452 | Chicago Fire FC | Chicago Fire FC 5-0 Sporting Kansas City | 2026-04-26 |
| **1.928** | 3.10 | 1.1717 | Philadelphia Union | Philadelphia Union 3-1 Red Bull New York | 2026-07-22 |
| **1.873** | 2.67 | 0.7975 | Charlotte FC | Charlotte FC 2-2 Atlanta United FC | 2026-07-23 |
| **1.637** | 1.84 | 0.2029 | Minnesota United FC | Sporting Kansas City 2-1 Minnesota United FC | 2026-07-23 |
| **1.554** | 1.82 | 0.2657 | Vancouver Whitecaps | FC Cincinnati 4-3 Vancouver Whitecaps | 2026-07-22 |
| **1.524** | 1.17 | 2.6944 | Columbus Crew | Columbus Crew 0-0 Chicago Fire FC | 2026-03-08 |
| **1.478** | 1.78 | 0.3021 | Orlando City SC | San Jose Earthquakes 0-4 Orlando City SC | 2026-07-23 |
| **1.468** | 1.63 | 0.1616 | Atlanta United FC | Charlotte FC 2-2 Atlanta United FC | 2026-07-23 |
| **1.368** | 1.90 | 0.5317 | Chicago Fire FC | Inter Miami CF 3-2 Chicago Fire FC | 2026-07-22 |
| **1.290** | 0.95 | 2.2401 | Columbus Crew | New England Revolution 2-1 Columbus Crew | 2026-04-18 |
| **1.221** | 3.76 | 4.9814 | San Jose Earthquakes | San Jose Earthquakes 3-0 San Diego FC | 2026-04-05 |

Both directions appear. The two largest are Sportec **higher**; six of the
twelve are on 2026-07-22/23, which is not a coincidence — see §4.

Worst two fixtures with both sides shown:

```
2026-04-11  New England Revolution 1-0 D.C. United
    API-Football  0.80 - 0.99      Sportec  1.2534 - 3.2384
2026-04-26  Chicago Fire FC 5-0 Sporting Kansas City
    API-Football  2.72 - 0.36      Sportec  4.8452 - 0.6897
```

### Ordering agreement — the one that matters for a ratings model

Do both sources agree **which team out-created**?

| | count | rate |
|---|---|---|
| fixtures where both give a strict ordering | **252 / 252** | — |
| **agree on direction** | **224 / 252** | **88.9%**, 95% CI (Wilson) **[84.4%, 92.2%]** |
| **disagree on direction** | **28 / 252** | 11.1% |
| exact ties | 0 in both, 0 API-F only, 0 Sportec only | — |

All 28 are named in the archive. A representative spread:

```
2026-03-08  Colorado Rapids 4-1 LA Galaxy
    API-Football 1.88-1.09 (xGD +0.79)   Sportec 1.8301-2.0366 (xGD -0.21)
2026-05-03  Austin FC 2-0 St. Louis CITY SC
    API-Football 1.82-3.16 (xGD -1.34)   Sportec 2.2750-1.9528 (xGD +0.32)
2026-04-05  Houston Dynamo FC 0-1 Seattle Sounders FC
    API-Football 0.94-1.40 (xGD -0.46)   Sportec 1.3649-0.4350 (xGD +0.93)
```

Several disagreements are near-ties in both sources (LAFC 0-0 Colorado: xGD
−0.02 vs +0.02) where the direction is meaningless. Others are large and
genuine (Austin vs St. Louis: −1.34 vs +0.32 is a whole match's worth of
disagreement). **11% is the honest headline: roughly one MLS fixture in nine,
the two providers tell opposite stories about who deserved to win.**

Notably, ordering agreement is **stable at ~89% in both calendar windows**, so
it is *not* an artefact of the July defect.

---

## 3. Would substituting the source change the fitted ratings?

`src/live/model_mls.py` is inside the MLS engine signature; changing it
invalidates the live approval and halts shadow collection. It was **not
modified**. `fit()` is already a pure function of its inputs, so it was
imported and passed a different `xg_by_fixture` map per source — same 252
fixtures, same `as_of`, same `MLS_XG_RATING_ALPHA=1.0`, same
`XG_SHRINK_GAMES=6.0`, same `SHRINK_GAMES=24.0`. The only difference is the xG.

| | Sportec | API-Football |
|---|---|---|
| `league_xg` | 1.5409 | 1.4839 |
| `xg_coverage` | 1.0 | 1.0 |
| (`league_gpg`, goals) | 1.6163 | 1.6163 |

### Fitted ratings, 30 teams

| | attack | defence |
|---|---|---|
| mean \|difference\| | 0.0344 | 0.0408 |
| mean signed difference | −0.0001 | +0.0000 |
| **max \|difference\|** | **0.1063** (Portland Timbers) | **0.1076** (Sporting Kansas City) |
| league spread — Sportec | 0.5244 | 0.4929 |
| league spread — API-Football | 0.5444 | **0.3981** |
| **Pearson of ratings** | **+0.9409** | **+0.8970** |
| **Spearman of ratings** | **+0.8621** | **+0.8888** |
| teams whose rank changes | 21 / 30 | 27 / 30 |
| max rank move | 12 | 11 |
| best-rated side | **Inter Miami CF** (both) | **Vancouver Whitecaps** (both) |

Largest attack moves: Portland Timbers 1.1799 → 1.0736 (−0.1063), New England
0.9878 → 0.9029 (−0.0849), CF Montréal 0.9336 → 1.0043 (+0.0707).

The **top of the league is stable** — both sources pick the same best attack
and best defence — but the **middle reshuffles substantially**: 21/30 attack
ranks and 27/30 defence ranks move, with swings up to 12 places. Those are
mostly small absolute moves among teams that are genuinely close together, but
they are not nothing. Note also that API-Football **compresses the defence
spread** (0.398 vs 0.493, −19%), consistent with the OLS slope of 0.81.

### The M3 rung — rolling-origin walk-forward

Replicated with `model_eval`'s own imported `fit_variant` / `predict_variant` /
`_score_fixture`, so the rung definitions cannot drift from the deployed ones.
Both runs scored the **identical** 176-fixture set (76 skipped for
insufficient history).

| rung | log loss (Sportec) | log loss (API-Football) | Δ |
|---|---|---|---|
| M0 | 1.06294 | 1.06294 | 0.00000 |
| M2C | 1.04944 | 1.04944 | 0.00000 |
| **M3** | **1.03386** | **1.03081** | **−0.00305** |

M0 and M2C are byte-identical across sources — they consume no xG. That is the
internal validity check: only the xG-consuming rung moves.

**M3 under Sportec vs M3 under API-Football**, same fixtures, n=176:
Δ log loss **−0.00305** (negative = API-Football slightly better), 95% CI
**[−0.01118, +0.00530]**, **not significant**.

The ladder edge each source produces:

| edge | Sportec | API-Football |
|---|---|---|
| M3 vs M2C | +0.01558, CI [−0.00207, +0.03514], **not sig** | +0.01863, CI [+0.00036, +0.03816], *sig* |
| M3 vs M0 | +0.02908, CI [−0.00345, +0.06236], **not sig** | +0.03213, CI [+0.00031, +0.06493], *sig* |

**Read those two "significant" flags with maximum suspicion.** The lower bounds
are **+0.00036** and **+0.00031** — three ten-thousandths above zero, one
bootstrap resample from crossing. And the difference between the two sources is
itself **not** significant. The correct reading is *"both sources produce the
same small, marginal xG edge; API-Football's happens to land a hair on the
significant side of a line Sportec's lands a hair on the other side of."* It is
**not** evidence that API-Football's xG is better.

**Validation that this replication is faithful:** the Sportec M3-vs-M0 edge
here is **+0.0291 at n=176**, against the platform's standing deployed figure
of **+0.0269 at n=177, CI [−0.0050, +0.0596]** (`AGENTS.md` §6). Reproducing
the deployed number to within 0.002 on an independently assembled fixture set,
with no database, is strong evidence the ladder replication is sound.

**Answer to the question that matters: no, substituting API-Football's xG would
not change the MLS model enough to matter.** The rung difference is
statistically indistinguishable from zero, the league's best sides are
unchanged, and the aggregate rating difference (mean |Δ| ≈ 0.035) is an order
of magnitude smaller than the rating spread (≈ 0.52).

---

## 4. The finding nobody was looking for: Sportec's July xG is broken

A disagreement says two numbers differ; it does not say which is wrong. Two
diagnostics separate them, both computed from fields each provider publishes
**alongside its own xG**.

**Internal consistency — total match xG per that provider's OWN shots on
target.** No shot model produces a collapsing rate while shot volume holds.

| month | n | API-F xG | API-F SoT | **xG/SoT** | Sportec xG | Sportec SoT | **xG/SoT** | goals |
|---|---|---|---|---|---|---|---|---|
| 2026-02 | 19 | 2.761 | 9.26 | 0.2980 | 3.157 | 9.63 | 0.3278 | 2.79 |
| 2026-03 | 55 | 2.821 | 9.60 | 0.2938 | 3.290 | 9.85 | 0.3339 | 3.11 |
| 2026-04 | 70 | 3.010 | 9.84 | 0.3058 | 3.398 | 10.21 | 0.3327 | 3.44 |
| 2026-05 | 74 | 3.136 | 9.96 | 0.3149 | 3.350 | 10.15 | 0.3300 | 3.45 |
| **2026-07** | 34 | 2.830 | 9.00 | **0.3144** | **2.285** | 9.29 | **0.2459** | 2.88 |

API-Football's ratio is stable all season (0.294 – 0.315). Sportec's is stable
Feb–May (0.328 – 0.334) then **collapses to 0.246 in July** while its own shot
volume is unchanged. Broken down by date, the damage is two slates:
**2026-07-22 (0.177)** and **2026-07-23 (0.161)**; 07-25 (0.309) and 07-26
(0.347) are normal.

**A plausibility screen applied identically to both providers** — total match
xG under 0.06 per its own shot on target, when every month's league ratio is
0.24–0.34:

| provider | flagged |
|---|---|
| API-Football | **0 / 252** |
| **Sportec** | **4 / 252** |

```
2026-07-23  Charlotte FC vs Atlanta United FC            SP total xG 0.959 on 16 own SoT = 0.0599   goals 2-2
2026-07-23  Sporting Kansas City vs Minnesota United FC  SP total xG 0.334 on 15 own SoT = 0.0223   goals 2-1
2026-07-23  Colorado Rapids vs San Diego FC              SP total xG 0.184 on  4 own SoT = 0.0459   goals 1-0
2026-07-23  San Jose Earthquakes vs Orlando City SC      SP total xG 0.432 on 14 own SoT = 0.0308   goals 0-4
```

Fifteen shots on target and three goals cannot produce 0.334 total xG. These
rows are wrong.

**And Sportec labels all four `data_status: "postmatch"`** — final, not
provisional (`postmatch` on 252/252 matches). **There is no flag on the payload
an ingester could use to reject them.** `src/live/mls_stats.py` does not read
`data_status` at all, and the live MLS plane fits the M3 rung on exactly this
field. This is an active data-quality defect in the deployed pipeline, not a
hypothetical.

### Calendar-split sensitivity

The split boundary is the **World Cup break** — there is no June fixture at
all, because MLS paused for the tournament. That boundary is external to the
measured values; it is **not** a threshold chosen from the disagreement
distribution, which is what would make it cherry-picking.

| window | n | r | ρ | signed diff | MAD | OLS slope | ordering agree |
|---|---|---|---|---|---|---|---|
| **ALL (primary)** | 252 | +0.8615 | +0.8357 | −0.1147 [−0.162, −0.064] | 0.3509 | 0.8076 | 224/252 = 88.9% |
| **Feb–May (clean)** | 218 | **+0.8993** | **+0.8890** | −0.1751 [−0.216, −0.134] | 0.3236 | 0.8610 | 194/218 = 89.0% |
| **July (post-restart)** | 34 | +0.6309 | +0.5956 | **+0.2723** [+0.077, +0.476] | 0.5257 | 0.5904 | 30/34 = 88.2% |

The signed difference **flips sign** in July (API-Football *higher* by 0.27)
because Sportec collapsed, not because API-Football changed — its own xG/SoT
ratio was 0.314 that month, right on its season average.

**Read the Feb–May row as the cleaner estimate of how the two providers
compare, and the ALL row as the primary pre-specified result.** Neither
excludes the other: the July degradation is real data a live ingester would
have consumed. But it would be wrong to charge that window's disagreement to
API-Football.

Even in the clean window the agreement is **not tight**: r = 0.90, MAD = 0.32,
slope = 0.86, and 24 of 218 fixtures still disagree on ordering.

---

## 5. What this does NOT establish

Stated plainly, because the temptation to over-generalise this result is the
main risk it carries.

1. **MLS agreement tells you materially less about EPL, La Liga and Liga MX
   than it appears to.** This measures API-Football's MLS xG against *MLS's own
   vendor*. Nothing here establishes that API-Football's EPL, La Liga or Liga MX
   xG comes from the same upstream model, the same vendor, the same
   tracking-data quality, or the same latency. Providers routinely aggregate
   different suppliers per competition, and this repo has already been broken at
   HTTP 200 twice in a week by a provider changing behaviour silently. **The
   honest scope is: API-Football ships a competent, internally consistent shot
   model on the one league where we can check it.** That is a meaningful update
   on the prior — it rules out the "the number is junk / a placeholder" failure
   mode — but it is not a coverage guarantee, and the dark leagues remain
   genuinely dark.
2. **It measures one season and one day's snapshot** of that season.
3. **It says nothing about whether xG helps in those leagues.** MLS's own xG
   feature measured +0.0235, real, monotonic and **not significant at n=162**.
   Both sources here produce a ~+0.03 M3-vs-M0 edge that is marginal at n=176.
   Expect the same standard of proof per league, and expect it to need a
   season's fixtures.
4. **It does not validate single-match xG.** With MAD = 0.35 and an 11%
   ordering disagreement rate, any feature that leans on one match's xG being
   correct is not supported by this measurement. Season-aggregated ratings are.
5. **It does not test API-Football's xG for leakage or revision.** `CHECK 4` of
   the trial tested four not-started fixtures at one moment; nothing here tests
   whether values are revised after publication. The T-10 lock remains the only
   structural leakage guard.
6. **The 36-hour pairing window is a disambiguator, not a validation.** It
   separated the two league meetings of an ordered team pair (months apart) and
   every pair also had to agree on the scoreline. It would not detect two
   genuinely distinct matches between the same clubs inside 36 hours; MLS does
   not schedule those.

---

## 6. Recommendations

1. **Proceed with fitting league xG ratings from API-Football for EPL, La Liga
   and Liga MX.** The measurement supports it. Frame the per-league claim as
   *unverified-by-a-second-source* in whatever documentation ships, because it
   is.
2. **Do not treat the "significant" API-Football ladder edge as a result.** Its
   CI lower bound is +0.0003.
3. **Fix the Sportec ingestion defect.** `src/live/mls_stats.py` should record
   `data_status` and should carry an internal-consistency guard (xG per own
   shot on target) that quarantines a match rather than feeding it to the
   ratings. Four MLS matches in the live plane currently carry xG that is
   provably wrong, and the provider's own status field says they are final.
   Consider re-fetching the 2026-07-22/23 slate to see whether Sportec has
   since corrected it.
4. **Consider carrying both sources for MLS** as a standing cross-check. It is
   the only competition where a provider defect can be *caught*, and it caught
   one on the first try.
5. **Re-run this comparison at season end** (`--from-archive` makes the
   analysis replayable at zero quota cost; a fresh fetch is ~255 requests, 3.4%
   of one day's PRO quota).

---

## 7. Reproduction and evidence

```bash
cd ~/dev/TRIVELA/backend                      # branch research-xg-source-comparison
python scripts/compare_xg_sources.py          # live: ~255 API-Football requests
python scripts/compare_xg_sources.py \
    --from-archive research_archive/xg_source_comparison_2026-07-29.json
                                              # replay: 0 requests, recomputes everything
```

| artefact | detail |
|---|---|
| harness | `scripts/compare_xg_sources.py` |
| **raw evidence** | `research_archive/xg_source_comparison_2026-07-29.json` (6.03 MB) — **every** provider response body, key redacted |
| — its sha256 | `0834ebe79bf64de59bffa2001d3b0019507581dcb5a3f89ba46c502b3c45f059` |
| **full analysis** | `research_archive/xg_source_comparison_2026-07-30.json` (0.56 MB) — replay; bodies not duplicated, hash-linked to the above |
| requests spent | API-Football **255** (of 7,500/day), Sportec **254** |
| secrets | key travels only in a request header; never printed, never in a URL or param; every byte to stdout or disk passes `_redact()`. Verified absent from both archives and the script |
| archives | non-clobbering: a re-run on the same UTC date gets a `.runN` suffix rather than destroying earlier evidence |

**MLS engine untouched.** `src/live/model_mls.py`:

```
bbb0eaff0dcd13066ff425372e2920494f39b60408190d4542ecb3b6d2344c08  working tree
bbb0eaff0dcd13066ff425372e2920494f39b60408190d4542ecb3b6d2344c08  origin/main blob
```

Byte-identical, and recorded before *and* after the refit inside the archive.
Same for `src/live/model_eval.py` (`586bd35c…`), `src/live/mls_stats.py`
(`37ddf611…`), `src/models/xg_model.py` (`61f8753d…`), `src/live/identity.py`
(`39af769b…`). `git diff origin/main -- src/ tests/ api/ jobs/ config.py
live_migrations/` is **empty**: this branch adds one script and two archives and
changes no runtime code.

**Tests:** `.venv/bin/python -m pytest tests/ -q` →
**719 passed, 7 skipped, exit code 0** (20.71s), run unpiped so the exit code
is real. `AGENTS.md` §9 documents "~530 passed, 7 skipped" as of 2026-07-26;
the suite has grown since and that figure is stale — reporting what was
observed, per `CLAUDE.md` §5. `AGENTS.md` was not edited from this worktree
because concurrent branches are touching it.

**No CI ran.** Both repos' workflows trigger on push to `main` and on
`pull_request`; this is a feature branch with no PR, so local runs are the only
signal.
