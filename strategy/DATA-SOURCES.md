# TRIVELA data sources — team and player statistics for MLS, EPL, La Liga, Liga MX

Research date: **2026-07-29**. Author: implementer agent, branch
`research-data-sources`. No account was created, no payment details were
entered, no terms were accepted anywhere in this research.

Every claim below is one of three things, and says which: **PROBED** (a
request was made from this machine and the response archived under
`research_archive/datasource_*_2026-07-29.json`), **QUOTED** (verbatim
from a public document, with the sentence reproduced), or **UNVERIFIED**
(a vendor claim that could not be exercised without a subscription — and
therefore worth exactly what an unexercised claim is worth).

---

## 0. The answer, before the evidence

**There is one source worth buying, and today is not the day to buy it.**

The source is **Sportmonks Football API 3.0**, on the Starter plan plus
the xG Basic add-on: **~€48/month (~€576/year), ~€40/month billed
annually.** It is the only candidate that *documents* xG for all four
leagues by league id, at team, player and shot level, with per-fixture
team stats and per-player match stats behind stable integer ids and a
terms of service that explicitly permits storing this data.

The reason not to buy it yet is not the price and not a doubt about the
vendor. It is that **three of the four leagues have no goals-only
evaluation ladder to measure an xG rung against** (backlog S-5), and the
only xG effect this platform has ever measured — the MLS provider-xG
rung, **+0.0235 vs baseline, NOT significant at n=162** — is not large
enough to buy blind. Buying now purchases a feature that cannot be
evaluated, for models whose base edge is itself unestablished
(**standing +0.0269, n=177, CI [−0.0050, +0.0596], not significant**).

So this document **agrees with backlog S-8** and does one useful thing to
it: it replaces S-8's probe vehicle. S-8 proposed one minimum-tier month
of API-Football (~$20–40) to discover whether xG exists for eng.1/esp.1.
That is the wrong instrument. API-Football will not let a non-browser
client read its own documentation (six URLs, all HTTP 403, archived), no
primary source anywhere confirms its xG, and the one behaviour this
project has actually measured from it was a silent empty answer on a
current season. Sportmonks *publishes the league list*, so the probe
stops being a discovery and becomes a confirmation — and its 14-day trial
makes the confirmation cost **€0** if cancelled in time.

---

## 1. The baseline any purchase has to beat

This matters more than any vendor comparison, because it is what makes
most of the market worthless here.

| Already held, free | Covers | Verified where |
| --- | --- | --- |
| **ESPN site API** — fixtures, scores, standings, team boxscores, per-player match stats via each summary's `participants` | all four (`usa.1`, `eng.1`, `esp.1`, `mex.1`) | in production; the fixture backbone |
| **`stats-api.mlssoccer.com`** — real Sportec xG, public, no auth | MLS only | `src/live/mls_stats.py`, migration `b7c8d9e0f1a2` |
| **Kalshi** — the frozen T-10 order book | contracted fixtures | the canonical market plane |

Consequence: **a paid feed's only real product here is xG for EPL, La
Liga and Liga MX.** Fixtures, results, standings, team boxscores and
per-player lines are already free and already integrated. Any candidate
that cannot deliver xG for those three leagues is selling this project a
more expensive copy of what it has — which is why SportsDataIO,
football-data.org, Highlightly and TheSportsDB all fail below despite
being perfectly competent products.

---

## 2. Comparison — candidate × the seven criteria

`Y` = evidenced yes · `N` = evidenced no · `?` = UNVERIFIED · `~` = partial

