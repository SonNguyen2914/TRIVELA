"""Club Friendlies data layer — a VIEWER surface, deliberately modelless.

Scope honesty, stated as a design fact rather than a roadmap: friendlies
get NO model, NO shadow plane, NO locks, NO database writes, NO scheduler
jobs — not "not yet", not dark, not pending. Rotation, arbitrary
substitutions (six-plus changes at half-time are routine) and low
motivation make friendly results structurally worthless as forecast
evidence, so this surface earns none of the evidence machinery and never
will. It is a read-only proxy + TTL cache for WATCHING the markets:
ESPN carries fixtures/scores/summaries, Kalshi carries the books. The
market hunter (feat-market-hunter) already scans KXCLUBFGAME for
structural findings; this module is its companion viewer, not a
competitor, and deliberately re-implements none of its detectors.

Standings do not exist for friendlies and no standings surface exists
here — there is no table to fabricate.

Provider ground truth (research_archive/friendlies_*_2026-07-28.json):
  - ESPN slug `club.friendly` serves live scoreboards, full summaries
    (lastFiveGames + seasonseries in the exact shapes src.mls parses)
    and a /teams payload with colors. `fifa.friendly` exists too but is
    INTERNATIONAL friendlies — out of scope for this club surface.
  - Kalshi series KXCLUBFGAME uses the exact KXMLSGAME grammar
    ({YYMONDD}{HOME}{AWAY} suffix, " vs " titles, team-code/TIE tails).
    200 events came back at probe limit=200 WITH a continuation cursor,
    so the count is a lower bound — the fetcher pages.
  - Sibling families verified LIVE with per-match listings and the MLS
    tail grammar: KXCLUBFTOTAL, KXCLUBFBTTS, KXCLUBFSPREAD. Probed and
    absent (404): SCORE, TEAMTOTAL, FTTS, 1H, MOV.

The PURE PARSERS are imported from src.mls rather than re-written: they
are league-neutral and each embodies a provider-drift lesson learned the
expensive way (winner-first score strings, the seasonseries rename).
Re-implementing them here would fork those fixes. The CACHE is this
module's own — sharing src.mls's dict would cross-serve leagues on
identical keys ("sb:<date>", "team_colors", ...), a known trap.

Two fail-closed rules added in the 2026-07-29 review round (P0-1/P0-2):

IDENTITY IS EXACT OR IT DOES NOT PRICE. Substring containment is banned:
it attached an ESPN "Real Madrid Castilla" fixture to the SENIOR club's
book with full confidence — in friendlies, B/reserve/academy sides make
that a live hazard, not a corner case. A side matches only by
normalized-name equality, token-set equality (generic club suffixes
stripped), or an evidence-backed alias (each entry verified against the
ARCHIVED Kalshi titles x ESPN buckets, both committed). A lone candidate
that matches only loosely (token subset) is `unresolved_name`: shown,
never priced. More than one candidate at the deciding tier is
`ambiguous`: shown, never guessed.

MISSING EVIDENCE IS NEVER RENDERED AS AUTHORITY. A registry page that
failed (or a pager that hit its cap with a cursor left) makes absence
claims impossible, so a no-candidate fixture is `registry_incomplete`,
not "unmapped". A market fetch that failed is `unavailable`, never "no
open markets". A stale cache may serve, but visibly: the book carries
{"state": "stale", "age_seconds": n}.

MARKET-IMPLIED PROBABILITIES (2026-07-29) do not weaken the modelless
scope line above — they are arithmetic on the exchange's own prices,
authored by the book and not by this platform. See the
`market_implied` section for why a forecast is structurally
unavailable here and why every name in that section refuses the words
"prediction", "forecast", "model" and "edge".
"""
from __future__ import annotations

import threading
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import requests

# league-neutral parsers, imported NOT copied (see module docstring)
from src.mls import (parse_event, parse_game_books, parse_summary,
                     parse_team_colors)
from src.mls import scoreline_disagreements as _mls_scoreline_audit
# The EXACT fee module, imported and never re-implemented: a second copy
# of the fee formula is precisely how the binary-float ceil that
# overcharged by a cent at some prices got written in the first place
# (V9.1 eval F3). Decimal in, Decimal out, no floats anywhere near it.
from src.live.paper import FEE_POLICY, order_fee_dollars

ESPN_LEAGUE = "club.friendly"          # fifa.friendly = internationals; out of scope
ESPN_BASE = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{ESPN_LEAGUE}"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

KALSHI_CLUBF_GAME = "KXCLUBFGAME"

