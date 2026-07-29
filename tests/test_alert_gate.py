"""The alert gate: what may reach Son's Discord, and what may not.

The defect this suite pins down (confirmed by independent review,
2026-07-29): the journal carve-out described a boundary that only
`journal.broadcast()` implemented. Every other producer —
`src/live_signals.py`, `src/positions.py`, `jobs/scheduler.py` — composed
betting content and handed it straight to the transports, and no
transport consulted `REAL_MONEY_SIGNALS_ENABLED`. Setting the flag false
stopped nothing. It was dormant only because `load_schedule()` still
points at a finished tournament.

EVERY REJECTION TEST HERE IS MEASURED AT THE NETWORK BOUNDARY.
`alerts.requests.post` is patched and counted, so the assertion is
"nothing left the process", not "the function we happened to patch was
not called". That distinction is what the previous rounds kept getting
wrong: a test that patches the layer above the defect passes over it.

Tests explicitly labelled CONTROL pass both before and after the fix.
They are stated as controls, never offered as evidence.
"""
from __future__ import annotations

import ast
import io
import os
from contextlib import redirect_stdout

import pytest

import config
from src import alerts
# The live-plane fixture and the REAL session-capability handshake live
# in the suites that own them. Reuse rather than build a second, subtly
# different capability — a stubbed one would prove nothing.
from tests.test_mls_shadow import live_session  # noqa: F401
from tests.test_personal_journal import session_cap  # noqa: F401

# Dummy credentials only. These are the shapes a real Discord webhook
# and a real ntfy topic have, so a leak test that passes on them would
# catch a leak of the real ones. No real credential is read here.
FAKE_WEBHOOK = ("https://discord.com/api/webhooks/000000000000000000/"
                "FAKE-WEBHOOK-TOKEN-DO-NOT-LOG")
FAKE_DETAIL_WEBHOOK = ("https://discord.com/api/webhooks/111111111111111111/"
                       "FAKE-DETAIL-TOKEN-DO-NOT-LOG")
FAKE_TOPIC = "fake-ntfy-topic-do-not-log"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Resp:
    status_code = 200


@pytest.fixture()
def wire(monkeypatch):
    """Configured transports plus a fake network that COUNTS posts.

    Configuring the webhooks matters: with them unset every transport
    short-circuits and a "no transport was called" assertion would pass
    for the wrong reason."""
    monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL", FAKE_WEBHOOK)
    monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL",
                        FAKE_DETAIL_WEBHOOK)
    monkeypatch.setattr(config, "NTFY_TOPIC", FAKE_TOPIC)
    posts: list[dict] = []

    def fake_post(url, json=None, data=None, headers=None, timeout=None):
        posts.append({"url": url, "json": json, "data": data})
        return _Resp()

    monkeypatch.setattr(alerts.requests, "post", fake_post)
    alerts._reset_refusals_for_tests()
    return posts


# ===========================================================================
# Acceptance 1 — with the flag false, every model/scheduler ACTION producer
# reaches zero transports and records a refusal.
# ===========================================================================

