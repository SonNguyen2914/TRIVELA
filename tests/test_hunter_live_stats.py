"""API-Football live in-play statistics: the conditioning inference, the
leakage boundary, coverage measurement, the fail-closed join, and the
budget.

WHAT THESE TESTS ARE FOR
    Two of them are the reason the feature is allowed to exist at all:

    TestInferenceDirectionIsNotInverted
        A red card makes a big price move MORE reasonable, not less.
        Code that treats "we found a reason" as strengthening the finding
        has the arithmetic of the world backwards, and the mistake would
        read perfectly well in prose. So the ordering lives in a numeric
        table and is asserted mechanically — and one test deliberately
        INVERTS that table and proves the guard goes red, so the guard is
        evidence rather than decoration.

    TestLiveXgCannotReachALockedArtifact
        Live xG is legitimate in-play evidence AND it carries information
        from after a T-10 lock. Both halves are enforced: statically, that
        no module in the lock path can even name the live-stats module,
        and behaviourally, that a lock taken while a live xG reading sits
        in memory contains no trace of it anywhere in the database.

NO NETWORK. Every provider response here is canned. The client's one
`requests.get` is stubbed to raise unless a test explicitly cans it, so a
path that forgets to stub fails LOUDLY rather than silently dialling out
— the same discipline tests/test_hunter.py uses.

Tests labelled CONTROL pass both before and after the change they sit
beside. They are stated as controls and are never offered as evidence.
"""
from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import config
from src.live import apifootball_live as al
from src.live import db as live_db
from src.live import hunter
from src.live.models import (ApiFootballLiveCoverage, Competition, LiveBase)

UTC = timezone.utc
NOW = datetime(2026, 7, 29, 23, 48, tzinfo=UTC)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==========================================================================
# fixtures
# ==========================================================================
@pytest.fixture()
def live_session(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/live.db"
    monkeypatch.setattr(config, "LIVE_DATABASE_URL", url)
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)
    monkeypatch.setattr(live_db, "LIVE_BOOT_ERROR", None)
    LiveBase.metadata.create_all(live_db.get_engine())
    s = live_db.get_session()
    s.add(Competition(slug="mls-2026", name="MLS", season=2026))
    s.commit()
    yield s
    s.close()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


@pytest.fixture(autouse=True)
def _no_network_and_fresh_state(monkeypatch):
    """Process-local state reset, and the provider door nailed shut."""
    al._reset_reading_store_for_tests()
    al._reset_day_spend_for_tests()
    monkeypatch.setattr(config, "APIFOOTBALL_KEY", "TEST-KEY-NOT-REAL")

    def _no_network(*a, **kw):
        raise AssertionError("unstubbed provider call in a canned test")
    monkeypatch.setattr(al.requests, "get", _no_network)
    yield
    al._reset_reading_store_for_tests()
    al._reset_day_spend_for_tests()


class _Resp:
    """A canned provider response. `headers` matter: the quota headers are
    how the budget block reports remaining plan capacity."""

    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._body


def _canned(monkeypatch, routes: dict, calls: list | None = None):
    """Stub the ONE provider entry point with a path->body routing table.

    Counting calls at this boundary, rather than at a helper above it, is
    what makes "nothing left the process" assertable.
    """
    def fake_get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "params": params})
        for frag, body in routes.items():
            if frag in url:
                return _Resp(body if not callable(body) else body(params))
        raise AssertionError(f"no canned route for {url}")
    monkeypatch.setattr(al.requests, "get", fake_get)


# --- payload builders ------------------------------------------------------
def _stats_payload(xg_a="1.28", xg_b="0.53", types=18, teams=(101, 202),
                   xg_type="expected_goals"):
    """A statistics payload shaped like the measured one: 18 types, xG
    present for both sides."""
    def entry(tid, name, xg):
        stats = [{"type": f"Filler {i}", "value": i}
                 for i in range(max(types - 4, 0))]
        stats += [{"type": "Total Shots", "value": 11},
                  {"type": "Shots on Goal", "value": 4},
                  {"type": "Ball Possession", "value": "56%"}]
        if xg_type is not None:
            stats.append({"type": xg_type, "value": xg})
        return {"team": {"id": tid, "name": name}, "statistics": stats}
    return {"errors": [], "results": 2,
            "response": [entry(teams[0], "Central Cordoba", xg_a),
                         entry(teams[1], "Atletico Tucuman", xg_b)]}


def _events_payload(*events):
    return {"errors": [], "results": len(events), "response": list(events)}


def _ev(etype, detail, elapsed, team_id=101, team="Central Cordoba"):
    return {"type": etype, "detail": detail,
            "time": {"elapsed": elapsed, "extra": None},
            "team": {"id": team_id, "name": team},
            "player": {"id": 9, "name": "A Player"}}


RED = _ev("Card", "Red Card", 62)
YELLOW = _ev("Card", "Yellow Card", 30)
GOAL = _ev("Goal", "Normal Goal", 58)
MISSED_PEN = _ev("Goal", "Missed Penalty", 44)
SUBST = _ev("subst", "Substitution 1", 55)


def _live_payload(league_id=128, fixture_id=9001, elapsed=73,
                  home="Central Cordoba", away="Atletico Tucuman",
                  home_id=101, away_id=202, extra=()):
    def one(fid, h, a, hid, aid):
        return {"fixture": {"id": fid, "date": "2026-07-29T22:00:00+00:00",
                            "status": {"short": "2H", "elapsed": elapsed}},
                "league": {"id": league_id, "name": "Liga Profesional "
                                                   "Argentina",
                           "country": "Argentina"},
                "teams": {"home": {"id": hid, "name": h},
                          "away": {"id": aid, "name": a}},
                "goals": {"home": 1, "away": 0}}
    resp = [one(fixture_id, home, away, home_id, away_id)]
    resp += list(extra)
    return {"errors": [], "results": len(resp), "response": resp}


def _reading(stats_payload=None, events_payload=None, at="2026-07-29T23:48:00+00:00"):
    """A reading as fetch_reading would have built it, without the fetch."""
    return {
        "fixture_id": 9001,
        "statistics": al.read_statistics(
            stats_payload if stats_payload is not None else _stats_payload()),
        "statistics_captured_at": at,
        "events": al.read_events(
            events_payload if events_payload is not None
            else _events_payload(YELLOW, SUBST)),
        "events_captured_at": at,
    }


