#!/usr/bin/env python3
"""Measure three editions of the Leagues Cup — what history can and
cannot teach the cross-league surface.

    python scripts/measure_leagues_cup_history.py \
        --out research_archive/leagues_cup_history_$(date -u +%F).json

WHY THIS EXISTS. The Leagues Cup surface runs on three numbers nobody
has measured: a cross-league strength read that is EXTERNAL_UNEVALUATED,
a draw leg on which our side is structurally SILENT (the read is a
points share; no provider publishes a usable draw probability), and an
unfitted MLS↔LigaMX scale conversion that `src/competitions.py` refuses
to guess. 216 completed matches across 2023–2025 are reachable through
our own viewer surface. This script measures what they can honestly
support, and REFUSES what they cannot — each refusal named in the
archive rather than silently absent.

THE THREE MEASUREMENTS, in order of cleanliness:

  1. DRAW RATE at 90 minutes — results-only, clean across all three
     editions. This cup settles group-stage draws by shootout, so a
     `PEN` status with equal goals IS a 90-minute draw, which is exactly
     what the Kalshi Tie leg settles on. Probed before writing: all
     three editions carry only FT and PEN statuses, and every PEN row
     has equal goals. An `AET` row, should one ever appear, is EXCLUDED
     and counted — its stored goals include extra time, so the
     90-minute result is not recoverable from this payload.

  2. CROSS-LEAGUE BALANCE — results-only, clean. MLS points share in
     MLS-vs-LigaMX meetings, split by which league hosted, per edition
     and pooled, bootstrap CIs. This is the empirical anchor the
     unfitted scale conversion has been missing: fitted from OUTCOMES,
     not from either league's ratings. Club→league resolution comes
     from our own standings surfaces at runtime (30 MLS, 18 Liga MX
     clubs), matched on identity tokens; an unmatched or ambiguous club
     excludes the fixture FROM THE BALANCE ONLY, with a count and the
     names, never a guess. Same-league pairings (possible in knockouts)
     are counted apart and excluded from the balance.

  3. SCORING THE STRENGTH READ — 2025 ONLY, and contaminated even
     there, which the archive says in the result object itself. The
     provider serves CURRENT ratings: a rating read today already
     contains the 2025 outcomes it would be "predicting". For 2023 and
     2024 that temporal leakage is disqualifying and the scoring is
     REFUSED BY NAME (AGENTS.md §13 — a feature may use only what was
     knowable at the moment predicted). For 2025 it is roughly a year
     of contamination: reported, labeled `evidence_class:
     "contaminated_retrospective"`, and fit for context only — never
     for a constant.

WHAT IS REFUSED OUTRIGHT: the draw rate CONDITIONAL ON HOW EVEN THE
MATCH WAS — the question the operator actually asked. Measuring it
needs a pre-match evenness number for each historical fixture, and no
leakage-free one exists: there were no captured books, and today's
ratings contain the outcomes. The refusal is in the archive with its
reason, and points at the only clean source: the 2026 prospective
corpus, whose T-60 books are frozen publicly before each kickoff.

NO NUMBER HERE IS AN EDGE. A base rate beside an ask is two numbers on
one page; whether the market's Tie pricing is right cannot be settled
by a pooled rate that mixes even matches with blowouts.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

UTC = timezone.utc
SEASONS = (2023, 2024, 2025)
TERMINAL = ("FT", "PEN")            # AET excluded+counted, see docstring
BOOT = 2000
SEED = 12345


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in n if not unicodedata.combining(c)).lower()


def _toks(s: str) -> frozenset[str]:
    from src.friendlies import _identity_tokens
    return frozenset(t for t in _identity_tokens(_fold(s))
                     if len(t) > 1 and any(c.isalnum() for c in t))


def _get(base: str, path: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(f"{base.rstrip('/')}{path}", params=params,
                         timeout=60)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[history] fetch failed {path}: {type(exc).__name__}",
              file=sys.stderr)
        return None


# --- league rosters, from our own surfaces ---------------------------------

def _standings_names(doc: dict | None) -> set[str]:
    names: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("name", "displayName", "team") and isinstance(v, str):
                    names.add(v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(doc or {})
    return names


# Evidence-backed roster aliases, the _KALSHI_ALIASES discipline: each
# entry is a pair OBSERVED in the two live surfaces this script joins
# (our standings x the fixture provider), never intuition. 'LAFC' is an
# initialism no token rule can bridge to 'Los Angeles FC'.
_ROSTER_ALIASES: dict[str, frozenset[str]] = {
    "lafc": frozenset({"los", "angeles"}),
}


def league_map(base: str) -> tuple[dict, dict]:
    """(rosters, meta). rosters = {"mls": [tokensets], "ligamx": [...]}.
    Derived at runtime from /api/mls/standings and /api/ligamx/standings
    — never a hand-typed list, which is how alias bugs are born."""
    mls = _standings_names(_get(base, "/api/mls/standings"))
    lmx = _standings_names(_get(base, "/api/ligamx/standings"))
    meta = {"mls_clubs": len(mls), "ligamx_clubs": len(lmx),
            "source": "runtime read of our own standings surfaces"}
    if not mls or not lmx:
        meta["error"] = ("a standings surface returned no clubs — the "
                        "cross-league balance cannot be measured this run")

    def entry(n: str) -> tuple[frozenset, str]:
        t = _toks(n)
        extra = _ROSTER_ALIASES.get(_fold(n).replace(" ", ""))
        return ((t | extra) if extra else t, n)

    return ({"mls": [entry(n) for n in sorted(mls)],
             "ligamx": [entry(n) for n in sorted(lmx)]}, meta)


def club_league(name: str, rosters: dict) -> tuple[str | None, str]:
    """-> (league|None, reason).

    This resolves the LEAGUE, never the club, and that difference is
    what makes best-overlap scoring safe here where it would not be for
    club identity: 'Los Angeles Galaxy' overlapping LAFC's tokens still
    lands in MLS, and no city fields clubs in both leagues. The two
    providers also disagree on word forms ('Red Bulls' vs 'Red Bull'),
    so containment alone dropped a fifth of the fixtures. A tie between
    the leagues, or zero overlap with either (a club no longer in
    either roster — Mazatlán left Liga MX), is still a refusal.
    """
    t = _toks(name)
    if not t:
        return None, "no identity tokens"
    best = {lg: max((len(t & rt) for rt, _n in roster), default=0)
            for lg, roster in rosters.items()}
    top = max(best.values())
    if top == 0:
        return None, "no roster match"
    leaders = [lg for lg, v in best.items() if v == top]
    if len(leaders) > 1:
        return None, "matches both rosters equally"
    return leaders[0], "matched"


# --- the measurements ------------------------------------------------------

def _ci(xs: list[float], rng: random.Random) -> list[float]:
    means = []
    for _ in range(BOOT):
        s = [xs[rng.randrange(len(xs))] for _ in range(len(xs))]
        means.append(sum(s) / len(s))
    means.sort()
    return [round(means[int(0.025 * BOOT)], 4),
            round(means[int(0.975 * BOOT)], 4)]


def draw_rate(rows_by_season: dict[int, list[dict]]) -> dict:
    rng = random.Random(SEED)
    out: dict = {"what_this_is": (
        "share of matches level after 90 minutes plus stoppage — the "
        "quantity the Kalshi Tie leg settles on. PEN = a group/knockout "
        "shootout AFTER a 90-minute draw and counts as a draw"),
        "editions": {}, }
    pooled: list[float] = []
    excluded_aet = 0
    for season, rows in sorted(rows_by_season.items()):
        aet = [r for r in rows if r.get("status") == "AET"]
        excluded_aet += len(aet)
        ok = [r for r in rows if r.get("status") in TERMINAL]
        ys = [1.0 if r["goals"]["home"] == r["goals"]["away"] else 0.0
              for r in ok]
        phase = {}
        for label, pred in (
                ("group", lambda r: str(r.get("round") or "")
                 .startswith("Group")),
                ("knockout", lambda r: not str(r.get("round") or "")
                 .startswith("Group"))):
            sub = [1.0 if r["goals"]["home"] == r["goals"]["away"] else 0.0
                   for r in ok if pred(r)]
            if sub:
                phase[label] = {"n": len(sub),
                                "draw_rate": round(sum(sub) / len(sub), 4)}
        out["editions"][season] = {
            "n": len(ys), "draws": int(sum(ys)),
            "draw_rate": round(sum(ys) / len(ys), 4) if ys else None,
            "ci95": _ci(ys, rng) if len(ys) >= 20 else None,
            "by_phase": phase}
        pooled.extend(ys)
    out["pooled"] = {"n": len(pooled), "draws": int(sum(pooled)),
                     "draw_rate": (round(sum(pooled) / len(pooled), 4)
                                   if pooled else None),
                     "ci95": _ci(pooled, rng) if len(pooled) >= 20 else None}
    out["aet_excluded"] = {
        "n": excluded_aet,
        "why": ("stored goals for an AET match include extra time, so "
                "the 90-minute result is not recoverable — excluded "
                "rather than miscounted")}
    return out


def cross_league_balance(rows_by_season: dict[int, list[dict]],
                         rosters: dict) -> dict:
    """MLS expected-points share in cross-league meetings, by hosting
    league. Fitted from outcomes; no rating is consulted anywhere."""
    rng = random.Random(SEED + 1)
    out: dict = {"what_this_is": (
        "the MLS side's realised points share (win 1, draw 0.5, loss 0) "
        "in MLS-vs-LigaMX meetings, split by which league's club "
        "hosted. The empirical anchor for the unfitted MLS<->LigaMX "
        "scale conversion — a description of three editions, not a "
        "constant to apply"),
        "editions": {}, "unresolved": []}
    pooled_by_host: dict[str, list[float]] = {"mls_hosted": [],
                                              "ligamx_hosted": []}
    same_league = 0
    for season, rows in sorted(rows_by_season.items()):
        ok = [r for r in rows if r.get("status") in TERMINAL]
        by_host: dict[str, list[float]] = {"mls_hosted": [],
                                           "ligamx_hosted": []}
        for r in ok:
            hn = (r.get("home") or {}).get("name") or ""
            an = (r.get("away") or {}).get("name") or ""
            hl, hwhy = club_league(hn, rosters)
            al, awhy = club_league(an, rosters)
            if hl is None or al is None:
                out["unresolved"].append(
                    {"season": season, "home": hn, "away": an,
                     "reason": f"home: {hwhy}; away: {awhy}"})
                continue
            if hl == al:
                same_league += 1
                continue
            g = r["goals"]
            home_pts = (1.0 if g["home"] > g["away"]
                        else 0.5 if g["home"] == g["away"] else 0.0)
            if hl == "mls":
                by_host["mls_hosted"].append(home_pts)
            else:
                by_host["ligamx_hosted"].append(1.0 - home_pts)
        ed = {}
        for host, xs in by_host.items():
            ed[host] = {"n": len(xs),
                        "mls_points_share": (round(sum(xs) / len(xs), 4)
                                             if xs else None),
                        "ci95": _ci(xs, rng) if len(xs) >= 20 else None}
            pooled_by_host[host].extend(xs)
        out["editions"][season] = ed
    out["pooled"] = {}
    for host, xs in pooled_by_host.items():
        out["pooled"][host] = {
            "n": len(xs),
            "mls_points_share": (round(sum(xs) / len(xs), 4) if xs
                                 else None),
            "ci95": _ci(xs, rng) if len(xs) >= 20 else None}
    allx = pooled_by_host["mls_hosted"] + pooled_by_host["ligamx_hosted"]
    out["pooled"]["all_venues"] = {
        "n": len(allx),
        "mls_points_share": (round(sum(allx) / len(allx), 4) if allx
                             else None),
        "ci95": _ci(allx, rng) if len(allx) >= 20 else None,
        "caveat": ("venue mix is not balanced by design — read the "
                   "per-host rows before this one")}
    out["same_league_pairings_excluded"] = same_league
    out["unresolved_count"] = len(out["unresolved"])
    return out


def score_read(rows_by_season: dict[int, list[dict]]) -> dict:
    """The strength read against outcomes — 2025 only, contaminated,
    and refused by name for 2023/2024."""
    out: dict = {
        "refused": {
            "2023_and_2024": (
                "the ratings provider serves CURRENT ratings; a rating "
                "read today contains two years of outcomes including "
                "the ones it would be scored on. Temporal leakage, "
                "AGENTS.md §13 — refused rather than reported")},
        "evidence_class": "contaminated_retrospective",
        "contamination": (
            "2025 outcomes are ~a year inside today's ratings. This "
            "score is CONTEXT for the prospective 2026 corpus, never "
            "grounds for a constant")}
    rows = [r for r in rows_by_season.get(2025, [])
            if r.get("status") in TERMINAL]
    scored = []
    for r in rows:
        st = r.get("strength") or {}
        eps = (st.get("expected_points_share") or {}).get("home") \
            if st.get("available") else None
        if eps is None:
            continue
        g = r["goals"]
        y = (1.0 if g["home"] > g["away"]
             else 0.5 if g["home"] == g["away"] else 0.0)
        scored.append((float(eps), y))
    n = len(scored)
    out["n_2025_scored"] = n
    out["coverage"] = round(n / len(rows), 4) if rows else None
    if n < 20:
        out["conclusion"] = "withheld"
        out["why"] = f"n={n} scored fixtures is below the floor of 20"
        return out
    home_rate = sum(y for _e, y in scored) / n
    def brier(pred):
        return sum((p - y) ** 2 for p, y in pred) / len(pred)
    out["brier_read"] = round(brier(scored), 4)
    out["brier_home_rate_baseline"] = round(
        sum((home_rate - y) ** 2 for _e, y in scored) / n, 4)
    out["brier_coin_flip"] = round(
        sum((0.5 - y) ** 2 for _e, y in scored) / n, 4)
    out["home_rate_2025"] = round(home_rate, 4)
    return out


# --- main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--seasons", default=",".join(map(str, SEASONS)))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if not a.api_base:
        print(json.dumps({"error": "no_api_base", "measured": False,
                          "how_to_fix": "set TRIVELA_API_BASE or pass "
                                        "--api-base"}, indent=2))
        return 2

    seasons = [int(s) for s in a.seasons.split(",") if s.strip()]
    rows_by_season: dict[int, list[dict]] = {}
    fetch_failures: dict[int, str] = {}
    for y in seasons:
        d = _get(a.api_base, "/api/comp/leagues-cup/fixtures",
                 {"season": y, "include_finished": "true"})
        if d is None:
            fetch_failures[y] = ("the season fetch FAILED — this season "
                                 "is missing from every measurement "
                                 "below, not 'had no matches'")
            continue
        rows_by_season[y] = [
            f for f in (d.get("fixtures") or [])
            if (f.get("goals") or {}).get("home") is not None]
    if not rows_by_season:
        print(json.dumps({"error": "no_season_data", "measured": False,
                          "failures": fetch_failures}, indent=2))
        return 2

    rosters, roster_meta = league_map(a.api_base)
    now = datetime.now(UTC)
    doc = {
        "measured_at": now.date().isoformat(),
        "measured_at_utc": now.isoformat(),
        "what_this_is": ("three editions of the Leagues Cup measured "
                         "for the three numbers the viewer surface "
                         "runs on unmeasured"),
        "what_this_is_NOT": ("an edge, a Tie-leg value claim, or a "
                             "fitted constant. Descriptions carry CIs; "
                             "constants need the evaluation this "
                             "document would later be cited by"),
        "seasons_measured": sorted(rows_by_season),
        "season_fetch_failures": fetch_failures,
        "rows_per_season": {y: len(r) for y, r in rows_by_season.items()},
        "league_rosters": roster_meta,
        "draw_rate_90min": draw_rate(rows_by_season),
        "cross_league_balance": cross_league_balance(rows_by_season,
                                                     rosters),
        "strength_read_scored": score_read(rows_by_season),
        "REFUSED_draw_rate_by_closeness": {
            "why": ("measuring the draw rate conditional on how even "
                    "the match was needs a PRE-MATCH evenness number "
                    "per historical fixture. None exists without "
                    "leakage: no books were captured for 2023-2025, "
                    "and today's ratings contain those outcomes. A "
                    "closeness bucket built from either would answer "
                    "the question by peeking at it"),
            "where_it_lives_instead": (
                "the 2026 prospective corpus — T-60 devigged books are "
                "frozen publicly per slate (issue #68) before kickoff. "
                "Bucket THOSE by |home_leg - away_leg| and this "
                "question becomes measurable, cleanly, in one "
                "tournament's time")},
    }
    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
