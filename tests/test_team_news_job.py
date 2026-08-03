"""The team-news capture must actually run, and must not lie when it fails.

`capture_absences` existed since the team-news work landed and NOTHING
called it outside probe scripts. Every fixture read `never_captured` and
the feed was dead — measured 2026-07-31, zero announced XI across 15
slate fixtures all day, including T-5 on a fixture whose strength read
was available.
"""
from datetime import datetime, timedelta, timezone

import pytest

from jobs import scheduler


class _Fx:
    """Mirrors the REAL Fixture columns. The first version invented
    `provider_fixture_ref` and `kickoff_utc`; neither exists, and the job
    failed at runtime while the fixture happily supplied them."""

    def __init__(self, i, ref, ko):
        self.id = i
        self.provider_fixture_id = ref
        self.current_kickoff_utc = ko


def _wire(monkeypatch, fixtures, capture):
    from src.live import db, team_news
    monkeypatch.setattr(team_news, "plane_ready", lambda: True)
    monkeypatch.setattr(team_news, "capture_absences", capture)

    class _Q:
        def filter(self, *a): return self
        def all(self): return fixtures

    class _S:
        def query(self, *a): return _Q()
        def close(self): pass

    monkeypatch.setattr(db, "get_session", lambda: _S())


def test_it_captures_every_fixture_in_the_window(monkeypatch):
    seen = []
    now = datetime.now(timezone.utc)
    fx = [_Fx(1, "111", now + timedelta(hours=1)),
          _Fx(2, "222", now + timedelta(hours=2))]
    _wire(monkeypatch, fx,
          lambda ref, **k: seen.append(ref) or {"stored": 3})
    scheduler.team_news_job()
    assert seen == ["111", "222"], seen


def test_a_dormant_return_is_not_counted_as_a_capture(monkeypatch, capsys):
    """The whole failure mode: a dead feed reporting healthy."""
    now = datetime.now(timezone.utc)
    _wire(monkeypatch, [_Fx(1, "111", now + timedelta(hours=1))],
          lambda ref, **k: {"dormant": True})
    scheduler.team_news_job()
    out = capsys.readouterr().out
    assert "0 captured" in out, out
    assert "1 failed" in out, out


def test_one_failing_fixture_does_not_stop_the_rest(monkeypatch):
    seen = []
    now = datetime.now(timezone.utc)
    fx = [_Fx(1, "111", now + timedelta(hours=1)),
          _Fx(2, "222", now + timedelta(hours=2))]

    def cap(ref, **k):
        if ref == "111":
            raise RuntimeError("provider 500")
        seen.append(ref)
        return {"stored": 1}

    _wire(monkeypatch, fx, cap)
    scheduler.team_news_job()
    assert seen == ["222"], "a failing fixture aborted the sweep"


def test_a_dormant_plane_does_not_burn_provider_budget(monkeypatch):
    called = []
    from src.live import team_news
    monkeypatch.setattr(team_news, "plane_ready", lambda: False)
    monkeypatch.setattr(team_news, "capture_absences",
                        lambda *a, **k: called.append(1))
    scheduler.team_news_job()
    assert called == []


def test_the_job_is_registered_on_the_scheduler():
    """It existing is the point — the capture function was never dead
    code, it was live code nothing invoked."""
    import inspect
    src = inspect.getsource(scheduler)
    assert 'id="team_news"' in src, "the job is not registered"
    assert "team_news_job" in src


def test_the_fixture_stub_matches_the_real_columns():
    """A stub that invents column names lets the job pass a test it
    cannot survive in production — which is exactly what happened."""
    from src.live.models import Fixture
    cols = set(Fixture.__table__.columns.keys())
    for attr in ("id", "provider_fixture_id", "current_kickoff_utc"):
        assert attr in cols, f"{attr} is not a real Fixture column"


def test_a_fixture_without_a_provider_id_is_skipped(monkeypatch):
    """An ESPN-only fixture cannot be queried on the injuries endpoint;
    passing its id would return nothing forever, silently."""
    seen = []
    now = datetime.now(timezone.utc)
    _wire(monkeypatch, [_Fx(1, None, now + timedelta(hours=1))],
          lambda ref, **k: seen.append(ref) or {"stored": 1})
    scheduler.team_news_job()
    assert seen == []
