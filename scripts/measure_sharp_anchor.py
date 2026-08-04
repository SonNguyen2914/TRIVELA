#!/usr/bin/env python3
"""Measure our Kalshi book against a sharp external anchor, one slate.

    python scripts/measure_sharp_anchor.py --list-sports
    python scripts/measure_sharp_anchor.py --out research_archive/sharp_anchor_2026-08-04.json

WHY THIS EXISTS. Every disagreement this platform publishes is measured
against ONE market — Kalshi — and Kalshi's own accuracy on these
competitions has never been established here. That leaves a question we
cannot answer from inside: when our number differs from Kalshi's, is that
because Kalshi is soft, or because we are wrong? A second, independently
priced book gives the first purchase on it. If Kalshi and a sharp
bookmaker agree closely, our disagreements are almost certainly ours. If
they diverge, the size and direction of the divergence is a measurement
rather than an argument.

WHAT THIS IS NOT. It is not an edge, and no line of it may be read as
one. It compares TWO PRICES; neither has been scored against outcomes
here. A gap between them is a DISAGREEMENT BETWEEN BOOKS. Nothing in the
output ranks a bet, and no code path here places one.

THE ASK ASYMMETRY, stated up front because it biases every number below.
Our Kalshi legs are derived from `yes_ask` (`src/friendlies.py`
`IMPLIED_BASIS`) — the price you PAY, one side of the spread — while the
anchor's decimal odds are the bookmaker's own offer. Both carry a margin
and both are devigged the same way, but the Kalshi side is still taken
from the expensive edge of a two-sided book. A small positive tilt on the
Kalshi legs is therefore EXPECTED and is not evidence of anything. No
correction is applied, because none has been measured. Read the signed
means with that in mind, and the RANKING (which is dominated by
fixture-level differences, not by a constant) with more confidence than
the level.

DEVIG. Proportional on both sides — p_i = (1/o_i) / sum(1/o_j) for the
anchor, leg/sum(legs) for Kalshi. That is an ASSUMPTION about how the
margin is distributed, not a measurement; the point of using it on both
sides is that a difference in method would be indistinguishable from a
difference in price, which would confound the only thing this measures.

SPORT KEYS ARE DISCOVERED, NEVER GUESSED. `src/competitions.py` carries
the scar: KXSERIEAGAME is ITALY's Serie A, so the plausible guess for
Brasileirão would have attached Italian markets to Brazilian fixtures.
Here the sports list is read from the provider and matched by required
and forbidden tokens; anything matching zero or more than one entry is
recorded as a NAMED ABSENCE and that competition is skipped. Use
`--list-sports` (free, no quota) to see what a key can actually reach,
and `--sport-map FILE` to pin a mapping the operator has verified.

FIXTURES ARE BRIDGED ON BOTH CLUBS. One shared token attaches Real
Sociedad's book to Real Madrid. Both sides must match and the match must
be unique; anything else lands in `unmatched` with a reason.

NO KEY, NO RESULT. Without a credential this exits 2 with a named
refusal. It never falls back to a cached slate, a partial run or an
invented number.

THE KEY IS NEVER PRINTED. It travels in a query parameter (the
provider's design), so no request URL is logged either — only the path.
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
ODDS_BASE = "https://api.the-odds-api.com/v4"
KEY_ENV = "THE_ODDS_API_KEY"
KEY_FILE = Path.home() / ".odds_api_key"
TIMEOUT = 20

# The conventional sharp anchors, in preference order. This is a PRIOR,
# not a measurement — Pinnacle and the Betfair exchange are named because
# they run thin margins and accept size, not because this repo has scored
# them. The rule that fired is recorded on every row, and so is the
# measured overround, so a reader can check the prior rather than take it.
SHARP_PREFERENCE = ("pinnacle", "betfair_ex_eu", "betfair_ex_uk", "betfair")

# Below this, a mean divergence is scatter with a story attached.
MIN_FIXTURES_FOR_SUMMARY = 10

# competition key -> (required tokens, forbidden tokens). Matched against
# the provider's own key + title + description, never against our display
# name alone. Forbidden tokens exist because "europa league" is a strict
# subset of "europa conference league" and a plain required-token match
# would silently pick whichever came first.
SPORT_MATCHERS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "mls": (frozenset({"major", "league", "soccer"}), frozenset({"women"})),
    "leagues-cup": (frozenset({"leagues", "cup"}), frozenset({"women"})),
    "ucl": (frozenset({"champions", "league"}),
            frozenset({"qualification", "women", "conference", "europa",
                       "concacaf", "afc", "caf"})),
    "uel": (frozenset({"europa", "league"}),
            frozenset({"conference", "qualification", "women"})),
    "ecl": (frozenset({"conference", "league"}),
            frozenset({"qualification", "women"})),
    "brasileirao": (frozenset({"brazil", "serie", "a"}),
                    frozenset({"b", "women"})),
    "argentina": (frozenset({"argentina", "primera"}),
                  frozenset({"nacional", "women"})),
    "usl": (frozenset({"usl"}), frozenset({"one", "two", "women", "league"})),
}


# --- the credential --------------------------------------------------------

def load_key() -> tuple[str, str, list[str]]:
    """(key, where_it_came_from, problems). Never returns or logs the key
    itself in any diagnostic — `source` is a NAME, not a value."""
    problems: list[str] = []
    env = (os.getenv(KEY_ENV) or "").strip()
    if env:
        return env, KEY_ENV, problems
    if not KEY_FILE.exists():
        return "", "nowhere", problems
    try:
        mode = KEY_FILE.stat().st_mode & 0o777
    except OSError as exc:
        problems.append(f"could not stat {KEY_FILE.name}: {exc}")
        return "", KEY_FILE.name, problems
    if mode & 0o077:
        problems.append(
            f"{KEY_FILE} is mode {mode:03o}; it must be 600 (owner-only). "
            f"Refusing to read a group- or world-readable secret. Fix with: "
            f"chmod 600 {KEY_FILE}")
        return "", KEY_FILE.name, problems
    try:
        raw = KEY_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"could not read {KEY_FILE.name}: {exc}")
        return "", KEY_FILE.name, problems
    key = raw.strip()                 # dashboard copy-paste smuggles newlines
    if not key:
        problems.append(f"{KEY_FILE} is empty")
        return "", KEY_FILE.name, problems
    return key, KEY_FILE.name, problems


# --- provider I/O ----------------------------------------------------------

def _get(path: str, key: str, params: dict | None = None) -> tuple:
    """(payload, quota, error). The key goes in the query string because
    that is the provider's design; the URL is therefore NEVER logged."""
    p = dict(params or {})
    p["apiKey"] = key
    try:
        r = requests.get(f"{ODDS_BASE}{path}", params=p, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return None, {}, f"{type(exc).__name__} on {path}"
    quota = {"remaining": r.headers.get("x-requests-remaining"),
             "used": r.headers.get("x-requests-used")}
    if r.status_code != 200:
        return None, quota, f"HTTP {r.status_code} on {path}"
    try:
        return r.json(), quota, None
    except ValueError:
        return None, quota, f"non-JSON body on {path}"


# --- name handling ---------------------------------------------------------

def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in n if not unicodedata.combining(c)).lower()


