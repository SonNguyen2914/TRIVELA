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

    def test_the_t10_lock_record_is_operational_and_carries_the_odds(self):
        """The one call site whose class rests on READERSHIP, not wording.

        The gate work stripped the locked model's H/D/A from this alert.
        Son restored it on 2026-07-29 on two facts that reasoning lacked:
        the detail and action webhooks are DISTINCT in Railway, and he is
        currently the channel's only reader — his friend bets on what Son
        concludes and relays, never on this feed. So this is the operator
        reading his own instrument, and OPERATIONAL is honest for it.

        If the friend is ever given channel access, this call site becomes
        a model signal and must be refused while the money lock is on.

        The league name was a literal "MLS" until the competition-keyed
        merge of 2026-07-30 parameterized it to {spec.label}, so the lock
        record now reads correctly for EPL, La Liga and Liga MX too. The
        assertion follows the source; what it GUARDS is unchanged — the
        odds still travel and the class is still OPERATIONAL."""
        src = open(os.path.join(REPO_ROOT, "src", "live", "runs.py"),
                   encoding="utf-8").read()
        assert "PAPER · {spec.label} T-10 lock" in src
        assert "raw_probability for c in o" in src
        assert "dispatch_class=OPERATIONAL" in src


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
# This is the historical list, kept whole and asserted against the two
# lists below, so a future edit cannot quietly shorten what is forbidden.
FORBIDDEN_OUTSIDE_THE_GATE = (
    "_post_discord", "_post_ntfy", "send_discord", "send_ntfy",
    "DISCORD_ACTION_WEBHOOK_URL", "DISCORD_DETAIL_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL", "NTFY_TOPIC", "ntfy.sh",
)

# The same list split by KIND, because the two kinds are matched
# differently (see `_gate_reaches`): a NAME is matched where it is
# referenced, a HOST is matched inside the string that builds the URL.
FORBIDDEN_IDENTIFIERS = frozenset({
    "_post_discord", "_post_ntfy", "send_discord", "send_ntfy",
    "DISCORD_ACTION_WEBHOOK_URL", "DISCORD_DETAIL_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL", "NTFY_TOPIC",
})
FORBIDDEN_ENDPOINTS = ("ntfy.sh",)


