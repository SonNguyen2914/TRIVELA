"""MLS data layer — the first next-league surface (Jul 22, 2026).

Self-contained by design: nothing here touches the WC26 archive paths,
the DB, or the scheduler. Keyless ESPN (usa.1) carries fixtures, live
scores, and standings; Kalshi's public API carries the KXMLSGAME 3-way
books and KXMLSCUP futures. Everything is fetch->parse split so the
parsers are unit-testable on canned payloads, and every fetch runs
through a small TTL cache so the public endpoints cannot stampede the
providers.

Scope honesty: this is the DATA layer only — no model, no suggestions,
no persistence. The V7 Part H acceptance gates still stand before any
MLS model/trading work; this module exists so real data flows today.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

import config

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/usa.1/standings"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# family constants, verified live 2026-07-22: KXMLSGAME = 3-way
# (home/tie/away) per fixture; KXMLSCUP = championship futures
KALSHI_MLS_GAME = "KXMLSGAME"
KALSHI_MLS_CUP = "KXMLSCUP"

# key -> (monotonic_at_store, wall_clock_at_store, data). The wall clock
# is carried so a freshness-aware caller can REPORT observed_at; the
# monotonic clock stays the arithmetic basis (immune to wall adjustments).
_cache: dict[str, tuple[float, datetime, object]] = {}

# TTLs of every cache on the PRICE path (journal-P0-B). One named table
# rather than scattered literals, because MLS_PRICE_MAX_AGE_SECONDS is
# the fail-closed ceiling over data ASSEMBLED from these caches: the
# ceiling must exceed every inner TTL or perfectly healthy data would
# be refused as stale.
PRICE_PATH_TTLS = {"events": 120, "mkts": 15, "allbooks": 30}


def _validate_price_freshness_config() -> None:
    """Fail fast at import (journal-P0-B): an incoherent ceiling is a
    config error, not something to discover mid-match."""
    import config as _config
    worst = max(PRICE_PATH_TTLS.values())
    if _config.MLS_PRICE_MAX_AGE_SECONDS <= worst:
        raise ValueError(
            f"MLS_PRICE_MAX_AGE_SECONDS="
            f"{_config.MLS_PRICE_MAX_AGE_SECONDS} must exceed the "
            f"largest price-path cache TTL ({worst}s; {PRICE_PATH_TTLS})"
            f" — a smaller ceiling would refuse healthy data as stale")


_validate_price_freshness_config()


def _cached(key: str, ttl: float, fetch):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[2]
    data = fetch()
    if data is not None:               # never cache a failed answer
        _cache[key] = (now, datetime.now(timezone.utc), data)
        return data
    return hit[2] if hit else None     # stale beats nothing


def _cached_meta(key: str, ttl: float, fetch):
    """`_cached` plus the raw provenance a COMPOSED caller needs
    (journal-P0-B): (data, meta) with meta = {mono, wall, fresh}.
    `mono`/`wall` are the store clocks of the value actually served —
    a stale serve keeps the ORIGINAL store time, never now — and
    `fresh` is False exactly when a refresh failed and an old value was
    served. meta is None when nothing exists at all."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[2], {"mono": hit[0], "wall": hit[1], "fresh": True}
    data = fetch()
    if data is not None:
        _cache[key] = (now, datetime.now(timezone.utc), data)
        h = _cache[key]
        return data, {"mono": h[0], "wall": h[1], "fresh": True}
    if hit:                            # stale serve: original clocks
        return hit[2], {"mono": hit[0], "wall": hit[1], "fresh": False}
    return None, None


def aggregate_freshness(metas: list, max_age: float) -> dict:
    """The freshness of a COMPOSED result is its least trustworthy
    constituent (journal-P0-B). age = now - the OLDEST constituent's
    store time — never assembly time, which is how a bundle rebuilt
    from stale inner caches used to launder a two-hour-old ask into
    `live, age 0`. Any stale constituent taints the whole to
    stale_fallback; past max_age the whole is unavailable."""
    ms = [m for m in metas if m]
    if not ms:
        return {"observed_at": None, "age_seconds": None,
                "status": "unavailable"}
    now = time.monotonic()
    oldest = min(ms, key=lambda m: m["mono"])
    age = int(now - oldest["mono"])
    if age > max_age:
        status = "unavailable"
    elif any(not m["fresh"] for m in ms):
        status = "stale_fallback"
    else:
        status = "live"
    return {"observed_at": oldest["wall"].isoformat(),
            "age_seconds": age, "status": status}