| Candidate | 1. Four leagues + 2026-27 season | 2. Fixture team stats + player match stats | 3. xG (and pre- vs post-hoc) | 4. History ≥2–3 seasons | 5. Real contract | 6. Stable identity | 7. Cost at this volume |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Sportmonks** ← recommended | **Y** leagues (PL 8, La Liga 564, Liga MX 743, MLS 779 — QUOTED from the xG coverage doc *and* the coverage matrix, `Match Statistics = YES` for all four). Season 2026-27 visibility **?** — no plan-scoped season limit is documented, but no key, so uncalled | **Y** documented — 97 fixture-scope statistic type codes, team level (`SHOTS_TOTAL`, `SHOTS_ON_TARGET`, `BALL_POSSESSION`, `CORNERS`, cards) and player level (`MINUTES_PLAYED`, `KEY_PASSES`, `TOUCHES`, `RATING`). Per-league presence **?** | **Y** — 13 expected types incl. `EXPECTED_GOALS`, `EXPECTED_GOALS_ON_TARGET`, `EXPECTED_NON_PENALTY_GOALS`; team, player **and shot** level. **Post-match only** on xG Basic: *"Access to all xG statistics immediately after match completion."* No pre-match xG is sold — the safe answer | **~** core stats: *"Historical data older than three seasons is available as a one-time add-on"* → 3 seasons on Starter. **xG: 2024/25 onwards ONLY** — *"The xG data is available from the 2024/2025 season to date."* ~2 completed seasons | **Y** versioned REST API, documented per-entity/hour limits with 429 semantics, published ToS: *"distribution, transfer, and storage of data provided by our services is allowed, but reselling the product is forbidden"* | **Y** integer `fixture_id` / participant id / `player_id` reused across endpoints. **N** on ESPN mapping — no cross-reference field; a bridge must be built | **€29 Starter (pick any 5 leagues) + €19 xG Basic = €48/mo**; ~€40/mo annual. 2,000 calls/entity/hour |
| **API-Football / api-sports.io** ← runner-up | leagues near-certain; **?** season visibility, and this is the exact trap: measured 2026-07-09, free plan returned an empty `live=all` for a season-2026 fixture *in progress* (`src/live_feed.py:221`) | **?** `/fixtures/statistics` and `/fixtures/players` exist per secondary sources; field list unreadable | **?** unchanged from S-8. No primary source obtainable; a competitor states its xG *"should be treated as inconsistent unless you have verified the exact league, season, plan, and endpoint"* | **?** | **~** a real API with real limits, but **PROBED: six vendor doc/pricing URLs all HTTP 403** to WebFetch *and* to curl with a browser UA. Contract diligence is not possible from outside a browser | **?** integer ids assumed; `player.api_football_id` exists in this schema from WC26 | **$19/mo Pro (7,500 req/day)** — secondary source only. Cheapest candidate *if* its xG is real |
| **football-data.org** | **Y — PROBED, the strongest season evidence in this table.** `/v4/competitions` answers **200 with no token**: EPL `2026-08-21..2027-05-30`, La Liga `2026-08-16..2027-05-30`, MLS 2026 matchday 18, Liga MX Apertura `2026-07-17..2026-11-23` matchday 3 | **~ / N** — €15 Statistic Add-On gives *"corners, free-kicks, offsides, fouls, ball possession, saves, shots on/off goal, cards"*. **No per-player match stats** — deep data is lineups, scorers, cards, squads | **N — absent.** xG appears nowhere in the pricing page, the add-on field list or the coverage table | **~** *"10 seasons history"* on the ML Pack; `numberOfAvailableSeasons` 128 (EPL), 95 (La Liga), 10 (Liga MX), 7 (MLS) | **Y** clean: documented v4 API, per-minute limits, published tiers | **Y** stable integer ids (EPL 2021, La Liga 2014, MLS 2145, Liga MX 2113) | **€99 Advanced + €15 Stats = €114/mo = €1,368/yr.** PROBED: the four leagues sit on *three different tiers* — EPL/La Liga Tier 1, MLS Tier 2 (€49), Liga MX Tier 3 (€99) |
| **StatsBomb / Hudl free open data** | **N — PROBED.** Liga MX absent entirely; EPL only 2003/04 + 2015/16; MLS only 2023; La Liga 2004/05–2020/21. No current season | **Y** richest in the table (event-level) | **Y** shot level with freeze frames — the best xG here, for the wrong seasons | **N** for currency | **Y** *"freely available for public use for research projects"*, attribution required | **Y** | **free** |
| **StatsBomb / Hudl (commercial)** | **?** *"190+ competitions"*, no league list published | **?** | **?** advertised | **?** | sales-gated | **?** | **no published price** — *"Fill out this form to get in touch with our sales team"* |
| **Stats Perform / Opta** | **?** *"3,900+ competitions"* | **?** | xG + XY tracking advertised | **?** | sales-gated; `statsperform.com/opta-api/` is 404 | **?** | **no published price**; four figures/season territory. S-8 already ruled this out on value |
| **Sportradar** | **?** | **?** | not mentioned | **?** | public dev portal, private pricing | **?** | **~$500–$1,000+/mo** (third-party) |
| **SportsDataIO** | **Y** names all four by name | **~** "team and player stats", granularity unstated | **N** — xG not mentioned anywhere on the soccer page | **~** trial gives *"last season's data"* | **~** no published pricing | **?** | **$99–$149/mo** (third-party) for a feed with no xG |
| **TheStatsAPI** | **?** — docs name **no leagues at all** | **Y** documented `/matches/{id}/stats` grouped incl. xG | **Y** documented `/matches/{id}/shotmap`: *"every shot in a match with xG, coordinates, body part, situation ... and non-penalty xG summary"* | claims *"10 years"* — **?** | **N on provenance.** The API *shape* is excellent (`xg_available` per-competition booleans, stable string ids `comp_3039` / `mt_838955483`) but **the source of the xG is never stated**, and the entity model (shotmap + body part + situation + player heatmaps + ratings) is shaped exactly like SofaScore's. A vendor that will not name its supplier cannot clear criterion 5 in a repo that rejected scraping on principle | **Y** stable string ids | **$50 / $129 / $379 per month** |
| **SofaScore + its RapidAPI resellers** | — | — | — | — | **N — PROBED.** `api.sofascore.com/api/v1/...` → **403**; `sofascore.com/terms-of-use` → **403**. Undocumented internal endpoint behind bot protection, terms unreadable | — | rejected |
| **football-data.co.uk** (free CSV) | **Y — PROBED**, incl. Liga MX 2026/2027 and MLS 2026 | **~ asymmetric — PROBED.** `E0`/`SP1` carry `HS/AS`, `HST/AST`, `HC/AC`, `HF/AF`, cards but no possession; **`MEX.csv`/`USA.csv` carry goals and odds ONLY** | **N** | **Y strong** — Liga MX 2012/13→2026/27 (15 seasons), MLS 2012→2026 | **? unclear.** No licence statement found; the site credits ESPN, Flashscore, Betbrain, Oddsportal as upstreams. **Terms unclear — not guessed** | **N — fails criterion 6.** Teams are free-text names, no ids. Exactly the "team names used as stable IDs" defect `AGENTS.md` §5 names | **free** |
| **TheSportsDB** | **Y** metadata; PROBED with the documented public test key | **N** | **N** | **~** | **Y** | **Y** — and it carries `idAPIfootball` cross-reference ids, a genuinely useful identity-bridging artefact | free / $9–$20 |
| **Highlightly** | MLS named, Liga MX not | **?** | **N** not mentioned | **?** | **Y** | **?** | $9.49–$45.99/mo |
| **apifootball.com** (Elenasport — a *different* vendor from api-football.com) | **?** | **?** | **N** none found | **?** | **N.** PROBED: returns **HTTP 200** with body `{"error":404,"message":"Authentification failed!"}`. A provider that lies in its status line is a fail-open ingestion hazard for a platform whose whole discipline is that missing evidence must never read as success | **?** | — |
| **Sportec Solutions direct** | **N** — MLS + Bundesliga only | — | Y (already have it free for MLS) | — | — | — | nothing to buy that widens coverage |
| Understat · FBref · FotMob · `ligamx.net` `/ws/<base64>` · `apim.laliga.com` | **already rejected in this repo.** Not reopened, not re-probed, not proposed | | | | | | |

