"""The preflight yells BEFORE match day; the digest says all-clear."""
import pytest

from jobs import preflight


@pytest.fixture(autouse=True)
def _reset():
    preflight._SEEN.clear()
    yield
    preflight._SEEN.clear()


def _wire(monkeypatch, rows):
    from src import alerts, competitions
    monkeypatch.setattr(competitions, "fixtures",
                        lambda key, **k: {"fixtures": rows}
                        if key == "leagues-cup" else {"fixtures": []})
    sent = []
    monkeypatch.setattr(alerts, "send_alert",
                        lambda msg, **k: sent.append(msg))
    return sent


def _fx(name, reason, hours=4):
    from datetime import datetime, timedelta, timezone
    ko = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"kickoff_utc": ko,
            "home": {"name": name}, "away": {"name": "OK FC"},
            "strength": {"home": {"rated": False, "reason": reason},
                         "away": {"rated": True}}}


def test_an_unmapped_club_inside_the_window_alerts_once(monkeypatch):
    sent = _wire(monkeypatch, [_fx("Atlante FC", "name_unmapped")])
    preflight.preflight_job()
    preflight.preflight_job()          # same gap: silent second time
    assert len(sent) == 1
    assert "Atlante FC" in sent[0]
    assert "name-probe" in sent[0]     # points at the measured tool


def test_a_fixture_outside_the_window_is_ignored(monkeypatch):
    sent = _wire(monkeypatch, [_fx("Far FC", "name_unmapped", hours=90)])
    preflight.preflight_job()
    assert sent == []


def test_provider_failure_reasons_do_not_alert(monkeypatch):
    """provider_request_failed is an OUTAGE, not a coverage gap — the
    provider watches own that; double-alerting trains deafness."""
    sent = _wire(monkeypatch,
                 [_fx("Some FC", "provider_request_failed")])
    preflight.preflight_job()
    assert sent == []


def test_the_digest_reports_armed_and_disarmed(monkeypatch):
    from src import alerts
    from src.live import db

    class _MV:
        def __init__(self, n, a): self.name, self.approved_for_shadow = n, a

    class _Q:
        def all(self): return [_MV("mls-2026-v0", True),
                               _MV("epl-2026-v0", False)]

    class _S:
        def query(self, *a): return _Q()
        def close(self): pass

    monkeypatch.setattr(db, "plane_ready", lambda: True)
    monkeypatch.setattr(db, "get_session", lambda: _S())
    # providers isolated: the first version hit both live APIs and cost
    # the suite 22 seconds — a unit test paying network prices
    from src.live import clubelo, worldclubratings as w
    monkeypatch.setattr(clubelo, "day_ranking", lambda d: {"A": {}})
    monkeypatch.setattr(clubelo, "served_vintage",
                        lambda: {"stale": False, "table_day": None})
    monkeypatch.setattr(w, "ratings", lambda force=False: {"i": {}})
    sent = []
    monkeypatch.setattr(alerts, "send_alert",
                        lambda msg, **k: sent.append(msg))
    preflight.ops_digest_job()
    assert len(sent) == 1
    assert "mls-2026-v0" in sent[0] and "disarmed: epl-2026-v0" in sent[0]


def test_jobs_are_registered():
    import inspect

    from jobs import scheduler
    src = inspect.getsource(scheduler)
    assert 'id="coverage_preflight"' in src
    assert 'id="ops_digest"' in src
