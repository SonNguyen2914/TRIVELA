"""UEFA Europa Conference League (api-football league 848) — a CUP, and
therefore deliberately NOT shaped like the four league planes.

WHY THIS IS NOT AN MLS/EPL/LA LIGA/LIGA MX CLONE. Those are round-robin
leagues where every club plays 30-38 matches against the same population,
which is what makes a fitted goals model meaningful. Measured on the 2025
ECL (2026-07-30):

    409 fixtures, 382 completed, 164 distinct clubs
    matches per club: min 1, MEDIAN 4, max 18
    clubs clearing the MIN_GAMES=5 floor: 53 of 164

Two thirds of participants could never be rated from this competition's own
fixtures, and that does not improve with time — it is the shape of a cup
with four qualifying rounds. Liga MX's empty board was a season that had
barely started; this would be permanent. A fitted ECL model would rate a
handful of league-stage clubs and refuse everyone else, while mixing
qualifying-round minnows and group-stage regulars into one population.

So there is NO model here, no approval decision, and no odds board. What
this module serves is fixtures, the Kalshi book, and the CROSS-LEAGUE
strength read — which is the right instrument for exactly the reason the
model is the wrong one: an ECL club's strength lives in its DOMESTIC
league, not in its two qualifying matches. src/live/clubelo.py carries ECL
as a competition in its own right (78 teams on 2026-07-30).

That also means this competition hits the same structural wall friendlies
do: its clubs come from different domestic leagues, so their league-relative
xG ratings cannot be compared (`xg_ratings.ScopedStrength` raises on the
attempt, correctly). The strength read is the only honest number available,
and it is labelled EXTERNAL_UNEVALUATED like everywhere else.

League-SCOPED queries are correct here, unlike friendlies. Friendlies
needed a date-scoped sweep because branded pre-season tournaments each get
their own league id; ECL has one stable id (848) for the whole competition
including qualifying.
"""
from __future__ import annotations

import threading
import time

APIF_LEAGUE_ID = 848
COMPETITION = "ecl-2026"
DISPLAY = "UEFA Europa Conference League"

# Kalshi series, PROBED not assumed: GET /series/KXUECLGAME returned 200 on
# 2026-07-30 (KXECLGAME, KXCONFGAME and KXUEFACONFGAME all 404, so the
# ticker is not guessable from the competition name).
KALSHI_GAME_SERIES = "KXUECLGAME"

# Stated once, served verbatim on every surface, so no client has to
# reconstruct why there is no model number here.
FRAMING = (
    "Conference League is served as fixtures, real Kalshi prices, and an "
    "EXTERNAL strength read. There is no model and no approval decision: "
    "measured on the 2025 edition, the median club played 4 matches and "
    "only 53 of 164 reached the 5-match floor a fitted rating needs, so a "
    "model built on this competition's own fixtures would refuse two "
    "thirds of its participants. Nothing here is a recommendation.")

NO_MODEL_REASON = {
    "state": "no_model_by_design",
    "measured": {"season": 2025, "fixtures": 409, "completed": 382,
                 "clubs": 164, "median_matches_per_club": 4,
                 "clubs_at_or_above_min_games": 53, "min_games": 5},
    "why": ("a cup with four qualifying rounds gives most clubs 2-4 "
            "matches. This is permanent, not a season that has not started "
            "yet, so waiting does not fix it"),
    "instead": ("the cross-league strength read is used, because an ECL "
                "club's strength lives in its domestic league rather than "
                "in its handful of ECL matches"),
}

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
CACHE_TTL = 300.0


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


def _fixtures_raw(season: int, status: str | None = None) -> list[dict]:
    """League-scoped fixture read. Returns [] on failure — callers must not
    read that as "no fixtures", and the surfaces below say which it is."""
    import requests

    from src.friendlies_apif import (APIF_BASE, APIF_TIMEOUT, load_key,
                                     parse_fixture, redact, response_items)
    key, _src, _problems = load_key()
    if not key:
        return []
    params = {"league": str(APIF_LEAGUE_ID), "season": str(season)}
    if status:
        params["status"] = status
    try:
        r = requests.get(f"{APIF_BASE}/fixtures", params=params,
                         headers={"x-apisports-key": key},
                         timeout=APIF_TIMEOUT)
        if r.status_code != 200:
            return []
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[ecl] fixtures: {redact(str(exc), key)[:160]}")
        return []
    return [p for p in (parse_fixture(x) for x in response_items(body))
            if p is not None]


def fixtures(season: int = 2026, days: int | None = None) -> dict:
    """Upcoming and recent ECL fixtures, each with its strength read.

    `round` is carried through verbatim: a 2nd-qualifying-round tie and a
    league-stage match are not the same kind of fixture, and flattening
    them would hide that most of this competition is qualifying."""
    def _run():
        rows = _fixtures_raw(season)
        out = []
        for f in rows:
            row = dict(f)
            try:
                from src.live import club_strength_estimate as cse
                row["strength"] = cse.for_fixture(f)
            except Exception as exc:
                row["strength"] = {
                    "available": False, "reason": "estimate_unavailable",
                    "detail": str(exc)[:160]}
            out.append(row)
        out.sort(key=lambda r: (r.get("kickoff_utc") or ""))
        return out

    rows = _cached(f"ecl_fixtures:{season}", CACHE_TTL, _run) or []
    if days:
        from datetime import datetime, timedelta, timezone
        cut = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        rows = [r for r in rows
                if r.get("kickoff_utc") and now <= r["kickoff_utc"] <= cut]
    rated = sum(1 for r in rows
                if (r.get("strength") or {}).get("available"))
    return {
        "competition": COMPETITION, "display": DISPLAY,
        "apif_league_id": APIF_LEAGUE_ID, "season": season,
        "fixtures": rows, "count": len(rows),
        "with_strength_read": rated,
        "model": NO_MODEL_REASON,
        "framing": FRAMING,
    }


def markets() -> dict:
    """Open Kalshi books for the probed series, or the honest empty state.

    Reuses the friendlies Kalshi reader rather than a second client: the
    per-event book shape is identical, and a separate implementation could
    drift on the spread/staleness handling that reader already gets right.
    """
    def _run():
        from src import friendlies
        try:
            d = friendlies._get_json(
                f"{friendlies.KALSHI_BASE}/events",
                {"series_ticker": KALSHI_GAME_SERIES, "limit": 200,
                 "with_nested_markets": "true"})
        except Exception as exc:
            return {"status": "unavailable", "detail": str(exc)[:160],
                    "means": ("the registry read FAILED, so absence cannot "
                              "be claimed — this is not 'no open books'")}
        if d is None:
            return {"status": "unavailable",
                    "series": KALSHI_GAME_SERIES,
                    "means": ("the registry read FAILED, so whether books "
                              "are listed is UNKNOWN, not 'none open'")}
        events = d.get("events") or []
        # "listed" and "tradeable" are different facts: the EPL probe found
        # ten status=open events that were all settled last-season
        # fixtures, so both counts are reported rather than the flattering
        # one.
        open_events = [e for e in events
                       if any(m.get("status") in friendlies.TRADEABLE
                              for m in (e.get("markets") or []))]
        return {"status": "ok", "series": KALSHI_GAME_SERIES,
                "listed_events": len(events),
                "tradeable_events": len(open_events),
                "events": open_events,
                "truncated": bool(d.get("cursor"))}

    return _cached("ecl_markets", CACHE_TTL, _run) or {}
