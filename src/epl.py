"""EPL data layer — Premier League (eng.1 / epl-2026), Jul 28 2026.

Mirrors src/mls.py's shape: keyless ESPN carries fixtures, live scores
and standings; Kalshi's public API carries the per-fixture 3-way books.
Fetch->parse split, small TTL caches, no DB, no model — the live plane
(src/live/epl_plane.py) owns persistence and the shadow machinery.

The PURE PARSERS are imported from src.mls rather than re-written: they
are league-neutral and each embodies a provider-drift lesson learned
the expensive way (winner-first score strings, the seasonseries rename,
conference-grouping drift). Re-implementing them here would fork those
fixes. This module owns only what is league-specific: endpoints, cache,
the Kalshi series config, the alias bridges, and the standings honesty
rule for a season that has not started.

Kalshi ground truth (research_archive/epl/, 2026-07-28):
  - KXEPLGAME EXISTS and carried 387 events across 2025-26 with the
    exact KXMLSGAME grammar ({YYMONDD}{HOME}{AWAY} suffix, team-code /
    TIE market tails, " vs " titles). NO 2026-27 fixture is listed yet.
    The series ticker is therefore config, verified-as-a-series but with
    the new season's listings UNVERIFIED.
  - `status=open` DOES NOT MEAN CURRENT here. The archived probe
    (kalshi_events_KXEPLGAME_2026-07-28T1015Z.json) returned TEN events
    under status=open, and every one is dated 26MAY24 — the final
    matchday of 2025-26, long settled. This is why retrieval below is
    bounded by the ticker DATE and never by provider status.
  - 10 non-game family series exist as DEFINITIONS (no KXEPLMOV). Their
    per-match listing behaviour and tail grammar for 26/27 are
    unverified; they ship as config and simply return no rows until
    Kalshi lists them.
"""
from __future__ import annotations

import time

import requests

import config
# league-neutral parsers, imported NOT copied (see module docstring)
from src.mls import (parse_event, parse_game_books, parse_summary,
                     parse_team_colors, _ranked, _standing_row)

ESPN_LEAGUE = "eng.1"
ESPN_BASE = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{ESPN_LEAGUE}"
ESPN_STANDINGS = f"https://site.api.espn.com/apis/v2/sports/soccer/{ESPN_LEAGUE}/standings"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Config, not fact: the series exists (research_archive/epl/) but the
# 2026-27 listings do not yet, so the ticker stays overridable and the
# discovery probe (src/live/epl_plane.py) reports its live status.
KALSHI_EPL_GAME = config.EPL_KALSHI_GAME_SERIES

# this module keeps its OWN cache — sharing src.mls's dict would collide
# on identical keys ("standings", "sb:<date>", ...)
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
        print(f"[epl] fetch failed {url}: {exc}")
        return None


# --- standings (single table + the preseason zero-row trap) ----------------

def parse_epl_standings(d: dict) -> list[dict]:
    """ESPN eng.1 standings -> [{table, entries}] — or [] before the
    season has produced a single result.

    Verified 2026-07-28 (research_archive/epl/espn_standings_*.json):
    preseason is NOT an empty payload. ESPN returns one child with 20
    complete rows, every stat 0.0 and `rank` assigned ALPHABETICALLY
    (AFC Bournemouth "1st"). Rendering that is fabricating a league
    order out of zero information — the same defect class as rendering
    the winner-first score string. A table exists here only once at
    least one club has actually played; otherwise the honest answer is
    an explicit empty, which the UI states in words.

    The EPL is a single league table (no conferences), so any club
    appearing in multiple children keeps its freshest row, and one
    combined table is emitted under the league's own name.
    """
    freshest: dict[str, dict] = {}
    name = None
    for group in d.get("children") or []:
        name = name or group.get("name")
        for e in (group.get("standings") or {}).get("entries") or []:
            r = _standing_row(e)
            if not r["_key"]:
                continue
            cur = freshest.get(r["_key"])
            if cur is None or (r["played"] or 0) > (cur["played"] or 0):
                freshest[r["_key"]] = r
    rows = [dict(v) for v in freshest.values()]
    if not rows or all((r["played"] or 0) == 0 for r in rows):
        return []                       # season not started: no fabricated table
    rows = _ranked(rows)
    for r in rows:
        r.pop("_key", None)
    return [{"table": name or "Premier League", "entries": rows}]


# --- fetchers --------------------------------------------------------------

def scoreboard(date: str | None = None) -> list[dict]:
    """Normalized fixtures for one ESPN bucket (YYYYMMDD; the default
    bucket is the NEXT MATCHDAY when nothing is on today — the heading
    is the frontend's to derive, never to assert). 60s cache."""
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


