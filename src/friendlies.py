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

Fixture->book matching keeps the MLS title/date approach but AMBIGUITY
FAILS EXPLICIT: when more than one same-day Kalshi title matches a
fixture's names, the result is an "ambiguous" status with the candidate
tickers, never a silently-picked book. First-match-wins on team names
has burned this repo before, and the friendlies universe (Real Madrid
vs Real Madrid Castilla on neighbouring days, B-teams, unbounded club
names) is where that class of bug lives. There is no alias table by
design: the club universe here is unbounded, so "unmapped" is the
honest state, not a gap to paper over with guesses.
"""
from __future__ import annotations

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


def _cached(key: str, ttl: float, fetch):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = fetch()
    if data is not None:               # never cache a failed answer
        _cache[key] = (now, data)
        return data
    return hit[1] if hit else None     # stale beats nothing


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


def _game_events(max_pages: int = 3) -> list[dict]:
    """The KXCLUBFGAME event list, PAGED — one probe returned 200 events
    with a continuation cursor, so a single call silently loses
    fixtures. Pages until the cursor runs out or `max_pages`, 120s
    cache. NO status filter (the MLS lesson: an in-play fixture's event
    stops reporting "open" while its markets keep trading as "active");
    tradability is judged per MARKET, at market-fetch time."""
    def fetch():
        events: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {"series_ticker": KALSHI_CLUBF_GAME, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = _get_json(f"{KALSHI_BASE}/events", params)
            if d is None:
                return events or None   # partial beats nothing; None if page 1 fails
            events.extend(d.get("events") or [])
            cursor = d.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)             # burst-throttle (Kalshi 429s)
        return events
    return _cached("events", 120, fetch) or []


def event_markets(event_ticker: str) -> list[dict]:
    """One event's tradeable markets, 15s cache — cheap enough for the
    match page's poll to ride."""
    def fetch():
        md = _get_json(f"{KALSHI_BASE}/markets",
                       {"event_ticker": event_ticker, "limit": 50})
        if md is None:
            return None
        return [m for m in (md.get("markets") or [])
                if m.get("status") in TRADEABLE]
    return _cached(f"mkts:{event_ticker}", 15, fetch) or []


def listed_events_summary() -> dict:
    """How much friendly market surface Kalshi lists, WITHOUT fetching
    any order books: {count, truncated, by_date}. `count` is a lower
    bound whenever `truncated` is true (the pager hit its cap). The full
    per-event detector work belongs to the market hunter, not here."""
    events = _game_events()
    by_date: dict[str, int] = {}
    for ev in events:
        d = _ticker_et_date(ev.get("event_ticker") or "")
        if d:
            by_date[d] = by_date.get(d, 0) + 1
    # truncated = the last page could have had a continuation; cheapest
    # honest signal is "a multiple of a full page" — a 600-event answer
    # under a 3-page cap may have more behind it
    return {"count": len(events),
            "truncated": len(events) >= 3 * 200,
            "by_date": dict(sorted(by_date.items()))}


# --- fixture <-> Kalshi book matching (ambiguity fails explicit) -----------

def _norm_name(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").strip()


def _side_matches(kalshi_side: str, espn_name: str) -> bool:
    """Substring match after accent/case normalization. NO alias table
    by design: the friendlies club universe is unbounded, so a bridge
    dict can never be complete and a partial one invites guesses. A
    name substring matching cannot cross renders as an unmapped
    fixture, which is the honest state."""
    k, e = _norm_name(kalshi_side), _norm_name(espn_name)
    if not k or not e:
        return False
    return k in e


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


def match_fixture_book(fixture_date: str, home_name: str, away_name: str,
                       events: list[dict] | None = None) -> dict:
    """This fixture's KXCLUBFGAME book, or an explicit refusal.

    -> {"status", "book", "candidates"} where status is one of
       mapped           exactly one same-ET-date title matched both names
       unmapped         no listed event matched (the normal state for
                        most ESPN friendlies — and vice versa: Kalshi
                        lists far more friendlies than ESPN's bucket)
       ambiguous        MORE than one matched. Never resolved by taking
                        the first: with B-teams sharing name prefixes
                        ("Real Madrid" / "Real Madrid Castilla"), a
                        silent pick is exactly the bug that burned the
                        MLS layer. The candidates are returned so a
                        human can see what collided.
       no_open_markets  one event matched but nothing in it trades
    """
    want = _fixture_et_date(fixture_date)
    if events is None:
        events = _game_events()
    cands = []
    for ev in events:
        ticker = ev.get("event_ticker") or ""
        if _ticker_et_date(ticker) != want:
            continue
        title = ev.get("title") or ""
        if " vs " not in title:
            continue
        k_home, k_away = title.split(" vs ", 1)
        if _side_matches(k_home, home_name) and \
                _side_matches(k_away, away_name):
            cands.append({"event_ticker": ticker, "title": title,
                          "markets": ev.get("markets")})
    if not cands:
        return {"status": "unmapped", "book": None, "candidates": []}
    if len(cands) > 1:
        return {"status": "ambiguous", "book": None,
                "candidates": [c["event_ticker"] for c in cands]}
    only = cands[0]
    if only.get("markets") is None:     # not injected (live path): fetch
        book = parse_game_books(
            [only], {only["event_ticker"]:
                     event_markets(only["event_ticker"])})[0]
    else:
        book = parse_game_books(
            [only], {only["event_ticker"]: only["markets"]})[0]
    if not book["markets"]:
        return {"status": "no_open_markets", "book": None,
                "candidates": [only["event_ticker"]]}
    return {"status": "mapped", "book": book,
            "candidates": [only["event_ticker"]]}


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
    ambiguity-refusing matcher; the other families share its ticker
    suffix ({YYMONDD}{HOME}{AWAY}), so that join is exact and needs no
    name resolution. 30s bundle cache.

    -> {"status", "candidates", "families": [{key,label,event_ticker,
        markets:[{ticker,label,yes_ask,yes_bid,status}]}]}
    """
    m = match_fixture_book(fixture_date, home_name, away_name)
    if m["status"] != "mapped":
        return {"status": m["status"], "candidates": m["candidates"],
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
            "families": families}


def daily_books(date: str | None = None) -> list[dict]:
    """The scoreboard bucket's fixtures joined to their game books —
    one row per ESPN fixture, every row carrying its explicit mapping
    status so the UI can say "no book found" or "ambiguous" in words
    instead of showing nothing."""
    out = []
    for f in scoreboard(date):
        m = match_fixture_book(f.get("date") or "",
                               (f.get("home") or {}).get("name") or "",
                               (f.get("away") or {}).get("name") or "")
        out.append({"fixture_id": f.get("id"),
                    "home": (f.get("home") or {}).get("name"),
                    "away": (f.get("away") or {}).get("name"),
                    "status": m["status"], "book": m["book"],
                    "candidates": m["candidates"]})
    return out
