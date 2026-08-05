"""The live analyser, repointed at the MLS live plane.

Every guard here was confirmed to FAIL against unmodified code before it
was counted as evidence; where a test passes either way it says so in its
own docstring rather than being presented as proof.

Controls are paired with claims on purpose: a test showing something is
refused sits next to one showing the same operation succeeding where it
should not be refused, in the same session and the same config.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from src import positions
from src.db import SessionLocal, TrackedPosition, init_db
from src.live import analyser
from src.live import db as live_db
from src.live.models import (Competition, Fixture, LiveBase, MarketContract,
                             MarketEvent, MarketQuote, PredictionContract,
                             PredictionRun, Team)

UTC = timezone.utc


def setup_module(m):
    init_db()


def _enforce_varchar_lengths(session, flush_context, instances):
    """PostgreSQL-grade VARCHAR enforcement on SQLite — the same guard
    tests/test_mls_shadow.py installs, for the same reason: SQLite ignores
    String(n) and production does not."""
    from sqlalchemy import String
    for obj in list(session.new) + list(session.dirty):
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        for col in table.columns:
            if not (isinstance(col.type, String) and col.type.length):
                continue
            val = getattr(obj, col.key, None)
            if isinstance(val, str) and len(val) > col.type.length:
                raise ValueError(
                    f"value too long for {table.name}.{col.name}: "
                    f"{len(val)} chars > String({col.type.length}) — "
                    f"PostgreSQL would raise StringDataRightTruncation. "
                    f"Value: {val!r}")


@pytest.fixture()
def live_session(tmp_path, monkeypatch):
    from tests import _livedb
    url, _livedb_done = _livedb.provision(tmp_path, monkeypatch)
    LiveBase.metadata.create_all(live_db.get_engine())
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _Session
    if _livedb.SIMULATE_VARCHAR:
        event.listen(_Session, "before_flush", _enforce_varchar_lengths)
    s = live_db.get_session()
    s.add(Competition(slug=config.COMPETITION, name="MLS", season=2026))
    s.commit()
    yield s
    if _livedb.SIMULATE_VARCHAR:
        event.remove(_Session, "before_flush", _enforce_varchar_lengths)
    s.close()
    _livedb_done()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


def _wipe_positions():
    with SessionLocal() as s:
        s.query(TrackedPosition).delete()
        s.commit()
    positions._state.clear()


def _fixture(s, status="in", event_id="401998877"):
    # team names are unique per competition, so each fixture in a test gets
    # its own pair rather than colliding on the constraint
    home = Team(competition_slug=config.COMPETITION,
                canonical_name=f"Austin FC {event_id}", abbrev="ATX")
    away = Team(competition_slug=config.COMPETITION,
                canonical_name=f"LA Galaxy {event_id}", abbrev="LAG")
    s.add_all([home, away])
    s.flush()
    f = Fixture(competition_slug=config.COMPETITION,
                espn_event_id=event_id,
                home_team_id=home.id, away_team_id=away.id,
                current_kickoff_utc=datetime.now(UTC) - timedelta(minutes=30),
                status=status)
    s.add(f)
    s.flush()
    return f


def _book(s, fixture, *, approved=True, yes_bid_c=40, yes_ask_c=42,
          no_bid_c=None, no_ask_c=None, model_p=0.30,
          ticker="KXMLSGAME-26JUL28ATXLAG-ATX"):
    ev = MarketEvent(competition_slug=config.COMPETITION,
                     kalshi_event_ticker=f"E-{fixture.id}-{approved}",
                     series="KXMLSGAME", fixture_id=fixture.id,
                     mapping_approved=approved)
    s.add(ev)
    s.flush()
    mc = MarketContract(market_event_id=ev.id, ticker=ticker,
                        side_label="Austin FC", outcome_key="home_win")
    s.add(mc)
    s.flush()
    s.add(MarketQuote(market_contract_id=mc.id,
                      captured_at=datetime.now(UTC),
                      yes_bid_c=yes_bid_c, yes_ask_c=yes_ask_c,
                      no_bid_c=no_bid_c, no_ask_c=no_ask_c))
    if model_p is not None:
        run = PredictionRun(id=f"run-{fixture.id}-{approved}",
                            fixture_id=fixture.id, status="complete",
                            run_type="scheduled",
                            captured_at=datetime.now(UTC))
        s.add(run)
        s.flush()
        s.add(PredictionContract(prediction_run_id=run.id,
                                 market_contract_id=mc.id,
                                 outcome_key="home_win",
                                 raw_probability=model_p))
    s.commit()
    return mc


# --- the stale-model guard -------------------------------------------------

class TestStaleModelVerdict:
    """FAILS without the fix: `_verdict` has no `model_stale` parameter on
    unmodified code, so every test in this class raises TypeError."""

    def test_stale_model_yields_no_hold_ev(self):
        v, hold, cash = positions._verdict(0.30, 0.40, 100, 50.0,
                                           model_stale=True)
        assert v == "STALE_MODEL"
        assert hold is None            # the whole point: it is not computable
        assert round(cash, 2) == 38.32  # ... but the book side still is

    def test_control_same_inputs_not_stale_still_exit(self):
        """The control. Identical numbers, `model_stale=False`: the verdict
        the analyser SHOULD still produce when the model is contemporaneous.
        Without this, 'STALE_MODEL fired' proves nothing — the inputs alone
        might have produced no EXIT anyway."""
        v, hold, cash = positions._verdict(0.30, 0.40, 100, 50.0,
                                           model_stale=False)
        assert v == "EXIT" and hold == 30.0 and round(cash, 2) == 38.32

    def test_stale_and_no_bid_reports_stale_with_null_cashout(self):
        v, hold, cash = positions._verdict(0.30, None, 100, 50.0,
                                           model_stale=True)
        assert v == "STALE_MODEL" and hold is None and cash is None


class TestInPlayGate:
    """FAILS without the fix: `evaluate_positions` has no `in_play`
    parameter on unmodified code (TypeError)."""

    def _pos(self):
        _wipe_positions()
        with SessionLocal() as s:
            s.add(TrackedPosition(match_id="401998877", market_id="M-1",
                                  market_title="Austin FC",
                                  entry_price=0.10, contracts=400,
                                  cost=42.0))
            s.commit()

    ROWS = {"M-1": {"model_probability": 0.02,
                    "market_yes_bid": 0.10,
                    "model_basis": analyser.BASIS_PREMATCH}}

    def test_prematch_probability_in_play_is_refused(self, monkeypatch):
        self._pos()
        sent = []
        monkeypatch.setattr(positions, "send_alert",
                            lambda m, **k: sent.append((m, k)))
        (item,) = positions.evaluate_positions(
            self.ROWS, "401998877", alert=True, in_play=True)
        assert item["verdict"] == "STALE_MODEL"
        assert item["hold_ev"] is None and item["model_stale"] is True
        assert item["cashout_now"] is not None   # book side survives
        assert sent == []                        # and nothing was pushed

    def test_control_same_row_before_kickoff_fires_normally(self, monkeypatch):
        """The control: identical row, identical position, same session —
        only `in_play` differs. Before kickoff the pre-match probability IS
        contemporaneous, so the EXIT must fire. This is what makes the
        refusal above attributable to the gate rather than to the data."""
        self._pos()
        sent = []
        monkeypatch.setattr(positions, "send_alert",
                            lambda m, **k: sent.append((m, k)))
        (item,) = positions.evaluate_positions(
            self.ROWS, "401998877", alert=True, in_play=False)
        assert item["verdict"] == "EXIT"
        assert item["hold_ev"] == 8.0
        assert len(sent) == 1 and "CASH-OUT" in sent[0][0]

    def test_control_live_basis_in_play_is_not_stale(self):
        """Second control: `in_play=True` alone must not refuse anything.
        A row whose model IS a live re-simulation (the WC26 path) prices
        normally even in play."""
        self._pos()
        rows = {"M-1": {"live_model_probability": 0.02,
                        "market_yes_bid": 0.10}}
        (item,) = positions.evaluate_positions(rows, "401998877",
                                               in_play=True)
        assert item["verdict"] == "EXIT" and item["model_stale"] is False


class TestAlertTitle:
    def test_mls_position_alert_is_not_labelled_wc26(self, monkeypatch):
        """Spec acceptance test 7. FAILS without the fix, though on the
        signature (`alert_title` does not exist on unmodified code) rather
        than on a wrong value — stated plainly rather than overclaimed."""
        _wipe_positions()
        with SessionLocal() as s:
            s.add(TrackedPosition(match_id="401998877", market_id="M-1",
                                  market_title="Austin FC",
                                  entry_price=0.10, contracts=400,
                                  cost=42.0))
            s.commit()
        seen = []
        monkeypatch.setattr(positions, "send_alert",
                            lambda m, **k: seen.append((m, k)))
        rows = {"M-1": {"live_model_probability": 0.02,
                        "market_yes_bid": 0.10}}
        positions.evaluate_positions(rows, "401998877", alert=True,
                                     alert_title=analyser.ALERT_TITLE,
                                     alert_label="Austin FC vs LA Galaxy")
        assert len(seen) == 1
        message, kwargs = seen[0]
        assert kwargs.get("title") == "MLS live"
        assert "WC26" not in kwargs.get("title", "")
        assert "WC26" not in message
        assert "Austin FC vs LA Galaxy" in message

    def test_control_archive_default_title_is_unchanged(self, monkeypatch):
        """PASSES BOTH BEFORE AND AFTER the change — it is not evidence
        that a guard fires. It is here to prove the WC26 archive plane's
        own alerts were not altered while adding the MLS title."""
        _wipe_positions()
        with SessionLocal() as s:
            s.add(TrackedPosition(match_id="FX", market_id="M-1",
                                  market_title="Spain 2-1",
                                  entry_price=0.10, contracts=400,
                                  cost=42.0))
            s.commit()
        seen = []
        monkeypatch.setattr(positions, "send_alert",
                            lambda m, **k: seen.append((m, k)))
        rows = {"M-1": {"live_model_probability": 0.02,
                        "market_yes_bid": 0.10}}
        positions.evaluate_positions(rows, "FX", alert=True)
        assert len(seen) == 1
        assert seen[0][1].get("title", "WC26") == "WC26"


# --- price derivation ------------------------------------------------------

class TestQuoteDerivation:
    """New-module behaviour: there is no unmodified counterpart to fail
    against. These assert the sell-side honesty rule is carried over from
    src/kalshi_client rather than reinvented."""

    def _q(self, **kw):
        return MarketQuote(market_contract_id=1,
                           captured_at=datetime.now(UTC), **kw)

    def test_sell_side_never_substitutes_the_ask(self):
        q = self._q(yes_ask_c=42, yes_bid_c=None, no_ask_c=None)
        assert analyser.market_sell_probability(q) is None
        # control: the same quote WITH a sell side prices normally, so the
        # None above is the missing-bid rule and not a broken function
        assert analyser.market_sell_probability(
            self._q(yes_ask_c=42, yes_bid_c=40)) == 0.40

    def test_sell_side_derives_from_the_no_ask(self):
        assert analyser.market_sell_probability(
            self._q(yes_bid_c=None, no_ask_c=63)) == 0.37

    def test_buy_side_prefers_the_ask_over_the_midpoint(self):
        # a 1c/3c book: midpoint 2c is a multiplier nobody can trade
        assert analyser.market_buy_probability(
            self._q(yes_bid_c=1, yes_ask_c=3)) == 0.03

    def test_boundary_cents_are_not_prices(self):
        assert analyser.market_buy_probability(
            self._q(yes_ask_c=100, no_bid_c=0)) is None


# --- fixture and mapping selection -----------------------------------------

class TestMarketRows:
    def test_unapproved_mapping_contributes_nothing(self, live_session):
        s = live_session
        f = _fixture(s)
        _book(s, f, approved=False)
        assert analyser.market_rows(s, f) == {}

    def test_control_approved_mapping_on_the_same_fixture_is_priced(
            self, live_session):
        """The control for the test above: same fixture, same session, same
        book shape — only `mapping_approved` differs."""
        s = live_session
        f = _fixture(s)
        mc = _book(s, f, approved=True)
        rows = analyser.market_rows(s, f)
        assert set(rows) == {mc.ticker}
        row = rows[mc.ticker]
        assert row["market_yes_bid"] == 0.40
        assert row["market_probability"] == 0.42
        assert row["model_probability"] == 0.30
        assert row["model_basis"] == analyser.BASIS_PREMATCH

    def test_newest_quote_wins(self, live_session):
        s = live_session
        f = _fixture(s)
        mc = _book(s, f, yes_bid_c=40, yes_ask_c=42)
        s.add(MarketQuote(market_contract_id=mc.id,
                          captured_at=datetime.now(UTC) + timedelta(minutes=5),
                          yes_bid_c=71, yes_ask_c=73))
        s.commit()
        assert analyser.market_rows(s, f)[mc.ticker]["market_yes_bid"] == 0.71

    def test_finished_fixtures_are_not_analysed(self, live_session):
        s = live_session
        _fixture(s, status="post", event_id="401000001")
        assert analyser.live_fixtures(s) == []

    def test_control_pre_and_in_fixtures_are(self, live_session):
        s = live_session
        _fixture(s, status="pre", event_id="401000002")
        _fixture(s, status="in", event_id="401000003")
        assert len(analyser.live_fixtures(s)) == 2


class TestPass:
    def test_counts_carry_their_denominator(self, live_session, monkeypatch):
        _wipe_positions()
        s = live_session
        f = _fixture(s, status="in")
        mc = _book(s, f)
        _fixture(s, status="pre", event_id="401000004")   # no book at all
        s.commit()
        with SessionLocal() as a:
            a.add(TrackedPosition(match_id=f.espn_event_id,
                                  market_id=mc.ticker,
                                  market_title="Austin FC", entry_price=0.10,
                                  contracts=400, cost=42.0))
            a.commit()
        monkeypatch.setattr(positions, "send_alert", lambda m, **k: None)
        r = analyser.evaluate_mls_positions(alert=True)
        assert r["fixtures"] == 2 and r["priced"] == 1
        assert r["positions"] == 1
        # in play + a pre-match model => refused, not guessed
        assert r["reads"][0]["verdict"] == "STALE_MODEL"

    def test_dormant_plane_is_an_instant_no_op(self, monkeypatch):
        monkeypatch.setattr(config, "LIVE_DATABASE_URL", "")
        monkeypatch.setattr(live_db, "_engine", None)
        monkeypatch.setattr(live_db, "_Session", None)
        r = analyser.evaluate_mls_positions()
        assert r["dormant"] is True and r["positions"] == 0


class TestSchedulerWiring:
    def test_analyser_job_is_registered(self):
        """FAILS without the fix: no job carries this id on unmodified
        code. Registration only takes EFFECT on a deploy, which is out of
        scope here — this asserts the wiring, not production behaviour."""
        import jobs.scheduler as sched
        sch = sched.start_scheduler()
        try:
            ids = {j.id for j in sch.get_jobs()}
            assert "mls_analyser" in ids
        finally:
            sch.shutdown(wait=False)

    def test_job_no_ops_without_a_live_plane(self, monkeypatch):
        monkeypatch.setattr(config, "LIVE_DATABASE_URL", "")
        monkeypatch.setattr(live_db, "_engine", None)
        monkeypatch.setattr(live_db, "_Session", None)
        import jobs.scheduler as sched
        sched.mls_live_analyser_job()          # must not raise


class TestMoneyStaysLocked:
    def test_analyser_reads_only(self):
        """The analyser prices positions Son already holds; it must contain
        no order path and no coupling to the money flag.

        Checked over the parsed AST, not the source text — the module
        DISCUSSES the money lock in prose, and a substring scan would
        either flag that comment or force the comment to be deleted to
        keep the test green. Names in code are what matter."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(analyser))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.name for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)}
        for forbidden in ("REAL_MONEY_SIGNALS_ENABLED", "AUTO_EXECUTION_ENABLED",
                          "create_order", "place_order", "submit_order"):
            assert forbidden not in names
        assert config.REAL_MONEY_SIGNALS_ENABLED is False


class TestNarratorNullSafety:
    def test_non_executable_position_does_not_break_the_brief(self):
        """FAILS without the fix: formatting None with `:.0f` raises
        TypeError, and live_signals catches it — so ONE no-bid position
        silently suppressed the entire narrator brief."""
        from src import narrator
        out = narrator._fmt_positions([
            {"market_title": "Austin FC", "hold_ev": 30.0,
             "cashout_now": None, "verdict": "NO_BID"},
            {"market_title": "LA Galaxy", "hold_ev": None,
             "cashout_now": 38.0, "verdict": "STALE_MODEL"},
        ])
        assert "NO_BID" in out and "STALE_MODEL" in out
        assert "—" in out