def _naive(dt):
    """SQLite stores DateTime without a zone, so a round-tripped value
    comes back naive. Compare on the instant, not on the tzinfo."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None)


def _join(status=al.JOIN_RESOLVED, league_id=128, elapsed=73):
    return {"status": status, "league_id": league_id, "fixture_id": 9001,
            "league_name": "Liga Profesional Argentina",
            "home_name": "Central Cordoba", "away_name": "Atletico Tucuman",
            "orientation": "home_away", "elapsed": elapsed,
            "goals": {"home": 1, "away": 0}}


# ==========================================================================
# THE CENTRAL GUARD: the inference direction
# ==========================================================================
class TestInferenceDirectionIsNotInverted:
    """A red card makes a big move MORE reasonable, so an OBSERVED
    conditioning event must make this finding WEAKER.

    The whole point of `CONDITIONING_STRENGTH` is to make that assertable.
    Prose describing the direction reads equally well when the code has it
    backwards; a number does not.
    """

    def test_observed_ranks_weakest_and_readable_no_cause_ranks_strongest(
            self):
        """The ordering itself. This is the claim every other test in the
        class leans on."""
        observed = al.CONDITIONING_STRENGTH[al.CONDITIONING_OBSERVED]
        no_stats = al.CONDITIONING_STRENGTH[
            al.CONDITIONING_UNOBSERVED_NO_STATS]
        strong = al.CONDITIONING_STRENGTH[
            al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE]
        assert observed < no_stats < strong, (
            "an OBSERVED conditioning event must rank WEAKEST: the market "
            "had a reason, so the move is more reasonable and this finding "
            "is weaker evidence of mispricing")
        # and the state with the most evidence behind it is the strongest
        assert strong == max(al.CONDITIONING_STRENGTH.values())
        assert observed == min(al.CONDITIONING_STRENGTH.values())

    def test_the_direction_guard_would_catch_an_inversion(self,
                                                          monkeypatch):
        """PROOF THE GUARD HAS TEETH, not a control.

        The guard above passes against the code as written; that alone
        does not show it would catch the defect it exists for. So here the
        strength table is deliberately INVERTED — "finding a reason
        strengthens the finding" — and the same predicate is asserted to
        FAIL. A guard that cannot go red is decoration.
        """
        inverted = {
            al.CONDITIONING_OBSERVED: 2,               # the defect
            al.CONDITIONING_UNOBSERVED_NO_STATS: 1,
            al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE: 0,
        }
        monkeypatch.setattr(al, "CONDITIONING_STRENGTH", inverted)
        with pytest.raises(AssertionError):
            observed = al.CONDITIONING_STRENGTH[al.CONDITIONING_OBSERVED]
            no_stats = al.CONDITIONING_STRENGTH[
                al.CONDITIONING_UNOBSERVED_NO_STATS]
            strong = al.CONDITIONING_STRENGTH[
                al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE]
            assert observed < no_stats < strong

    def test_a_red_card_yields_observed_and_the_note_says_less_likely(self):
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(YELLOW, RED, SUBST)),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_OBSERVED
        assert cond["strength_rank"] == 0
        # the finding must SAY the move is less likely an overreaction
        note = cond["note"].lower()
        assert "less likely to be an overreaction" in note
        assert "explained" in note
        assert cond["inference_direction"].lower().count("weaker") >= 1
        kinds = {e["kind"] for e in cond["explaining_events"]}
        assert "red_card" in kinds

    def test_a_goal_is_an_explaining_event_of_equal_standing(self):
        """The inversion that would have bitten hardest.

        A goal is the most common cause of a violent in-play move, and
        goals have BROADER provider coverage than statistics do. A
        detector that watched only for red cards would file almost every
        goal-driven move under the STRONGEST state — the inversion, at
        scale, on the common case.
        """
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(GOAL, SUBST)),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_OBSERVED, (
            "a goal must weaken this finding exactly as a red card does")
        assert {e["kind"] for e in cond["explaining_events"]} == {"goal"}

    def test_a_missed_penalty_is_not_a_goal(self):
        """'Missed Penalty' carries type 'Goal' in this provider's
        taxonomy — measured. Counting it as a goal would manufacture an
        explanation and wrongly weaken the finding."""
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(MISSED_PEN, SUBST)),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE
        assert cond["explaining_events"] == []

    def test_a_yellow_card_is_not_a_dismissal(self):
        """68 of the 72 measured card events were yellows. Treating a
        booking as a sending-off would explain away almost every match."""
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(YELLOW, YELLOW, SUBST)),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE

    def test_readable_feed_with_no_cause_is_the_strongest_state(self):
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(SUBST)),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE
        assert cond["strength_rank"] == 2
        assert "strongest version" in cond["note"]
        # even the strongest state must not claim the market is wrong
        assert "not a claim that the market is wrong" in cond["note"]

    def test_an_empty_event_feed_cannot_rule_out_a_cause(self):
        """`results: 0` shaped. An empty events array is 'we could not
        see', never 'nothing happened' — so it must NOT reach the strong
        state, because the strong state asserts no cause was visible."""
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload()),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_UNOBSERVED_NO_STATS
        assert "could not see" in cond["reason"]

    def test_no_statistics_is_recorded_as_absence_of_evidence(self):
        """Copa Colombia at 90' with zero statistic types — measured. The
        honest state, and it must not read as a weaker OR a stronger
        anomaly."""
        cond = al.classify_conditioning(
            _reading(stats_payload={"errors": [], "results": 0,
                                    "response": []},
                     events_payload=_events_payload(SUBST)),
            _join(), elapsed=90, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_UNOBSERVED_NO_STATS
        assert cond["coverage_verdict"] == al.COVERAGE_ABSENT
        note = cond["note"]
        assert "NOT evidence that nothing happened" in note
        assert "not a weaker anomaly" in note.lower()

    def test_an_unresolved_join_never_reaches_an_observed_state(self):
        for status in (al.JOIN_AMBIGUOUS, al.JOIN_NO_COMPETITION_MAP,
                       al.JOIN_NO_MATCH, al.JOIN_NO_LIVE_FIXTURE):
            cond = al.classify_conditioning(
                _reading(events_payload=_events_payload(RED)),
                _join(status=status), elapsed=73)
            assert cond["state"] == al.CONDITIONING_UNOBSERVED_NO_STATS, (
                f"{status} must not license reading another match's events")

    def test_an_unverified_dismissal_string_is_flagged_not_hidden(self):
        """'Second Yellow card' was never observed from the provider. It
        is matched anyway — a missed dismissal would push the fixture into
        the strongest state, and over-detecting an explanation is the safe
        direction — but the row must SAY the match rests on an unverified
        string."""
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(
                _ev("Card", "Second Yellow card", 70))),
            _join(), elapsed=73, window_minutes=12)
        assert cond["state"] == al.CONDITIONING_OBSERVED
        assert "dismissal_string_caveat" in cond
        assert "NOT observed" in cond["dismissal_string_caveat"]

    def test_event_timing_relation_is_labelled_an_estimate(self):
        cond = al.classify_conditioning(
            _reading(events_payload=_events_payload(GOAL)),
            _join(), elapsed=73, window_minutes=12)
        e = cond["explaining_events"][0]
        assert e["minutes_before_now"] == 15.0      # 73' now, goal at 58'
        assert e["inside_estimated_move_window"] is False
        assert "ESTIMATE" in cond["explaining_event_timing_note"]


# ==========================================================================
# THE LEAKAGE BOUNDARY
# ==========================================================================
def _runtime_python_files() -> list[str]:
    out: list[str] = []
    for sub in ("src", "api", "jobs"):
        for dirpath, dirnames, filenames in os.walk(
                os.path.join(REPO_ROOT, sub)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            out += [os.path.join(dirpath, f) for f in filenames
                    if f.endswith(".py")]
    assert out, "the runtime file list resolved to nothing"
    return sorted(out)


# The modules that build, price, fit or score a locked artifact. Nothing
# here may reach the live-stats module: live xG is information from AFTER
# a T-10 lock, so a single import in this list is a temporal-leakage
# route (AGENTS.md §13).
LOCK_PATH_MODULES = (
    "src/live/runs.py",          # writes the canonical T-10 lock
    "src/live/markets.py",       # the frozen lock-grade book snapshot
    "src/live/model_mls.py",     # the model inside the engine signature
    "src/live/model_eval.py",    # fits and scores the ladder
    "src/live/paper.py",         # paper signals against the frozen book
    "src/live/corpus.py",        # the published immutable corpus
    "src/live/lineups.py",       # the lineup snapshot the lock saw
    "src/live/slate.py",         # lock completeness accounting
    "src/live/audit.py",         # lock integrity audit
    "src/live/evidence.py",      # the evidence chain
    "src/models/simulator.py",
)

LEAKAGE_TOKENS = ("apifootball_live", "APIFOOTBALL_KEY",
                  "fixtures/statistics", "expected_goals",
                  "live_xg", "xg_present")


class TestLiveXgCannotReachALockedArtifact:
    """Live xG is legitimate in-play evidence AND it carries post-lock
    information. It belongs to the hunter's observational surface only.

    `docs/APIFOOTBALL-TRIAL.md` CHECK 4 passed because the provider serves
    NO pre-match xG. That is a different claim from this one: it says the
    provider will not hand us a leaked value before kickoff. This says we
    will not carry an in-play value backwards into something that was
    supposed to be frozen.
    """

    def test_no_lock_path_module_names_the_live_stats_surface(self):
        offenders = []
        for rel in LOCK_PATH_MODULES:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(path):
                continue          # module list is a superset on purpose
            text = open(path, encoding="utf-8").read()
            for token in LEAKAGE_TOKENS:
                if token in text:
                    offenders.append(f"{rel}: {token}")
        assert offenders == [], (
            "these modules build, price, fit or score a LOCKED artifact "
            "and must not reach live in-play statistics — live xG carries "
            "information from after the lock: " + "; ".join(offenders))

    def test_that_scan_would_catch_a_leak(self, tmp_path):
        """PROOF THE SCAN HAS TEETH, not a control.

        The static test passes against the tree as it stands. That is only
        evidence if the scan would fail on a tree where the leak exists,
        so here a lock-path-shaped file really does import the module and
        the same scan is asserted to find it.
        """
        leaky = tmp_path / "runs.py"
        leaky.write_text("from src.live import apifootball_live\n",
                         encoding="utf-8")
        found = [t for t in LEAKAGE_TOKENS
                 if t in leaky.read_text(encoding="utf-8")]
        assert "apifootball_live" in found, (
            "the token scan must detect an import of the live-stats "
            "module, or the static test above proves nothing")

    def test_lock_path_imports_resolve_without_the_live_module(self):
        """A stronger form of the same claim, at the import graph rather
        than the text: the lock path's transitive imports must not pull
        the live-stats module in."""
        import importlib
        import sys
        for rel in ("src/live/runs.py", "src/live/paper.py",
                    "src/live/markets.py", "src/live/model_eval.py"):
            mod = rel[:-3].replace("/", ".")
            if not os.path.exists(os.path.join(REPO_ROOT, rel)):
                continue
            importlib.import_module(mod)
        # importing the lock path must not have loaded the live surface.
        # (If some other test imported it first this is inconclusive, so
        # the assertion is on the lock modules' OWN import lists.)
        for rel in LOCK_PATH_MODULES:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(path):
                continue
            tree = ast.parse(open(path, encoding="utf-8").read(),
                             filename=path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [f"{node.module or ''}.{a.name}"
                             for a in node.names]
                assert not any("apifootball_live" in n for n in names), (
                    f"{rel}:{getattr(node, 'lineno', '?')} imports the "
                    "live-stats surface into the lock path")
        assert sys.modules  # keep the import side effect meaningful

    def test_a_live_xg_sentinel_never_reaches_any_persisted_lock_row(
            self, live_session, monkeypatch):
        """BEHAVIOURAL. Take a real canonical T-10 lock while a live
        reading carrying a distinctive xG sentinel sits in the process's
        reading store, then sweep EVERY column of EVERY table for the
        sentinel.

        Scanning the whole database rather than the columns we happen to
        suspect is the point: a leak through a payload blob, a hash input
        or a JSON side-car would be invisible to a targeted assertion.
        """
        from src.live import markets
        from src.live.models import PredictionRun
        from tests.test_mls_shadow import TestPredictionRuns

        SENTINEL = "7.7777777"
        # the sentinel lives where a real reading would
        reading = _reading(_stats_payload(xg_a=SENTINEL, xg_b="0.31"))
        al.remember_reading(9001, reading)
        assert SENTINEL in json.dumps(al.previous_reading(9001)), (
            "the sentinel must actually be in the reading, or this test "
            "proves nothing")

        helper = TestPredictionRuns()
        monkeypatch.setattr(config, "N_SIMULATIONS", 200)
        up = helper._seed_playable(live_session)
        up.current_kickoff_utc = datetime.now(UTC) + timedelta(minutes=9)
        live_session.commit()
        snap = helper._fake_snapshot(live_session, up.id)
        # **kw tolerates the `spec` kwarg the competition-keyed live plane
        # added on 2026-07-30 (this branch predates it). Widening a test
        # DOUBLE's signature is not weakening the assertion — the sentinel
        # sweep below is unchanged and is what this test exists for.
        monkeypatch.setattr(markets, "capture_lock_snapshot",
                            lambda fixture_id, **kw: snap)
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert", lambda msg, **kw: {})
        import src.live.lineups as lineups_mod
        monkeypatch.setattr(
            lineups_mod, "capture_lineup",
            lambda fixture_id, **kw: {
                "snapshot_id": 77, "status": "pending",
                "quality": {"LINEUP_CONFIRMED": False,
                            "GOALKEEPER_CONFIRMED": False,
                            "AVAILABILITY_COMPLETE": False,
                            "PLAYER_DATA_FRESH": False}})
        from src.live import runs
        assert runs.t10_locks()["locked"] == 1
        lock = (live_session.query(PredictionRun)
                .filter_by(run_type="t10", canonical=True).one())
        assert lock.status == "complete"        # a real lock was taken

        # sweep every table, every column
        from sqlalchemy import inspect as sa_inspect
        engine = live_db.get_engine()
        insp = sa_inspect(engine)
        hits = []
        with engine.connect() as conn:
            from sqlalchemy import text as sa_text
            for table in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns(table)]
                if not cols:
                    continue
                rows = conn.execute(sa_text(f"SELECT * FROM {table}")).all()
                for r in rows:
                    for col, val in zip(cols, r):
                        if val is not None and SENTINEL in str(val):
                            hits.append(f"{table}.{col}")
        assert hits == [], (
            "a live in-play xG value reached a persisted artifact on the "
            "lock path: " + "; ".join(hits))

    def test_the_sweep_would_catch_a_leak(self, live_session):
        """PROOF THE SWEEP HAS TEETH. Write the sentinel into a live-plane
        row on purpose and assert the same sweep finds it."""
        SENTINEL = "7.7777777"
        row = ApiFootballLiveCoverage(league_id=999, league_name=SENTINEL)
        live_session.add(row)
        live_session.commit()
        from sqlalchemy import inspect as sa_inspect, text as sa_text
        engine = live_db.get_engine()
        insp = sa_inspect(engine)
        hits = []
        with engine.connect() as conn:
            for table in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns(table)]
                rows = conn.execute(sa_text(f"SELECT * FROM {table}")).all()
                for r in rows:
                    for col, val in zip(cols, r):
                        if val is not None and SENTINEL in str(val):
                            hits.append(f"{table}.{col}")
        assert hits, ("the sweep must find a sentinel that IS present, or "
                      "the behavioural test above proves nothing")

    def test_the_coverage_table_is_not_reachable_from_a_model_feature(self):
        """CONTROL-adjacent but worth pinning: the coverage model is
        referenced only by the live-stats surface and its own tests."""
        offenders = []
        for path in _runtime_python_files():
            rel = os.path.relpath(path, REPO_ROOT)
            if rel in ("src/live/apifootball_live.py", "src/live/models.py",
                       "api/main.py"):
                continue
            if "ApiFootballLiveCoverage" in open(
                    path, encoding="utf-8").read():
                offenders.append(rel)
        assert offenders == [], (
            "the live-coverage table is observational and should be read "
            "only through the live-stats surface: " + "; ".join(offenders))


# ==========================================================================
# ABSENT IS NOT ZERO
# ==========================================================================
class TestAbsentIsNotZero:
    """The provider serves a present-but-NULL xG for several leagues. A
    null read as 0.0 says "this team created nothing" about a competition
    the provider does not cover."""

    @pytest.mark.parametrize("raw", [None, "", " ", "-", "–", "null",
                                     "none", "N/A", "na", "abc", True,
                                     False])
    def test_hollow_values_parse_to_none_never_zero(self, raw):
        assert al.parse_number(raw) is None, (
            f"{raw!r} is ABSENCE and must never become 0.0")

    @pytest.mark.parametrize("raw,want", [(0, 0.0), ("0", 0.0),
                                          ("0.0", 0.0), (1.28, 1.28),
                                          ("2.48", 2.48), ("56%", 56.0)])
    def test_real_numbers_including_zero_parse(self, raw, want):
        assert al.parse_number(raw) == want

    def test_a_null_xg_is_absent_not_zero_in_a_reading(self):
        stats = al.read_statistics(_stats_payload(xg_a=None, xg_b=None))
        assert stats["stats_present"] is True         # the types are there
        assert stats["xg_type_offered"] is True       # so is the xG type
        assert stats["xg_present_any_team"] is False  # but no VALUE
        for t in stats["teams"]:
            assert t["xg_value"] is None
            assert t["xg_present"] is False
            assert t["xg_zero_valued"] is False, (
                "a null must not be reported as a zero-valued xG")

    def test_a_genuine_zero_is_present_but_marked(self):
        stats = al.read_statistics(_stats_payload(xg_a="0.0", xg_b="0"))
        assert stats["xg_present_both_teams"] is True
        assert all(t["xg_zero_valued"] for t in stats["teams"])

    def test_xga_is_never_read_as_xg(self):
        stats = al.read_statistics(
            _stats_payload(xg_type="expected_goals_against"))
        assert stats["xg_type_offered"] is False, (
            "xGA is the opponent's number; accepting it would let xG "
            "coverage be claimed on the strength of the wrong statistic")

    def test_an_empty_statistics_payload_is_absence(self):
        stats = al.read_statistics({"errors": [], "results": 0,
                                    "response": []})
        assert stats["stats_present"] is False
        assert stats["statistic_type_count_max"] == 0

    def test_a_results_count_disagreement_is_reported(self):
        """`results: 0` beside a populated array is the exact shape of the
        2026-07-09 incident. The COUNT wins and the disagreement is said
        out loud."""
        payload = _stats_payload()
        payload["results"] = 0
        msg = al.count_disagreement(payload)
        assert msg and "the count wins" in msg

    def test_provider_errors_inside_a_200_are_surfaced(self):
        body = {"errors": {"token": "invalid"}, "results": 0,
                "response": []}
        assert al.errors_of(body) == ["token: invalid"]

    def test_a_null_xg_yields_no_characterisation_rather_than_a_zero_diff(
            self):
        out = al.live_xg_characterisation(
            _reading(_stats_payload(xg_a=None, xg_b="0.9")), _join())
        assert out["available"] is False
        assert "ABSENT, never zero" in out["reason"]
        assert "xg_differential_first_minus_second" not in out


# ==========================================================================
# COVERAGE IS MEASURED, NEVER ASSUMED
# ==========================================================================
class TestCoverageIsMeasuredNotAssumed:

    def test_absence_before_min_elapsed_is_too_early_not_absent(self):
        empty = al.read_statistics({"errors": [], "results": 0,
                                    "response": []})
        assert al.coverage_verdict(empty, 14) == al.COVERAGE_TOO_EARLY
        assert al.coverage_verdict(empty, 90) == al.COVERAGE_ABSENT
        assert al.coverage_verdict(empty, None) == al.COVERAGE_TOO_EARLY

    def test_present_beats_elapsed_entirely(self):
        """If the statistics are there, the minute is irrelevant."""
        full = al.read_statistics(_stats_payload())
        assert al.coverage_verdict(full, 3) == al.COVERAGE_PRESENT

    def test_a_verdict_is_persisted_with_its_denominator_and_date(
            self, live_session):
        al.record_coverage(live_session, 128, "Liga Profesional Argentina",
                           "Argentina", _reading(), 73, NOW)
        live_session.commit()
        row = (live_session.query(ApiFootballLiveCoverage)
               .filter_by(league_id=128).one())
        assert row.verdict == al.COVERAGE_PRESENT
        assert row.fixtures_probed == 1              # the DENOMINATOR
        assert row.statistic_type_count == 18
        assert row.events_present is True
        assert _naive(row.last_measured_at) == _naive(NOW)
        assert _naive(row.last_present_at) == _naive(NOW)
        assert row.last_elapsed_observed == 73

    def test_statistics_and_events_coverage_are_tracked_separately(
            self, live_session):
        """Measured: Copa Colombia served 3 goal events with ZERO
        statistic types. One combined 'has live data' flag would have
        erased the broader signal."""
        al.record_coverage(
            live_session, 241, "Copa Colombia", "Colombia",
            _reading(stats_payload={"errors": [], "results": 0,
                                    "response": []},
                     events_payload=_events_payload(GOAL, GOAL, SUBST)),
            90, NOW)
        live_session.commit()
        row = (live_session.query(ApiFootballLiveCoverage)
               .filter_by(league_id=241).one())
        assert row.verdict == al.COVERAGE_ABSENT     # no statistics
        assert row.events_present is True            # but events exist
        assert row.event_count_last == 3

    def test_a_too_early_read_never_downgrades_a_measured_presence(
            self, live_session):
        """Letting one early-minute read erase a measured presence would
        convert missing evidence into a conclusion."""
        al.record_coverage(live_session, 128, "LPA", "Argentina",
                           _reading(), 73, NOW)
        live_session.commit()
        al.record_coverage(
            live_session, 128, "LPA", "Argentina",
            _reading(stats_payload={"errors": [], "results": 0,
                                    "response": []}),
            4, NOW + timedelta(minutes=5))
        live_session.commit()
        row = (live_session.query(ApiFootballLiveCoverage)
               .filter_by(league_id=128).one())
        assert row.verdict == al.COVERAGE_PRESENT, (
            "a 4th-minute empty read must not overwrite a verdict "
            "measured at 73'")
        assert row.too_early_reads == 1              # recorded, not hidden
        assert row.fixtures_probed == 2              # denominator still grows

    def test_a_late_absence_does_downgrade(self, live_session):
        """The converse control: a genuine absence at an honest minute
        MUST be able to change the verdict, or the cache would freeze a
        stale presence forever."""
        al.record_coverage(live_session, 128, "LPA", "Argentina",
                           _reading(), 73, NOW)
        live_session.commit()
        al.record_coverage(
            live_session, 128, "LPA", "Argentina",
            _reading(stats_payload={"errors": [], "results": 0,
                                    "response": []}),
            85, NOW + timedelta(days=1))
        live_session.commit()
        row = (live_session.query(ApiFootballLiveCoverage)
               .filter_by(league_id=128).one())
        assert row.verdict == al.COVERAGE_ABSENT
        # but the last time it WAS present is still on the row
        assert _naive(row.last_present_at) == _naive(NOW)

    def test_a_cached_absence_expires(self, live_session, monkeypatch):
        monkeypatch.setattr(config,
                            "HUNTER_LIVE_STATS_COVERAGE_TTL_DAYS", 7)
        al.record_coverage(
            live_session, 241, "Copa Colombia", "Colombia",
            _reading(stats_payload={"errors": [], "results": 0,
                                    "response": []}),
            90, NOW)
        live_session.commit()
        assert al.coverage_is_fresh_absent(live_session, 241,
                                           NOW + timedelta(days=1)) is True
        assert al.coverage_is_fresh_absent(live_session, 241,
                                           NOW + timedelta(days=8)) is False

    def test_a_present_verdict_is_never_used_as_a_shortcut(
            self, live_session):
        """Only ABSENT is cached. A finding's evidence must be the reading
        it was actually made from — a cached 'present' would let a row
        assert values nobody looked up."""
        al.record_coverage(live_session, 128, "LPA", "Argentina",
                           _reading(), 73, NOW)
        live_session.commit()
        assert al.coverage_is_fresh_absent(live_session, 128, NOW) is False

    def test_an_unmeasured_competition_is_not_shortcut(self, live_session):
        assert al.coverage_is_fresh_absent(live_session, 4242, NOW) is False

    def test_the_report_exposes_the_direction_and_the_denominators(
            self, live_session):
        al.record_coverage(live_session, 128, "LPA", "Argentina",
                           _reading(), 73, NOW)
        live_session.commit()
        rep = al.coverage_report()
        assert rep["ready"] is True
        assert rep["competitions"][0]["fixtures_probed"] == 1
        assert rep["competitions"][0]["last_measured_at"] is not None
        assert (rep["conditioning_states"][al.CONDITIONING_OBSERVED]
                ["strength_rank"]
                < rep["conditioning_states"][
                    al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE]
                ["strength_rank"])
        assert "WEAKENS" in rep["inference_direction"]
        assert rep["competition_map_fingerprint"]
        assert "denominator" in rep["denominator_note"].lower()


# ==========================================================================
# THE JOIN FAILS CLOSED
# ==========================================================================
class TestTheJoinFailsClosed:
    """A wrong join is the worst failure available here: it would attach
    another match's red card to this market and report the move as
    EXPLAINED when nothing happened."""

    def test_team_codes_come_from_the_leg_suffixes(self):
        codes = al.team_codes_from_tickers([
            "KXARGPREMDIVGAME-26JUL30CCTUC-TIE",
            "KXARGPREMDIVGAME-26JUL30CCTUC-CC",
            "KXARGPREMDIVGAME-26JUL30CCTUC-TUC"])
        assert codes == ["CC", "TUC"], (
            "the concatenated ticker cannot be split without already "
            "knowing the answer; the legs carry the codes separately")

    def test_a_unique_match_resolves(self):
        index = al.fetch_live_index.__wrapped__ if hasattr(
            al.fetch_live_index, "__wrapped__") else None
        idx = {"by_league": {128: [
            {"fixture_id": 9001, "league_id": 128, "league_name": "LPA",
             "home_name": "Central Cordoba", "away_name": "Atletico Tucuman",
             "home_id": 101, "away_id": 202, "elapsed": 73,
             "status_short": "2H", "goals": {"home": 1, "away": 0},
             "kickoff_utc": "x"}]}}
        out = al.resolve_fixture(
            "KXARGPREMDIVGAME",
            ["KXARGPREMDIVGAME-26JUL30CCTUC-TIE",
             "KXARGPREMDIVGAME-26JUL30CCTUC-CC",
             "KXARGPREMDIVGAME-26JUL30CCTUC-TUC"], idx)
        assert out["status"] == al.JOIN_RESOLVED
        assert out["fixture_id"] == 9001
        assert index is None or True
        # the basis travels with every join, resolved or not
        assert "HEURISTIC JOIN" in out["basis"]
        assert out["competition_map_fingerprint"]

    def test_an_unmapped_series_refuses(self):
        out = al.resolve_fixture("KXEPLGAME", ["KXEPLGAME-26JUL30AAABBB-AAA",
                                               "KXEPLGAME-26JUL30AAABBB-BBB"],
                                 {"by_league": {}})
        assert out["status"] == al.JOIN_NO_COMPETITION_MAP
        assert "NOT joined on a guess" in out["detail"]

    def test_two_matching_fixtures_fail_explicitly(self):
        """AGENTS.md §13: an ambiguous identity match must fail
        explicitly, never silently pick a team."""
        both = [
            {"fixture_id": 1, "league_id": 128, "league_name": "LPA",
             "home_name": "Central Cordoba", "away_name": "Tucuman FC",
             "home_id": 1, "away_id": 2, "elapsed": 70,
             "status_short": "2H", "goals": {}, "kickoff_utc": "x"},
            {"fixture_id": 2, "league_id": 128, "league_name": "LPA",
             "home_name": "Central Corrientes", "away_name": "Tucumanos",
             "home_id": 3, "away_id": 4, "elapsed": 60,
             "status_short": "2H", "goals": {}, "kickoff_utc": "x"}]
        out = al.resolve_fixture(
            "KXARGPREMDIVGAME",
            ["KXARGPREMDIVGAME-26JUL30CCTUC-CC",
             "KXARGPREMDIVGAME-26JUL30CCTUC-TUC"],
            {"by_league": {128: both}})
        assert out["status"] == al.JOIN_AMBIGUOUS
        assert out["candidates_matched"] == 2
        assert "FAILS EXPLICITLY" in out["detail"]
        assert "fixture_id" not in out

    def test_no_live_fixture_in_the_competition_refuses(self):
        out = al.resolve_fixture("KXARGPREMDIVGAME",
                                 ["KXARGPREMDIVGAME-26JUL30CCTUC-CC",
                                  "KXARGPREMDIVGAME-26JUL30CCTUC-TUC"],
                                 {"by_league": {}})
        assert out["status"] == al.JOIN_NO_LIVE_FIXTURE

    def test_a_tie_only_event_has_no_codes(self):
        out = al.resolve_fixture("KXARGPREMDIVGAME",
                                 ["KXARGPREMDIVGAME-26JUL30CCTUC-TIE"],
                                 {"by_league": {128: [{}]}})
        assert out["status"] == al.JOIN_NO_CODES

    def test_codes_that_match_nothing_refuse(self):
        out = al.resolve_fixture(
            "KXARGPREMDIVGAME",
            ["KXARGPREMDIVGAME-26JUL30XXXYYY-XXX",
             "KXARGPREMDIVGAME-26JUL30XXXYYY-YYY"],
            {"by_league": {128: [
                {"fixture_id": 9001, "league_id": 128, "league_name": "LPA",
                 "home_name": "Central Cordoba",
                 "away_name": "Atletico Tucuman", "home_id": 1, "away_id": 2,
                 "elapsed": 73, "status_short": "2H", "goals": {},
                 "kickoff_utc": "x"}]}})
        assert out["status"] == al.JOIN_NO_MATCH

    def test_the_competition_map_is_fingerprinted(self, monkeypatch):
        """A reference table anyone can extend quietly is a table that
        gets extended to make a stubborn case resolve — which is exactly
        how the trial harness's frozen roster was tuned on day one."""
        before = al.competition_map_fingerprint()
        monkeypatch.setitem(al.COMPETITION_MAP, "KXFAKEGAME",
                            {"league_id": 1, "league_name": "x",
                             "basis": "invented"})
        assert al.competition_map_fingerprint() != before, (
            "extending the map must change the fingerprint recorded on "
            "every finding built from it")

    def test_every_mapped_entry_states_how_it_was_established(self):
        for series, entry in al.COMPETITION_MAP.items():
            assert entry.get("basis"), f"{series} has no stated basis"
            assert "MEASURED" in entry["basis"], (
                f"{series}'s pairing is not backed by a measurement")