---

## 3. Recommendation

### Buy (when the trigger fires): Sportmonks Starter + xG Basic

Why it wins, in order of weight:

1. **It is the only candidate that names our four leagues in its own xG
   coverage list.** Not "top leagues" marketing — a table of 50 league
   ids, of which Premier League `8`, La Liga `564`, Liga MX `743` and
   Major League Soccer `779` are four. Liga MX is the one every other
   affordable candidate quietly omits.
2. **Its per-league coverage matrix independently confirms the stats
   layer.** `Match Statistics`, `Advanced Player Stats`, `Standard Player
   Stats and Lineups` and `Historical data` all read YES for all four.
   (Only **112 of 2,314** leagues have Advanced Player Stats — ours do. The
   parser behind that claim was validated against a sparse league,
   Mauritian League `#1523`, which correctly reads NO on six flags,
   before its output on our four was trusted.)
3. **Post-match-only xG is the right answer, not a limitation.** It sells
   no pre-match xG, so there is no product here that could tempt a
   leakage bug. xG can feed ratings from *completed prior* matches, which
   is precisely how the MLS xG rung already works.
4. **The terms permit this use, in writing.** *"distribution, transfer,
   and storage of data provided by our services is allowed, but reselling
   the product is forbidden without our consent."* Private research plus a
   paper-trading record resells nothing. (The Copyright section's
   "personal and non-commercial use" sentence is scoped to website
   *material* — text, illustrations, audio, video — not to API data, which
   the preceding sentence explicitly allows to be stored and distributed.)