# The honest framing, served with every response so no consumer can
# forget what this surface is. Wording is load-bearing: no model runs
# here and none is planned — never "coming soon".
FRAMING = ("Club friendlies are a market-watching surface: live scores and "
           "Kalshi books, nothing else. No model runs here and none is "
           "planned — rotation, arbitrary substitutions and low motivation "
           "make friendlies structurally worthless as forecast evidence.")

# this module keeps its OWN cache — sharing src.mls's dict would collide
# on identical keys ("sb:<date>", "team_colors", ...)
_cache: dict[str, tuple[float, object]] = {}

# Single-flight guards: N concurrent COLD requests for one key must
# produce ONE provider fetch, not N (review 2026-07-29). Sync endpoints
# run in a threadpool, so these are real races. Per-key locks; the
# registry lock guards the lock dict itself.
_flight_locks: dict[str, threading.Lock] = {}
_flight_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _flight_guard:
        return _flight_locks.setdefault(key, threading.Lock())


def _cached(key: str, ttl: float, fetch):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _lock_for(key):
        # another flight may have landed while we waited
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        data = fetch()
        if data is not None:               # never cache a failed answer
            _cache[key] = (time.monotonic(), data)
            return data
        return hit[1] if hit else None     # stale beats nothing — but see
                                           # _cached_state for surfaces that
                                           # must SAY they are stale