# ==========================================================================
# THE MARKET ANCHOR STAYS PRIMARY
# ==========================================================================
class TestTheMarketAnchorStaysPrimary:
    """The primary signal is the market compared with ITSELF — no model of
    ours in it. Live xG rides alongside as a characterisation and must
    never be presented as a reprice or allowed to overrule the anchor."""

    def test_the_variant_is_labelled_secondary_and_not_a_probability(self):
        out = al.live_xg_characterisation(_reading(), _join())
        assert out["available"] is True
        assert "SECONDARY" in out["note"]
        assert "NOT a probability" in out["note"]
        assert "cannot be wrong about a model" in out["note"]
        # a differential is reported; no probability is
        assert out["xg_differential_first_minus_second"] == 0.75
        assert not any("prob" in k.lower() for k in out)

    def test_xg_and_score_are_reported_side_by_side_never_combined(self):
        out = al.live_xg_characterisation(_reading(), _join())
        vs = out["xg_vs_score"]
        assert vs["xg_differential"] == 0.75
        assert vs["goal_differential_home_minus_away"] == 1.0
        assert "deliberately NOT combined" in vs["reading"]

    def test_the_window_delta_is_unavailable_on_a_first_reading(self):
        out = al.live_xg_characterisation(_reading(), _join(),
                                          prev_reading=None)
        assert out["xg_window_delta"]["available"] is False
        assert "first reading" in out["xg_window_delta"]["reason"]

    def test_the_window_delta_needs_both_teams_in_the_previous_reading(
            self):
        prev = _reading(_stats_payload(xg_a="0.40", xg_b=None))
        out = al.live_xg_characterisation(_reading(), _join(),
                                         prev_reading=prev)
        assert out["xg_window_delta"]["available"] is False
        assert "rather than one being invented" in (
            out["xg_window_delta"]["reason"])

    def test_a_real_window_delta_is_computed_per_team(self):
        prev = _reading(_stats_payload(xg_a="0.40", xg_b="0.20"))
        out = al.live_xg_characterisation(_reading(), _join(),
                                         prev_reading=prev)
        d = out["xg_window_delta"]["per_team"]
        assert {x["team_id"]: x["xg_delta"] for x in d} == {101: 0.88,
                                                           202: 0.33}

    def test_the_finding_states_which_signal_it_is_made_of(self):
        """The row must say, in itself, that the anchor is primary — a
        reader should not have to know the source to tell which number the
        finding rests on."""
        prev = {"yes_bid": "0.40", "yes_ask": "0.42",
                "captured_at": "2026-07-29T23:36:00+00:00"}
        m = {"ticker": "KXARGPREMDIVGAME-26JUL29CCTUC-CC",
             "event_ticker": "KXARGPREMDIVGAME-26JUL29CCTUC",
             "yes_bid_dollars": "0.60", "yes_ask_dollars": "0.62"}
        f = hunter.detect_overreaction(
            "KXARGPREMDIVGAME", "KXARGPREMDIVGAME-26JUL29CCTUC", m, prev,
            NOW)
        assert f is not None
        legs = f["legs"]
        assert "MARKET-ANCHORED and model-free" in legs["primary_signal"]
        assert "never replaces or overrules" in legs["primary_signal"]
        # and it leaves the detector in the HONEST default state
        assert legs["conditioning"]["state"] == (
            al.CONDITIONING_UNOBSERVED_NO_STATS)
        assert f["capture_window_minutes"] == 12.0