5. **The cheapest tier is the right tier.** *"All plans include the same
   professional-grade data features. The only difference between plans is
   the number of leagues you can select and your API call capacity."*
   We need four leagues. Starter gives five.

### Runner-up: API-Football, and why it lost

On price it should win — ~$19/mo against ~€48/mo, for a project that
should not be spending money on an unproven edge at all. It lost on
**verifiability**, which for this repo is a first-class criterion:

- Its pricing, documentation and coverage pages return **403 to every
  non-browser client tried**, including a browser User-Agent. No attempt
  was made to defeat that, so its xG remains what S-8 called it:
  UNVERIFIED. It cannot be *bought* on evidence, only on hope.
- The one thing this project has measured about it is the worst possible
  failure mode: **HTTP 200, `results: 0`, on a season-2026 fixture that
  was live at the 45th minute.** That is indistinguishable from "nothing
  is happening", and it is now permanently encoded as a fallback path in
  `src/live_feed.py`. A provider whose absence looks like a normal
  response is a provider this platform has already been burned by.

If Sportmonks fails its probe (§6), API-Football is where to look next —
but the probe has to be run *from a browser session* to read the docs
first, and it has to defeat the 200-with-zero-rows trap explicitly.

### Best-evidenced source that does not solve the problem: football-data.org

Worth naming because it is genuinely the cleanest thing found and it is
tempting. Its unauthenticated `/v4/competitions` is the single strongest
piece of season evidence in this whole document — it *proves*, with no
key, that all four 2026-27 seasons exist and are being updated. But it
has **no xG at all** and **no per-player match stats**, and the four
leagues straddle three price tiers so the cheapest plan covering them is
**€114/mo — 2.4× Sportmonks for strictly less than ESPN already gives.**
It fails criteria 2 and 3, which are the only criteria that justify spend.

---

## 4. Cost at this volume, and what the cheapest useful tier buys

Volume: **~1,600 fixtures/season** across the four (EPL 380, La Liga 380,
MLS ~510, Liga MX Apertura+Clausura+liguilla ~340), plus in-season daily
polling.

| Line | Monthly-billed | Annual-billed |
| --- | --- | --- |
| Sportmonks **Starter** — *"Pick any 5 leagues worldwide"*, 2,000 API calls/entity/hour | €29 | €24/mo |
| **xG Basic** add-on — all 13 xG metrics, team + player + shot, post-match | €19 | ~€16/mo |
| **Total** | **€48/mo · €576/yr** | **~€40/mo · ~€480/yr** |
| Optional: history older than 3 seasons | from €29 **one-time** | same |
| Optional: 5th/6th league slot (see the playoff gotcha in §6.6) | from €4/mo | same |

> **Price discrepancy to settle at checkout, not in advance.** The xG
> coverage doc prices xG Basic on Starter at **€19/mo**; the pricing page
> prices an *"xG & Pressure Index"* bundle from **€29/mo (€24 yearly)**.
> Either two products or one stale page. Read the line item before
> confirming.

**Rate headroom is a non-issue.** 2,000 calls per entity per hour against
a workload of ~1,600 hydrated fixture calls for a whole-season backfill
(~4,800 for three seasons) and a daily in-season poll measured in tens of
calls. The Starter plan is not a constraint; the league count is.

**What the cheapest tier actually buys, stated plainly:** the same full
feature set as the €249 Pro plan, restricted to five chosen leagues and a
lower hourly ceiling — plus three seasons of history and xG back to
2024/25. Nothing about the recommendation depends on upgrading.

---

## 5. What this recommendation does NOT solve

Stated plainly, because the value of the purchase is easy to overstate:

1. **It does not create an edge.** Standing result: **+0.0269, n=177, CI
   [−0.0050, +0.0596] — not significant.** The only measured xG rung on
   this platform: **+0.0235, not significant, n=162.** Buying xG buys a
   candidate feature, not a result.
2. **It does nothing for MLS**, which already has real Sportec xG for
   free. The purchase is for three leagues, and its value is dominated by
   ladders that do not exist yet.
3. **xG history stops at 2024/25.** Goals go back a decade; xG goes back
   about two completed seasons. So an xG rung and a goals-only rung
   *cannot* be compared on their natural windows — the comparison must
   hold the window fixed to the xG era or it is not a comparison. This is
   the sharpest limitation and it is easy to get wrong.
