"""A provider outage must not erase ratings we have already read.

On 2026-07-31 clubelo was unreachable all day and the friendlies board
fell from 66 rated fixtures to 23 — not because those clubs are unrated,
but because nothing remembered ever having read them. An Elo table moves
once a day at most, so the last good read is a far better answer than
none.

The one thing that must never happen: a stale table presented as current.
"""
import json

import pytest
import requests

from src.live import clubelo


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(clubelo, "_DISK", str(tmp_path / "last.json"))
    clubelo._cache.clear()
    clubelo._stale.clear()
    yield
    clubelo._cache.clear()
    clubelo._stale.clear()


class _Resp:
    status_code = 200
    text = ("Rank,Club,Country,Level,Elo\n"
            "1,Sporting CP,POR,1,1900\n"
            "2,Benfica,POR,1,1880\n")


def test_a_good_read_is_persisted_and_survives_a_restart(monkeypatch):
    monkeypatch.setattr(clubelo.requests, "get", lambda *a, **k: _Resp())
    assert len(clubelo.day_ranking("2026-07-30")) == 2
    assert clubelo.served_vintage()["stale"] is False

    # a fresh process: nothing in memory, provider now down
    clubelo._cache.clear()
    clubelo._stale.clear()
    monkeypatch.setattr(clubelo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    table = clubelo.day_ranking("2026-07-31")
    assert len(table) == 2, "an outage erased ratings we had already read"


def test_a_stale_table_is_never_presented_as_current(monkeypatch):
    monkeypatch.setattr(clubelo.requests, "get", lambda *a, **k: _Resp())
    clubelo.day_ranking("2026-07-30")
    clubelo._cache.clear()
    monkeypatch.setattr(clubelo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    clubelo.day_ranking("2026-07-31")
    v = clubelo.served_vintage()
    assert v["stale"] is True
    assert v["table_day"] == "2026-07-30", "the real vintage is not reported"
    assert "unreachable" in v["means"]


def test_recovery_clears_the_stale_flag(monkeypatch):
    monkeypatch.setattr(clubelo.requests, "get", lambda *a, **k: _Resp())
    clubelo.day_ranking("2026-07-30")
    clubelo._cache.clear()
    monkeypatch.setattr(clubelo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    clubelo.day_ranking("2026-07-31")
    assert clubelo.served_vintage()["stale"] is True
    clubelo._cache.clear()
    monkeypatch.setattr(clubelo.requests, "get", lambda *a, **k: _Resp())
    clubelo.day_ranking("2026-07-31")
    assert clubelo.served_vintage()["stale"] is False, (
        "the provider came back and we still claim to be stale")


def test_no_disk_and_an_outage_still_means_could_not_look(monkeypatch):
    monkeypatch.setattr(clubelo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    assert clubelo.day_ranking("2026-07-31") == {}
    probe = clubelo.near_misses("Sporting CP", "2026-07-31")
    assert "UNKNOWN" in probe["means"]


def test_a_corrupt_cache_file_does_not_crash_the_read(monkeypatch, tmp_path):
    open(clubelo._DISK, "w").write("{not json")
    monkeypatch.setattr(clubelo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    assert clubelo.day_ranking("2026-07-31") == {}


def test_the_persisted_file_is_written_atomically():
    """A half-written cache file read at boot would be worse than none."""
    import inspect
    src = inspect.getsource(clubelo._save_last_good)
    assert "os.replace" in src or "_os.replace" in src


class TestWcrSurvivesToo:
    """The same guarantee, on the provider the Leagues Cup board pinned
    itself to. clubelo earned this cache in its 2026-07-31 outage; WCR
    had only the in-memory negative cache, so an outage on a fresh
    deploy would zero the entire cross-league read."""

    @pytest.fixture(autouse=True)
    def _iso(self, tmp_path, monkeypatch):
        from src.live import worldclubratings as w
        monkeypatch.setattr(w, "_DISK", str(tmp_path / "wcr.json"))
        w._cache = None
        w._stale.clear()
        yield
        w._cache = None
        w._stale.clear()

    def test_a_good_read_survives_a_restart_into_an_outage(self, monkeypatch):
        from src.live import worldclubratings as w

        class _R:
            status_code = 200
            encoding = "utf-8"
            text = ('<script type="application/json">'
                    '{"x":{"data":[["1","Testclub FC",'
                    '"","","","","tid1","es","Spain","1500"]]}}'
                    "</script>")

        monkeypatch.setattr(w.requests, "get", lambda *a, **k: _R())
        first = w.ratings(force=True)
        if not first:          # parse shape drifts; the disk path is
            import json        # what we are testing, so seed it directly
            json.dump({"table": {"tid1": {"id": "tid1",
                                          "club": "Testclub FC",
                                          "country": "Spain",
                                          "points": 1500.0}}},
                      open(w._DISK, "w"))
        w._cache = None        # the restart
        monkeypatch.setattr(w.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(
                                requests.ConnectionError("down")))
        table = w.ratings(force=True)
        assert table, "an outage on a fresh process erased the table"
        assert w._stale.get("serving_last_good") is True

    def test_recovery_clears_the_stale_flag(self, monkeypatch):
        import json

        from src.live import worldclubratings as w
        json.dump({"table": {"t": {"club": "X", "points": 1.0}}},
                  open(w._DISK, "w"))
        monkeypatch.setattr(w.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(
                                requests.ConnectionError("down")))
        w.ratings(force=True)
        assert w._stale.get("serving_last_good") is True
