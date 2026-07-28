"""Personal bet journal + execution-fidelity pilot.

The wall test comes first and deliberately so: everything else in this
module is a feature, and the wall is the invariant those features must
not breach. If personal rows can reach the paper ledger's totals, the
research evidence is contaminated and nothing else here matters.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import config
# The live-plane fixture (throwaway SQLite + the VARCHAR-length guard
# that makes SQLite behave like PostgreSQL) lives in the shadow suite.
# Reuse it rather than standing up a second, subtly different one.
from tests.test_mls_shadow import live_session  # noqa: F401
from src.live.models import (Competition, Fixture, MarketContract,
                             MarketEvent, MarketQuote, MarketSnapshot,
                             PersonalBet, PredictionContract,
                             PredictionRun)

UTC = timezone.utc


def _seed(s, kickoff_in_hours=3.0):
    """A fixture with a quote, a completed run, and a priced contract."""
    fx = Fixture(id=1, competition_slug="mls-2026", espn_event_id="e1",
                 status="pre",
                 current_kickoff_utc=datetime.now(UTC)
                 + timedelta(hours=kickoff_in_hours))
    snap = MarketSnapshot(id=1, fixture_id=1, captured_at=datetime.now(UTC),
                          status="complete", execution_ready=True,
                          oldest_quote_age_seconds=30)
    ev = MarketEvent(id=1, competition_slug="mls-2026",
                     kalshi_event_ticker="KXMLSGAME-x", series="KXMLSGAME",
                     fixture_id=1, mapping_approved=True)
    mc = MarketContract(id=1, market_event_id=1,
                        ticker="KXMLSGAME-x-H", outcome_key="home_win")
    q = MarketQuote(id=1, market_contract_id=1,
                    captured_at=datetime.now(UTC) - timedelta(minutes=1),
                    yes_ask_c=45, yes_bid_c=43, yes_ask_size=100,
                    market_snapshot_id=1)
    run = PredictionRun(id="run1", fixture_id=1, run_type="scheduled",
                        status="complete", captured_at=datetime.now(UTC))
    s.add_all([fx, snap, ev, mc, q, run])
    s.flush()
    s.add(PredictionContract(prediction_run_id="run1",
                             market_contract_id=1, market_quote_id=1,
                             outcome_key="home_win", raw_probability=0.52))
    s.commit()
    return fx


class TestTheWall:
    """The paper ledger is mechanical research evidence. These rows are
    human-selected. If they ever sum, the research record is worthless —
    so this is asserted directly rather than left to convention."""

    def test_paper_summary_is_unchanged_by_journal_rows(
            self, live_session, monkeypatch):
        import json as _json

        from src.live import journal, paper
        monkeypatch.setattr(config, "PAPER_TRADING_ENABLED", True)
        _seed(live_session)
        before = _json.dumps(paper.paper_summary(), sort_keys=True,
                             default=str)

        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", stated_size="100",
                                  market_quote_id=1, rationale="test")
        journal.resolve_view(bet["id"], "taken")
        journal.record_execution(
            bet["id"], "friend-A",
            consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="100",
            fee_paid="1.20", market_quote_id_at_fill=1)

        after = _json.dumps(paper.paper_summary(), sort_keys=True,
                            default=str)
        assert before == after, "journal rows leaked into the paper ledger"

    def test_no_orm_relationship_between_the_two(self):
        """A join would let a future aggregate blur them by accident."""
        from src.live.models import PaperFill, PaperSignal, PersonalBet
        personal_cols = {c.name for c in PersonalBet.__table__.columns}
        for m in (PaperSignal, PaperFill):
            paper_cols = {c.name for c in m.__table__.columns}
            assert "personal_bet_id" not in paper_cols
        assert not any(c.startswith("paper_") for c in personal_cols)


class TestRecordThePasses:
    """A journal of only the bets that were taken has exactly the
    survivorship problem the paper ledger's retained rejections prevent."""

    def test_a_pass_is_recorded_with_the_same_evidence_as_a_take(
            self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1,
                                  rationale="too rich")
        passed = journal.resolve_view(bet["id"], "passed")
        assert passed["status"] == "passed"
        # the pass keeps the frozen evidence — it is not a lesser record
        assert passed["price_basis"] == "observed_quote"
        assert passed["model_probability"] == 0.52
        assert passed["prediction_run_id"] == "run1"

    def test_summary_reports_the_denominator(self, live_session):
        from src.live import journal
        _seed(live_session)
        a = journal.record_view(1, "KXMLSGAME-x-H", outcome_key="home_win",
                                market_quote_id=1)
        b = journal.record_view(1, "KXMLSGAME-x-H", outcome_key="home_win",
                                market_quote_id=1)
        journal.resolve_view(a["id"], "taken")
        journal.resolve_view(b["id"], "passed")
        out = journal.journal_summary()
        assert out["counts"]["taken"] == 1
        assert out["counts"]["passed"] == 1
        assert out["total_recorded"] == 2
        # no bare hit rate anywhere — the passes are what make the takes
        # interpretable
        assert "hit_rate" not in out


