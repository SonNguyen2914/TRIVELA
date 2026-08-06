#!/usr/bin/env python3
"""When is a book cheapest to cross, and does the crowd drift predictably?

    python scripts/measure_book_timing.py \
        --books research_archive/books/*.json

TWO QUESTIONS THAT LOOK ALIKE AND ARE NOT.

  COST      how wide is the book when you cross it? Directly observable
            from the sum of asks, needs no forecast, and is paid on every
            ticket whether the bet wins or loses.

  DIRECTION where will the price go before kickoff? A forecast, and one
            this project has no established ability to make.

Conflating them is the mistake this script exists to prevent. The
operator asked for an alert for "the best time to place bets", and the
honest answer is that the COST half is measurable now while the
DIRECTION half is not — so the cost half is reported and the direction
half REFUSES until it clears a floor.

WHY A FLOOR AT ALL. The first look at this, on 12 fixtures across two
slates, found starting de-vigged home share correlated with subsequent
drift at r = +0.488, permutation p = 0.109. That is not significant, it
had two clean counterexamples, and the two slates' observation windows
were not even the same length. An alert built on it would be fitting
noise and shipping it as a feature — the exact failure the hosting-offset
work refuses to commit, and this refuses it the same way.

WHAT THE COST HALF ALREADY SHOWS, and why it is worth more than the
forecast would be. On matchday 2 the vig WIDENED into kickoff on 4 of 6
(Nashville 0%->3%, Miami 3%->5%); across the 35 hours before matchday 3
it COMPRESSED on 4 of 6 (NYCFC 4%->0%, Portland 2%->0%). Books tighten
as liquidity arrives and widen again as kickoff nears, so the cheapest
moment to cross is somewhere in the middle rather than at either end.
NYCFC's 4-point swing is roughly twice the size of the drift effect and
requires predicting nothing.

That shape is DESCRIBED here from observed data, per fixture. It is not
extrapolated to fixtures that have not been observed, and the script
says which is which.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import math
import random
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

# A drift relationship is reported as a FINDING only when it clears
# both. n and significance, because either alone is gameable: a huge
# sample can carry a meaningless correlation, and a tiny one can throw
# up a significant-looking accident.
MIN_FIXTURES = 40
MAX_P = 0.05
N_PERM = 20000
SEED = 12345


def _utc(ts):
    if not ts:
        return None
    t = str(ts).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load(paths):
    """Snapshots ordered by their OWN captured_at, never by filename or
    mtime — see the witness note in capture_market_book.py."""
    snaps = []
    for p in paths:
        doc = json.loads(Path(p).read_text())
        at = _utc(doc.get("captured_at"))
        if at is None:
            continue                       # unwitnessed: cannot be ordered
        snaps.append((at, doc))
    return sorted(snaps, key=lambda s: s[0])


def series(snaps):
    """Per event: the observed (lead_seconds, vig, home_share) points."""
    out = {}
    for at, doc in snaps:
        for e in (doc.get("events") or []):
            if not e.get("three_way") or e.get("vig") is None:
                continue
            ko = _utc(e.get("kickoff_utc"))
            if ko is None or at >= ko:
                continue                   # pre-kickoff observations only
            legs = e.get("legs") or []
            asks = {l.get("label"): l.get("yes_ask") for l in legs}
            tie = next((v for k, v in asks.items()
                        if (k or "").strip().lower() == "tie"), None)
            others = [v for k, v in asks.items()
                      if (k or "").strip().lower() != "tie" and v is not None]
            s = e.get("sum_of_asks")
            home_share = None
            # the home leg is the one the fixture payload names; without
            # that we cannot tell the sides apart and record no share
            hn = (e.get("home") or "").strip().lower()
            hv = next((v for k, v in asks.items()
                       if hn and (k or "").strip().lower() in hn), None)
            if hv is not None and tie is not None and s:
                home_share = (hv + tie / 2) / s
            out.setdefault(e["event_ticker"], {
                "home": e.get("home"), "away": e.get("away"),
                "kickoff_utc": e.get("kickoff_utc"), "points": []
            })["points"].append({
                "captured_at": at.isoformat(),
                "lead_seconds": int((ko - at).total_seconds()),
                "vig": e["vig"], "home_share": home_share,
            })
    for v in out.values():
        v["points"].sort(key=lambda p: -p["lead_seconds"])
    return out


def cost_report(ser):
    """The observable half. Per fixture, where the book was cheapest
    among the moments actually observed — never interpolated between
    them, and never extrapolated to a fixture with one observation."""
    rows, single = [], []
    for tk, v in ser.items():
        pts = [p for p in v["points"] if p["vig"] is not None]
        if len(pts) < 2:
            single.append({"event_ticker": tk, "home": v["home"],
                           "n_observations": len(pts),
                           "why": ("one observation cannot show a "
                                   "trajectory; capture more often")})
            continue
        best = min(pts, key=lambda p: p["vig"])
        first, last = pts[0], pts[-1]
        rows.append({
            "event_ticker": tk, "home": v["home"], "away": v["away"],
            "n_observations": len(pts),
            "vig_first": first["vig"],
            "vig_last": last["vig"],
            "vig_cheapest": best["vig"],
            "cheapest_at_lead_minutes": best["lead_seconds"] // 60,
            "observed_window_minutes": [last["lead_seconds"] // 60,
                                        first["lead_seconds"] // 60],
            "cost_of_crossing_at_the_worst_observed_moment":
                round(max(p["vig"] for p in pts) - best["vig"], 4),
        })
    rows.sort(key=lambda r: -(r["cost_of_crossing_at_the_worst_observed_moment"]))
    return {
        "means": ("the cheapest OBSERVED moment to cross each book. Not "
                  "interpolated between observations and not extrapolated "
                  "to other fixtures — a book unobserved at its true "
                  "minimum will report the best moment seen, not the best "
                  "moment there was"),
        "why_this_is_the_reliable_half": (
            "the vig is paid on every ticket whether it wins or loses, "
            "and reading it requires no forecast at all"),
        "fixtures": rows,
        "fixtures_with_one_observation": single,
    }


def drift_report(ser):
    """The forecast half, which refuses."""
    pairs = []
    for tk, v in ser.items():
        pts = [p for p in v["points"] if p["home_share"] is not None]
        if len(pts) < 2:
            continue
        pairs.append({"event_ticker": tk, "home": v["home"],
                      "start_share": round(pts[0]["home_share"], 4),
                      "drift": round(pts[-1]["home_share"]
                                     - pts[0]["home_share"], 4),
                      "window_minutes": [pts[-1]["lead_seconds"] // 60,
                                         pts[0]["lead_seconds"] // 60]})
    n = len(pairs)
    base = {"n_fixtures": n, "floor": MIN_FIXTURES, "max_p": MAX_P,
            "observations": pairs}
    if n < MIN_FIXTURES:
        return {**base, "refused": (
            f"{n} fixtures against a floor of {MIN_FIXTURES}. No drift "
            f"relationship is reported and NO ALERT is issued"),
            "why_a_floor": (
                "the first look at this found r=+0.488 at p=0.109 on 12 "
                "fixtures — not significant, with two clean "
                "counterexamples. An alert built on that would be noise "
                "shipped as a feature")}
    x = [p["start_share"] for p in pairs]
    y = [p["drift"] for p in pairs]

    def corr(a, b):
        ma, mb = st.mean(a), st.mean(b)
        sa, sb = st.pstdev(a), st.pstdev(b)
        if not sa or not sb:
            return 0.0
        return sum((p - ma) * (q - mb) for p, q in zip(a, b)) / n / (sa * sb)

    r = corr(x, y)
    rng = random.Random(SEED)
    hits = 0
    for _ in range(N_PERM):
        s = y[:]
        rng.shuffle(s)
        if abs(corr(x, s)) >= abs(r):
            hits += 1
    p = hits / N_PERM
    c = d = 0
    for i, j in itertools.combinations(range(n), 2):
        if x[i] == x[j] or y[i] == y[j]:
            continue
        if (x[i] > x[j]) == (y[i] > y[j]):
            c += 1
        else:
            d += 1
    out = {**base, "r": round(r, 4), "permutation_p": p,
           "concordance": round(c / (c + d), 4) if c + d else None}
    if p > MAX_P:
        out["refused"] = (f"p={p:.3f} exceeds {MAX_P}. The floor is met "
                          f"but the relationship is not established, so "
                          f"NO ALERT is issued")
        return out
    out["established"] = (
        f"r={r:.3f} at p={p:.3f} over {n} fixtures. This is the first "
        f"point at which a timing alert would rest on measurement rather "
        f"than on a pattern someone liked the look of")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--books", nargs="+", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    paths = sorted({p for pat in a.books for p in glob.glob(pat)} or set(a.books))
    snaps = load(paths)
    if len(snaps) < 2:
        print(json.dumps({"error": "not_enough_snapshots",
                          "n_witnessed": len(snaps),
                          "means": ("book behaviour is a trajectory; two "
                                    "witnessed captures is the minimum. "
                                    "Snapshots without `captured_at` are "
                                    "skipped because they cannot be "
                                    "ordered")}, indent=2))
        return 2
    ser = series(snaps)
    doc = {
        "what_this_is": ("two separate questions about book timing: what "
                         "crossing COSTS, which is observed, and where the "
                         "price DRIFTS, which is forecast and refuses "
                         "until measured"),
        "what_this_is_NOT": ("not an edge claim and not a recommendation "
                             "to bet. The cheapest moment to cross a book "
                             "says nothing about whether crossing it is "
                             "worthwhile"),
        "n_snapshots": len(snaps),
        "span": [snaps[0][0].isoformat(), snaps[-1][0].isoformat()],
        "n_fixtures_observed": len(ser),
        "cost": cost_report(ser),
        "drift": drift_report(ser),
    }
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    else:
        print(text)
    return 0 if "established" in doc["drift"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
