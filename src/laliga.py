"""La Liga data layer — the third league surface (Jul 28, 2026).

Mirrors src/mls.py deliberately: same fetch->parse split, same TTL
cache discipline, same bid/ask book rules. The PURE parsers are
imported from src.mls rather than copied — they are league-neutral
functions of ESPN/Kalshi payload shapes (verified identical for esp.1,
see research_archive/laliga_provider_research_2026-07-28.json), and a
provider-drift fix must reach every league at once, not whichever copy
someone remembered to patch. Everything league-specific (URLs, series
tickers, aliases, the preseason-standings guard) lives HERE.

Verified ground truth (2026-07-28, archived):
  - ESPN slug esp.1 serves "Spanish LALIGA"; season 2026-27 starts
    Aug 15. The winner-first `score` string trap exists here too.
  - Kalshi series KXLALIGAGAME EXISTS (383 historical 2025-26 events)
    but had ZERO open events on probe day. All 12 per-match family
    series + the annual KXLALIGA futures series exist. Existence is
    verified; 2026-27 COVERAGE is not — the tickers stay config, and
    nothing renders until events actually open.
  - Preseason standings are NOT empty: ESPN serves 20 all-zero rows
    ranked 1..20. Rendering that is a fabricated table; standings()
    suppresses it and reports preseason instead.

Scope honesty: DATA layer only — no model, no suggestions, no
persistence. The live-plane wiring lives in src/live/laliga_live.py.
"""
from __future__ import annotations

import time

import requests

import config
from src.mls import (parse_event, parse_game_books, parse_standings,
                     parse_summary, parse_team_colors,
                     scoreline_disagreements)

__all__ = ["scoreboard", "schedule", "standings", "team_colors",
           "match_summary", "raw_summary", "game_books", "futures",
           "find_book", "find_all_books", "series_probe",
           "scoreline_disagreements"]

ESPN_LEAGUE = "esp.1"
ESPN_BASE = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
             f"{ESPN_LEAGUE}")
