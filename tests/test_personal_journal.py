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
            fee_paid="1.20", filled_at=datetime.now(UTC),
            market_quote_id_at_fill=1)

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


@pytest.fixture()
def session_cap(monkeypatch):
    """A REAL interactive-session capability, minted through the REAL
    challenge/response handshake (journal-P0-G).

    Deliberately not a stub. `journal.broadcast` re-checks the object
    against the live capability store, so a hand-built `SessionCapability`
    would be rejected — which is the property under test. Every broadcast
    test therefore exercises the composed path: handshake -> capability
    -> dispatch."""
    from src.live import session_capability as sc
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(config, "JOURNAL_SESSION_SECRET",
                        "second-factor-not-the-admin-token")
    sc._reset_for_tests()
    ch = sc.challenge()
    assert "error" not in ch, ch
    opened = sc.open_session(ch["challenge_id"],
                             sc.expected_response(ch["nonce"]),
                             label="trivela-live")
    assert "error" not in opened, opened
    token = opened["session_token"]
    yield {"token": token, "capability": sc.verify(token),
           "headers": {"X-Admin-Token": "s3cret",
                       "X-Session-Token": token}}
    sc._reset_for_tests()


def _seed_other_fixture(s):
    """A second approved fixture/event/contract with quote id=5 —
    shared by the view-time (F3) and fill-time (P0-E) identity tests."""
    fx2 = Fixture(id=2, competition_slug="mls-2026",
                  espn_event_id="e2", status="pre",
                  current_kickoff_utc=datetime.now(UTC)
                  + timedelta(hours=3))
    ev2 = MarketEvent(id=2, competition_slug="mls-2026",
                      kalshi_event_ticker="KXMLSGAME-y",
                      series="KXMLSGAME", fixture_id=2,
                      mapping_approved=True)
    mc2 = MarketContract(id=2, market_event_id=2,
                         ticker="KXMLSGAME-y-H",
                         outcome_key="home_win")
    q2 = MarketQuote(id=5, market_contract_id=2,
                     captured_at=datetime.now(UTC)
                     - timedelta(minutes=1),
                     yes_ask_c=60, yes_bid_c=58)
    s.add_all([fx2, ev2, mc2, q2])
    s.commit()


class TestQuoteIdentity:
    """journal-P0 F3: a cited quote must BELONG — fixture → approved
    MarketEvent → MarketContract → quote. One that exists but belongs
    elsewhere is rejected with the mismatch named, never accepted as a
    falsifiable price for the wrong match."""

    def test_a_quote_from_another_fixture_is_rejected(self, live_session):
        from src.live import journal
        _seed(live_session)
        _seed_other_fixture(live_session)
        r = journal.record_view(1, "KXMLSGAME-y-H",
                                outcome_key="home_win",
                                stated_price="0.60", market_quote_id=5)
        assert "error" in r
        assert "belongs to fixture 2" in r["error"]
        # rejected means NOT recorded — not downgraded, not kept
        assert journal.journal_summary()["total_recorded"] == 0

    def test_a_quote_for_another_outcome_is_rejected(self, live_session):
        from src.live import journal
        _seed(live_session)
        r = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="away_win",
                                market_quote_id=1)
        assert "error" in r
        assert "home_win" in r["error"] and "away_win" in r["error"]

    def test_a_quote_for_another_ticker_is_rejected(self, live_session):
        from src.live import journal
        _seed(live_session)
        r = journal.record_view(1, "KXMLSGAME-x-T",
                                outcome_key="home_win",
                                market_quote_id=1)
        assert "error" in r
        assert "KXMLSGAME-x-H" in r["error"]
        assert "KXMLSGAME-x-T" in r["error"]

    def test_an_unapproved_event_mapping_is_rejected(self, live_session):
        from src.live import journal
        _seed(live_session)
        ev = live_session.get(MarketEvent, 1)
        ev.mapping_approved = False
        live_session.commit()
        r = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                market_quote_id=1)
        assert "error" in r
        assert "not approved" in r["error"]

    def test_the_verified_chain_fills_the_contract_fk(self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        assert bet["price_basis"] == "observed_quote"
        stored = live_session.get(PersonalBet, bet["id"])
        assert stored.market_contract_id == 1


class TestExecutionLifecycle:
    """journal-P0 F5: considered → taken → filled|partial → settled →
    reconciled, append-only. Invalid transitions are rejected, fill
    facts are never manufactured, and retries are no-ops."""

    def _taken(self, journal):
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", stated_size="100",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def test_end_to_end_lifecycle(self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", stated_size="100",
                                  market_quote_id=1)
        assert bet["status"] == "considered"
        taken = journal.resolve_view(bet["id"], "taken")
        assert taken["status"] == "taken"
        # the fill moment must not precede the taken moment (P0-D
        # chronology), so it is captured AFTER the resolution above
        now = datetime.now(UTC)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="100", fee_paid="1.20",
            filled_at=now, market_quote_id_at_fill=1,
            exchange_order_id="ORD-1")
        assert ex["status"] == "filled"
        st = journal.settle_execution(
            ex["id"], settlement_credit="100", settled_at=now,
            settled_outcome="home_win")
        assert st["settled_at"] is not None
        assert st["settlement_credit_dollars"] == "100"
        assert Decimal(st["gaps"]["settlement_delta"]) == Decimal("0")
        rec = journal.reconcile_execution(
            ex["id"], note="matches the Kalshi statement line for ORD-1")
        assert rec["reconciled"] is True
        assert rec["reconciliation_note"]

    def test_a_fill_against_a_passed_entry_is_rejected(self,
                                                       live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "passed")
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="100", fee_paid="1.20",
            filled_at=datetime.now(UTC))
        assert "error" in r
        assert "passed" in r["error"]

    def test_a_fill_against_a_void_entry_is_rejected(self, live_session):
        from src.live import journal
        _seed(live_session, kickoff_in_hours=-1.0)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        assert bet["status"] == "void"
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="100", fee_paid="1.20",
            filled_at=datetime.now(UTC))
        assert "error" in r
        assert "void" in r["error"]

    def test_a_fill_against_an_unresolved_entry_is_rejected(
            self, live_session):
        """considered → filled skips the decision; the walk is
        considered → taken → filled."""
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="100", fee_paid="1.20",
            filled_at=datetime.now(UTC))
        assert "error" in r
        assert "considered" in r["error"]

    def test_missing_economics_on_filled_is_rejected(self, live_session):
        """A filled execution without price, quantity, fee and the real
        fill moment measures nothing — and the server must not invent
        the missing facts (it used to default filled_at to now())."""
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        r = journal.record_execution(
            bet["id"], "friend-A",
            consent_recorded_at=datetime.now(UTC),
            fill_price="0.47")           # no quantity, fee or timestamp
        assert "error" in r
        for field in ("filled_contracts", "fee_paid", "filled_at"):
            assert field in r["error"], field

    def test_a_repeat_post_is_a_no_op(self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        kw = dict(consent_recorded_at=now, fill_price="0.47",
                  filled_contracts="100", fee_paid="1.20", filled_at=now,
                  exchange_order_id="ORD-9")
        first = journal.record_execution(bet["id"], "friend-A", **kw)
        assert "error" not in first
        again = journal.record_execution(bet["id"], "friend-A", **kw)
        assert again.get("idempotent") is True
        assert again["id"] == first["id"]
        assert live_session.query(PBE).count() == 1

    def test_the_database_itself_enforces_order_status_uniqueness(
            self, live_session):
        """Test-DB parity: the (exchange_order_id, status) uniqueness is
        a real constraint the SQLite suite enforces too, not a promise
        the handler makes."""
        from sqlalchemy.exc import IntegrityError
        from src.live.models import PersonalBetExecution as PBE
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        for _ in range(2):
            live_session.add(PBE(
                personal_bet_id=bet["id"], account_label="friend-A",
                consent_recorded_at=now, status="filled",
                fill_price_dollars="0.47", filled_contracts="100",
                fee_paid_dollars="1.20", filled_at=now,
                exchange_order_id="ORD-DUP"))
        with pytest.raises(IntegrityError):
            live_session.commit()
        live_session.rollback()

    def test_resolution_is_immutable_once_set(self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        r = journal.resolve_view(bet["id"], "passed")
        assert "error" in r
        assert "immutable" in r["error"]
        assert live_session.get(PersonalBet, bet["id"]).status == "taken"

    def test_a_correction_is_a_new_row_referencing_the_old(
            self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        fix = journal.record_view(
            1, "KXMLSGAME-x-H", outcome_key="home_win",
            market_quote_id=1, rationale="mis-resolved; he passed",
            corrects_bet_id=bet["id"])
        assert fix["corrects_bet_id"] == bet["id"]
        assert fix["id"] != bet["id"]
        # the mistaken row is untouched — the record keeps its mistake
        assert live_session.get(PersonalBet, bet["id"]).status == "taken"
        bad = journal.record_view(1, "KXMLSGAME-x-H",
                                  corrects_bet_id=999)
        assert "error" in bad

    def test_settlement_requires_a_fill_and_is_immutable(
            self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        nf = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            status="not_filled", not_filled_reason="price_moved")
        r = journal.settle_execution(nf["id"], settlement_credit="0",
                                     settled_at=now)
        assert "error" in r                    # not_filled never settles
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="100", fee_paid="1.20",
            filled_at=now, exchange_order_id="ORD-2")
        ok = journal.settle_execution(ex["id"], settlement_credit="100",
                                      settled_at=now)
        assert "error" not in ok
        # identical retry: no-op; conflicting rewrite: refused
        again = journal.settle_execution(ex["id"],
                                         settlement_credit="100",
                                         settled_at=now)
        assert again.get("idempotent") is True
        bad = journal.settle_execution(ex["id"], settlement_credit="50",
                                       settled_at=now)
        assert "error" in bad
        assert "immutable" in bad["error"]

    def test_reconcile_requires_settlement_first(self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="100", fee_paid="1.20",
            filled_at=now)
        r = journal.reconcile_execution(ex["id"], note="premature")
        assert "error" in r
        assert "not settled" in r["error"]
        journal.settle_execution(ex["id"], settlement_credit="100",
                                 settled_at=now)
        ok = journal.reconcile_execution(ex["id"], note="checked")
        assert ok["reconciled"] is True
        again = journal.reconcile_execution(ex["id"], note="checked")
        assert again.get("idempotent") is True


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
        journal.resolve_view(bet["id"], "taken")
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
        journal.resolve_view(bet["id"], "taken")
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.49", filled_contracts="100", fee_paid="1.30",
            filled_at=datetime.now(UTC), market_quote_id_at_fill=2)
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
        journal.resolve_view(bet["id"], "taken")
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=datetime.now(UTC),
            fill_price="0.45", filled_contracts="100",
            fee_paid="2.00", filled_at=datetime.now(UTC),
            market_quote_id_at_fill=1)
        g = r["gaps"]
        assert Decimal(g["fee_delta"]) != 0
        assert Decimal(g["fee_paid"] if "fee_paid" in g
                       else "2.00") == Decimal("2.00")
        # the modelled value is reported beside it, not instead of it
        assert Decimal(g["fee_modelled"]) > 0