class TestFalsifiability:
    """A price nobody can check is a story, not a record."""

    def test_a_quote_that_does_not_exist_is_refused_not_cited(
            self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.31",
                                  market_quote_id=9999)   # no such quote
        assert bet["price_basis"] == "stated_only"
        assert bet["market_quote_id"] is None
        assert bet["counts_toward_aggregate"] is False

    def test_stated_only_is_recorded_but_never_counted(self, live_session):
        from src.live import journal
        _seed(live_session)
        journal.record_view(1, "KXMLSGAME-x-H", outcome_key="home_win",
                            stated_price="0.31")
        out = journal.journal_summary()
        assert out["total_recorded"] == 1
        assert out["countable"] == 0
        assert out["excluded_stated_only"] == 1

    def test_a_quote_captured_after_recording_cannot_be_cited(
            self, live_session):
        """Otherwise an entry could be priced off a book that did not
        exist when the view was formed."""
        from src.live import journal
        _seed(live_session)
        q = live_session.get(MarketQuote, 1)
        q.captured_at = datetime.now(UTC) + timedelta(minutes=5)
        live_session.commit()
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        assert bet["price_basis"] == "stated_only"

    def test_after_kickoff_a_view_is_void(self, live_session):
        from src.live import journal
        _seed(live_session, kickoff_in_hours=-1.0)      # already started
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        assert bet["status"] == "void"
        assert bet["counts_toward_aggregate"] is False
        assert journal.resolve_view(bet["id"], "taken").get("error")

    def test_the_model_probability_is_frozen_at_record_time(
            self, live_session):
        """A later run must not retroactively change what was recorded."""
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        assert bet["model_probability"] == 0.52
        live_session.add(PredictionRun(
            id="run2", fixture_id=1, run_type="scheduled",
            status="complete",
            captured_at=datetime.now(UTC) + timedelta(minutes=1)))
        live_session.flush()
        live_session.add(PredictionContract(
            prediction_run_id="run2", market_contract_id=1,
            outcome_key="home_win", raw_probability=0.99))
        live_session.commit()
        stored = live_session.get(PersonalBet, bet["id"])
        assert stored.model_probability == 0.52
        assert stored.prediction_run_id == "run1"