4. **No pre-match xG exists to buy.** xG may only feed ratings from
   completed prior matches. Anything else is target leakage, which
   `AGENTS.md` §13 treats as a critical defect.
5. **It adds a second identity space.** ESPN stays the fixture backbone,
   so every Sportmonks fixture, team and player must be reconciled to an
   ESPN one, and every ambiguous match must fail explicitly rather than
   pick. The platform has done this before (ESPN↔Sportec players, 99.5%
   via per-match participants) and knows it is real work with real new
   failure modes.
6. **Sportmonks retroactively corrects historical data**, by its own
   documented process, with no version or as-of guarantee: *"once a data
   correction is reported or identified, our data correction team
   immediately investigates the issue and takes the necessary steps to fix
   it."* A silently recomputed historical xG would mutate a stored feature
   *after* a replay was fitted on it. **Mitigation is mandatory, not
   optional: snapshot the raw payload at ingestion and fit from the
   snapshot, never from a re-fetch.** This is the same class of hazard as
   the Kalshi `updated_time` incident — a provider field that did not mean
   what a requirement assumed.
7. **No tracking, no possession-value, no freeze frames.** That is
   StatsBomb/Opta territory and remains unbought and unjustified.
8. **Sportmonks' xG model is undisclosed.** Comparing an xG rung across
   MLS (Sportec) and EPL/La Liga/Liga MX (Sportmonks) compares two
   different models. Do not pool them into one effect estimate.

---

## 6. Verification plan — the requests Son should run before committing money

Run these inside the **14-day free trial** on Starter + xG Basic, then
**cancel before day 14 unless 6.1, 6.2 and 6.3 all pass.** The trial
requires a credit card and *"After 14 days, your credit card will be
charged, unless you choose to cancel your subscription in time"* — set a
calendar reminder for day 12. Subscribing and entering payment details is
**Son's action, not an agent's.**

> **The governing rule for every probe below: assert a non-zero COUNT, never
> a status code.** The failure that burned this project was HTTP 200 with
> an empty array. Every probe therefore needs a *positive control* — a
> request that must return rows — so that "empty" can be distinguished
> from "absent".

**6.1 Season visibility (the specific thing that burned us)**
```
GET /v3/football/leagues/8?include=currentSeason      # EPL
GET /v3/football/leagues/564?include=currentSeason    # La Liga
GET /v3/football/leagues/779?include=currentSeason    # MLS
GET /v3/football/leagues/743?include=currentSeason    # Liga MX
GET /v3/football/fixtures/between/2026-08-21/2026-08-24?filters=fixtureLeagues:8
```
Pass = a 2026/2027 season object for 8 and 564, a 2026 season for 779, the
current Apertura for 743, **and** a non-empty list of scheduled EPL
matchday-1 fixtures. Fail = any empty `data` array. Archive all five.

**6.2 xG existence, per league, one league at a time**
For a **completed** fixture in *each* of the four leagues:
```
GET /v3/football/fixtures/{id}?include=statistics.type;lineups.details.type
GET /v3/football/expected/fixtures/{id}          # or the documented xG include
```
Pass = type `5304 EXPECTED_GOALS` present for **both** participants, a
player-level xG row, and a non-empty shot-level payload — **in all four
leagues separately.** Do not generalise from EPL to Liga MX; Liga MX is
where every other vendor's coverage quietly stops.

**6.3 Pre-match vs post-hoc — the leakage guard**
Pick a fixture that has **not kicked off**. Request its xG payload at
roughly T-10 and archive the response. Then request the same fixture
after full time and archive again.
Pass = **xG absent/null before kickoff, present after.** That is the
evidence the corpus needs to show no xG value existed at lock time. If xG
were ever populated pre-kickoff, it is either a forecast (must be labelled
as such and can never be mixed with the post-match series) or a leak —
either way, stop and re-scope.

**6.4 Identity stability**
- Fetch the same fixture twice, ≥7 days apart; assert `fixture_id`,
  participant ids and `player_id`s are byte-identical.
- Fetch a player who changed clubs between seasons; assert `player_id` is
  constant across both clubs.
- Build the ESPN↔Sportmonks **team** map for all four leagues and assert
  **1:1 with zero ambiguous matches.** An ambiguous match must fail
  explicitly (`AGENTS.md` §13) — never silently pick a team.

