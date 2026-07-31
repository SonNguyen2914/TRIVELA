#!/usr/bin/env python3
"""Measure how well the friendlies strength read predicts, and fit its shrink.

    python scripts/measure_friendly_calibration.py [--days 45] [--out FILE]

WHY THIS EXISTS. The strength read is published as EXTERNAL_UNEVALUATED —
an external number nobody had ever checked. Friendlies have results, so
"is it any good" is measurable rather than arguable. Measured 2026-07-30
over 45 days and 626 scored friendlies, the RAW read is slightly WORSE
than a coin flip (Brier 0.2027 vs 0.1973 out of sample). It is not noise:
it is systematically OVERCONFIDENT, predicting 0.235 where reality is
0.412 and 0.766 where reality is 0.682. Club ratings are fitted on
full-strength league football; friendlies are rotation-heavy and
low-motivation, so real outcomes compress toward the middle.

WHAT IT FITS, AND WHAT IT REFUSES TO. One parameter: a shrink toward 0.5.
A home-advantage term was measured and DROPPED — it adds only +0.0040 and
the edge is +0.045 where the venue is null against +0.018 where it is
known, which is backwards for a real home effect. Pre-season friendlies
are full of neutral grounds and tour matches, so the provider's "home"
label frequently does not mean home. One well-founded correction beats two
where the second may be an artifact.

DISCIPLINE. Fitted on the OLDER half, scored on the NEWER half, with a
bootstrap CI on the improvement — the same bar every approval in this repo
carries. A constant that cannot clear a CI excluding zero does not ship.

SCOPE LIMIT, deliberately narrow: this is fitted on PRE-SEASON friendlies.
Mid-season international-break friendlies are a different population and
these constants are not known to hold there. Re-run this before trusting
them in another window.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests


def collect(days: int, today: date) -> list[dict]:
    from src.friendlies_apif import (APIF_BASE, APIF_TIMEOUT, load_key,
                                     parse_fixture, response_items)
    key, _s, _p = load_key()
    if not key:
        return []
    out = []
    for d in range(1, days + 1):
        day = (today - timedelta(days=d)).isoformat()
        try:
            r = requests.get(f"{APIF_BASE}/fixtures", params={"date": day},
                             headers={"x-apisports-key": key},
                             timeout=APIF_TIMEOUT)
            if r.status_code != 200:
                continue
            for x in response_items(r.json()):
                p = parse_fixture(x)
                if (p and p.get("is_classified_friendly")
                        and p.get("status") in ("FT", "AET", "PEN")
                        and (p.get("goals") or {}).get("home") is not None):
                    out.append(p)
        except requests.RequestException:
            continue
    out.sort(key=lambda f: f.get("kickoff_utc") or "")
    return out


def score(rows: list[dict]) -> list[tuple[float, float, bool]]:
    """(predicted home points share, actual, venue_known)."""
    from src.live import club_strength_estimate as cse
    sc = []
    for f in rows:
        e = (cse.for_fixture(f) or {}).get("expected_points_share")
        if not e:
            continue
        g = f["goals"]
        y = (1.0 if g["home"] > g["away"]
             else (0.5 if g["home"] == g["away"] else 0.0))
        sc.append((e["home"], y, bool(f.get("venue"))))
    return sc


def brier(d, k: float = 1.0) -> float:
    return sum(((0.5 + k * (p - 0.5)) - y) ** 2 for p, y in d) / len(d)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--out", default=None)
    ap.add_argument("--today", default=None)
    a = ap.parse_args()
    today = date.fromisoformat(a.today) if a.today else date.today()

    rows = collect(a.days, today)
    sc = score(rows)
    if len(sc) < 60:
        print(json.dumps({"error": "too few scored friendlies",
                          "completed": len(rows), "scored": len(sc)}))
        return 2

    pairs = [(p, y) for p, y, _ in sc]
    cut = len(pairs) // 2
    train, test = pairs[:cut], pairs[cut:]

    k = min(((brier(train, ki / 100), ki / 100) for ki in range(0, 101)))[1]
    raw, cal = brier(test), brier(test, k)
    coin = sum((0.5 - y) ** 2 for _, y in test) / len(test)

    rng = random.Random(12345)
    diffs = []
    for _ in range(2000):
        s = [test[rng.randrange(len(test))] for _ in range(len(test))]
        diffs.append(brier(s) - brier(s, k))
    diffs.sort()
    lo, hi = diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]

    buckets = []
    for b_lo, b_hi in ((0.0, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 1.01)):
        b = [(p, y) for p, y in pairs if b_lo <= p < b_hi]
        if b:
            buckets.append({"range": [b_lo, b_hi], "n": len(b),
                            "mean_predicted": round(sum(p for p, _ in b) / len(b), 4),
                            "mean_actual": round(sum(y for _, y in b) / len(b), 4)})

    known = [x for x in sc if x[2]]
    null_v = [x for x in sc if not x[2]]

    def edge(g):
        if len(g) < 20:
            return None
        return round(sum(y for _, y, _ in g) / len(g)
                     - sum(p for p, _, _ in g) / len(g), 4)

    doc = {
        "measured_at": today.isoformat(),
        "window_days": a.days,
        "completed_friendlies": len(rows),
        "scored": len(sc),
        "coverage": round(len(sc) / max(len(rows), 1), 4),
        "method": ("fitted on the OLDER half, scored on the NEWER half, "
                   "bootstrap CI (2000 resamples) on the improvement"),
        "fitted_shrink_k": k,
        "out_of_sample": {
            "n_test": len(test),
            "brier_raw": round(raw, 4),
            "brier_coin_flip": round(coin, 4),
            "brier_calibrated": round(cal, 4),
            "improvement_vs_raw": round(raw - cal, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "significant": bool(lo > 0),
        },
        "calibration_buckets": buckets,
        "home_term_REJECTED": {
            "why": ("adds only ~+0.004 Brier, and the apparent edge is "
                    "LARGER where the venue is unknown than where it is "
                    "known — backwards for a real home effect. Pre-season "
                    "friendlies are often at neutral or touring grounds, so "
                    "the provider's home label frequently does not mean "
                    "home."),
            "edge_venue_known": edge(known),
            "edge_venue_null": edge(null_v),
            "n_venue_known": len(known), "n_venue_null": len(null_v),
        },
        "scope_limit": ("fitted on PRE-SEASON friendlies. Mid-season "
                        "international-break friendlies are a different "
                        "population; re-run before trusting these there."),
        "what_this_does_NOT_do": ("direction accuracy is ~59-61% before and "
                                  "after — calibration corrects CONFIDENCE, "
                                  "not skill. It stops the read overclaiming; "
                                  "it does not make friendlies predictable."),
    }
    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        open(a.out, "w").write(text)
        print(f"wrote {a.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
