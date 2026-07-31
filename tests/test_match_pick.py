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
