"""Phase 2 scorer — what it must refuse to say.

Most of these assert ABSENCE. A scorer's dangerous failure is not a
wrong number, it is a confident one over nothing: an empty journal
reporting 0.0 CLV, or a slate's worth of rows reporting an ROI. Both
read as measurements. So the tests that matter here check that the keys
are missing and the reason is named.
"""
from datetime import datetime, timedelta, timezone

import config
from src.live.models import (Fixture, MarketContract, MarketEvent,
                             MarketQuote, MarketSnapshot, PersonalBet,
                             PredictionContract, PredictionRun)
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc


def _seed_settled(s, *, fx_id=1, home=2, away=0, entry_ask=40,
                  close_ask=50, model_p=0.52, with_lock=True):
    """A SETTLED fixture with an entry quote and a distinct T-10 close.

    entry and close are deliberately different quote rows: an entry that
    cites the lock's own quote has no gap to measure, which is its own
    test below.
    """
    ko = datetime.now(UTC) - timedelta(hours=3)
    fx = Fixture(id=fx_id, competition_slug="mls-2026",
                 espn_event_id=f"e{fx_id}", status="post",
                 home_goals=home, away_goals=away,
                 current_kickoff_utc=ko)
    ev = MarketEvent(id=fx_id, competition_slug="mls-2026",
                     kalshi_event_ticker=f"KXMLSGAME-x{fx_id}",
                     series="KXMLSGAME", fixture_id=fx_id,
                     mapping_approved=True)
    s.add_all([fx, ev])
    s.flush()
    contracts, quotes = {}, {}
    for i, (okey, suffix) in enumerate(
            (("home_win", "H"), ("draw", "T"), ("away_win", "A"))):
        mc = MarketContract(id=fx_id * 10 + i, market_event_id=fx_id,
                            ticker=f"KXMLSGAME-x{fx_id}-{suffix}",
                            outcome_key=okey)
        s.add(mc)
        contracts[okey] = mc
    s.flush()
    # entry quotes, captured BEFORE the lock
    for i, okey in enumerate(("home_win", "draw", "away_win")):
        a = entry_ask if okey == "home_win" else 30
        q = MarketQuote(id=fx_id * 100 + i, market_contract_id=contracts[okey].id,
                        captured_at=ko - timedelta(minutes=40),
                        yes_ask_c=a, yes_bid_c=a - 2, yes_ask_size=100)
        s.add(q)
        quotes[okey] = q
    # the T-10 close
    close = {}
    for i, okey in enumerate(("home_win", "draw", "away_win")):
        a = close_ask if okey == "home_win" else 30
        q = MarketQuote(id=fx_id * 100 + 50 + i,
                        market_contract_id=contracts[okey].id,
                        captured_at=ko - timedelta(minutes=10),
                        yes_ask_c=a, yes_bid_c=a - 2, yes_ask_size=100)
        s.add(q)
        close[okey] = q
    s.flush()
    if with_lock:
        snap = MarketSnapshot(id=fx_id, fixture_id=fx_id,
                              captured_at=ko - timedelta(minutes=10),
                              status="complete", execution_ready=True)
        run = PredictionRun(id=f"lock{fx_id}", fixture_id=fx_id,
                            run_type="t10", status="complete",
                            canonical=True,
                            captured_at=ko - timedelta(minutes=10))
        s.add_all([snap, run])
        s.flush()
        for okey in ("home_win", "draw", "away_win"):
            s.add(PredictionContract(
                prediction_run_id=f"lock{fx_id}",
                market_contract_id=contracts[okey].id,
                market_quote_id=close[okey].id, outcome_key=okey,
                raw_probability=model_p if okey == "home_win" else 0.24))
    s.commit()
    return fx, quotes, close


def _bet(s, *, fx_id=1, quote_id=100, okey="home_win", model_p=0.52,
         basis="observed_quote", status="passed"):
    b = PersonalBet(competition_slug="mls-2026", fixture_id=fx_id,
                    market_ticker=f"KXMLSGAME-x{fx_id}-H",
                    outcome_key=okey, status=status, price_basis=basis,
                    market_quote_id=quote_id,
                    quote_observed_at=datetime.now(UTC) - timedelta(hours=3),
                    quote_age_seconds=30,
                    recorded_at=datetime.now(UTC) - timedelta(hours=4),
                    model_probability=model_p)
    s.add(b)
    s.commit()
    return b


class TestEmptyIsEmpty:
    """The failure this module exists to prevent."""

    def test_empty_journal_reports_empty_never_zero(self, live_session):
        from src.live import scorer
        out = scorer.score()
        assert out["corpus"] == "empty"
        assert out["countable"] == 0
        for k in ("clv", "calibration", "pnl"):
            assert out[k]["available"] is False
            assert "reason" in out[k]
        # the dangerous shape: a number that reads as a measurement
        assert "aggregate" not in out["clv"]
        assert "brier_model" not in out["calibration"]
        assert "total_pnl" not in out["pnl"]

    def test_an_empty_corpus_says_empty_not_zero(self, live_session):
        from src.live import scorer
        out = scorer.score()
        assert "EMPTY, not zero" in out["pnl"]["means"]


class TestTheSampleFloor:
    """A slate is not a lesson."""

    def test_one_row_lists_it_and_withholds_every_aggregate(
            self, live_session):
        from src.live import scorer
        _seed_settled(live_session)
        _bet(live_session)
        out = scorer.score()
        assert out["countable"] == 1
        assert out["clv"]["available"] is True
        assert len(out["clv"]["rows"]) == 1
        assert "aggregate" not in out["clv"]
        w = out["clv"]["aggregate_withheld"]
        assert w["reason"] == "below_minimum_observations"
        assert w["minimum"] == scorer.MIN_OBSERVATIONS
        assert "brier_model" not in out["calibration"]

    def test_the_floor_matches_the_journal_policy(self):
        from src.live import journal, scorer
        assert scorer.MIN_OBSERVATIONS == journal.MIN_EXECUTIONS_FOR_AGGREGATE


