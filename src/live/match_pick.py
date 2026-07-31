"""The market beside the strength read, on ONE scale — and a stated pick.

THE COMPARISON IS LEGITIMATE, and earlier it was not. The match page
refused to subtract the two numbers because a three-way market price and a
two-way expected points share are different quantities. That objection is
answered by converting the market into the SAME quantity:

    market points share (home) = P(home) + 0.5 * P(draw)

with the vig removed by normalising the three legs to sum to 1. Both sides
are then "expected share of the points, draws counted as half", and their
difference means something.

WHAT THE DIFFERENCE IS NOT. It is a DISAGREEMENT, not a verified edge. To
call it an edge we would have to know our read is better than the market,
and we do not:

  - our calibrated read scores Brier 0.1892 against a coin flip's 0.1973 —
    a real but small improvement (+0.0134, CI [+0.0040, +0.0233]);
  - the market's pre-match skill on friendlies is UNMEASURED here. An
    attempt to measure it from settled Kalshi books was CIRCULAR and
    thrown out: `last_price_dollars` on a settled market has already
    converged to 0 or 1, so it scores the outcome against itself (it
    produced an impossible Brier of 0.0002).

Priors say an exchange with real money on it beats a shrunk external Elo,
so a disagreement is more likely OUR error than a mispricing. The
vocabulary here says "disagreement" throughout and never "edge", "value"
or "opportunity", and the pick states its own basis rather than implying
one.
"""
from __future__ import annotations

# MEASURED, not chosen by feel. Across 29 fixtures carrying both a market
# and a read (UCL, UEL, Argentina, friendlies, 2026-07-30) the gap between
# the two had sd 0.0924 and a mean of -0.0155 with 95% CI [-0.0491,
# +0.0181] — i.e. NO significant systematic offset, just spread. The band
# is ~0.54 sd: inside it, a gap is indistinguishable from the noise the two
# sources show against each other anyway, so calling it a disagreement
# would be reading signal out of scatter.
AGREE_BAND = 0.05
AGREE_BAND_BASIS = (
    "0.05 = ~0.54 sd of the measured market-vs-read gap (sd 0.0924, n=29, "
    "2026-07-30). Inside it the two sources are not distinguishable from "
    "their own scatter")

DISAGREEMENT_NOTE = (
    "our read minus the market's, on the same scale (expected points "
    "share, draws counted as half, market vig removed). A gap is a "
    "DISAGREEMENT, not a verified edge: the market's own accuracy on "
    "friendlies is unmeasured here, and an exchange with money on it "
    "plausibly beats a shrunk external rating.")


def market_points_share(book: dict | None) -> dict | None:
    """The market's expected points share for the HOME side, vig removed.

    Returns None rather than a number whenever the book cannot support the
    arithmetic — a missing leg, a non-3-way book, or prices that do not
    form a partition. A silent 0.5 here would look exactly like a genuine
    coin-flip market.
    """
    if not book:
        return None
    markets = book.get("markets") or []
    legs = {}
    for m in markets:
        label = (m.get("label") or m.get("yes_sub_title") or "").strip()
        raw = m.get("yes_ask") or m.get("yes_ask_dollars")
        try:
            p = float(raw)
        except (TypeError, ValueError):
            continue
        if 0.0 < p < 1.0:
            legs[label] = p
    if len(legs) != 3:
        return {"available": False, "reason": "not_a_three_way_book",
                "means": ("fewer than three priced legs, so no partition "
                          "exists to normalise")}
    total = sum(legs.values())
    if total <= 0:
        return {"available": False, "reason": "unpriced"}
    return {"available": True, "legs": legs, "vig": round(total - 1.0, 4),
            "normalised": {k: round(v / total, 4) for k, v in legs.items()},
            "note": ("asks carry the exchange's spread; the vig is removed "
                     "proportionally, which is an assumption about how it "
                     "is distributed, not a measurement")}


