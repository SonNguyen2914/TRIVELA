#!/usr/bin/env python3
"""Measure the ask-to-mid tilt on Kalshi's soccer books — the sharp
anchor's stated blind spot, quantified.

    python scripts/measure_ask_mid_tilt.py \
        --out research_archive/ask_mid_tilt_$(date -u +%F).json

WHY. Every sharp-anchor comparison devigs OUR legs from `yes_ask` (the
price you pay) while the anchor's odds are the bookmaker's own offer.
The script's docstring has warned since day one that a small positive
tilt on the Kalshi side is EXPECTED and uncorrected "because none has
been measured". This measures it: for every tradeable three-way book
across the competitions we carry, the home points share devigged from
ASKS minus the same share devigged from MIDS ((bid+ask)/2). The
distribution of that difference IS the correction candidate.

WHAT IT IS NOT. Not a correction applied anywhere — a measurement,
archived, for the operator to decide with. One-sided books (any leg
with a zero bid) are EXCLUDED and counted, because a mid of half the
ask is fiction; the exclusion rate is itself part of the answer.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SERIES = {
    "mls": "KXMLSGAME",
    "leagues-cup": "KXLEAGUESCUPGAME",
    "asean": "KXASEANGAME",
    "ucl": "KXUCLGAME",
    "uel": "KXUELGAME",
    "ecl": "KXUECLGAME",
    "brasileirao": "KXBRASILEIROGAME",
    "argentina": "KXARGPREMDIVGAME",
    "usl": "KXUSLGAME",
}


def _cents(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0 < f < 100 else (f * 100 if 0 < f < 1 else None)


def book_tilt(markets: list[dict]) -> dict | None:
    """One event's ask-share minus mid-share for the FIRST listed leg
    (a stable, arbitrary anchor — the tilt is a property of the book's
    spread structure, not of which side is home; per-leg tilts are also
    returned so nothing hides in the choice)."""
    legs = []
    for m in markets or []:
        if m.get("status") != "active":
            continue
        # Kalshi's nested-events payload quotes in the *_dollars string
        # family; the bare-cents names ride along as None. Read dollars
        # first, legacy second.
        ask = _cents(m.get("yes_ask_dollars") or m.get("yes_ask"))
        bid = _cents(m.get("yes_bid_dollars") or m.get("yes_bid"))
        if ask is None:
            return None
        if bid is None or bid <= 0:
            return {"excluded": "one_sided"}
        legs.append((ask, (ask + bid) / 2.0))
    if len(legs) != 3:
        return None
    ask_tot = sum(a for a, _ in legs)
    mid_tot = sum(m for _, m in legs)
    if ask_tot <= 0 or mid_tot <= 0:
        return None
    per_leg = [round(a / ask_tot - m / mid_tot, 5) for a, m in legs]
    return {"per_leg_tilt": per_leg,
            "max_abs_leg_tilt": max(abs(t) for t in per_leg),
            "ask_overround": round(ask_tot / 100 - 1, 4),
            "mid_overround": round(mid_tot / 100 - 1, 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    from src import friendlies

    rows, excluded = [], 0
    per_comp: dict[str, list[float]] = {}
    for comp, series in SERIES.items():
        d = friendlies._get_json(
            f"{friendlies.KALSHI_BASE}/events",
            {"series_ticker": series, "limit": 200,
             "with_nested_markets": "true"})
        if d is None:
            per_comp[comp] = []
            continue
        for e in d.get("events") or []:
            t = book_tilt(e.get("markets"))
            if t is None:
                continue
            if t.get("excluded"):
                excluded += 1
                continue
            rows.append({"competition": comp,
                         "event": e.get("event_ticker"), **t})
            per_comp.setdefault(comp, []).append(t["max_abs_leg_tilt"])

    tilts = [abs(t) for r in rows for t in r["per_leg_tilt"]]
    doc = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "what_this_is": ("the devig tilt from pricing legs off yes_ask "
                         "instead of the bid/ask mid, per tradeable "
                         "three-way book"),
        "what_this_is_not": ("a correction — nothing anywhere applies "
                             "this number; it is the measurement the "
                             "sharp anchor's caveat has been waiting on"),
        "n_books": len(rows),
        "n_excluded_one_sided": excluded,
        "abs_leg_tilt": ({
            "mean": round(statistics.mean(tilts), 5),
            "median": round(statistics.median(tilts), 5),
            "p90": round(sorted(tilts)[int(0.9 * len(tilts))], 5),
            "max": round(max(tilts), 5)} if tilts else None),
        "by_competition_mean_max_abs": {
            c: round(statistics.mean(v), 5)
            for c, v in per_comp.items() if v},
        "books": rows,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        from pathlib import Path
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    print(json.dumps({k: v for k, v in doc.items() if k != "books"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
