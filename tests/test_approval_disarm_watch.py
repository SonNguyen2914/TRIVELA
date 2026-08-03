"""A plane that loses its runtime flag must say so.

`approved_for_shadow` gates shadow collection and disarms on ANY
container restart, not only deploys — four times on 2026-08-01/02, most
with no deploy involved, one caught flipping true -> false inside two
minutes. Nothing warned, because `/api/*/approval` stays green: the
DECISION is immutable and persists, and only the runtime flag resets.

A disarmed plane at kickoff collects nothing and looks exactly like a
quiet slate.
"""
import pytest

from jobs import scheduler


class _MV:
    def __init__(self, name, armed):
        self.name, self.approved_for_shadow = name, armed


def _wire(monkeypatch, rows):
    from src.live import db
    monkeypatch.setattr(db, "plane_ready", lambda: True)

    class _Q:
        def filter(self, *a): return self
        def all(self): return rows

    class _S:
        def query(self, *a): return _Q()
        def close(self): pass

    monkeypatch.setattr(db, "get_session", lambda: _S())
    sent = []
    from src import alerts
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: sent.append(k.get("title")))
    return sent


@pytest.fixture(autouse=True)
def _reset():
    scheduler._APPROVAL_STATE.clear()
    yield
    scheduler._APPROVAL_STATE.clear()


def test_a_cold_start_does_not_alert(monkeypatch):
    sent = _wire(monkeypatch, [_MV("mls-2026-v0", False)])
    scheduler.approval_disarm_watch_job()
    assert sent == [], "alerted with no prior state to have changed from"


def test_a_disarm_alerts_once_and_not_again(monkeypatch):
    rows = [_MV("mls-2026-v0", True)]
    sent = _wire(monkeypatch, rows)
    scheduler.approval_disarm_watch_job()      # armed, silent
    rows[0].approved_for_shadow = False
    scheduler.approval_disarm_watch_job()      # transition: one alert
    scheduler.approval_disarm_watch_job()      # still disarmed: silent
    assert sent == ["shadow approval DISARMED"], sent


def test_a_rearm_clears_the_state_and_does_not_alert(monkeypatch):
    rows = [_MV("mls-2026-v0", True)]
    sent = _wire(monkeypatch, rows)
    scheduler.approval_disarm_watch_job()
    rows[0].approved_for_shadow = False
    scheduler.approval_disarm_watch_job()
    rows[0].approved_for_shadow = True
    scheduler.approval_disarm_watch_job()      # rearm is not an alert
    assert sent == ["shadow approval DISARMED"]
    # and a SECOND disarm must alert again — the state really reset
    rows[0].approved_for_shadow = False
    scheduler.approval_disarm_watch_job()
    assert len(sent) == 2, sent


def test_every_disarmed_plane_is_named(monkeypatch):
    rows = [_MV("mls-2026-v0", True), _MV("epl-2026-v0", True)]
    named = []
    from src.live import db
    monkeypatch.setattr(db, "plane_ready", lambda: True)

    class _Q:
        def filter(self, *a): return self
        def all(self): return rows

    class _S:
        def query(self, *a): return _Q()
        def close(self): pass

    monkeypatch.setattr(db, "get_session", lambda: _S())
    from src import alerts
    monkeypatch.setattr(alerts, "send_alert",
                        lambda msg, **k: named.append(msg))
    scheduler.approval_disarm_watch_job()
    for r in rows:
        r.approved_for_shadow = False
    scheduler.approval_disarm_watch_job()
    assert "mls-2026-v0" in named[0] and "epl-2026-v0" in named[0], named


def test_a_dormant_plane_does_not_alert(monkeypatch):
    sent = _wire(monkeypatch, [_MV("mls-2026-v0", True)])
    from src.live import db
    monkeypatch.setattr(db, "plane_ready", lambda: False)
    scheduler.approval_disarm_watch_job()
    assert sent == []


def test_the_job_is_registered():
    import inspect
    src = inspect.getsource(scheduler)
    assert 'id="approval_disarm_watch"' in src


def test_the_watch_list_matches_the_real_model_names():
    """A stale name watches nothing, silently — the same shape as the
    LAFC alias that pointed at a name ESPN does not use, and the same
    consequence: the guard is present, looks fine, and covers zero."""
    from src.live import (model_epl, model_laliga, model_ligamx,
                          model_mls)
    real = {model_mls.MODEL_NAME, model_epl.MODEL_NAME,
            model_ligamx.MODEL_NAME, model_laliga.MODEL_NAME}
    watched = set(scheduler.WATCHED_MODELS)
    assert not real - watched, f"unwatched planes: {real - watched}"
    assert not watched - real, f"watching names no model uses: {watched - real}"
