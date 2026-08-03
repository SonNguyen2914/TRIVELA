"""ClubElo provider client — cross-league club strength, Europe only.

Probed 2026-07-30 (research_archive/clubelo_probe_2026-07-30.json). What
that probe established, and why each fact constrains this module:

  - `robots.txt` is OPEN, with no AI-crawler disallow. This matters
    because footballdatabase.com — the site originally suggested —
    explicitly Disallows ClaudeBot by name and offers no API, so it was
    ruled out rather than worked around.
  - The day ranking (`/YYYY-MM-DD`) carries 589 clubs with Country AND
    Level (league tier). Usable.
  - `/Fixtures` carries ZERO friendlies — 125 rows, every one a
    competitive match. Its precomputed probabilities are therefore
    useless here, and any friendly read must be computed from the day
    ranking. This module does not call /Fixtures at all.
  - Responses are CSV, not JSON. Unlike every other provider client in
    this repo.

PUBLISHED, so quotable: E = 1 / (10**(-dr/400) + 1), and k=20.

NOT published, so never invented here:
  - the current home-field advantage. The site publishes the UPDATE RULE
    (`HFA += sum(dElo)*0.075`) and says HFA is per-country and converged
    daily, but never the value. No home-field term is applied. A
    pre-season friendly is also frequently at a neutral or touring venue,
    so the term would be questionable even if the number were published.
  - the 1X2 split. "The match odds are based on a result histogram for
    the two club's Elo difference" — that histogram is not published as a
    formula, so NO draw probability is derived, for any pair.
"""
from __future__ import annotations

import csv
import json as _json
import os as _os
import io
import re
import threading
import time
import unicodedata

import requests

BASE = "http://api.clubelo.com"
TIMEOUT = 20
TTL_SECONDS = 3600.0            # the ranking moves once a day at most

# The Elo expectation, verbatim from clubelo.com/System. Note what it
# MEANS: "draws counting as half win/half loss", so this is an expected
# POINTS SHARE, not P(win). Naming it `expected_points_share` rather than
# `win_probability` is the whole point — the caller cannot accidentally
# publish it as a win percentage.
ELO_DIVISOR = 400.0

_cache: dict[str, tuple[float, dict]] = {}
_stale: dict[str, object] = {}

# Where the last good table is kept so it survives a RESTART. An outage on
# a fresh deploy is exactly the case an in-memory cache cannot help with:
# the process has never read the provider, so it has nothing to fall back
# on and every club reads as unrated.
_DISK = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__)))), "var", "clubelo_last.json")


def _save_last_good(day: str, table: dict) -> None:
    try:
        _os.makedirs(_os.path.dirname(_DISK), exist_ok=True)
        tmp = _DISK + ".tmp"
        with open(tmp, "w") as fh:
            _json.dump({"day": day, "table": table}, fh)
        _os.replace(tmp, _DISK)      # atomic — never a half-written file
    except OSError as exc:
        print(f"[clubelo] could not persist table: {exc}")


def _load_last_good() -> dict | None:
    try:
        with open(_DISK) as fh:
            d = _json.load(fh)
        if isinstance(d.get("table"), dict) and d["table"]:
            return d
    except (OSError, ValueError):
        pass
    return None
_lock = threading.Lock()

# Aliases added ONLY from measured misses, never from intuition — the same
# rule the Kalshi bridges carry. Each was confirmed present in the
# 2026-07-30 day ranking under the mapped name.
ALIASES = {
    # the five unique-containment matches measured 2026-07-30, promoted to
    # aliases so the containment tier below could be tightened. Each was a
    # SINGLE-TOKEN row absorbed into a longer query — the exact shape that
    # also matched Inter (ITA) to "Inter Miami" and Atletico (ESP) to
    # "Atletico San Luis" on the 2026-08-03 Leagues Cup slate, publishing
    # two European giants' ratings under two North American clubs.
    "leeds united": "Leeds",
    "birmingham city": "Birmingham",
    "borussia dortmund": "Dortmund",
    "atletico madrid": "Atletico",
    "cardiff city": "Cardiff",
    "manchester city": "Man City",          # ENG L1, Elo 1971
    "manchester united": "Man United",      # ENG L1, Elo 1915
    # containment CORRECTLY refused this one: "Inter" also substring-
    # matches Winterthur and Inter Turku, so it needs an explicit alias
    # rather than a looser matching tier
    "internazionale": "Inter",              # ITA L1, Elo 1889
}

_STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "club", "de",
         "the", "cd", "sd", "ca"}