class TestLifecycleDomains:
    """journal-P0-D: the lifecycle starts where the state machine says
    it starts, operator-supplied values carry DOMAINS, timestamps carry
    their timezone, and time runs forward. Every rejection is proven
    against the DATABASE — an invalid request writes exactly nothing —
    because round 1 validated some of this after rows were already
    committed."""

    HDRS = {"X-Admin-Token": "s3cret"}

    @pytest.fixture()
    def client(self, live_session, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        from fastapi.testclient import TestClient
        from api import main as api_main
        api_main._rate_last.clear()
        return TestClient(api_main.app)

    def _taken(self, journal):
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def test_an_initial_taken_cannot_be_posted_through_the_api(
            self, live_session, client):
        """The COMPOSED path: JournalViewIn.status was a default, not a
        constraint — POSTing status="taken" skipped considered→resolve
        entirely, and with it the moment the passes become recordable.
        There is no spelling of a pre-resolved entry at any layer now."""
        _seed(live_session)
        r = client.post("/api/admin/mls/journal/view", headers=self.HDRS,
                        json={"fixture_id": 1,
                              "market_ticker": "KXMLSGAME-x-H",
                              "outcome_key": "home_win",
                              "market_quote_id": 1,
                              "status": "taken"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "considered"
        stored = live_session.get(PersonalBet, r.json()["id"])
        assert stored.status == "considered"

    def test_record_view_accepts_no_status_at_all(self):
        """Not an ignored value — the parameter does not exist, so no
        future caller can quietly reintroduce the bypass."""
        import inspect
        from src.live import journal
        assert "status" not in inspect.signature(
            journal.record_view).parameters

    def test_view_price_and_size_domains_reject_and_write_nothing(
            self, live_session):
        from src.live import journal
        _seed(live_session)
        cases = [{"stated_price": "NaN"}, {"stated_price": "Infinity"},
                 {"stated_price": "-1"}, {"stated_price": "1.01"},
                 {"stated_price": "garbage"},
                 {"stated_size": "0"}, {"stated_size": "-5"},
                 {"stated_size": "NaN"}]
        for kw in cases:
            r = journal.record_view(1, "KXMLSGAME-x-H",
                                    outcome_key="home_win",
                                    market_quote_id=1, **kw)
            assert "error" in r, kw
            (field, _), = kw.items()
            assert field in r["error"]
        assert live_session.query(PersonalBet).count() == 0

    def test_execution_economics_domains_reject_and_write_nothing(
            self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        base = dict(consent_recorded_at=now, filled_at=now,
                    fill_price="0.47", filled_contracts="10",
                    fee_paid="0.12")
        cases = [{"fill_price": "1.01"}, {"fill_price": "-0.2"},
                 {"fill_price": "NaN"}, {"fill_price": "inf"},
                 {"filled_contracts": "0"}, {"filled_contracts": "-5"},
                 {"fee_paid": "-0.01"},
                 {"best_available_price": "2"}]
        for kw in cases:
            r = journal.record_execution(
                bet["id"], "friend-A", **{**base, **kw})
            assert "error" in r, kw
            (field, _), = kw.items()
            assert field in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_naive_timestamps_are_rejected_never_relabelled(
            self, live_session):
        """'Assume UTC' converts a data-entry mistake into wrong
        provenance with no trace — on the consent moment, the fill
        moment and the settlement moment alike."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        naive = datetime.now(UTC).replace(tzinfo=None)
        aware = datetime.now(UTC)
        econ = dict(fill_price="0.47", filled_contracts="10",
                    fee_paid="0.12")
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=naive,
            filled_at=aware, **econ)
        assert "error" in r and "timezone-naive" in r["error"]
        assert "consent_recorded_at" in r["error"]
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=aware,
            filled_at=naive, **econ)
        assert "error" in r and "filled_at" in r["error"]
        assert live_session.query(PBE).count() == 0
        ok = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=aware,
            filled_at=datetime.now(UTC), exchange_order_id="ORD-TZ",
            **econ)
        assert "error" not in ok
        r = journal.settle_execution(
            ok["id"], settlement_credit="10",
            settled_at=datetime.now(UTC).replace(tzinfo=None))
        assert "error" in r and "settled_at" in r["error"]
        live_session.expire_all()
        assert live_session.get(PBE, ok["id"]).settled_at is None

    def test_a_fill_cannot_antedate_the_taken_moment(self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        r = journal.record_execution(
            bet["id"], "friend-A",
            consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=datetime.now(UTC) - timedelta(minutes=10))
        assert "error" in r and "antedate" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_settlement_cannot_precede_the_fill(self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-CHR")
        r = journal.settle_execution(
            ex["id"], settlement_credit="10",
            settled_at=now - timedelta(hours=1))
        assert "error" in r and "settle before the fill" in r["error"]
        live_session.expire_all()
        row = live_session.get(PBE, ex["id"])
        assert row.settled_at is None
        assert row.settlement_credit_dollars is None

    def test_a_gap_computation_failure_cannot_unrecord_a_fill(
            self, live_session, monkeypatch):
        """The execution row COMMITS before the derived-gap computation
        runs, so a failure there must degrade to an explicit marker on
        the response — never a 500 that makes a recorded fill look
        unrecorded."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)

        def boom(*a, **kw):
            raise RuntimeError("fee model exploded")
        monkeypatch.setattr(journal, "gaps_for", boom)
        now = datetime.now(UTC)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-GAP")
        assert "error" not in r
        assert "fee model exploded" in r["gaps"]["gaps_computation_error"]
        assert live_session.query(PBE).count() == 1


class TestFillBookIdentity:
    """journal-P0-E: `market_quote_id_at_fill` earns the SAME identity
    chain the view-time quote got — validated against the PARENT BET's
    fixture/contract/outcome, with a capture time consistent with the
    fill moment. Round 1 stored the id raw, and gaps_for() then derived
    drift and execution cost from whatever book the id happened to
    name."""

    def _taken(self, journal, ticker="KXMLSGAME-x-H",
               market_quote_id=1):
        bet = journal.record_view(1, ticker, outcome_key="home_win",
                                  stated_price="0.45",
                                  market_quote_id=market_quote_id)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def _fill(self, journal, bet, quote_id, filled_at=None):
        return journal.record_execution(
            bet["id"], "friend-A",
            consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=filled_at or datetime.now(UTC),
            market_quote_id_at_fill=quote_id)

    def test_a_fill_book_from_another_fixture_is_rejected(
            self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        _seed_other_fixture(live_session)
        bet = self._taken(journal)
        r = self._fill(journal, bet, 5)
        assert "error" in r and "belongs to fixture 2" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_a_fill_book_on_another_contract_is_rejected(
            self, live_session):
        """Same fixture, wrong contract — the away-side book cannot be
        the book the home-side bet filled against."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        live_session.add_all([
            MarketContract(id=3, market_event_id=1,
                           ticker="KXMLSGAME-x-T",
                           outcome_key="away_win"),
            MarketQuote(id=7, market_contract_id=3,
                        captured_at=datetime.now(UTC)
                        - timedelta(minutes=1),
                        yes_ask_c=55, yes_bid_c=53)])
        live_session.commit()
        bet = self._taken(journal)
        r = self._fill(journal, bet, 7)
        assert "error" in r
        assert "market contract 3" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_a_fill_book_for_another_outcome_is_rejected(
            self, live_session):
        """A stated-ticker-only bet (no resolved contract FK) still gets
        the outcome check against the parent bet."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        live_session.add_all([
            MarketContract(id=3, market_event_id=1,
                           ticker="KXMLSGAME-x-T",
                           outcome_key="away_win"),
            MarketQuote(id=7, market_contract_id=3,
                        captured_at=datetime.now(UTC)
                        - timedelta(minutes=1),
                        yes_ask_c=55, yes_bid_c=53)])
        live_session.commit()
        # the bet states the away contract's TICKER but the home
        # outcome, and cites no view quote — so only the outcome check
        # can catch the mismatch
        bet = journal.record_view(1, "KXMLSGAME-x-T",
                                  outcome_key="home_win",
                                  stated_price="0.45")
        journal.resolve_view(bet["id"], "taken")
        r = self._fill(journal, bet, 7)
        assert "error" in r
        assert "away_win" in r["error"] and "home_win" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_an_unapproved_event_fill_book_is_rejected(
            self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        ev = live_session.get(MarketEvent, 1)
        ev.mapping_approved = False
        live_session.commit()
        r = self._fill(journal, bet, 1)
        assert "error" in r and "not approved" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_a_fill_book_captured_after_the_fill_is_rejected(
            self, live_session):
        """An impossible capture time: a book observed after the fill
        cannot be the book at fill."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        live_session.add(MarketQuote(
            id=8, market_contract_id=1,
            captured_at=datetime.now(UTC) + timedelta(hours=1),
            yes_ask_c=45, yes_bid_c=43))
        live_session.commit()
        bet = self._taken(journal)
        r = self._fill(journal, bet, 8,
                       filled_at=datetime.now(UTC))
        assert "error" in r and "AFTER the fill" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_a_nonexistent_fill_book_is_rejected(self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        r = self._fill(journal, bet, 999)
        assert "error" in r and "names no stored quote" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_a_missing_fill_book_is_classified_not_silent(
            self, live_session):
        """gaps_basis names the evidence: with no fill book the
        decomposed metrics are explicitly ABSENT under
        `no_fill_book`, never silently missing — and never computed
        from a wrong book."""
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        r = journal.record_execution(
            bet["id"], "friend-A",
            consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=datetime.now(UTC))
        g = r["gaps"]
        assert g["gaps_basis"] == "no_fill_book"
        for k in ("market_drift", "execution_cost", "aggressiveness"):
            assert k not in g
        # CONTROL, passes both ways: with both books the decomposition
        # is present and labelled accordingly
        bet2 = journal.record_view(1, "KXMLSGAME-x-H",
                                   outcome_key="home_win",
                                   stated_price="0.45",
                                   market_quote_id=1)
        journal.resolve_view(bet2["id"], "taken")
        r2 = journal.record_execution(
            bet2["id"], "friend-A",
            consent_recorded_at=datetime.now(UTC),
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=datetime.now(UTC), market_quote_id_at_fill=1,
            exchange_order_id="ORD-BOTH")
        assert r2["gaps"]["gaps_basis"] == "record_and_fill_books"
        assert "market_drift" in r2["gaps"]


class TestComposedFreshness:
    """journal-P0-B, the reviewer's exact repro through the REAL path:
    the bundle is assembled from inner event/market caches that serve
    stale on failure, and the outer wrapper used to stamp assembly time
    — laundering a two-hour-old ask into `live, age 0`. Fake network,
    fake clock, real find_book/event_markets/find_all_books composition."""

    DATE = "2026-07-28T23:30Z"
    HOME, AWAY = "FC Cincinnati", "Columbus Crew"

    class _FakeTime:
        def __init__(self):
            self.now = 1000.0

        def monotonic(self):
            return self.now

        def sleep(self, _s):
            pass

    @pytest.fixture()
    def market_world(self, monkeypatch):
        """A live Kalshi world behind a kill switch: canned events and
        markets while up, every fetch failing while down."""
        from src import mls
        ft = self._FakeTime()
        monkeypatch.setattr(mls, "time", ft)
        mls._cache.clear()
        state = {"up": True}

        def fake_get_json(url, params=None):
            if not state["up"]:
                return None
            if "/events" in url:
                return {"events": [{
                    "event_ticker": "KXMLSGAME-26JUL28CINCLB",
                    "title": "FC Cincinnati vs Columbus Crew"}]}
            if "/markets" in url:
                return {"markets": [{
                    "ticker": "KXMLSGAME-26JUL28CINCLB-CIN",
                    "yes_sub_title": "FC Cincinnati",
                    "yes_ask_dollars": "0.45",
                    "yes_bid_dollars": "0.43",
                    "status": "active"}]}
            return None

        monkeypatch.setattr(mls, "_get_json", fake_get_json)
        yield mls, ft, state
        mls._cache.clear()

    def test_stale_inner_caches_cannot_launder_into_live(
            self, market_world):
        mls, ft, state = market_world
        books, meta = mls.find_all_books_with_freshness(
            self.DATE, self.HOME, self.AWAY)
        assert meta["status"] == "live" and books
        # provider dies; every inner ttl expires; the bundle REBUILDS
        # from stale inner caches — the old code stamped that live/age 0
        state["up"] = False
        ft.now += 200
        books, meta = mls.find_all_books_with_freshness(
            self.DATE, self.HOME, self.AWAY)
        assert meta["status"] == "stale_fallback"     # NOT live
        assert meta["age_seconds"] >= 200             # oldest constituent
        assert books                                  # served, labelled
        # beyond the ceiling: fail closed, no price presented at all
        ft.now += 600
        books, meta = mls.find_all_books_with_freshness(
            self.DATE, self.HOME, self.AWAY)
        assert meta["status"] == "unavailable"
        assert books == []
        assert meta["age_seconds"] >= 700
        assert meta["observed_at"] is not None

    def test_briefing_composes_the_real_price_path(
            self, live_session, market_world, monkeypatch):
        """Seeded caches older than MLS_PRICE_MAX_AGE_SECONDS + every
        provider fetch failing -> the REAL briefing path answers
        unavailable, empty current books, no live label."""
        mls, ft, state = market_world
        _seed(live_session)
        monkeypatch.setattr(mls, "match_summary", lambda eid: {
            "date": self.DATE, "home": {"name": self.HOME},
            "away": {"name": self.AWAY}})
        from src.live import runs as live_runs
        monkeypatch.setattr(live_runs, "model_for_event",
                            lambda eid: {}, raising=False)
        from src.live import journal
        out = journal.briefing("e1")
        assert out["market_current"]["status"] == "live"
        state["up"] = False
        ft.now += 700          # past MLS_PRICE_MAX_AGE_SECONDS (600)
        out = journal.briefing("e1")
        cur = out["market_current"]
        assert cur["status"] == "unavailable"
        assert cur["basis"] != "live_read"
        assert cur["books"] == []
        assert cur["age_seconds"] >= 700

    def test_price_ceiling_must_exceed_inner_ttls(self, monkeypatch):
        """journal-P0-B: the ceiling is validated at startup against
        the price-path TTL table — an incoherent config fails fast."""
        from src import mls
        monkeypatch.setattr(config, "MLS_PRICE_MAX_AGE_SECONDS", 60)
        with pytest.raises(ValueError):
            mls._validate_price_freshness_config()
        monkeypatch.setattr(config, "MLS_PRICE_MAX_AGE_SECONDS", 600)
        mls._validate_price_freshness_config()


class TestPriceFreshness:
    """journal-P0 F4: a stale price must never wear a live label. The
    cache may serve a fallback — WITH its age — and past the TTL it
    fails closed. Plus the briefing half of F3: the payload carries the
    persisted, citable quote ids."""

    class _FakeTime:
        def __init__(self):
            self.now = 1000.0

        def monotonic(self):
            return self.now

        def sleep(self, _s):
            pass

    def test_cached_price_past_ttl_with_failed_refresh_fails_closed(
            self, monkeypatch):
        """Cache a price, advance the clock, make refresh fail: within
        max_age the answer is a LABELLED fallback with its age; past it
        the answer is no price at all."""
        from src import mls
        ft = self._FakeTime()
        monkeypatch.setattr(mls, "time", ft)
        mls._cache.clear()
        data, meta = mls.cached_with_freshness(
            "k", 30, lambda: {"yes_ask": "0.45"}, 600)
        assert meta["status"] == "live" and meta["age_seconds"] == 0
        ft.now += 100                      # past ttl; refresh now fails
        data, meta = mls.cached_with_freshness("k", 30, lambda: None,
                                               600)
        assert data == {"yes_ask": "0.45"}
        assert meta["status"] == "stale_fallback"   # NOT live
        assert meta["age_seconds"] == 100
        assert meta["observed_at"] is not None
        ft.now += 600                      # past max_age: fail CLOSED
        data, meta = mls.cached_with_freshness("k", 30, lambda: None,
                                               600)
        assert data is None
        assert meta["status"] == "unavailable"
        assert meta["age_seconds"] == 700
        mls._cache.clear()

    def _briefing(self, monkeypatch, meta, books):
        from src import mls
        from src.live import runs as live_runs
        monkeypatch.setattr(mls, "match_summary", lambda eid: {
            "date": "2026-07-28T23:30Z",
            "home": {"name": "FC Cincinnati"},
            "away": {"name": "Columbus Crew"}})
        monkeypatch.setattr(mls, "find_all_books_with_freshness",
                            lambda *a, **kw: (books, meta),
                            raising=False)
        monkeypatch.setattr(live_runs, "model_for_event",
                            lambda eid: {}, raising=False)
        from src.live import journal
        return journal.briefing("e1")

    def test_briefing_never_labels_a_stale_book_live(self, live_session,
                                                     monkeypatch):
        _seed(live_session)
        out = self._briefing(
            monkeypatch,
            {"observed_at": "2026-07-28T00:00:00+00:00",
             "age_seconds": 120, "status": "stale_fallback"},
            [{"key": "winner", "markets": []}])
        cur = out["market_current"]
        assert cur["basis"] == "cached_read"        # NOT live_read
        assert cur["status"] == "stale_fallback"
        assert cur["age_seconds"] == 120
        assert cur["observed_at"] is not None
        assert "NOT a live read" in cur["note"]

    def test_briefing_fails_closed_when_the_price_is_too_old(
            self, live_session, monkeypatch):
        _seed(live_session)
        out = self._briefing(
            monkeypatch,
            {"observed_at": "2026-07-27T00:00:00+00:00",
             "age_seconds": 90000, "status": "unavailable"},
            [{"key": "winner", "markets": [{"yes_ask": "0.45"}]}])
        cur = out["market_current"]
        assert cur["basis"] == "unavailable"
        assert cur["status"] == "unavailable"
        assert cur["books"] == []      # no price presented as current
        assert cur["age_seconds"] == 90000

    def test_briefing_carries_citable_quote_ids(self, live_session,
                                                monkeypatch):
        """journal-P0 F3: the documented happy path — 'cite
        market_quote_id from the briefing' — must produce ids the
        server accepts, for both the frozen and the persisted book."""
        _seed(live_session)
        live_session.add(PredictionRun(
            id="lock1", fixture_id=1, run_type="t10", canonical=True,
            status="complete", captured_at=datetime.now(UTC)))
        live_session.flush()
        live_session.add(PredictionContract(
            prediction_run_id="lock1", market_contract_id=1,
            market_quote_id=1, outcome_key="home_win",
            raw_probability=0.52))
        live_session.commit()
        out = self._briefing(monkeypatch,
                             {"observed_at": None, "age_seconds": None,
                              "status": "unavailable"}, [])
        frozen = out["market_frozen_t10"]["contracts"]
        assert frozen and frozen[0]["market_quote_id"] == 1
        assert frozen[0]["quote_captured_at"] is not None
        quotes = out["market_persisted"]["quotes"]
        assert quotes and quotes[0]["market_quote_id"] == 1
        assert quotes[0]["captured_at"] is not None
        assert "age_seconds" in quotes[0] and "status" in quotes[0]
        # and the id the briefing hands out is one record_view ACCEPTS
        from src.live import journal
        bet = journal.record_view(
            1, quotes[0]["ticker"], outcome_key=quotes[0]["outcome_key"],
            market_quote_id=quotes[0]["market_quote_id"])
        assert bet["price_basis"] == "observed_quote"


class TestJsonBodies:
    """journal-P1 F8: mutation payloads travel as typed JSON bodies —
    the query string URL-decodes, so a rationale containing `&`, `%`,
    newlines or Unicode was mangled in the one table whose whole value
    is verbatim provenance. Plus the API half of P0 F5: consent is
    operator-supplied, never manufactured server-side."""

    HDRS = {"X-Admin-Token": "s3cret"}

    @pytest.fixture()
    def client(self, live_session, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        from fastapi.testclient import TestClient
        from api import main as api_main
        api_main._rate_last.clear()
        return TestClient(api_main.app)

    def test_a_hostile_rationale_round_trips_exactly(self, live_session,
                                                     client):
        _seed(live_session)
        rationale = ("draw looks rich & CIN pressing high?\n"
                     "2nd half: ünïcode ✓ 100% — \"quotes\" 'single' "
                     "%26%3D literal\nfinal line")
        r = client.post("/api/admin/mls/journal/view", headers=self.HDRS,
                        json={"fixture_id": 1,
                              "market_ticker": "KXMLSGAME-x-H",
                              "outcome_key": "home_win",
                              "stated_price": "0.45",
                              "market_quote_id": 1,
                              "rationale": rationale})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rationale"] == rationale
        stored = live_session.get(PersonalBet, body["id"])
        assert stored.rationale == rationale

    def test_consent_is_operator_supplied_never_fabricated(
            self, live_session, client):
        """The route used to stamp consent_recorded_at=utcnow() —
        manufacturing the provenance of a third party's consent. Absent
        consent is refused; supplied consent is stored VERBATIM, not
        replaced with the server clock."""
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        now = datetime.now(UTC)
        payload = {"bet_id": bet["id"], "account_label": "friend-A",
                   "fill_price": "0.47", "filled_contracts": "10",
                   "fee_paid": "0.12",
                   "filled_at": now.isoformat(),
                   "exchange_order_id": "ORD-API-1"}
        r = client.post("/api/admin/mls/journal/execution",
                        headers=self.HDRS, json=payload)
        assert r.status_code == 422        # consent absent -> refused
        supplied = (now - timedelta(hours=6)).replace(microsecond=0)
        payload["consent_recorded_at"] = supplied.isoformat()
        ok = client.post("/api/admin/mls/journal/execution",
                         headers=self.HDRS, json=payload)
        assert ok.status_code == 200, ok.text
        from src.live.models import PersonalBetExecution as PBE
        stored = live_session.get(PBE, ok.json()["id"])
        got = stored.consent_recorded_at
        got = got if got.tzinfo else got.replace(tzinfo=UTC)
        assert got == supplied             # verbatim, hours from now()

    def test_settlement_and_reconcile_routes_write_the_columns(
            self, live_session, client):
        """journal-P0 F5: the settlement/reconciliation columns get an
        authenticated writer."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        # whole-second for exact isoformat round-trips, rounded UP —
        # rounding down would put the fill before the taken moment,
        # which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-API-2")
        st = client.post("/api/admin/mls/journal/settlement",
                         headers=self.HDRS,
                         json={"execution_id": ex["id"],
                               "settlement_credit": "10",
                               "settled_at": now.isoformat(),
                               "settled_outcome": "home_win"})
        assert st.status_code == 200, st.text
        assert st.json()["settlement_credit_dollars"] == "10"
        rc = client.post("/api/admin/mls/journal/reconcile",
                         headers=self.HDRS,
                         json={"execution_id": ex["id"],
                               "note": "matches statement",
                               "publication_consent": True})
        assert rc.status_code == 200, rc.text
        live_session.expire_all()
        stored = live_session.get(PBE, ex["id"])
        assert stored.settlement_credit_dollars == "10"
        assert stored.settled_at is not None
        assert stored.reconciled is True
        assert stored.reconciliation_note == "matches statement"
        assert stored.publication_consent is True
        assert live_session.get(PersonalBet,
                                bet["id"]).settled_outcome == "home_win"

    def test_broadcast_rides_a_json_body(self, live_session, client,
                                         monkeypatch, session_cap):
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        _seed(live_session)
        msg = "draw & over 2.5 both moved\nsecond line ✓"
        r = client.post("/api/admin/mls/broadcast",
                        headers=session_cap["headers"],
                        json={"message": msg, "channel": "action",
                              "fixture_id": 1, "session_label": "live"})
        assert r.status_code == 200, r.text
        assert r.json()["dispatched"] is True
        from src.live import journal
        assert journal.recent_broadcasts(1)[0]["message"] == msg


class TestPublicRedaction:
    """journal-P0 F2 + P0-C: the briefing, journal and corpus routes
    are UNAUTHENTICATED, and an execution row is a third party's
    financial record. ONE public projection serves every surface; the
    person, the sizing, the money and the broadcast prose never ride
    public bytes."""

    HDRS = {"X-Admin-Token": "s3cret"}
    RATIONALE = "RATIONALE-SENTINEL private prose"
    ACCT = "ACCT-SENTINEL"
    ORDER = "ORD-SENTINEL-1"
    SIZE = "76543.21"
    CONSENT_ISO = "2026-07-01T12:00:00+00:00"
    BCAST = "BROADCAST-SENTINEL he filled ten at 0.43"
    PRIVATE_EXEC_FIELDS = (
        "account_label", "consent_recorded_at", "exchange_order_id",
        "fill_price_dollars", "filled_contracts", "fee_paid_dollars",
        "best_available_price_dollars", "settlement_credit_dollars",
        "reconciliation_note")
    SENTINELS = (RATIONALE, ACCT, ORDER, SIZE, CONSENT_ISO, BCAST,
                 "9.87", "7.77", "RECON-SENTINEL")

    @pytest.fixture()
    def client(self, live_session, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
        from src import mls
        mls._cache.clear()                 # route-level caches
        from fastapi.testclient import TestClient
        from api import main as api_main
        api_main._rate_last.clear()
        return TestClient(api_main.app)

    def _record_full(self, live_session, monkeypatch, session_cap):
        """A journal entry + settled, reconciled REAL execution + a
        broadcast, every private field carrying a sentinel.
        publication_consent False."""
        import src.alerts as alerts
        from src.live import journal
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": True})
        _seed(live_session)
        fx = live_session.get(Fixture, 1)
        fx.espn_event_id = "4013212"       # route requires digits
        live_session.commit()
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45",
                                  stated_size=self.SIZE,
                                  market_quote_id=1,
                                  rationale=self.RATIONALE)
        journal.resolve_view(bet["id"], "taken")
        now = datetime.now(UTC)
        ex = journal.record_execution(
            bet["id"], self.ACCT,
            consent_recorded_at=datetime.fromisoformat(self.CONSENT_ISO),
            fill_price="0.43", filled_contracts="10", fee_paid="9.87",
            filled_at=now, market_quote_id_at_fill=1,
            exchange_order_id=self.ORDER)
        journal.settle_execution(ex["id"], settlement_credit="7.77",
                                 settled_at=now,
                                 settled_outcome="home_win")
        journal.reconcile_execution(ex["id"], note="RECON-SENTINEL")
        journal.broadcast(self.BCAST, channel="action", fixture_id=1,
                          capability=session_cap["capability"])
        return bet, ex

    def _mock_providers(self, monkeypatch):
        from src import mls
        from src.live import runs as live_runs
        monkeypatch.setattr(mls, "match_summary", lambda eid: {
            "date": "2026-07-28T23:30Z", "home": {"name": "A"},
            "away": {"name": "B"}})
        monkeypatch.setattr(mls, "find_all_books_with_freshness",
                            lambda *a, **kw:
                            ([], {"observed_at": None,
                                  "age_seconds": None,
                                  "status": "unavailable"}),
                            raising=False)
        monkeypatch.setattr(live_runs, "model_for_event",
                            lambda eid: {}, raising=False)

    def test_public_briefing_redacts_field_by_field(self, live_session,
                                                    client, monkeypatch,
                                                    session_cap):
        self._record_full(live_session, monkeypatch, session_cap)
        self._mock_providers(monkeypatch)
        r = client.get("/api/mls/briefing/4013212")
        assert r.status_code == 200, r.text
        body = r.json()
        entries = body["journal"]
        assert entries, "briefing lost the journal section"
        for entry in entries:
            assert "rationale" not in entry
            assert "stated_size" not in entry
            # executions surface as a COUNT, not linkable rows
            assert entry["executions"] == {"count": 1}
        # broadcast prose never rides the public briefing
        assert body["said_already"] == {
            "count": 1,
            "note": ("broadcast content is operator-authenticated — "
                     "GET /api/admin/mls/broadcasts?fixture_id=...")}
        for sentinel in self.SENTINELS:
            assert sentinel not in r.text, sentinel

    def test_public_journal_summary_carries_no_private_values(
            self, live_session, client, monkeypatch, session_cap):
        self._record_full(live_session, monkeypatch, session_cap)
        r = client.get("/api/mls/journal")
        assert r.status_code == 200
        for sentinel in self.SENTINELS:
            assert sentinel not in r.text, sentinel

    def test_corpus_preview_and_published_redact(self, live_session,
                                                 client, monkeypatch,
                                                 session_cap):
        from src.live import corpus
        self._record_full(live_session, monkeypatch, session_cap)
        r = client.get("/api/mls/corpus?preview=1&full=1")
        assert r.status_code == 200, r.text
        bundle = r.json()
        bets = bundle["sections"]["personal_journal.json"]
        assert bets
        for row in bets:
            assert "rationale" not in row
            assert "stated_size" not in row
        # absent consent, the execution contributes NOTHING to public
        # bytes — and the omission is explicit in the manifest counts
        assert bundle["sections"]["personal_journal_executions.json"] \
            == []
        counts = bundle["manifest"]["counts"]
        assert counts["journal_executions_total"] == 1
        assert counts["journal_executions_published"] == 0
        for sentinel in self.SENTINELS:
            assert sentinel not in r.text, sentinel
        # the PUBLISHED corpus (immutable bytes) is equally clean
        corpus.publish_corpus("t-priv-1")
        pub = client.get("/api/mls/corpus?version=t-priv-1&full=1")
        assert pub.status_code == 200
        for sentinel in self.SENTINELS:
            assert sentinel not in pub.text, sentinel

    def test_publication_consent_releases_the_execution_fields(
            self, live_session, monkeypatch, session_cap):
        """The explicit gate: with consent set on the ROW, the corpus
        carries the full execution record; the bet's rationale and
        sizing still never travel."""
        from src.live import corpus
        from src.live.models import PersonalBetExecution as PBE
        _bet, ex = self._record_full(live_session, monkeypatch,
                                     session_cap)
        row = live_session.get(PBE, ex["id"])
        row.publication_consent = True
        live_session.commit()
        bundle = corpus.build_corpus()
        execs = bundle["sections"]["personal_journal_executions.json"]
        assert execs[0]["account_label"] == self.ACCT
        assert execs[0]["exchange_order_id"] == self.ORDER
        bets = bundle["sections"]["personal_journal.json"]
        assert all("rationale" not in b and "stated_size" not in b
                   for b in bets)

    def test_the_private_field_list_is_enforced_not_decorative(
            self, live_session, monkeypatch, session_cap):
        """`_PRIVATE_EXECUTION_FIELDS` sits beside the privacy boundary
        and reads like a guarantee. Assert it against the REAL redacted
        projection so that it is one — round 3 added fields derived from
        the private economics (fill-book age, payload hashes), and a
        list nobody checks would have quietly fallen behind them."""
        from src.live import journal
        self._record_full(live_session, monkeypatch, session_cap)
        entries = journal.open_entries(live_session, 1)   # redacted
        assert entries
        for entry in entries:
            for field in journal._PRIVATE_EXECUTION_FIELDS:
                assert field not in entry, field
            assert set(entry["executions"]) == {"count"}
        # and each named field really is on the operator record, so the
        # list cannot pass by naming fields that do not exist
        full = journal.full_entries(1)[0]["executions"][0]
        for field in journal._PRIVATE_EXECUTION_FIELDS:
            assert field in full, field

    def test_operator_surface_keeps_the_full_record(self, live_session,
                                                    client, monkeypatch,
                                                    session_cap):
        self._record_full(live_session, monkeypatch, session_cap)
        denied = client.get("/api/admin/mls/journal")
        assert denied.status_code == 403
        r = client.get("/api/admin/mls/journal", headers=self.HDRS)
        assert r.status_code == 200
        entry = r.json()["entries"][0]
        assert entry["rationale"] == self.RATIONALE
        assert entry["stated_size"] == self.SIZE
        ex = entry["executions"][0]
        assert ex["account_label"] == self.ACCT
        assert ex["exchange_order_id"] == self.ORDER
        assert ex["fill_price_dollars"] == "0.43"
        assert ex["settlement_credit_dollars"] == "7.77"
        assert "gaps" in ex
        # the broadcast thread lives behind the token too
        no = client.get("/api/admin/mls/broadcasts?fixture_id=1")
        assert no.status_code == 403
        b = client.get("/api/admin/mls/broadcasts?fixture_id=1",
                       headers=self.HDRS)
        assert b.status_code == 200
        msgs = [x["message"] for x in b.json()["broadcasts"]]
        assert self.BCAST in msgs


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

    def test_the_marker_survives_reaching_the_minimum(self,
                                                      live_session):
        """journal-P1 F7, at exactly n=20: crossing the floor must not
        silently drop the marker. Aggregation is DELIBERATELY not
        implemented — pre-specified metrics do not exist yet — and the
        summary says so explicitly rather than implying a green light."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        now = datetime.now(UTC)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45",
                                  market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        for i in range(journal.MIN_EXECUTIONS_FOR_AGGREGATE):
            live_session.add(PBE(
                personal_bet_id=bet["id"], account_label="friend-A",
                consent_recorded_at=now, status="filled",
                fill_price_dollars="0.45", filled_contracts="10",
                fee_paid_dollars="0.10", filled_at=now,
                settlement_credit_dollars="10", settled_at=now,
                exchange_order_id=f"ORD-N{i}"))
        live_session.commit()
        out = journal.journal_summary()
        assert out["executions"]["settled"] == \
            journal.MIN_EXECUTIONS_FOR_AGGREGATE
        marker = out["aggregate_withheld"]        # never disappears
        assert marker["reason"] == (
            "aggregation deliberately not implemented — "
            "pre-specified metrics not yet defined")
        assert marker["settled_executions"] == \
            journal.MIN_EXECUTIONS_FOR_AGGREGATE
        # and still no synthetic statistic anywhere
        for k in ("mean_slippage", "roi_pct", "hit_rate",
                  "mean_fee_delta"):
            assert k not in out


class TestCorpusScoping:
    """journal-P1 F9: the journal sections scope to the corpus's
    competition like every other section, and /api/mls/journal reports
    the MLS journal only."""

    def _seed_two_competitions(self, s):
        from src.live import journal
        _seed(s)                                       # mls-2026, fx 1
        s.add(Competition(slug="epl-2026", name="EPL", season=2026))
        fx2 = Fixture(id=2, competition_slug="epl-2026",
                      espn_event_id="e2", status="pre",
                      current_kickoff_utc=datetime.now(UTC)
                      + timedelta(hours=3))
        ev2 = MarketEvent(id=2, competition_slug="epl-2026",
                          kalshi_event_ticker="KXEPLGAME-z",
                          series="KXEPLGAME", fixture_id=2,
                          mapping_approved=True)
        mc2 = MarketContract(id=2, market_event_id=2,
                             ticker="KXEPLGAME-z-H",
                             outcome_key="home_win")
        q2 = MarketQuote(id=5, market_contract_id=2,
                         captured_at=datetime.now(UTC)
                         - timedelta(minutes=1),
                         yes_ask_c=50, yes_bid_c=48)
        s.add_all([fx2, ev2, mc2, q2])
        s.commit()
        mls_bet = journal.record_view(1, "KXMLSGAME-x-H",
                                      outcome_key="home_win",
                                      market_quote_id=1)
        epl_bet = journal.record_view(2, "KXEPLGAME-z-H",
                                      outcome_key="home_win",
                                      market_quote_id=5)
        journal.resolve_view(mls_bet["id"], "taken")
        journal.resolve_view(epl_bet["id"], "taken")
        now = datetime.now(UTC)
        for b, order in ((mls_bet, "ORD-MLS"), (epl_bet, "ORD-EPL")):
            # consent granted on BOTH so the scoping test below proves
            # the competition filter, not the consent gate (P0-C)
            journal.record_execution(
                b["id"], "friend-A", consent_recorded_at=now,
                fill_price="0.47", filled_contracts="10",
                fee_paid="0.12", filled_at=now, exchange_order_id=order,
                publication_consent=True)
        return mls_bet, epl_bet

    def test_the_mls_corpus_exports_only_mls_journal_rows(
            self, live_session):
        from src.live import corpus
        mls_bet, epl_bet = self._seed_two_competitions(live_session)
        bundle = corpus.build_corpus()
        assert bundle["manifest"]["schema_version"] == "corpus-v5"
        bets = bundle["sections"]["personal_journal.json"]
        assert [b["id"] for b in bets] == [mls_bet["id"]]
        assert all(b["competition_slug"] == "mls-2026" for b in bets)
        execs = bundle["sections"]["personal_journal_executions.json"]
        assert [e["personal_bet_id"] for e in execs] == [mls_bet["id"]]

    def test_journal_summary_scopes_to_the_competition(self,
                                                       live_session):
        from src.live import journal
        self._seed_two_competitions(live_session)
        out = journal.journal_summary(competition_slug="mls-2026")
        assert out["total_recorded"] == 1
        assert out["executions"]["total"] == 1
        everything = journal.journal_summary(competition_slug=None)
        assert everything["total_recorded"] == 2


class TestCorrectionScope:
    """journal-P1-F: a correction restates the SAME observation — same
    competition, fixture, market and outcome — and supersedes it.
    Chains are linear, aggregates count ONE effective observation per
    chain, and the corrected rows remain as immutable audit. Round 1
    validated corrects_bet_id for existence only, so a 'correction'
    could point across fixtures or competitions (a dangling reference
    in the scoped corpus) and journal_summary counted corrected rows as
    independent observations."""

    def _taken(self, journal, ticker="KXMLSGAME-x-H", quote=1,
               fixture=1):
        bet = journal.record_view(fixture, ticker,
                                  outcome_key="home_win",
                                  stated_price="0.45",
                                  market_quote_id=quote)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def test_a_cross_fixture_correction_is_rejected(self, live_session):
        from src.live import journal
        _seed(live_session)
        _seed_other_fixture(live_session)
        bet = self._taken(journal)
        r = journal.record_view(2, "KXMLSGAME-y-H",
                                outcome_key="home_win",
                                market_quote_id=5,
                                corrects_bet_id=bet["id"])
        assert "error" in r and "out of scope" in r["error"]
        assert "fixture" in r["error"]
        assert live_session.query(PersonalBet).count() == 1

    def test_a_cross_market_or_outcome_correction_is_rejected(
            self, live_session):
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        r = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="away_win",
                                corrects_bet_id=bet["id"])
        assert "error" in r and "outcome_key" in r["error"]
        r = journal.record_view(1, "KXMLSGAME-x-T",
                                outcome_key="home_win",
                                corrects_bet_id=bet["id"])
        assert "error" in r and "market_ticker" in r["error"]
        assert live_session.query(PersonalBet).count() == 1

    def test_a_cross_competition_correction_cannot_dangle_in_the_corpus(
            self, live_session):
        """The scoped corpus is why the scope check exists: an MLS
        export must never carry a corrects_bet_id its own file cannot
        resolve."""
        from src.live import corpus, journal
        _seed(live_session)
        live_session.add_all([
            Competition(slug="epl-2026", name="EPL", season=2026),
            Fixture(id=2, competition_slug="epl-2026",
                    espn_event_id="e2", status="pre",
                    current_kickoff_utc=datetime.now(UTC)
                    + timedelta(hours=3))])
        live_session.commit()
        bet = self._taken(journal)
        r = journal.record_view(2, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                corrects_bet_id=bet["id"])
        assert "error" in r and "competition" in r["error"]
        assert live_session.query(PersonalBet).count() == 1
        bundle = corpus.build_corpus()
        rows = bundle["sections"]["personal_journal.json"]
        exported_ids = {b["id"] for b in rows}
        for b in rows:
            assert (b["corrects_bet_id"] is None
                    or b["corrects_bet_id"] in exported_ids)

    def test_a_chain_counts_once_everywhere(self, live_session):
        """taken, corrected to passed = TWO immutable rows and ONE
        effective pass — in the summary, the corpus and the public
        entry list alike."""
        from src.live import corpus, journal
        _seed(live_session)
        a = self._taken(journal)
        b = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                stated_price="0.45", market_quote_id=1,
                                rationale="mis-resolved; he passed",
                                corrects_bet_id=a["id"])
        journal.resolve_view(b["id"], "passed")
        # two immutable rows — the mistake is preserved, not rewritten
        assert live_session.query(PersonalBet).count() == 2
        assert live_session.get(PersonalBet, a["id"]).status == "taken"
        out = journal.journal_summary()
        assert out["total_recorded"] == 2
        assert out["effective_recorded"] == 1
        assert out["superseded"] == 1
        assert out["counts"]["taken"] == 0
        assert out["counts"]["passed"] == 1
        assert out["countable"] == 1
        bundle = corpus.build_corpus()
        rows = {x["id"]: x
                for x in bundle["sections"]["personal_journal.json"]}
        assert len(rows) == 2
        assert rows[a["id"]]["superseded"] is True
        assert rows[a["id"]]["counts_toward_aggregate"] is False
        assert rows[b["id"]]["superseded"] is False
        assert rows[b["id"]]["counts_toward_aggregate"] is True
        counts = bundle["manifest"]["counts"]
        assert counts["journal_entries"] == 2
        assert counts["journal_entries_effective"] == 1
        assert counts["journal_entries_superseded"] == 1
        entries = {e["id"]: e
                   for e in journal.open_entries(live_session, 1)}
        assert entries[a["id"]]["superseded"] is True
        assert entries[b["id"]]["superseded"] is False

    def test_chains_are_linear_correct_the_head(self, live_session):
        from src.live import journal
        _seed(live_session)
        a = self._taken(journal)
        b = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                market_quote_id=1,
                                corrects_bet_id=a["id"])
        r = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                market_quote_id=1,
                                corrects_bet_id=a["id"])
        assert "error" in r
        assert "already corrected" in r["error"]
        assert str(b["id"]) in r["error"]
        assert live_session.query(PersonalBet).count() == 2
        # correcting the HEAD extends the chain; still one effective
        c = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                market_quote_id=1,
                                corrects_bet_id=b["id"])
        assert "error" not in c
        out = journal.journal_summary()
        assert out["total_recorded"] == 3
        assert out["effective_recorded"] == 1


class TestBroadcast:
    def test_a_broadcast_is_persisted_even_if_dispatch_fails(
            self, live_session, monkeypatch, session_cap):
        from src.live import journal
        import src.alerts as alerts
        _seed(live_session)

        def boom(*a, **kw):
            raise RuntimeError("discord down")
        monkeypatch.setattr(alerts, "send_alert", boom)
        r = journal.broadcast("CIN drifted to 0.34", fixture_id=1,
                              session_label="trivela-live",
                              capability=session_cap["capability"])
        assert r["dispatched"] is False
        assert journal.recent_broadcasts(1)[0]["message"] == \
            "CIN drifted to 0.34"

    def test_a_reopened_session_can_read_what_it_already_said(
            self, live_session, monkeypatch, session_cap):
        from src.live import journal
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": True})
        _seed(live_session)
        cap = session_cap["capability"]
        journal.broadcast("first", fixture_id=1, capability=cap)
        journal.broadcast("second", fixture_id=1, capability=cap)
        said = journal.recent_broadcasts(1)
        assert [b["message"] for b in said] == ["second", "first"]
        # journal-P0-G: the record names the capability the server
        # VERIFIED, not merely the source the caller claimed
        assert all(b["dispatch_capability_id"] == cap.id for b in said)


class TestWirePayloadIntegrity:
    """journal-P0-A, composed path: REAL broadcast -> REAL send_alert ->
    fake network. The transports truncate at TRANSPORT_MESSAGE_LIMIT,
    so the qualifier's space is reserved and the PROSE is truncated —
    and the stored dispatch record is the byte-exact wire payload."""

    @pytest.fixture()
    def wire(self, monkeypatch):
        """Fake network capturing exactly what each transport POSTs."""
        import src.alerts as alerts
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL",
                            "https://fake/discord-action")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL",
                            "https://fake/discord-detail")
        monkeypatch.setattr(config, "NTFY_TOPIC", "fake-topic")
        captured = {}

        class _Resp:
            status_code = 200

        def fake_post(url, json=None, data=None, headers=None,
                      timeout=None):
            if "discord-action" in url:
                captured["discord_action"] = json["content"]
            elif "discord-detail" in url:
                captured["discord_detail"] = json["content"]
            elif "ntfy" in url:
                captured["ntfy"] = data.decode("utf-8")
            return _Resp()

        monkeypatch.setattr(alerts.requests, "post", fake_post)
        return captured

    def test_oversized_action_message_keeps_the_full_qualifier(
            self, live_session, wire, session_cap):
        """3000 chars of prose through the REAL transport wrappers:
        both Discord and ntfy receive the COMPLETE qualifier, the prose
        is what shrank, and the stored record IS the wire payload."""
        import hashlib as _h
        from src import alerts
        from src.live import journal
        _seed(live_session)
        prose = "CIN drifting hard, book thinning. " * 100   # ~3400
        assert len(prose) > alerts.TRANSPORT_MESSAGE_LIMIT
        r = journal.broadcast(prose, channel="action", fixture_id=1,
                              capability=session_cap["capability"])
        assert r["dispatched"] is True
        assert r["payload_truncated"] is True
        # every transport got the same, complete payload
        assert set(wire) == {"discord_action", "discord_detail", "ntfy"}
        for name, payload in wire.items():
            assert len(payload) <= alerts.TRANSPORT_MESSAGE_LIMIT, name
            assert "[shadow]" in payload, name
            assert "not a real-money signal" in payload, name
            assert ("not significant" in payload
                    or "no established edge" in payload), name
            assert "…[truncated" in payload, name
        assert wire["discord_action"] == wire["ntfy"]
        # the stored dispatch record matches the actual wire bytes
        said = journal.recent_broadcasts(1)[0]
        assert said["dispatched_body"] == wire["discord_action"]
        assert said["dispatched_sha256"] == _h.sha256(
            wire["discord_action"].encode("utf-8")).hexdigest()
        assert said["dispatched_sha256"] == r["dispatched_sha256"]
        # the operator's full prose is preserved in the journal record
        assert said["message"] == prose

    def test_normal_sized_prose_is_not_truncated(self, live_session,
                                                 wire, session_cap):
        from src.live import journal
        _seed(live_session)
        r = journal.broadcast("CIN 0.31, book thin", channel="action",
                              fixture_id=1,
                              capability=session_cap["capability"])
        assert r["payload_truncated"] is False
        assert "CIN 0.31, book thin" in wire["discord_action"]
        assert "[shadow]" in wire["discord_action"]
        assert "…[truncated" not in wire["discord_action"]


class TestBroadcastBoundary:
    """journal-P0 F1: the action channel reaches a human whose friend
    bets real money. Only operator-authenticated session prose may
    dispatch; the lock governs model-generated signals, so a computed or
    automated caller is refused mechanically — and every action dispatch
    carries the standing-edge qualifier so no number travels without its
    uncertainty."""

    def test_session_action_broadcast_dispatches_with_the_qualifier(
            self, live_session, monkeypatch, session_cap):
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
                              source="session", fixture_id=1,
                              capability=session_cap["capability"])
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
                                             monkeypatch, session_cap):
        """Refused even holding a VALID capability — the source check and
        the origin check are independent gates, not one gate spelled
        twice."""
        from src.live import journal
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        _seed(live_session)
        r = journal.broadcast("flip: exit > hold", source="computed",
                              fixture_id=1,
                              capability=session_cap["capability"])
        assert r.get("refused") is True
        assert r["dispatched"] is False
        assert "error" in r
        assert sent == []                    # nothing reached a transport
        # and nothing was persisted as "said" — it never was
        assert journal.recent_broadcasts(1) == []

    def test_a_scheduler_frame_is_refused_even_claiming_session(
            self, live_session, monkeypatch, session_cap):
        """The stack check: source="session" is verified against the
        call stack, not taken on the caller's word — and holding a real
        capability does not buy a scheduler frame a dispatch."""
        from src.live import journal
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": True})
        _seed(live_session)
        code = compile(
            "result = journal.broadcast('auto', source='session', "
            "fixture_id=1, capability=cap)", "<scheduler>", "exec")
        g = {"__name__": "jobs.scheduler", "journal": journal,
             "cap": session_cap["capability"]}
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
                                                    monkeypatch,
                                                    session_cap):
        """journal-P1 F6: dispatched=true only when a transport ACCEPTED;
        all-fail reports false; the accepting transports are named."""
        from src.live import journal
        import src.alerts as alerts
        _seed(live_session)
        cap = session_cap["capability"]
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": False,
                                             "ntfy": True})
        r = journal.broadcast("one leg down", fixture_id=1, capability=cap)
        assert r["dispatched"] is True
        assert r["accepted"] == ["ntfy"]
        assert r["transports"] == {"discord_action": False, "ntfy": True}
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: {"discord_action": False,
                                             "ntfy": False})
        r2 = journal.broadcast("all legs down", fixture_id=1,
                               capability=cap)
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


# ===========================================================================
# Round 3 — the three confirmed P0s, each exercised on the COMPOSED path.
#
# The recurring failure across rounds 1 and 2 was that a fix stopped one
# layer short of the boundary and the test checked the layer above the
# defect. So: the origin tests go over HTTP with a real TestClient, the
# quote-age tests go through record_view/record_execution AND through the
# read surfaces that consume them, and the idempotency tests assert the
# HTTP status code AND the database afterwards.
# ===========================================================================

def _open_session_over_http(client, admin_headers):
    """Walk the REAL handshake through the REAL routes and return the
    headers an authorised interactive session would send."""
    ch = client.post("/api/admin/mls/session/challenge",
                     headers=admin_headers)
    assert ch.status_code == 200, ch.text
    from src.live import session_capability as sc
    op = client.post("/api/admin/mls/session/open", headers=admin_headers,
                     json={"challenge_id": ch.json()["challenge_id"],
                           "response": sc.expected_response(
                               ch.json()["nonce"]),
                           "session_label": "trivela-live"})
    assert op.status_code == 200, op.text
    return ({**admin_headers,
             "X-Session-Token": op.json()["session_token"]},
            op.json())


class TestBroadcastOriginIsVerified:
    """journal-P0-G: `source="session"` must be a fact the SERVER can
    check, not a string the client sends.

    Round 2 added a call-stack check, which is real — but it only ever
    bound in-process callers. Over HTTP the stack is uvicorn -> fastapi
    -> api.main and contains no guarded frame, so any automated client
    holding the generic operator token could POST source="session" and
    reach the Discord/ntfy channel Son's friend reads before placing
    real money. Every test here goes through the HTTP route.
    """

    ADMIN = {"X-Admin-Token": "s3cret"}

    @pytest.fixture()
    def client(self, live_session, monkeypatch):
        from src.live import session_capability as sc
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        monkeypatch.setattr(config, "JOURNAL_SESSION_SECRET",
                            "second-factor-not-the-admin-token")
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
        sc._reset_for_tests()
        from fastapi.testclient import TestClient
        from api import main as api_main
        api_main._rate_last.clear()
        yield TestClient(api_main.app)
        sc._reset_for_tests()

    @pytest.fixture()
    def transports(self, monkeypatch):
        """Records every message that reached a transport. Empty is the
        assertion that matters: a refusal must not merely report
        failure, it must never call one."""
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        return sent

    def test_a_generic_admin_client_cannot_dispatch(
            self, live_session, client, transports):
        """THE defect. A valid operator token, source="session", and no
        interactive-session capability: refused, no transport touched,
        and nothing persisted as having been said."""
        from src.live import journal
        _seed(live_session)
        r = client.post("/api/admin/mls/broadcast", headers=self.ADMIN,
                        json={"message": "CIN value at 0.31",
                              "channel": "action", "source": "session",
                              "fixture_id": 1})
        assert r.status_code == 403, r.text
        assert transports == []
        assert journal.recent_broadcasts(1) == []

    def test_an_authorised_interactive_session_can_dispatch(
            self, live_session, client, transports):
        """The other half: an explicitly authorised session DOES
        dispatch, and the stored record names the capability the server
        verified rather than the source the caller claimed."""
        from src.live import journal
        _seed(live_session)
        headers, opened = _open_session_over_http(client, self.ADMIN)
        r = client.post("/api/admin/mls/broadcast", headers=headers,
                        json={"message": "CIN value at 0.31",
                              "channel": "action", "source": "session",
                              "fixture_id": 1})
        assert r.status_code == 200, r.text
        assert r.json()["dispatched"] is True
        assert len(transports) == 1
        said = journal.recent_broadcasts(1)
        assert len(said) == 1
        assert said[0]["dispatch_capability_id"] == opened["capability_id"]

    def test_the_operator_token_alone_cannot_mint_a_capability(
            self, live_session, client):
        """The handshake needs a SECOND factor. Signing the nonce with
        the admin token — the only secret a generic operator client
        holds — does not verify."""
        import hashlib
        import hmac
        _seed(live_session)
        ch = client.post("/api/admin/mls/session/challenge",
                         headers=self.ADMIN)
        assert ch.status_code == 200, ch.text
        forged = hmac.new(b"s3cret", ch.json()["nonce"].encode(),
                          hashlib.sha256).hexdigest()
        bad = client.post("/api/admin/mls/session/open",
                          headers=self.ADMIN,
                          json={"challenge_id": ch.json()["challenge_id"],
                                "response": forged})
        assert bad.status_code == 403, bad.text
        assert "second factor" in bad.json()["detail"]

    def test_a_challenge_is_single_use(self, live_session, client):
        """A captured nonce is worth nothing twice — otherwise a replayed
        request would mint a fresh capability."""
        from src.live import session_capability as sc
        _seed(live_session)
        ch = client.post("/api/admin/mls/session/challenge",
                         headers=self.ADMIN).json()
        body = {"challenge_id": ch["challenge_id"],
                "response": sc.expected_response(ch["nonce"])}
        assert client.post("/api/admin/mls/session/open",
                           headers=self.ADMIN, json=body).status_code == 200
        again = client.post("/api/admin/mls/session/open",
                            headers=self.ADMIN, json=body)
        assert again.status_code == 403
        assert "already-answered" in again.json()["detail"]

    def test_an_unset_second_factor_disables_dispatch_entirely(
            self, live_session, client, monkeypatch, transports):
        """Fail-closed: with no session secret configured there is no
        way to open a session, so the relay simply cannot dispatch."""
        from src.live import journal
        monkeypatch.setattr(config, "JOURNAL_SESSION_SECRET", "")
        _seed(live_session)
        ch = client.post("/api/admin/mls/session/challenge",
                         headers=self.ADMIN)
        assert ch.status_code == 503
        assert "JOURNAL_SESSION_SECRET is unset" in ch.json()["detail"]
        r = client.post("/api/admin/mls/broadcast", headers=self.ADMIN,
                        json={"message": "x", "fixture_id": 1})
        assert r.status_code == 403
        assert transports == []
        assert journal.recent_broadcasts(1) == []

    def test_the_second_factor_must_differ_from_the_admin_token(
            self, live_session, client, monkeypatch):
        """Setting them equal would collapse two factors into one and
        restore exactly the defect, so it is refused rather than
        accepted as configuration."""
        monkeypatch.setattr(config, "JOURNAL_SESSION_SECRET", "s3cret")
        _seed(live_session)
        ch = client.post("/api/admin/mls/session/challenge",
                         headers=self.ADMIN)
        assert ch.status_code == 503
        assert "collapses the two factors" in ch.json()["detail"]

    def test_closing_a_session_revokes_dispatch(
            self, live_session, client, transports):
        """A leaked token must be revocable without waiting out the TTL
        or restarting the service."""
        from src.live import journal
        _seed(live_session)
        headers, _ = _open_session_over_http(client, self.ADMIN)
        assert client.post("/api/admin/mls/session/close",
                           headers=headers).json()["closed"] is True
        r = client.post("/api/admin/mls/broadcast", headers=headers,
                        json={"message": "after close", "fixture_id": 1})
        assert r.status_code == 403
        assert transports == []
        assert journal.recent_broadcasts(1) == []

    def test_a_hand_built_capability_object_is_not_a_capability(
            self, live_session, monkeypatch):
        """The in-process half: `SessionCapability` is an ordinary
        dataclass, so the check cannot be "did the caller pass one" — it
        is "is this one the store actually issued"."""
        from src.live import journal
        from src.live.session_capability import SessionCapability
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        _seed(live_session)
        forged = SessionCapability(
            id="not-issued-by-the-store", label="live",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1))
        r = journal.broadcast("forged", fixture_id=1, capability=forged)
        assert r.get("refused") is True
        assert r["dispatched"] is False
        assert sent == []
        assert journal.recent_broadcasts(1) == []

    def test_an_expired_capability_cannot_dispatch(
            self, live_session, monkeypatch):
        from src.live import journal
        from src.live import session_capability as sc
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(
            alerts, "send_alert",
            lambda m, **kw: (sent.append(m), {"discord_action": True})[1])
        monkeypatch.setattr(config, "JOURNAL_SESSION_SECRET", "sf")
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        monkeypatch.setattr(config, "JOURNAL_SESSION_TTL_SECONDS", 0)
        sc._reset_for_tests()
        _seed(live_session)
        ch = sc.challenge()
        opened = sc.open_session(ch["challenge_id"],
                                 sc.expected_response(ch["nonce"]))
        assert "error" not in opened, opened
        assert sc.verify(opened["session_token"]) is None
        r = journal.broadcast("stale session", fixture_id=1,
                              capability=SessionCapabilityFromDict(opened))
        assert r.get("refused") is True
        assert sent == []
        assert journal.recent_broadcasts(1) == []
        sc._reset_for_tests()


def SessionCapabilityFromDict(opened):
    """Rebuild the capability object a caller would have been holding
    when its TTL ran out — the store has already dropped it."""
    from src.live.session_capability import SessionCapability
    return SessionCapability(
        id=opened["capability_id"], label=opened.get("session_label"),
        issued_at=datetime.fromisoformat(opened["issued_at"]),
        expires_at=datetime.fromisoformat(opened["expires_at"]))


class TestQuoteTemporalProvenance:
    """journal-P0-H: Kalshi publishes NO quote timestamp, so our capture
    clock is the only evidence of when a book was observed. Round 2
    bounded the citation on one side only — a quote could not postdate
    the moment it described, but it could predate it by any amount. An
    hours-old same-contract quote therefore became `observed_quote`,
    counted toward the aggregate, and fed market_drift/execution_cost as
    though it were the book at the time.
    """

    def _stale_quote(self, s, qid, minutes, contract=1):
        s.add(MarketQuote(
            id=qid, market_contract_id=contract,
            captured_at=datetime.now(UTC) - timedelta(minutes=minutes),
            yes_ask_c=45, yes_bid_c=43, market_snapshot_id=1))
        s.commit()

    def test_a_stale_same_contract_quote_is_refused_and_classified(
            self, live_session):
        """Same fixture, same contract, same outcome — identity is fine.
        It is the AGE that disqualifies it, and the refusal is named on
        the row rather than left as an absent field."""
        from src.live import journal
        _seed(live_session)
        self._stale_quote(live_session, 20, minutes=120)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=20)
        assert "error" not in bet, bet
        assert bet["price_basis"] == "stated_only"
        assert bet["market_quote_id"] is None
        assert bet["quote_rejection_reason"] == "stale_capture"
        assert bet["quote_age_seconds"] >= 7200
        assert bet["quote_max_age_seconds"] == \
            config.JOURNAL_QUOTE_MAX_AGE_SECONDS
        assert bet["counts_toward_aggregate"] is False
        out = journal.journal_summary()
        assert out["countable"] == 0
        assert out["excluded_stale_quote"] == 0   # basis was downgraded
        assert out["excluded_stated_only"] == 1

    def test_a_fresh_quote_is_accepted_with_its_age_reported(
            self, live_session):
        """The ordinary case must still pass: a ceiling that refuses
        real evidence is not a fix. NOT a both-ways control — it also
        asserts the newly reported age, so it fails against pre-fix
        sources on the missing field rather than on the behaviour."""
        from src.live import journal
        _seed(live_session)                      # quote 1: ~60s old
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        assert bet["price_basis"] == "observed_quote"
        assert bet["counts_toward_aggregate"] is True
        assert 0 <= bet["quote_age_seconds"] <= 120
        assert bet["quote_rejection_reason"] is None

    def test_the_ceiling_is_configuration_not_a_literal(
            self, live_session, monkeypatch):
        """Tightening the ceiling reclassifies the SAME quote — proof the
        decision is the age against the ceiling, not a hardcoded shape
        of the test data."""
        from src.live import journal
        monkeypatch.setattr(config, "JOURNAL_QUOTE_MAX_AGE_SECONDS", 5)
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  market_quote_id=1)
        assert bet["price_basis"] == "stated_only"
        assert bet["quote_rejection_reason"] == "stale_capture"

    def test_a_stale_fill_book_is_refused_and_writes_nothing(
            self, live_session):
        """The fill book is what market_drift and execution_cost are
        measured against, so it must be CONTEMPORANEOUS with the fill it
        claims to describe."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        self._stale_quote(live_session, 21, minutes=60)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        now = datetime.now(UTC)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, market_quote_id_at_fill=21)
        assert "error" in r
        assert "contemporaneity ceiling" in r["error"]
        assert str(config.JOURNAL_FILL_QUOTE_MAX_AGE_SECONDS) in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_a_contemporaneous_fill_book_is_accepted_with_its_age(
            self, live_session):
        """A contemporaneous book still earns the full decomposition.
        NOT a both-ways control — it also asserts the newly reported
        fill-book age, so against pre-fix sources it fails on the
        missing field, not on the behaviour."""
        from src.live import journal
        _seed(live_session)
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        now = datetime.now(UTC)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, market_quote_id_at_fill=1,
            exchange_order_id="ORD-FRESH")
        assert "error" not in r, r
        assert r["gaps"]["gaps_basis"] == "record_and_fill_books"
        assert r["gaps"]["fill_quote_age_seconds"] <= \
            config.JOURNAL_FILL_QUOTE_MAX_AGE_SECONDS
        assert r["gaps"]["fill_quote_freshness"] == "fresh"

    def _legacy_stale_entry(self, s, bet_id=500, hours=6):
        """A row as it would have been written BEFORE the ceiling
        existed: observed_quote, an ancient capture, and no stored age.
        The reader is where these rows are actually used, so the reader
        is where they have to be caught."""
        now = datetime.now(UTC)
        s.add(PersonalBet(
            id=bet_id, competition_slug="mls-2026", fixture_id=1,
            market_ticker="KXMLSGAME-x-H", outcome_key="home_win",
            market_contract_id=1, status="taken",
            stated_price_dollars="0.45", price_basis="observed_quote",
            market_quote_id=1,
            quote_observed_at=now - timedelta(hours=hours),
            recorded_at=now, resolved_at=now))
        s.commit()
        return bet_id

    def test_a_row_recorded_before_the_ceiling_stops_counting(
            self, live_session):
        """Fixing the writer alone would leave every already-recorded
        entry counting off an arbitrarily old book. The read-side check
        is derived from the two timestamps the row already carries."""
        from src.live import journal
        _seed(live_session)
        self._legacy_stale_entry(live_session)
        out = journal.journal_summary()
        assert out["total_recorded"] == 1
        assert out["countable"] == 0
        assert out["excluded_stale_quote"] == 1
        entry = journal.open_entries(live_session, 1)[0]
        assert entry["quote_freshness"] == "stale"
        assert entry["counts_toward_aggregate"] is False

    def test_gaps_refuse_to_decompose_off_a_stale_book(
            self, live_session):
        """`record_and_fill_books` is a claim about evidence. A book that
        exists but is nowhere near the moment it describes does not earn
        it, and the decomposed metrics stay absent under an explicit
        classification instead."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet_id = self._legacy_stale_entry(live_session)
        now = datetime.now(UTC)
        live_session.add(PBE(
            personal_bet_id=bet_id, account_label="friend-A",
            consent_recorded_at=now, status="filled",
            fill_price_dollars="0.47", filled_contracts="10",
            fee_paid_dollars="0.12", filled_at=now,
            market_quote_id_at_fill=1, exchange_order_id="ORD-LEGACY"))
        live_session.commit()
        row = journal.full_entries(1)[0]["executions"][0]
        g = row["gaps"]
        assert g["gaps_basis"] == "stale_record_book"
        for k in ("market_drift", "execution_cost", "aggressiveness"):
            assert k not in g, k
        # the age is REPORTED, so the classification is checkable
        assert g["record_quote_age_seconds"] >= 6 * 3600
        assert g["record_quote_freshness"] == "stale"
        assert g["fill_quote_freshness"] == "fresh"

    def test_the_public_projection_carries_the_capture_age(
            self, live_session, monkeypatch):
        """The whole point of citing a quote is that a reader can check
        it — and they cannot check a book whose age they are not told."""
        from src.live import corpus, journal
        _seed(live_session)
        journal.record_view(1, "KXMLSGAME-x-H", outcome_key="home_win",
                            stated_price="0.45", market_quote_id=1)
        row = corpus.build_corpus()["sections"]["personal_journal.json"][0]
        assert "quote_age_seconds" in row
        assert "quote_max_age_seconds" in row
        assert "quote_freshness" in row
        assert row["quote_freshness"] == "fresh"


class TestPayloadSensitiveIdempotency:
    """journal-P0-I: a retry is the SAME payload arriving twice. The key
    (exchange_order_id, status) alone identified a row, so the same key
    carrying different economics got the earlier row back with
    `idempotent: true` — the API reported that the new numbers had been
    confirmed when it had discarded them. On a third party's money that
    is the worst available failure mode.
    """

    ADMIN = {"X-Admin-Token": "s3cret"}

    @pytest.fixture()
    def client(self, live_session, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
        from fastapi.testclient import TestClient
        from api import main as api_main
        api_main._rate_last.clear()
        return TestClient(api_main.app)

    def _taken(self, journal):
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def _payload(self, bet_id, now):
        return {"bet_id": bet_id, "account_label": "friend-A",
                "consent_recorded_at": now.isoformat(),
                "fill_price": "0.47", "filled_contracts": "10",
                "fee_paid": "0.12", "filled_at": now.isoformat(),
                "exchange_order_id": "ORD-IDEM",
                "publication_consent": False}

    def test_an_exact_repeat_is_a_no_op(self, live_session, client):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        # whole-second so isoformat round-trips exactly, rounded
        # UP: rounding down would put the fill before the taken
        # moment, which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        body = self._payload(bet["id"], now)
        first = client.post("/api/admin/mls/journal/execution",
                            headers=self.ADMIN, json=body)
        assert first.status_code == 200, first.text
        assert "error" not in first.json(), first.text
        again = client.post("/api/admin/mls/journal/execution",
                            headers=self.ADMIN, json=body)
        assert again.status_code == 200, again.text
        assert again.json()["idempotent"] is True
        assert again.json()["id"] == first.json()["id"]
        assert live_session.query(PBE).count() == 1

    def test_any_changed_canonical_field_is_409_and_writes_nothing(
            self, live_session, client):
        """Every canonical field, one at a time, over the COMPOSED HTTP
        path: the status code is 409, the response says conflict, and
        the stored row is byte-for-byte what the first call wrote."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        # whole-second so isoformat round-trips exactly, rounded
        # UP: rounding down would put the fill before the taken
        # moment, which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        body = self._payload(bet["id"], now)
        first = client.post("/api/admin/mls/journal/execution",
                            headers=self.ADMIN, json=body)
        assert first.status_code == 200, first.text
        assert "error" not in first.json(), first.text
        before = {c.name: getattr(
            live_session.get(PBE, first.json()["id"]), c.name)
            for c in PBE.__table__.columns}
        changes = [
            {"fill_price": "0.48"},
            {"filled_contracts": "11"},
            {"fee_paid": "0.13"},
            {"filled_at": (now + timedelta(seconds=1)).isoformat()},
            {"consent_recorded_at":
                (now - timedelta(hours=1)).isoformat()},
            {"account_label": "friend-B"},
            {"publication_consent": True},
            {"best_available_price": "0.49"},
        ]
        for change in changes:
            r = client.post("/api/admin/mls/journal/execution",
                            headers=self.ADMIN, json={**body, **change})
            assert r.status_code == 409, (change, r.status_code, r.text)
            assert r.json()["conflict"] is True
            assert r.json()["conflicting_write"] == "execution"
            assert live_session.query(PBE).count() == 1, change
            live_session.expire_all()
            after = {c.name: getattr(
                live_session.get(PBE, first.json()["id"]), c.name)
                for c in PBE.__table__.columns}
            assert after == before, change

    def test_decimal_spelling_is_not_a_conflict(self, live_session,
                                                client):
        """0.47 and 0.470 are the same fill. The hash is over VALUES, so
        an honest retry must not 409 on formatting."""
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        # whole-second so isoformat round-trips exactly, rounded
        # UP: rounding down would put the fill before the taken
        # moment, which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        body = self._payload(bet["id"], now)
        first = client.post("/api/admin/mls/journal/execution",
                            headers=self.ADMIN, json=body)
        assert first.status_code == 200, first.text
        assert "error" not in first.json(), first.text
        again = client.post("/api/admin/mls/journal/execution",
                            headers=self.ADMIN,
                            json={**body, "fill_price": "0.4700",
                                  "filled_contracts": "10.0",
                                  "fee_paid": "0.120"})
        assert again.status_code == 200, again.text
        assert again.json()["idempotent"] is True

    def test_a_row_written_before_the_hash_existed_is_still_checked(
            self, live_session, client):
        """Rows predating this fix carry no stored hash, and they are
        exactly the rows most likely to be retried. Treating "no
        recorded hash" as "matches" would reintroduce the defect for
        them, so the hash is recomputed from the stored columns."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        # whole-second so isoformat round-trips exactly, rounded
        # UP: rounding down would put the fill before the taken
        # moment, which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        live_session.add(PBE(
            personal_bet_id=bet["id"], account_label="friend-A",
            consent_recorded_at=now, status="filled",
            fill_price_dollars="0.47", filled_contracts="10",
            fee_paid_dollars="0.12", filled_at=now,
            exchange_order_id="ORD-IDEM"))
        live_session.commit()
        assert live_session.query(PBE).first().execution_payload_hash \
            is None
        body = self._payload(bet["id"], now)
        same = client.post("/api/admin/mls/journal/execution",
                           headers=self.ADMIN, json=body)
        assert same.status_code == 200, same.text
        assert same.json().get("idempotent") is True, same.text
        bad = client.post("/api/admin/mls/journal/execution",
                          headers=self.ADMIN,
                          json={**body, "fill_price": "0.99"})
        assert bad.status_code == 409, bad.text
        assert live_session.query(PBE).count() == 1

    def test_a_conflicting_settlement_is_409_including_the_outcome(
            self, live_session, client):
        """The old check compared credit and timestamp only, so a retry
        carrying a DIFFERENT settled_outcome was answered `idempotent`
        and the outcome-conflict check was never reached."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        # whole-second so isoformat round-trips exactly, rounded
        # UP: rounding down would put the fill before the taken
        # moment, which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-SET")
        base = {"execution_id": ex["id"], "settlement_credit": "10",
                "settled_at": now.isoformat(),
                "settled_outcome": "home_win"}
        settled = client.post("/api/admin/mls/journal/settlement",
                              headers=self.ADMIN, json=base)
        assert settled.status_code == 200, settled.text
        assert "error" not in settled.json(), settled.text
        assert client.post("/api/admin/mls/journal/settlement",
                           headers=self.ADMIN,
                           json=base).json()["idempotent"] is True
        for change in ({"settlement_credit": "5"},
                       # a different, still-plausible settled_at: after
                       # the fill and inside the clock-skew tolerance,
                       # so it is refused for CONFLICTING, not for being
                       # out of domain
                       {"settled_at":
                        (now + timedelta(seconds=5)).isoformat()},
                       {"settled_outcome": "away_win"}):
            r = client.post("/api/admin/mls/journal/settlement",
                            headers=self.ADMIN, json={**base, **change})
            assert r.status_code == 409, (change, r.text)
            assert r.json()["conflicting_write"] == "settlement"
            live_session.expire_all()
            row = live_session.get(PBE, ex["id"])
            assert row.settlement_credit_dollars == "10", change
            assert live_session.get(
                PersonalBet, bet["id"]).settled_outcome == "home_win"

    def test_a_conflicting_reconciliation_is_409_including_consent(
            self, live_session, client):
        """The old check looked only at the `reconciled` flag, so a
        second call granting or revoking publication_consent returned
        success and wrote nothing — the consent of a third party over
        their own financial record, silently discarded."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        # whole-second so isoformat round-trips exactly, rounded
        # UP: rounding down would put the fill before the taken
        # moment, which P0-D chronology rejects
        now = (datetime.now(UTC)
               + timedelta(seconds=1)).replace(microsecond=0)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-REC")
        journal.settle_execution(ex["id"], settlement_credit="10",
                                 settled_at=now)
        base = {"execution_id": ex["id"], "note": "matches statement"}
        rec = client.post("/api/admin/mls/journal/reconcile",
                          headers=self.ADMIN, json=base)
        assert rec.status_code == 200, rec.text
        assert "error" not in rec.json(), rec.text
        assert client.post("/api/admin/mls/journal/reconcile",
                           headers=self.ADMIN,
                           json=base).json()["idempotent"] is True
        for change in ({"note": "actually it did not match"},
                       {"publication_consent": True},
                       {"publication_consent": False}):
            r = client.post("/api/admin/mls/journal/reconcile",
                            headers=self.ADMIN, json={**base, **change})
            assert r.status_code == 409, (change, r.text)
            assert r.json()["conflicting_write"] == "reconciliation"
            live_session.expire_all()
            row = live_session.get(PBE, ex["id"])
            assert row.reconciliation_note == "matches statement", change
            assert row.publication_consent is False, change


class TestLifecycleContradictions:
    """journal-P1-H: combinations that cannot describe a real event are
    refused rather than stored. Round 2 validated each field's domain and
    two chronology edges; a row could still assert "nothing filled" AND a
    fill price, or claim a fill in the future (which produced a negative
    latency in the fidelity metrics)."""

    def _taken(self, journal):
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def test_not_filled_cannot_carry_fill_facts(self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        base = dict(consent_recorded_at=now, status="not_filled",
                    not_filled_reason="price_moved")
        for kw in ({"fill_price": "0.47"}, {"filled_contracts": "10"},
                   {"fee_paid": "0.12"}, {"filled_at": now},
                   {"market_quote_id_at_fill": 1}):
            r = journal.record_execution(bet["id"], "friend-A",
                                         **{**base, **kw})
            assert "error" in r, kw
            (field, _), = kw.items()
            assert field in r["error"], kw
        assert live_session.query(PBE).count() == 0
        # CONTROL: the honest not_filled row still records, with the
        # liquidity evidence that makes it worth having
        ok = journal.record_execution(
            bet["id"], "friend-A", best_available_price="0.52", **base)
        assert "error" not in ok, ok
        assert ok["best_available_price_dollars"] == "0.52"

    def test_a_fill_cannot_carry_a_not_filled_reason(self, live_session):
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, not_filled_reason="price_moved")
        assert "error" in r and "not_filled_reason" in r["error"]
        assert live_session.query(PBE).count() == 0

    def test_no_recorded_moment_may_lie_in_the_future(self, live_session):
        """A fill that has not happened yet has nothing to record, and a
        future filled_at made latency_seconds negative."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        soon = now + timedelta(hours=2)
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=soon)
        assert "error" in r and "filled_at" in r["error"]
        assert "future" in r["error"]
        r = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=soon,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now)
        assert "error" in r and "consent_recorded_at" in r["error"]
        assert live_session.query(PBE).count() == 0
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-FUT")
        assert "error" not in ex, ex
        bad = journal.settle_execution(ex["id"], settlement_credit="10",
                                       settled_at=soon)
        assert "error" in bad and "future" in bad["error"]
        live_session.expire_all()
        assert live_session.get(PBE, ex["id"]).settled_at is None

    def test_settlement_cannot_exceed_the_contracts_held(self,
                                                         live_session):
        """A Kalshi contract settles at $1 or $0, so a credit above the
        contracts filled is a data-entry error — and settlement_delta
        would have carried it into the fidelity metrics as a measured
        discrepancy."""
        from src.live import journal
        from src.live.models import PersonalBetExecution as PBE
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        ex = journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, exchange_order_id="ORD-CAP")
        r = journal.settle_execution(ex["id"], settlement_credit="1000",
                                     settled_at=now)
        assert "error" in r and "maximum payout" in r["error"]
        live_session.expire_all()
        assert live_session.get(PBE, ex["id"]).settled_at is None
        ok = journal.settle_execution(ex["id"], settlement_credit="10",
                                      settled_at=now)
        assert "error" not in ok, ok


