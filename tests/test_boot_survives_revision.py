"""A replay plane's boot keeps an EARNED flag through a revision-only
deploy, and stays dark for everything else.

The unconditional force-dark was correct while the engine check was
strict equality — every deploy failed it, so an armed flag at boot was
indistinguishable from a stale one. #59 gave the runtime path a second
arm; this extends the same rule to boot. Fail-closed is preserved: the
flag is True only when the stored engine hash reproduces under either
the current revision or the decision's own.
"""
import json

import pytest

from src.live import model_eval


class _Row:
    def __init__(self, doc, rev):
        self.decision_document = doc
        self.code_revision = rev


class _FakeModel:
    """engine_signature(code_revision=...) — hash varies with revision,
    mirroring the real behaviour that revision moves the hash."""

    MODEL_NAME = "fake-2026-v0"

    def __init__(self, current_rev="rev-B"):
        self.current_rev = current_rev

    def engine_signature(self, code_revision=None):
        rev = code_revision or self.current_rev
        return {"signature_hash": f"sig-at-{rev}"}


def _wire(monkeypatch, row):
    from src.live import db

    class _Q:
        def filter_by(self, **k): return self
        def order_by(self, *a): return self
        def first(self): return row

    class _S:
        def query(self, *a): return _Q()
        def close(self): pass

    monkeypatch.setattr(db, "get_session", lambda: _S())


def test_no_decision_stays_dark(monkeypatch):
    _wire(monkeypatch, None)
    assert model_eval.boot_shadow_flag(_FakeModel(), "fake-2026-v0") is False


def test_unmoved_revision_stays_armed(monkeypatch):
    doc = json.dumps({"engine_signature": "sig-at-rev-B"})
    _wire(monkeypatch, _Row(doc, "rev-B"))
    assert model_eval.boot_shadow_flag(_FakeModel("rev-B"),
                                       "fake-2026-v0") is True


def test_revision_only_drift_stays_armed():
    """THE case: repo moved (rev-A -> rev-B), engine identical. The
    stored hash reproduces under the stored revision — second arm."""
    doc = json.dumps({"engine_signature": "sig-at-rev-A"})

    class _M(_FakeModel):
        pass

    import pytest as _p  # noqa
    from src.live import db as _db

    class _Q:
        def filter_by(self, **k): return self
        def order_by(self, *a): return self
        def first(self): return _Row(doc, "rev-A")

    class _S:
        def query(self, *a): return _Q()
        def close(self): pass

    import unittest.mock as mock
    with mock.patch.object(_db, "get_session", lambda: _S()):
        assert model_eval.boot_shadow_flag(_M("rev-B"),
                                           "fake-2026-v0") is True


def test_genuine_engine_change_stays_dark(monkeypatch):
    """Source changed: stored hash reproduces under NEITHER revision."""
    doc = json.dumps({"engine_signature": "sig-of-old-source"})
    _wire(monkeypatch, _Row(doc, "rev-A"))
    assert model_eval.boot_shadow_flag(_FakeModel("rev-B"),
                                       "fake-2026-v0") is False


def test_missing_revision_stays_dark(monkeypatch):
    doc = json.dumps({"engine_signature": "sig-at-rev-A"})
    _wire(monkeypatch, _Row(doc, None))
    assert model_eval.boot_shadow_flag(_FakeModel("rev-B"),
                                       "fake-2026-v0") is False


def test_any_error_stays_dark(monkeypatch):
    from src.live import db
    monkeypatch.setattr(db, "get_session",
                        lambda: (_ for _ in ()).throw(RuntimeError("db")))
    assert model_eval.boot_shadow_flag(_FakeModel(), "fake-2026-v0") is False


def test_both_planes_boot_through_the_flag_not_a_literal_false():
    import inspect

    from src.live import epl_plane, ligamx_plane
    for mod in (epl_plane, ligamx_plane):
        src = inspect.getsource(mod)
        assert "boot_shadow_flag" in src, mod.__name__
        assert "ensure_model_version(approved_for_shadow=False)" not in src, (
            f"{mod.__name__} still force-darks unconditionally")