# RESERVE / YOUTH / WOMEN'S markers. Any of these present means the club is
# NOT the senior side ClubElo rates, and matching MUST be refused outright.
#
# Found by the first live smoke test on 2026-07-30: "Mallorca II" matched
# "Mallorca" through unique containment, because {mallorca} is a strict
# subset of {mallorca, ii}. That would have attached a top-flight Elo to a
# reserve team and published a confident number about the wrong club — the
# exact Real Madrid vs Real Madrid Castilla hazard this repo warns about in
# three separate files. Friendlies are FULL of these sides: the same run
# also surfaced FC Porto B, Mallorca II and Athletic Bilbao's feeder.
_RESERVE_MARKERS = {
    "ii", "iii", "b", "c", "reserve", "reserves", "res",
    "u19", "u20", "u21", "u23", "youth", "acad", "academy",
    "castilla", "atletic", "atl",          # Castilla / Atlètic feeder names
    "w", "women", "womens", "feminine", "femenino", "feminino",
}


def _is_non_senior(name: str) -> bool:
    return bool(_tokens(name) & _RESERVE_MARKERS)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore")
    s = s.decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w for w in s.split() if w not in _STOP).strip()


def _tokens(s: str) -> frozenset[str]:
    return frozenset(_norm(s).split())


# A FAILED read is cached too, for much less time than a good one. Without
# this, an outage at the provider takes THIS service down with it: the
# failure path returned {} without caching, so every club lookup re-tried
# a fresh fetch and paid the full timeout again. The friendlies board asks
# for ~650 clubs, so a 20s timeout became hours of blocking and the
# endpoint stopped answering at all while /api/health stayed green.
#
# The cached value is still {}, so callers keep reading it as "could not
# look" rather than "no coverage" — the negative cache changes only how
# often we ask, never what an empty table MEANS.
FAILURE_TTL_SECONDS = 120.0