class TestCorrectionChainLinearityUnderRace:
    """journal-P1-G: the handler's read-then-insert head check cannot
    survive concurrency — two simultaneous corrections of one entry both
    read "no head" and both commit, leaving an ambiguous effective
    observation. The database is the only place this can be settled."""

    def _taken(self, journal):
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45", market_quote_id=1)
        journal.resolve_view(bet["id"], "taken")
        return bet

    def test_the_database_itself_refuses_a_second_head(self,
                                                       live_session):
        """Test-DB parity: the partial unique index is a real constraint
        the SQLite suite enforces too, not a promise the handler makes."""
        from sqlalchemy.exc import IntegrityError
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        now = datetime.now(UTC)
        for i in range(2):
            live_session.add(PersonalBet(
                competition_slug="mls-2026", fixture_id=1,
                market_ticker="KXMLSGAME-x-H", outcome_key="home_win",
                status="considered", price_basis="stated_only",
                recorded_at=now, corrects_bet_id=bet["id"]))
        with pytest.raises(IntegrityError):
            live_session.commit()
        live_session.rollback()

    def test_a_lost_race_reads_as_the_linear_chain_error(
            self, live_session, monkeypatch):
        """Simulate the interleaving: the pre-check passes (as it would
        for both racers) and the INSERT is the thing that loses. The
        caller must get the same message the sequential path gives, not
        a 500 — concurrency is not a different API contract."""
        from src.live import journal
        _seed(live_session)
        bet = self._taken(journal)
        first = journal.record_view(1, "KXMLSGAME-x-H",
                                    outcome_key="home_win",
                                    market_quote_id=1,
                                    corrects_bet_id=bet["id"])
        assert "error" not in first, first

        real_query = journal.get_session

        class _BlindSession:
            """The head pre-check sees nothing — exactly what the losing
            racer saw when it read before the winner committed."""
            def __init__(self, inner):
                self._inner = inner
                self._blinded = True

            def query(self, *a, **kw):
                q = self._inner.query(*a, **kw)
                if self._blinded and a and a[0] is PersonalBet:
                    self._blinded = False
                    class _Empty:
                        def filter_by(self, **kw):
                            return self
                        def first(self):
                            return None
                    return _Empty()
                return q

            def __getattr__(self, name):
                return getattr(self._inner, name)

        monkeypatch.setattr(journal, "get_session",
                            lambda: _BlindSession(real_query()))
        r = journal.record_view(1, "KXMLSGAME-x-H",
                                outcome_key="home_win",
                                market_quote_id=1,
                                corrects_bet_id=bet["id"])
        assert "error" in r, r
        assert "already corrected" in r["error"]
        assert str(first["id"]) in r["error"]
        # restore only THIS patch — monkeypatch.undo() would also revert
        # the live-plane fixture and leave the journal dormant
        monkeypatch.setattr(journal, "get_session", real_query)
        # and the losing insert left NOTHING behind: still one chain,
        # one effective observation
        assert live_session.query(PersonalBet).count() == 2
        assert journal.journal_summary()["effective_recorded"] == 1