def standings() -> list[dict]:
    def fetch():
        d = _get_json(ESPN_STANDINGS)
        return parse_epl_standings(d) if d else None
    return _cached("standings", 300, fetch) or []


TRADEABLE = ("active", "open", "initialized")

# --- fixture horizon + provider-call budget (P0-1) -------------------------
# KXEPLGAME carries 387 ARCHIVED 2025-26 events and, as of 2026-07-28,
# ZERO current listings. An unbounded per-event market fan-out therefore
# spends the whole Kalshi rate budget on settled history and returns
# nothing — the events call is one request, but the /markets calls are one
# PER EVENT, so the cost is linear in the size of the archive.
#
# Retrieval is bounded by the ticker's DATE, never by provider status: an
# in-play fixture's event stops reporting "open" while its markets keep
# trading (the MLS lesson above), so a recent event is retained whatever
# its status says. The number of per-event market calls is additionally
# capped, and the cap is REPORTED (`game_books_state`) rather than
# silently truncating.
EVENT_HISTORY_DAYS = 2          # yesterday + today's late finishes
EVENT_HORIZON_DAYS = 14         # two matchweeks ahead
EVENT_MARKET_CALL_BUDGET = 24   # per-event /markets calls per sweep


def _ticker_day(event_ticker: str):
    """'KXEPLGAME-26MAY24WHULEE' -> date(2026, 5, 24), or None."""
    from datetime import datetime
    seg = _ticker_et_date(event_ticker)
    if not seg:
        return None
    try:
        return datetime.strptime(seg, "%y%b%d").date()
    except ValueError:
        return None


def events_in_horizon(events: list[dict], today=None) -> list[dict]:
    """The subset of a game-series event list inside the fixture horizon.

    An event whose ticker date cannot be parsed is KEPT: an unreadable
    date is missing evidence, and dropping it would silently convert
    "we don't know when this is" into "this is history"."""
    from datetime import date, timedelta
    today = today or date.today()
    lo = today - timedelta(days=EVENT_HISTORY_DAYS)
    hi = today + timedelta(days=EVENT_HORIZON_DAYS)
    out = []
    for ev in events:
        day = _ticker_day(ev.get("event_ticker") or "")
        if day is None or lo <= day <= hi:
            out.append(ev)
    return out


def _game_events(limit: int = 60) -> list[dict]:
    """The configured game-series event list — one cheap call, 120s
    cache. NO status filter (the MLS lesson: an in-play fixture's event
    stops reporting "open" while its markets keep trading)."""
    def fetch():
        d = _get_json(f"{KALSHI_BASE}/events",
                      {"series_ticker": KALSHI_EPL_GAME, "limit": limit})
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


def game_books_state(limit: int = 60) -> dict:
    """Every in-horizon fixture's tradeable book, PLUS what it cost.

    `market_calls` never exceeds EVENT_MARKET_CALL_BUDGET, and
    `budget_exhausted` says so explicitly when the horizon held more
    events than the budget allows — a truncated answer must name itself
    (the same rule the registry sweep follows)."""
    all_events = _game_events(limit)
    events = events_in_horizon(all_events)
    fetched: list[dict] = []
    markets: dict[str, list] = {}
    for ev in events:
        if len(fetched) >= EVENT_MARKET_CALL_BUDGET:
            break
        ticker = ev.get("event_ticker")
        if not ticker:
            continue
        markets[ticker] = event_markets(ticker)
        fetched.append(ev)
    books = parse_game_books(fetched, markets)
    return {
        "games": [b for b in books if b["markets"]],
        "events_seen": len(all_events),
        "events_in_horizon": len(events),
        "market_calls": len(fetched),
        "market_call_budget": EVENT_MARKET_CALL_BUDGET,
        "budget_exhausted": len(events) > len(fetched),
        "horizon_days": [EVENT_HISTORY_DAYS, EVENT_HORIZON_DAYS],
    }


def game_books(limit: int = 60) -> list[dict]:
    """Every in-horizon fixture's tradeable book. Empty until Kalshi
    lists 26/27 — an honest empty, surfaced as such by the frontend."""
    return game_books_state(limit)["games"]


# --- per-match summary (reuses the MLS parser verbatim) --------------------
# Verified 2026-07-28: eng.1 summaries carry every STAT_ORDER key plus
# lastFiveGames + seasonseries, so parse_summary — including the derived
# result letters and the drift audit — applies unchanged.

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
    """The derived-letter-vs-shown-scores audit, on eng.1 payloads."""
    from src.mls import scoreline_disagreements as _audit
    return _audit(summary)


# --- fixture <-> Kalshi book matching (fail-closed identity, P0-4) --------
#
# Identity is EXACT or it does not decide, and a date+name coincidence
# never picks the first candidate it finds. Substring containment is
# banned outright: it is not a bridge but a guess that happened to be
# right, and it is the defect class that let a reserve side attach to its
# parent club elsewhere in this codebase — "Leeds" contains-matches
# "Leeds United" and would equally match a future "Leeds United U21".
# A related-name overlap is SURFACED as a candidate and never priced.

