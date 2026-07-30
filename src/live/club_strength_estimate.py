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

WHAT IT PUBLISHES, and what it refuses to. ClubElo's published
expectation E = 1/(10**(-dr/400)+1) counts draws as HALF a win, so it is
an expected POINTS SHARE, not a win probability. Its own 1X2 split comes
from a result histogram that is not published, so no draw number is
derived here for any pair. Calling E a "win %" would misstate the source,
so the field is named `expected_points_share` and every payload says what
that means.

Non-European clubs are not in ClubElo at all (measured: Al Sadd, Al
Nassr, FC Tokyo, Estrela). They fall back to a league-standing signal
that is explicitly coarser, and a pairing that mixes the two says so.
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
NO_ELO_COVERAGE = "clubelo_no_coverage"
NAME_UNMAPPED = "clubelo_name_unmapped"
REQUEST_FAILED = "clubelo_request_failed"
STANDING_UNAVAILABLE = "standing_fallback_unavailable"
STANDING_THIN = "standing_fallback_insufficient_matches"

REASON_WORDS = {
    NO_ELO_COVERAGE: ("this club is outside ClubElo's coverage, which is "
                      "European club football only"),
    NAME_UNMAPPED: ("ClubElo was read but no confident name match exists "
                    "for this club — refused rather than guessed"),
    REQUEST_FAILED: ("the ClubElo request failed, so coverage is UNKNOWN "
                     "for this club, not absent"),
    STANDING_UNAVAILABLE: "no league-standing signal is available either",
    STANDING_THIN: ("too few matches played for a points-per-game signal "
                    "to mean anything"),
}

# Pairing confidence. Both-Elo is the only tier where the two numbers came
# from one consistently-maintained scale.
BOTH_ELO = "both_clubelo"
MIXED = "mixed_clubelo_standing"
BOTH_STANDING = "both_standing"

PAIR_WORDS = {
    BOTH_ELO: ("both clubs read from the same ClubElo scale — the only "
               "pairing where the difference is meaningful as published"),
    MIXED: ("one club from ClubElo, one from a league-standing fallback. "
            "These are DIFFERENT scales crudely aligned; treat the "
            "difference as a direction, not a magnitude"),
    BOTH_STANDING: ("neither club is in ClubElo. Both numbers are coarse "
                    "league-standing signals from different leagues — the "
                    "weakest pairing this surface produces"),
}


def _side(name: str, day: str) -> dict:
    from src.live import clubelo
    if not name:
        return {"club": name, "rated": False, "reason": NAME_UNMAPPED,
                "reason_words": REASON_WORDS[NAME_UNMAPPED]}
    table = clubelo.day_ranking(day)
    if not table:
        return {"club": name, "rated": False, "reason": REQUEST_FAILED,
                "reason_words": REASON_WORDS[REQUEST_FAILED]}
    row = clubelo.lookup(name, day)
    if row is None:
        # Read succeeded and this club is not in it. Reported as
        # NAME_UNMAPPED rather than NO_ELO_COVERAGE because from here the
        # two are indistinguishable: a European club we failed to match
        # and a non-European club that is genuinely absent look identical.
        # Claiming "outside Europe" would be an inference, not a reading.
        return {"club": name, "rated": False, "reason": NAME_UNMAPPED,
                "reason_words": REASON_WORDS[NAME_UNMAPPED]}
    return {"club": name, "rated": True, "source": "clubelo",
            "elo": round(row["elo"], 1), "country": row.get("country"),
            "league_level": row.get("level"),
            "match_tier": row.get("match_tier")}


def for_fixture(f: dict, day: str | None = None) -> dict:
    """The strength read for one friendly fixture, or named refusals.

    Never returns a bare blank, never a zero, and never a default 50% —
    an unrated pairing says which side could not be read and why.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    home_name = ((f.get("home") or {}).get("name")) or ""
    away_name = ((f.get("away") or {}).get("name")) or ""
    h, a = _side(home_name, day), _side(away_name, day)
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
        from src.live import clubelo
        e = clubelo.expected_points_share(h["elo"], a["elo"])
        out["available"] = True
        out["pair_confidence"] = BOTH_ELO
        out["pair_confidence_words"] = PAIR_WORDS[BOTH_ELO]
        out["expected_points_share"] = {
            "home": round(e, 4), "away": round(1.0 - e, 4)}
        out["elo_difference"] = round(h["elo"] - a["elo"], 1)
    else:
        out["unavailable_reason"] = (
            "at least one club could not be read; see home/away for which")
    return out