def compare(strength: dict | None, book: dict | None,
            home_name: str = "", away_name: str = "") -> dict:
    """Market and read on one scale, plus the stated pick."""
    out = {"available": False, "note": DISAGREEMENT_NOTE}
    ours = None
    if strength and strength.get("available"):
        cal = (strength.get("calibrated") or {}).get("expected_points_share")
        raw = strength.get("expected_points_share")
        src = cal or raw
        if src:
            ours = float(src["home"])
            out["our_points_share"] = round(ours, 4)
            out["our_basis"] = ("calibrated" if cal else "raw")

    mk = market_points_share(book)
    theirs = None
    if mk and mk.get("available"):
        norm = mk["normalised"]
        tie = next((k for k in norm if k.lower() in ("tie", "draw")), None)
        home_leg = next((k for k in norm
                         if k != tie and home_name
                         and k.lower() in home_name.lower()), None)
        if tie and home_leg:
            away_leg = next((k for k in norm if k not in (tie, home_leg)),
                            None)
            theirs = norm[home_leg] + 0.5 * norm[tie]
            out["market_points_share"] = round(theirs, 4)
            out["market_vig"] = mk["vig"]
            out["market_legs"] = norm
            # The three legs resolved to home/tie/away HERE, once. The
            # frontend previously would have had to re-derive which label
            # was which by name-matching, which is the same guess made
            # twice — and the two copies could disagree about the same
            # book.
            out["market_three_way"] = {
                "home": {"club": home_name or home_leg,
                         "label": home_leg, "p": norm[home_leg]},
                "tie": {"club": "Draw", "label": tie, "p": norm[tie]},
                "away": {"club": away_name or away_leg,
                         "label": away_leg,
                         "p": norm[away_leg] if away_leg else None},
            }
            # Our side has NO third number and must not appear to. Both
            # ratings providers publish an expected POINTS share with the
            # draw already folded in at half weight, and ClubElo's own 1X2
            # split comes from an unpublished histogram. A draw number
            # here would be invented, not derived.
            # The market's per-side POINTS SHARE — the away side too, not
            # just the home one. Without it the UI can only align our
            # two-way read against the market's three-way win legs, and a
            # reader subtracting down that column gets a number that means
            # nothing (57% win vs 61% points share is not -4).
            out["market_share_by_side"] = {
                "home": round(theirs, 4),
                "away": round(1.0 - theirs, 4),
                "means": ("expected points share per side, draw counted as "
                          "half — the SAME quantity as the read, and the "
                          "only pair on this surface that may be compared "
                          "directly"),
            }
            out["read_is_two_way"] = {
                "has_draw": False,
                "why": ("the strength read is an expected POINTS share, "
                        "draws already counted as half — not a 1X2 split. "
                        "Neither ratings provider publishes a usable draw "
                        "probability, so none is shown rather than one "
                        "invented"),
                "comparable_on": ("expected points share, which is the one "
                                  "scale both sources genuinely produce"),
            }
        else:
            out["market_unavailable_reason"] = (
                "could not map the book's legs onto home/draw/away by name, "
                "so no share is computed rather than one guessed")
    elif mk:
        out["market_unavailable_reason"] = mk.get("means") or mk.get("reason")

    if ours is None or theirs is None:
        out["pick"] = _no_pick(ours, theirs, out)
        return out

    gap = ours - theirs
    out["available"] = True
    out["disagreement"] = round(gap, 4)
    out["direction"] = ("agree" if abs(gap) <= AGREE_BAND
                        else ("read_higher_on_home" if gap > 0
                              else "read_lower_on_home"))
    out["pick"] = _pick(gap, ours, theirs, home_name, away_name, strength,
                        (out.get("market_three_way") or {}).get("tie", {})
                        .get("p"))
    return out


def _no_pick(ours, theirs, out) -> dict:
    if ours is None and theirs is None:
        why = "neither a strength read nor a priced book exists for this match"
    elif ours is None:
        why = "no strength read — at least one club is not in either ratings source"
    else:
        why = (out.get("market_unavailable_reason")
               or "no tradeable three-way book is listed for this match")
    return {"has_pick": False, "reason": why,
            "means": ("nothing is picked. An unpriced or unrated match is "
                      "not a coin flip — it is a match we cannot speak to")}


def _pick(gap: float, ours: float, theirs: float,
          home: str, away: str, strength: dict | None,
          draw_p: float | None = None) -> dict:
    """What the numbers actually support, and how weakly.

    Deliberately NOT a bet. It names a side only when the two sources
    AGREE, because that is the only case where our weak read is not the
    sole thing carrying the claim. Where they disagree it says so and picks
    nothing — a disagreement is the case where we are most likely wrong.
    """
    side_us = home if ours > 0.5 else away
    lean = max(ours, 1 - ours)
    conf = ("slight" if lean < 0.58 else
            "moderate" if lean < 0.70 else "strong")

    if abs(gap) <= AGREE_BAND:
        # THE DRAW IS NOT A DETAIL. Both numbers above are points shares,
        # in which a draw counts as HALF. A Kalshi contract on a side pays
        # NOTHING on a draw. So a side that looks strong on points share
        # can still lose outright a quarter of the time, and a pick that
        # omits this is quietly answering a different question from the
        # one a reader is asking.
        draw_note = None
        if draw_p is not None:
            draw_note = (
                f"the market prices a draw at {draw_p:.0%}. That is folded "
                f"in at HALF weight on both numbers above, because both "
                f"are points shares. A contract on {side_us} pays nothing "
                f"on a draw, so {draw_p:.0%} of outcomes beat this side "
                f"outright — the points share does not say otherwise, it "
                f"asks a different question")
        return {
            "has_pick": True, "side": side_us,
            "confidence": conf,
            "agreement": "market agrees",
            "draw_probability": draw_p,
            "draw_note": draw_note,
            "reasoning": (
                f"the strength read makes {side_us} the stronger side "
                f"({lean:.0%} expected points share) and the market prices "
                f"it within {AGREE_BAND:.0%} of the same number. Both "
                f"sources point the same way, which is the only case this "
                f"surface names a side."),
            "not_advice": (
                "this is a summary of two numbers agreeing, NOT a bet and "
                "NOT a claim of value — when the market agrees with us "
                "there is by definition no gap to exploit"),
        }

    stronger_by_market = home if theirs > 0.5 else away
    return {
        "has_pick": False,
        "reason": "sources_disagree",
        "our_side": side_us, "market_side": stronger_by_market,
        "disagreement": round(gap, 4),
        "reasoning": (
            f"the strength read favours {side_us} while the market prices "
            f"{stronger_by_market} at {max(theirs, 1 - theirs):.0%}. No side "
            f"is named. Our read beats a coin flip only narrowly (Brier "
            f"0.1892 vs 0.1973) and the market's own accuracy here is "
            f"unmeasured, so a gap is more likely our error than a "
            f"mispricing."),
        "what_would_change_this": (
            "measuring the market's pre-match accuracy on friendlies from "
            "captured pre-kickoff books. Settled books cannot answer it — "
            "their prices have already converged to the result."),
    }
