"""What a match MEANS is derived or absent — never asserted.

An aggregate is the sum of two real scorelines. A group record is counted
from finished fixtures. Leagues Cup's shootout bonus point cannot be
attributed from a scoreline, so a drawn record widens to a points RANGE
rather than fabricating a rank.
"""
from src.competitions import _meaning


def _fx(hid, aid, hg=None, ag=None, status="NS", rnd="Group Stage"):
    return {"round": rnd, "status": status,
            "home": {"name": f"H{hid}", "apif_team_id": hid},
            "away": {"name": f"A{aid}", "apif_team_id": aid},
            "goals": {"home": hg, "away": ag}}


def test_second_leg_carries_the_real_aggregate():
    leg1 = _fx(2, 1, 3, 1, "FT", "2nd Qualifying Round")
    leg2 = _fx(1, 2, rnd="2nd Qualifying Round")
    m = _meaning(leg2, [leg1, leg2], "ucl")
    assert m["tie"]["leg"] == 2
    assert m["tie"]["first_leg"] == "3-1"
    assert "trail" in m["tie"]["means"]     # H1 lost the away leg 3-1


def test_first_leg_claims_no_aggregate():
    leg1 = _fx(1, 2, rnd="Qualifying")
    m = _meaning(leg1, [leg1], "uel")
    assert m["tie"]["leg"] == 1
    assert "first_leg" not in m["tie"]


def test_drawn_cup_match_widens_to_a_range_not_a_rank():
    played = _fx(1, 2, 2, 2, "PEN")        # drawn, shootout unknown
    upcoming = _fx(1, 3)
    m = _meaning(upcoming, [played, upcoming], "leagues-cup")
    h = m["stakes"]["home"]
    assert h["points"] is None
    assert h["points_range"] == [1, 2]      # shootout lost vs won
    assert "cannot" in m["stakes"]["format"]


def test_clean_wins_give_exact_points():
    played = _fx(1, 2, 3, 0, "FT")
    upcoming = _fx(1, 3)
    m = _meaning(upcoming, [played, upcoming], "leagues-cup")
    assert m["stakes"]["home"]["points"] == 3
    assert m["stakes"]["home"]["points_range"] is None


def test_a_meaning_failure_degrades_to_a_named_absence():
    # _meaning wrapped at the call site; here: garbage in, no raise
    m = _meaning({"round": None, "home": {}, "away": {}}, [], "ecl")
    assert isinstance(m, dict)
