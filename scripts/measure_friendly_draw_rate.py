"""Can we estimate the draw probability ourselves?

Neither ratings provider publishes a 1X2 split, which is why this surface
has only ever shown a two-way points share. That is a fact about the
PROVIDERS, not about what is knowable: friendlies settle, so the draw
rate is measurable on our own results like any other quantity here.

THE CONSTRAINT THAT SHAPES THE MODEL. Given a points share E and a draw
probability d, the split is forced:

    P(home) = E - d/2        P(away) = 1 - E - d/2

so d <= 2*min(E, 1-E) or a probability goes NEGATIVE. A constant draw
rate is therefore not merely crude, it is invalid: at E=0.93 a flat 26%
draw gives P(away) = -0.06. Any usable form must send d to zero as the
mismatch grows. This fits

    d(E) = dmax * (4*E*(1-E))**gamma

which is dmax at E=0.5 and 0 at the extremes, by construction.

Fitted on the older half, scored on the newer half, against a constant-d
control. If the fitted form does not beat the control out of sample, the
answer is that we cannot estimate it and no draw number should ship.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, timedelta

import requests


def corpus(days: int, today: date) -> list[dict]:
    from src.friendlies_apif import (APIF_BASE, APIF_TIMEOUT, load_key,
                                     parse_fixture, response_items)
    key, _s, _p = load_key()
    if not key:
        raise SystemExit("no API-Football key")
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


def scored(rows: list[dict]) -> list[tuple[float, int]]:
    """(expected points share, outcome) with outcome 0=home,1=draw,2=away.

    PEN and AET are counted by the 90-minute score the provider reports as
    `goals`, matching how the market settles a regulation result.
    """
    from src.live import club_strength_estimate as cse
    out = []
    for f in rows:
        est = cse.for_fixture(f) or {}
        e = est.get("expected_points_share")
        if not e:
            continue
        g = f["goals"]
        o = 0 if g["home"] > g["away"] else (1 if g["home"] == g["away"] else 2)
        out.append((float(e["home"]), o))
    return out


def split(e: float, d: float) -> tuple[float, float, float]:
    """(home, draw, away), clipped so no leg is negative."""
    d = max(0.0, min(d, 2 * min(e, 1 - e)))
    h, a = e - d / 2, 1 - e - d / 2
    h, a = max(h, 1e-9), max(a, 1e-9)
    s = h + d + a
    return h / s, d / s, a / s


def logloss(data, fn) -> float:
    tot = 0.0
    for e, o in data:
        p = split(e, fn(e))[o]
        tot -= math.log(max(p, 1e-12))
    return tot / len(data)


def brier3(data, fn) -> float:
    tot = 0.0
    for e, o in data:
        p = split(e, fn(e))
        tot += sum((p[i] - (1.0 if i == o else 0.0)) ** 2 for i in range(3))
    return tot / len(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    rows = corpus(a.days, date.today())
    data = scored(rows)
    n = len(data)
    print(f"  completed friendlies swept : {len(rows)}")
    print(f"  with a strength read       : {n}")
    if n < 120:
        print("  TOO FEW to fit and hold out — no draw estimate.")
        return 1

    half = n // 2
    fit, held = data[:half], data[half:]
    base_rate = sum(1 for _, o in fit if o == 1) / len(fit)
    print(f"\n  unconditional draw rate (fit half) : {base_rate:.4f}")
    print(f"  unconditional draw rate (held half): "
          f"{sum(1 for _, o in held if o == 1) / len(held):.4f}")

    # control: a constant draw rate, clipped by the same constraint
    const = lambda _e: base_rate                                  # noqa: E731

    best, grid = None, []
    for dmax in [x / 100 for x in range(14, 46, 1)]:
        for gamma in [x / 10 for x in range(2, 31)]:
            f = (lambda dm, gm: (lambda e: dm * (4 * e * (1 - e)) ** gm))(
                dmax, gamma)
            ll = logloss(fit, f)
            grid.append((ll, dmax, gamma))
            if best is None or ll < best[0]:
                best = (ll, dmax, gamma, f)
    _, dmax, gamma, fitted = best
    print(f"  fitted  dmax={dmax:.2f}  gamma={gamma:.1f}")

    print("\n  HELD-OUT (never seen by the fit)")
    for name, f in (("constant draw", const), ("fitted d(E)", fitted)):
        print(f"    {name:14} logloss {logloss(held, f):.4f} "
              f"| brier3 {brier3(held, f):.4f}")
    gain_ll = logloss(held, const) - logloss(held, fitted)
    gain_br = brier3(held, const) - brier3(held, fitted)
    print(f"    fitted beats constant by  logloss {gain_ll:+.4f} "
          f"| brier3 {gain_br:+.4f}")

    # how often the constant would have produced an impossible split
    bad = sum(1 for e, _ in data if base_rate > 2 * min(e, 1 - e))
    print(f"\n  fixtures where a CONSTANT draw rate is invalid "
          f"(implies a negative leg): {bad}/{n} = {bad / n:.1%}")

    # calibration of the fitted draw, by predicted-draw bucket
    print("\n  draw calibration (held-out)")
    buckets = [(0, .18), (.18, .24), (.24, .28), (.28, 1)]
    for lo, hi in buckets:
        sel = [(e, o) for e, o in held if lo <= fitted(e) < hi]
        if len(sel) < 15:
            continue
        pred = sum(split(e, fitted(e))[1] for e, _ in sel) / len(sel)
        act = sum(1 for _, o in sel if o == 1) / len(sel)
        print(f"    d in [{lo:.2f},{hi:.2f})  n={len(sel):4}  "
              f"predicted {pred:.3f}  actual {act:.3f}")

    # THE VERDICT MUST NOT REST ON LOGLOSS. Unbounded, it is dominated by
    # whichever model avoids a near-zero leg on a single upset: measured
    # 2026-07-31, the worst FIVE held-out fixtures supplied 103% of the
    # fitted form's logloss advantage, and one 0.069-share underdog that
    # won supplied most of that alone. Brier is bounded and said +0.0001 —
    # nil — over the same data. So Brier decides, logloss is reported, and
    # the concentration of the logloss gap is reported beside it.
    per = sorted((logloss([d], const) - logloss([d], fitted) for d in held),
                 reverse=True)
    total = sum(per) or 1e-12
    top5_share = sum(per[:5]) / total
    worse = sum(1 for x in per if x < 0)
    print(f"\n  logloss gap concentration: worst 5 fixtures supply "
          f"{top5_share:.1%} of it")
    print(f"  fitted is WORSE than constant on {worse}/{len(held)} fixtures")
    print(f"  median per-fixture logloss advantage: "
          f"{per[len(per) // 2]:+.4f}")

    verdict = ("USABLE" if gain_br > 0.002 else
               "NOT BETTER THAN A CONSTANT — do not ship a fitted draw")
    print(f"\n  VERDICT: {verdict}")

    if a.out:
        json.dump({"measured_at": date.today().isoformat(), "n": n,
                   "fit_n": len(fit), "held_n": len(held),
                   "unconditional_draw_rate": round(base_rate, 4),
                   "fitted": {"dmax": dmax, "gamma": gamma,
                              "form": "d(E) = dmax * (4E(1-E))**gamma"},
                   "held_out": {
                       "constant_logloss": round(logloss(held, const), 4),
                       "fitted_logloss": round(logloss(held, fitted), 4),
                       "constant_brier3": round(brier3(held, const), 4),
                       "fitted_brier3": round(brier3(held, fitted), 4)},
                   "constant_invalid_share": round(bad / n, 4),
                   "logloss_gap_top5_share": round(top5_share, 4),
                   "fitted_worse_on": worse,
                   "verdict": verdict,
                   "why_brier_decides": (
                       "logloss is unbounded and was dominated by a single "
                       "0.069-share underdog that won, where the constant "
                       "implied a near-zero leg; Brier is bounded and "
                       "showed no gain"),
                   "population_caveat": (
                       "measured while clubelo was unreachable, so the "
                       "corpus is worldclubratings-only and NOT the "
                       "population this would serve")},
                  open(a.out, "w"), indent=2)
        print(f"  archived -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
