"""Liga MX data layer — Mexican Liga BBVA MX (mex.1 / liga-mx-2026),
Jul 29 2026.

Mirrors src/mls.py's shape: keyless ESPN carries fixtures, live scores
and standings; Kalshi's public API carries the per-fixture books.
Fetch->parse split, small TTL caches, no DB, no model — the live plane
(src/live/ligamx_plane.py) owns persistence and the shadow machinery.

The PURE PARSERS are imported from src.mls rather than re-written: they
are league-neutral and each embodies a provider-drift lesson learned
the expensive way (winner-first score strings, the seasonseries rename,
conference-grouping drift). This module owns only what is league-
specific: endpoints, cache, the Kalshi series config, the alias
bridges, and the SPLIT-SEASON honesty rules below.

SPLIT SEASONS (the structural difference from MLS/EPL/La Liga): Liga MX
plays two tournaments per season — Apertura (Jul–Dec) and Clausura
(Jan–May), each with its own table and its own Liguilla playoff. ESPN's
season year 2026 spans BOTH ("2026-27 Liga BBVA MX"); the current
tournament is carried per event (season.slug, e.g. "torneo-apertura")
and per standings child (named "2026 Torneo Apertura"). Nothing here
hardcodes a tournament: every fixture card carries a `tournament`
field, every standings table is labelled with ESPN's own tournament
name, and current_tournament() reports what the provider is actually
serving (research_archive/ligamx_RESEARCH_SUMMARY_2026-07-29.json).

Kalshi ground truth (research_archive/ligamx_*_2026-07-29.json, all
verified LIVE against open Apertura books — unlike the EPL build):
  - KXLIGAMXGAME exists with 9 open events + 221 historical, exact
    KXMLSGAME grammar ({YYMONDD}{HOME}{AWAY} suffix, team-code/TIE
    tails, " vs " titles home-first).
  - 10 non-game family series exist AND list per-match markets today;
    their tail grammar was verified against the live PUECDG books
    (tail 2 = "Over 1.5", PUE2 = "wins by more than 1.5", PUE1CDG2 =
    home-first correct score). KXLIGAMXMOV exists with 0 open events;
    KXLIGAMXCUP does not exist (no futures section, none invented).
"""
from __future__ import annotations

import time

import requests

import config
# league-neutral parsers, imported NOT copied (see module docstring)
from src.mls import (parse_event, parse_game_books, parse_summary,
                     parse_team_colors, _ranked, _standing_row)

ESPN_LEAGUE = "mex.1"
ESPN_BASE = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{ESPN_LEAGUE}"
ESPN_STANDINGS = f"https://site.api.espn.com/apis/v2/sports/soccer/{ESPN_LEAGUE}/standings"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Config with a live probe (src/live/ligamx_plane.py.discovery_status),
# verified to serve open events on 2026-07-29.
KALSHI_LIGAMX_GAME = config.LIGAMX_KALSHI_GAME_SERIES

# this module keeps its OWN cache — sharing src.mls's (or src.epl's)
# dict would collide on identical keys ("standings", "sb:<date>", ...)
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
        print(f"[ligamx] fetch failed {url}: {exc}")
        return None


# --- the tournament, derived from data (never asserted) --------------------

def event_card(e: dict) -> dict:
    """One ESPN scoreboard event -> the shared normalized card PLUS the
    tournament it belongs to (season.slug, e.g. "torneo-apertura").
    Split seasons make this a first-class field: a card without it
    cannot say which competition table it counts toward."""
    card = parse_event(e)
    card["tournament"] = (e.get("season") or {}).get("slug")
    return card