def day_ranking(day: str) -> dict:
    """{club -> row} for one date, cached. `{}` when the fetch fails —
    callers must treat that as "could not look", never "no coverage"."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(day)
        if hit:
            age, table = now - hit[0], hit[1]
            ttl = TTL_SECONDS if table else FAILURE_TTL_SECONDS
            if age < ttl:
                return table

    def _remember_failure():
        """Serve the last table we successfully read, if we have one.

        An Elo ranking moves once a day at most, so yesterday's table is a
        far better answer than none — on 2026-07-31 clubelo was
        unreachable all day and the board fell from 66 rated fixtures to
        23, not because those clubs are unrated but because nothing
        remembered ever having read them.

        NEVER passed off as fresh: `served_vintage()` reports the day the
        table was actually fetched.
        """
        disk = _load_last_good()
        with _lock:
            if disk:
                _cache[day] = (now, disk["table"])
                _stale["day"] = disk["day"]
                print(f"[clubelo] {day}: serving last good table from "
                      f"{disk['day']} ({len(disk['table'])} clubs)")
                return disk["table"]
            _cache[day] = (now, {})
        return {}

    try:
        r = requests.get(f"{BASE}/{day}", timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"[clubelo] {day}: HTTP {r.status_code}")
            return _remember_failure()
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except (requests.RequestException, csv.Error, ValueError) as exc:
        print(f"[clubelo] {day}: {type(exc).__name__}: {str(exc)[:120]}")
        return _remember_failure()
    out = {}
    for row in rows:
        club = (row.get("Club") or "").strip()
        if not club:
            continue
        try:
            elo = float(row.get("Elo") or "")
        except ValueError:
            continue
        out[club] = {"club": club, "elo": elo,
                     "country": (row.get("Country") or "").strip() or None,
                     "level": (row.get("Level") or "").strip() or None}
    with _lock:
        _cache[day] = (time.monotonic(), out)
        _stale.pop("day", None)
        _stale.pop("since", None)
    if out:
        _save_last_good(day, out)
    return out


def served_vintage() -> dict:
    """Which day's table is actually being served, and whether it is the
    one asked for. A stale rating presented as current is the failure this
    module exists to avoid, so callers can put this in front of a reader."""
    with _lock:
        stale_day = _stale.get("day")
    return {"stale": bool(stale_day), "table_day": stale_day,
            "means": (f"the provider is unreachable; ratings are the last "
                      f"good read, from {stale_day}" if stale_day else
                      "ratings are today's published table")}


_idx_cache: tuple[int, dict, list] | None = None


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


def _indexes(table: dict):
    """(normalised-name map, (tokens, key) list), computed once per table —
    both were previously rebuilt on EVERY lookup. See the same fix in
    worldclubratings._token_index for the cost that carried."""
    global _idx_cache
    key = id(table)
    if _idx_cache and _idx_cache[0] == key:
        return _idx_cache[1], _idx_cache[2]
    by_norm = {_norm(k): k for k in table}
    index = [(t, k) for k, t in ((k, _tokens(k)) for k in table) if t]
    _idx_cache = (key, by_norm, index)
    return by_norm, index


def lookup(name: str, day: str) -> dict | None:
    """Resolve one club name to its Elo row, or None.

    Tiers, strongest first, mirroring the discipline the Kalshi bridges
    use: exact-normalized, then an evidence-backed alias, then token-set,
    then UNIQUE directional containment. A loose or ambiguous match is
    refused — a wrong Elo attachment is a wrong number that looks
    legitimate, which is worse than no number.

    Measured on 2026-07-30 over 22 real friendly club slots: 10 exact,
    0 token-set, 5 unique-containment (Leeds United->Leeds, Birmingham
    City->Birmingham, Borussia Dortmund->Dortmund, Atletico
    Madrid->Atletico, Cardiff City->Cardiff) = 68% with no aliases, ~82%
    with the three in ALIASES.
    """
    table = day_ranking(day)
    if not table:
        return None
    # a reserve/youth/women's side is never the senior club ClubElo rates,
    # and no tier below may be allowed to bridge the gap
    if _is_non_senior(name):
        return None
    by_norm, index = _indexes(table)
    n = _norm(name)
    if n in by_norm:
        return {**table[by_norm[n]], "match_tier": "exact"}
    alias = ALIASES.get(n)
    if alias and alias in table:
        return {**table[alias], "match_tier": "alias"}
    want = _tokens(name)
    if want:
        ts = [k for toks, k in index if toks == want]
        if len(ts) == 1:
            return {**table[ts[0]], "match_tier": "token_set"}
        nq = _norm(name)
        # row ⊂ query was treated as the safe direction ("Leeds United"
        # -> "Leeds"). It is NOT safe when the row is a single token: a
        # one-word canonical name absorbed into a longer name is
        # indistinguishable from a DIFFERENT club sharing the word.
        # Measured 2026-08-03: "Inter Miami" -> Inter (ITA, 1888.6) and
        # "Atletico San Luis" -> Atletico (ESP, 1827.7), live on the
        # Leagues Cup board. The five legitimate single-token matches are
        # pinned in ALIASES above; anything else falls through to the
        # other provider rather than borrowing a European club's rating.
        uc = [k for toks, k in index
              if (toks < want and len(toks) > 1)
              or (want < toks and _prefix_ok(want, toks, nq, _norm(k)))]
        if len(uc) == 1:
            return {**table[uc[0]], "match_tier": "unique_containment"}
    return None


def expected_points_share(elo_home: float, elo_away: float) -> float:
    """The published Elo expectation for the HOME side.

    E = 1 / (10**(-dr/400) + 1). This is an expected POINTS share with
    draws counted as half — NOT a win probability. No home-field term is
    added: its current value is not published (see module docstring).
    """
    dr = float(elo_home) - float(elo_away)
    return 1.0 / (10.0 ** (-dr / ELO_DIVISOR) + 1.0)


def near_misses(name: str, day: str, limit: int = 12) -> dict:
    """Candidate rows for a name `lookup` REFUSED, ranked by token overlap.

    Diagnostic only — it never resolves anything and nothing in the
    pricing path calls it. Its purpose is the rule this module already
    states: the alias table grows only from MEASURED misses, never from
    intuition. Without a way to see what the provider actually calls a
    club, the only way to add an alias is to guess at the spelling, which
    is how a wrong club gets attached to a fixture.

    Returns the raw provider strings, deliberately unfiltered by any
    confidence bar — a human reads them and decides.
    """
    table = day_ranking(day)
    if not table:
        return {"query": name, "day": day, "table_rows": 0,
                "candidates": [],
                "means": ("the provider table could not be read, so whether "
                          "this club is covered is UNKNOWN — not 'absent'")}
    q = _tokens(name)
    scored = []
    for club in table:
        t = _tokens(club)
        if not t:
            continue
        overlap = len(q & t)
        if not overlap:
            continue
        scored.append({
            "provider_name": club,
            "shared_tokens": sorted(q & t),
            "jaccard": round(overlap / len(q | t), 3),
            "elo": (table[club] or {}).get("elo"),
            "country": (table[club] or {}).get("country"),
            "would_lookup_resolve": lookup(club, day) is not None,
        })
    scored.sort(key=lambda r: -r["jaccard"])
    return {"query": name, "day": day, "table_rows": len(table),
            "resolved": lookup(name, day) is not None,
            "candidates": scored[:limit],
            "means": ("candidates share at least one token with the query. "
                      "This is NOT a match and must not be treated as one — "
                      "an alias is added by a human who recognises the club")}
