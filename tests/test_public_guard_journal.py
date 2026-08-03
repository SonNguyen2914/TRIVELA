"""JOURNAL_TOKEN must survive the read-only middleware — in production's
actual configuration.

#51 built the narrow key and proved it at the route. The _public_guard
middleware outranked it silently: PUBLIC_READ_ONLY=true rejects every
mutating verb unless _admin_ok passes, and _admin_ok knows only
ADMIN_TOKEN. So the one credential the design existed to keep off phones
was the only one that could write. The post-#51 "live verification"
misread the middleware's 403 as the scoped gate firing — every test here
therefore runs with PUBLIC_READ_ONLY **True**, the configuration that
check never exercised.
"""
import pytest
from fastapi.testclient import TestClient

import api.main as main
import config


@pytest.fixture(autouse=True)
def _prod_mode(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_READ_ONLY", True)
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(config, "JOURNAL_TOKEN", "journal-secret")
    yield


def test_a_valid_journal_token_passes_the_middleware():
    """The case production never had: read-only ON, narrow key presented.
    Reaching the route at all is the assertion — any status but the
    middleware's own 403 proves passage."""
    c = TestClient(main.app)
    r = c.post("/api/admin/mls/journal/view",
               headers={"x-journal-token": "journal-secret"}, json={})
    assert not (r.status_code == 403
                and "read-only mode" in r.text), r.text


def test_a_wrong_token_still_dies_at_the_middleware():
    c = TestClient(main.app)
    r = c.post("/api/admin/mls/journal/view",
               headers={"x-journal-token": "wrong"}, json={})
    assert r.status_code == 403 and "read-only mode" in r.text


def test_the_narrow_key_opens_nothing_else_through_this_door():
    """Above all: approval activation. The key that lives on a phone must
    never arm or disarm a plane, and the middleware is the outer wall."""
    c = TestClient(main.app)
    for path in ("/api/admin/mls/approval/activate",
                 "/api/admin/mls/sweep",
                 "/api/admin/epl-2026/replay-approval/activate"):
        r = c.post(path, headers={"x-journal-token": "journal-secret"})
        assert r.status_code == 403 and "read-only mode" in r.text, path


def test_every_allowlisted_path_is_a_journal_route():
    for p in main.JOURNAL_WRITE_PATHS:
        assert "/journal/" in p, f"{p} joined the allowlist by accident"


def test_the_admin_token_is_unaffected():
    c = TestClient(main.app)
    r = c.post("/api/admin/mls/journal/view",
               headers={"x-admin-token": "admin-secret"}, json={})
    assert not (r.status_code == 403 and "read-only mode" in r.text)


def test_journal_ok_runs_twice_on_a_successful_write(monkeypatch):
    """Ported from the closed #62, where it was better than anything #61
    carried: one call would mean a single point of authorization no
    matter which layer held it. Two means the middleware LET THROUGH and
    the route DECIDED."""
    calls = []
    real = main._journal_ok

    def counting(request):
        r = real(request)
        calls.append(r)
        return r

    monkeypatch.setattr(main, "_journal_ok", counting)
    c = TestClient(main.app)
    c.post("/api/admin/mls/journal/view",
           headers={"x-journal-token": "journal-secret"}, json={})
    # FastAPI validates the body BEFORE the handler, so a
    # schema-mismatched post 422s after the middleware's call but before
    # the route's — the same trap the #51 tests fell into. Middleware
    # passage is asserted unconditionally; the route's second call only
    # when the request actually reached it.
    assert calls and calls[0] is True, "middleware never called or refused"
    r2 = c.post("/api/admin/mls/journal/view",
                headers={"x-journal-token": "journal-secret"}, json={})
    if r2.status_code != 422:
        assert len(calls) >= 2, (
            f"_journal_ok ran {len(calls)}x — one layer is not checking")


def test_the_allowlist_equals_the_journal_post_routes():
    """Also from #62: the list and the routes must not drift apart. A
    route added without joining the list is unreachable by the narrow
    key; a list entry without a route is a door to nothing."""
    import ast as _ast
    t = _ast.parse(open(main.__file__).read())
    posts = set()
    for x in _ast.walk(t):
        if isinstance(x, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            for d in x.decorator_list:
                u = _ast.unparse(d)
                if u.startswith("app.post") and "/journal/" in u:
                    posts.add(u.split("'")[1])
    assert posts == set(main.JOURNAL_WRITE_PATHS), (
        f"drift: routes={posts ^ set(main.JOURNAL_WRITE_PATHS)}")
