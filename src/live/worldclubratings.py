"""worldclubratings.com provider client — WORLDWIDE club ratings.

Probed 2026-07-30. Why this exists alongside src/live/clubelo.py: ClubElo
is European club football only (589 clubs), which left the majority of a
friendlies slate unrated — measured, 63 of 399 fixtures got a read. This
source carries 2,446 clubs across 166 countries, and every club ClubElo
missed on that slate is in it (Al-Sadd SC, Al Nassr Riyadh, FC Tokyo,
Estrela Amadora, CD Guadalajara, Puebla FC).

ACCESS. robots.txt carries a Sitemap line and NOTHING else — no Disallow,
no AI-crawler block. (Contrast footballdatabase.com, which names and
disallows ClaudeBot; it was ruled out rather than worked around.) The
whole table arrives in ONE 162KB GET as a Quarto/htmlwidget
`application/json` blob, so a full refresh costs a single request rather
than 98 paginated ones. Updated weekly, on Tuesdays.

THE SCALE IS NOT CLUBELO'S. This is the load-bearing fact. Their published
expectation is

    We = 1 / (10**(-dr/600) + 1)

with divisor **600**, because the method is an adaptation of FIFA's
post-2018 ranking procedure, not classic Elo. ClubElo publishes the same
shape with divisor **400**. A 200-point gap therefore means a materially
different thing in each system, and mixing a rating from one with a rating
from the other inside a single expectation would be arithmetic without a
referent. Each source owns its own divisor here, and
`club_strength_estimate` refuses to pair across them.

Same semantics as ClubElo in one respect: W counts a draw as 0.5, so the
expectation is an expected POINTS SHARE, not a win probability.

THE AUTHORS' OWN CAVEAT, carried rather than paraphrased away: they
describe the ranking as "not ... a try to accurately rank club-level
football around the world, but rather as an experiment on the
effectiveness of the underlying method". That is a weaker claim than
ClubElo makes about itself, and it travels with every number this module
returns.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata

import requests

URL = "http://worldclubratings.com/rankings/elo_men/index.html"
TIMEOUT = 40
TTL_SECONDS = 6 * 3600.0        # the source itself refreshes weekly
FAILURE_TTL_SECONDS = 120.0   # see clubelo.FAILURE_TTL_SECONDS

# Published at worldclubratings.com/rankings/methods/elo_men.html. NOT 400
# — see the module docstring. Named so a reader cannot mistake which
# system it belongs to.
FIFA_ADAPTED_DIVISOR = 600.0

METHOD_NOTE = (
    "worldclubratings.com, an adaptation of FIFA's post-2018 ranking "
    "procedure to club football (expectation divisor 600, not Elo's 400). "
    "Its authors describe it as an experiment on the method's "
    "effectiveness rather than an authoritative world ranking.")

_cache: tuple[float, dict] | None = None
_lock = threading.Lock()

# Reserve / youth / women's markers — the same guard clubelo.py carries,
# for the same reason: "Mallorca II" matching "Mallorca" would publish a
# senior club's rating about a reserve side, and friendlies are full of
# these teams.
_RESERVE_MARKERS = {
    "ii", "iii", "b", "c", "reserve", "reserves", "res",
    "u19", "u20", "u21", "u23", "youth", "acad", "academy",
    "castilla", "w", "women", "womens", "feminine", "femenino",
}

# Dropped before comparison. Note "sc" is here, which is WHY the Qatari
# "Al-Sadd SC" and the Yemeni "Al Sadd" both normalise to "al sadd" — the
# collision becomes DETECTABLE as an ambiguity instead of resolving
# silently to whichever row happened to be found first.
_STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "club", "de",
         "the", "cd", "sd", "ca", "cs", "sk", "fk", "ks", "if", "bk"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore")
    s = s.decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w for w in s.split() if w not in _STOP).strip()


def _tokens(s: str) -> frozenset[str]:
    return frozenset(_norm(s).split())


def _is_non_senior(name: str) -> bool:
    return bool(_tokens(name) & _RESERVE_MARKERS)


def ratings(force: bool = False) -> dict:
    """{stable_id -> row}. `{}` on failure — callers must read that as
    "could not look", never as "this club is not covered".

    Rows carry the provider's own STABLE ID (col 6) and country code
    (col 7), which is what makes identity here something better than a
    name: AGENTS.md requires provider-stable ids, and this source has
    them.
    """
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache and not force:
            # a FAILED read is remembered too, briefly — see the note on
            # clubelo.FAILURE_TTL_SECONDS. Without it every club lookup
            # re-tries a fresh fetch during an outage and the caller pays
            # the full timeout hundreds of times over.
            ttl = TTL_SECONDS if _cache[1] else FAILURE_TTL_SECONDS
            if now - _cache[0] < ttl:
                return _cache[1]

    def _remember_failure():
        global _cache
        with _lock:
            _cache = (now, {})
        return {}

    try:
        r = requests.get(URL, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"[wcr] HTTP {r.status_code}")
            return _remember_failure()
        # EXPLICIT utf-8. The response omits a charset in its Content-Type,
        # and requests then falls back to Latin-1 for text/html per RFC
        # 2616 — which turned "AS Saint-Étienne" into "AS Saint-Ã‰tienne".
        # That is not cosmetic: normalisation strips non-ASCII, so every
        # accented club name silently stopped matching, which is a large
        # share of European and Latin American football. Found by asking
        # why ClubElo was out-covering a table four times its size.
        r.encoding = "utf-8"
        blobs = re.findall(
            r'<script type="application/json"[^>]*>(.*?)</script>',
            r.text, re.S)
    except requests.RequestException as exc:
        print(f"[wcr] fetch failed: {type(exc).__name__}: {str(exc)[:120]}")
        return _remember_failure()
    for b in blobs:
        try:
            data = ((json.loads(b).get("x") or {}).get("data"))
        except (ValueError, AttributeError):
            continue
        if not data or len(data) < 8:
            continue
        rank, _chg, name, country, points, _pchg, sid, ccode = data[:8]
        out: dict[str, dict] = {}
        for i in range(len(sid)):
            try:
                pts = float(points[i])
            except (TypeError, ValueError):
                continue
            key = str(sid[i])
            out[key] = {
                "id": key,
                "club": str(name[i]),
                "country": str(country[i]),
                "country_code": str(ccode[i]),
                "points": pts,
                "rank": rank[i],
            }
        if out:
            with _lock:
                _cache = (time.monotonic(), out)
            return out
    return {}


_index_cache: tuple[int, list[tuple[frozenset, dict]]] | None = None


def _prefix_ok(query_tokens, row_tokens, query: str, row: str) -> bool:
    """Whether a query CONTAINED IN a longer row name may match it.

    Both containment directions are needed, but they carry very different
    risk:

      row ⊂ query   "Leeds United" -> "Leeds". The provider simply uses a
                    shorter canonical name. Safe.
      query ⊂ row   "Al Nassr" -> "Al Nassr Riyadh" is right, but
                    "Miami FC" -> "Inter Miami" is a DIFFERENT CLUB — a
                    USL side matched to an MLS one, found live on
                    2026-07-30. {miami} is a strict subset of
                    {inter, miami}, so plain containment accepted it.

    The distinguishing fact is POSITION. A club's own name normally starts
    its full name: "Estrela" begins "Estrela Amadora", "Al Nassr" begins
    "Al Nassr Riyadh". A club whose name merely CONTAINS another club's is
    a different club: "Inter Miami" does not begin with "Miami".

    So the query must be a PREFIX of the row when the row is the longer
    one. Cheap, explainable, and it keeps every match that was already
    correct while refusing the one that was not.
    """
    if row_tokens < query_tokens:            # row is shorter: safe direction
        return True
    return row.startswith(query)


def _token_index(table: dict) -> list[tuple[frozenset, dict]]:
    """(tokens, row) for the whole table, computed ONCE per table.

    `_candidates` used to tokenise all 2,413 club names on every single
    lookup. The friendlies hub does 304 fixtures x 2 sides, so that was
    ~1.5 MILLION tokenisations per cold request — measured 2026-07-30 as a
    42-second response where the same call served from cache in 0.20s, and
    77 seconds through the proxy, which left the page rendering no rows at
    all in production."""
    global _index_cache
    key = id(table)
    if _index_cache and _index_cache[0] == key:
        return _index_cache[1]
    idx = [(_tokens(r["club"]), r) for r in table.values()]
    idx = [(t, r) for t, r in idx if t]
    _index_cache = (key, idx)
    return idx


def _candidates(query: str, table: dict) -> list[dict]:
    """Every row plausibly naming the same club, both containment
    directions.

    Both directions are needed because this provider's names run in either
    relation to a fixture's name: "Inter" (API-Football) is a subset of
    "Inter Mailand" (here), while "Leeds United" is a superset of "Leeds".
    Collecting ALL of them — rather than returning the first tier that
    yields exactly one — is what makes a cross-country collision visible.
    """
    q = _tokens(query)
    if not q:
        return []
    nq = _norm(query)
    return [row for toks, row in _token_index(table)
            if toks == q
            or ((toks < q or q < toks)
                and _prefix_ok(q, toks, nq, _norm(row["club"])))]


def lookup(name: str, country_hint: str | None = None) -> dict | None:
    """Resolve a club name to its rating row, or None.

    Refuses rather than guesses in three situations:
      - the name carries a reserve/youth/women's marker;
      - candidates span MORE THAN ONE COUNTRY and no hint disambiguates —
        this is the Al-Sadd case (Qatar 1350 vs Yemen 1029, both
        normalising to "al sadd"), and picking one would publish a
        confident number about the wrong club;
      - candidates sit in one country but are still several distinct
        clubs.

    `country_hint` may be a country name or an ISO-ish code as this
    provider spells it ("qa", "gb-eng", "mx").
    """
    if not name or _is_non_senior(name):
        return None
    table = ratings()
    if not table:
        return None
    hits = _candidates(name, table)
    if not hits:
        return None
    # EXACT token identity outranks a containment match, and resolves the
    # same-country collisions without guessing at anything: "FC Tokyo"
    # is exactly "FC Tokyo" while merely being contained in "Tokyo
    # Verdy", and "Guadalajara" is exactly "CD Guadalajara" (CD is a stop
    # word) while merely being contained in "Atlas Guadalajara". Applied
    # BEFORE the country hint so it cannot mask a real cross-country
    # collision — Al-Sadd SC and Al Sadd are both exact, so that case
    # stays ambiguous, which is the point.
    q = _tokens(name)
    exact = [r for r in hits if _tokens(r["club"]) == q]
    if len(exact) == 1:
        return {**exact[0], "match_tier": "exact"}
    if exact:
        hits = exact
    if len(hits) > 1 and country_hint:
        h = _norm(country_hint)
        narrowed = [r for r in hits
                    if _norm(r["country"]) == h
                    or _norm(r["country_code"]) == h]
        if len(narrowed) == 1:
            return {**narrowed[0], "match_tier": "name_plus_country"}
        hits = narrowed or hits
    if len(hits) != 1:
        countries = sorted({r["country"] for r in hits})
        return {"ambiguous": True,
                "candidates": [{"club": r["club"], "country": r["country"],
                                "points": r["points"]} for r in hits[:6]],
                "countries": countries}
    row = hits[0]
    tier = ("exact" if _tokens(row["club"]) == _tokens(name)
            else "unique_containment")
    return {**row, "match_tier": tier}


def expected_points_share(points_home: float, points_away: float) -> float:
    """This provider's PUBLISHED expectation, with ITS divisor (600).

    We = 1/(10**(-dr/600)+1), draws counted as half — an expected points
    share, not a win probability. Never call this with a ClubElo rating:
    that system's divisor is 400 and the two are not interchangeable.
    """
    dr = float(points_home) - float(points_away)
    return 1.0 / (10.0 ** (-dr / FIFA_ADAPTED_DIVISOR) + 1.0)