class TestCLV:
    def test_clv_is_close_minus_entry_on_mids(self, live_session):
        from src.live import scorer
        _seed_settled(live_session, entry_ask=40, close_ask=50)
        _bet(live_session)
        row = scorer.score()["clv"]["rows"][0]
        # entry mid = (0.40+0.38)/2 = 0.39 ; close mid = (0.50+0.48)/2 = 0.49
        assert round(row["entry_mid"], 4) == 0.39
        assert round(row["close_mid"], 4) == 0.49
        assert round(row["clv"], 4) == 0.10

    def test_a_market_moving_away_is_negative_clv(self, live_session):
        from src.live import scorer
        _seed_settled(live_session, entry_ask=50, close_ask=40)
        _bet(live_session)
        assert scorer.score()["clv"]["rows"][0]["clv"] < 0

    def test_entry_citing_the_close_is_excluded_not_scored_as_zero(
            self, live_session):
        """CLV of an entry that IS the close is zero by construction.

        Averaging it in would drag any real signal toward zero while
        looking like an observation.
        """
        from src.live import scorer
        _fx, _entry, close = _seed_settled(live_session)
        _bet(live_session, quote_id=close["home_win"].id)
        out = scorer.score()
        assert out["countable"] == 0
        assert out["excluded"]["entry_is_the_close"] == 1
        assert out["clv"]["available"] is False


class TestWhatIsExcluded:
    """Every drop is counted and named; none is silent."""

    def test_stated_only_never_counts(self, live_session):
        from src.live import scorer
        _seed_settled(live_session)
        _bet(live_session, basis="stated_only")
        out = scorer.score()
        assert out["countable"] == 0
        assert out["excluded"]["stated_only"] == 1

    def test_void_never_counts(self, live_session):
        from src.live import scorer
        _seed_settled(live_session)
        _bet(live_session, status="void")
        out = scorer.score()
        assert out["countable"] == 0
        assert out["excluded"]["void_recorded_after_kickoff"] == 1

    def test_an_unsettled_fixture_is_excluded_by_name(self, live_session):
        from src.live import scorer
        _seed_settled(live_session)
        fx = live_session.get(Fixture, 1)
        fx.home_goals = fx.away_goals = None
        live_session.commit()
        _bet(live_session)
        out = scorer.score()
        assert out["excluded"]["fixture_not_settled"] == 1

    def test_no_lock_means_no_closing_price_and_is_said(self, live_session):
        """The whole point: never substitute a proxy for the close."""
        from src.live import scorer
        _seed_settled(live_session, with_lock=False)
        _bet(live_session)
        out = scorer.score()
        assert out["countable"] == 0
        assert out["excluded"]["no_canonical_t10_lock"] == 1


class TestCalibrationHasABaseline:
    def test_model_brier_never_appears_without_the_market_brier(
            self, live_session):
        from src.live import scorer
        for i in range(1, scorer.MIN_OBSERVATIONS + 1):
            _seed_settled(live_session, fx_id=i)
            _bet(live_session, fx_id=i, quote_id=i * 100)
        cal = scorer.score()["calibration"]
        assert "brier_model" in cal and "brier_market" in cal
        assert "difference" in cal and "ci95" in cal["difference"]

    def test_a_home_win_scores_the_model_on_the_realised_outcome(
            self, live_session):
        from src.live import scorer
        _seed_settled(live_session, home=2, away=0, model_p=0.52)
        _bet(live_session, model_p=0.52)
        row = scorer.score()["calibration"]["rows"][0]
        assert row["hit"] is True
        assert round(row["brier_model"], 4) == round((0.52 - 1) ** 2, 4)

    def test_an_unpriced_close_leg_blocks_the_partition(self, live_session):
        """Two legs normalised into a fake three-way is a guess."""
        from src.live import scorer
        _fx, _entry, close = _seed_settled(live_session)
        q = live_session.get(MarketQuote, close["draw"].id)
        q.yes_ask_c = q.yes_bid_c = None
        live_session.commit()
        _bet(live_session)
        cal = scorer.score()["calibration"]
        assert cal["available"] is False
        assert cal["excluded_no_partition"] == 1


class TestPnLIsLastAndNeverBare:
    def test_passed_entries_carry_no_pnl_but_still_score_clv(
            self, live_session):
        from src.live import scorer
        _seed_settled(live_session)
        _bet(live_session, status="passed")
        out = scorer.score()
        assert out["clv"]["available"] is True
        assert out["pnl"]["available"] is False
        # rows exist but nothing was staked: the distinct message, so a
        # reader cannot mistake "no money on it" for "it broke even"
        assert "EMPTY, not break-even" in out["pnl"]["means"]

    def test_pnl_key_order_puts_it_after_clv_and_calibration(self):
        """The order is the claim; a reader takes the first number."""
        from src.live import scorer
        src = open(scorer.__file__, encoding="utf-8").read()
        assert (src.index('out["clv"]') < src.index('out["calibration"]')
                < src.index('out["pnl"]'))


class TestTheWallHolds:
    def test_scorer_never_reports_journal_rows_as_edge(self, live_session):
        from src.live import scorer
        out = scorer.score()
        assert out["counts_toward_edge"] is False
        assert "never model edge" in out["note"]