class TestModelProducersReachNoTransport:

    def test_the_money_lock_is_actually_off(self):
        """The premise every test below rests on. Stated, not assumed."""
        assert config.REAL_MONEY_SIGNALS_ENABLED is False

    def test_new_value_bet_is_refused(self, wire):
        alerts.alert_new_take("Cincinnati vs Columbus", "CIN win", 0.08,
                              0.14)
        assert wire == [], "a NEW VALUE BET reached a transport"
        self._assert_refused()

    def test_ripe_bet_window_is_refused(self, wire):
        alerts.alert_ripe("Cincinnati vs Columbus", "CIN win",
                          {"score": 82.0, "current_odds": 2.4,
                           "current_edge": 0.09,
                           "reasons": ["book thinned", "line moved"]})
        assert wire == [], "a RIPE alert reached a transport"
        self._assert_refused()

    def test_final_decision_lock_is_refused(self, wire):
        alerts.alert_final_lock("Cincinnati vs Columbus", {
            "market_title": "CIN win", "model_probability": 0.61,
            "decimal_odds": 2.1, "expected_value": 0.28,
            "recommendation": "TAKE"})
        assert wire == [], "a FINAL DECISION reached a transport"
        self._assert_refused()

    def test_final_decision_lock_skip_variant_is_refused(self, wire):
        """The no-bet branch is a separate dispatch and needs its own
        assertion — a gate applied to one arm of an if/else is not a
        gate."""
        alerts.alert_final_lock("Cincinnati vs Columbus", None)
        assert wire == []
        self._assert_refused()

    def test_live_buy_sell_signal_is_refused(self, wire):
        """Composed path through the REAL `_fire`: the LiveSignal row is
        still written (it is evidence about what the model said, which
        the lock does not govern) and the PUSH is refused."""
        from src.db import LiveSignal, SessionLocal, init_db
        from src import live_signals
        init_db()
        with SessionLocal() as s:
            s.query(LiveSignal).delete()
            s.commit()
        live_signals._fire(_FakeMatch(), _row(), "BUY", 0.12, "watched",
                           61.0)
        assert wire == [], "a BUY signal reached a transport"
        with SessionLocal() as s:
            assert s.query(LiveSignal).count() == 1, \
                "the evidence row must survive the refusal"
            s.query(LiveSignal).delete()
            s.commit()
        self._assert_refused("src.live_signals:_fire")

    def test_easy_win_is_refused(self, wire):
        from src.db import LiveSignal, SessionLocal, init_db
        from src import live_signals
        init_db()
        with SessionLocal() as s:
            s.query(LiveSignal).delete()
            s.commit()
        live_signals._fire(_FakeMatch(), _row(0.94, 0.80), "BUY", 0.14,
                           "easy_win", 71.0)
        assert wire == [], "an EASY WIN reached a transport"
        with SessionLocal() as s:
            s.query(LiveSignal).delete()
            s.commit()
        self._assert_refused("src.live_signals:_fire")

    def test_the_refusal_carries_no_message_content(self, wire):
        """The observability surface must not become a readable copy of
        the content the gate just withheld."""
        alerts.alert_new_take("Cincinnati vs Columbus", "CIN win", 0.08,
                              0.14)
        row = alerts.recent_refusals()[0]
        blob = repr(row)
        assert "NEW VALUE BET" not in blob
        assert "Cincinnati" not in blob

    def test_an_unknown_class_fails_closed(self, wire):
        """A class the gate does not recognise must be refused, not
        waved through: adding one without teaching the gate about it
        should stop dispatch, never open one."""
        out = alerts.send_alert("x", dispatch_class="something_new")
        assert out == {}
        assert wire == []
        assert alerts.recent_refusals()[0]["dispatch_class"] == \
            "something_new"

    def test_ambient_narration_cannot_reach_the_action_channel(self, wire):
        """AMBIENT_DETAIL carries model board numbers, so the gate — not
        the narrator's own good manners — is what keeps it off the
        act-now channel and the phone."""
        out = alerts.send_alert("board: CIN 41% (mkt 38%)", kind="action",
                                dispatch_class=alerts.AMBIENT_DETAIL)
        assert out == {} and wire == []
        assert alerts.recent_refusals()[0]["kind"] == "action"

    def test_session_relay_without_a_capability_is_refused(self, wire):
        """The carve-out is narrow: the class alone buys nothing. A
        caller that names SESSION_RELAY but presents no live capability
        is refused exactly like a model signal."""
        out = alerts.send_alert("prose", dispatch_class=alerts.SESSION_RELAY)
        assert out == {} and wire == []
        assert alerts.recent_refusals()[0]["dispatch_class"] == \
            alerts.SESSION_RELAY

    def test_a_forged_capability_object_is_refused(self, wire):
        """`SessionCapability` is an ordinary dataclass, so anyone can
        build one. Only the server-side store makes it real."""
        from datetime import datetime, timedelta, timezone
        from src.live.session_capability import SessionCapability
        now = datetime.now(timezone.utc)
        forged = SessionCapability(id="forged", label="me", issued_at=now,
                                   expires_at=now + timedelta(hours=1))
        out = alerts.send_alert("prose", dispatch_class=alerts.SESSION_RELAY,
                                capability=forged)
        assert out == {} and wire == []

    @staticmethod
    def _assert_refused(origin: str | None = None):
        """Every refusal is recorded, attributed and counted.

        `origin` is the first frame OUTSIDE the gate module, so the three
        `alert_*` composers — which live inside `src/alerts.py` — report
        their caller (the scheduler in production, this test here). That
        is the right attribution: the scheduler is the call site, the
        composer is just where the string is built. Where the producer
        is its own module the exact origin is asserted."""
        rows = alerts.recent_refusals()
        assert rows, "the refusal was silent — nothing was recorded"
        assert rows[0]["dispatch_class"] == alerts.MODEL_SIGNAL
        assert rows[0]["origin"] not in ("", "unknown")
        if origin is not None:
            assert rows[0]["origin"] == origin, rows[0]["origin"]
        assert rows[0]["real_money_signals_enabled"] is False
        assert alerts.refusal_counts()[alerts.MODEL_SIGNAL] >= 1


