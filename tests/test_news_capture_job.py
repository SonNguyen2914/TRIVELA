"""The capture runs unattended WITHOUT crossing the lock-path boundary.

#53 wired this inside jobs/scheduler.py and the isolation guard rightly
killed it: scheduler is lock-path, and nothing lock-path may name team
news. jobs/news_capture.py is the indirection — it owns the import, and
these tests pin BOTH directions of the boundary plus the job's honesty.
"""
import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from jobs import news_capture, scheduler


class _Fx:
    def __init__(self, i, ref, ko):
        self.id = i
        self.provider_fixture_id = ref
        self.current_kickoff_utc = ko


def test_the_fixture_stub_matches_real_columns():
    from src.live.models import Fixture
    cols = set(Fixture.__table__.columns.keys())
    for a in ("id", "provider_fixture_id", "current_kickoff_utc"):
        assert a in cols


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


def test_captures_every_provider_id_in_the_window(monkeypatch):
    seen = []
    now = datetime.now(timezone.utc)
    _wire(monkeypatch, [_Fx(1, "111", now + timedelta(hours=1)),
                        _Fx(2, None, now + timedelta(hours=1)),
                        _Fx(3, "333", now + timedelta(hours=2))],
          lambda ref, **k: seen.append(ref) or {"stored": 1})
    news_capture.capture_window_job()
    assert seen == ["111", "333"]        # ESPN-only fixture skipped


def test_a_dormant_return_is_a_failure_not_a_capture(monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    _wire(monkeypatch, [_Fx(1, "111", now + timedelta(hours=1))],
          lambda ref, **k: {"dormant": True})
    news_capture.capture_window_job()
    out = capsys.readouterr().out
    assert "0 captured" in out and "1 failed" in out


def test_one_failure_does_not_abort_the_sweep(monkeypatch):
    seen = []
    now = datetime.now(timezone.utc)

    def cap(ref, **k):
        if ref == "111":
            raise RuntimeError("500")
        seen.append(ref)
        return {"stored": 1}

    _wire(monkeypatch, [_Fx(1, "111", now + timedelta(hours=1)),
                        _Fx(2, "222", now + timedelta(hours=2))], cap)
    news_capture.capture_window_job()
    assert seen == ["222"]


def test_news_capture_imports_no_model_or_lock_module():
    """One direction of the boundary: this module reads team news and
    NOTHING that writes forecast evidence."""
    src = inspect.getsource(news_capture)
    for banned in ("model_mls", "runs", "paper", "simulator", "xg_model",
                   "model_eval", "slate", "corpus", "audit", "risk"):
        for node in ast.walk(ast.parse(src)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [a.name for a in node.names]
            assert not any(banned == m.split(".")[-1] for m in mods), (
                f"news_capture imports {banned}")


def test_scheduler_still_never_names_team_news():
    """The other direction: the indirection must leave scheduler clean."""
    src = inspect.getsource(scheduler)
    assert "team_news" not in src
    assert 'id="news_capture"' in src