def _cached_state(key: str, ttl: float, fetch):
    """Fail-closed cache read for surfaces where freshness must be
    VISIBLE (P0-2): -> (data|None, meta) with meta one of
      {"state": "fresh", "age_seconds": 0}
      {"state": "stale", "age_seconds": n}   fetch failed, warm cache served
      {"state": "missing", "age_seconds": None}  fetch failed, nothing at all
    Single-flighted like _cached."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1], {"state": "fresh", "age_seconds": 0}
    with _lock_for(key):
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1], {"state": "fresh", "age_seconds": 0}
        data = fetch()
        if data is not None:
            _cache[key] = (time.monotonic(), data)
            return data, {"state": "fresh", "age_seconds": 0}
        if hit:
            age = int(time.monotonic() - hit[0])
            return hit[1], {"state": "stale", "age_seconds": age}
        return None, {"state": "missing", "age_seconds": None}


def _get_json(url: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        print(f"[friendlies] fetch failed {url}: {exc}")
        return None


# --- ESPN fetchers ---------------------------------------------------------

def scoreboard(date: str | None = None) -> list[dict]:
    """Normalized fixtures for one ESPN bucket (YYYYMMDD; ESPN's default
    bucket when omitted — the heading is the frontend's to derive from
    the fixture dates, never to assert as "today"). 60s cache."""
    params = {"dates": date} if date else None

    def fetch():
        d = _get_json(f"{ESPN_BASE}/scoreboard", params)
        return ([parse_event(e) for e in d.get("events") or []]
                if d else None)
    return _cached(f"sb:{date or 'today'}", 60, fetch) or []


def schedule(days: int = 7) -> list[dict]:
    """The next `days` days of fixtures (today inclusive), flattened and
    kickoff-ordered. 300s cache per day-bucket."""
    from datetime import datetime, timedelta, timezone
    out: list[dict] = []
    today = datetime.now(timezone.utc)
    for i in range(max(1, min(days, 14))):
        day = (today + timedelta(days=i)).strftime("%Y%m%d")

        def fetch(day=day):
            d = _get_json(f"{ESPN_BASE}/scoreboard", {"dates": day})
            return ([parse_event(e) for e in d.get("events") or []]
                    if d else None)
        out.extend(_cached(f"sb:{day}", 300, fetch) or [])
    seen: set[str] = set()
    uniq = [f for f in out
            if f["id"] not in seen and not seen.add(f["id"])]
    uniq.sort(key=lambda f: f.get("date") or "")
    return uniq


def team_colors() -> dict[str, dict]:
    """Club signature colors (1h cache). Best-effort only: the
    club.friendly /teams payload carried 120 clubs on 2026-07-28, far
    fewer than play friendlies — a missing color is normal, not drift."""
    def fetch():
        d = _get_json(f"{ESPN_BASE}/teams")
        return parse_team_colors(d) if d else None
    return _cached("team_colors", 3600, fetch) or {}


def match_summary(event_id: str) -> dict | None:
    """One match's live stat page. 30s cache (it IS the live view).
    Friendlies vary per match: the archived Real Madrid summary carried
    an empty boxscore while PSV's was complete — parse_summary tolerates
    both, and the UI renders what exists."""
    def fetch():
        d = _get_json(f"{ESPN_BASE}/summary", {"event": event_id})
        if not d:
            return None
        out = parse_summary(d)
        colors = team_colors()
        for side in ("home", "away"):
            c = colors.get((out.get(side) or {}).get("abbrev") or "")
            if c:
                out[side]["color"] = c.get("color")
                out[side]["alt_color"] = c.get("alt")
        return out
    return _cached(f"sum:{event_id}", 30, fetch)


def scoreline_disagreements(summary: dict) -> list[dict]:
    """The derived-letter-vs-shown-scores audit, on club.friendly
    payloads (same contract as src.mls — verified in the archive)."""
    return _mls_scoreline_audit(summary)


# --- Kalshi fetchers -------------------------------------------------------

TRADEABLE = ("active", "open", "initialized")


def _game_events_state(max_pages: int = 3):
    """The KXCLUBFGAME event registry, PAGED, with its completeness on
    the record: -> ({"events", "complete", "truncated"}, cache_meta).

    complete=False when any page fetch failed — the partial list is
    KEPT (a fixture found in it still maps) but absence claims are
    downgraded to registry_incomplete downstream. truncated=True when
    the pager hit max_pages with a cursor still outstanding: same
    downstream consequence, distinguished for the census.

    An INCOMPLETE registry is never cached (the next request retries);
    a complete one caches for 120s. Cold path is single-flighted."""
    key = "events"
    ttl = 120.0
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1], {"state": "fresh", "age_seconds": 0}
    with _lock_for(key):
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1], {"state": "fresh", "age_seconds": 0}
        events: list[dict] = []
        complete = True
        cursor = None
        for _ in range(max_pages):
            params: dict = {"series_ticker": KALSHI_CLUBF_GAME, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = _get_json(f"{KALSHI_BASE}/events", params)
            if d is None:
                complete = False           # a page failed: registry partial
                cursor = None
                break
            events.extend(d.get("events") or [])
            cursor = d.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)                # burst-throttle (Kalshi 429s)
        truncated = bool(cursor)           # cap hit with more behind it
        reg = {"events": events, "complete": complete,
               "truncated": truncated}
        if complete:
            _cache[key] = (time.monotonic(), reg)
        return reg, {"state": "fresh", "age_seconds": 0}


def event_markets_state(event_ticker: str):
    """One event's tradeable markets WITH evidence state, 15s cache:
    -> (rows|None, meta). rows=[] means 'fetched, nothing trades';
    rows=None means 'could not fetch and no cache' — callers must say
    `unavailable`, never `no_open_markets` (P0-2). A warm cache served
    through a failed refetch comes back meta.state='stale' with age."""
    def fetch():
        md = _get_json(f"{KALSHI_BASE}/markets",
                       {"event_ticker": event_ticker, "limit": 50})
        if md is None:
            return None
        return [m for m in (md.get("markets") or [])
                if m.get("status") in TRADEABLE]
    return _cached_state(f"mkts:{event_ticker}", 15, fetch)


def event_markets(event_ticker: str) -> list[dict]:
    """Markets-or-empty convenience for the family rows, where a missing
    family is simply omitted rather than statused."""
    rows, _meta = event_markets_state(event_ticker)
    return rows or []


def listed_events_summary() -> dict:
    """How much friendly market surface Kalshi lists, WITHOUT fetching
    any order books: {count, truncated, complete, by_date}. `count` is
    a floor whenever truncated or not complete — and it says which. The
    full per-event detector work belongs to the market hunter."""
    reg, _meta = _game_events_state()
    by_date: dict[str, int] = {}
    for ev in reg["events"]:
        d = _ticker_et_date(ev.get("event_ticker") or "")
        if d:
            by_date[d] = by_date.get(d, 0) + 1
    return {"count": len(reg["events"]),
            "truncated": reg["truncated"],
            "complete": reg["complete"],
            "by_date": dict(sorted(by_date.items()))}


# --- fixture <-> Kalshi book matching (fail-closed identity) ---------------

# Generic club-form suffixes that carry no identity: stripping them lets
# "Sevilla" == "Sevilla FC" without letting "Real Madrid" == "Real
# Madrid Castilla" ("castilla" is NOT in this set, deliberately — nor is
# "b", "ii", or any reserve/academy marker).
_GENERIC_TOKENS = {"fc", "cf", "sc", "cd", "ud", "ac", "afc", "cfc",
                   "cp", "club"}

# Kalshi title -> ESPN displayName bridges. EVIDENCE-BACKED ONLY: each
# entry is a pair observed in the committed 2026-07-28 archives (Kalshi
# event titles x ESPN scoreboard buckets, same fixtures):
#   "Atletico vs Getafe"  x ESPN "Atlético Madrid vs Getafe"  (Jul 29)
#   "Cerezo vs Dortmund"  x ESPN "Cerezo Osaka vs Borussia Dortmund"
# Never add an entry from intuition — an unverified bridge is exactly
# the guess the unresolved_name state exists to prevent.
_KALSHI_ALIASES = {
    "atletico": "atletico madrid",
    "cerezo": "cerezo osaka",
    "dortmund": "borussia dortmund",
}


def _norm_name(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").strip()


def _identity_tokens(s: str) -> frozenset[str]:
    return frozenset(t for t in _norm_name(s).split()
                     if t not in _GENERIC_TOKENS)


def _side_identity(kalshi_side: str, espn_name: str) -> str:
    """-> "exact" | "loose" | "none". Exact = same club, safe to price:
    normalized equality, token-set equality (generic suffixes ignored),
    or an archived-evidence alias. Loose = related-name overlap (token
    subset either way) — enough to SHOW as a candidate, never enough to
    price: this is where "Real Madrid"/"Real Madrid Castilla" and
    "Barcelona"/"Barcelona B" live. Substring containment is banned
    outright (P0-1)."""
    k, e = _norm_name(kalshi_side), _norm_name(espn_name)
    if not k or not e:
        return "none"
    if k == e:
        return "exact"
    kt, et = _identity_tokens(kalshi_side), _identity_tokens(espn_name)
    if kt and kt == et:
        return "exact"
    alias = _KALSHI_ALIASES.get(k)
    if alias and (alias == e or _identity_tokens(alias) == et):
        return "exact"
    if kt and et and (kt <= et or et <= kt):
        return "loose"
    return "none"


def _ticker_et_date(event_ticker: str) -> str | None:
    """KXCLUBFGAME-26JUL29LFCWRE -> '26JUL29' (Kalshi dates are
    US-Eastern)."""
    import re
    m = re.match(rf"{KALSHI_CLUBF_GAME}-(\d{{2}}[A-Z]{{3}}\d{{2}})",
                 event_ticker or "")
    return m.group(1) if m else None


def _fixture_et_date(iso_date: str) -> str | None:
    """ESPN UTC kickoff -> the Kalshi-style US-Eastern date segment.
    Real wall-clock Eastern time via IANA zone, never a fixed offset."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    et = dt.astimezone(ZoneInfo("America/New_York"))
    return et.strftime("%y%b%d").upper()