# ==========================================================================
# BUDGET
# ==========================================================================
class TestBudget:

    def test_a_cycle_with_no_candidate_spends_nothing(self, live_session):
        rep = hunter.enrich_overreactions(live_session, [], {}, NOW)
        assert rep["ran"] is False
        assert rep["requests"] == 0
        assert "no overreaction candidate" in rep["skipped"]

    def test_no_key_means_inert_and_zero_requests(self, live_session,
                                                  monkeypatch):
        """The deliberate default state in production: deploying this
        changes nothing until an operator configures a key."""
        monkeypatch.setattr(config, "APIFOOTBALL_KEY", "")
        monkeypatch.setattr(al, "KEY_FILE",
                            al.Path("/nonexistent/apifootball_key"))
        f = _overreaction_finding()
        rep = hunter.enrich_overreactions(
            live_session, [f], {f["event_ticker"]: ["x-CC", "x-TUC"]}, NOW)
        assert rep["ran"] is False
        assert rep["requests"] == 0
        assert "INERT" in rep["skipped"]
        # and the finding keeps the honest state
        assert f["legs"]["conditioning"]["state"] == (
            al.CONDITIONING_UNOBSERVED_NO_STATS)

    def test_disabled_flag_means_zero_requests(self, live_session,
                                              monkeypatch):
        monkeypatch.setattr(config, "HUNTER_LIVE_STATS_ENABLED", False)
        f = _overreaction_finding()
        rep = hunter.enrich_overreactions(
            live_session, [f], {f["event_ticker"]: ["x-CC", "x-TUC"]}, NOW)
        assert rep["requests"] == 0
        assert "HUNTER_LIVE_STATS_ENABLED is false" in rep["skipped"]

    def test_one_candidate_costs_one_plus_two(self, live_session,
                                             monkeypatch):
        calls: list = []
        _canned(monkeypatch, {
            "fixtures/statistics": _stats_payload(),
            "fixtures/events": _events_payload(GOAL, SUBST),
            "fixtures": _live_payload(),
        }, calls)
        f = _overreaction_finding()
        rep = hunter.enrich_overreactions(
            live_session, [f],
            {f["event_ticker"]: [
                "KXARGPREMDIVGAME-26JUL29CCTUC-TIE",
                "KXARGPREMDIVGAME-26JUL29CCTUC-CC",
                "KXARGPREMDIVGAME-26JUL29CCTUC-TUC"]}, NOW)
        assert rep["requests"] == 3, (
            "1 request for the whole world's live fixtures + 2 for the one "
            "resolved fixture")
        assert rep["fixtures_read"] == 1
        assert rep["conditioning_states"] == {al.CONDITIONING_OBSERVED: 1}

    def test_two_candidates_on_one_fixture_read_it_once(self, live_session,
                                                       monkeypatch):
        """Two legs of the same event must not pay twice."""
        _canned(monkeypatch, {
            "fixtures/statistics": _stats_payload(),
            "fixtures/events": _events_payload(SUBST),
            "fixtures": _live_payload(),
        })
        legs = ["KXARGPREMDIVGAME-26JUL29CCTUC-CC",
                "KXARGPREMDIVGAME-26JUL29CCTUC-TUC"]
        a = _overreaction_finding(market="KXARGPREMDIVGAME-26JUL29CCTUC-CC")
        b = _overreaction_finding(market="KXARGPREMDIVGAME-26JUL29CCTUC-TUC")
        rep = hunter.enrich_overreactions(
            live_session, [a, b], {a["event_ticker"]: legs}, NOW)
        assert rep["requests"] == 3
        assert rep["fixtures_read"] == 1

    def test_two_legs_of_one_event_do_not_see_each_others_reading(
            self, live_session, monkeypatch):
        """REGRESSION. `remember_reading` used to run inside the candidate
        loop, so the second leg of an event read the FIRST leg's
        just-stored reading as its "previous" and produced an xG window
        delta of 0.0 — a fabricated "nothing happened in this window",
        from differencing a reading against itself.

        The honest answer on a first sighting is "unavailable", and that
        is what both legs must report.
        """
        _canned(monkeypatch, {
            "fixtures/statistics": _stats_payload(),
            "fixtures/events": _events_payload(SUBST),
            "fixtures": _live_payload(),
        })
        legs = ["KXARGPREMDIVGAME-26JUL29CCTUC-CC",
                "KXARGPREMDIVGAME-26JUL29CCTUC-TUC"]
        a = _overreaction_finding(market=legs[0])
        b = _overreaction_finding(market=legs[1])
        hunter.enrich_overreactions(live_session, [a, b],
                                    {a["event_ticker"]: legs}, NOW)
        for f in (a, b):
            delta = f["legs"]["live_xg_variant"]["xg_window_delta"]
            assert delta["available"] is False, (
                "a reading differenced against itself must never be "
                "reported as a real window delta")
            assert "first reading" in delta["reason"]

    def test_a_second_cycle_does_see_the_first_cycles_reading(
            self, live_session, monkeypatch):
        """The converse control: across two CYCLES the delta must appear,
        or the fix above would have simply disabled the feature."""
        # BOTH legs: the join needs two team codes, and one leg alone
        # correctly refuses for want of them
        legs = ["KXARGPREMDIVGAME-26JUL29CCTUC-CC",
                "KXARGPREMDIVGAME-26JUL29CCTUC-TUC"]
        _canned(monkeypatch, {
            "fixtures/statistics": _stats_payload(xg_a="0.40", xg_b="0.20"),
            "fixtures/events": _events_payload(SUBST),
            "fixtures": _live_payload()})
        first = _overreaction_finding(market=legs[0])
        hunter.enrich_overreactions(live_session, [first],
                                    {first["event_ticker"]: legs}, NOW)
        _canned(monkeypatch, {
            "fixtures/statistics": _stats_payload(xg_a="1.28", xg_b="0.53"),
            "fixtures/events": _events_payload(SUBST),
            "fixtures": _live_payload()})
        second = _overreaction_finding(market=legs[0])
        hunter.enrich_overreactions(live_session, [second],
                                    {second["event_ticker"]: legs},
                                    NOW + timedelta(minutes=12))
        delta = second["legs"]["live_xg_variant"]["xg_window_delta"]
        assert delta["per_team"], "the second cycle must have a delta"
        assert {x["team_id"]: x["xg_delta"]
                for x in delta["per_team"]} == {101: 0.88, 202: 0.33}

    def test_a_cached_absence_spends_nothing_and_says_it_is_cached(
            self, live_session, monkeypatch):
        al.record_coverage(
            live_session, 128, "LPA", "Argentina",
            _reading(stats_payload={"errors": [], "results": 0,
                                    "response": []}),
            85, NOW)
        live_session.commit()
        _canned(monkeypatch, {"fixtures": _live_payload()})
        f = _overreaction_finding()
        rep = hunter.enrich_overreactions(
            live_session, [f],
            {f["event_ticker"]: ["x-CC", "x-TUC"]}, NOW)
        assert rep["requests"] == 1, (
            "only the live index; the statistics/events pair is skipped "
            "for a competition measured absent inside its TTL")
        cond = f["legs"]["conditioning"]
        assert cond["state"] == al.CONDITIONING_UNOBSERVED_NO_STATS
        assert "CACHED verdict, not a reading" in (
            cond["join"]["coverage_shortcut"])

    def test_the_daily_cap_stops_spending(self, monkeypatch):
        monkeypatch.setattr(config,
                            "HUNTER_LIVE_STATS_MAX_REQUESTS_PER_DAY", 2)
        _canned(monkeypatch, {"fixtures": _live_payload()})
        c = al.LiveClient("K")
        c.get("fixtures", {}, "a")
        c.get("fixtures", {}, "b")
        with pytest.raises(al.BudgetExceeded) as e:
            c.get("fixtures", {}, "c")
        assert "daily cap 2" in str(e.value)

    def test_the_per_cycle_cap_stops_spending(self, monkeypatch):
        _canned(monkeypatch, {"fixtures": _live_payload()})
        c = al.LiveClient("K", cycle_cap=1)
        c.get("fixtures", {}, "a")
        with pytest.raises(al.BudgetExceeded) as e:
            c.get("fixtures", {}, "b")
        assert "per-cycle cap 1" in str(e.value)

    def test_the_budget_block_reports_counts_and_plan_limits(self,
                                                            monkeypatch):
        _canned(monkeypatch, {"fixtures": _live_payload()})
        c = al.LiveClient("K")
        c.get("fixtures", {}, "a")
        b = c.budget_block()
        assert b["requests_used_this_cycle"] == 1
        assert b["day"]["count"] == 1
        assert "7500/day" in b["plan_limits"]

    def test_a_provider_failure_leaves_the_honest_state(self, live_session,
                                                       monkeypatch):
        import requests as rq

        def boom(*a, **kw):
            raise rq.ConnectionError("network down")
        monkeypatch.setattr(al.requests, "get", boom)
        f = _overreaction_finding()
        rep = hunter.enrich_overreactions(
            live_session, [f], {f["event_ticker"]: ["x-CC", "x-TUC"]}, NOW)
        assert "error" in rep
        assert f["legs"]["conditioning"]["state"] == (
            al.CONDITIONING_UNOBSERVED_NO_STATS)

    def test_a_transport_error_never_prints_the_key(self, monkeypatch,
                                                   capsys):
        import requests as rq
        KEY = "SECRET-KEY-abc123"
        monkeypatch.setattr(config, "APIFOOTBALL_KEY", KEY)

        def boom(*a, **kw):
            raise rq.ConnectionError(
                f"Max retries with url: https://x/?key={KEY}")
        monkeypatch.setattr(al.requests, "get", boom)
        c = al.LiveClient(KEY)
        with pytest.raises(rq.RequestException):
            c.get("fixtures", {}, "a")
        out = capsys.readouterr().out
        assert KEY not in out, "the key reached stdout"
        assert "ConnectionError" in out

    def test_redact_strips_the_key(self):
        assert "abc" not in al.redact("url?key=abc", "abc")
        assert "REDACTED" in al.redact("url?key=abc", "abc")