class TestExecutions:
    def test_consent_is_required(self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        r = journal.record_execution(bet["id"], "friend-A",
                                     consent_recorded_at=None,
                                     fill_price="0.47")
        assert "consent" in (r.get("error") or "")

    def test_a_failed_fill_is_evidence_not_an_absence(self, live_session):
        """Liquidity is half of what this pilot measures."""
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.31", market_quote_id=1)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            status="not_filled", not_filled_reason="price_moved",
            best_available_price="0.34")
        assert r["status"] == "not_filled"
        assert r["best_available_price_dollars"] == "0.34"
        out = journal.journal_summary()
        assert out["executions"]["not_filled"] == 1

    def test_not_filled_demands_a_reason(self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            status="not_filled")
        assert r.get("error")

    def test_slippage_decomposes_into_drift_and_execution_cost(
            self, live_session):
        """`fill - stated` alone cannot distinguish the market moving
        from the spread costing you, and only one of those is a
        statement about the execution model."""
        from src.live import journal
        _seed(live_session)
        # a second quote representing the book AT FILL time, mid moved up
        live_session.add(MarketQuote(
            id=2, market_contract_id=1, captured_at=datetime.now(UTC),
            yes_ask_c=49, yes_bid_c=47, yes_ask_size=100,
            market_snapshot_id=1))
        live_session.commit()
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.49", filled_contracts="100", fee_paid="1.30",
            market_quote_id_at_fill=2)
        g = r["gaps"]
        # mid@record 0.44 · mid@fill 0.48 · stated 0.45 · fill 0.49
        assert Decimal(g["slippage"]) == Decimal("0.04")
        assert Decimal(g["market_drift"]) == Decimal("0.04")
        assert Decimal(g["execution_cost"]) == Decimal("0.01")
        assert Decimal(g["aggressiveness"]) == Decimal("0.01")
        # THREE components, and they reconstruct exactly. A two-way
        # split would have silently charged the unrealistic stated
        # price to execution cost.
        assert g["slippage_identity_holds"] is True
        assert (Decimal(g["market_drift"]) + Decimal(g["execution_cost"])
                - Decimal(g["aggressiveness"])) == Decimal(g["slippage"])

    def test_fee_delta_surfaces_rather_than_deferring_to_the_model(
            self, live_session):
        """Where the model and the exchange disagree, the exchange wins
        and the difference is recorded."""
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.45", filled_contracts="100",
            fee_paid="2.00", market_quote_id_at_fill=1)
        g = r["gaps"]
        assert Decimal(g["fee_delta"]) != 0
        assert Decimal(g["fee_paid"] if "fee_paid" in g
                       else "2.00") == Decimal("2.00")
        # the modelled value is reported beside it, not instead of it
        assert Decimal(g["fee_modelled"]) > 0


class TestReportingFloor:
    def test_no_summary_statistic_below_the_minimum(self, live_session):
        """Three fills tell you the fee model is not OBVIOUSLY broken.
        That is not the same as telling you it is right."""
        from src.live import journal
        _seed(live_session)
        out = journal.journal_summary()
        assert "aggregate_withheld" in out
        assert out["aggregate_withheld"]["minimum"] == \
            journal.MIN_EXECUTIONS_FOR_AGGREGATE
        # absent, not zero — a zero would read as a measured result
        for k in ("mean_slippage", "roi_pct", "hit_rate",
                  "mean_fee_delta"):
            assert k not in out


class TestBroadcast:
    def test_a_broadcast_is_persisted_even_if_dispatch_fails(
            self, live_session, monkeypatch):
        from src.live import journal
        import src.alerts as alerts
        _seed(live_session)

        def boom(*a, **kw):
            raise RuntimeError("discord down")
        monkeypatch.setattr(alerts, "send_alert", boom)
        r = journal.broadcast("CIN drifted to 0.34", fixture_id=1,
                              session_label="trivela-live")
        assert r["dispatched"] is False
        assert journal.recent_broadcasts(1)[0]["message"] == \
            "CIN drifted to 0.34"

    def test_a_reopened_session_can_read_what_it_already_said(
            self, live_session, monkeypatch):
        from src.live import journal
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": True})
        _seed(live_session)
        journal.broadcast("first", fixture_id=1)
        journal.broadcast("second", fixture_id=1)
        said = journal.recent_broadcasts(1)
        assert [b["message"] for b in said] == ["second", "first"]


