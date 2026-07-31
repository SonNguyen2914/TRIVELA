"""The provider watch must alert on a CHANGE, not on every run.

clubelo went dark on 2026-07-31 and the friendlies board fell from 66
rated fixtures to 23 with no signal that a third party was the cause. A
daily "still down" would be noise that trains you to ignore the channel;
the two moments worth a message are going dark and coming back.
"""
import pytest

from jobs import scheduler


@pytest.fixture(autouse=True)
def _reset():
    scheduler._clubelo_state.clear()
    yield
    scheduler._clubelo_state.clear()


def _rows(monkeypatch, n):
    from src.live import clubelo
    monkeypatch.setattr(clubelo, "day_ranking",
                        lambda day: {f"c{i}": {} for i in range(n)})


def test_first_probe_does_not_alert(monkeypatch):
    sent = []
    from src import alerts
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: sent.append((a, k)))
    _rows(monkeypatch, 0)
    scheduler.clubelo_watch_job()
    assert sent == [], "a cold start alerted before knowing any prior state"


def test_recovery_alerts_once_and_not_again(monkeypatch):
    sent = []
    from src import alerts
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: sent.append(k.get("title")))
    _rows(monkeypatch, 0)
    scheduler.clubelo_watch_job()          # first probe: down, silent
    _rows(monkeypatch, 700)
    scheduler.clubelo_watch_job()          # transition up: one alert
    scheduler.clubelo_watch_job()          # still up: silent
    scheduler.clubelo_watch_job()
    assert sent == ["clubelo RECOVERED"], sent


def test_going_down_alerts_once(monkeypatch):
    sent = []
    from src import alerts
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: sent.append(k.get("title")))
    _rows(monkeypatch, 700)
    scheduler.clubelo_watch_job()
    _rows(monkeypatch, 0)
    scheduler.clubelo_watch_job()
    scheduler.clubelo_watch_job()
    assert sent == ["clubelo DOWN"], sent


def test_the_watch_bypasses_its_own_negative_cache(monkeypatch):
    """A watch that reads the cached failure would report 'down' forever
    after the first miss, and never notice the recovery it exists for."""
    from src.live import clubelo
    clubelo._cache["x"] = (0.0, {})
    seen = []

    def fake(day):
        seen.append(day in clubelo._cache)
        return {}

    monkeypatch.setattr(clubelo, "day_ranking", fake)
    scheduler.clubelo_watch_job()
    assert seen == [False], "the probe did not clear today's cache entry"


def test_a_probe_error_does_not_kill_the_scheduler(monkeypatch):
    from src.live import clubelo
    monkeypatch.setattr(clubelo, "day_ranking",
                        lambda day: (_ for _ in ()).throw(RuntimeError("x")))
    scheduler.clubelo_watch_job()          # must not raise
