"""The narrow journal key has to survive the read-only door.

`_public_guard` rejects every mutating verb unless `_admin_ok` passes, and
it runs BEFORE any route. So a journal write carrying JOURNAL_TOKEN died
at the middleware and never reached `_journal_ok` — which meant the only
credential that could write the journal was ADMIN_TOKEN, the exact one
#51's narrow key exists to avoid ("a credential's blast radius is set by
its weakest holder, and ADMIN_TOKEN arms and disarms model planes").

Every test here runs with PUBLIC_READ_ONLY=True, because that is
production's actual state and the configuration the original verification
never exercised — it read the middleware's 403 as the scoped gate working.
A suite that only tests with the guard off cannot see this bug at all.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

import config

JOURNAL_WRITES = (
    "/api/admin/mls/journal/view",
    "/api/admin/mls/journal/resolve",
    "/api/admin/mls/journal/execution",
    "/api/admin/mls/journal/settlement",
    "/api/admin/mls/journal/reconcile",
)
READ_ONLY_MSG = "read-only mode"


@pytest.fixture()
def locked(monkeypatch):
    """Production's shape: read-only ON, both credentials configured and
    DIFFERENT — a test where they coincide proves nothing about which one
    opened the door."""
    from api import main as api_main
    monkeypatch.setattr(config, "PUBLIC_READ_ONLY", True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(config, "JOURNAL_TOKEN", "journal-secret")
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
    return TestClient(api_main.app, raise_server_exceptions=False)


def _blocked_by_middleware(resp) -> bool:
    """The middleware's refusal, distinguished from the route's."""
    if resp.status_code != 403:
        return False
    return READ_ONLY_MSG in str(resp.json().get("detail", ""))


class TestTheKeyReachesTheRoute:
    """Test 1 — the configuration the #51 verification never exercised."""

    @pytest.mark.parametrize("path", JOURNAL_WRITES)
    def test_journal_token_gets_past_the_read_only_guard(self, locked, path):
        r = locked.post(path, headers={"X-Journal-Token": "journal-secret"},
                        json={})
        # It must NOT be the middleware that answered. What the route then
        # does (422 for a bad body, 503 dormant, an error dict) is the
        # route's business — the point is that the request got there.
        assert not _blocked_by_middleware(r), (
            f"{path} still died at the middleware: {r.status_code} "
            f"{r.text[:120]}")


class TestTheDoorStillShuts:
    """Test 2 — a wrong or absent token dies exactly where it used to."""

    @pytest.mark.parametrize("path", JOURNAL_WRITES)
    def test_absent_token_is_refused_by_the_middleware(self, locked, path):
        assert _blocked_by_middleware(locked.post(path, json={}))

    @pytest.mark.parametrize("path", JOURNAL_WRITES)
    def test_wrong_token_is_refused_by_the_middleware(self, locked, path):
        r = locked.post(path, headers={"X-Journal-Token": "not-the-token"},
                        json={})
        assert _blocked_by_middleware(r)

    def test_an_empty_configured_journal_token_opens_nothing(
            self, locked, monkeypatch):
        """An unset JOURNAL_TOKEN must not match an empty header."""
        monkeypatch.setattr(config, "JOURNAL_TOKEN", "")
        r = locked.post(JOURNAL_WRITES[0], headers={"X-Journal-Token": ""},
                        json={})
        assert _blocked_by_middleware(r)


