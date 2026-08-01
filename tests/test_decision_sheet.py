"""The sheet briefs; it never recommends, and it never renders a blank.

Two rules, both load-bearing:
  1. no field names a side to back — this is not a tipping surface;
  2. every absent section says WHY, because a blank and a measured
     absence rendering alike cost an hour on 2026-07-31.
"""
import pytest

from src.live import decision_sheet as ds

BANNED = ["recommend", "you should", "best bet", "edge", "value bet",
          "lock of the day", "advice", "guaranteed", "sure thing"]


def _sheet(monkeypatch, *, match=None, books=None, meta=None, model=None,
           news=None):
    from src import mls
    monkeypatch.setattr(mls, "match_summary", lambda e: match)
    monkeypatch.setattr(mls, "find_all_books_with_freshness",
                        lambda *a: (books or [], meta or {}))
    from src.live import runs
    monkeypatch.setattr(runs, "model_for_event", lambda e, **k: model)
    from src.live import team_news
    monkeypatch.setattr(team_news, "fixture_news", lambda r: news)
    return ds.build("401877024", "mls")


MATCH = {"id": "1", "date": "2026-08-01T23:30Z",
         "home": {"name": "Cincinnati"}, "away": {"name": "San Jose"}}


def test_every_absent_section_says_why(monkeypatch):
    s = _sheet(monkeypatch, match=MATCH)
    for key in ("market", "model", "team_news"):
        assert s[key]["available"] is False, key
        assert s[key].get("reason"), f"{key} absent with no reason"
        assert len(s[key].get("means", "")) > 20, f"{key} reason not in words"


def test_a_missing_book_is_not_reported_as_no_book(monkeypatch):
    s = _sheet(monkeypatch, match=MATCH, meta={"status": "unavailable"})
    assert "NOT that the exchange lists none" in s["market"]["means"]


def test_a_missing_model_run_is_not_a_coin_flip(monkeypatch):
    s = _sheet(monkeypatch, match=MATCH)
    assert "NOT a 50/50" in s["model"]["means"]
    assert "0.5" not in str(s["model"].get("outcomes", ""))


def test_no_side_is_ever_named(monkeypatch):
    book = [{"key": "winner", "markets": [
        {"label": "Cincinnati", "yes_ask": 0.55},
        {"label": "Tie", "yes_ask": 0.25},
        {"label": "San Jose", "yes_ask": 0.25}]}]
    s = _sheet(monkeypatch, match=MATCH, books=book,
               meta={"status": "live"},
               model={"outcomes": {"home_win": 0.6, "draw": 0.2,
                                   "away_win": 0.2}})
    assert s["market_vs_model"]["available"] is True
    # the comparison exists, but nothing picks a winner
    for banned in ("pick", "recommended_side", "best_side", "bet"):
        assert banned not in str(s.keys())
    assert "DISAGREEMENT" in s["market_vs_model"]["means"]


def test_the_vig_is_removed_and_reported(monkeypatch):
    book = [{"key": "winner", "markets": [
        {"label": "Cincinnati", "yes_ask": 0.55},
        {"label": "Tie", "yes_ask": 0.25},
        {"label": "San Jose", "yes_ask": 0.25}]}]
    s = _sheet(monkeypatch, match=MATCH, books=book,
               meta={"status": "live"}, model={"outcomes": {}})
    c = s["market_vs_model"]
    assert abs(sum(c["market_legs_devigged"].values()) - 1.0) < 1e-3
    assert c["market_vig"] > 0


def test_a_two_way_book_refuses_rather_than_normalising(monkeypatch):
    book = [{"key": "winner", "markets": [
        {"label": "Cincinnati", "yes_ask": 0.6},
        {"label": "San Jose", "yes_ask": 0.4}]}]
    s = _sheet(monkeypatch, match=MATCH, books=book,
               meta={"status": "live"}, model={"outcomes": {}})
    assert s["market_vs_model"]["available"] is False
    assert "three" in s["market_vs_model"]["means"].lower()


@pytest.mark.parametrize("word", BANNED)
def test_no_recommendation_vocabulary(word, monkeypatch):
    s = _sheet(monkeypatch, match=MATCH)
    blob = str(s).lower() + (ds.__doc__ or "").lower()
    if word in blob:
        # the module may NAME a banned word to say it refuses it
        assert "not" in blob or "never" in blob, f"{word!r} used as a claim"


def test_one_dead_provider_does_not_kill_the_sheet(monkeypatch):
    from src import mls
    monkeypatch.setattr(mls, "match_summary", lambda e: MATCH)

    def boom(*a):
        raise RuntimeError("kalshi down")

    monkeypatch.setattr(mls, "find_all_books_with_freshness", boom)
    from src.live import runs
    monkeypatch.setattr(runs, "model_for_event",
                        lambda e, **k: {"outcomes": {"home_win": 0.5}})
    s = ds.build("401877024", "mls")
    assert s["market"]["available"] is False
    assert s["model"]["available"] is True, "a dead book killed the model"
