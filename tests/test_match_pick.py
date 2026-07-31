"""The pick must refuse more often than it speaks, and must never use the
vocabulary of a recommendation."""
import pytest

from src.live import match_pick as mp

BANNED = ["edge", "value", "opportunity", "fair value", "should bet",
          "advice", "guaranteed", "lock", "free money", "mispriced"]


def _book(home, draw, away, home_name="Alpha FC"):
    return {"markets": [{"label": home_name, "yes_ask": home},
                        {"label": "Tie", "yes_ask": draw},
                        {"label": "Beta FC", "yes_ask": away}]}


def _read(share):
    return {"available": True,
            "calibrated": {"expected_points_share": {"home": share,
                                                     "away": 1 - share}}}


def test_no_book_yields_no_pick_not_a_coin_flip():
    out = mp.compare(_read(0.7), None, "Alpha FC", "Beta FC")
    assert out["available"] is False
    assert out["pick"]["has_pick"] is False
    assert "0.5" not in str(out.get("market_points_share", ""))
    assert "market_points_share" not in out


def test_no_read_yields_no_pick():
    out = mp.compare(None, _book(0.5, 0.25, 0.25), "Alpha FC", "Beta FC")
    assert out["pick"]["has_pick"] is False
    assert "strength read" in out["pick"]["reason"]


def test_two_way_book_is_refused_not_normalised():
    b = {"markets": [{"label": "Alpha FC", "yes_ask": 0.6},
                     {"label": "Beta FC", "yes_ask": 0.4}]}
    out = mp.compare(_read(0.7), b, "Alpha FC", "Beta FC")
    assert out["available"] is False
    assert "three" in out["market_unavailable_reason"].lower()


def test_disagreement_names_no_side():
    # read says home 0.75, market says home ~0.45 — a wide gap
    out = mp.compare(_read(0.75), _book(0.33, 0.25, 0.42), "Alpha FC",
                     "Beta FC")
    assert out["available"] is True
    assert out["pick"]["has_pick"] is False
    assert out["pick"]["reason"] == "sources_disagree"
    assert "what_would_change_this" in out["pick"]


def test_agreement_names_a_side_and_disclaims_value():
    out = mp.compare(_read(0.62), _book(0.50, 0.25, 0.25), "Alpha FC",
                     "Beta FC")
    assert out["direction"] == "agree"
    assert out["pick"]["has_pick"] is True
    assert out["pick"]["side"] == "Alpha FC"
    # the ONLY case a side is named is also the case with no gap to exploit
    assert "no gap" in out["pick"]["not_advice"]


def test_vig_is_removed_and_reported():
    out = mp.compare(_read(0.5), _book(0.50, 0.30, 0.30), "Alpha FC",
                     "Beta FC")
    assert out["market_vig"] > 0
    # legs are rounded to 4dp for display, so 1e-3 not 1e-6
    assert abs(sum(out["market_legs"].values()) - 1.0) < 1e-3


@pytest.mark.parametrize("word", BANNED)
def test_no_recommendation_vocabulary_anywhere(word):
    """Guard the prose, including the module docstring — the earlier
    friendlies surface twice shipped banned wording that only a test
    caught."""
    blobs = [mp.DISAGREEMENT_NOTE, mp.AGREE_BAND_BASIS, mp.__doc__ or ""]
    for share, book in ((0.75, _book(0.33, 0.25, 0.42)),
                        (0.62, _book(0.50, 0.25, 0.25)),
                        (0.62, None)):
        blobs.append(str(mp.compare(_read(share), book, "Alpha FC",
                                    "Beta FC")))
    for b in blobs:
        low = b.lower()
        # the module may NAME the banned word to say it refuses it
        if word in low and "never" not in low and "not a verified" not in low:
            raise AssertionError(f"{word!r} used as a claim in: {b[:200]}")


def test_dormant_competition_declines_with_409_not_500(monkeypatch):
    """A dormant league has nothing to approve. That must read as a
    refusal, not a server fault and not a silent 200 with a null id.

    `_admin_ok` is patched deliberately: without it this test passes on a
    403 and never reaches the branch it claims to cover — which is exactly
    what it did on the first attempt.
    """
    import config
    config.PUBLIC_READ_ONLY = False
    config.RATE_LIMIT_SECONDS = 0
    from fastapi.testclient import TestClient

    import api.main as main
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    c = TestClient(main.app)
    r = c.post("/api/admin/la-liga-2026/replay-approval/activate")
    assert r.status_code == 409, (r.status_code, r.text[:200])
    d = r.json()["detail"]
    assert d["activated"] is False
    assert d["declined"] == "dormant"
    assert "nothing to approve" in d["means"]


def test_three_way_detail_resolves_each_leg_to_a_side():
    out = mp.compare(_read(0.62), _book(0.50, 0.25, 0.25), "Alpha FC",
                     "Beta FC")
    tw = out["market_three_way"]
    assert tw["home"]["club"] == "Alpha FC"
    assert tw["away"]["club"] == "Beta FC"
    assert tw["tie"]["club"] == "Draw"
    # the three legs are a partition of 1 after the vig comes out
    assert abs(sum(x["p"] for x in tw.values()) - 1.0) < 1e-3
    # and the home share is reconstructible from them
    assert abs((tw["home"]["p"] + 0.5 * tw["tie"]["p"])
               - out["market_points_share"]) < 1e-3


def test_the_read_never_gains_a_draw_number():
    """Our side has two numbers and must not appear to have three."""
    out = mp.compare(_read(0.62), _book(0.50, 0.25, 0.25), "Alpha FC",
                     "Beta FC")
    assert out["read_is_two_way"]["has_draw"] is False
    assert "invented" in out["read_is_two_way"]["why"]
    # nothing anywhere in our half of the payload looks like a draw
    ours = {k: v for k, v in out.items() if k.startswith("our_")}
    assert not any("draw" in str(v).lower() or "tie" in str(v).lower()
                   for v in ours.values()), ours


def test_a_named_side_always_states_what_the_draw_does_to_it():
    """A points share counts a draw as half; a Kalshi contract pays zero
    on one. A pick that names a side without saying so is answering a
    different question from the one being asked."""
    out = mp.compare(_read(0.62), _book(0.50, 0.25, 0.25), "Alpha FC",
                     "Beta FC")
    p = out["pick"]
    assert p["has_pick"] is True
    assert p["draw_probability"] is not None
    assert "pays nothing on a draw" in p["draw_note"]
    # the stated draw must be the DE-VIGGED one, not the raw ask
    assert abs(p["draw_probability"] - 0.25) < 0.02


def test_market_share_is_given_per_side_so_columns_can_align():
    """The UI aligns market and read by team. It needs the market's share
    for BOTH sides, or it can only line our two-way read up against the
    market's win legs — where subtracting down a column is meaningless."""
    out = mp.compare(_read(0.62), _book(0.50, 0.25, 0.25), "Alpha FC",
                     "Beta FC")
    ms = out["market_share_by_side"]
    assert abs(ms["home"] + ms["away"] - 1.0) < 1e-3
    assert abs(ms["home"] - out["market_points_share"]) < 1e-6
    # and it is NOT the same as the win leg — the whole reason it exists
    assert abs(ms["home"] - out["market_three_way"]["home"]["p"]) > 0.01
