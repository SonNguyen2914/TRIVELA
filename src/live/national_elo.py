"""National-team Elo from eloratings.net — the strength read for
NATIONAL-TEAM viewer competitions.

WHY A THIRD SOURCE. Every strength source in this codebase rates CLUBS
(ClubElo: European clubs; worldclubratings: top-flight clubs worldwide).
A national-team competition can match NEITHER without lying — "Vietnam"
is not a club, and a club provider that happens to fuzzy-match it would
be attaching a rating measured on a different population entirely. The
World Football Elo Ratings (eloratings.net) rate national teams and only
national teams, over results back to 1872, with the method published on
the site. They cover all eleven ASEAN federations, down to Brunei.

WHAT THIS PUBLISHES. The provider's own Elo expectation
E = 1 / (10 ** (-dr/400) + 1), as an expected POINTS SHARE in which a
draw counts half — the same semantics the club read publishes, so the
comparison surface treats both identically. No draw split is derived
(the provider does not publish one) and no win probability is claimed.

HOME ADVANTAGE IS NOT APPLIED, and that is a stated choice, not an
oversight: eloratings.net's method adds 100 points to a team playing at
a TRUE home venue, but whether a tournament fixture is truly home,
nominally home, or neutral is not knowable from the fixture feed — the
ASEAN Championship mixes all three across a single group stage. The raw
neutral-venue expectation is published and says so. A reader pricing a
genuine home fixture should expect the home side to outperform this
number, and by roughly the amount the provider's +100 implies (~64% of
a point share at equal ratings).

DATA TRANSPORT. The site serves its data as bare TSV (World.tsv for the
current ranking, en.teams.tsv for the code -> name mapping). Codes are
the site's OWN two-letter scheme, not ISO — EN is England, and Vietnam
is VN while historical North/South Vietnam keep NV/RV — so the mapping
file is fetched rather than assumed. Both files were probed 2026-08-04:
243 ranked teams, all ASEAN entrants present.
"""
from __future__ import annotations

import threading
import time
import unicodedata

ELO_DIVISOR = 400
SRC_NATIONAL = "eloratings_national"
BASE = "https://www.eloratings.net"
WORLD_TSV = f"{BASE}/World.tsv"
TEAMS_TSV = f"{BASE}/en.teams.tsv"
TIMEOUT = 15
CACHE_TTL = 6 * 3600.0  # ratings move only when matches finish

# Provider-name -> eloratings-name, for the handful of spellings the
# fixture provider (API-Football) and eloratings.net disagree on. Each
# entry was verified against BOTH sources by hand — an override is a
# pinned fact, never a fuzzy guess.
NAME_OVERRIDES = {
    "timor-leste": "east timor",
}

ATTRIBUTION = ("World Football Elo Ratings from eloratings.net, computed "
               "from national-team results since 1872; methodology "
               "published at eloratings.net/about.")
SEMANTICS = (
    "expected_points_share is eloratings.net's Elo expectation "
    "E = 1/(10**(-dr/400)+1), in which DRAWS COUNT AS HALF A WIN. It is "
    "a points share, NOT a win probability, and no draw number is "
    "published because the provider does not publish one.")
HFA_NOTE = (
    "not applied — eloratings.net adds 100 points for a TRUE home venue, "
    "but tournament fixtures mix real, nominal and neutral hosting and "
    "the fixture feed cannot tell them apart. This is the neutral-venue "
    "expectation; a genuine home side should be expected to outperform "
    "it.")

_lock = threading.Lock()
_cache: dict | None = None      # {"names": {folded: code}, "ratings":
_cache_at: float = 0.0          #  {code: int}, "fetched_utc": iso}


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s or "")
    return " ".join("".join(c for c in n if not unicodedata.combining(c))
                    .lower().split())


def _parse_teams(text: str) -> dict[str, str]:
    """folded name/alias -> code. A name claimed by two codes is dropped
    from the map entirely — an ambiguous key must refuse, not coin-flip."""
    names: dict[str, str] = {}
    claimed: dict[str, set] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) < 2:
            continue
        code = parts[0]
        for alias in parts[1:]:
            f = _fold(alias)
            if f:
                claimed.setdefault(f, set()).add(code)
    for f, codes in claimed.items():
        if len(codes) == 1:
            names[f] = next(iter(codes))
    return names


