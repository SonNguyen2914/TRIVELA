"""JOURNAL_TOKEN may write the journal and NOTHING else.

The pick loop runs on a phone. ADMIN_TOKEN reaches every admin route
including approval activation — arming and disarming model planes — so
handing it to a pick-logger is an over-grant whose blast radius is set by
its weakest holder.

The two properties that matter: the narrow key opens the journal, and it
opens nothing else. Both are asserted, because a scoped credential that
silently widens is worse than no scoping at all.
"""
import ast

import pytest
from fastapi.testclient import TestClient

import api.main as main
import config

JOURNAL_ROUTES = ["/api/admin/mls/journal", "/api/admin/mls/journal/view",
                  "/api/admin/mls/journal/resolve",
                  "/api/admin/mls/journal/execution",
                  "/api/admin/mls/journal/settlement",
                  "/api/admin/mls/journal/reconcile"]


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_READ_ONLY", False)
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(config, "JOURNAL_TOKEN", "journal-secret")
    yield


def _routes_using(name: str) -> set[str]:
    t = ast.parse(open(main.__file__).read())
    out = set()
    for x in ast.walk(t):
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decs = [ast.unparse(d) for d in x.decorator_list]
            route = next((d for d in decs if d.startswith("app.")), None)
            if route and name in ast.unparse(x):
                out.add(route.split("'")[1] if "'" in route else route)
    return out


def test_the_journal_token_reaches_only_journal_routes():
    scoped = _routes_using("_journal_ok")
    assert scoped, "no route accepts the scoped token"
    for r in scoped:
        assert "/journal" in r, f"{r} accepts the journal token but is not journal"


def test_approval_activation_never_accepts_the_journal_token():
    """The whole point. If this ever fails, a phone can disarm a plane."""
    scoped = _routes_using("_journal_ok")
    for r in scoped:
        assert "activate" not in r, f"{r} is reachable with the narrow key"
    admin_only = _routes_using("_admin_ok")
    assert any("activate" in r for r in admin_only)


class _Req:
    """Minimal request stand-in. The check is tested DIRECTLY rather than
    through a route: FastAPI validates the body before the handler runs,
    so a schema-mismatched post returns 422 and never reaches the auth
    branch — which would have made these assertions vacuous."""

    def __init__(self, **headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


def test_an_unset_journal_token_grants_nothing(monkeypatch):
    """It must never fall back to ADMIN_TOKEN — a silent widening of
    scope is the failure this constant exists to prevent."""
    monkeypatch.setattr(config, "JOURNAL_TOKEN", "")
    assert main._journal_ok(_Req()) is False
    assert main._journal_ok(_Req(**{"x-journal-token": ""})) is False
    # and crucially: the ADMIN token must not be accepted via the
    # journal header just because JOURNAL_TOKEN is unset
    assert main._journal_ok(
        _Req(**{"x-journal-token": "admin-secret"})) is False


def test_a_wrong_journal_token_is_refused():
    assert main._journal_ok(_Req(**{"x-journal-token": "nope"})) is False


def test_the_right_journal_token_is_accepted():
    assert main._journal_ok(
        _Req(**{"x-journal-token": "journal-secret"})) is True


def test_the_operator_token_still_outranks_it():
    c = TestClient(main.app)
    r = c.post("/api/admin/mls/journal/view",
               headers={"x-admin-token": "admin-secret"}, json={})
    assert r.status_code != 403


def test_the_journal_token_cannot_activate_an_approval():
    """End to end, not just structural."""
    c = TestClient(main.app)
    r = c.post("/api/admin/mls/approval/activate",
               headers={"x-journal-token": "journal-secret"})
    assert r.status_code == 403, r.status_code
