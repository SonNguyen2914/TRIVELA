"""Viewer competitions — fixtures, Kalshi books, and the cross-league
strength read, for competitions that have NO model here.

WHY A SHARED MODULE. src/ecl.py was written first and would have been
copy-pasted five more times. The three league match pages in the frontend
are already a cautionary tale about that: they drifted apart on fee
arithmetic, and only one of them is correct. One implementation, a
registry of competitions, no copies.

WHAT A "VIEWER" IS, AND IS NOT. These competitions get real fixtures, real
Kalshi prices and an EXTERNAL strength read. They get no model, no approval
decision and no odds board. But the REASON differs by competition, and
collapsing the two would be dishonest:

  BY_DESIGN   a cup. Most entrants play two or three qualifying matches
              and go home, so a fitted rating can never cover the field.
              Measured on ECL 2025: 164 clubs, MEDIAN 4 matches each, 53
              clearing the 5-match floor. Waiting does not fix this — it is
              the shape of the competition.

  NOT_BUILT   a round-robin league where a model IS viable (30-38 matches
              per club against one population), and simply has not been
              built. Brasileirão, Argentina and USL are here. Saying
              "by design" about these would be a lie about our own backlog.

Every ticker below was PROBED against Kalshi's series endpoint, never
guessed from the competition name. That is not pedantry: KXSERIEAGAME is
ITALY's Serie A, so the plausible-looking guess for Brasileirão would have
attached Italian markets to Brazilian fixtures. The real ones are
KXBRASILEIROGAME and KXARGPREMDIVGAME, found by enumerating Kalshi's 235
GAME series rather than by pattern-matching the name.
"""
from __future__ import annotations

import threading
import time

BY_DESIGN = "no_model_by_design"
NOT_BUILT = "no_model_not_built"

_BY_DESIGN_WHY = (
    "a cup with qualifying rounds gives most entrants 2-4 matches, so a "
    "rating fitted on this competition's own fixtures would refuse the "
    "majority of the field. This is permanent — it is the shape of the "
    "competition, not a season that has not started yet")
_NOT_BUILT_WHY = (
    "a round-robin league where a fitted model IS viable — every club "
    "plays the same population 30-38 times. It has not been built here "
    "yet. That is a fact about our backlog, not about the competition")
_INSTEAD = (
    "the cross-league strength read is used meanwhile, because a club's "
    "strength lives in the league it plays week to week")


class Viewer:
    def __init__(self, key, display, apif_league_id, kalshi_series,
                 no_model, accent="#7dd3fc", note=None, why=None,
                 strength_kind="club", season_default=None):
        self.key = key
        self.display = display
        self.apif_league_id = apif_league_id
        self.kalshi_series = kalshi_series      # PROBED, see module docstring
        self.no_model = no_model
        self.accent = accent
        self.note = note
        # "club" reads ClubElo/worldclubratings; "national" reads
        # eloratings.net. A national team run through a CLUB provider can
        # only ever produce a false match, so the kind is pinned on the
        # competition, never inferred from a name.
        self.strength_kind = strength_kind
        # the season label the FIXTURE PROVIDER uses for the current
        # edition, when it differs from the calendar year. API-Football
        # files the Aug-2026 ASEAN Championship under season 2025 (probed
        # 2026-08-04: season 2025 = 22 fixtures, season 2026 = 0), so a
        # caller defaulting to the calendar year would see an empty
        # tournament that is actually mid-group-stage.
        self.season_default = season_default
        # a competition-specific reason. The shared BY_DESIGN text says
        # "qualifying rounds give most entrants 2-4 matches" — TRUE for
        # the UEFA cups, FALSE for Leagues Cup, where every club is
        # well-rated by its own league's model and the refusal is about
        # the UNITS, not the sample. One wrong sentence under a heading
        # is worse than none.
        self.why = why
        # which ratings providers may serve this competition's strength
        # read. None = all. Leagues Cup and the non-European leagues pin
        # to worldclubratings: ClubElo is European club football only, so
        # consulting it there can only produce a false match.
        self.strength_sources = None

    def model_block(self) -> dict:
        return {
            "state": self.no_model,
            "why": self.why or (_BY_DESIGN_WHY if self.no_model == BY_DESIGN
                                else _NOT_BUILT_WHY),
            "instead": _INSTEAD,
            "note": self.note,
        }