def _toks(s: str) -> frozenset[str]:
    out, cur = [], []
    for ch in _fold(s):
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return frozenset(out)


def _club_toks(s: str) -> frozenset[str]:
    """Identity tokens for a club — the repo's own helper, not a second
    copy of it. `src/competitions.py` bridges Kalshi events with exactly
    this function, so a fixture that bridges there bridges here.

    One refinement on top, and only in the safe direction: tokens with no
    alphanumeric content are dropped. 'U.N.A.M. - Pumas' otherwise yields
    a bare '-', and two unrelated clubs both carrying one would intersect
    on punctuation — a both-sides match made of nothing.
    """
    from src.friendlies import _identity_tokens
    return frozenset(t for t in _identity_tokens(s)
                     if len(t) > 1 and any(c.isalnum() for c in t))


# --- sport-key resolution --------------------------------------------------

def resolve_sport_keys(sports: list[dict],
                       wanted: dict[str, tuple[frozenset, frozenset]],
                       ) -> dict[str, dict]:
    """competition key -> {"sport_key": ...} or {"absent": reason, ...}.

    Zero matches and two matches are BOTH refusals. Picking the first of
    two is how Italy's Serie A ends up priced as Brazil's.
    """
    soccer = [s for s in sports
              if _fold(s.get("group") or "") == "soccer" and s.get("active")]
    out: dict[str, dict] = {}
    for comp, (need, forbid) in wanted.items():
        hits = []
        for s in soccer:
            t = _toks(f"{s.get('key','')} {s.get('title','')} "
                      f"{s.get('description','')}")
            if need <= t and not (forbid & t):
                hits.append(s)
        if len(hits) == 1:
            out[comp] = {"sport_key": hits[0]["key"],
                         "provider_title": hits[0].get("title"),
                         "resolved_by": "token match on the provider's own "
                                        "sports list, unique"}
        elif not hits:
            out[comp] = {"absent": "sport_not_listed",
                         "means": ("the provider lists no active soccer "
                                   "sport matching this competition — NOT "
                                   "'no odds exist'; it is outside what "
                                   "this key can see")}
        else:
            out[comp] = {"absent": "sport_key_ambiguous",
                         "candidates": [h["key"] for h in hits],
                         "means": ("more than one provider sport matches; "
                                   "none is used rather than one guessed. "
                                   "Pin it with --sport-map")}
    return out