def cached_with_freshness(key: str, ttl: float, fetch,
                          max_age: float) -> tuple[object, dict]:
    """`_cached`, but truthful about age (journal-P0 F4) — the
    SINGLE-KEY view, built on `_cached_meta`. For composed results use
    `aggregate_freshness` over every constituent's meta instead: a
    single outer key stamps assembly time and launders inner staleness.

        live            fresh fetch, or a cache hit within ttl
        stale_fallback  refresh failed; cache older than ttl but within
                        max_age — served, WITH its age
        unavailable     refresh failed and nothing young enough exists —
                        data is None; fail closed, no price presented
    """
    data, meta = _cached_meta(key, ttl, fetch)
    if meta is None:
        return None, {"observed_at": None, "age_seconds": None,
                      "status": "unavailable"}
    now = time.monotonic()
    age = int(now - meta["mono"])
    if meta["fresh"]:
        return data, {"observed_at": meta["wall"].isoformat(),
                      "age_seconds": age, "status": "live"}
    if age <= max_age:
        return data, {"observed_at": meta["wall"].isoformat(),
                      "age_seconds": age, "status": "stale_fallback"}
    return None, {"observed_at": meta["wall"].isoformat(),
                  "age_seconds": age, "status": "unavailable"}


def _get_json(url: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        print(f"[mls] fetch failed {url}: {exc}")
        return None


# --- parsers (pure, canned-payload testable) -------------------------------

def parse_event(e: dict) -> dict:
    """One ESPN scoreboard event -> a normalized fixture card."""
    comp = (e.get("competitions") or [{}])[0]
    sides: dict[str, dict] = {}
    for c in comp.get("competitors") or []:
        t = c.get("team") or {}
        rec = ""
        for r in c.get("records") or []:
            if r.get("summary"):
                rec = r["summary"]
                break
        sides[c.get("homeAway", "?")] = {
            "name": t.get("displayName"),
            "short": t.get("shortDisplayName"),
            "abbrev": t.get("abbreviation"),
            "logo": t.get("logo"),
            "score": c.get("score"),
            "record": rec,
        }
    status = e.get("status") or {}
    stype = status.get("type") or {}
    return {
        "id": e.get("id"),
        "date": e.get("date"),
        "state": stype.get("state"),           # pre | in | post
        "detail": stype.get("shortDetail"),
        "minute": status.get("displayClock"),
        "venue": (comp.get("venue") or {}).get("fullName"),
        "home": sides.get("home", {}),
        "away": sides.get("away", {}),
    }


def _standing_row(entry: dict) -> dict:
    team = entry.get("team") or {}
    stats = {s.get("name"): s.get("value") for s in entry.get("stats") or []}
    return {
        "team": team.get("displayName"),
        "abbrev": team.get("abbreviation"),
        "logo": (team.get("logos") or [{}])[0].get("href")
        if team.get("logos") else None,
        "rank": stats.get("rank"),
        "played": stats.get("gamesPlayed"),
        "wins": stats.get("wins"),
        "losses": stats.get("losses"),
        "ties": stats.get("ties"),
        "points": stats.get("points"),
        "goals_for": stats.get("pointsFor"),
        "goals_against": stats.get("pointsAgainst"),
        "goal_diff": stats.get("pointDifferential"),
        "ppg": stats.get("ppg"),
        "_key": str(team.get("id") or team.get("abbreviation") or
                    team.get("displayName") or ""),
    }


def _ranked(rows: list[dict]) -> list[dict]:
    """Order a conference and guarantee each place appears once.

    ESPN's rank is kept when it is already a clean 1..n — it encodes the
    league's own tiebreakers, which are more authoritative than anything
    reconstructed here. It is RECOMPUTED (points, wins, goal difference,
    goals for) only when the supplied ranks collide, which is the state
    that would otherwise render as two teams sharing a place."""
    rows = sorted(rows, key=lambda r: (r["rank"] is None, r["rank"] or 0,
                                       -(r["points"] or 0)))
    ranks = [r["rank"] for r in rows]
    if None in ranks or len(set(ranks)) != len(ranks):
        rows.sort(key=lambda r: (-(r["points"] or 0), -(r["wins"] or 0),
                                 -(r["goal_diff"] or 0),
                                 -(r["goals_for"] or 0),
                                 r["team"] or ""))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
    return rows


def parse_standings(d: dict) -> list[dict]:
    """ESPN standings -> [{conference, entries: [...]}], rank-ordered.

    ESPN's own grouping cannot be trusted to be a conference. On
    2026-07-26 the child NAMED "Eastern Conference" carried all 30 clubs
    (its inner standings block is literally `name: "overall"`) while
    "Western Conference" carried the correct 15 — so every Western club
    was rendered twice, once in each table, and the East table showed a
    Western club in first place. Worse, the two blocks disagreed: the
    30-row block was one matchday fresher, so the same club showed
    different points depending on which table you read.

    So membership and statistics are taken from different places, each
    from the source that can actually be trusted for it:

      - MEMBERSHIP from any child that lists a STRICT SUBSET of the
        clubs. A child listing every club is the league table wearing a
        conference's name, and is never treated as a roster; its members
        are whatever no genuine roster claimed.
      - STATISTICS from the FRESHEST row for each club across all
        blocks (most games played). Standings only move forward, so
        this resolves the disagreement deterministically and makes one
        club appear with one set of numbers.

    Each club therefore appears exactly once, in one conference. When no
    child is a strict subset (nothing to split on) the honest output is
    a single combined table rather than two invented ones.
    """
    blocks: list[tuple[str, list[dict]]] = []
    for group in d.get("children") or []:
        rows = [_standing_row(e)
                for e in (group.get("standings") or {}).get("entries") or []]
        blocks.append((group.get("name"), [r for r in rows if r["_key"]]))
    if not blocks:
        return []

    freshest: dict[str, dict] = {}
    for _, rows in blocks:
        for r in rows:
            cur = freshest.get(r["_key"])
            if cur is None or (r["played"] or 0) > (cur["played"] or 0):
                freshest[r["_key"]] = r
    pool = set(freshest)

    rosters = {name: {r["_key"] for r in rows} for name, rows in blocks}
    genuine = {n: m for n, m in rosters.items() if 0 < len(m) < len(pool)}
    if not genuine:
        # every block spans the whole league: there is nothing to split
        # on, and inventing a split would repeat the bug in a new shape
        return [{"conference": blocks[0][0] or "MLS",
                 "entries": _ranked([dict(freshest[k]) for k in pool])}]
    claimed: set[str] = set().union(*genuine.values())

    out = []
    for name, _rows in blocks:
        members = genuine.get(name)
        if members is None:
            members = pool - claimed          # the unclaimed remainder
            if not members:
                continue                      # a pure duplicate block
        out.append({"conference": name,
                    "entries": _ranked([dict(freshest[k]) for k in members
                                        if k in freshest])})
    for e in out:
        for r in e["entries"]:
            r.pop("_key", None)
    return out


def parse_game_books(events: list[dict],
                     markets_by_event: dict[str, list[dict]]) -> list[dict]:
    """Kalshi KXMLSGAME events + their markets -> per-fixture 3-way books
    with BOTH sides of each book (ask to buy, bid to exit — the bid/ask
    discipline is day-one policy for every new competition)."""
    out = []
    for ev in events:
        ticker = ev.get("event_ticker")
        rows = []
        for m in markets_by_event.get(ticker, []):
            rows.append({
                "ticker": m.get("ticker"),
                "label": m.get("yes_sub_title") or m.get("title"),
                "yes_ask": m.get("yes_ask_dollars"),
                "yes_bid": m.get("yes_bid_dollars"),
                "status": m.get("status"),
            })
        out.append({"event_ticker": ticker, "title": ev.get("title"),
                    "markets": rows})
    return out


# --- fetchers --------------------------------------------------------------

def scoreboard(date: str | None = None) -> list[dict]:
    """Normalized fixtures for one day (YYYYMMDD; ESPN's default bucket
    when omitted). 60s cache."""
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
        return parse_standings(d) if d else None
    return _cached("standings", 300, fetch) or []


TRADEABLE = ("active", "open", "initialized")


def _game_events_meta(limit: int = 60):
    """The KXMLSGAME event list — one cheap call, 120s cache. NO status
    filter: an in-play fixture's event stops reporting "open" while its
    markets keep trading as "active" (found live on MLS night one — the
    CLB-NYC book was active at 38/33/30 while the open-filtered list
    omitted it). Tradability is judged per MARKET, at market-fetch time.

    Returns (events, meta) — the meta travels so composed callers can
    report the OLDEST constituent honestly (journal-P0-B)."""
    def fetch():
        d = _get_json(f"{KALSHI_BASE}/events",
                      {"series_ticker": KALSHI_MLS_GAME, "limit": limit})
        return (d.get("events") or []) if d else None
    data, meta = _cached_meta("events", PRICE_PATH_TTLS["events"], fetch)
    return (data or []), meta


def _game_events(limit: int = 60) -> list[dict]:
    return _game_events_meta(limit)[0]


def event_markets_meta(event_ticker: str):
    """One event's tradeable markets, 15s cache — cheap enough for the
    match page's 30s poll to ride. Returns (markets, meta)."""
    def fetch():
        # limit 50: correct-score events carry 30+ markets
        md = _get_json(f"{KALSHI_BASE}/markets",
                       {"event_ticker": event_ticker, "limit": 50})
        if md is None:
            return None
        return [m for m in (md.get("markets") or [])
                if m.get("status") in TRADEABLE]
    data, meta = _cached_meta(f"mkts:{event_ticker}",
                              PRICE_PATH_TTLS["mkts"], fetch)
    return (data or []), meta


def event_markets(event_ticker: str) -> list[dict]:
    return event_markets_meta(event_ticker)[0]


def game_books(limit: int = 60) -> list[dict]:
    """Every fixture's tradeable book (the dashboard grid). Assembled
    from the shared event list + per-event market caches, so the match
    page and the dashboard amortize the same fetches."""
    events = _game_events(limit)
    markets = {ev["event_ticker"]: event_markets(ev["event_ticker"])
               for ev in events}
    books = parse_game_books(events, markets)
    return [b for b in books if b["markets"]]   # drop settled fixtures


def cup_futures() -> list[dict]:
    """KXMLSCUP championship futures. 300s cache."""
    def fetch():
        d = _get_json(f"{KALSHI_BASE}/events",
                      {"series_ticker": KALSHI_MLS_CUP, "limit": 5,
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
            time.sleep(0.2)
        return parse_game_books(events, markets)
    return _cached("cup", 300, fetch) or []


# --- per-match summary (the stat page) -------------------------------------

# boxscore stats worth showing, in display order
STAT_ORDER = (
    ("possessionPct", "Possession %"),
    ("totalShots", "Shots"),
    ("shotsOnTarget", "On target"),
    ("wonCorners", "Corners"),
    ("foulsCommitted", "Fouls"),
    ("offsides", "Offsides"),
    ("yellowCards", "Yellow cards"),
    ("redCards", "Red cards"),
    ("saves", "Saves"),
)


def parse_summary(d: dict) -> dict:
    """ESPN event summary -> {header, stats, events}. Tolerant of missing
    sections (pre-match summaries carry empty boxscores)."""
    header = d.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    sides: dict[str, dict] = {}
    id_to_side: dict[str, str] = {}
    for c in comp.get("competitors") or []:
        t = c.get("team") or {}
        side = c.get("homeAway", "?")
        id_to_side[str(t.get("id"))] = side
        sides[side] = {
            "name": t.get("displayName"), "abbrev": t.get("abbreviation"),
            "logo": t.get("logos", [{}])[0].get("href")
            if t.get("logos") else t.get("logo"),
            "score": c.get("score"),
        }
    status = comp.get("status") or {}
    stype = status.get("type") or {}

    by_side: dict[str, dict] = {}
    for team in (d.get("boxscore") or {}).get("teams") or []:
        side = id_to_side.get(str((team.get("team") or {}).get("id")))
        if side:
            by_side[side] = {s.get("name"): s.get("displayValue")
                             for s in team.get("statistics") or []}
    stats = []
    for key, label in STAT_ORDER:
        h = by_side.get("home", {}).get(key)
        a = by_side.get("away", {}).get(key)
        if h is not None or a is not None:
            stats.append({"key": key, "label": label, "home": h, "away": a})

    events = []
    for kev in d.get("keyEvents") or []:
        events.append({
            "minute": (kev.get("clock") or {}).get("displayValue"),
            "type": (kev.get("type") or {}).get("text"),
            "team": (kev.get("team") or {}).get("displayName"),
            "text": kev.get("text"),
            "scoring": bool(kev.get("scoringPlay")),
        })

    return {
        "id": str((header.get("id") or d.get("meta", {}).get("id") or "")),
        "date": comp.get("date"),
        "state": stype.get("state"),
        "detail": stype.get("shortDetail"),
        "minute": status.get("displayClock"),
        "venue": ((d.get("gameInfo") or {}).get("venue") or {}).get("fullName"),
        "home": sides.get("home", {}),
        "away": sides.get("away", {}),
        "stats": stats,
        "events": events,
        "scouting": {"last_five": _parse_last_five(d),
                     "head_to_head": _parse_h2h(d)},
    }


def parse_team_colors(d: dict) -> dict[str, dict]:
    """ESPN teams payload -> {abbrev: {color, alt}} hex strings."""
    out = {}
    leagues = (d.get("sports") or [{}])[0].get("leagues") or [{}]
    for t in leagues[0].get("teams") or []:
        team = t.get("team") or {}
        ab = team.get("abbreviation")
        if ab:
            out[ab] = {"color": team.get("color"),
                       "alt": team.get("alternateColor")}
    return out


def team_colors() -> dict[str, dict]:
    """Club signature colors (1h cache — they change never)."""
    def fetch():
        d = _get_json(f"{ESPN_BASE}/teams")
        return parse_team_colors(d) if d else None
    return _cached("team_colors", 3600, fetch) or {}


def raw_summary(event_id: str) -> dict | None:
    """The RAW ESPN summary payload, cached separately from the parsed
    view. The lineup section needs fields parse_summary doesn't keep
    (rosters), and sharing one cache keeps it to one upstream fetch."""
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


# --- match hub: scouting + the fixture's own Kalshi book -------------------

def _last_five_scores(e: dict) -> tuple[int | None, int | None]:
    """This team's goals and the opponent's, for one lastFiveGames event.

    ESPN's `score` STRING is WINNER-FIRST (the higher score leads),
    NOT the team's own score first and NOT home-away order. Rendering it
    beside the perspective result letter inverted every defeat: a 0-1
    home loss displayed as "L 1-0", which reads as a win (reported Jul 24,
    2026). Wins and draws happen to look right, which is exactly why it
    survived so long.

    The authoritative fields are homeTeamScore/awayTeamScore plus atVs
    ("vs" = this team was home, "@" = away); derive both sides from those
    and never trust the pre-formatted string."""
    def _num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    home, away = _num(e.get("homeTeamScore")), _num(e.get("awayTeamScore"))
    if home is None or away is None:
        return None, None
    at_home = e.get("atVs") == "vs"
    return (home, away) if at_home else (away, home)


def _parse_last_five(d: dict) -> list[dict]:
    """lastFiveGames -> per team: form string + recent results.

    Emits `team_score`/`opponent_score` (this team's goals first) so the
    UI never has to reinterpret a provider string. `score` is retained
    verbatim as raw provenance only — it must not be displayed."""
    out = []
    for t in d.get("lastFiveGames") or []:
        evs = t.get("events") or []
        games = []
        for e in evs[:5]:
            ts, os_ = _last_five_scores(e)
            # DERIVE the W/L/D from the very numbers displayed next to it,
            # so the letter and the scoreline cannot contradict each other
            # no matter how the provider reorders its fields. The provider's
            # own letter is kept only for drift detection.
            if ts is not None and os_ is not None:
                result = "W" if ts > os_ else "L" if ts < os_ else "D"
            else:
                result = e.get("gameResult")
            games.append({
                "result": result,
                "provider_result": e.get("gameResult"),   # audit only
                "team_score": ts,
                "opponent_score": os_,
                "score": e.get("score"),      # raw provider string; unused
                "at_vs": e.get("atVs"),
                "opponent": (e.get("opponent") or {}).get("abbreviation"),
                "date": e.get("gameDate"),
            })
        out.append({
            "team": (t.get("team") or {}).get("displayName"),
            "abbrev": (t.get("team") or {}).get("abbreviation"),
            "form": " ".join(g["result"] or "?" for g in games),
            "games": games,
        })
    return out


def scoreline_disagreements(summary: dict) -> list[dict]:
    """Self-consistency audit for every scouting row we render: does the
    W/L/D letter agree with the two scores shown next to it?

    This is the systemic guard against the class of bug that hit twice —
    a provider silently reordering or renaming score fields. Any
    disagreement is returned (empty list = consistent), so a test can
    fail loudly instead of the UI quietly showing a loss as a win."""
    bad = []
    for t in _parse_last_five(summary):
        for g in t["games"]:
            ts, os_ = g["team_score"], g["opponent_score"]
            provider = g.get("provider_result")
            if ts is None or os_ is None or not provider:
                continue
            # what the scores say vs what the provider labelled it. The UI
            # renders the derived value, so a mismatch here is PROVIDER
            # DRIFT to investigate, never a wrong number on the page.
            expect = "W" if ts > os_ else "L" if ts < os_ else "D"
            if expect != provider:
                bad.append({"block": "last_five", "team": t["abbrev"],
                            "opponent": g["opponent"],
                            "provider_result": provider, "derived": expect,
                            "team_score": ts, "opponent_score": os_})
    for g in _parse_h2h(summary):
        h, a, r = g["home_score"], g["away_score"], g["result"]
        try:
            h, a = int(h), int(a)
        except (TypeError, ValueError):
            continue
        # scores are in match home-away order; at_vs says which side the
        # perspective team was on
        ts, os_ = (h, a) if g["at_vs"] == "vs" else (a, h)
        expect = "W" if ts > os_ else "L" if ts < os_ else "D"
        if expect != r:
            bad.append({"block": "h2h", "team": g["perspective"],
                        "opponent": g["opponent"], "result": r,
                        "team_score": ts, "opponent_score": os_})
    return bad


def _parse_h2h(d: dict) -> list[dict]:
    """Recent meetings between the two clubs, from one team's view.

    ESPN RENAMED this feed (observed Jul 24, 2026): the old
    `headToHeadGames` block is gone and the data now arrives as
    `seasonseries` (a list whose head-to-head entry carries `events` with
    per-competitor scores). Both shapes are parsed — legacy first, so a
    restored old field keeps working — and both emit the SAME contract the
    match page renders: perspective/result/home_score/away_score/at_vs/
    opponent/date."""
    legacy = d.get("headToHeadGames") or []
    if legacy:
        team = (legacy[0].get("team") or {}).get("abbreviation")
        return [{
            "perspective": team,
            "result": e.get("gameResult"),
            "home_score": e.get("homeTeamScore"),
            "away_score": e.get("awayTeamScore"),
            "at_vs": e.get("atVs"),
            "opponent": (e.get("opponent") or {}).get("abbreviation"),
            "date": e.get("gameDate"),
        } for e in (legacy[0].get("events") or [])[:6]]

    series = [s for s in (d.get("seasonseries") or [])
              if (s.get("type") or "").lower() in
              ("head-to-head", "headtohead")] or (d.get("seasonseries") or [])
    if not series:
        return []
    events = [e for e in (series[0].get("events") or [])
              if (e.get("statusType") or {}).get("completed")]
    if not events:
        return []
    # perspective = the team in THIS fixture we describe results from; use
    # the home side of the most recent meeting so `at_vs` is meaningful
    persp = None
    for c in (events[0].get("competitors") or []):
        if c.get("homeAway") == "home":
            persp = (c.get("team") or {}).get("abbreviation")
    out = []
    for e in events[:6]:
        comps = {c.get("homeAway"): c for c in (e.get("competitors") or [])}
        home, away = comps.get("home") or {}, comps.get("away") or {}
        h_ab = (home.get("team") or {}).get("abbreviation")
        a_ab = (away.get("team") or {}).get("abbreviation")
        if persp is None:
            persp = h_ab
        # result from the SCORES shown next to it (falling back to the
        # provider's winner flag only when a score is missing), so the
        # letter can never contradict the numbers on the page
        me = home if h_ab == persp else away
        opp = away if h_ab == persp else home

        def _n(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        my_s, opp_s = _n(me.get("score")), _n(opp.get("score"))
        if my_s is not None and opp_s is not None:
            result = "W" if my_s > opp_s else "L" if my_s < opp_s else "D"
        elif me.get("winner"):
            result = "W"
        elif opp.get("winner"):
            result = "L"
        else:
            result = "D"
        out.append({
            "perspective": persp,
            "result": result,
            "home_score": home.get("score"),
            "away_score": away.get("score"),
            "at_vs": "vs" if h_ab == persp else "@",
            "opponent": a_ab if h_ab == persp else h_ab,
            "date": e.get("date"),
        })
    return out


# Kalshi title-name -> ESPN displayName bridges that substring matching
# cannot cross. Verified against the live KXMLSGAME slate 2026-07-22.
_KALSHI_ALIASES = {
    "los angeles g": "la galaxy",
    # ESPN's displayName is "LAFC" — NOT "Los Angeles FC". The old
    # target never matched anything, so every LAFC fixture showed no
    # book (VAN-LAFC, Aug 1 slate). Same defect as the RBNY line below,
    # written against an assumed ESPN name instead of a measured one.
    "los angeles f": "lafc",
    "saint louis": "st louis city",
    # ESPN's displayName is "Red Bull New York", NOT "New York Red
    # Bulls" — the old alias never matched, so every RBNY fixture
    # showed "no open book" (caught on Son's phone, RBNY-CLT Jul 25)
    "new york rb": "red bull new york",
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
    """KXMLSGAME-26JUL25SJLAG -> '26JUL25' (Kalshi dates are US-Eastern)."""
    import re
    m = re.match(r"KXMLSGAME-(\d{2}[A-Z]{3}\d{2})", event_ticker or "")
    return m.group(1) if m else None


def _fixture_et_date(iso_date: str) -> str | None:
    """ESPN UTC kickoff -> the Kalshi-style US-Eastern date segment.
    Real wall-clock Eastern time: a fixed UTC-4 offset misdates
    late-evening fixtures once DST ends Nov 1 (playoffs)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    et = dt.astimezone(ZoneInfo("America/New_York"))
    return et.strftime("%y%b%d").upper()


# --- every per-match Kalshi family (discovered live Jul 23: 17 MLS
# series, 12 of them per-match) -------------------------------------------

MATCH_FAMILIES = [
    ("winner", "KXMLSGAME", "Winner · 3-way"),
    ("total", "KXMLSTOTAL", "Total goals"),
    ("btts", "KXMLSBTTS", "Both teams to score"),
    ("spread", "KXMLSSPREAD", "Spread"),
    ("team_total", "KXMLSTEAMTOTAL", "Team totals"),
    ("score", "KXMLSSCORE", "Correct score"),
    ("ftts", "KXMLSFTTS", "First team to score"),
    ("mov", "KXMLSMOV", "Method of victory"),
    ("h1", "KXMLS1H", "1st half · winner"),
    ("h1_total", "KXMLS1HTOTAL", "1st half · total"),
    ("h1_spread", "KXMLS1HSPREAD", "1st half · spread"),
    ("h1_btts", "KXMLS1HBTTS", "1st half · BTTS"),
]


def model_key_for(series: str, ticker: str, suffix_codes: str) -> str | None:
    """One Kalshi market ticker -> the model's probability key, parsed
    from the machine-readable ticker TAIL (never the display label).
    suffix_codes = the letters after the date in the event suffix
    ("CLBCIN"), used to tell home codes from away codes: the suffix is
    home-first, so a tail code that PREFIXES it is the home side (this
    also survives NYRB/NYC-style near-collisions). None = the model has
    no number for this market; it renders market-only."""
    import re as _re
    tail = (ticker or "").rsplit("-", 1)[-1]
    if series == "KXMLSBTTS":
        return "btts" if tail == "BTTS" else None
    if series == "KXMLSTOTAL":
        # tail 1 = over 0.5 ... tail 6 = over 5.5
        return f"over_{int(tail) - 1}_5" if tail.isdigit() else None
    if series == "KXMLSSPREAD":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        # tail n = "wins by more than n-0.5" = margin >= n
        return f"{side}_margin_{int(m.group(2))}"
    if series == "KXMLSTEAMTOTAL":
        m = _re.match(r"^([A-Z]+)(\d+)$", tail)
        if not m:
            return None
        side = "home" if suffix_codes.startswith(m.group(1)) else "away"
        return f"{side}_team_over_{int(m.group(2)) - 1}_5"
    if series == "KXMLSSCORE":
        m = _re.match(r"^([A-Z]+)(\d+)([A-Z]+)(\d+)$", tail)
        if not m:
            return None                     # "any other" style tails
        h, a = int(m.group(2)), int(m.group(4))
        if not suffix_codes.startswith(m.group(1)):
            h, a = a, h                     # defensive: observed home-first
        return f"score_{h}_{a}"
    if series == "KXMLSFTTS":
        if tail in ("NONE", "NEITHER", "NOGOAL"):
            return "no_goal"
        if tail.isalpha():
            side = "home" if suffix_codes.startswith(tail) else "away"
            return f"{side}_first_goal"
    return None                              # MOV + 1H: market-only


def find_all_books(fixture_date: str, home_name: str,
                   away_name: str) -> list[dict]:
    """Every Kalshi market family for one fixture. The GAME event is
    located by the approved-name matching; every other family shares
    its ticker suffix ({date}{HOME}{AWAY}), so the join is exact and
    needs no name resolution at all. 30s bundle cache."""
    books, _meta = find_all_books_with_freshness(
        fixture_date, home_name, away_name)
    return books


def find_all_books_with_freshness(
        fixture_date: str, home_name: str,
        away_name: str) -> tuple[list[dict], dict]:
    """`find_all_books` plus the freshness meta a truthful caller needs
    (journal-P0 F4/P0-B): (books, {observed_at, age_seconds, status}).

    The bundle is COMPOSED from inner caches (the event list, each
    family's markets), every one of which serves stale on a failed
    refresh. So the meta is computed over EVERY constituent's store
    time and freshness — the aggregate status is the least trustworthy
    constituent, and age is the OLDEST constituent's, never assembly
    time. Past `MLS_PRICE_MAX_AGE_SECONDS` this fails CLOSED — status
    `unavailable`, empty books, no price presented as current."""
    game, game_metas = find_book_with_meta(fixture_date, home_name,
                                           away_name)
    if game is None:
        return [], {"observed_at": None, "age_seconds": None,
                    "status": "unavailable"}
    suffix = game["event_ticker"].split("-", 1)[1]
    suffix_codes = suffix[7:]                # strip the YYMONDD date

    def fetch():
        # constituent metas are stored WITH the bundle so a cached
        # serve still reports the build-time constituents' ages
        metas = [m for m in game_metas if m]
        fams = [{"key": "winner", "label": "Winner · 3-way",
                 "event_ticker": game["event_ticker"],
                 "markets": [dict(r, model_key=None)
                             for r in game["markets"]]}]
        for key, series, label in MATCH_FAMILIES:
            if series == "KXMLSGAME":
                continue
            ticker = f"{series}-{suffix}"
            ms, m_meta = event_markets_meta(ticker)
            if m_meta:
                metas.append(m_meta)
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
        return {"fams": fams, "metas": metas}
    bundle, outer_meta = _cached_meta(
        f"allbooks:{suffix}", PRICE_PATH_TTLS["allbooks"], fetch)
    if bundle is None:
        return [], {"observed_at": None, "age_seconds": None,
                    "status": "unavailable"}
    # every constituent counts: the bundle's build-time inputs, the
    # bundle store itself, and the CURRENT call's game-location inputs
    # (the current event list may be staler than the stored bundle)
    metas = list(bundle["metas"]) + [outer_meta] + \
        [m for m in game_metas if m]
    meta = aggregate_freshness(metas, config.MLS_PRICE_MAX_AGE_SECONDS)
    books = bundle["fams"] if meta["status"] != "unavailable" else []
    return books, meta


def find_book_with_meta(fixture_date: str, home_name: str,
                        away_name: str,
                        books: list[dict] | None = None):
    """`find_book` plus the constituent cache metas that produced the
    answer (journal-P0-B): (game|None, [metas]). The metas let a
    composed caller report the oldest constituent instead of stamping
    assembly time."""
    want = _fixture_et_date(fixture_date)
    metas: list = []
    if books is not None:               # injected (tests)
        pool = books
    else:
        # two cheap calls instead of a full sweep: match on the EVENT
        # list first, then fetch only this fixture's markets
        evs, ev_meta = _game_events_meta()
        if ev_meta:
            metas.append(ev_meta)
        pool = [{"event_ticker": ev.get("event_ticker"),
                 "title": ev.get("title"), "markets": None}
                for ev in evs]
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
                ms, m_meta = event_markets_meta(b["event_ticker"])
                if m_meta:
                    metas.append(m_meta)
                b = parse_game_books(
                    [b], {b["event_ticker"]: ms})[0]
            return (b if b["markets"] else None), metas
    return None, metas


def find_book(fixture_date: str, home_name: str, away_name: str,
              books: list[dict] | None = None) -> dict | None:
    """This fixture's KXMLSGAME book: ET-date segment must match the
    ticker (teams meet twice inside one open window — SJ played Jul 22
    AND Jul 25 on the day this shipped), then both title sides must
    match the ESPN names."""
    return find_book_with_meta(fixture_date, home_name, away_name,
                               books=books)[0]