VIEWERS: dict[str, Viewer] = {
    "ecl": Viewer(
        "ecl", "UEFA Europa Conference League", 848, "KXUECLGAME",
        BY_DESIGN, "#7dd3fc",
        note=("measured on the 2025 edition: 409 fixtures, 164 clubs, "
              "median 4 matches per club, 53 clearing the 5-match floor")),
    "uel": Viewer(
        "uel", "UEFA Europa League", 3, "KXUELGAME", BY_DESIGN, "#fb923c"),
    "ucl": Viewer(
        "ucl", "UEFA Champions League", 2, "KXUCLGAME", BY_DESIGN, "#a5b4fc"),
    "brasileirao": Viewer(
        "brasileirao", "Brasileirão Série A", 71, "KXBRASILEIROGAME",
        NOT_BUILT, "#4ade80"),
    "argentina": Viewer(
        "argentina", "Liga Profesional Argentina", 128, "KXARGPREMDIVGAME",
        NOT_BUILT, "#7dd3fc"),
    "usl": Viewer(
        "usl", "USL Championship", 255, "KXUSLGAME", NOT_BUILT, "#f472b6"),
    # MLS clubs against Liga MX clubs. BOTH approved models cover these
    # clubs — and neither may price a single one of these fixtures,
    # because each model's ratings are relative to ITS OWN league's mean
    # and the codebase refuses that arithmetic by construction
    # (ScopedStrength raises CrossCompetitionArithmetic). That refusal is
    # the honest position: no conversion between the two scales has been
    # fitted or published, and picking one implicitly is exactly how a
    # confident number about nothing gets made. The cross-league strength
    # read exists for precisely this case. Ticker PROBED 2026-08-03: 31
    # tradeable events on KXLEAGUESCUPGAME, a 12-series family.
    "leagues-cup": Viewer(
        "leagues-cup", "Leagues Cup", 772, "KXLEAGUESCUPGAME",
        BY_DESIGN, "#facc15",
        why=("every club here IS well-rated — by its own league's model, "
             "relative to its own league's mean. Those two scales have no "
             "fitted conversion, and this codebase refuses cross-league "
             "arithmetic by construction rather than guessing one. A "
             "number produced by mixing them would be confident about "
             "nothing"),
        note=("probed 2026-08-03: 31 tradeable KXLEAGUESCUPGAME events; "
              "the MLS fit is scoped to competition_slug='mls-2026', so "
              "these cross-league results cannot leak into its ratings")),
    # NATIONAL teams, the first non-club competition on this surface.
    # Every model and both club ratings providers rate CLUBS, so the
    # strength read here is World Football Elo (eloratings.net), which
    # rates national teams and only national teams — strength_kind pins
    # that structurally. Ticker PROBED 2026-08-04: KXASEANGAME, an
    # 18-event board inside a 5-series family (GAME/BTTS/ADVANCE/SPREAD/
    # TOTAL). Season pinned 2025 — see season_default on the class.
    "asean": Viewer(
        "asean", "ASEAN Championship", 24, "KXASEANGAME",
        BY_DESIGN, "#fbbf24",
        why=("a national-team cup. Every fitted model in this codebase "
             "rates clubs within one league's population; no national-"
             "team model exists here and none is claimed. The strength "
             "read is eloratings.net's published national Elo — an "
             "external rating, not a model of ours"),
        note=("probed 2026-08-04: 18 listed KXASEANGAME events; all "
              "eleven ASEAN federations carry a current eloratings.net "
              "rating, Thailand 1395 down to Brunei 572"),
        strength_kind="national", season_default=2025),
}

