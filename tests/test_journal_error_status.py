"""A refusal must not answer 200.

Demonstrated in production on 2026-08-04: resolving bet 1 to an invalid
status answered `200 {"error": "resolve to taken or passed"}`. Nothing was
written; nothing above the body said so. An operator driving the loop from
curl at T-10 — the one moment nobody re-reads a response body — would have
read that as a completed resolution.

This is the same shape as the bug #61 fixed one layer up, where two
different 403s looked alike in a terminal. There the fix was to make the
layers distinguishable; here it is to make the envelope agree with the
body.

WHAT THESE TESTS PIN, and it is as much about what did NOT change:
  - refusals carry a 4xx/5xx
  - success still carries 200
  - the payload CONFLICT path still carries 409, not the new 400
  - `dormant` is 503, not 400 — the plane being down is not a caller error
  - error BODIES are byte-identical to before, because callers parse them
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from src.live.models import (Fixture, MarketContract, MarketEvent,
                             MarketQuote, MarketSnapshot, PredictionContract,
                             PredictionRun)
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc
HDRS = {"X-Journal-Token": "journal-secret"}
VIEW = "/api/admin/mls/journal/view"
RESOLVE = "/api/admin/mls/journal/resolve"


@pytest.fixture()
def client(monkeypatch):
    """Production's auth shape, with the read-only door open to the
    narrow key (#61) so these tests exercise the ROUTE, not the guard."""
    from api import main as api_main
    monkeypatch.setattr(config, "PUBLIC_READ_ONLY", True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(config, "JOURNAL_TOKEN", "journal-secret")
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
    return TestClient(api_main.app, raise_server_exceptions=False)


def _seed(s, kickoff_in_hours=3.0):
    """A real fixture with a real quote — the payload shapes the route
    actually sees, not a stub."""
    ko = datetime.now(UTC) + timedelta(hours=kickoff_in_hours)
    from tests import _livedb
    _livedb.add_ordered(s, *[
        Fixture(id=1, competition_slug="mls-2026", espn_event_id="e1",
                status="pre", current_kickoff_utc=ko),
        MarketSnapshot(id=1, fixture_id=1, captured_at=datetime.now(UTC),
                       status="complete", execution_ready=True),
        MarketEvent(id=1, competition_slug="mls-2026",
                    kalshi_event_ticker="KXMLSGAME-x", series="KXMLSGAME",
                    fixture_id=1, mapping_approved=True),
        MarketContract(id=1, market_event_id=1,
                       ticker="KXMLSGAME-x-H", outcome_key="home_win"),
        MarketQuote(id=1, market_contract_id=1,
                    captured_at=datetime.now(UTC) - timedelta(minutes=1),
                    yes_ask_c=45, yes_bid_c=43, yes_ask_size=100,
                    market_snapshot_id=1),
        PredictionRun(id="run1", fixture_id=1, run_type="scheduled",
                      status="complete", captured_at=datetime.now(UTC)),
    ])
    s.add(PredictionContract(prediction_run_id="run1", market_contract_id=1,
                             market_quote_id=1, outcome_key="home_win",
                             raw_probability=0.52))
    s.commit()


def _record(client, **over):
    body = {"fixture_id": 1, "market_ticker": "KXMLSGAME-x-H",
            "outcome_key": "home_win", "market_quote_id": 1,
            "stated_price": "0.45"}
    body.update(over)
    return client.post(VIEW, headers=HDRS, json=body)


class TestTheBugThatWasFound:
    """The exact production observation, now pinned."""

    def test_resolving_to_an_invalid_status_is_not_200(
            self, live_session, client):
        _seed(live_session)
        bet = _record(client).json()
        r = client.post(RESOLVE, headers=HDRS,
                        json={"bet_id": bet["id"], "status": "void"})
        assert r.status_code == 400, r.text
        # the body a caller parses is unchanged
        assert r.json()["error"] == "resolve to taken or passed"

    def test_resolving_an_unknown_entry_is_not_200(self, live_session,
                                                   client):
        _seed(live_session)
        r = client.post(RESOLVE, headers=HDRS,
                        json={"bet_id": 999999, "status": "passed"})
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "no such entry"


class TestRefusalsOnTheRecordRoute:
    def test_an_unknown_fixture_is_refused_with_a_status(
            self, live_session, client):
        _seed(live_session)
        r = _record(client, fixture_id=424242)
        assert r.status_code == 400, r.text
        assert "no such fixture" in r.json()["error"]

    def test_a_quote_from_another_fixture_is_refused_with_a_status(
            self, live_session, client):
        """journal-P0 F3 — an identity error, and the message that names
        the mismatch must survive the status change verbatim."""
        _seed(live_session)
        live_session.add_all([
            Fixture(id=2, competition_slug="mls-2026", espn_event_id="e2",
                    status="pre",
                    current_kickoff_utc=datetime.now(UTC)
                    + timedelta(hours=3)),
        ])
        live_session.commit()
        r = _record(client, fixture_id=2)
        assert r.status_code == 400, r.text
        assert "belongs to fixture 1" in r.json()["error"]


class TestWhatMustNotHaveChanged:
    def test_a_successful_record_is_still_200(self, live_session, client):
        _seed(live_session)
        r = _record(client)
        assert r.status_code == 200, r.text
        assert "error" not in r.json()
        assert r.json()["price_basis"] == "observed_quote"

    def test_a_successful_resolve_is_still_200(self, live_session, client):
        _seed(live_session)
        bet = _record(client).json()
        r = client.post(RESOLVE, headers=HDRS,
                        json={"bet_id": bet["id"], "status": "passed"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "passed"

    def test_dormant_is_503_not_400(self, monkeypatch, client):
        """The plane being down is not the caller's fault, and a 400
        would tell them to change a request that was fine."""
        from api import main as api_main
        from src.live import journal
        monkeypatch.setattr(journal, "resolve_view",
                            lambda *a, **k: {"error": "dormant"})
        r = client.post(RESOLVE, headers=HDRS,
                        json={"bet_id": 1, "status": "passed"})
        assert r.status_code == 503, r.text
        assert r.json()["error"] == "dormant"

    def test_a_payload_conflict_is_still_409_not_400(self, client,
                                                     monkeypatch):
        """journal-P0-I. Conflict is checked BEFORE the generic error
        branch; if that order ever flips, a contradictory retry starts
        answering 400 and loses its distinct meaning."""
        from api import main as api_main
        out = api_main._journal_write({"conflict": True, "error": "x"})
        assert out.status_code == 409

    def test_idempotent_replays_are_untouched(self, client):
        """A no-op retry is a SUCCESS, not a refusal — it carries no
        `error`, so the new branch must not see it."""
        from api import main as api_main
        out = api_main._journal_write({"idempotent": True, "id": 7})
        assert not hasattr(out, "status_code")   # passed through as 200


class TestTheClassificationIsStructural:
    def test_the_mapping_reads_fields_not_prose(self):
        """55 error strings live in journal.py. A mapping keyed on their
        wording is one an unlucky rewrite defeats — the failure the alert
        gate exists to prevent one layer down."""
        import inspect
        from api import main as api_main
        src = inspect.getsource(api_main._journal_write)
        # the only string compared against is the bare `dormant` sentinel
        import re
        compared = set(re.findall(r'==\s*"([^"]+)"', src))
        assert compared == {"dormant"}, compared

    def test_every_journal_write_route_is_wrapped(self):
        """A route that skips the wrapper answers 200 on refusal again —
        which is exactly how `view` and `resolve` came to differ from
        `execution` and `settlement` in the first place."""
        import inspect
        from api import main as api_main
        for route in api_main.app.routes:
            path = getattr(route, "path", "")
            if path in api_main.JOURNAL_WRITE_PATHS:
                src = inspect.getsource(route.endpoint)
                assert "_journal_write(" in src, (
                    f"{path} does not route its result through "
                    f"_journal_write — refusals will answer 200")