# --- devig -----------------------------------------------------------------

def devig_decimal(prices: dict[str, float]) -> dict | None:
    """Decimal odds -> a probability partition, proportionally.

    None whenever the arithmetic cannot be supported: not three legs, or
    a price at or below 1.0 (which implies a probability above 1).
    """
    if len(prices) != 3:
        return None
    imp = {}
    for k, v in prices.items():
        try:
            o = float(v)
        except (TypeError, ValueError):
            return None
        if o <= 1.0:
            return None
        imp[k] = 1.0 / o
    total = sum(imp.values())
    if total <= 0:
        return None
    return {"normalised": {k: round(v / total, 4) for k, v in imp.items()},
            "overround": round(total - 1.0, 4)}


def pick_anchor(bookmakers: list[dict], home: str, away: str) -> dict | None:
    """The sharpest listed h2h book for one event, devigged.

    A named sharp book wins when it is listed and prices all three legs;
    otherwise the lowest measured overround does. The rule that fired is
    on the row — the preference list is a prior, and a reader must be
    able to see when it was used.
    """
    priced = []
    for b in bookmakers or []:
        h2h = next((m for m in b.get("markets") or []
                    if m.get("key") == "h2h"), None)
        if not h2h:
            continue
        legs: dict[str, float] = {}
        for o in h2h.get("outcomes") or []:
            name = o.get("name") or ""
            if _fold(name) == _fold(home):
                slot = "home"
            elif _fold(name) == _fold(away):
                slot = "away"
            elif _fold(name) in ("draw", "tie"):
                slot = "tie"
            else:
                continue
            legs[slot] = o.get("price")
        if set(legs) != {"home", "tie", "away"}:
            continue
        d = devig_decimal(legs)
        if d:
            priced.append((b.get("key"), b.get("title"), d))
    if not priced:
        return None
    for name in SHARP_PREFERENCE:
        hit = next((p for p in priced if p[0] == name), None)
        if hit:
            return {"book": hit[0], "book_title": hit[1],
                    "rule": f"named_sharp:{name}",
                    "overround": hit[2]["overround"],
                    "legs": hit[2]["normalised"],
                    "books_priced": len(priced)}
    best = min(priced, key=lambda p: p[2]["overround"])
    return {"book": best[0], "book_title": best[1],
            "rule": "lowest_overround",
            "rule_note": ("no book from the preference list was listed for "
                          "this event, so the thinnest margin stood in. "
                          "That is a proxy for sharpness, not sharpness"),
            "overround": best[2]["overround"],
            "legs": best[2]["normalised"],
            "books_priced": len(priced)}


# --- our side --------------------------------------------------------------