# Generic club-form suffixes that carry no identity. "AFC" is here so
# Kalshi's "Bournemouth" equals ESPN's "AFC Bournemouth" by token set.
# "United", "City", "Forest", "Hotspur", "Albion" are deliberately NOT —
# they are the whole difference between "Leeds" and "Leeds United".
_GENERIC_TOKENS = {"fc", "afc", "cf", "sc", "club"}

# Kalshi title -> ESPN displayName bridges. EVIDENCE-BACKED ONLY: every
# entry is a pair observed across all 387 KXEPLGAME event titles of
# 2025-26 (research_archive/epl/kalshi_events_KXEPLGAME_full_*.json) set
# against the archived ESPN eng.1 team list for 2026-27
# (espn_teams_2026-07-28T1015Z.json). Never add one from intuition.
#
# All but "Man Utd" were previously handled by substring containment.
# The three promoted clubs (Coventry City, Hull City, Ipswich Town) have
# NO Kalshi history, so their Kalshi names are unknowable until 26/27
# lists and they stay honestly unmapped. Relegated 25/26 sides (Burnley,
# West Ham, Wolverhampton) are absent from the 26/27 ESPN team list, so
# no evidenced ESPN displayName exists for them and none is invented.
_KALSHI_ALIASES = {
    "man utd": "manchester united",
    "bournemouth": "afc bournemouth",
    "brighton": "brighton & hove albion",
    "newcastle": "newcastle united",
    "nottingham": "nottingham forest",
    "tottenham": "tottenham hotspur",
}


