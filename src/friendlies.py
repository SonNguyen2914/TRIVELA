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
"""
from __future__ import annotations

import threading
import time

import requests

# league-neutral parsers, imported NOT copied (see module docstring)
from src.mls import (parse_event, parse_game_books, parse_summary,
                     parse_team_colors)
from src.mls import scoreline_disagreements as _mls_scoreline_audit

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

    -> {"status", "candidates", "freshness", "families": [{key,label,
        event_ticker, markets:[{ticker,label,yes_ask,yes_bid,status}]}]}
    """
    m = match_fixture_book(fixture_date, home_name, away_name)
    if m["status"] != "mapped":
        return {"status": m["status"], "candidates": m["candidates"],
                "freshness": m["freshness"], "families": []}
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
            "freshness": m["freshness"], "families": families}


def daily_books(date: str | None = None) -> list[dict]:
    """The scoreboard bucket's fixtures joined to their game books —
    one row per ESPN fixture, every row carrying its explicit mapping
    status AND freshness so the UI can say "couldn't look", "registry
    incomplete", "ambiguous" or "stale, {n}s old" in words instead of
    showing a confident nothing."""
    out = []
    for f in scoreboard(date):
        m = match_fixture_book(f.get("date") or "",
                               (f.get("home") or {}).get("name") or "",
                               (f.get("away") or {}).get("name") or "")
        out.append({"fixture_id": f.get("id"),
                    "home": (f.get("home") or {}).get("name"),
                    "away": (f.get("away") or {}).get("name"),
                    "status": m["status"], "book": m["book"],
                    "candidates": m["candidates"],
                    "freshness": m["freshness"]})
    return out
