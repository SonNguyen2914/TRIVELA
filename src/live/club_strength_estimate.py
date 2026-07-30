"""Cross-league club strength for friendlies — a DELIBERATELY LOWER tier.

This module exists because friendlies pit clubs from different leagues
against each other, and this platform's own ratings structurally cannot
compare those: `xg_ratings.ScopedStrength` raises
`CrossCompetitionArithmetic` on any arithmetic between two ratings fitted
in different competitions, because each is relative to its own league's
mean. That refusal is correct and stays.

So this is a SIBLING system, not an extension:
  - it never imports ScopedStrength / XgScope / pair_for_pricing;
  - it never touches ModelApprovalDecision or the evaluation ladder —
    that machinery means "internally backtested against a baseline with a
    bootstrapped CI", and this is none of those things;
  - it carries its own class, EXTERNAL_UNEVALUATED, explicitly BELOW the
    MEASURED / REPLAYED / PILOT hierarchy that approvals use.

TWO PROVIDERS, NEVER MIXED, and COMPLEMENTARY rather than redundant.
ClubElo is narrow-but-DEEP: 589 clubs, European only, but including the
second tiers of the big five (England 43, Spain 42, Italy 40; 99
second-tier clubs overall). worldclubratings is broad-but-SHALLOW: 2,413
clubs across 166 countries, top flight only, ~20 per country.

Neither can be dropped. Pre-season friendlies are full of second-tier
clubs, so on the 2026-07-30 slate ClubElo alone rated 63 pairs against
worldclubratings' 37 — despite the latter being four times the size. But
worldclubratings reaches Qatar, Japan, Brazil and Mexico that ClubElo
cannot. Measured: 46 fixtures only ClubElo could rate, 20 only
worldclubratings, 17 both. Dropping either loses fixtures outright.

ClubElo is tried FIRST, which matters only for the 17 both cover: it
refreshes daily rather than weekly, it is classic Elo over results back to
1939, and its authors make a plainer claim for it than worldclubratings'
do. Any fixture both cover is European — ClubElo's specialism.

Both publish the same SHAPE of expectation and both count a draw as half a
win, so both give an expected POINTS SHARE, not a win probability; the
field is named `expected_points_share` so it cannot be misread. Neither
publishes a usable 1X2 split (ClubElo's comes from an unpublished
histogram), so no draw number is derived for any pair.

But their DIVISORS DIFFER: worldclubratings publishes 600 (its method is
an adaptation of FIFA's post-2018 ranking procedure), ClubElo publishes
400 (classic Elo). No conversion between them is published, so the source
is chosen at the PAIR level — one provider must rate BOTH clubs — and a
pairing that would need one rating from each is simply not produced.

That was learned the hard way: an earlier version chose per SIDE and
measurably regressed coverage on the 2026-07-30 slate, from 63 rated
pairs to 50, by stranding 34 fixtures whose two clubs resolved in
different providers. Pair-level selection took it to 83.
"""
from __future__ import annotations

from datetime import datetime, timezone

ESTIMATE_CLASS = "EXTERNAL_UNEVALUATED"
ESTIMATE_MEANING = (
    "An EXTERNAL strength read for orientation only. It is not a forecast, "
    "not a model output, and establishes nothing about value. It has never "
    "been backtested on this platform, holds no approval decision, and sits "
    "BELOW the MEASURED / REPLAYED / PILOT evidence classes that every "
    "approved model here is judged against. Nothing here is a "
    "recommendation.")

# Every way this can fail to produce a number, named. Three truths kept
# apart, the convention this repo uses everywhere: we measured absence /
# we never looked / we tried and failed.
NO_ELO_COVERAGE = "no_rating_coverage"
NAME_UNMAPPED = "name_unmapped"
NAME_AMBIGUOUS = "name_ambiguous"
REQUEST_FAILED = "provider_request_failed"
SCALE_MISMATCH = "sources_not_on_one_scale"

REASON_WORDS = {
    NO_ELO_COVERAGE: "neither ratings source carries this club",
    NAME_UNMAPPED: ("the sources were read but no confident name match "
                    "exists for this club — refused rather than guessed"),
    NAME_AMBIGUOUS: ("this name matches more than one club and nothing "
                     "disambiguates them, so no rating is attached"),
    REQUEST_FAILED: ("the ratings request failed, so coverage is UNKNOWN "
                     "for this club, not absent"),
    SCALE_MISMATCH: (
        "the two clubs are rated by DIFFERENT providers whose scales are "
        "not interchangeable: worldclubratings publishes an expectation "
        "divisor of 600 (a FIFA-adapted scheme) and ClubElo publishes 400 "
        "(classic Elo). Combining them would produce a number with no "
        "referent, so no expectation is published for this pairing"),
}