def _book_from(cand: dict) -> tuple[dict | None, dict | None]:
    """One candidate's parsed book + freshness (None when fresh).
    Injected markets (tests) count as fresh; the live path reads
    event_markets_state and propagates unavailability as (None, meta)."""
    if cand.get("markets") is not None:
        rows, meta = cand["markets"], {"state": "fresh", "age_seconds": 0}
    else:
        rows, meta = event_markets_state(cand["event_ticker"])
        if rows is None:
            return None, meta              # could not look — unavailable
    book = parse_game_books(
        [cand], {cand["event_ticker"]: rows})[0]
    return book, (meta if meta["state"] == "stale" else None)


def match_fixture_book(fixture_date: str, home_name: str, away_name: str,
                       events: list[dict] | None = None,
                       registry_complete: bool = True) -> dict:
    """This fixture's KXCLUBFGAME book, or an explicit refusal.

    -> {"status", "book", "candidates", "freshness"} where status is
       mapped              exactly one same-ET-date title matched both
                           names EXACTLY (equality/token-set/archived
                           alias — never substring)
       unmapped            nothing matched, and the registry was
                           complete enough to say so
       ambiguous           more than one candidate at the deciding
                           tier; the candidates are returned, none is
                           picked
       unresolved_name     exactly one candidate, but it matches only
                           loosely (token subset — the B-team /
                           reserve-side shape). Shown, never priced.
       no_open_markets     one exact match whose book was FETCHED and
                           holds nothing tradeable
       unavailable         one exact match whose book could not be
                           fetched and has no cache — 'we couldn't
                           look', which is not 'closed'
       registry_incomplete no candidate found, but the event registry
                           was partial (failed page or capped pager) —
                           'unmapped' would assert an absence the
                           evidence cannot support
    freshness is None for fresh answers, or {"state": "stale",
    "age_seconds": n} when a warm cache was served through a failed
    refetch — visibly stale, never silently current."""
    want = _fixture_et_date(fixture_date)
    if events is None:
        reg, _meta = _game_events_state()
        events = reg["events"]
        registry_complete = reg["complete"] and not reg["truncated"]

    exact: list[dict] = []
    loose: list[dict] = []
    for ev in events:
        ticker = ev.get("event_ticker") or ""
        if _ticker_et_date(ticker) != want:
            continue
        title = ev.get("title") or ""
        if " vs " not in title:
            continue
        k_home, k_away = title.split(" vs ", 1)
        h_id = _side_identity(k_home, home_name)
        a_id = _side_identity(k_away, away_name)
        if h_id == "none" or a_id == "none":
            continue
        cand = {"event_ticker": ticker, "title": title,
                "markets": ev.get("markets")}
        if h_id == "exact" and a_id == "exact":
            exact.append(cand)
        else:
            loose.append(cand)

    def _refuse(status, cands):
        return {"status": status, "book": None,
                "candidates": [c["event_ticker"] for c in cands],
                "freshness": None}

    if len(exact) > 1:
        return _refuse("ambiguous", exact)
    if len(exact) == 1:
        only = exact[0]
        book, fresh = _book_from(only)
        if book is None:
            return _refuse("unavailable", [only])
        if not book["markets"]:
            return _refuse("no_open_markets", [only])
        return {"status": "mapped", "book": book,
                "candidates": [only["event_ticker"]],
                "freshness": fresh}
    if len(loose) > 1:
        return _refuse("ambiguous", loose)
    if len(loose) == 1:
        return _refuse("unresolved_name", loose)
    if not registry_complete:
        return _refuse("registry_incomplete", [])
    return {"status": "unmapped", "book": None, "candidates": [],
            "freshness": None}