def parse_current_tournament(d: dict) -> dict | None:
    """The tournament ESPN says it is serving NOW, from the scoreboard's
    leagues block: {'name': 'Torneo Apertura', 'label':
    '2026 Liga MX Apertura', 'season_display': '2026-27 Liga BBVA MX'}.
    None when the payload carries no season type — an honest missing,
    never a guess."""
    lg = (d.get("leagues") or [{}])[0]
    season = lg.get("season") or {}
    t = season.get("type") or {}
    if not isinstance(t, dict) or not t.get("name"):
        return None
    return {"name": t.get("name"),
            "label": t.get("abbreviation") or t.get("name"),
            "season_display": season.get("displayName")}


def _raw_scoreboard(date: str | None = None, ttl: float = 60):
    params = {"dates": date} if date else None
    return _cached(f"sbraw:{date or 'today'}", ttl,
                   lambda: _get_json(f"{ESPN_BASE}/scoreboard", params))


def current_tournament() -> dict | None:
    """Cached view over the scoreboard fetch (no extra upstream call
    beyond the shared raw-scoreboard cache)."""
    d = _raw_scoreboard()
    return parse_current_tournament(d) if d else None


# --- standings (one table PER TOURNAMENT + the zero-row trap) --------------

def parse_ligamx_standings(d: dict) -> list[dict]:
    """ESPN mex.1 standings -> [{table, tournament, entries}] — one
    table per tournament child, each labelled with ESPN's OWN child
    name ("2026 Torneo Apertura").

    Split-season honesty: unlike the EPL parser this NEVER collapses
    children into one table — Apertura and Clausura are separate
    competitions with separate tables, and merging them would fabricate
    a combined order no governing body publishes. Within each child,
    the preseason zero-row trap still applies (ESPN ships complete
    all-zero rows ranked alphabetically before a ball is kicked —
    research_archive/epl/): a child whose every row has 0 played is
    dropped, so a Clausura preseason can never fabricate a table beside
    a real Apertura one."""
    out = []
    for group in d.get("children") or []:
        rows = []
        for e in (group.get("standings") or {}).get("entries") or []:
            r = _standing_row(e)
            if r["_key"]:
                rows.append(r)
        if not rows or all((r["played"] or 0) == 0 for r in rows):
            continue                    # not started: no fabricated table
        rows = _ranked(rows)
        for r in rows:
            r.pop("_key", None)
        name = group.get("name") or "Liga MX"
        out.append({"table": name, "tournament": name, "entries": rows})
    return out


# --- fetchers --------------------------------------------------------------

def scoreboard(date: str | None = None) -> list[dict]:
    """Normalized fixtures for one ESPN bucket (YYYYMMDD; the default
    bucket is the NEXT MATCHDAY when nothing is on today — the heading
    is the frontend's to derive, never to assert). 60s cache."""
    d = _raw_scoreboard(date)
    return [event_card(e) for e in d.get("events") or []] if d else []


def schedule(days: int = 7) -> list[dict]:
    """The next `days` days of fixtures (today inclusive), flattened and
    kickoff-ordered. 300s cache per day-bucket."""
    from datetime import datetime, timedelta, timezone
    out: list[dict] = []
    today = datetime.now(timezone.utc)
    for i in range(max(1, min(days, 14))):
        day = (today + timedelta(days=i)).strftime("%Y%m%d")
        d = _raw_scoreboard(day, ttl=300)
        out.extend([event_card(e) for e in d.get("events") or []]
                   if d else [])
    seen: set[str] = set()
    uniq = [f for f in out
            if f["id"] not in seen and not seen.add(f["id"])]
    uniq.sort(key=lambda f: f.get("date") or "")
    return uniq


def standings() -> list[dict]:
    def fetch():
        d = _get_json(ESPN_STANDINGS)
        return parse_ligamx_standings(d) if d else None
    return _cached("standings", 300, fetch) or []


TRADEABLE = ("active", "open", "initialized")


def _game_events(limit: int = 60) -> list[dict]:
    """The configured game-series event list — one cheap call, 120s
    cache. NO status filter (the MLS lesson: an in-play fixture's event
    stops reporting "open" while its markets keep trading)."""
    def fetch():
        d = _get_json(f"{KALSHI_BASE}/events",
                      {"series_ticker": KALSHI_LIGAMX_GAME, "limit": limit})
        return (d.get("events") or []) if d else None
    return _cached("events", 120, fetch) or []