# Which provider supplied a side. Never mixed inside one expectation.
SRC_WCR = "worldclubratings"
SRC_CLUBELO = "clubelo"

# Pairing confidence, strongest first. Both tiers require ONE provider for
# both sides — there is no "mixed" tier that computes anything, because
# the two providers' divisors differ and a crude alignment between them
# would be inventing a conversion neither publishes.
BOTH_WCR = "both_worldclubratings"
BOTH_ELO = "both_clubelo"

PAIR_WORDS = {
    BOTH_WCR: ("both clubs read from worldclubratings, on its own "
               "FIFA-adapted scale (divisor 600). Its authors describe the "
               "ranking as an experiment on the method rather than an "
               "authoritative world ranking, so treat it as orientation"),
    BOTH_ELO: ("both clubs read from ClubElo, on its own Elo scale "
               "(divisor 400) — European club football only"),
}


def _side_from(source: str, name: str, day: str,
               country_hint: str | None = None) -> dict:
    """One club's rating FROM ONE NAMED SOURCE, or a named refusal.

    Deliberately single-source. An earlier version tried one provider then
    fell back to the other PER SIDE, which measurably made things worse:
    on the 2026-07-30 slate it left 34 fixtures refused as
    `sources_not_on_one_scale`, because one club resolved in one provider
    and its opponent only in the other. Coverage fell from 63 rated pairs
    to 50. Source choice belongs to the PAIR (see `for_fixture`), not to
    the side.
    """
    if not name:
        return {"club": name, "rated": False, "reason": NAME_UNMAPPED,
                "reason_words": REASON_WORDS[NAME_UNMAPPED]}
    if source == SRC_WCR:
        from src.live import worldclubratings as wcr
        if not wcr.ratings():
            return {"club": name, "rated": False, "reason": REQUEST_FAILED,
                    "reason_words": REASON_WORDS[REQUEST_FAILED]}
        row = wcr.lookup(name, country_hint)
        if row and row.get("ambiguous"):
            return {"club": name, "rated": False, "reason": NAME_AMBIGUOUS,
                    "reason_words": REASON_WORDS[NAME_AMBIGUOUS],
                    "candidates": row["candidates"],
                    "candidate_countries": row["countries"]}
        if row:
            return {"club": name, "rated": True, "source": SRC_WCR,
                    "rating": round(row["points"], 1),
                    "provider_id": row["id"],
                    "provider_rank": row.get("rank"),
                    "country": row.get("country"),
                    "match_tier": row.get("match_tier"),
                    "scale": "fifa_adapted_600"}
        return {"club": name, "rated": False, "reason": NAME_UNMAPPED,
                "reason_words": REASON_WORDS[NAME_UNMAPPED]}

    from src.live import clubelo
    if not clubelo.day_ranking(day):
        return {"club": name, "rated": False, "reason": REQUEST_FAILED,
                "reason_words": REASON_WORDS[REQUEST_FAILED]}
    row = clubelo.lookup(name, day)
    if row:
        return {"club": name, "rated": True, "source": SRC_CLUBELO,
                "rating": round(row["elo"], 1),
                "country": row.get("country"),
                "league_level": row.get("level"),
                "match_tier": row.get("match_tier"),
                "scale": "elo_400"}
    return {"club": name, "rated": False, "reason": NAME_UNMAPPED,
            "reason_words": REASON_WORDS[NAME_UNMAPPED]}