class _FakeMatch:
    match_id = "FX-GATE"
    home = "Cincinnati"
    away = "Columbus"


def _row(model_p: float = 0.70, market_p: float = 0.58) -> dict:
    return {"market_id": "KX-GATE-1", "market_title": "CIN win",
            "live_model_probability": model_p,
            "market_probability": market_p, "outcome_key": "home_win"}


# ===========================================================================
# Acceptance 2 — CONTROL. The carve-out still works with the flag false.
# Passes before AND after the fix; stated as a control, never as evidence.
# ===========================================================================

class TestSessionCarveOutStillDispatches:
    """CONTROL. A verified interactive-session capability dispatches
    while REAL_MONEY_SIGNALS_ENABLED is false, qualifier attached. This
    passed before the gate existed too — its job is to prove the fix did
    not take the carve-out down with the defect."""

    def test_a_live_capability_dispatches_with_the_qualifier(
            self, live_session, wire, session_cap):
        from tests.test_personal_journal import _seed
        from src.live import journal
        _seed(live_session)
        r = journal.broadcast("CIN 0.31, book thin", channel="action",
                              fixture_id=1,
                              capability=session_cap["capability"])
        assert r["dispatched"] is True
        assert wire, "the carve-out stopped dispatching"
        bodies = [p["json"]["content"] if p["json"] else
                  p["data"].decode("utf-8") for p in wire]
        for body in bodies:
            assert "CIN 0.31, book thin" in body
            assert "[shadow]" in body
            assert "not a real-money signal" in body
        assert alerts.recent_refusals() == []

    def test_the_gate_itself_rechecks_the_capability(self, wire,
                                                     session_cap):
        """NOT a control. The gate re-verifies against the server-side
        store rather than trusting that `journal.broadcast` checked
        first: a capability revoked between the two must not dispatch.
        A boundary that trusts its callers is a convention."""
        from src.live import session_capability as sc
        cap = session_cap["capability"]
        assert alerts.send_alert("prose",
                                 dispatch_class=alerts.SESSION_RELAY,
                                 capability=cap) != {}
        wire.clear()
        sc.close_session(session_cap["token"])
        assert alerts.send_alert("prose",
                                 dispatch_class=alerts.SESSION_RELAY,
                                 capability=cap) == {}
        assert wire == []


# ===========================================================================
# Acceptance 3 — CONTROL. Operational telemetry keeps working.
# ===========================================================================