def _documentation_strings(tree: ast.AST) -> set[int]:
    """`id()` of every string literal that is DOCUMENTATION, not a value.

    A bare string expression statement is a no-op at runtime: module,
    class and function docstrings, and the free-standing paragraphs this
    repo writes under constants. Nothing evaluates them, so a credential
    named inside one is prose about the gate, not a read of it."""
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def _settings_key_strings(tree: ast.AST) -> set[int]:
    """`id()` of every string literal in an env-or-settings LOOKUP.

    That is a call argument (`os.getenv("NTFY_TOPIC")`,
    `getattr(config, "NTFY_TOPIC")`) or a subscript key
    (`os.environ["NTFY_TOPIC"]`). Position is half the test; the other
    half is in `_gate_reaches`, which requires the literal to equal the
    setting name exactly. "set NTFY_TOPIC in Railway" is a sentence
    about the setting, and printing a sentence reaches nothing."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value,
                                                                str):
                    out.add(id(arg))
        elif isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and isinstance(
                    node.slice.value, str):
                out.add(id(node.slice))
    return out


def _gate_reaches(source: str, filename: str = "<candidate>") -> list[str]:
    """Every reference in `source` that reaches past the alert gate.

    STRUCTURAL, not textual, and that is not a relaxation of the guard —
    it is the repair of a false-positive CLASS in how the guard was
    measured. A substring scan cannot tell `config.NTFY_TOPIC` from a
    comment saying only the gate may read `config.NTFY_TOPIC`, so every
    file that documented the gate tripped it: a runbook, an incident
    record, a docstring explaining why the gate exists. Reproduced
    2026-07-29 on a probe module whose only mention was in a comment and
    whose only import was `annotations`. The repair available in that
    moment was to shorten the forbidden list — weakening a real guard to
    quiet a bug in its scanner. Parsing removes the choice: comments
    never enter the tree at all, docstrings are excluded explicitly, and
    everything the textual scan legitimately caught is still caught.

    Reported as a reach:
      * a NAME or ATTRIBUTE reference — `NTFY_TOPIC`, `config.NTFY_TOPIC`,
        `alerts._post_discord` — including an assignment that redeclares
        one outside `config.py`
      * an IMPORT of one, under any alias
      * a string literal that IS a settings key, in a lookup position
      * a string literal containing the transport HOST, which builds the
        transport rather than naming it

    Out of scope on purpose: `getattr(config, "NTFY_" + "TOPIC")` and
    other computed spellings. This guard stops an author who does not
    know the gate exists; it was never a defence against evasion, and
    the textual scan it replaces could not catch those either."""
    tree = ast.parse(source, filename=filename)
    docs = _documentation_strings(tree)
    keys = _settings_key_strings(tree)
    found: list[str] = []

    def hit(token: str, node: ast.AST) -> None:
        found.append(f"{token} (line {getattr(node, 'lineno', '?')})")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_IDENTIFIERS:
                hit(node.id, node)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_IDENTIFIERS:
                hit(node.attr, node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # the imported NAME is what matters, not the local alias:
            # `from src.alerts import _post_discord as p` is a reach
            for alias in node.names:
                leaf = alias.name.split(".")[-1]
                if leaf in FORBIDDEN_IDENTIFIERS:
                    hit(leaf, node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue
            for host in FORBIDDEN_ENDPOINTS:
                if host in node.value:
                    hit(host, node)
            if node.value in FORBIDDEN_IDENTIFIERS and id(node) in keys:
                hit(node.value, node)
    return found


class TestTheGateIsTheOnlyDoor:

    def test_no_runtime_module_names_a_transport_or_a_webhook(self):
        offenders = []
        for path in _runtime_python_files():
            if path in (GATE_MODULE, CONFIG_MODULE):
                continue          # the gate owns them; config declares them
            rel = os.path.relpath(path, REPO_ROOT)
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            offenders += [f"{rel}: {r}"
                          for r in _gate_reaches(source, filename=path)]
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
# Acceptance 4b — the scanner above, measured on its own.
# ===========================================================================

class TestTheScannerSeesReferencesNotText:
    """A guard is only as good as what it measures, and the scan this
    replaced measured raw text — so it had a false-positive CLASS, not a
    false positive. Any file that documented the gate tripped it, which
    put the pressure on the forbidden list rather than on the scanner.

    Both directions are pinned here because only pinning both makes the
    change safe: prose stays green, and every real reach stays red. The
    snippets are source TEXT rather than files on disk so they cost
    nothing and cannot drift out of sync with the scanner they test.
    `tests/` is outside `_runtime_python_files()`, so the tokens spelled
    out below are not themselves scanned."""

    # --- real reaches: each of these must be caught ---------------------

    def test_a_bare_name_reference_is_a_reach(self):
        assert _gate_reaches("from config import NTFY_TOPIC\n"
                             "topic = NTFY_TOPIC\n")

    def test_an_attribute_read_of_the_setting_is_a_reach(self):
        assert _gate_reaches("import config\n"
                             "url = config.DISCORD_ACTION_WEBHOOK_URL\n")

    def test_importing_a_private_transport_is_a_reach(self):
        assert _gate_reaches("from src.alerts import _post_discord\n")

    def test_an_aliased_import_is_still_a_reach(self):
        """The local name is the author's choice; the imported one is the
        fact."""
        assert _gate_reaches("from src.alerts import _post_ntfy as push\n")

    def test_calling_a_transport_through_the_module_is_a_reach(self):
        assert _gate_reaches("from src import alerts\n"
                             "alerts._post_discord('hi')\n")

    def test_an_env_lookup_by_string_key_is_a_reach(self):
        assert _gate_reaches("import os\n"
                             "t = os.getenv('NTFY_TOPIC', '')\n")

    def test_a_getattr_lookup_is_a_reach(self):
        assert _gate_reaches("import config\n"
                             "u = getattr(config, 'DISCORD_WEBHOOK_URL')\n")

    def test_an_environ_subscript_is_a_reach(self):
        assert _gate_reaches("import os\n"
                             "t = os.environ['NTFY_TOPIC']\n")

    def test_building_the_transport_url_is_a_reach(self):
        """The host in a runtime string IS the transport, whatever the
        surrounding code calls itself."""
        assert _gate_reaches("import requests\n"
                             "requests.post(f'https://ntfy.sh/{t}')\n")

    def test_redeclaring_the_setting_elsewhere_is_a_reach(self):
        """A second home for the credential is a second door."""
        assert _gate_reaches("import os\n"
                             "NTFY_TOPIC = os.environ.get('X', '')\n")

    # --- prose: none of these may be caught ----------------------------
    # This is the defect. Every one of them failed the textual scan.

    def test_a_comment_naming_the_setting_is_not_a_reach(self):
        """The reproduced bug, 2026-07-29: a comment naming the ntfy
        topic setting failed the guard in a file that dispatched
        nothing."""
        assert _gate_reaches(
            "# never read NTFY_TOPIC here — only src/alerts.py may\n"
            "x = 1\n") == []

    def test_a_docstring_naming_the_transports_is_not_a_reach(self):
        assert _gate_reaches(
            '"""Why the gate exists: _post_discord and _post_ntfy are\n'
            'private, and DISCORD_ACTION_WEBHOOK_URL is a credential."""\n'
            "x = 1\n") == []

    def test_a_function_docstring_naming_a_setting_is_not_a_reach(self):
        assert _gate_reaches(
            "def f():\n"
            '    """Does not read NTFY_TOPIC. See src/alerts.py."""\n'
            "    return 1\n") == []

    def test_prose_mentioning_the_setting_in_a_log_line_is_not_a_reach(self):
        """An operator-facing sentence names the setting; it does not
        read it. Exact-match in a lookup position is what separates the
        two."""
        assert _gate_reaches(
            "print('set NTFY_TOPIC in Railway to enable push')\n") == []

    def test_an_incident_record_module_is_not_a_reach(self):
        """The shape that made this worth fixing: a module whose whole
        purpose is explaining the gate."""
        assert _gate_reaches(
            '"""Incident: DISCORD_ACTION_WEBHOOK_URL leaked into logs."""\n'
            "\n"
            "# The fix made _post_discord log a class, never the URL.\n"
            "REMEDIATED = True\n") == []

    # --- the guard's scope may not shrink ------------------------------

    def test_the_split_lists_still_cover_every_historical_token(self):
        """Structural matching was the change. WHAT is forbidden was
        not, and this is what stops the next author from quieting a
        scanner bug by deleting a token."""
        assert (set(FORBIDDEN_IDENTIFIERS) | set(FORBIDDEN_ENDPOINTS)
                == set(FORBIDDEN_OUTSIDE_THE_GATE))

    def test_every_historical_token_is_still_caught_somewhere(self):
        """Token by token, so a gap cannot hide behind the set equality
        above: each one, written as a real reference, is reported."""
        probes = {
            "_post_discord": "from src.alerts import _post_discord\n",
            "_post_ntfy": "from src.alerts import _post_ntfy\n",
            "send_discord": "from src.alerts import send_discord\n",
            "send_ntfy": "from src.alerts import send_ntfy\n",
            "DISCORD_ACTION_WEBHOOK_URL":
                "import config\nu = config.DISCORD_ACTION_WEBHOOK_URL\n",
            "DISCORD_DETAIL_WEBHOOK_URL":
                "import config\nu = config.DISCORD_DETAIL_WEBHOOK_URL\n",
            "DISCORD_WEBHOOK_URL":
                "import config\nu = config.DISCORD_WEBHOOK_URL\n",
            "NTFY_TOPIC": "import os\nt = os.getenv('NTFY_TOPIC')\n",
            "ntfy.sh": "u = 'https://ntfy.sh/' + t\n",
        }
        assert set(probes) == set(FORBIDDEN_OUTSIDE_THE_GATE), \
            "a forbidden token has no probe — add one"
        missed = [tok for tok, src in probes.items()
                  if not any(r.startswith(tok) for r in _gate_reaches(src))]
        assert missed == [], f"forbidden but not caught: {missed}"


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