**6.5 History depth, and proving the xG cutoff on purpose**
```
GET /v3/football/seasons?filters=seasonLeagues:8
```
Count the seasons actually returned on Starter; assert ≥3 and record the
oldest. Then deliberately request xG for a **2023/24** fixture and confirm
it comes back **empty** — matching the documented 2024/25 start — so that
a missing-xG season is never later mistaken for a bug.

**6.6 Competition structure (a cheap gotcha with a real price tag)**
Confirm whether **MLS playoffs** and the **Liga MX liguilla / play-in**,
and the **Apertura↔Clausura split**, live *inside* league ids 779 and 743
or are separate competitions needing extra league slots at €4/mo.
football-data.org lists "MLS - Playoffs" as a *separate* competition
(2175) — if Sportmonks does the same, the Starter plan's five slots are
tighter than they look.

**6.7 Rate-limit semantics**
Burst a few dozen calls and read the response headers; confirm the
documented per-entity/hour accounting and capture the 429 body. Two
incidents on this platform (Kalshi `updated_time`, the volume DiskFull)
were both "a field did not mean what a requirement assumed." Verify the
meter before trusting it.

**Abort conditions** — any one of these ends the trial with a cancellation
and no spend: xG missing for **any** of the four leagues (6.2); an empty
season for any league (6.1); xG populated **before** kickoff without a
clear forecast/observation distinction (6.3); or ambiguous team identity
that cannot be resolved 1:1 (6.4).

---

## 7. Relation to backlog S-8

**S-8's conclusion stands. Its instrument changes.**

| S-8 as written | This research |
| --- | --- |
| Defer the purchase; re-open when (a) the league's goals-only ladder exists (S-5) with the M2-shape rung holding, **and** (b) MLS prospective evidence at n ≥ 250 keeps the xG rung's sign | **Unchanged. Both trigger conditions are the right ones and neither is met.** |
| *"No public source exists for either league"* | Confirmed, and extended to Liga MX. Nothing found changes it. |
| Probe vehicle: one minimum-tier month of API-Football, ~$20–40 | **Replaced.** API-Football's own docs are 403 to non-browser clients, no primary source confirms its xG, and its measured failure mode here is a silent 200. **Use the Sportmonks 14-day trial instead: €0 if cancelled, and it confirms a published list rather than discovering an unpublished one.** |
| *"Enterprise feeds (Opta/StatsBomb, four figures/season) are not justified"* | Confirmed by probe: neither publishes a price at all; both are sales-gated. |
| Cost of the eventual buy left open | **Now priced: €48/mo, ~€576/yr, ~€480/yr annual.** The deferral is now a decision with a number attached. |

**Suggested S-8 amendment** (for Son, not applied here): keep the
hypothesis, the evidence and the trigger; swap the probe vehicle to
Sportmonks Starter + xG Basic, record the €576/yr figure, and add the
snapshot-at-ingestion requirement from §5.6 as a precondition of the buy
rather than an implementation detail discovered afterwards.

---

## 8. Evidence index

All under `research_archive/`, dated `2026-07-29`:

| File | Contains |
| --- | --- |
| `datasource_sportmonks_2026-07-29.json` | 401 unauth probe; full 50-league xG coverage list; per-league feature flags for all four targets **plus the sparse-league control**; 97 fixture statistic type codes; 13 expected types; pricing, FAQ, rate-limit and ToS quotes |
| `datasource_football_data_org_2026-07-29.json` | the unauthenticated `/v4/competitions` catalogue for all four leagues with `currentSeason` blocks and plan tiers; three 403s on data endpoints; tier→price coverage flags; full price list |
| `datasource_api_football_2026-07-29.json` | the 403 wall (7 URLs); the unauth API error body; this project's own season-blindness evidence with file and line citations; secondary pricing, labelled as such |
| `datasource_statsbomb_open_data_2026-07-29.json` | complete competition/season index (80 rows) and target-league availability; README terms quote |
| `datasource_football_data_couk_2026-07-29.json` | CSV headers and season lists for `E0`, `SP1`, `MEX`, `USA`; the main-league vs extra-league asymmetry |
| `datasource_candidates_surveyed_2026-07-29.json` | the 12 candidates not shortlisted, each with the probe or quote that eliminated it — including SofaScore's double 403, apifootball.com's 200-on-auth-failure, and TheStatsAPI's unstated provenance |

**Not archived, deliberately:** nothing was probed behind an account, no
terms were accepted, and no attempt was made to work around
api-football.com's or SofaScore's bot protection. Those gaps are labelled
UNVERIFIED above rather than filled in.