class TestOperationalTelemetryStillDispatches:
    """CONTROL. Readiness, storage headroom and the channel probe are
    statements about the PLATFORM, carry no model output and no market
    view, and are not governed by the money lock. These passed before
    the gate existed; they are here so a future tightening cannot
    silence them without a red test. Silence is precisely how the
    DiskFull incident hid behind {"created": 0}."""

    def test_readiness_stall_alert_dispatches(self, live_session, wire):
        from datetime import datetime, timedelta, timezone
        import jobs.scheduler as sched
        from src.live import model_mls
        from src.live.models import ModelVersion
        mv = (live_session.query(ModelVersion)
              .filter_by(name=model_mls.MODEL_NAME).first())
        if mv is None:
            mv = ModelVersion(name=model_mls.MODEL_NAME,
                              approved_for_shadow=False)
            live_session.add(mv)
        mv.approved_for_shadow = False
        live_session.commit()
        sched._unapproved_alerted = False
        sched._unapproved_since = (datetime.now(timezone.utc)
                                   - timedelta(seconds=3600))
        sched.mls_readiness_watch()
        assert wire, "the readiness alert was silenced"
        assert sched._unapproved_alerted is True
        assert alerts.recent_refusals() == []

    def test_storage_headroom_alert_dispatches(self, wire, monkeypatch):
        from src.live import observability as obs
        monkeypatch.setattr(obs, "storage_headroom", lambda: {
            "dormant": False, "over_threshold": True, "used_pct": 88.0,
            "database_bytes": 4 * 1024 ** 3, "volume_bytes": 5 * 1024 ** 3,
            "threshold_pct": 80})
        obs._last_storage_alert = None
        r = obs.check_storage_headroom()
        assert r["alerted"] is True
        assert wire, "the storage alert was silenced"
        assert alerts.recent_refusals() == []

    def test_the_channel_probe_dispatches(self, wire):
        out = alerts.channel_probe()
        assert out["sent"] is True
        assert len(wire) == 3          # action + detail + ntfy legs
        assert alerts.recent_refusals() == []

    def test_the_t10_heartbeat_is_operational_and_carries_no_odds(self):
        """The one call site that was SPLIT rather than classified whole.
        It used to relay the locked model's H/D/A to the act-now
        channel; the operational half (the sweep took a lock) stays, the
        market view does not."""
        src = open(os.path.join(REPO_ROOT, "src", "live", "runs.py"),
                   encoding="utf-8").read()
        assert "T-10 lock TAKEN" in src
        assert "dispatch_class=OPERATIONAL" in src
        # the behavioural assertion (nothing shaped like a probability on
        # the wire) lives with the T-10 lock test in test_mls_shadow.py,
        # where the whole sweep actually runs


# ===========================================================================
# Acceptance 4 — static: nothing outside the gate can reach a transport.
# ===========================================================================

def _runtime_python_files() -> list[str]:
    """Every runtime source file: src/, api/, jobs/, scripts/, plus the
    top-level modules. Tests are excluded — a test that patches a
    private transport is exercising the gate, not routing around it."""
    out: list[str] = []
    for sub in ("src", "api", "jobs", "scripts"):
        for dirpath, dirnames, filenames in os.walk(
                os.path.join(REPO_ROOT, sub)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            out += [os.path.join(dirpath, f) for f in filenames
                    if f.endswith(".py")]
    for f in ("run.py", "config.py"):
        p = os.path.join(REPO_ROOT, f)
        if os.path.exists(p):
            out.append(p)
    assert out, "the runtime file list resolved to nothing"
    return sorted(out)


GATE_MODULE = os.path.join(REPO_ROOT, "src", "alerts.py")
CONFIG_MODULE = os.path.join(REPO_ROOT, "config.py")

# Naming any of these outside the gate is a route around it: the two
# transports, and the settings that ARE the credentials they post to.
FORBIDDEN_OUTSIDE_THE_GATE = (
    "_post_discord", "_post_ntfy", "send_discord", "send_ntfy",
    "DISCORD_ACTION_WEBHOOK_URL", "DISCORD_DETAIL_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL", "NTFY_TOPIC", "ntfy.sh",
)


class TestTheGateIsTheOnlyDoor:

    def test_no_runtime_module_names_a_transport_or_a_webhook(self):
        offenders = []
        for path in _runtime_python_files():
            if path in (GATE_MODULE, CONFIG_MODULE):
                continue          # the gate owns them; config declares them
            text = open(path, encoding="utf-8").read()
            for token in FORBIDDEN_OUTSIDE_THE_GATE:
                if token in text:
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}"
                                     f": {token}")
        assert offenders == [], (
            "these reach past the alert gate — the transports are private "
            "to src/alerts.py and the webhook/topic settings are read only "
            "there: " + "; ".join(offenders))

    def test_every_dispatch_call_site_declares_its_class(self):
        """`dispatch_class` has no default, so this cannot silently
        regress — but a call site is also where the classification
        belongs, and asserting it statically documents that."""
        undeclared = []
        for path in _runtime_python_files():
            if path == GATE_MODULE:
                continue
            tree = ast.parse(open(path, encoding="utf-8").read(),
                             filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute)
                        else None)
                if name != "send_alert":
                    continue
                if not any(kw.arg == "dispatch_class"
                           for kw in node.keywords):
                    undeclared.append(
                        f"{os.path.relpath(path, REPO_ROOT)}:"
                        f"{node.lineno}")
        assert undeclared == [], (
            "these dispatch without declaring a class: "
            + "; ".join(undeclared))

    def test_the_gate_exports_exactly_one_dispatch_entry_point(self):
        """`send_alert` is it. `channel_probe` and the three `alert_*`
        composers are exported too — they all route THROUGH the gate (or,
        for the probe, are fixed operational text inside it), so they add
        no bypass. What must not exist is a public raw transport."""
        public = [n for n in dir(alerts)
                  if not n.startswith("_") and callable(getattr(alerts, n))]
        assert "send_alert" in public
        assert "send_discord" not in public
        assert "send_ntfy" not in public


