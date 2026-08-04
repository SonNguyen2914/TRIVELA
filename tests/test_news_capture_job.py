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


def _wire(monkeypatch, fixtures, capture, comp_rows=None):
    # the comp sweep would otherwise hit the live registry (network) and
    # append real fixture ids — three tests failed exactly that way
    from src import competitions
    monkeypatch.setattr(
        competitions, "fixtures",
        lambda key, **k: {"fixtures": comp_rows or []})
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


def test_the_comp_sweep_joins_the_window(monkeypatch):
    """Viewer-competition fixtures inside the window are captured too —
    their provider ids key the same injuries endpoint."""
    from datetime import datetime, timedelta, timezone
    seen = []
    now = datetime.now(timezone.utc)
    # book-gated since 2026-08-04: a comp fixture is watched because a
    # Kalshi book exists for it, at ANY distance — tiered by proximity.
    # 9002 sits 30h out in the 6h tier, first sweep, so it IS due.
    comp = [{"fixture_id": 9001, "kalshi_event": "KX-A",
             "kickoff_utc": (now + timedelta(hours=1)).isoformat()},
            {"fixture_id": 9002, "kalshi_event": "KX-B",
             "kickoff_utc": (now + timedelta(hours=30)).isoformat()}]
    _wire(monkeypatch, [], lambda ref, **k: seen.append(ref) or {"stored": 1},
          comp_rows=comp)
    news_capture.capture_window_job()
    assert seen == ["9001", "9002"], seen


class TestBookKeyedWatching:
    """A booked match is watched from listing, tiered by proximity."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        news_capture._LAST_SWEPT.clear()
        news_capture._ALERTED.clear()
        yield
        news_capture._LAST_SWEPT.clear()
        news_capture._ALERTED.clear()

    def test_tiers(self):
        assert news_capture._tier_seconds(48) == 6 * 3600
        assert news_capture._tier_seconds(10) == 2 * 3600
        assert news_capture._tier_seconds(1) == 0.0

    def test_due_respects_the_tier_gap(self):
        assert news_capture._due("x", 48, 1000.0) is True     # first
        assert news_capture._due("x", 48, 1000.0 + 3600) is False
        assert news_capture._due("x", 48, 1000.0 + 7 * 3600) is True

    def test_an_unbooked_far_fixture_is_never_swept(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        seen = []
        now = datetime.now(timezone.utc)
        comp = [{"fixture_id": 9100, "kalshi_event": None,
                 "kickoff_utc": (now + timedelta(hours=50)).isoformat()},
                {"fixture_id": 9101, "kalshi_event": "KX-1",
                 "kickoff_utc": (now + timedelta(hours=50)).isoformat()}]
        _wire(monkeypatch, [],
              lambda ref, **k: seen.append(ref) or {"stored": 0},
              comp_rows=comp)
        news_capture.capture_window_job()
        assert seen == ["9101"], "book presence must gate the far watch"

    def test_a_new_absence_on_a_booked_match_alerts_once(self, monkeypatch):
        from datetime import datetime, timezone

        class _Row:
            subject_ref = "9101"; player_key = "p1"
            player_name = "A. Player"; provider_type = "Missing Fixture"
            provider_reason = "Knee Injury"

        from src.live import db
        class _Q:
            def filter(self, *a): return self
            def all(self): return [_Row()]
        class _S:
            def query(self, *a): return _Q()
            def close(self): pass
        monkeypatch.setattr(db, "get_session", lambda: _S())
        sent = []
        from src import alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda msg, **k: sent.append(msg))
        news_capture._alert_new_absences({"9101"})
        news_capture._alert_new_absences({"9101"})   # same row: silent
        assert len(sent) == 1
        assert "A. Player" in sent[0] and "Knee Injury" in sent[0]