def event_markets(event_ticker: str) -> list[dict]:
    """One event's tradeable markets, 15s cache."""
    def fetch():
        md = _get_json(f"{KALSHI_BASE}/markets",
                       {"event_ticker": event_ticker, "limit": 50})
        if md is None:
            return None
        return [m for m in (md.get("markets") or [])
                if m.get("status") in TRADEABLE]
    return _cached(f"mkts:{event_ticker}", 15, fetch) or []


def game_books(limit: int = 60) -> list[dict]:
    """Every fixture's tradeable book (9 open Apertura events at build
    time — live-verified, not aspirational)."""
    events = _game_events(limit)
    markets = {ev["event_ticker"]: event_markets(ev["event_ticker"])
               for ev in events}
    books = parse_game_books(events, markets)
    return [b for b in books if b["markets"]]


# --- per-match summary (reuses the MLS parser verbatim) --------------------
# Verified 2026-07-29: mex.1 summaries carry every STAT_ORDER key plus
# lastFiveGames + seasonseries + rosters, so parse_summary — including
# the derived result letters and the drift audit — applies unchanged.
# Names carry Spanish accents (América, Querétaro, Efraín Álvarez):
# nothing in this module may ASCII-fold display values.

def raw_summary(event_id: str) -> dict | None:
    """The RAW ESPN summary payload (the lineup view needs rosters)."""
    return _cached(f"rawsum:{event_id}", 30,
                   lambda: _get_json(f"{ESPN_BASE}/summary",
                                     {"event": event_id}))


def team_colors() -> dict[str, dict]:
    def fetch():
        d = _get_json(f"{ESPN_BASE}/teams")
        return parse_team_colors(d) if d else None
    return _cached("team_colors", 3600, fetch) or {}


def match_summary(event_id: str) -> dict | None:
    """One match's live stat page. 30s cache."""
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
    """The derived-letter-vs-shown-scores audit, on mex.1 payloads."""
    from src.mls import scoreline_disagreements as _audit
    return _audit(summary)


# --- fixture <-> Kalshi book matching --------------------------------------