# ===========================================================================
# Fix 2 — transport failure logging must never print a credential.
# ===========================================================================

class TestTransportLoggingNeverPrintsCredentials:
    """A Discord webhook URL and an ntfy topic ARE credentials: anyone
    holding either can post into Son's channel. `requests` exceptions
    embed the request URL, so `print(exc)` publishes the credential into
    the logs. Dummy credentials only, below."""

    SECRET_BODY = "SENTINEL-PROSE-that-must-not-reach-the-logs"

    def _raiser(self, url_in_exception: str):
        import requests as _rq

        def boom(*a, **kw):
            raise _rq.ConnectionError(
                f"HTTPSConnectionPool: Max retries exceeded with url: "
                f"{url_in_exception} (Caused by NewConnectionError)")
        return boom

    def test_discord_failure_logs_the_class_not_the_webhook(
            self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL",
                            FAKE_WEBHOOK)
        monkeypatch.setattr(alerts.requests, "post",
                            self._raiser(FAKE_WEBHOOK))
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert alerts._post_discord(self.SECRET_BODY) is False
        out = buf.getvalue()
        assert "discord" in out                      # the transport NAME
        assert "ConnectionError" in out              # the error CLASS
        assert FAKE_WEBHOOK not in out
        assert "FAKE-WEBHOOK-TOKEN-DO-NOT-LOG" not in out
        assert "discord.com" not in out
        assert "/api/webhooks/" not in out
        assert self.SECRET_BODY not in out

    def test_ntfy_failure_logs_the_class_not_the_topic(self, monkeypatch):
        monkeypatch.setattr(config, "NTFY_TOPIC", FAKE_TOPIC)
        monkeypatch.setattr(alerts.requests, "post",
                            self._raiser(f"https://ntfy.sh/{FAKE_TOPIC}"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert alerts._post_ntfy(self.SECRET_BODY) is False
        out = buf.getvalue()
        assert "ntfy" in out
        assert "ConnectionError" in out
        assert FAKE_TOPIC not in out
        assert "ntfy.sh/" not in out
        assert self.SECRET_BODY not in out

    def test_an_http_status_is_kept_because_it_is_diagnostic(self,
                                                             monkeypatch):
        """The sanitizer strips the URL, not the usefulness."""
        import requests as _rq

        class _Err:
            status_code = 404

        def boom(*a, **kw):
            exc = _rq.HTTPError("404 Client Error for url: " + FAKE_WEBHOOK)
            exc.response = _Err()
            raise exc

        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL",
                            FAKE_WEBHOOK)
        monkeypatch.setattr(alerts.requests, "post", boom)
        buf = io.StringIO()
        with redirect_stdout(buf):
            alerts._post_discord("x")
        out = buf.getvalue()
        # credential assertion FIRST — it is the security property; the
        # status assertion after it is the usability one
        assert FAKE_WEBHOOK not in out
        assert "discord.com" not in out
        assert "HTTP 404" in out

    def test_the_unconfigured_branch_never_prints_the_message(self,
                                                              monkeypatch):
        """The no-webhook path used to print the first 80 characters of
        the message. On the relay channel that is the prose itself."""
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL", "")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert alerts._post_discord(self.SECRET_BODY) is False
        out = buf.getvalue()
        # the leak assertion FIRST, so a baseline run fails on the leak
        # rather than on a changed log prefix
        assert self.SECRET_BODY not in out
        assert "SENTINEL" not in out
        assert "discord/action" in out