def our_slate(comp: str, api_base: str | None, season: int) -> dict:
    """The viewer surface for one competition, from the deployed route or
    in process. Both return the SAME object — the route is a thin wrapper
    over `competitions.fixtures` — so nothing downstream branches on it."""
    if api_base:
        try:
            r = requests.get(f"{api_base.rstrip('/')}/api/comp/{comp}/fixtures",
                             params={"season": season}, timeout=60)
            r.raise_for_status()
            return r.json() or {}
        except (requests.RequestException, ValueError) as exc:
            return {"error": f"{type(exc).__name__}"}
    from src import competitions
    return competitions.fixtures(comp, season) or {}


def our_legs(fixture: dict) -> dict | None:
    """home/tie/away, vig already removed by the viewer surface."""
    tw = ((fixture.get("market_vs_read") or {}).get("market_three_way")
          or {})
    legs = {}
    for slot in ("home", "tie", "away"):
        p = (tw.get(slot) or {}).get("p")
        if not isinstance(p, (int, float)):
            return None
        legs[slot] = round(float(p), 4)
    return legs


# --- bridge and compare ----------------------------------------------------

def bridge(rows: list[dict], events: list[dict]) -> tuple[list, list]:
    """(pairs, unmatched). BOTH clubs must match, and uniquely."""
    tagged = [(e, _club_toks(e.get("home_team") or ""),
               _club_toks(e.get("away_team") or "")) for e in events]
    pairs, unmatched = [], []
    for r in rows:
        hn = (r.get("home") or {}).get("name") or ""
        an = (r.get("away") or {}).get("name") or ""
        ht, at = _club_toks(hn), _club_toks(an)
        if not ht or not at:
            unmatched.append({"home": hn, "away": an,
                              "reason": "no identity tokens on a side"})
            continue
        # orientation-agnostic: the two providers disagree about which
        # club is "home" often enough (neutral venues, tour matches) that
        # requiring the same orientation would drop real matches. The
        # SIDES are still resolved by name, never by position.
        hits = [e for e, eh, ea in tagged
                if ((ht & eh) and (at & ea)) or ((ht & ea) and (at & eh))]
        if len(hits) != 1:
            unmatched.append({
                "home": hn, "away": an,
                "reason": ("no anchor event matches both clubs"
                           if not hits else
                           f"{len(hits)} anchor events match both clubs; "
                           f"none is attached rather than one guessed")})
            continue
        pairs.append((r, hits[0]))
    return pairs, unmatched


def compare_one(comp: str, row: dict, event: dict) -> dict | None:
    ours = our_legs(row)
    if ours is None:
        return {"competition": comp, "skipped": "no_kalshi_three_way",
                "home": (row.get("home") or {}).get("name"),
                "away": (row.get("away") or {}).get("name"),
                "means": ("our own surface could not form a three-leg "
                          "partition for this fixture, so there is nothing "
                          "to compare — not 'the books agree'")}
    anchor = pick_anchor(event.get("bookmakers"),
                         event.get("home_team") or "",
                         event.get("away_team") or "")
    if anchor is None:
        return {"competition": comp, "skipped": "no_anchor_three_way",
                "home": (row.get("home") or {}).get("name"),
                "away": (row.get("away") or {}).get("name"),
                "means": ("no listed bookmaker prices all three legs of "
                          "this event")}
    # If the two providers list the clubs in opposite orientations, the
    # anchor's home/away must be flipped onto OUR orientation before any
    # subtraction. Getting this wrong inverts the largest divergences.
    ht = _club_toks((row.get("home") or {}).get("name") or "")
    flipped = not (ht & _club_toks(event.get("home_team") or ""))
    a = dict(anchor["legs"])
    if flipped:
        a["home"], a["away"] = a["away"], a["home"]
    delta = {k: round(ours[k] - a[k], 4) for k in ("home", "tie", "away")}
    return {
        "competition": comp,
        "kickoff_utc": row.get("kickoff_utc"),
        "home": (row.get("home") or {}).get("name"),
        "away": (row.get("away") or {}).get("name"),
        "kalshi_event": row.get("kalshi_event"),
        "anchor_book": anchor["book"],
        "anchor_rule": anchor["rule"],
        "anchor_books_priced": anchor["books_priced"],
        "anchor_overround": anchor["overround"],
        "kalshi_vig": (row.get("market_vs_read") or {}).get("market_vig"),
        "orientation_flipped": flipped,
        "ours": ours,
        "anchor": {k: round(v, 4) for k, v in a.items()},
        "delta_ours_minus_anchor": delta,
        "max_abs_divergence": round(max(abs(v) for v in delta.values()), 4),
        "l1_divergence": round(sum(abs(v) for v in delta.values()), 4),
    }