# Kalshi title -> ESPN displayName bridges that substring matching cannot
# cross. Kalshi titles are ASCII while ESPN names carry accents; the
# normalizer strips accents, so "America" already reaches "América" and
# "Queretaro" reaches "Querétaro" without an alias. CURATED from all 221
# archived KXLIGAMXGAME titles + the 9 open ones (36 distinct sides,
# research_archive/ligamx_kalshi_events_full_2026-07-29.json). Mazatlán
# is relegated (not an ESPN mex.1 club) and deliberately has no alias.
_KALSHI_ALIASES = {
    # current open-event forms substring matching cannot cross
    "tijuana de caliente": "tijuana",
    "santos laguna": "santos",
    # historical 25/26 long forms, kept because they have appeared in
    # real titles and may return; every target is a current ESPN name
    "cf america": "america",
    "cd guadalajara": "guadalajara",
    "cf cruz azul": "cruz azul",
    "cf monterrey": "monterrey",
    "cf pachuca": "pachuca",
    "club leon": "leon",
    "club necaxa": "necaxa",
    "club puebla": "puebla",
    "club santos laguna": "santos",
    "club tijuana de caliente": "tijuana",
    "deportivo toluca fc": "toluca",
    "atletico san luis": "san luis",
    "atlas fc": "atlas",
    "queretaro fc": "queretaro",
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
    """KXLIGAMXGAME-26JUL31PUECDG -> '26JUL31'. Kalshi ticker dates are
    US-Eastern — verified live: the 2026-08-01T01:00Z Puebla kickoff
    (Jul 31 21:00 EDT) carries 26JUL31. Mexican kickoffs are evening
    US-time, so the ET segment regularly differs from the UTC date; the
    zoneinfo conversion below is the join, never string-slicing."""
    import re
    m = re.match(rf"{KALSHI_LIGAMX_GAME}-(\d{{2}}[A-Z]{{3}}\d{{2}})",
                 event_ticker or "")
    return m.group(1) if m else None


def _fixture_et_date(iso_date: str) -> str | None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    et = dt.astimezone(ZoneInfo("America/New_York"))
    return et.strftime("%y%b%d").upper()


# --- per-match Kalshi families ---------------------------------------------
# ALL verified live 2026-07-29 against the open PUECDG books
# (research_archive/ligamx_kalshi_family_markets_PUECDG_2026-07-29.json):
# every family below listed real markets except MOV (series exists,
# 0 open events at probe time — it contributes rows if/when it lists).

MATCH_FAMILIES = [
    ("winner", KALSHI_LIGAMX_GAME, "Winner · 3-way"),
    ("total", "KXLIGAMXTOTAL", "Total goals"),
    ("btts", "KXLIGAMXBTTS", "Both teams to score"),
    ("spread", "KXLIGAMXSPREAD", "Spread"),
    ("team_total", "KXLIGAMXTEAMTOTAL", "Team totals"),
    ("score", "KXLIGAMXSCORE", "Correct score"),
    ("ftts", "KXLIGAMXFTTS", "First team to score"),
    ("mov", "KXLIGAMXMOV", "Method of victory"),
    ("h1", "KXLIGAMX1H", "1st half · winner"),
    ("h1_total", "KXLIGAMX1HTOTAL", "1st half · total"),
    ("h1_spread", "KXLIGAMX1HSPREAD", "1st half · spread"),
    ("h1_btts", "KXLIGAMX1HBTTS", "1st half · BTTS"),
]


def model_key_for(series: str, ticker: str, suffix_codes: str) -> str | None:
    """One Kalshi market ticker -> the model's probability key, from the
    machine-readable ticker TAIL. Grammar VERIFIED LIVE against the open
    PUECDG family books 2026-07-29 (tail 2 titled "Over 1.5 goals
    scored"; PUE2 "wins by more than 1.5"; PUE1CDG2 "CD Guadalajara
    wins 2-1" — home-first). A tail it cannot parse maps to None
    (market-only row), never a guess."""
    import re as _re
    tail = (ticker or "").rsplit("-", 1)[-1]
    if series == "KXLIGAMXBTTS":
        return "btts" if tail == "BTTS" else None
    if series == "KXLIGAMXTOTAL":
        return f"over_{int(tail) - 1}_5" if tail.isdigit() else None
    if series == "KXLIGAMXSPREAD":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_margin_{int(m.group(2))}"
    if series == "KXLIGAMXTEAMTOTAL":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_team_over_{int(m.group(2)) - 1}_5"
    if series == "KXLIGAMXSCORE":
        m = _re.match(r"^([A-Z]+)(\d+)([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        h, a = int(m.group(2)), int(m.group(4))
        if not suffix_codes.startswith(m.group(1)):
            h, a = a, h
        return f"score_{h}_{a}"
    if series == "KXLIGAMXFTTS":
        if tail in ("NONE", "NEITHER", "NOGOAL"):
            return "no_goal"
        if tail.isalpha():
            side = "home" if suffix_codes.startswith(tail) else "away"
            return f"{side}_first_goal"
    return None                              # MOV + 1H families: market-only


def find_all_books(fixture_date: str, home_name: str,
                   away_name: str) -> list[dict]:
    """Every Kalshi market family for one fixture, suffix-joined from
    the game event. 30s bundle cache."""
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
            if series == KALSHI_LIGAMX_GAME:
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
    """This fixture's game-series book: date segment must match the
    ticker, then both title sides must match the ESPN names."""
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
            # observed in MLS/EPL history: some events title
            # "{Team}: Game Winner?" — no side split, no name-verified
            # match; skip honestly
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
