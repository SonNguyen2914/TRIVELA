"""An outage at a ratings provider must not take this service down.

Both clients returned {} on a failed fetch WITHOUT caching it, so every
club lookup re-tried a fresh request and paid the full timeout again. The
friendlies board asks for roughly 650 clubs, so a 20s provider timeout
became hours of blocking: /api/friendlies/fixtures stopped answering
entirely while /api/health stayed green.

The fix must not change what an empty table MEANS. `{}` is still "could
not look", never "no coverage" — the negative cache changes only how
often we ask.
"""
import time

import pytest
import requests

from src.live import clubelo, worldclubratings


@pytest.fixture(autouse=True)
def _clear():
    clubelo._cache.clear()
    worldclubratings._cache = None
    yield
    clubelo._cache.clear()
    worldclubratings._cache = None


def test_clubelo_asks_once_per_outage_window(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("provider down")

    monkeypatch.setattr(clubelo.requests, "get", boom)
    for _ in range(50):
        assert clubelo.day_ranking("2026-07-31") == {}
    assert len(calls) == 1, (
        f"{len(calls)} provider requests for 50 lookups — an outage still "
        f"stampedes")


def test_wcr_asks_once_per_outage_window(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("provider down")

    monkeypatch.setattr(worldclubratings.requests, "get", boom)
    for _ in range(50):
        assert worldclubratings.ratings() == {}
    assert len(calls) == 1, f"{len(calls)} requests for 50 lookups"


def test_the_failure_cache_expires_so_recovery_is_seen(monkeypatch):
    """A provider that comes back must be picked up, not cached as dead."""
    monkeypatch.setattr(clubelo, "FAILURE_TTL_SECONDS", 0.05)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(clubelo.requests, "get", boom)
    clubelo.day_ranking("2026-07-31")
    clubelo.day_ranking("2026-07-31")
    assert len(calls) == 1
    time.sleep(0.08)
    clubelo.day_ranking("2026-07-31")
    assert len(calls) == 2, "the failure cache never expires — recovery unseen"


def test_an_empty_table_still_means_could_not_look(monkeypatch):
    """The negative cache must not convert 'unknown' into 'absent'."""
    monkeypatch.setattr(clubelo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    probe = clubelo.near_misses("Sporting CP", "2026-07-31")
    assert probe["candidates"] == []
    assert "UNKNOWN" in probe["means"]
    assert "absent" in probe["means"]