# --- summary ---------------------------------------------------------------

def _boot_ci(xs: list[float], seed: int = 12345) -> list[float]:
    rng = random.Random(seed)
    means = []
    for _ in range(2000):
        s = [xs[rng.randrange(len(xs))] for _ in range(len(xs))]
        means.append(sum(s) / len(s))
    means.sort()
    return [round(means[int(0.025 * len(means))], 4),
            round(means[int(0.975 * len(means))], 4)]


def summarise(rows: list[dict]) -> dict:
    scored = [r for r in rows if "max_abs_divergence" in r]
    n = len(scored)
    base = {"n_compared": n,
            "floor": MIN_FIXTURES_FOR_SUMMARY,
            "what_a_divergence_is": (
                "a disagreement between two BOOKS. Neither has been scored "
                "against outcomes here, so this ranks nothing and "
                "recommends nothing")}
    if n < MIN_FIXTURES_FOR_SUMMARY:
        return {**base, "conclusion": "withheld",
                "why": (f"n={n} is below the floor of "
                        f"{MIN_FIXTURES_FOR_SUMMARY}. A mean over fewer "
                        f"fixtures than that is scatter with a story "
                        f"attached")}
    home = [r["delta_ours_minus_anchor"]["home"] for r in scored]
    absd = [r["max_abs_divergence"] for r in scored]
    lo, hi = _boot_ci(home)
    return {**base,
            "mean_abs_divergence": round(sum(absd) / n, 4),
            "median_abs_divergence": round(sorted(absd)[n // 2], 4),
            "mean_signed_home_leg": round(sum(home) / n, 4),
            "ci95_signed_home_leg": [lo, hi],
            "significant": bool(lo > 0 or hi < 0),
            "significance_caveat": (
                "a significant POSITIVE mean on the Kalshi side is the "
                "expected consequence of pricing our legs off yes_ask, "
                "not a finding about either book. See the ask asymmetry "
                "in this script's docstring"),
            "by_competition": {
                c: round(sum(r["max_abs_divergence"] for r in scored
                             if r["competition"] == c)
                         / max(sum(1 for r in scored
                                   if r["competition"] == c), 1), 4)
                for c in sorted({r["competition"] for r in scored})}}


# --- main ------------------------------------------------------------------

def _refuse(reason: str, **extra) -> int:
    print(json.dumps({"error": reason, "measured": False, **extra,
                      "means": "no measurement was produced. This is an "
                               "inability to look, never a result",
                      "how_to_fix": (
                          f"put a free the-odds-api.com key in ${KEY_ENV} "
                          f"or in {KEY_FILE} (chmod 600), then re-run")},
                     indent=2))
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare our Kalshi legs against a sharp external book.")
    ap.add_argument("--regions", default="us,uk,eu",
                    help="provider regions; each one multiplies quota cost")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--competitions", default=None,
                    help="comma-separated subset; default is every "
                         "competition we carry a Kalshi series for")
    ap.add_argument("--sport-map", default=None,
                    help="JSON file of {competition: provider_sport_key} "
                         "pinning a mapping the operator has verified")
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"),
                    help="read our side from a deployed instance instead "
                         "of calling Kalshi in process")
    ap.add_argument("--list-sports", action="store_true",
                    help="print the soccer sports this key can see, and "
                         "how each competition resolves. Costs no quota")
    ap.add_argument("--out", default=None,
                    help="also write the archive document here")
    a = ap.parse_args()

    key, source, problems = load_key()
    if not key:
        return _refuse("no_odds_api_key", key_source=source,
                       problems=problems)

    sports, quota, err = _get("/sports", key)
    if err or sports is None:
        return _refuse("sports_list_unavailable", detail=err,
                       key_source=source)

    wanted = dict(SPORT_MATCHERS)
    if a.competitions:
        pick = {c.strip() for c in a.competitions.split(",") if c.strip()}
        unknown = pick - set(wanted)
        if unknown:
            print(json.dumps({"error": "unknown_competition",
                              "unknown": sorted(unknown),
                              "known": sorted(wanted)}, indent=2))
            return 2
        wanted = {k: v for k, v in wanted.items() if k in pick}

    resolved = resolve_sport_keys(sports, wanted)
    if a.sport_map:
        pinned = json.loads(Path(a.sport_map).read_text(encoding="utf-8"))
        for comp, sk in pinned.items():
            if comp in resolved:
                resolved[comp] = {"sport_key": sk,
                                  "resolved_by": f"pinned by {a.sport_map}"}

    if a.list_sports:
        print(json.dumps({
            "soccer_sports": sorted(
                ({"key": s.get("key"), "title": s.get("title"),
                  "description": s.get("description")}
                 for s in sports
                 if _fold(s.get("group") or "") == "soccer"
                 and s.get("active")),
                key=lambda s: s["key"] or ""),
            "resolution": resolved,
            "quota": quota,
            "key_source": source,
            "note": "the sports list is free; no odds quota was spent",
        }, indent=2))
        return 0

    compared: list[dict] = []
    unmatched: list[dict] = []
    for comp, res in resolved.items():
        if "sport_key" not in res:
            continue
        odds, quota, err = _get(f"/sports/{res['sport_key']}/odds", key,
                                {"regions": a.regions, "markets": "h2h",
                                 "oddsFormat": "decimal", "dateFormat": "iso"})
        if err or odds is None:
            res["odds_error"] = err
            res["means"] = ("the anchor fetch FAILED for this competition — "
                            "this is not 'the books agree'")
            continue
        ours = our_slate(comp, a.api_base, a.season)
        if ours.get("error"):
            res["our_side_error"] = ours["error"]
            continue
        rows = ours.get("fixtures") or []
        res["our_fixtures"] = len(rows)
        res["anchor_events"] = len(odds)
        pairs, un = bridge(rows, odds)
        res["bridged"] = len(pairs)
        for u in un:
            unmatched.append({"competition": comp, **u})
        for row, ev in pairs:
            out = compare_one(comp, row, ev)
            if out:
                compared.append(out)

    compared.sort(key=lambda r: r.get("max_abs_divergence", -1), reverse=True)
    now = datetime.now(UTC)
    doc = {
        "measured_at": now.date().isoformat(),
        "measured_at_utc": now.isoformat(),
        "what_this_is": ("one slate of our Kalshi legs beside the sharpest "
                         "listed bookmaker's, both devigged proportionally"),
        "what_this_is_NOT": ("an edge, a recommendation, or evidence that "
                             "either book is right. Neither side has been "
                             "scored against outcomes here"),
        "ask_asymmetry": ("our legs come from yes_ask, the anchor's from "
                          "the bookmaker's offer. A small positive tilt on "
                          "our side is expected and uncorrected, because no "
                          "correction has been measured"),
        "devig_method": ("proportional on BOTH sides — a difference in "
                         "method would be indistinguishable from a "
                         "difference in price"),
        "anchor_provider": "the-odds-api.com v4",
        "anchor_selection": {
            "preference": list(SHARP_PREFERENCE),
            "fallback": "lowest measured overround",
            "status": ("the preference list is a PRIOR about which books "
                       "run thin and take size. It has not been scored "
                       "here; every row records which rule fired")},
        "regions": a.regions,
        "key_source": source,
        "key_problems": problems,
        "quota": quota,
        "our_side": ("deployed instance" if a.api_base
                     else "in-process src.competitions"),
        "competitions": resolved,
        "summary": summarise(compared),
        "ranked_by_max_abs_divergence": compared,
        "unmatched": unmatched,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
