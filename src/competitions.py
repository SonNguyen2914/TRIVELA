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
                 no_model, accent="#7dd3fc", note=None):
        self.key = key
        self.display = display
        self.apif_league_id = apif_league_id
        self.kalshi_series = kalshi_series      # PROBED, see module docstring
        self.no_model = no_model
        self.accent = accent
        self.note = note

    def model_block(self) -> dict:
        return {
            "state": self.no_model,
            "why": (_BY_DESIGN_WHY if self.no_model == BY_DESIGN
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
}

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


def _fixtures_raw(league_id: int, season: int) -> list[dict]:
    """League-scoped fixture read; [] on failure, which callers must not
    read as "no fixtures". League scoping is correct here — unlike
    friendlies, where branded pre-season tournaments each get their own
    league id and only a date sweep sees them."""
    import requests

    from src.friendlies_apif import (APIF_BASE, APIF_TIMEOUT, load_key,
                                     parse_fixture, redact, response_items)
    key, _s, _p = load_key()
    if not key:
        return []
    try:
        r = requests.get(f"{APIF_BASE}/fixtures",
                         params={"league": str(league_id),
                                 "season": str(season)},
                         headers={"x-apisports-key": key},
                         timeout=APIF_TIMEOUT)
        if r.status_code != 200:
            return []
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[comp {league_id}] fixtures: {redact(str(exc), key)[:140]}")
        return []
    return [p for p in (parse_fixture(x) for x in response_items(body))
            if p is not None]


def fixtures(key: str, season: int = 2026, days: int | None = None,
             include_finished: bool = False) -> dict | None:
    v = VIEWERS.get(key)
    if v is None:
        return None

    def _run():
        rows = []
        for f in _fixtures_raw(v.apif_league_id, season):
            row = dict(f)
            try:
                from src.live import club_strength_estimate as cse
                from src.friendlies_apif import _slim_strength
                row["strength"] = _slim_strength(cse.for_fixture(f))
            except Exception as exc:
                row["strength"] = {"available": False,
                                   "reason": "estimate_unavailable",
                                   "detail": str(exc)[:140]}
            rows.append(row)
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
        from src import friendlies
        try:
            d = friendlies._get_json(
                f"{friendlies.KALSHI_BASE}/events",
                {"series_ticker": v.kalshi_series, "limit": 200,
                 "with_nested_markets": "true"})
        except Exception as exc:
            return {"status": "unavailable", "detail": str(exc)[:140],
                    "series": v.kalshi_series,
                    "means": ("the registry read FAILED — this is not 'no "
                              "book exists'")}
        if d is None:
            return {"status": "unavailable", "series": v.kalshi_series,
                    "means": ("the registry read FAILED, so whether books "
                              "are listed is UNKNOWN, not 'none open'")}
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

    return _cached(f"comp:{key}:markets", CACHE_TTL, _run) or {}


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
