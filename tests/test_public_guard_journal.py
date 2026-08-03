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