def _parse_world(text: str) -> dict[str, int]:
    """code -> current rating. Column 3 is the code and column 4 the
    rating (probed 2026-08-04); a row that does not parse is skipped
    rather than guessed at."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        code = parts[2].strip()
        try:
            rating = int(parts[3].strip())
        except ValueError:
            continue
        if code:
            out[code] = rating
    return out


def _snapshot() -> tuple[dict | None, str | None]:
    """(data, error). A fetch failure with a previous snapshot serves the
    old one — with its fetched_utc intact so staleness is visible — and
    only refuses when there has never been a successful fetch."""
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and now - _cache_at < CACHE_TTL:
            return _cache, None
    import requests
    from datetime import datetime, timezone
    try:
        rw = requests.get(WORLD_TSV, timeout=TIMEOUT)
        rt = requests.get(TEAMS_TSV, timeout=TIMEOUT)
        rw.raise_for_status()
        rt.raise_for_status()
        data = {
            "names": _parse_teams(rt.text),
            "ratings": _parse_world(rw.text),
            "fetched_utc": datetime.now(timezone.utc).isoformat(),
        }
        if not data["names"] or not data["ratings"]:
            raise ValueError("empty parse")
    except Exception as exc:
        with _lock:
            if _cache is not None:
                return _cache, None      # stale but real beats absent
        return None, f"{type(exc).__name__}: {str(exc)[:100]}"
    with _lock:
        _cache = data
        _cache_at = now
    return data, None


def lookup(name: str) -> dict:
    """One side. {rated: True, source, rating, code, matched} or
    {rated: False, reason, reason_words} — never a default rating."""
    data, err = _snapshot()
    if data is None:
        return {"rated": False, "club": name, "reason": "provider_unreachable",
                "reason_words": f"eloratings.net could not be read ({err}); "
                                f"whether this team is rated is UNKNOWN"}
    f = _fold(name)
    f = NAME_OVERRIDES.get(f, f)
    code = data["names"].get(f)
    if code is None:
        return {"rated": False, "club": name, "reason": "name_unmapped",
                "reason_words": ("no eloratings.net team matches this name; "
                                 "nothing was fuzzy-matched in its place")}
    rating = data["ratings"].get(code)
    if rating is None:
        return {"rated": False, "club": name, "reason": "not_in_ranking",
                "reason_words": (f"eloratings.net knows the name (code "
                                 f"{code}) but its current ranking carries "
                                 f"no rating for it")}
    return {"rated": True, "club": name, "source": SRC_NATIONAL,
            "rating": rating, "code": code,
            "as_of": data["fetched_utc"]}


def expected_points_share(home_rating: float, away_rating: float) -> float:
    return 1.0 / (10.0 ** (-(home_rating - away_rating) / ELO_DIVISOR) + 1.0)


def for_fixture(f: dict) -> dict:
    """The national-team strength read for one fixture — the same shape
    the club read produces, so every consumer downstream (slimming,
    market comparison, the frontend types) treats both identically."""
    from src.live import club_strength_estimate as cse
    home_name = ((f.get("home") or {}).get("name")) or ""
    away_name = ((f.get("away") or {}).get("name")) or ""
    h = lookup(home_name)
    a = lookup(away_name)
    out = {
        "estimate_class": cse.ESTIMATE_CLASS,
        "estimate_meaning": cse.ESTIMATE_MEANING,
        "home": h, "away": a,
        "available": False,
        "expected_points_share": None,
        "semantics": SEMANTICS,
        "home_field_advantage": HFA_NOTE,
        "attribution": ATTRIBUTION,
    }
    if h.get("rated") and a.get("rated"):
        e = expected_points_share(h["rating"], a["rating"])
        out["available"] = True
        out["source"] = SRC_NATIONAL
        out["expectation_divisor"] = ELO_DIVISOR
        out["elo_difference"] = h["rating"] - a["rating"]
        out["expected_points_share"] = {
            "home": round(e, 4), "away": round(1.0 - e, 4)}
    return out