# ClubElo is European club football only. For these competitions every
# entrant is non-European, so the read runs on worldclubratings alone —
# a false ClubElo match cannot even be attempted.
for _k in ("leagues-cup", "brasileirao", "argentina", "usl"):
    VIEWERS[_k].strength_sources = ("worldclubratings",)

FRAMING = (
    "Fixtures, real Kalshi prices, and an EXTERNAL strength read. No model "
    "runs on this surface and no approval decision exists for it, so no "
    "model number appears anywhere. Nothing here is a recommendation.")

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
CACHE_TTL = 300.0
FINISHED = {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "WO", "PST"}


def _cached(key: str, ttl: float, fetch):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    data = fetch()
    if data is not None:
        with _lock:
            _cache[key] = (now, data)
    return data


def _fixtures_raw(league_id: int, season: int) -> list[dict] | None:
    """League-scoped fixture read. None on FAILURE (no key, transport,
    non-200) — which callers must never cache or read as "no fixtures" —
    and [] only when the provider genuinely answered with an empty
    season. League scoping is correct here — unlike friendlies, where
    branded pre-season tournaments each get their own league id and only
    a date sweep sees them."""
    import requests

    from src.friendlies_apif import (APIF_BASE, APIF_TIMEOUT, load_key,
                                     parse_fixture, redact, response_items)
    key, _s, _p = load_key()
    if not key:
        return None
    try:
        r = requests.get(f"{APIF_BASE}/fixtures",
                         params={"league": str(league_id),
                                 "season": str(season)},
                         headers={"x-apisports-key": key},
                         timeout=APIF_TIMEOUT)
        if r.status_code != 200:
            return None
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[comp {league_id}] fixtures: {redact(str(exc), key)[:140]}")
        return None
    return [p for p in (parse_fixture(x) for x in response_items(body))
            if p is not None]


