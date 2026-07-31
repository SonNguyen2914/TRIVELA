"""`/api/mls/match/{id}` must say WHY it has no book.

On 2026-07-31 two of fifteen slate fixtures served no book in production
while returning 11 families locally on identical inputs. The payload
could not distinguish three different causes — no Kalshi event bridged
the fixture, the bundle failed CLOSED past MLS_PRICE_MAX_AGE_SECONDS, or
the lookup raised — because all three produced `books: []`.

That is the "unavailable rendering as a deliberate state" pattern this
codebase refuses everywhere else, and it is what made the fault take an
hour of bisection instead of one request.
"""
import config
import pytest
from fastapi.testclient import TestClient

import api.main as main
from src import mls


@pytest.fixture(autouse=True)
def _open():
    config.PUBLIC_READ_ONLY = False
    config.RATE_LIMIT_SECONDS = 0
    yield


def _summary(*a, **k):
    return {"id": "1", "date": "2026-08-01T23:30Z", "state": "pre",
            "detail": "", "minute": None, "venue": "X",
            "home": {"name": "CF Montréal"},
            "away": {"name": "New England Revolution"},
            "stats": [], "events": [], "scouting": {}}


def test_a_stale_failed_closed_bundle_is_not_reported_as_no_book(monkeypatch):
    monkeypatch.setattr(mls, "match_summary", _summary)
    monkeypatch.setattr(mls, "find_all_books_with_freshness",
                        lambda *a: ([], {"observed_at": None,
                                         "age_seconds": None,
                                         "status": "unavailable"}))
    d = TestClient(main.app).get("/api/mls/match/761697").json()
    assert d["books"] == []
    assert d["book_meta"]["status"] == "unavailable"
    assert "NOT that the exchange lists none" in d["book_meta"]["means"]


def test_a_raising_lookup_says_it_raised(monkeypatch):
    monkeypatch.setattr(mls, "match_summary", _summary)

    def boom(*a):
        raise RuntimeError("kalshi 429")

    monkeypatch.setattr(mls, "find_all_books_with_freshness", boom)
    d = TestClient(main.app).get("/api/mls/match/761697").json()
    assert d["books"] == []
    assert "RAISED" in d["book_meta"]["means"]
    assert "RuntimeError" in d["book_meta"]["error"]


def test_a_live_book_still_reports_live(monkeypatch):
    monkeypatch.setattr(mls, "match_summary", _summary)
    fam = [{"key": "winner", "label": "Winner · 3-way",
            "event_ticker": "KXMLSGAME-26AUG01MTLNE", "markets": []}]
    monkeypatch.setattr(mls, "find_all_books_with_freshness",
                        lambda *a: (fam, {"status": "live",
                                          "age_seconds": 3}))
    d = TestClient(main.app).get("/api/mls/match/761697").json()
    assert d["book_meta"]["status"] == "live"
    assert d["book"]["key"] == "winner"
    # the legacy top-level `book` must survive — the frontend reads it
    assert d["books"] == fam


def test_the_three_causes_are_distinguishable(monkeypatch):
    """The whole point: three different empties must not look alike."""
    monkeypatch.setattr(mls, "match_summary", _summary)
    seen = set()
    for meta in ({"status": "unavailable"}, {"status": "stale_fallback"}):
        monkeypatch.setattr(mls, "find_all_books_with_freshness",
                            lambda *a, m=meta: ([], m))
        seen.add(TestClient(main.app).get(
            "/api/mls/match/761697").json()["book_meta"]["status"])

    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(mls, "find_all_books_with_freshness", boom)
    d = TestClient(main.app).get("/api/mls/match/761697").json()
    seen.add("raised" if d["book_meta"].get("error") else "?")
    assert len(seen) == 3, seen