# --- market-implied probabilities (arithmetic, NOT a forecast) -------------
#
# WHY THIS EXISTS, and why every name here refuses forecast vocabulary.
#
# Son asked for model predictions on friendlies. This platform cannot
# produce them, and the reason is structural rather than a matter of
# effort: team ratings are competition-scoped — `model_mls.fit` filters
# `competition_slug="mls-2026"` and MLS is the only approved model —
# while friendlies span the global club universe (J-League, Bundesliga,
# Championship, League Two). ZERO of those clubs hold a rating anywhere
# in this system, so there is no quantity to forecast WITH. A forecast is
# therefore *unavailable*, not withheld and not pending.
#
# What IS available is arithmetic on prices somebody else set. Take the
# exchange's own YES asks across a full 3-way partition, remove the
# overround proportionally, and what falls out is the probability set
# implied BY THE BOOK. It claims nothing beyond "this is what the book
# prices", which is exactly what makes it honest on a surface with no
# model: this platform is not the author of these numbers, and no field,
# string or comment in this section may let a reader think otherwise.
#
# Consequences that are load-bearing, not stylistic:
#   - the vocabulary is "market-implied, vig stripped". Never
#     "prediction", "forecast", "model", "edge", nor "fair value"
#     unqualified. Tests assert the absence of those words.
#   - the ASK side is the basis, because the ask is what a reader would
#     actually pay. The BID side is reported separately so the spread —
#     the width of the market's own uncertainty — is visible beside the
#     number instead of being averaged away into a clean-looking mid.
#   - the RAW ask sum ships with the answer. A percentage that has had
#     6% of vig quietly removed should say so; the reader can see how
#     much of the number was the exchange's margin.
#   - a raw sum BELOW $1.00 is not a probability set at all, it is a
#     structural anomaly, and normalizing it upward would launder an
#     arbitrage-shaped book into a comfortable-looking forecast. It comes
#     back flagged with NO probabilities. The market hunter's
#     SUM_BELOW_ONE finding class owns that observation (net of fees,
#     with liquidity guards); this section deliberately re-detects
#     nothing and asserts no margin.
#   - a partial book is INCOMPLETE. Renormalizing two legs into a 3-way
#     would invent a draw price out of nothing.
#
# Exact Decimal throughout. Fees come from the exact fee module.

IMPLIED_BASIS = "yes_ask"
IMPLIED_METHOD = "proportional_normalization"

# The Kalshi taker fee is computed once on a WHOLE order and rounded up
# to the centicent, so a "fee per contract" is not a well-defined number
# — it depends on order size. The fee-adjusted breakeven is therefore
# stated for an explicit reference order, and the reference is declared
# rather than assumed. This is a UNIT for expressing the fee, NOT a
# suggested size: money is locked, there is no order path here, and
# nothing on this surface sizes anything.
IMPLIED_FEE_REFERENCE_CONTRACTS = 100

