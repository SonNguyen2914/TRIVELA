# The pick loop — brief for a dedicated session

Open a session in `~/dev/TRIVELA/backend` and paste §1. Everything else
here is context that session should read before its first slate.

---

## 1. The brief (paste this)

> **At every session start:** read `gh issue view 68 --comments` — the
> coordination channel. Its ground rules and dispatch protocol bind you;
> pick up the lowest open TASK-n ORDER without a REPORT. Before finishing
> any session, schedule yourself a wake 60 minutes out to re-check it.

> Your whole function is the pick loop: **brief → lock → score → repeat.**
>
> You are an INTERFACE, NOT A STORE. Every pick is written to
> `PersonalBet` through the API; every score is read out of the scorer.
> Never compute a result yourself, never hold state that would be lost if
> you were deleted. Test yourself constantly: *if this session vanished,
> what is lost?* The only acceptable answer is "the conversation."
>
> You do not tell the user what to bet. You surface what is known —
> including what is ABSENT and why — and they decide. No code path here
> places an order.
>
> There is no established edge in this platform. MLS's shadow approval is
> +0.0272 with a CI crossing zero; friendlies beat a coin flip narrowly
> and the market's own accuracy there is unmeasured; the totals family is
> measurably an echo of the book. Do not imply an edge exists. A gap
> between our number and Kalshi's is a DISAGREEMENT, not a mispricing.
>
> Before trusting any prior report about this repo, verify against
> `origin/main`. On 2026-07-31, five of seven handoffs had a materially
> stale premise, and two would have caused damage if acted on.

---

## 2. Why the session must not be the record

On 2026-07-31 seven background sessions were deleted in one evening. The
ones safe to delete were exactly those whose work was committed; the one
that had never been pushed would have taken its entire investigation with
it, unreachable and eventually garbage-collected.

A pick that lives in a chat log is not a record. It has no content hash,
nothing stops it being re-read more charitably later, and it cannot be
scored mechanically. `PersonalBet` already solves this properly — read it
in `src/live/models.py` before building anything, because it is better
than you would expect:

- `status` — `considered | taken | passed | void`
- `price_basis` — `observed_quote | stated_only`, and a stated-only entry
  "is recorded honestly and **counted nowhere**"
- `model_probability` FROZEN at record time, with the `prediction_run_id`
  it came from, so a later re-run cannot change what was recorded
- resolutions are IMMUTABLE: a mistaken entry is corrected by a NEW row
  citing the old one. The record of the mistake is part of the record.

## 3. What to build, in order

**Phase 1 — the decision sheet.** One read-only endpoint per fixture
returning everything known at request time: model 1X2 with run id,
de-vigged Kalshi book, strength read, market-vs-read, form, xG, team
news. A NAMED REASON wherever data is absent, never a blank. **No
recommendation field.** No migration, no model change — safe to deploy
whenever.

**Phase 2 — the scorer.** Over settled journal rows, in this order:

1. **CLV** (closing-line value) — did the entry price beat the final
   pre-kickoff price? This is the primary metric.
2. **Calibration** — Brier, always against the MARKET's Brier on the same
   fixtures. A number with no baseline says nothing.
3. **P&L** — reported LAST, and never without a confidence interval.

Check first whether the **T-10 lock bundles already carry the closing
book**. They are frozen pre-kickoff and `/api/ready` shows them
populated. If they do, CLV needs no new capture and Phase 2 collapses to
just the scorer.

**Phase 3 — learning.** NOT buildable on demand; it needs a corpus of
slates. Do not write analysis that has nothing to analyse.

## 4. Why P&L is the wrong thing to learn from

Over a few dozen bets P&L is almost entirely variance. A prior analysis
found a +48% day sitting at the **98.2nd percentile of its own
distribution**, on a slate where home teams won 73.3% against a long-run
45–50% and the day ran 2.27 goals against an implied 2.8. Tuning toward
profit at that sample size fits noise, and gets worse while feeling
better.

CLV converges far faster: it is measurable on every bet including losers,
and a bettor who consistently beats the close has an edge whether or not
this month was profitable.

**Enforce a sample floor.** Refuse to draw a lesson from a slate. Every
conclusion carries its CI or is not stated.

## 5. What legitimately persists in the session

Preferences only — sizing, risk tolerance, which markets the user
actually plays, what they want flagged. Small, stable, belongs in the
memory namespace. Note that the namespace loaded from the Desktop path
marks itself a STALE COPY superseded by `~/dev/TRIVELA`'s; write to the
authoritative one.

## 6. Standing constraints

- Money stays LOCKED. `REAL_MONEY_SIGNALS_ENABLED=false`. No code path
  may enable it.
- No code path places an order. Humans bet on the exchange.
- Never rewrite historical evidence to improve a result.
- Never silently convert missing evidence into confidence.
- Any backend deploy disarms the runtime `approved_for_shadow` flag on
  every plane — as does a plain container restart. Check `/api/ready`,
  NOT `/api/*/approval`; the decision persists and stays green either way.