def _norm_name(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace(".", "").strip()


def _identity_tokens(s: str) -> frozenset:
    return frozenset(t for t in _norm_name(s).split()
                     if t not in _GENERIC_TOKENS)


def side_identity(kalshi_side: str, espn_name: str) -> str:
    """-> "exact" | "loose" | "none".

    exact  same club, safe to price: normalized equality, token-set
           equality (generic club suffixes ignored), or an
           archive-evidenced alias.
    loose  related-name overlap (token subset either way) — enough to
           SHOW as a candidate, never enough to decide. "Leeds" vs
           "Leeds United" lives here, and so would any reserve or
           academy side a provider ever lists.
    none   unrelated.
    """
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
    """KXEPLGAME-26MAY24WHULEE -> '26MAY24'. Kalshi ticker dates are
    US-Eastern; every realistic UK kickoff (11:30-21:00 UK) lands on the
    same US-Eastern calendar day, so the ET conversion below is also the
    UK date for EPL fixtures."""
    import re
    m = re.match(rf"{KALSHI_EPL_GAME}-(\d{{2}}[A-Z]{{3}}\d{{2}})",
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
# PROVIDER-UNVERIFIED (config.EPL_FAMILY_GRAMMAR_STATUS). Series
# DEFINITIONS were probed and exist 2026-07-28 (no KXEPLMOV), and that is
# ALL that has been observed: nothing is known about whether these
# families list per-match markets for 2026-27, nor whether their
# ticker-TAIL grammar matches the MLS families `model_key_for` below was
# written against. Until real listings appear each contributes zero rows,
# and an unparseable tail maps to None (market-only), never a guess. The
# status is reported at /api/epl/markets/discovery so the caveat travels
# with the data rather than living only here.

MATCH_FAMILIES = [
    ("winner", KALSHI_EPL_GAME, "Winner · 3-way"),
    ("total", "KXEPLTOTAL", "Total goals"),
    ("btts", "KXEPLBTTS", "Both teams to score"),
    ("spread", "KXEPLSPREAD", "Spread"),
    ("team_total", "KXEPLTEAMTOTAL", "Team totals"),
    ("score", "KXEPLSCORE", "Correct score"),
    ("ftts", "KXEPLFTTS", "First team to score"),
    ("h1", "KXEPL1H", "1st half · winner"),
    ("h1_total", "KXEPL1HTOTAL", "1st half · total"),
    ("h1_spread", "KXEPL1HSPREAD", "1st half · spread"),
    ("h1_btts", "KXEPL1HBTTS", "1st half · BTTS"),
]


def model_key_for(series: str, ticker: str, suffix_codes: str) -> str | None:
    """One Kalshi market ticker -> the model's probability key, from the
    machine-readable ticker TAIL. Grammar carried over from the verified
    MLS families; for EPL it is exercised only once real 26/27 markets
    list, and a tail it cannot parse maps to None (market-only row),
    never a guess."""
    import re as _re
    tail = (ticker or "").rsplit("-", 1)[-1]
    if series == "KXEPLBTTS":
        return "btts" if tail == "BTTS" else None
    if series == "KXEPLTOTAL":
        return f"over_{int(tail) - 1}_5" if tail.isdigit() else None
    if series == "KXEPLSPREAD":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_margin_{int(m.group(2))}"
    if series == "KXEPLTEAMTOTAL":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_team_over_{int(m.group(2)) - 1}_5"
    if series == "KXEPLSCORE":
        m = _re.match(r"^([A-Z]+)(\d+)([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        h, a = int(m.group(2)), int(m.group(4))
        if not suffix_codes.startswith(m.group(1)):
            h, a = a, h
        return f"score_{h}_{a}"
    if series == "KXEPLFTTS":
        if tail in ("NONE", "NEITHER", "NOGOAL"):
            return "no_goal"
        if tail.isalpha():
            side = "home" if suffix_codes.startswith(tail) else "away"
            return f"{side}_first_goal"
    return None                              # 1H families: market-only


def find_all_books(fixture_date: str, home_name: str,
                   away_name: str) -> list[dict]:
    """Every Kalshi market family for one fixture, suffix-joined from
    the game event. 30s bundle cache. Empty unless the game event mapped
    UNAMBIGUOUSLY — see match_book."""
    game = match_book(fixture_date, home_name, away_name)["book"]
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
            if series == KALSHI_EPL_GAME:
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


def match_book(fixture_date: str, home_name: str, away_name: str,
               books: list[dict] | None = None) -> dict:
    """This fixture's game-series book, or an EXPLICIT refusal (P0-4).

    -> {"status", "book", "candidates", "loose_candidates"} where status is
       mapped           exactly ONE same-ET-date event matched both names
                        EXACTLY (equality / token-set / archived alias)
       unmapped         nothing matched
       ambiguous        more than one candidate at the deciding tier —
                        the candidates are returned and NONE is picked
       unresolved_name  exactly one candidate, matching only loosely
                        (token subset). Shown, never priced.
       no_open_markets  one exact match whose book holds nothing tradeable

    The previous implementation returned the FIRST event whose ticker
    date matched and whose title sides CONTAINED the ESPN names. Two
    same-date candidates therefore resolved silently to whichever the
    provider happened to list first, and containment made "Leeds" a match
    for "Leeds United". Nothing here consults the ticker's team
    ABBREVIATIONS either: they are a 3-letter code with no registry
    behind them, so they can propose but must never decide.
    """
    want = _fixture_et_date(fixture_date)
    if books is not None:               # injected (tests)
        pool = books
    else:
        pool = [{"event_ticker": ev.get("event_ticker"),
                 "title": ev.get("title"), "markets": None}
                for ev in _game_events()]
    exact: list[dict] = []
    loose: list[dict] = []
    for b in pool:
        if _ticker_et_date(b.get("event_ticker", "")) != want:
            continue
        title = b.get("title") or ""
        if " vs " not in title:
            # defensive: no side split, so no name-verified identity is
            # possible. (Every archived 25/26 title DOES carry " vs " —
            # see the note on the ": Game Winner?" shape in the tests.)
            continue
        k_home, k_away = title.split(" vs ", 1)
        h = side_identity(k_home, home_name)
        a = side_identity(k_away, away_name)
        if h == "none" or a == "none":
            continue
        (exact if (h == "exact" and a == "exact") else loose).append(b)

    def _refuse(status, cands, loose_cands=()):
        return {"status": status, "book": None,
                "candidates": [c.get("event_ticker") for c in cands],
                "loose_candidates": [c.get("event_ticker")
                                     for c in loose_cands]}

    loose_tickers = [c.get("event_ticker") for c in loose]
    if len(exact) > 1:
        print(f"[epl] AMBIGUOUS book match for {home_name!r} vs "
              f"{away_name!r} on {want}: "
              f"{[c.get('event_ticker') for c in exact]} — refusing")
        return _refuse("ambiguous", exact, loose)
    if len(exact) == 1:
        b = exact[0]
        if b.get("markets") is None:
            b = parse_game_books(
                [b], {b["event_ticker"]:
                      event_markets(b["event_ticker"])})[0]
        if not b["markets"]:
            return _refuse("no_open_markets", exact, loose)
        return {"status": "mapped", "book": b,
                "candidates": [b["event_ticker"]],
                "loose_candidates": loose_tickers}
    if len(loose) > 1:
        return _refuse("ambiguous", loose)
    if len(loose) == 1:
        return _refuse("unresolved_name", [], loose)
    return _refuse("unmapped", [])


def find_book(fixture_date: str, home_name: str, away_name: str,
              books: list[dict] | None = None) -> dict | None:
    """The book when — and only when — match_book says `mapped`."""
    return match_book(fixture_date, home_name, away_name,
                      books=books)["book"]