class TestCorpusReferentialClosure:
    """corpus-v5: the corpus claims to be SELF-CONTAINED. Quotes were
    scoped to lock snapshots, but a journal entry may cite any persisted
    observation — so an exported entry's `market_quote_id` could name a
    quote no file in the bundle contained, and a reader could not check
    the one price the entry exists to make checkable."""

    def _cite_a_non_lock_quote(self, s):
        """A quote on the approved chain that belongs to NO market
        snapshot — the ordinary observation stream."""
        from src.live import journal
        s.add(MarketQuote(id=30, market_contract_id=1,
                          captured_at=datetime.now(UTC)
                          - timedelta(seconds=30),
                          yes_ask_c=46, yes_bid_c=44,
                          market_snapshot_id=None))
        s.commit()
        return journal.record_view(1, "KXMLSGAME-x-H",
                                   outcome_key="home_win",
                                   stated_price="0.45",
                                   market_quote_id=30)

    def test_every_cited_quote_resolves_inside_the_bundle(
            self, live_session):
        from src.live import corpus
        _seed(live_session)
        bet = self._cite_a_non_lock_quote(live_session)
        assert bet["price_basis"] == "observed_quote"
        bundle = corpus.build_corpus()
        exported = {q["id"] for q in
                    bundle["sections"]["market_quotes.json"]}
        for row in bundle["sections"]["personal_journal.json"]:
            assert row["market_quote_id"] is None \
                or row["market_quote_id"] in exported, row
        assert 30 in exported
        assert bundle["manifest"]["counts"][
            "journal_referenced_quotes"] == 1
        assert bundle["manifest"]["quote_scope"] == \
            "lock_snapshot_plus_journal_referenced"

    def test_a_withheld_executions_fill_book_is_not_pulled_in(
            self, live_session):
        """Closure must not become a leak: including a quote referenced
        ONLY by an execution with no publication consent would tell a
        reader which book that withheld execution used."""
        from src.live import corpus, journal
        _seed(live_session)
        live_session.add(MarketQuote(
            id=31, market_contract_id=1,
            captured_at=datetime.now(UTC) - timedelta(seconds=10),
            yes_ask_c=47, yes_bid_c=45, market_snapshot_id=None))
        live_session.commit()
        bet = journal.record_view(1, "KXMLSGAME-x-H",
                                  outcome_key="home_win",
                                  stated_price="0.45")
        journal.resolve_view(bet["id"], "taken")
        now = datetime.now(UTC)
        journal.record_execution(
            bet["id"], "friend-A", consent_recorded_at=now,
            fill_price="0.47", filled_contracts="10", fee_paid="0.12",
            filled_at=now, market_quote_id_at_fill=31,
            exchange_order_id="ORD-PRIV", publication_consent=False)
        bundle = corpus.build_corpus()
        exported = {q["id"] for q in
                    bundle["sections"]["market_quotes.json"]}
        assert 31 not in exported
        assert bundle["sections"]["personal_journal_executions.json"] == []