def for_fixture(f: dict, day: str | None = None) -> dict:
    """The strength read for one friendly fixture, or named refusals.

    Never returns a bare blank, never a zero, and never a default 50% —
    an unrated pairing says which side could not be read and why.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    home_name = ((f.get("home") or {}).get("name")) or ""
    away_name = ((f.get("away") or {}).get("name")) or ""
    # the fixture's league country, when the provider gave one, is what
    # separates Al-Sadd SC (Qatar) from Al Sadd (Yemen). Often "World" on a
    # friendly, which disambiguates nothing — and then the pairing is
    # refused as ambiguous rather than guessed.
    hint = f.get("league_country")

    # PAIR-LEVEL source choice: find a provider that rates BOTH clubs and
    # use only that one. Never one side from each — the divisors differ
    # (400 vs 600) and no conversion between them is published.
    #
    # ORDER: ClubElo first, worldclubratings second. An earlier version had
    # this reversed and justified it on coverage, which was simply wrong
    # reasoning: under pair-level selection the UNION IS ORDER-INDEPENDENT
    # (83 rated either way on the 2026-07-30 slate). Order decides only
    # which provider wins the 17 fixtures BOTH cover, and for those ClubElo
    # is the better read:
    #   - it refreshes DAILY; worldclubratings refreshes weekly;
    #   - it is classic Elo with a fully published method over results back
    #     to 1939, and it makes a straightforward claim for itself, whereas
    #     worldclubratings' own authors describe their ranking as "an
    #     experiment on the effectiveness of the underlying method";
    #   - any fixture both cover is a European one, which is precisely
    #     ClubElo's specialism.
    #
    # The two are complementary rather than redundant, and neither can be
    # dropped: ClubElo is narrow-but-DEEP (England 43 clubs incl. 2nd tier,
    # Spain 42, Italy 40 — 99 second-tier clubs in total), worldclubratings
    # is broad-but-SHALLOW (top flight only, ~20 per country, but 166
    # countries). Pre-season friendlies are full of second-tier clubs, so
    # ClubElo alone rates 63 pairs here against worldclubratings' 37; but
    # worldclubratings reaches Qatar, Japan, Brazil and Mexico that ClubElo
    # cannot. Dropping either loses fixtures outright.
    attempts = []
    h = a = None
    for src in (SRC_CLUBELO, SRC_WCR):
        hh = _side_from(src, home_name, day, hint)
        aa = _side_from(src, away_name, day, hint)
        attempts.append((src, hh, aa))
        if hh.get("rated") and aa.get("rated"):
            h, a = hh, aa
            break
    if h is None:
        # No single provider covers both. Report the attempt that got
        # FURTHEST — one side rated beats neither — so the payload names a
        # real reason per club instead of the last provider's answer.
        attempts.sort(key=lambda t: -(int(t[1].get("rated", False))
                                      + int(t[2].get("rated", False))))
        _src, h, a = attempts[0]
    out = {
        "estimate_class": ESTIMATE_CLASS,
        "estimate_meaning": ESTIMATE_MEANING,
        "as_of": day,
        "home": h, "away": a,
        "available": False,
        "expected_points_share": None,
        "semantics": (
            "expected_points_share is ClubElo's published Elo expectation "
            "E = 1/(10**(-dr/400)+1), in which DRAWS COUNT AS HALF A WIN. "
            "It is a points share, NOT a win probability, and this surface "
            "publishes no draw number because ClubElo's own 1X2 split "
            "comes from a histogram it does not publish."),
        "home_field_advantage": (
            "not applied — ClubElo publishes the HFA update rule but never "
            "its current value, and a pre-season friendly is often at a "
            "neutral or touring venue anyway"),
        "attribution": ("Club Elo ratings from clubelo.com, computed from "
                        "match results since 1939; methodology published "
                        "at clubelo.com/System."),
    }
    if h.get("rated") and a.get("rated"):
        if h["source"] != a["source"]:
            # DEFENSIVE, and unreachable by construction: the pair-level
            # loop above only accepts a source that rates BOTH sides, so
            # the two can never disagree here. Kept because it states the
            # invariant in executable form — if per-side selection is ever
            # reintroduced (it was, and it measurably regressed coverage),
            # this refuses instead of silently combining a divisor-600
            # rating with a divisor-400 one, which no published conversion
            # relates and which would look entirely plausible.
            out["unavailable_reason"] = REASON_WORDS[SCALE_MISMATCH]
            out["refused"] = SCALE_MISMATCH
            out["sources"] = {"home": h["source"], "away": a["source"]}
            return out
        from src.live import clubelo, worldclubratings as wcr
        if h["source"] == SRC_WCR:
            e = wcr.expected_points_share(h["rating"], a["rating"])
            tier, divisor = BOTH_WCR, wcr.FIFA_ADAPTED_DIVISOR
            out["source_note"] = wcr.METHOD_NOTE
        else:
            e = clubelo.expected_points_share(h["rating"], a["rating"])
            tier, divisor = BOTH_ELO, clubelo.ELO_DIVISOR
        out["available"] = True
        out["source"] = h["source"]
        out["expectation_divisor"] = divisor
        out["pair_confidence"] = tier
        out["pair_confidence_words"] = PAIR_WORDS[tier]
        out["expected_points_share"] = {
            "home": round(e, 4), "away": round(1.0 - e, 4)}
        out["rating_difference"] = round(h["rating"] - a["rating"], 1)
        # kept under the old name too: the frontend shipped against it
        out["elo_difference"] = out["rating_difference"]
    else:
        out["unavailable_reason"] = (
            "at least one club could not be read; see home/away for which")
    return out