def fixtures(key: str, season: int | None = None, days: int | None = None,
             include_finished: bool = False) -> dict | None:
    v = VIEWERS.get(key)
    if v is None:
        return None
    # None -> the provider's label for the current edition when the
    # registry pins one, else the calendar-year convention.
    season = season or v.season_default or 2026

    def _run():
        raw = _fixtures_raw(v.apif_league_id, season)
        if raw is None:
            # provider FAILURE — return None so _cached stores nothing;
            # caching an empty board for the TTL turned one transient
            # hiccup into five minutes of "no fixtures" (found on the
            # Leagues Cup board, matchday 1, 2026-08-04)
            return None
        rows = []
        for f in raw:
            row = dict(f)
            try:
                from src.friendlies_apif import _slim_strength
                if v.strength_kind == "national":
                    from src.live import national_elo
                    row["strength"] = _slim_strength(
                        national_elo.for_fixture(f))
                else:
                    from src.live import club_strength_estimate as cse
                    # apply_calibration=False: the shrink is a FRIENDLIES
                    # measurement and these are competitive fixtures — the
                    # helper's Aug-4 briefing caught the leak (slope 0.185)
                    row["strength"] = _slim_strength(
                        cse.for_fixture(f, sources=v.strength_sources,
                                        apply_calibration=False))
            except Exception as exc:
                row["strength"] = {"available": False,
                                   "reason": "estimate_unavailable",
                                   "detail": str(exc)[:140]}
            rows.append(row)
        from datetime import datetime, timedelta, timezone as _tz
        soon = (datetime.now(_tz.utc) + timedelta(hours=48)).isoformat()
        # weather reaches further than news: captures firm up near
        # kickoff, but a forecast is exactly the thing an operator wants
        # while eyeing a price days ahead, and the provider's hourly
        # horizon (16d) comfortably covers a week
        soon_weather = (datetime.now(_tz.utc) + timedelta(days=7)).isoformat()
        now_s = datetime.now(_tz.utc).isoformat()
        for r in rows:
            # captured absences, for near fixtures only — a read, never a
            # fetch; `never_captured` is passed through honestly
            if now_s <= (r.get("kickoff_utc") or "") <= soon:
                try:
                    from src.live import team_news
                    n = team_news.fixture_news(str(r.get("fixture_id")))
                    if n:
                        r["news"] = n
                except Exception:
                    pass
            if now_s <= (r.get("kickoff_utc") or "") <= soon_weather:
                try:
                    from src.live import match_weather
                    r["weather"] = match_weather.for_fixture(
                        r.get("venue_city"), r.get("kickoff_utc"))
                except Exception:
                    pass
            try:
                r["meaning"] = _meaning(r, rows, v.key)
            except Exception as exc:
                r["meaning"] = {"available": False,
                                "reason": f"{type(exc).__name__}"}
        rows.sort(key=lambda r: (r.get("kickoff_utc") or ""))
        return rows

    rows = _cached(f"comp:{key}:{season}", CACHE_TTL, _run) or []

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    finished = [r for r in rows if (r.get("status") in FINISHED
                                    or (r.get("kickoff_utc") or "") < now_iso)]
    if not include_finished:
        ids = {id(r) for r in finished}
        rows = [r for r in rows if id(r) not in ids]
    if days:
        cut = (now + timedelta(days=days)).isoformat()
        rows = [r for r in rows if (r.get("kickoff_utc") or "") <= cut]

    # The market beside the read. Bridged HERE rather than client-side: the
    # fixture list and the Kalshi event list are two different id spaces, and
    # joining them in the browser is the fragile pattern the friendlies
    # dashboard was rewired away from.
    try:
        mk = markets(key) or {}
        events = mk.get("events") or []
        if events:
            from src.live import match_pick
            from src import friendlies as _fr
            # BOTH sides must match, never one. A single shared token
            # attaches Real Sociedad's book to Real Madrid — the exact
            # mis-bridge this repo refuses in three other places. One
            # matched side is NOT evidence the event is this fixture.
            titled = [(e, _fr._identity_tokens(e.get("title") or ""))
                      for e in events]
            for r in rows:
                hn = (r.get("home") or {}).get("name") or ""
                an = (r.get("away") or {}).get("name") or ""
                ht, at = _fr._identity_tokens(hn), _fr._identity_tokens(an)
                if not ht or not at:
                    continue
                hits = [e for e, toks in titled
                        if (ht & toks) and (at & toks)]
                # ambiguity is a refusal, not a coin flip between books
                if len(hits) != 1:
                    if len(hits) > 1:
                        r["market_vs_read"] = {
                            "available": False,
                            "market_unavailable_reason": (
                                f"{len(hits)} Kalshi events match both clubs "
                                f"by name; none is attached rather than one "
                                f"guessed")}
                    continue
                ev = hits[0]
                r["market_vs_read"] = match_pick.compare(
                    r.get("strength"), ev, hn, an)
                r["kalshi_event"] = ev.get("event_ticker")
    except Exception as exc:
        print(f"[comp {key}] market_vs_read: {exc}")

    from src.friendlies_apif import strength_notes
    return {
        "competition": v.key, "display": v.display,
        "apif_league_id": v.apif_league_id, "season": season,
        "accent": v.accent,
        "fixtures": rows, "count": len(rows),
        "with_strength_read": sum(
            1 for r in rows if (r.get("strength") or {}).get("available")),
        "finished_hidden": len(finished),
        "strength_notes": strength_notes(),
        "model": v.model_block(),
        "framing": FRAMING,
    }


