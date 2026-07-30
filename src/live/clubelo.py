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
_lock = threading.Lock()

# Aliases added ONLY from measured misses, never from intuition — the same
# rule the Kalshi bridges carry. Each was confirmed present in the
# 2026-07-30 day ranking under the mapped name.
ALIASES = {
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


def day_ranking(day: str) -> dict:
    """{club -> row} for one date, cached. `{}` when the fetch fails —
    callers must treat that as "could not look", never "no coverage"."""
    with _lock:
        hit = _cache.get(day)
        if hit and time.monotonic() - hit[0] < TTL_SECONDS:
            return hit[1]
    try:
        r = requests.get(f"{BASE}/{day}", timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except (requests.RequestException, csv.Error, ValueError) as exc:
        print(f"[clubelo] {day}: {type(exc).__name__}: {str(exc)[:120]}")
        return {}
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
    return out


_idx_cache: tuple[int, dict, list] | None = None


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
        uc = [k for toks, k in index if toks < want]
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