class TestTheKeyOpensNothingElse:
    """Test 3 — the blast radius. Approval activation above all: that is
    the route that arms and disarms model planes."""

    @pytest.mark.parametrize("path", (
        "/api/admin/mls/approval/activate",
        "/api/admin/mls/approval/bind-corpus",
        "/api/admin/mls/sweep",
        "/api/admin/mls/corpus/publish",
        "/api/admin/mls/broadcast",
        "/api/admin/mls/paper-backfill",
        # near-misses on the allowlist: neither may inherit it
        "/api/admin/mls/journal",
        "/api/admin/mls/journal/view/extra",
    ))
    def test_journal_token_is_refused_everywhere_else(self, locked, path):
        r = locked.post(path, headers={"X-Journal-Token": "journal-secret"},
                        json={})
        assert _blocked_by_middleware(r), (
            f"JOURNAL_TOKEN opened {path} — the narrow key must open "
            f"nothing but the journal writes")

    def test_the_allowlist_is_exactly_the_journal_write_routes(self):
        """Explicit, not a prefix: a new route under the same path must
        not inherit write access through the read-only door."""
        from api import main as api_main
        assert api_main._JOURNAL_WRITE_PATHS == frozenset(JOURNAL_WRITES)
        posts = {r.path for r in api_main.app.routes
                 if "POST" in getattr(r, "methods", set())
                 and "/journal/" in r.path}
        assert api_main._JOURNAL_WRITE_PATHS == posts, (
            "a journal write route exists that the allowlist does not "
            "name (or vice versa)")


class TestTheMiddlewareDoesNotAuthorize:
    """The exemption lets through; it never grants. The route decides."""

    def test_both_layers_check_independently(self, locked, monkeypatch):
        """`_journal_ok` must be consulted TWICE on a successful write:
        once by the middleware deciding whether to let the request
        through, once by the route deciding whether to act on it.

        One call would mean a single point of authorization — either the
        middleware granting on the route's behalf, or the route trusting
        a decision made upstream. Two is the property.
        """
        from api import main as api_main
        real = api_main._journal_ok
        calls = []

        def counting(request):
            calls.append(request.url.path)
            return real(request)

        monkeypatch.setattr(api_main, "_journal_ok", counting)
        locked.post("/api/admin/mls/journal/resolve",
                    headers={"X-Journal-Token": "journal-secret"},
                    json={"bet_id": 999999, "status": "passed"})
        assert calls.count("/api/admin/mls/journal/resolve") >= 2, (
            f"_journal_ok ran {len(calls)}x — the route must re-derive "
            f"authorization rather than inherit the middleware's verdict")

    def test_every_journal_route_still_guards_itself(self):
        """Structural: removing the route's own check would leave the
        middleware as the sole gate, which is what this whole change is
        careful NOT to create."""
        import inspect
        from api import main as api_main
        for route in api_main.app.routes:
            if getattr(route, "path", None) in JOURNAL_WRITES:
                src = inspect.getsource(route.endpoint)
                assert "_journal_ok(request)" in src, (
                    f"{route.path} does not run its own credential check")

    def test_nothing_is_stamped_on_the_request(self):
        """The middleware must not hand the route a verdict to trust."""
        import inspect
        from api import main as api_main
        src = inspect.getsource(api_main._public_guard)
        for smell in ("request.state", "request.scope[", "setattr(request"):
            assert smell not in src, (
                f"_public_guard writes {smell} — the route must re-derive "
                f"authorization, never inherit it")


class TestRedGreen:
    """Test 4 — remove the exemption and watch test 1 fail."""

    def test_without_the_allowlist_the_journal_write_is_blocked_again(
            self, locked, monkeypatch):
        from api import main as api_main
        monkeypatch.setattr(api_main, "_JOURNAL_WRITE_PATHS", frozenset())
        r = locked.post("/api/admin/mls/journal/view",
                        headers={"X-Journal-Token": "journal-secret"},
                        json={})
        assert _blocked_by_middleware(r), (
            "with the allowlist emptied the write should die at the "
            "middleware — if it does not, this suite is not testing the "
            "exemption at all")

    def test_and_with_it_restored_the_write_gets_through(self, locked):
        r = locked.post("/api/admin/mls/journal/view",
                        headers={"X-Journal-Token": "journal-secret"},
                        json={})
        assert not _blocked_by_middleware(r)