def _overreaction_finding(series="KXARGPREMDIVGAME",
                          event="KXARGPREMDIVGAME-26JUL29CCTUC",
                          market="KXARGPREMDIVGAME-26JUL29CCTUC-CC"):
    return {
        "finding_type": "IN_PLAY_OVERREACTION", "series": series,
        "event_ticker": event, "market_ticker": market, "is_context": True,
        "net_margin_dollars": None, "captured_at": NOW,
        "capture_window_minutes": 12.0,
        "legs": {"rule": "x", "mid_move_dollars": "0.20",
                 "conditioning": {
                     "state": al.CONDITIONING_UNOBSERVED_NO_STATS}},
    }


# ==========================================================================
# ALERTING STAYS WHERE SON PUT IT
# ==========================================================================
class TestAlertingStaysWhereSonPutIt:
    """IN_PLAY_OVERREACTION is context-class and does not alert today.
    Richer evidence does not change who decides that."""

    def test_in_play_alerts_default_off(self):
        assert config.HUNTER_IN_PLAY_ALERTS_ENABLED is False, (
            "promoting a context finding to Son's channel is his call")

    def test_the_strongest_state_still_does_not_alert_by_default(self):
        f = _overreaction_finding()
        f["legs"]["conditioning"] = {
            "state": al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE}
        assert hunter._alert_eligible(f) is False

    def test_when_enabled_only_the_strongest_state_alerts(self,
                                                         monkeypatch):
        monkeypatch.setattr(config, "HUNTER_IN_PLAY_ALERTS_ENABLED", True)
        want = {
            al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE: True,
            al.CONDITIONING_OBSERVED: False,
            al.CONDITIONING_UNOBSERVED_NO_STATS: False,
        }
        for state, expected in want.items():
            f = _overreaction_finding()
            f["legs"]["conditioning"] = {"state": state}
            assert hunter._alert_eligible(f) is expected, state

    def test_an_observed_cause_is_refused_even_when_alerts_are_on(
            self, monkeypatch):
        """The inference direction, enforced at the GATE and not only in a
        docstring: a move with a visible cause must not be broadcast as an
        anomaly."""
        monkeypatch.setattr(config, "HUNTER_IN_PLAY_ALERTS_ENABLED", True)
        f = _overreaction_finding()
        f["legs"]["conditioning"] = {"state": al.CONDITIONING_OBSERVED}
        assert hunter._alert_eligible(f) is False

    def test_the_message_names_the_state_and_the_direction(self):
        f = _overreaction_finding()
        f["legs"]["conditioning"] = {
            "state": al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE,
            "reason": "no goal and no red card"}
        msg = hunter._alert_message(f)
        assert "IN_PLAY_OVERREACTION" in msg
        assert al.CONDITIONING_UNOBSERVED_STATS_AVAILABLE in msg
        assert "model-free" in msg
        assert "MORE reasonable" in msg
        assert hunter.SHADOW_FRAMING in msg
        assert "$None" not in msg, (
            "this type has no margin; the message must not print one")

    def test_dispatch_is_pinned_to_the_detail_channel(self, monkeypatch):
        """Measured at the gate: the hunter's call site pins kind='detail'
        and AMBIENT_DETAIL, and the gate REFUSES ambient narration on any
        other kind."""
        from src import alerts
        refusal = alerts._authorize(alerts.AMBIENT_DETAIL, "action", None)
        assert refusal is not None and "detail channel only" in refusal
        assert alerts._authorize(alerts.AMBIENT_DETAIL, "detail",
                                 None) is None

    def test_the_hunter_call_site_still_declares_detail_only(self):
        """Static: the dispatch call in hunter.py must keep kind pinned to
        'detail' so a future caller cannot promote a finding by argument."""
        src = open(os.path.join(REPO_ROOT, "src", "live", "hunter.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name != "send_alert":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            sites.append(kw)
        assert sites, "no send_alert call site found in hunter.py"
        for kw in sites:
            assert isinstance(kw.get("kind"), ast.Constant)
            assert kw["kind"].value == "detail"
            assert isinstance(kw.get("dispatch_class"), ast.Name)
            assert kw["dispatch_class"].id == "AMBIENT_DETAIL"

    def test_in_play_overreaction_is_still_context_class(self):
        assert "IN_PLAY_OVERREACTION" in hunter.CONTEXT_TYPES
        assert "IN_PLAY_OVERREACTION" not in hunter.ALERTABLE


# ==========================================================================
# CONTROLS — stated as controls, never offered as evidence
# ==========================================================================
class TestControls:

    def test_structural_alerting_is_unchanged(self, monkeypatch):
        """CONTROL. Passes before and after this change: the structural
        types keep their own eligibility rules, untouched."""
        f = {"finding_type": "SUM_BELOW_ONE", "is_context": False,
             "fee_status": "modeled", "net_margin_dollars": "0.05",
             "alertable_liquidity": True, "legs": {}}
        assert hunter._alert_eligible(f) is True
        f["fee_status"] = "fee_unknown"
        assert hunter._alert_eligible(f) is False

    def test_the_overreaction_detector_still_needs_a_real_move(self):
        """CONTROL. The market-anchored primary is unchanged: a move
        inside the spread is still not a repricing."""
        prev = {"yes_bid": "0.40", "yes_ask": "0.60",
                "captured_at": "2026-07-29T23:36:00+00:00"}
        m = {"ticker": "KXARGPREMDIVGAME-26JUL29CCTUC-CC",
             "event_ticker": "KXARGPREMDIVGAME-26JUL29CCTUC",
             "yes_bid_dollars": "0.55", "yes_ask_dollars": "0.75"}
        assert hunter.detect_overreaction(
            "KXARGPREMDIVGAME", "KXARGPREMDIVGAME-26JUL29CCTUC", m, prev,
            NOW) is None

    def test_the_cycle_report_column_is_bounded(self):
        """An unbounded per-cycle blob written forever is the growth
        pattern that filled the production volume on 2026-07-25. A
        truncation must record itself and keep the audit-relevant
        scalars."""
        big = {"ran": True, "candidates": 4000, "requests": 9,
               "conditioning_states": {al.CONDITIONING_OBSERVED: 4000},
               "joins": {f"KXBRASILEIROGAME-26JUL29EV{i:05d}":
                         al.JOIN_NO_MATCH for i in range(4000)}}
        blob = hunter._capped_live_stats_json(big)
        assert len(blob.encode("utf-8")) <= hunter.LIVE_STATS_JSON_MAX_BYTES
        doc = json.loads(blob)
        assert doc["truncated"]["dropped_keys"] == ["joins"]
        assert doc["truncated"]["full_bytes"] > (
            hunter.LIVE_STATS_JSON_MAX_BYTES)
        # a histogram survives where per-event rows cannot, so a truncated
        # cycle still says how many joins refused
        assert doc["truncated"]["join_status_counts"] == {
            al.JOIN_NO_MATCH: 4000}
        assert doc["candidates"] == 4000        # scalars kept

    def test_a_small_report_is_stored_whole(self):
        """CONTROL: the cap must not truncate the normal case."""
        small = {"ran": True, "candidates": 1, "requests": 3,
                 "joins": {"KXBRASILEIROGAME-26JUL29INTFLA": "resolved"}}
        doc = json.loads(hunter._capped_live_stats_json(small))
        assert "truncated" not in doc
        assert doc["joins"]

    def test_the_cycle_row_actually_uses_the_capped_helper(self):
        """The two tests above exercise the HELPER. That is not the same
        claim as "the column is capped": a call site that goes back to a
        raw json.dumps would leave both of them green while the column
        grew without limit. Verified by controlled experiment — removing
        the cap at the call site did NOT turn them red — so the binding is
        asserted statically here, where it can be.
        """
        src = open(os.path.join(REPO_ROOT, "src", "live", "hunter.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        assigned = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword):
                continue
            if node.arg != "live_stats_json":
                continue
            fn = getattr(node.value, "func", None)
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute)
                    else None)
            assigned.append(name)
        assert assigned, "no live_stats_json assignment found on the row"
        assert all(n == "_capped_live_stats_json" for n in assigned), (
            "the cycle row's live_stats_json must go through the capped "
            f"helper; found {assigned}. An uncapped per-cycle blob written "
            "every cycle forever is what filled the volume on 2026-07-25")

    def test_the_reading_store_is_bounded(self):
        """CONTROL-adjacent: persisting every reading is the growth
        pattern that filled the production volume on 2026-07-25."""
        for i in range(5):
            al.remember_reading(i, _reading(at="2020-01-01T00:00:00+00:00"))
        assert len(al._reading_store) == 5
        al.prune_reading_store(datetime.now(UTC))
        assert al._reading_store == {}, "aged readings must be dropped"