ESPN_STANDINGS = ("https://site.api.espn.com/apis/v2/sports/soccer/"
                  f"{ESPN_LEAGUE}/standings")
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi series come from config (the prefix is verified to exist,
# 2026-07-28, but coverage for 2026-27 is not — never hardcode a
# guessed ticker as fact). GAME anchors fixture mapping; the other
# families suffix-join to it exactly as in MLS.
SERIES_PREFIX = config.LALIGA_KALSHI_SERIES_PREFIX          # "KXLALIGA"
KALSHI_GAME = f"{SERIES_PREFIX}GAME"
KALSHI_FUTURES = config.LALIGA_KALSHI_FUTURES_SERIES        # "KXLALIGA"

# (key, series, label) — series EXISTENCE verified via the Kalshi
# series endpoint 2026-07-28 (family probe archived); market coverage
# for 2026-27 remains unverified until events open.
MATCH_FAMILIES = tuple(
    (key, f"{SERIES_PREFIX}{suffix}", label) for key, suffix, label in (
        ("winner", "GAME", "Winner · 3-way"),
        ("total", "TOTAL", "Total goals"),
        ("btts", "BTTS", "Both teams to score"),
        ("spread", "SPREAD", "Spread"),
        ("team_total", "TEAMTOTAL", "Team totals"),
        ("score", "SCORE", "Correct score"),
        ("ftts", "FTTS", "First team to score"),
        ("mov", "MOV", "Method of victory"),
        ("h1", "1H", "1st half · winner"),
        ("h1_total", "1HTOTAL", "1st half · total"),
        ("h1_spread", "1HSPREAD", "1st half · spread"),
        ("h1_btts", "1HBTTS", "1st half · BTTS"),
    ))

# Own cache, NEVER shared with src.mls: the two modules use identical
# key strings ("standings", "cup", "sb:today"), so sharing the dict
# would silently serve one league's payload under the other's key.
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
        print(f"[laliga] fetch failed {url}: {exc}")
        return None


# --- fetchers (ESPN) -------------------------------------------------------

def scoreboard(date: str | None = None) -> list[dict]:
    """Normalized fixtures for one day (YYYYMMDD; ESPN's default bucket
    when omitted — which is a MATCHDAY, not a calendar day). 60s cache."""
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


def _is_preseason(tables: list[dict]) -> bool:
    """True when every row of every table is an all-zero placeholder.

    ESPN's preseason standings are NOT empty: on 2026-07-28 esp.1
    served 20 rows, every stat 0, ranks a clean 1..20 (archived).
    Passed through the shared parser that renders as a plausible
    alphabetical table of zeros — a fabricated surface, which the
    frontend rules forbid. Zero games AND zero points AND zero goals
    is not a table any real matchday can produce (round one already
    awards points), so suppressing exactly that state can never hide
    a genuine standings payload."""
    rows = [r for t in tables for r in t.get("entries") or []]
    return bool(rows) and all(
        (r.get("played") or 0) == 0 and (r.get("points") or 0) == 0
        and (r.get("wins") or 0) == 0 and (r.get("goals_for") or 0) == 0
        for r in rows)


def standings_view(d: dict) -> dict:
    """Pure: raw ESPN standings payload -> the served standings state.
    Split from standings() so the preseason guard is testable against
    the archived provider bytes."""
    tables = parse_standings(d)
    if _is_preseason(tables):
        return {"tables": [], "preseason": True}
    return {"tables": tables, "preseason": False}


def standings() -> dict:
    """{"tables": [...], "preseason": bool}. `tables` is empty until
    the season produces a real table — the explicit empty state, never
    an all-zero fabrication. 300s cache."""
    def fetch():
        d = _get_json(ESPN_STANDINGS)
        return standings_view(d) if d is not None else None
    return _cached("standings", 300, fetch) or {"tables": [],
                                                "preseason": False}


def team_colors() -> dict[str, dict]:
    """Club signature colors (1h cache — they change never)."""
    def fetch():
        d = _get_json(f"{ESPN_BASE}/teams")
        return parse_team_colors(d) if d else None
    return _cached("team_colors", 3600, fetch) or {}


def raw_summary(event_id: str) -> dict | None:
    """The RAW ESPN summary payload, cached separately from the parsed
    view (the lineup section needs fields parse_summary doesn't keep)."""
    return _cached(f"rawsum:{event_id}", 30,
                   lambda: _get_json(f"{ESPN_BASE}/summary",
                                     {"event": event_id}))


def match_summary(event_id: str) -> dict | None:
    """One match's live stat page. 30s cache (it IS the live view)."""
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


# --- Kalshi ----------------------------------------------------------------

TRADEABLE = ("active", "open", "initialized")


def series_probe() -> dict:
    """The discovery probe for the UNVERIFIED-coverage series: does the
    configured game series exist, and how many events are open right
    now? Cached 600s — this is a posture read, not a market feed. The
    response says exactly what was verified and what was not."""
    def fetch():
        s = _get_json(f"{KALSHI_BASE}/series/{KALSHI_GAME}")
        exists = bool(s and (s.get("series") or {}).get("ticker"))
        open_events = None
        if exists:
            d = _get_json(f"{KALSHI_BASE}/events",
                          {"series_ticker": KALSHI_GAME, "status": "open",
                           "limit": 200})
            if d is not None:
                open_events = len(d.get("events") or [])
        return {
            "series": KALSHI_GAME,
            "series_exists": exists,
            "open_events": open_events,
            # coverage is a claim about the CURRENT season's markets;
            # existence of the series ticker alone never establishes it
            "coverage_verified": bool(open_events),
        }
    return _cached("series_probe", 600, fetch) or {
        "series": KALSHI_GAME, "series_exists": None,
        "open_events": None, "coverage_verified": False}


def _game_events(limit: int = 60) -> list[dict]:
    """The game-series event list — one cheap call, 120s cache. NO
    status filter (an in-play fixture's event stops reporting "open"
    while its markets keep trading — the MLS night-one lesson).
    Tradability is judged per MARKET, at market-fetch time."""
    def fetch():
        d = _get_json(f"{KALSHI_BASE}/events",
                      {"series_ticker": KALSHI_GAME, "limit": limit})
        return (d.get("events") or []) if d else None
    return _cached("events", 120, fetch) or []


def event_markets(event_ticker: str) -> list[dict]:
    """One event's tradeable markets, 15s cache."""
    def fetch():
        # limit 50: correct-score events carry 30+ markets
        md = _get_json(f"{KALSHI_BASE}/markets",
                       {"event_ticker": event_ticker, "limit": 50})
        if md is None:
            return None
        return [m for m in (md.get("markets") or [])
                if m.get("status") in TRADEABLE]
    return _cached(f"mkts:{event_ticker}", 15, fetch) or []


def game_books(limit: int = 60) -> list[dict]:
    """Every fixture's tradeable book (the dashboard grid). Empty until
    Kalshi opens 2026-27 events — an honest zero, never an error."""
    events = _game_events(limit)
    markets = {ev["event_ticker"]: event_markets(ev["event_ticker"])
               for ev in events}
    books = parse_game_books(events, markets)
    return [b for b in books if b["markets"]]   # drop settled fixtures


def futures() -> list[dict]:
    """KXLALIGA season-winner futures (annual series, verified to
    exist 2026-07-28). 300s cache."""
    def fetch():
        d = _get_json(f"{KALSHI_BASE}/events",
                      {"series_ticker": KALSHI_FUTURES, "limit": 5,
                       "status": "open"})
        if not d:
            return None
        events = d.get("events") or []
        markets: dict[str, list[dict]] = {}
        for ev in events:
            md = _get_json(f"{KALSHI_BASE}/markets",
                           {"event_ticker": ev["event_ticker"],
                            "limit": 100})
            markets[ev["event_ticker"]] = (md.get("markets") or []
                                           if md else [])
            time.sleep(0.2)                     # Kalshi burst throttle
        return parse_game_books(events, markets)
    return _cached("futures", 300, fetch) or []


# Kalshi title-name -> ESPN displayName bridges that substring matching
# cannot cross. Harvested from ALL 383 historical KXLALIGAGAME events
# (2025-26 season, archived 2026-07-28): every other continuing club's
# Kalshi name is contained in its accent-normalized ESPN displayName.
# The three promoted clubs (Deportivo La Coruña, Málaga, Racing
# Santander) appear in NO historical event — their Kalshi names are
# UNKNOWN and must be curated when the venue first lists them, never
# guessed (the KXMENWORLDCUP lesson).
_KALSHI_ALIASES = {
    "bilbao": "athletic club",
}


def _norm_name(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").strip()


def _side_matches(kalshi_side: str, espn_name: str) -> bool:
    k, e = _norm_name(kalshi_side), _norm_name(espn_name)
    if not k or not e:
        return False
    return k in e or _KALSHI_ALIASES.get(k, "\x00") in e


def _ticker_et_date(event_ticker: str) -> str | None:
    """KXLALIGAGAME-26MAY24VILATM -> '26MAY24'."""
    import re
    m = re.match(rf"{KALSHI_GAME}-(\d{{2}}[A-Z]{{3}}\d{{2}})",
                 event_ticker or "")
    return m.group(1) if m else None


def _fixture_et_date(iso_date: str) -> str | None:
    """ESPN UTC kickoff -> the Kalshi-style US-Eastern date segment.

    Kalshi dates its MLS tickers in US-Eastern wall clock. For La Liga
    the convention is INDISTINGUISHABLE from Spain-local dating at
    realistic kickoff times (every 2025-26 kickoff lands on the same
    calendar day in both zones — Spain evenings are US afternoons), so
    the ET reading is used for consistency with the MLS machinery and
    the ambiguity is noted rather than resolved by assertion."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    et = dt.astimezone(ZoneInfo("America/New_York"))
    return et.strftime("%y%b%d").upper()


def model_key_for(series: str, ticker: str, suffix_codes: str) -> str | None:
    """One Kalshi market ticker -> the model's probability key, parsed
    from the machine-readable ticker TAIL — the same rules as MLS,
    keyed by this league's series names. None = market-only."""
    import re as _re
    tail = (ticker or "").rsplit("-", 1)[-1]
    if series == f"{SERIES_PREFIX}BTTS":
        return "btts" if tail == "BTTS" else None
    if series == f"{SERIES_PREFIX}TOTAL":
        # tail 1 = over 0.5 ... tail 6 = over 5.5
        return f"over_{int(tail) - 1}_5" if tail.isdigit() else None
    if series == f"{SERIES_PREFIX}SPREAD":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_margin_{int(m.group(2))}"
    if series == f"{SERIES_PREFIX}TEAMTOTAL":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_team_over_{int(m.group(2)) - 1}_5"
    if series == f"{SERIES_PREFIX}SCORE":
        m = _re.match(r"^([A-Z]+)(\d+)([A-Z]+)(\d+)$", tail)
        if not m:
            return None                     # "any other" style tails
        h, a = int(m.group(2)), int(m.group(4))
        if not suffix_codes.startswith(m.group(1)):
            h, a = a, h                     # defensive: observed home-first
        return f"score_{h}_{a}"
    if series == f"{SERIES_PREFIX}FTTS":
        if tail in ("NONE", "NEITHER", "NOGOAL"):
            return "no_goal"
        if tail.isalpha():
            side = "home" if suffix_codes.startswith(tail) else "away"
            return f"{side}_first_goal"
    return None                              # MOV + 1H: market-only


def find_all_books(fixture_date: str, home_name: str,
                   away_name: str) -> list[dict]:
    """Every Kalshi market family for one fixture — the GAME event is
    located by name matching, every other family suffix-joins. 30s
    bundle cache. Empty until 2026-27 events open."""
    game = find_book(fixture_date, home_name, away_name)
    if game is None:
        return []
    suffix = game["event_ticker"].split("-", 1)[1]
    suffix_codes = suffix[7:]                # strip the YYMONDD date

    def fetch():
        fams = [{"key": "winner", "label": "Winner · 3-way",
                 "event_ticker": game["event_ticker"],
                 "markets": [dict(r, model_key=None)
                             for r in game["markets"]]}]
        for key, series, label in MATCH_FAMILIES:
            if series == KALSHI_GAME:
                continue
            ticker = f"{series}-{suffix}"
            ms = event_markets(ticker)
            time.sleep(0.1)                  # burst-throttle (Kalshi 429s)
            rows = [{
                "ticker": m.get("ticker"),
                "label": m.get("yes_sub_title") or m.get("title"),
                "yes_ask": m.get("yes_ask_dollars"),
                "yes_bid": m.get("yes_bid_dollars"),
                "status": m.get("status"),
                "model_key": model_key_for(series, m.get("ticker", ""),
                                           suffix_codes),
            } for m in ms or []]
            if rows:
                fams.append({"key": key, "label": label,
                             "event_ticker": ticker, "markets": rows})
        return fams
    return _cached(f"allbooks:{suffix}", 30, fetch) or []


def find_book(fixture_date: str, home_name: str, away_name: str,
              books: list[dict] | None = None) -> dict | None:
    """This fixture's game-series book: the ticker's date segment must
    match the fixture's, then both title sides must match the ESPN
    names (a title without " vs " — Kalshi shipped one, 'Real Madrid:
    Game Winner?' — is skipped, never guessed at)."""
    want = _fixture_et_date(fixture_date)
    if books is not None:               # injected (tests)
        pool = books
    else:
        pool = [{"event_ticker": ev.get("event_ticker"),
                 "title": ev.get("title"), "markets": None}
                for ev in _game_events()]
    for b in pool:
        if _ticker_et_date(b.get("event_ticker", "")) != want:
            continue
        title = b.get("title") or ""
        if " vs " not in title:
            continue
        k_home, k_away = title.split(" vs ", 1)
        if _side_matches(k_home, home_name) and \
                _side_matches(k_away, away_name):
            if b["markets"] is None:
                b = parse_game_books(
                    [b], {b["event_ticker"]:
                          event_markets(b["event_ticker"])})[0]
            return b if b["markets"] else None
    return None