def markets(key: str) -> dict | None:
    v = VIEWERS.get(key)
    if v is None:
        return None

    def _run():
        # a FAILURE returns None so _cached stores nothing — one
        # transient Kalshi error was being served as "unavailable" for
        # the full TTL, which stripped every book off the Leagues Cup
        # board for five minutes on matchday 1 (2026-08-04). The honest
        # unavailable body is built below, OUTSIDE the cache.
        from src import friendlies
        try:
            d = friendlies._get_json(
                f"{friendlies.KALSHI_BASE}/events",
                {"series_ticker": v.kalshi_series, "limit": 200,
                 "with_nested_markets": "true"})
        except Exception:
            return None
        if d is None:
            return None
        events = d.get("events") or []
        # listed vs tradeable stay APART: an EPL probe once returned ten
        # status=open events that were all settled prior-season fixtures.
        open_events = [e for e in events
                       if any(m.get("status") in friendlies.TRADEABLE
                              for m in (e.get("markets") or []))]
        return {"status": "ok", "series": v.kalshi_series,
                "listed_events": len(events),
                "tradeable_events": len(open_events),
                "events": open_events,
                "truncated": bool(d.get("cursor"))}

    out = _cached(f"comp:{key}:markets", CACHE_TTL, _run)
    if out:
        return out
    return {"status": "unavailable", "series": v.kalshi_series,
            "means": ("the registry read FAILED, so whether books are "
                      "listed is UNKNOWN, not 'none open' — retried on "
                      "every request, never cached")}


def status(key: str) -> dict | None:
    v = VIEWERS.get(key)
    if v is None:
        return None
    import config
    return {"competition": v.key, "display": v.display,
            "apif_league_id": v.apif_league_id,
            "kalshi_series": v.kalshi_series,
            "accent": v.accent,
            "model": v.model_block(),
            "framing": FRAMING,
            "real_money_signals": config.REAL_MONEY_SIGNALS_ENABLED}


def listing() -> dict:
    return {"competitions": [
        {"key": v.key, "display": v.display, "accent": v.accent,
         "kalshi_series": v.kalshi_series,
         "model_state": v.no_model} for v in VIEWERS.values()]}


# --- what the match MEANS -----------------------------------------------
# "Friendly", "second leg, 3-1 down", "must win to stay alive" — the
# context a price sits inside. Derived ONLY from the season's own fixture
# list (already fetched for the board), never asserted: an aggregate is
# the sum of two real scorelines or it is absent; a group record is
# counted from finished fixtures or it is absent.