---

## 7. The write path, and what actually blocks it

Written after the 2026-08-01 slate, where **fourteen fixtures were
analysed and zero were recorded**. Eleven then became permanently
unrecordable. Nothing about that failure was conceptual — the session had
the analysis hours early — so the mechanics belong here rather than being
rediscovered.

### The flow

One briefing call supplies everything the write needs:

```text
GET  /api/mls/briefing/{espn_event_id}
       -> fixture_id                                 (INTERNAL id)
       -> market_frozen_t10.contracts[].market_quote_id
       -> market_persisted.quotes[].market_quote_id
POST /api/admin/mls/journal/view      X-Journal-Token
POST /api/admin/mls/journal/resolve   -> taken | passed
```

`scripts/journal_pick.py` wraps it. `show` needs no credential:

```bash
python scripts/journal_pick.py show 761696          # ids + the clock
python scripts/journal_pick.py record 761696 home_win --rationale "..."
python scripts/journal_pick.py resolve 1234 taken
```

### Four ways a write is silently worthless

Each of these returns a stored row and looks like success:

- **`fixture_id` is not the ESPN event id.** `record_view` takes the
  internal `fixture.id` (761696 → 431). The briefing hands it over; there
  is no other public route to it.
- **After kickoff the row is `void`** (`journal.py`, "after kickoff a
  view is not a forecast"). Kept, counted nowhere, irreversible.
- **No `market_quote_id` means `stated_only`**, which "is recorded
  honestly and counted nowhere". `record_view` NEVER resolves a quote for
  you — an omitted id is a downgrade, not a lookup.
- **A quote older than `JOURNAL_QUOTE_MAX_AGE_SECONDS` (900s)** is
  refused as the price basis. Same downgrade, different cause, and the
  reason lands on the row rather than in an absent field.

### Timing

**The T-10 sweep is the window.** It writes frozen quotes ~10 minutes
before kickoff, and `market_frozen_t10` then carries a `market_quote_id`
per contract with the model probability beside it. Recording there gets
`observed_quote` against the same book the platform's own evidence uses —
which is what CLV will later be measured from. Outside that window the
newest persisted quote ages past 900s and the entry stops counting.

A fixture days out has **no citable quote at all** — Kalshi has not
opened the book and no sweep has run. `show` says so rather than
implying an id exists.

### Credentials

`JOURNAL_TOKEN` only. It is deliberately not a general admin check
(`api/main.py`): a holder cannot arm a model plane. **Never put
`ADMIN_TOKEN` in a pick-loop session** — it activates approvals. An unset
`JOURNAL_TOKEN` grants nothing and never falls back.

---

## 8. The sharp anchor — run it yourself

Every disagreement this platform publishes is measured against ONE
market. `scripts/measure_sharp_anchor.py` puts a second, independently
priced book beside it, so "Kalshi is soft here" and "we are wrong here"
stop being the same observation. It compares two PRICES; neither has
been scored against outcomes, so nothing it prints is an edge.

**The key is yours to create** — free tier, the-odds-api.com. Store it
so the script can find it without it ever reaching a chat log or a diff:

```bash
printf '%s' 'YOUR_KEY' > ~/.odds_api_key && chmod 600 ~/.odds_api_key
```

A group- or world-readable file is refused rather than read. `600` is
checked, not assumed.

```bash
# what your key can actually see, and how each competition resolves.
# Free — the sports list costs no odds quota.
.venv/bin/python scripts/measure_sharp_anchor.py --list-sports

# one slate, archived with the date it was measured
.venv/bin/python scripts/measure_sharp_anchor.py \
    --out research_archive/sharp_anchor_$(date -u +%F).json
```

Without a key it exits `2` with a named refusal and produces no
document. That is the whole point: a measurement script that emits a
plausible archive with nothing behind it is worse than one that stops.

Each region requested multiplies the quota cost, so `--regions us` is
the cheap probe and the default `us,uk,eu` is the real one. If a
competition resolves to `sport_key_ambiguous` or `sport_not_listed`,
that is recorded and skipped — pin a mapping you have verified with
`--sport-map`, never a guess.

**Read `unmatched` before reading the ranking.** Our fixture list carries
whole seasons and the anchor lists about a week, so most rows legitimately
have no counterpart. The 2026-08-04 run bridged 49 Argentine rows out of
33 anchor events — arithmetically impossible, and it put two false
attaches at the top of the divergence ranking. Since then a bridge needs
all three of: both clubs matching on tokens, kickoffs within
`KICKOFF_WINDOW_HOURS` (36) of each other, and no other fixture claiming
the same anchor event. `bridged` counts on each competition should now
never exceed `anchor_events` — if one does, that is a bug, not a slate.
