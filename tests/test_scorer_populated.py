"""The scorer's POPULATED path, walked before it matters.

`tests/test_scorer.py` exercises exclusions, the empty corpus and the
sample floor. What it never does is drive a corpus over MIN_OBSERVATIONS
and read the aggregates out — so on 2026-08-04 every aggregate branch in
`src/live/scorer.py` had run only in the author's head. Production has
only ever served `corpus: "empty"`.

This walks the whole path against real payload shapes: a settled fixture,
a canonical T-10 lock whose PredictionContracts carry frozen quotes, an
entry citing an EARLIER quote, executions with real fill economics, and
settlement. Then it reads CLV, calibration against the market's Brier,
and P&L with its interval.

TWO QUOTES PER OUTCOME, DELIBERATELY. The lock's quote is the CLOSE. An
entry must cite a different, earlier one or there is no gap to measure —
`scorer._rows` drops that case as `entry_is_the_close`, and a seed that
cited the lock's own quote would silently produce an empty corpus while
looking thorough. That trap is asserted directly below.

The author's own tests are the least trustworthy artefact here, so every
guard is proved red-then-green by mutating the SEED — the thing under
test is whether the scorer notices — rather than by monkeypatching the
scorer's internals.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.live import scorer
from src.live.models import (Fixture, MarketContract, MarketEvent,
                             MarketQuote, MarketSnapshot, PersonalBet,
                             PersonalBetExecution, PredictionContract,
                             PredictionRun)
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc
THREE = ("home_win", "draw", "away_win")
SUFFIX = {"home_win": "H", "draw": "T", "away_win": "A"}

# Priced so the arithmetic is checkable by hand rather than by rerunning
# the code that is under test:
#   entry mid = (0.40 + 0.38) / 2 = 0.39
#   close mid = (0.50 + 0.48) / 2 = 0.49   ->  CLV = +0.10
ENTRY_ASK, CLOSE_ASK = 40, 50
OTHER_ASK = 30                      # draw and away, both books


def _seed_fixture(s, fx_id, *, home_goals=2, away_goals=0,
                  entry_ask=ENTRY_ASK, close_ask=CLOSE_ASK,
                  model_home=0.52, with_lock=True, lock_quote=True):
    """One settled fixture, its T-10 lock, and two books.

    Returns (entry_quotes, close_quotes) keyed by outcome.
    """
    ko = datetime.now(UTC) - timedelta(hours=6)
    s.add_all([
        Fixture(id=fx_id, competition_slug="mls-2026",
                espn_event_id=f"e{fx_id}", status="post",
                home_goals=home_goals, away_goals=away_goals,
                current_kickoff_utc=ko),
        MarketEvent(id=fx_id, competition_slug="mls-2026",
                    kalshi_event_ticker=f"KXMLSGAME-x{fx_id}",
                    series="KXMLSGAME", fixture_id=fx_id,
                    mapping_approved=True),
    ])
    s.flush()
    contracts = {}
    for i, okey in enumerate(THREE):
        mc = MarketContract(id=fx_id * 10 + i, market_event_id=fx_id,
                            ticker=f"KXMLSGAME-x{fx_id}-{SUFFIX[okey]}",
                            outcome_key=okey)
        s.add(mc)
        contracts[okey] = mc
    s.flush()

    def _book(base_id, ask_home, captured):
        out = {}
        for i, okey in enumerate(THREE):
            ask = ask_home if okey == "home_win" else OTHER_ASK
            q = MarketQuote(id=base_id + i,
                            market_contract_id=contracts[okey].id,
                            captured_at=captured, yes_ask_c=ask,
                            yes_bid_c=ask - 2, yes_ask_size=100)
            s.add(q)
            out[okey] = q
        return out

    entry = _book(fx_id * 100, entry_ask, ko - timedelta(minutes=40))
    close = _book(fx_id * 100 + 50, close_ask, ko - timedelta(minutes=10))
    s.flush()

    if with_lock:
        s.add_all([
            MarketSnapshot(id=fx_id, fixture_id=fx_id,
                           captured_at=ko - timedelta(minutes=10),
                           status="complete", execution_ready=True),
            PredictionRun(id=f"lock{fx_id}", fixture_id=fx_id,
                          run_type="t10", status="complete", canonical=True,
                          captured_at=ko - timedelta(minutes=10)),
        ])
        s.flush()
        for okey in THREE:
            s.add(PredictionContract(
                prediction_run_id=f"lock{fx_id}",
                market_contract_id=contracts[okey].id,
                market_quote_id=(close[okey].id if lock_quote else None),
                outcome_key=okey,
                raw_probability=(model_home if okey == "home_win"
                                 else (1 - model_home) / 2)))
    s.commit()
    return entry, close


def _entry(s, fx_id, quote, *, outcome="home_win", status="passed",
           model_p=0.52):
    """A journal row as `record_view` would have written it."""
    now = datetime.now(UTC)
    b = PersonalBet(
        competition_slug="mls-2026", fixture_id=fx_id,
        market_ticker=f"KXMLSGAME-x{fx_id}-{SUFFIX[outcome]}",
        outcome_key=outcome, status=status, price_basis="observed_quote",
        market_quote_id=quote.id, quote_observed_at=quote.captured_at,
        quote_age_seconds=30, recorded_at=now - timedelta(hours=7),
        model_probability=model_p)
    s.add(b)
    s.commit()
    return b


def _fill(s, bet, *, price="0.40", contracts="100", fee="0.28",
          credit="100.00"):
    """A real fill and its settlement — the shapes `record_execution`
    and `settle_execution` persist."""
    ex = PersonalBetExecution(
        personal_bet_id=bet.id, account_label="probe",
        consent_recorded_at=datetime.now(UTC) - timedelta(hours=7),
        status="filled", fill_price_dollars=price,
        filled_contracts=contracts, fee_paid_dollars=fee,
        filled_at=datetime.now(UTC) - timedelta(hours=7),
        settlement_credit_dollars=credit,
        settled_at=datetime.now(UTC) - timedelta(hours=1))
    s.add(ex)
    s.commit()
    return ex


def _corpus(s, n, **kw):
    """n settled fixtures, each with one countable entry."""
    out = []
    for i in range(1, n + 1):
        entry, _close = _seed_fixture(s, i, **kw)
        out.append(_entry(s, i, entry["home_win"]))
    return out


class TestOneFixtureEndToEnd:
    """Before any aggregate: does a single real row score at all."""

    def test_a_countable_row_survives_every_exclusion(self, live_session):
        entry, _ = _seed_fixture(live_session, 1)
        _entry(live_session, 1, entry["home_win"])
        out = scorer.score()
        assert out["corpus"] == "populated"
        assert out["countable"] == 1
        assert out["excluded"] == {}

    def test_clv_is_the_lock_quote_minus_the_entry_quote(self, live_session):
        entry, _ = _seed_fixture(live_session, 1)
        _entry(live_session, 1, entry["home_win"])
        row = scorer.score()["clv"]["rows"][0]
        assert round(row["entry_mid"], 4) == 0.39
        assert round(row["close_mid"], 4) == 0.49
        assert round(row["clv"], 4) == 0.10

    def test_an_entry_citing_the_lock_quote_is_dropped_by_name(
            self, live_session):
        """The trap this seed exists to avoid. Cite the close and there
        is no gap — a corpus built that way scores nothing while looking
        complete."""
        _entry_q, close = _seed_fixture(live_session, 1)
        _entry(live_session, 1, close["home_win"])
        out = scorer.score()
        assert out["countable"] == 0
        assert out["excluded"] == {"entry_is_the_close": 1}


class TestAggregatesAboveTheFloor:
    """MIN_OBSERVATIONS rows — the branches production has never run."""

    def test_clv_aggregate_reports_mean_interval_and_hit_rate(
            self, live_session):
        _corpus(live_session, scorer.MIN_OBSERVATIONS)
        clv = scorer.score()["clv"]
        assert "aggregate_withheld" not in clv
        agg = clv["aggregate"]
        assert agg["n"] == scorer.MIN_OBSERVATIONS
        assert round(agg["mean"], 4) == 0.10
        assert agg["ci95"] is not None
        assert clv["beat_close_rate"] == 1.0

    def test_a_uniform_corpus_has_a_degenerate_interval_and_says_so(
            self, live_session):
        """Every row identical -> zero variance -> the interval collapses
        onto the point. It must not be reported as a significant effect
        without that being visible."""
        _corpus(live_session, scorer.MIN_OBSERVATIONS)
        agg = scorer.score()["clv"]["aggregate"]
        lo, hi = agg["ci95"]
        assert lo == hi == agg["mean"]

    def test_calibration_reports_both_briers_never_ours_alone(
            self, live_session):
        _corpus(live_session, scorer.MIN_OBSERVATIONS)
        cal = scorer.score()["calibration"]
        assert "brier_model" in cal and "brier_market" in cal
        # home won every fixture; model said .52, market's de-vigged
        # home leg is .49/(.49+.29+.29) with both others at mid .29
        assert cal["brier_model"] == round((0.52 - 1.0) ** 2, 6)
        assert cal["brier_market"] > 0
        assert "ci95" in cal["difference"]

    def test_market_brier_comes_from_the_devigged_close_not_the_ask(
            self, live_session):
        _corpus(live_session, 1)
        row = scorer.score()["calibration"]["rows"][0]
        mids = {"home_win": 0.49, "draw": 0.29, "away_win": 0.29}
        expected = mids["home_win"] / sum(mids.values())
        assert abs(row["market_p"] - expected) < 1e-3

    def test_pnl_reports_only_with_an_interval(self, live_session):
        bets = _corpus(live_session, scorer.MIN_OBSERVATIONS)
        for b in bets:
            b.status = "taken"
        live_session.commit()
        for b in bets:
            _fill(live_session, b)
        pnl = scorer.score()["pnl"]
        assert pnl["available"] is True
        assert "aggregate_withheld" not in pnl
        # cost = 0.40*100 + 0.28 = 40.28 ; credit 100.00 ; pnl +59.72
        assert round(pnl["rows"][0]["pnl"], 2) == 59.72
        assert round(pnl["total_pnl"], 2) == round(59.72 * len(bets), 2)
        assert pnl["per_bet"]["ci95"] is not None
        assert pnl["per_bet"]["n"] == len(bets)


class TestTheFloorStillBites:
    def test_one_below_the_floor_withholds_every_aggregate(
            self, live_session):
        bets = _corpus(live_session, scorer.MIN_OBSERVATIONS - 1)
        for b in bets:
            b.status = "taken"
        live_session.commit()
        for b in bets:
            _fill(live_session, b)
        out = scorer.score()
        assert out["countable"] == scorer.MIN_OBSERVATIONS - 1
        for section in ("clv", "calibration", "pnl"):
            assert "aggregate_withheld" in out[section], section
            assert out[section]["aggregate_withheld"]["reason"] == \
                "below_minimum_observations"
        assert "aggregate" not in out["clv"]
        assert "brier_model" not in out["calibration"]
        assert "total_pnl" not in out["pnl"]

    def test_exactly_the_floor_computes(self, live_session):
        """Off-by-one on a refusal threshold is the classic way a guard
        either never fires or never releases."""
        _corpus(live_session, scorer.MIN_OBSERVATIONS)
        assert "aggregate" in scorer.score()["clv"]


class TestPnLRefusesWhatItCannotPrice:
    def test_passed_entries_score_clv_but_carry_no_pnl(self, live_session):
        _corpus(live_session, scorer.MIN_OBSERVATIONS)   # all `passed`
        out = scorer.score()
        assert "aggregate" in out["clv"]
        assert out["pnl"]["available"] is False
        assert "EMPTY, not break-even" in out["pnl"]["means"]

    def test_a_not_filled_execution_is_not_scored_as_a_loss(
            self, live_session):
        """Liquidity evidence, not money lost. Pricing it as a loss would
        invent a stake that was never placed."""
        bets = _corpus(live_session, 1)
        bets[0].status = "taken"
        live_session.commit()
        ex = _fill(live_session, bets[0], credit="0")
        ex.status = "not_filled"
        ex.fill_price_dollars = None
        ex.filled_contracts = None
        live_session.commit()
        assert scorer.score()["pnl"]["available"] is False

    def test_an_unsettled_fill_is_not_scored(self, live_session):
        bets = _corpus(live_session, 1)
        bets[0].status = "taken"
        live_session.commit()
        ex = _fill(live_session, bets[0])
        ex.settlement_credit_dollars = None
        live_session.commit()
        assert scorer.score()["pnl"]["available"] is False


class TestRedGreenBySeedMutation:
    """Each guard proved by breaking the WORLD, not the scorer — the
    question is whether the scorer notices."""

    @pytest.mark.parametrize("mutate,expect", [
        ("drop_lock", "no_canonical_t10_lock"),
        ("lock_without_quote", "lock_carries_no_quote_for_this_outcome"),
        ("unsettle_fixture", "fixture_not_settled"),
    ])
    def test_a_broken_world_is_excluded_by_name(self, live_session,
                                                mutate, expect):
        kw = {}
        if mutate == "drop_lock":
            kw["with_lock"] = False
        elif mutate == "lock_without_quote":
            kw["lock_quote"] = False
        entry, _ = _seed_fixture(live_session, 1, **kw)
        _entry(live_session, 1, entry["home_win"])
        if mutate == "unsettle_fixture":
            fx = live_session.get(Fixture, 1)
            fx.home_goals = fx.away_goals = None
            live_session.commit()
        out = scorer.score()
        assert out["countable"] == 0
        assert out["excluded"] == {expect: 1}, out["excluded"]

    def test_a_stale_quote_entry_stops_counting(self, live_session):
        """journal-P0-H: a book that exists is not automatically evidence
        for the moment it is claimed to describe."""
        entry, _ = _seed_fixture(live_session, 1)
        b = _entry(live_session, 1, entry["home_win"])
        b.quote_age_seconds = 99999
        b.quote_observed_at = datetime.now(UTC) - timedelta(days=2)
        live_session.commit()
        out = scorer.score()
        assert out["countable"] == 0
        assert "quote_not_contemporaneous" in out["excluded"]

    def test_an_unpriced_close_leg_blocks_only_calibration(
            self, live_session):
        """CLV needs one leg; the de-vig needs all three. The sections
        must degrade independently rather than together."""
        _entry_q, close = _seed_fixture(live_session, 1)
        q = live_session.get(MarketQuote, close["draw"].id)
        q.yes_ask_c = q.yes_bid_c = None
        live_session.commit()
        _entry(live_session, 1, _entry_q["home_win"])
        out = scorer.score()
        assert out["clv"]["available"] is True
        assert out["calibration"]["available"] is False
        assert out["calibration"]["excluded_no_partition"] == 1