class TestBroadcastBoundary:
    """journal-P0 F1: the action channel reaches a human whose friend
    bets real money. Only operator-authenticated session prose may
    dispatch; the lock governs model-generated signals, so a computed or
    automated caller is refused mechanically — and every action dispatch
    carries the standing-edge qualifier so no number travels without its
    uncertainty."""

    def test_session_action_broadcast_dispatches_with_the_qualifier(
            self, live_session, monkeypatch):
        """The carve-out: with REAL_MONEY_SIGNALS_ENABLED=false, session
        prose still dispatches — WITH the uncertainty attached."""
        from src.live import journal
        import src.alerts as alerts
        assert config.REAL_MONEY_SIGNALS_ENABLED is False
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        _seed(live_session)
        r = journal.broadcast("CIN value at 0.31", channel="action",
                              source="session", fixture_id=1)
        assert r["dispatched"] is True
        assert len(sent) == 1
        assert sent[0].startswith("🗣")
        # the qualifier is appended SERVER-SIDE: shadow-mode + the
        # standing edge with its significance state, never a bare number
        assert "[shadow]" in sent[0]
        assert "not a real-money signal" in sent[0]
        assert ("not significant" in sent[0]
                or "no established edge" in sent[0])

    def test_a_non_session_source_is_refused(self, live_session,
                                             monkeypatch):
        from src.live import journal
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        _seed(live_session)
        r = journal.broadcast("flip: exit > hold", source="computed",
                              fixture_id=1)
        assert r.get("refused") is True
        assert r["dispatched"] is False
        assert "error" in r
        assert sent == []                    # nothing reached a transport
        # and nothing was persisted as "said" — it never was
        assert journal.recent_broadcasts(1) == []

    def test_a_scheduler_frame_is_refused_even_claiming_session(
            self, live_session, monkeypatch):
        """The stack check: source="session" is verified against the
        call stack, not taken on the caller's word."""
        from src.live import journal
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": True})
        _seed(live_session)
        code = compile(
            "result = journal.broadcast('auto', source='session', "
            "fixture_id=1)", "<scheduler>", "exec")
        g = {"__name__": "jobs.scheduler", "journal": journal}
        exec(code, g)
        assert g["result"].get("refused") is True
        assert "jobs.scheduler" in g["result"]["error"]

    def test_no_scheduler_or_model_module_touches_broadcast(self):
        """Static guard: the scheduler and every model/pipeline module
        must not import or call the broadcast path at all."""
        import glob
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        guarded = sorted(glob.glob(os.path.join(root, "jobs", "*.py")) + [
            os.path.join(root, "src", "live", f) for f in (
                "runs.py", "paper.py", "ingest.py", "markets.py",
                "model_mls.py", "model_eval.py", "slate.py", "risk.py",
                "mls_stats.py", "lineups.py", "audit.py")])
        assert guarded, "guarded module list resolved to nothing"
        for path in guarded:
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            assert "broadcast" not in text, (
                f"{path} references broadcast — scheduled/model code "
                f"must never reach the relay")
            assert "src.live.journal" not in text, (
                f"{path} imports the journal — scheduled/model code "
                f"must never reach the relay")

    def test_dispatched_reports_per_transport_truth(self, live_session,
                                                    monkeypatch):
        """journal-P1 F6: dispatched=true only when a transport ACCEPTED;
        all-fail reports false; the accepting transports are named."""
        from src.live import journal
        import src.alerts as alerts
        _seed(live_session)
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": False,
                                             "ntfy": True})
        r = journal.broadcast("one leg down", fixture_id=1)
        assert r["dispatched"] is True
        assert r["accepted"] == ["ntfy"]
        assert r["transports"] == {"discord_action": False, "ntfy": True}
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": False,
                                             "ntfy": False})
        r2 = journal.broadcast("all legs down", fixture_id=1)
        assert r2["dispatched"] is False
        assert r2["accepted"] == []

    def test_send_alert_returns_per_transport_results(self, monkeypatch):
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_discord",
                            lambda m, channel="action": channel == "action")
        monkeypatch.setattr(alerts, "send_ntfy",
                            lambda m, **kw: False)
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL", "a")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL", "d")
        out = alerts.send_alert("x", kind="action")
        assert out == {"discord_action": True, "discord_detail": False,
                       "ntfy": False}
        assert alerts.send_alert("x", kind="detail") == {
            "discord_detail": False}
