"""Venue city for a fixture whose feed carries none — from the HOME
team's registered ground, used only when that is defensible.

WHY. The fixture feed frequently serves {venue: {id: None, name: X,
city: None}} (probed 2026-08-05: nearly every Leagues Cup fixture), so
the weather read refused most of a slate. The provider DOES know each
team's registered venue and its city via /teams?id= — one request per
team, cacheable indefinitely.

WHEN IT MAY BE USED, exactly:
  - the fixture names NO venue at all: the home team's registered city
    is used, and the basis says the assumption out loud;
  - the fixture names the SAME venue as the team's registered ground
    (folded comparison): the registered city fills the blank;
  - the fixture names a DIFFERENT venue: REFUSED. A mismatch is either
    a neutral site (wrong city guaranteed) or a naming-rights rename
    (right city, unprovable from here) — and 'Nu Stadium' vs 'Chase
    Stadium' (Inter Miami, probed 2026-08-05) is exactly that
    undecidable case. Weather at the wrong city is worse than none.

The returned basis string rides on the weather payload, so a reader
always sees WHERE the city came from, never just a temperature.
"""
from __future__ import annotations

import threading
import unicodedata

TIMEOUT = 10
_lock = threading.Lock()
_cache: dict[int, dict | None] = {}      # team id -> {vname, city} | None


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s or "")
    return " ".join("".join(c for c in n if not unicodedata.combining(c))
                    .lower().split())


def _team_venue(team_id: int) -> dict | None:
    with _lock:
        if team_id in _cache:
            return _cache[team_id]
    import requests
    from src.friendlies_apif import APIF_BASE, load_key
    key, _s, _p = load_key()
    if not key:
        return None                       # failure: never cached
    try:
        r = requests.get(f"{APIF_BASE}/teams", params={"id": str(team_id)},
                         headers={"x-apisports-key": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        resp = (r.json().get("response") or [None])[0] or {}
    except Exception:
        return None
    v = resp.get("venue") or {}
    out = None
    if v.get("city"):
        # 'Fort Lauderdale, Florida' -> the geocoder wants the city alone
        out = {"vname": v.get("name") or "",
               "city": str(v["city"]).split(",")[0].strip(),
               "country": (resp.get("team") or {}).get("country")}
    with _lock:
        _cache[team_id] = out             # a MISS is a fact; cache it
    return out


def country_for(home_team_id: int | None) -> str | None:
    """The home team's federation country — the geocoder's country hint.
    Available even when the CITY path refuses (a neutral venue is still
    almost always inside the home team's country for these formats)."""
    if not home_team_id:
        return None
    tv = _team_venue(home_team_id)
    return (tv or {}).get("country")


def city_for(home_team_id: int | None,
             fixture_venue_name: str | None) -> tuple[str | None, str]:
    """(city, basis). city None means REFUSED, and basis says why."""
    if not home_team_id:
        return None, "no home team id to resolve a registered venue from"
    tv = _team_venue(home_team_id)
    if tv is None:
        return None, ("the home team's registered venue could not be "
                      "read from the provider")
    if not fixture_venue_name:
        return tv["city"], (f"fixture names no venue; assumed the home "
                            f"team's registered ground ({tv['vname']})")
    if _fold(fixture_venue_name) == _fold(tv["vname"]):
        return tv["city"], (f"fixture venue matches the home team's "
                            f"registered ground; city from the team "
                            f"record")
    return None, (f"fixture venue '{fixture_venue_name}' differs from "
                  f"the registered ground '{tv['vname']}' — a neutral "
                  f"site or a renamed stadium, undecidable from here; "
                  f"refusing a possibly-wrong city")