# Reported probabilities are quantized to a micro-probability. Exact
# Decimal division does not terminate for most books, so a residue is
# unavoidable; it is assigned to the LARGEST leg (first maximal ask in
# book order — deterministic) so that the published set sums to exactly
# $1. A set that does not sum to 1 is not a distribution, and burying
# the residue somewhere unstated would be the dishonest fix.
IMPLIED_PRECISION = Decimal("0.000001")
IMPLIED_RESIDUE_RULE = "largest_leg_absorbs_rounding_residue"

IMPLIED_ATTRIBUTION = (
    "Market-implied probabilities: the exchange's own YES asks with the "
    "overround removed proportionally. This is arithmetic on observed "
    "Kalshi prices — the market's view, not this platform's. No model "
    "runs on friendlies and none is planned.")

_DRAW_TAILS = ("TIE", "DRAW")


def _read_clock() -> str:
    """OUR read clock, for `captured_at`. Deliberately not the
    exchange's: Kalshi publishes no quote timestamp (see
    `market_implied`), so the only honest instant available is the one
    at which this process assembled the answer."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _is_draw_leg(row: dict) -> bool:
    """The draw leg of a KXCLUBF/KXMLS-grammar 3-way book. Ticker tail
    first (the grammar's own marker, same convention as the market
    hunter's `_tie_leg`), label as a fallback for rows whose ticker did
    not survive a provider rename."""
    tail = (row.get("ticker") or "").rsplit("-", 1)[-1].upper()
    if tail in _DRAW_TAILS:
        return True
    return _norm_name(row.get("label") or "") in ("tie", "draw")


def _dollars(row: dict, field: str) -> Decimal | None:
    """Exact dollar price of one side of one leg, or None when that side
    is EMPTY. Only a value strictly inside (0, 1) counts: Kalshi reports
    0 and 1 as empty-side placeholders, and treating a placeholder 0 as
    "this outcome is priced at zero" would convert a missing quote into
    certainty — the one thing this module exists not to do. Same rule as
    the market hunter's `_price`."""
    v = row.get(field)
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d <= 0 or d >= 1:
        return None
    return d


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def _implied_freshness(freshness: dict | None) -> dict:
    """The book's freshness, restated so the implied block can never be
    read as current when it isn't. `match_fixture_book` threads None for
    a fresh answer and {"state": "stale", "age_seconds": n} for a warm
    cache served through a failed refetch; a "missing" book cannot reach
    here (it becomes status `unavailable` upstream) but is mapped anyway
    rather than defaulted to fresh."""
    state = (freshness or {}).get("state") or "fresh"
    if state == "missing":
        return {"state": "unavailable", "age_seconds": None}
    if state == "stale":
        age = (freshness or {}).get("age_seconds")
        return {"state": "stale",
                "age_seconds": age if isinstance(age, int) else None}
    return {"state": "fresh", "age_seconds": 0}


def _implied_legs(rows: list[dict]) -> list[dict]:
    """Per-leg ask/bid/spread and the fee-adjusted breakeven. No
    probabilities here — those only exist once the whole partition is
    proven priced."""
    out = []
    for r in rows:
        ask = _dollars(r, "yes_ask")
        bid = _dollars(r, "yes_bid")
        fee = cost = breakeven = None
        if ask is not None:
            c = Decimal(IMPLIED_FEE_REFERENCE_CONTRACTS)
            fee = order_fee_dollars(ask, IMPLIED_FEE_REFERENCE_CONTRACTS)
            # what the reference order costs, and the probability at
            # which it breaks even net of that fee: (C*ask + fee) / C.
            # Every step exact — Decimal division by a power of ten is
            # exact, so the breakeven is not a rounded figure.
            cost = ask * c + fee
            breakeven = cost / c
        out.append({
            "ticker": r.get("ticker"),
            "label": r.get("label"),
            "is_draw": _is_draw_leg(r),
            "ask_dollars": _s(ask),
            "bid_dollars": _s(bid),
            "spread_dollars": _s(None if (ask is None or bid is None)
                                 else ask - bid),
            "implied_probability": None,       # set only when priced
            "fee_dollars": _s(fee),
            "cost_dollars": _s(cost),
            "breakeven_probability": _s(breakeven),
        })
    return out


def market_implied(book: dict | None, freshness: dict | None = None,
                   captured_at: str | None = None) -> dict:
    """The probabilities a friendly's 3-way Kalshi book IMPLIES, with the
    vig removed proportionally — arithmetic on observed prices, never a
    forecast (see the section comment above for why a forecast is
    structurally unavailable on this surface).

    PURE: dict in, dict out. No network, no cache, no clock — the caller
    supplies `captured_at` because the only capture clock available is
    OURS. Kalshi publishes no quote timestamp: `updated_time` is the
    market *definition* clock and has been observed ~30h stale on
    actively trading markets, so any claim of an exchange-side quote
    instant would be fabricated. `captured_at` is therefore labelled as
    this platform's read time, and staleness is carried separately.

    -> {"state", "reason", ...} where state is
       priced          exactly 3 legs, exactly one draw leg, every leg
                       with a real ask, and the asks sum to >= $1.00.
                       Each leg carries `implied_probability`; the set
                       sums to exactly 1.
       sum_below_one   a fully priced 3-way whose asks sum BELOW $1.00.
                       Reported with the raw sum and the shortfall and
                       NO probability set: normalizing it upward would
                       dress a structural anomaly as a forecast. The
                       hunter's SUM_BELOW_ONE class owns the finding;
                       no margin is asserted here.
       incomplete      the arithmetic's preconditions are not met, with
                       `reason` one of
                         no_book           nothing to compute from
                         book_unavailable  the fetch failed upstream
                         leg_count         not exactly 3 legs
                         partition_unproven not exactly one draw leg, so
                                           the legs are not provably a
                                           mutually-exclusive partition
                         missing_ask       at least one leg has no ask
                         normalization_failed  unreachable by
                                           construction; refuses to
                                           publish a set that does not
                                           sum to 1 rather than assert
                       In every incomplete state `implied_probability` is
                       None on every leg. A 2-way is NEVER renormalized
                       into a 3-way: that invents a draw price.

    `sum_ask_dollars` / `sum_bid_dollars` / `overround_dollars` ship with
    every computable answer so a reader can see how much of the book was
    the exchange's margin, and how wide the two sides sat.
    `overround_dollars` is SIGNED (sum - 1), so the anomalous book shows
    a negative one and `shortfall_dollars` names the same gap positively
    — neither is silently clamped to zero."""
    fresh = _implied_freshness(freshness)
    base = {
        "state": "incomplete",
        "reason": "no_book",
        "basis": IMPLIED_BASIS,
        "method": None,
        "legs": [],
        "sum_ask_dollars": None,
        "sum_bid_dollars": None,
        "overround_dollars": None,
        "shortfall_dollars": None,
        "sum_implied_probability": None,
        "residue_rule": None,
        "fee_reference_contracts": IMPLIED_FEE_REFERENCE_CONTRACTS,
        "fee_policy_version": FEE_POLICY["version"],
        "freshness": fresh,
        "captured_at": captured_at,
        "attribution": IMPLIED_ATTRIBUTION,
    }
    if fresh["state"] == "unavailable":
        return {**base, "reason": "book_unavailable"}
    rows = (book or {}).get("markets") or []
    if not rows:
        return base

    legs = _implied_legs(rows)
    asks = [None if leg["ask_dollars"] is None else Decimal(leg["ask_dollars"])
            for leg in legs]
    bids = [None if leg["bid_dollars"] is None else Decimal(leg["bid_dollars"])
            for leg in legs]
    known_asks = [a for a in asks if a is not None]
    known_bids = [b for b in bids if b is not None]
    out = {**base, "legs": legs,
           "sum_ask_dollars": (_s(sum(known_asks))
                               if len(known_asks) == len(asks) else None),
           "sum_bid_dollars": (_s(sum(known_bids))
                               if len(known_bids) == len(bids) else None)}

    # Preconditions, each refused separately so the caller can SAY which
    # one failed instead of showing a confident nothing.
    if len(rows) != 3:
        return {**out, "reason": "leg_count"}
    if sum(1 for leg in legs if leg["is_draw"]) != 1:
        return {**out, "reason": "partition_unproven"}
    if len(known_asks) != 3:
        return {**out, "reason": "missing_ask"}

    total = sum(known_asks)
    overround = total - Decimal(1)
    out = {**out, "overround_dollars": str(overround)}
    if total < Decimal(1):
        # NOT normalized, deliberately: see the section comment.
        return {**out, "state": "sum_below_one", "reason": "asks_sum_below_one",
                "shortfall_dollars": str(Decimal(1) - total)}

    probs = [(a / total).quantize(IMPLIED_PRECISION, rounding=ROUND_HALF_UP)
             for a in known_asks]
    residue = Decimal(1) - sum(probs)
    if residue != 0:
        # first maximal ask in book order — deterministic, and the
        # smallest relative distortion available
        i = max(range(len(known_asks)), key=lambda k: known_asks[k])
        probs[i] = probs[i] + residue
    if sum(probs) != Decimal(1) or any(p < 0 for p in probs):
        # Unreachable by construction, and fail-closed anyway rather
        # than asserted: a bare assert would 500 the whole markets route
        # for every fixture, and a set that does not sum to 1 (or that
        # went negative absorbing the residue) is not a probability set
        # — refusing to publish it is the only honest option.
        return {**out, "reason": "normalization_failed"}
    for leg, p in zip(legs, probs):
        leg["implied_probability"] = str(p)
    return {**out, "state": "priced", "reason": None,
            "method": IMPLIED_METHOD,
            "residue_rule": IMPLIED_RESIDUE_RULE,
            "sum_implied_probability": str(sum(probs))}


# --- per-match Kalshi families (all VERIFIED live 2026-07-28) --------------
# Rows are market-only BY DESIGN: no model_key field exists on this
# surface because no model does. See FRAMING.

MATCH_FAMILIES = [
    ("winner", KALSHI_CLUBF_GAME, "Winner · 3-way"),
    ("total", "KXCLUBFTOTAL", "Total goals"),
    ("btts", "KXCLUBFBTTS", "Both teams to score"),
    ("spread", "KXCLUBFSPREAD", "Spread"),
]


def find_all_books(fixture_date: str, home_name: str,
                   away_name: str) -> dict:
    """Every verified Kalshi market family for one fixture, or the
    explicit non-mapped status. The GAME event is located by the
    fail-closed matcher; the other families share its ticker suffix
    ({YYMONDD}{HOME}{AWAY}), so that join is exact and needs no name
    resolution. 30s bundle cache.

    -> {"status", "candidates", "freshness", "implied", "families":
        [{key,label,event_ticker, markets:[{ticker,label,yes_ask,yes_bid,
        status}]}]}

    `implied` is the ADDITIVE market-implied block for the 3-way winner
    book (see `market_implied`) — arithmetic on the exchange's asks, not
    a forecast. It is None when there is no book to compute from, which
    the `status` already explains in words. Market rows stay market-only:
    nothing is added to them, because a per-row field would be the first
    step towards a row shape that implies a model exists.
    """
    m = match_fixture_book(fixture_date, home_name, away_name)
    if m["status"] != "mapped":
        return {"status": m["status"], "candidates": m["candidates"],
                "freshness": m["freshness"], "implied": None,
                "families": []}
    game = m["book"]
    suffix = game["event_ticker"].split("-", 1)[1]

    def fetch():
        fams = [{"key": "winner", "label": "Winner · 3-way",
                 "event_ticker": game["event_ticker"],
                 "markets": game["markets"]}]
        for key, series, label in MATCH_FAMILIES:
            if series == KALSHI_CLUBF_GAME:
                continue
            ticker = f"{series}-{suffix}"
            ms = event_markets(ticker)
            time.sleep(0.1)              # burst-throttle (Kalshi 429s)
            rows = [{
                "ticker": mk.get("ticker"),
                "label": mk.get("yes_sub_title") or mk.get("title"),
                "yes_ask": mk.get("yes_ask_dollars"),
                "yes_bid": mk.get("yes_bid_dollars"),
                "status": mk.get("status"),
            } for mk in ms or []]
            if rows:
                fams.append({"key": key, "label": label,
                             "event_ticker": ticker, "markets": rows})
        return fams
    families = _cached(f"allbooks:{suffix}", 30, fetch) or []
    return {"status": "mapped", "candidates": m["candidates"],
            "freshness": m["freshness"], "families": families,
            "implied": market_implied(game, m["freshness"], _read_clock())}


def daily_books(date: str | None = None) -> list[dict]:
    """The scoreboard bucket's fixtures joined to their game books —
    one row per ESPN fixture, every row carrying its explicit mapping
    status AND freshness so the UI can say "couldn't look", "registry
    incomplete", "ambiguous" or "stale, {n}s old" in words instead of
    showing a confident nothing.

    Each row also carries the additive `implied` block (None where there
    is no book): the probabilities the book itself implies, vig stripped.
    Arithmetic on observed prices — see `market_implied`."""
    out = []
    clock = _read_clock()
    for f in scoreboard(date):
        m = match_fixture_book(f.get("date") or "",
                               (f.get("home") or {}).get("name") or "",
                               (f.get("away") or {}).get("name") or "")
        out.append({"fixture_id": f.get("id"),
                    "home": (f.get("home") or {}).get("name"),
                    "away": (f.get("away") or {}).get("name"),
                    "status": m["status"], "book": m["book"],
                    "candidates": m["candidates"],
                    "freshness": m["freshness"],
                    "implied": (market_implied(m["book"], m["freshness"], clock)
                                if m["book"] else None)})
    return out