def _meaning(row: dict, season_rows: list[dict], comp_key: str) -> dict:
    rnd = (row.get("round") or "")
    out = {"round": rnd or None}
    hid = (row.get("home") or {}).get("apif_team_id")
    aid = (row.get("away") or {}).get("apif_team_id")

    # A two-legged tie announces itself two ways. UEFA rounds carry the
    # word ("Semi-finals 1st Leg"); the ASEAN Championship's do NOT —
    # its semis and final are two-legged under the bare round names
    # "Semi-finals" and "Final" (probed on the 2024 edition: 4 and 2
    # fixtures). The tie's SIGNATURE still shows in the fixture list:
    # the same pairing reversed within the SAME round. Group-like rounds
    # are excluded because a round-robin's reverse meetings are separate
    # matches, not legs of one tie.
    rl = rnd.lower()
    same_round_reverse = ("group" not in rl and any(
        f is not row and (f.get("round") or "") == rnd
        and (f.get("home") or {}).get("apif_team_id") == aid
        and (f.get("away") or {}).get("apif_team_id") == hid
        for f in season_rows))
    # ASEAN's semis and final are two-legged BY FORMAT. The signature
    # check above needs both legs in the feed — but the provider often
    # publishes leg 1 before leg 2 exists, and in that gap the fixture
    # would fall through to group stakes, which is wrong twice over.
    known_two_leg = (comp_key == "asean"
                     and rl in ("semi-finals", "final"))
    if "qualif" in rl or "leg" in rl or same_round_reverse or known_two_leg:
        # two-legged tie: find the FINISHED reverse fixture this season
        rev = next((f for f in season_rows
                    if f.get("status") in FINISHED - {"CANC", "PST", "ABD"}
                    and (f.get("home") or {}).get("apif_team_id") == aid
                    and (f.get("away") or {}).get("apif_team_id") == hid
                    and (f.get("goals") or {}).get("home") is not None),
                   None)
        if rev:
            g = rev["goals"]
            # aggregate from THIS fixture's home side's view
            out["tie"] = {
                "leg": 2,
                "first_leg": f"{g['home']}-{g['away']}",
                "aggregate_before": f"{g['away']}-{g['home']}",
                "means": (f"second leg. First leg finished "
                          f"{g['home']}-{g['away']} away — "
                          f"{row['home']['name']} "
                          + ("lead" if g['away'] > g['home'] else
                             "trail" if g['away'] < g['home'] else
                             "are level")
                          + " on aggregate"),
            }
        else:
            out["tie"] = {"leg": 1,
                          "means": "first leg of a two-legged tie — the "
                                   "return leg decides it"}
        return out

    if comp_key == "leagues-cup":
        # group-phase records counted from finished fixtures. Leagues Cup
        # awards a SHOOTOUT bonus point on draws which the scoreline
        # alone cannot attribute, so a drawn match widens the range
        # rather than fabricating a rank.
        def record(tid):
            w = l = d = 0
            for f in season_rows:
                if f.get("status") not in ("FT", "AET", "PEN"):
                    continue
                g = f.get("goals") or {}
                if g.get("home") is None:
                    continue
                fh = (f.get("home") or {}).get("apif_team_id")
                fa = (f.get("away") or {}).get("apif_team_id")
                if tid not in (fh, fa):
                    continue
                mine = g["home"] if tid == fh else g["away"]
                theirs = g["away"] if tid == fh else g["home"]
                if mine > theirs: w += 1
                elif mine < theirs: l += 1
                else: d += 1
            lo = 3 * w + d          # shootout lost every draw
            hi = 3 * w + 2 * d      # shootout won every draw
            return {"played": w + l + d, "w": w, "d_shootout": d, "l": l,
                    "points": lo if lo == hi else None,
                    "points_range": [lo, hi] if lo != hi else None}
        out["stakes"] = {
            "format": ("group phase: top four per league advance to the "
                       "knockouts; a drawn match goes to a shootout for a "
                       "bonus point, which the scoreline alone cannot "
                       "attribute — so a drawn record shows a points RANGE "
                       "rather than an invented rank"),
            "home": record(hid), "away": record(aid),
        }
        return out

    if comp_key == "asean" and "group" in rl and "qualif" not in rl:
        # group rounds only — a knockout fixture reaching here would
        # otherwise wear a group record it is not playing under.
        # standard scoring — 3 for a win, 1 for a draw, no shootout —
        # so unlike Leagues Cup the record is a POINT COUNT, not a range
        def record(tid):
            w = l = d = 0
            for f in season_rows:
                if f.get("status") not in ("FT", "AET", "PEN"):
                    continue
                frl = (f.get("round") or "").lower()
                # "Qualification Round Group Stage - 1" contains "group"
                # but is the pre-tournament qualifier, not the group
                if "group" not in frl or "qualif" in frl:
                    continue
                g = f.get("goals") or {}
                if g.get("home") is None:
                    continue
                fh = (f.get("home") or {}).get("apif_team_id")
                fa = (f.get("away") or {}).get("apif_team_id")
                if tid not in (fh, fa):
                    continue
                mine = g["home"] if tid == fh else g["away"]
                theirs = g["away"] if tid == fh else g["home"]
                if mine > theirs: w += 1
                elif mine < theirs: l += 1
                else: d += 1
            return {"played": w + d + l, "w": w, "d": d, "l": l,
                    "points": 3 * w + d}
        out["stakes"] = {
            "format": ("group stage: two groups, single round-robin; the "
                       "top two per group advance to two-legged "
                       "semi-finals, then a two-legged final. A draw is "
                       "one point — no shootout bonus in this "
                       "competition"),
            "home": record(hid), "away": record(aid),
        }
        return out

    out["stakes"] = {"means": "league/group match — three points at stake"}
    return out
