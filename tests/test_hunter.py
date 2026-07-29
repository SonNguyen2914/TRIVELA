"""Kalshi market hunter: detectors, persistence, alert discipline,
denominators, heartbeat. All canned — no network anywhere. Every
detection rule has a positive fixture (fires, with exact Decimal
arithmetic asserted) AND a negative control (a fair/uncrossed/unmoved
book that must NOT fire), including fee-aware controls where the gross
condition holds but exact fees erase the margin."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import config
from src.live import db as live_db
from src.live import hunter
from src.live.models import (Competition, Fixture, HunterCycle,
                             HunterFinding, HunterObservationWatermark,
                             LiveBase, MarketContract, MarketEvent,
                             ModelApprovalDecision, ModelVersion,
                             PredictionContract, PredictionRun, Team)

UTC = timezone.utc

# a Tuesday 14:00 ET → 18:00 UTC (EDT); the matching ticker date segment
NOW = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
TODAY_SEG = "26JUL28"           # NOW in US/Eastern wall clock
EV = f"KXUCLGAME-{TODAY_SEG}AAABBB"


def _enforce_varchar_lengths(session, flush_context, instances):
    """PostgreSQL-grade VARCHAR enforcement on SQLite (same guard as
    test_mls_shadow — the parity rule that caught the fee-policy
    truncation that erased a night of production fills)."""
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
                    f"{len(val)} > String({col.type.length})")


@pytest.fixture()
def live_session(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/live.db"
    monkeypatch.setattr(config, "LIVE_DATABASE_URL", url)
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)
    monkeypatch.setattr(live_db, "LIVE_BOOT_ERROR", None)
    LiveBase.metadata.create_all(live_db.get_engine())
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _Session
    event.listen(_Session, "before_flush", _enforce_varchar_lengths)
    s = live_db.get_session()
    s.add(Competition(slug="mls-2026", name="MLS", season=2026))
    s.commit()
    yield s
    event.remove(_Session, "before_flush", _enforce_varchar_lengths)
    s.close()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


@pytest.fixture(autouse=True)
def _fresh_hunter_state(monkeypatch):
    """Process-local hunter state must not leak between tests, and no
    test may reach a provider: both network entry points added for the
    P0 fixes are stubbed to raise, so a path that forgets to cann one
    fails LOUDLY instead of silently dialling out."""
    from collections import deque
    monkeypatch.setattr(hunter, "_pair_store", {})
    monkeypatch.setattr(hunter, "_alert_times", deque())
    monkeypatch.setattr(hunter, "_roster",
                        {"at": None, "series": [], "active": set(),
                         "fee": {}})

    def _no_network(*a, **kw):
        raise AssertionError("unstubbed provider call in a canned test")
    monkeypatch.setattr(hunter, "_fetch_series_fee_schedule", _no_network)
    monkeypatch.setattr(hunter, "_refetch_market", _no_network)


def _quad(series="KXUCLGAME"):
    """The provider-declared schedule this repo models exactly:
    fee_type 'quadratic' at multiplier 1 (90 of 97 soccer GAME series)."""
    return hunter.FeeSchedule(series, fee_type="quadratic", multiplier=1)


def _maker(series="KXUCLGAME"):
    """The schedule 7 soccer GAME series really declare — including
    KXUCLGAME itself. Kalshi publishes the NAME and not the rate, so it
    is fee_unknown here."""
    return hunter.FeeSchedule(series, fee_type="quadratic_with_maker_fees",
                              multiplier=1)


def _mkt(ticker, event=EV, bid=None, ask=None, no_bid=None, no_ask=None,
         ask_size="50", bid_size="50"):
    m = {"ticker": ticker, "event_ticker": event, "status": "active"}
    if bid is not None:
        m["yes_bid_dollars"] = bid
    if ask is not None:
        m["yes_ask_dollars"] = ask
    if no_bid is not None:
        m["no_bid_dollars"] = no_bid
    if no_ask is not None:
        m["no_ask_dollars"] = no_ask
    if ask_size is not None:
        m["yes_ask_size_fp"] = ask_size
    if bid_size is not None:
        m["yes_bid_size_fp"] = bid_size
    return m


def _healthy_three_way(a1="0.34", a2="0.34", a3="0.34"):
    """A fair 3-way book: asks sum over $1, tight spreads, deep sizes."""
    def near(a):                      # 2c inside the ask
        return str(Decimal(a) - Decimal("0.02"))
    return [
        _mkt(f"{EV}-AAA", bid=near(a1), ask=a1, no_bid=near("0.66"),
             no_ask="0.68"),
        _mkt(f"{EV}-TIE", bid=near(a2), ask=a2, no_bid=near("0.66"),
             no_ask="0.68"),
        _mkt(f"{EV}-BBB", bid=near(a3), ask=a3, no_bid=near("0.66"),
             no_ask="0.68"),
    ]


def _of_type(findings, ftype):
    return [f for f in findings if f["finding_type"] == ftype]


# ---------------------------------------------------------------------------
class TestSumBelowOne:
    def test_fires_with_exact_decimal_arithmetic(self):
        legs = [
            _mkt(f"{EV}-AAA", bid="0.28", ask="0.30"),
            _mkt(f"{EV}-TIE", bid="0.30", ask="0.32"),
            _mkt(f"{EV}-BBB", bid="0.28", ask="0.30"),
        ]
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad()), "SUM_BELOW_ONE")
        assert len(fs) == 1
        f = fs[0]
        # asks 0.92; exact fees 0.0147 + 0.0153 + 0.0147 = 0.0447
        assert f["net_margin_dollars"] == "0.0353"
        assert f["legs"]["sum_asks_dollars"] == "0.92"
        assert f["legs"]["sum_fees_dollars"] == "0.0447"
        assert len(f["legs"]["legs"]) == 3
        assert f["legs"]["fee_policy"] == "kalshi-fee-2026-07-general"
        assert not f["is_context"]

    def test_fair_book_does_not_fire(self):
        fs = hunter.detect_event_findings(
            "KXUCLGAME", EV, _healthy_three_way(), NOW, _quad())
        assert _of_type(fs, "SUM_BELOW_ONE") == []

    def test_fees_erase_a_gross_margin(self):
        # asks sum 0.98 (< $1) but exact fees are 0.0463 — a fee-blind
        # detector would call this a win; the finding must NOT exist
        legs = [
            _mkt(f"{EV}-AAA", bid="0.30", ask="0.32"),
            _mkt(f"{EV}-TIE", bid="0.31", ask="0.33"),
            _mkt(f"{EV}-BBB", bid="0.31", ask="0.33"),
        ]
        fs = hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad())
        assert _of_type(fs, "SUM_BELOW_ONE") == []

    def test_partition_guards(self):
        # no TIE leg → not a provable 3-way partition, however cheap
        legs = [_mkt(f"{EV}-AAA", ask="0.20"),
                _mkt(f"{EV}-BBB", ask="0.20"),
                _mkt(f"{EV}-CCC", ask="0.20")]
        assert _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad()), "SUM_BELOW_ONE") == []
        # two legs only
        legs = [_mkt(f"{EV}-AAA", ask="0.20"),
                _mkt(f"{EV}-TIE", ask="0.20")]
        assert _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad()), "SUM_BELOW_ONE") == []
        # a leg with no ask
        legs = [_mkt(f"{EV}-AAA", ask="0.30"),
                _mkt(f"{EV}-TIE", ask="0.32"),
                _mkt(f"{EV}-BBB")]
        assert _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad()), "SUM_BELOW_ONE") == []


class TestCrossedBook:
    def test_fires_and_stores_both_sides(self):
        m = _mkt(f"{EV}-AAA", bid="0.60", ask="0.62",
                 no_bid="0.55", no_ask="0.40")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad()), "CROSSED_BOOK")
        assert len(fs) == 1
        legs = fs[0]["legs"]
        # 0.60 + 0.55 - 1 = 0.15 gross; fees fee(0.40)=0.0168 +
        # fee(0.45)=0.0174 → net 0.1158
        assert fs[0]["net_margin_dollars"] == "0.1158"
        assert legs["yes_bid_dollars"] == "0.60"
        assert legs["no_bid_dollars"] == "0.55"
        assert legs["fee_leg_yes_dollars"] == "0.0168"
        assert legs["fee_leg_no_dollars"] == "0.0174"

    def test_uncrossed_book_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.60", ask="0.62",
                 no_bid="0.38", no_ask="0.40")
        fs = hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad())
        assert _of_type(fs, "CROSSED_BOOK") == []

    def test_fees_erase_a_thin_cross(self):
        # gross +0.02 but two taker fee legs at 0.49 cost 0.0350
        m = _mkt(f"{EV}-AAA", bid="0.51", ask="0.53",
                 no_bid="0.51", no_ask="0.53")
        fs = hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad())
        assert _of_type(fs, "CROSSED_BOOK") == []


class TestLiquidityContext:
    def test_wide_spread_is_context_only(self):
        m = _mkt(f"{EV}-AAA", bid="0.05", ask="0.80")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad()), "WIDE_SPREAD")
        assert len(fs) == 1
        assert fs[0]["is_context"] is True
        assert fs[0]["net_margin_dollars"] is None
        assert "not a win" in fs[0]["legs"]["rule"]

    def test_tight_spread_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52")
        fs = hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad())
        assert _of_type(fs, "WIDE_SPREAD") == []

    def test_thin_book_fires_on_small_size_and_one_sided(self):
        thin = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52", ask_size="3.00")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [thin], NOW, _quad()), "THIN_BOOK")
        assert len(fs) == 1 and fs[0]["is_context"] is True
        one_sided = _mkt(f"{EV}-BBB", ask="0.52")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [one_sided], NOW, _quad()), "THIN_BOOK")
        assert len(fs) == 1
        assert "one_sided_or_empty_book" in fs[0]["legs"]["reasons"]

    def test_deep_two_sided_book_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52",
                 ask_size="50", bid_size="50")
        fs = hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad())
        assert _of_type(fs, "THIN_BOOK") == []


class TestInPlayOverreaction:
    PREV = {"yes_bid": "0.30", "yes_ask": "0.32",
            "captured_at": "2026-07-28T17:50:00+00:00"}

    def test_fires_capture_paired_with_conditionality(self):
        m = _mkt(f"{EV}-AAA", bid="0.55", ask="0.57")
        f = hunter.detect_overreaction("KXUCLGAME", EV, m, self.PREV, NOW)
        assert f is not None
        assert f["finding_type"] == "IN_PLAY_OVERREACTION"
        assert f["is_context"] is True          # never a win
        legs = f["legs"]
        assert legs["mid_move_dollars"] == "0.25"
        pair = legs["capture_pair"]
        assert pair["prev"]["captured_at"] == self.PREV["captured_at"]
        assert pair["now"]["captured_at"] == NOW.isoformat()
        assert pair["prev"]["mid_dollars"] == "0.31"
        assert pair["now"]["mid_dollars"] == "0.56"
        assert "UNOBSERVED" in legs["conditionality"]
        assert "never that the market is wrong" in legs["conditionality"]

    def test_small_move_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.40", ask="0.42")   # mid 0.41, Δ 0.10
        assert hunter.detect_overreaction(
            "KXUCLGAME", EV, m, self.PREV, NOW) is None

    def test_move_inside_the_spread_does_not_fire(self):
        # prev spread 0.50 dwarfs the 0.20 mid move: quote noise
        prev = {"yes_bid": "0.10", "yes_ask": "0.60",
                "captured_at": "2026-07-28T17:50:00+00:00"}
        m = _mkt(f"{EV}-AAA", bid="0.30", ask="0.80")
        assert hunter.detect_overreaction(
            "KXUCLGAME", EV, m, prev, NOW) is None

    def test_not_dated_today_does_not_fire(self):
        ev = "KXUCLGAME-26AUG04AAABBB"
        m = _mkt(f"{ev}-AAA", event=ev, bid="0.55", ask="0.57")
        assert hunter.detect_overreaction(
            "KXUCLGAME", ev, m, self.PREV, NOW) is None

    def test_no_previous_capture_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.55", ask="0.57")
        assert hunter.detect_overreaction(
            "KXUCLGAME", EV, m, None, NOW) is None


# ---------------------------------------------------------------------------
def _seed_mls_event(s, kickoff, espn_id="401", tickers=None,
                    suffix="HOMAWA"):
    home = Team(competition_slug="mls-2026",
                canonical_name=f"Home FC {suffix}")
    away = Team(competition_slug="mls-2026",
                canonical_name=f"Away FC {suffix}")
    s.add_all([home, away])
    s.flush()
    fx = Fixture(competition_slug="mls-2026", espn_event_id=espn_id,
                 home_team_id=home.id, away_team_id=away.id,
                 current_kickoff_utc=kickoff, status="in")
    s.add(fx)
    s.flush()
    ev_ticker = f"KXMLSGAME-{TODAY_SEG}{suffix}"
    me = MarketEvent(competition_slug="mls-2026",
                     kalshi_event_ticker=ev_ticker, series="KXMLSGAME",
                     title="Home FC vs Away FC", fixture_id=fx.id,
                     mapping_approved=True, mapped_via="alias")
    s.add(me)
    s.flush()
    tickers = tickers or {}
    for okey, tick in (("home_win", f"{ev_ticker}-HOM"),
                       ("draw", f"{ev_ticker}-TIE"),
                       ("away_win", f"{ev_ticker}-AWA")):
        s.add(MarketContract(market_event_id=me.id,
                             ticker=tickers.get(okey, tick),
                             side_label=okey, outcome_key=okey))
    s.commit()
    return fx, me, ev_ticker


def _espn_event(espn_id="401", state="post", home_goals="2",
                away_goals="1"):
    return {"id": espn_id, "competitions": [{
        "competitors": [
            {"homeAway": "home", "score": home_goals,
             "team": {"displayName": "Home FC"}},
            {"homeAway": "away", "score": away_goals,
             "team": {"displayName": "Away FC"}},
        ],
        "status": {"type": {"state": state}},
    }], "status": {"type": {"state": state}}}


def _cann_requote(monkeypatch, markets, clock=None, fail=()):
    """Cann the P0-2 post-finality re-read. `markets` is the ticker →
    market map returned AFTER finality; by default it is the same book,
    i.e. a market that has not repriced."""
    calls = []

    def fake(ticker):
        calls.append(ticker)
        import requests as _rq
        if ticker in fail:
            raise _rq.RequestException("re-quote failed")
        return markets.get(ticker), (clock() if callable(clock)
                                     else (clock or hunter._now()))
    monkeypatch.setattr(hunter, "_refetch_market", fake)
    return calls


class TestPostCertainty:
    def test_fires_on_finished_match_with_fresh_espn_read(
            self, live_session, monkeypatch):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        calls = []

        def fake_scoreboard(date_str):
            calls.append(date_str)
            return {"events": [_espn_event()]}
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            fake_scoreboard)
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.93")}
        _cann_requote(monkeypatch, markets)
        fs, complete = hunter.detect_post_certainty(
            live_session, markets, NOW, _quad("KXMLSGAME"))
        assert complete is True
        assert len(fs) == 1
        f = fs[0]
        # 1 - 0.93 - fee(0.93)=0.0046 → 0.0654
        assert f["net_margin_dollars"] == "0.0654"
        assert f["legs"]["certain_outcome"] == "home_win"
        assert f["legs"]["final_score_home"] == 2
        assert f["legs"]["final_score_away"] == 1
        # BOTH capture stamps present, and the QUOTE clock is the
        # post-finality re-read — never the earlier cycle capture
        assert f["espn_captured_at"] is not None
        assert f["captured_at"] >= f["espn_captured_at"]
        assert f["legs"]["kalshi_captured_at"] \
            == f["captured_at"].isoformat()
        assert f["legs"]["kalshi_captured_at"] != NOW.isoformat()
        assert f["legs"]["pre_finality_kalshi_captured_at"] \
            == NOW.isoformat()
        assert f["legs"]["espn_captured_at"]
        # the ESPN page is fetched for the fixture's US-Eastern local
        # date (IANA zone, never the runner's local zone or a fixed
        # offset): kickoff NOW-3h = 15:00 UTC Jul 28 = 11:00 EDT Jul 28
        from zoneinfo import ZoneInfo
        expected_date = ((NOW - timedelta(hours=3))
                         .astimezone(ZoneInfo("America/New_York"))
                         .strftime("%Y%m%d"))
        assert expected_date == "20260728"      # pinned, not recomputed
        assert calls == [expected_date]

        # the re-read happens EVERY detection — never cached from a
        # previous cycle
        hunter.detect_post_certainty(live_session, markets, NOW,
                                     _quad("KXMLSGAME"))
        assert len(calls) == 2

    def test_in_play_match_does_not_fire(self, live_session, monkeypatch):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(
            hunter, "_fetch_espn_scoreboard",
            lambda d: {"events": [_espn_event(state="in")]})
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.60")}
        requotes = _cann_requote(monkeypatch, markets)
        assert hunter.detect_post_certainty(
            live_session, markets, NOW, _quad("KXMLSGAME")) == ([], True)
        # no finality → no post-finality quote was even requested
        assert requotes == []

    def test_price_already_at_certainty_does_not_fire(
            self, live_session, monkeypatch):
        # ask 0.9999: margin = 0.0001 - fee(0.0001) = 0 → no finding
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            lambda d: {"events": [_espn_event()]})
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.9999")}
        _cann_requote(monkeypatch, markets)
        assert hunter.detect_post_certainty(
            live_session, markets, NOW, _quad("KXMLSGAME")) == ([], True)

    def test_draw_outcome_derived_from_score_numbers(
            self, live_session, monkeypatch):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(
            hunter, "_fetch_espn_scoreboard",
            lambda d: {"events": [_espn_event(home_goals="1",
                                              away_goals="1")]})
        markets = {f"{ev}-TIE": _mkt(f"{ev}-TIE", event=ev, ask="0.90")}
        _cann_requote(monkeypatch, markets)
        fs, complete = hunter.detect_post_certainty(
            live_session, markets, NOW, _quad("KXMLSGAME"))
        assert complete is True
        assert len(fs) == 1
        assert fs[0]["legs"]["certain_outcome"] == "draw"


# ---------------------------------------------------------------------------
def _seed_approval(s, decision=True, runtime_active=True,
                   content_hash=None):
    """Seed the two DISTINCT approval states P0-3 is about: the
    persisted immutable decision (`decision`) and the RUNTIME flag boot
    fails closed on (`runtime_active` → approved_for_shadow). Returns
    the decision id (None when no decision seeded)."""
    mv = (s.query(ModelVersion).filter_by(name="mls-2026-v0").first())
    if mv is None:
        mv = ModelVersion(name="mls-2026-v0",
                          approved_for_shadow=runtime_active)
        s.add(mv)
        s.flush()
    else:
        mv.approved_for_shadow = runtime_active
    dec_id = None
    if decision:
        row = ModelApprovalDecision(
            model_version_id=mv.id, model_version_name="mls-2026-v0",
            eval_version="model-eval-v1",
            policy_version="shadow-approval-v1",
            approved_mode="shadow", approved=True, n_scored=177,
            edge_json=json.dumps({"delta_log_loss": 0.0269,
                                  "ci95": [-0.0050, 0.0596],
                                  "significant": False}),
            decision_document="{}",
            content_hash=(content_hash or "h" * 64),
            created_at=NOW)
        s.add(row)
        s.flush()
        dec_id = row.id
    s.commit()
    return dec_id


class TestModelEdge:
    def _seed_run(self, s, fx, p_home=0.55, decision_id=None,
                  approved_at_run=True):
        """A complete run BOUND (by default) the way runs.py binds one:
        model_approved_at_run stamped and the authorizing decision id
        recorded. The P0-3 tests below unbind each in turn."""
        run = PredictionRun(fixture_id=fx.id, run_type="scheduled",
                            status="complete", created_at=NOW,
                            model_approved_at_run=approved_at_run,
                            model_approval_decision_id=decision_id)
        s.add(run)
        s.flush()
        s.add(PredictionContract(prediction_run_id=run.id,
                                 outcome_key="home_win",
                                 raw_probability=p_home,
                                 anchored_probability=p_home))
        s.commit()
        return run

    def test_readout_carries_the_standing_qualifier(self, live_session):
        dec_id = _seed_approval(live_session)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        self._seed_run(live_session, fx, decision_id=dec_id)
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.45")}
        fs = hunter.detect_model_edge(live_session, markets, NOW,
                                        _quad("KXMLSGAME"))
        assert len(fs) == 1
        f = fs[0]
        # 0.55 - 0.45 - fee(0.45)=0.0174 → 0.0826
        assert f["net_margin_dollars"] == "0.0826"
        q = f["model_qualifier"]
        assert q["edge_vs_baseline"] == 0.0269
        assert q["ci_low"] == -0.0050 and q["ci_high"] == 0.0596
        assert q["n_scored"] == 177
        assert q["significant"] is False
        assert "never read this as an established edge" in q["note"]
        # the qualifier rides inside the stored arithmetic too
        assert f["legs"]["standing_qualifier"]["significant"] is False
        # P0-3 acceptance (d): the emitted edge is BOUND — the published
        # qualifier's decision id EQUALS the run's authorizing decision
        assert q["decision_id"] == dec_id
        assert f["legs"]["model_approval_decision_id"] == dec_id

    def test_no_approval_means_no_numbers(self, live_session):
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        self._seed_run(live_session, fx)
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.45")}
        assert hunter.detect_model_edge(live_session, markets, NOW,
                                        _quad("KXMLSGAME")) == []
        rep = hunter.findings_report()
        assert rep["model_status"]["mls-2026"] == "no model"

    def test_edge_below_threshold_does_not_fire(self, live_session):
        dec_id = _seed_approval(live_session)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        self._seed_run(live_session, fx, decision_id=dec_id)
        # 0.55 - 0.54 - fee = negative
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.54")}
        assert hunter.detect_model_edge(live_session, markets, NOW,
                                        _quad("KXMLSGAME")) == []


class TestModelEdgeRuntimeBinding:
    """P0-3: MODEL_EDGE must be bound to the RUNTIME approval state and
    to the run's own authorization — the reviewer reproduced an edge
    row emitted during the exact boot fail-closed window."""

    def _markets(self, ev):
        return {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.45")}

    def test_failclosed_window_emits_no_edge(self, live_session):
        # the reviewer's repro, exactly: an older persisted APPROVED
        # decision exists, but boot has set approved_for_shadow=False
        # and the run was minted unapproved (model_approved_at_run
        # False). No edge may exist, and the API must say the model is
        # not ACTIVE rather than quoting the stale decision.
        dec_id = _seed_approval(live_session, runtime_active=False)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        TestModelEdge._seed_run(TestModelEdge(), live_session, fx,
                                decision_id=dec_id,
                                approved_at_run=False)
        assert hunter.detect_model_edge(
            live_session, self._markets(ev), NOW,
            _quad("KXMLSGAME")) == []
        rep = hunter.findings_report()
        assert "no active model" in rep["model_status"]["mls-2026"]

    def test_runtime_flag_off_blocks_even_a_bound_run(self, live_session):
        # acceptance (a): decision present + approved_for_shadow=False
        # → NO edge, even when the run itself is fully bound
        dec_id = _seed_approval(live_session, runtime_active=False)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        TestModelEdge._seed_run(TestModelEdge(), live_session, fx,
                                decision_id=dec_id)
        assert hunter.detect_model_edge(
            live_session, self._markets(ev), NOW,
            _quad("KXMLSGAME")) == []
        rep = hunter.findings_report()
        assert "no active model" in rep["model_status"]["mls-2026"]

    def test_run_without_contemporaneous_approval_yields_no_edge(
            self, live_session):
        # acceptance (b): runtime active, but the run was NOT approved
        # at capture time → no edge
        dec_id = _seed_approval(live_session)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        TestModelEdge._seed_run(TestModelEdge(), live_session, fx,
                                decision_id=dec_id,
                                approved_at_run=False)
        assert hunter.detect_model_edge(
            live_session, self._markets(ev), NOW,
            _quad("KXMLSGAME")) == []

    def test_mismatched_decision_id_yields_no_edge(self, live_session):
        # acceptance (c): the run is bound to an OLDER decision than the
        # one the qualifier publishes → no edge (an edge under decision
        # N must never ride a run authorized under decision M)
        old_id = _seed_approval(live_session, content_hash="a" * 64)
        new_id = _seed_approval(live_session, content_hash="b" * 64)
        assert new_id > old_id
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        TestModelEdge._seed_run(TestModelEdge(), live_session, fx,
                                decision_id=old_id)
        assert hunter.detect_model_edge(
            live_session, self._markets(ev), NOW,
            _quad("KXMLSGAME")) == []


# ---------------------------------------------------------------------------
def _fake_paged(books):
    """Canned _paged_markets: a value per series that is a list (clean
    scan), a (list, truncated) tuple, or an Exception to raise."""
    def fake(series, counter):
        v = books.get(series, [])
        if isinstance(v, Exception):
            raise v
        if isinstance(v, tuple):
            return v
        return v, False
    return fake


def _run_cycle(monkeypatch, books, alerts=None, accept=True,
               fee_types=None, requote=None, requote_fail=None):
    """One scan_cycle against canned per-series market lists.

    The fake send_alert records (title, msg) and reports per-transport
    acceptance per the P0-2 alert contract (accept=False simulates all
    transports failing).

    `fee_types` cans each series' PROVIDER-DECLARED fee schedule the way
    discovery would read it (default: 'quadratic', the schedule this
    repo models); a series mapped to None has no readable schedule at
    all. `requote` cans the POST_CERTAINTY post-finality re-read per
    ticker — by default it returns the same book the cycle scanned, so
    a market that has NOT repriced behaves as before; `requote_fail`
    lists tickers whose re-read raises."""
    monkeypatch.setattr(config, "HUNTER_SERIES", list(books.keys()))
    monkeypatch.setattr(hunter, "_paged_markets", _fake_paged(books))

    fee_types = {} if fee_types is None else fee_types

    def fake_fee(ticker, counter):
        counter["requests"] = counter.get("requests", 0) + 1
        ft = fee_types.get(ticker, "quadratic")
        if ft is None:
            return None
        return {"fee_type": ft, "fee_multiplier": 1}
    monkeypatch.setattr(hunter, "_fetch_series_fee_schedule", fake_fee)

    scanned = {}
    for v in books.values():
        ms = v[0] if isinstance(v, tuple) else v
        if isinstance(ms, list):
            for m in ms:
                if m.get("ticker"):
                    scanned[m["ticker"]] = m

    def fake_requote(ticker):
        import requests as _rq
        if requote_fail and ticker in requote_fail:
            raise _rq.RequestException("re-quote failed")
        m = (requote or {}).get(ticker, scanned.get(ticker))
        return m, hunter._now()
    monkeypatch.setattr(hunter, "_refetch_market", fake_requote)

    if alerts is not None:
        import src.alerts as alerts_mod

        def fake_send(msg, title="x", **kw):
            alerts.append((title, msg))
            return {"discord_action": bool(accept), "ntfy": False}
        monkeypatch.setattr(alerts_mod, "send_alert", fake_send)
    return hunter.scan_cycle()


class TestScanCycleLifecycle:
    def test_finding_dedupes_then_expires_never_deletes(
            self, live_session, monkeypatch):
        arb = [_mkt(f"{EV}-AAA", bid="0.28", ask="0.30"),
               _mkt(f"{EV}-TIE", bid="0.30", ask="0.32"),
               _mkt(f"{EV}-BBB", bid="0.28", ask="0.30")]
        r1 = _run_cycle(monkeypatch, {"KXUCLGAME": arb}, alerts=[])
        assert r1["findings_new"] >= 1
        rows = (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").all())
        assert len(rows) == 1 and rows[0].status == "open"

        # same anomaly next cycle: no duplicate row, bookkeeping only
        _run_cycle(monkeypatch, {"KXUCLGAME": arb}, alerts=[])
        live_session.expire_all()
        rows = (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").all())
        assert len(rows) == 1
        assert rows[0].observed_cycles == 2
        first_arithmetic = rows[0].legs_json

        # anomaly gone: the row EXPIRES with a second timestamp — the
        # original arithmetic is untouched and nothing is deleted
        _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                   alerts=[])
        live_session.expire_all()
        rows = (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").all())
        assert len(rows) == 1
        assert rows[0].status == "expired"
        assert rows[0].expired_at is not None
        assert rows[0].legs_json == first_arithmetic

    def test_cycle_rows_are_the_heartbeat_and_denominator(
            self, live_session, monkeypatch):
        _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                   alerts=[])
        _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                   alerts=[])
        cycles = live_session.query(HunterCycle).all()
        assert len(cycles) == 2
        assert all(c.status == "complete" for c in cycles)
        assert cycles[-1].markets_scanned == 3
        rep = hunter.findings_report()
        d = rep["denominators"]
        assert d["cycles_run"] == 2
        assert d["markets_scanned_total"] == 6
        assert d["last_cycle"]["age_seconds"] is not None
        assert "DEAD" in d["heartbeat_note"]

    def test_disabled_and_dormant_short_circuit(self, monkeypatch):
        monkeypatch.setattr(config, "HUNTER_ENABLED", False)
        assert hunter.scan_cycle() == {"skipped": "disabled"}
        monkeypatch.setattr(config, "HUNTER_ENABLED", True)
        monkeypatch.setattr(config, "LIVE_DATABASE_URL", "")
        assert hunter.scan_cycle() == {"skipped": "dormant"}


class TestAlertDiscipline:
    ARB = [_mkt(f"{EV}-AAA", bid="0.28", ask="0.30"),
           _mkt(f"{EV}-TIE", bid="0.30", ask="0.32"),
           _mkt(f"{EV}-BBB", bid="0.28", ask="0.30")]

    def test_structural_finding_alerts_observationally_once(
            self, live_session, monkeypatch):
        sent = []
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=sent)
        assert len(sent) == 1
        title, msg = sent[0]
        assert "SUM_BELOW_ONE" in msg
        assert "net margin $0.0353" in msg
        assert "shadow mode" in msg
        assert "no order" in msg
        # observational, never imperative
        for banned in ("BUY", "TAKE", "SELL", "EASY WIN", "easy win"):
            assert banned not in msg
        # a repeat cycle of the SAME open finding does not re-alert
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=sent)
        assert len(sent) == 1
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        # CONTROL for the P0-2 path: confirmed delivery marked the row,
        # kept the durable claim, retained the per-transport results,
        # and consumed exactly one budget slot
        assert row.alerted_at is not None
        assert row.alert_claimed_at is not None
        res = json.loads(row.alert_results_json)
        assert res["attempts"] == 1
        assert res["last"]["accepted"] is True
        assert res["last"]["results"] == {"discord_action": True,
                                          "ntfy": False}
        assert len(hunter._alert_times) == 1

    def test_context_findings_never_alert(self, live_session, monkeypatch):
        sent = []
        wide = [_mkt(f"{EV}-AAA", bid="0.05", ask="0.80", ask_size="2")]
        _run_cycle(monkeypatch, {"KXUCLGAME": wide}, alerts=sent)
        assert sent == []
        rows = live_session.query(HunterFinding).all()
        assert rows and all(r.is_context for r in rows)

    def test_margin_below_threshold_records_but_stays_quiet(
            self, live_session, monkeypatch):
        monkeypatch.setattr(config, "HUNTER_ALERT_MIN_MARGIN_DOLLARS",
                            "0.05")
        sent = []
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=sent)
        assert sent == []
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE",
                           status="open").count() == 1)

    def test_hourly_alert_budget(self, live_session, monkeypatch):
        monkeypatch.setattr(config, "HUNTER_ALERT_MAX_PER_HOUR", 1)
        ev2 = f"KXEPLGAME-{TODAY_SEG}CCCDDD"
        arb2 = [_mkt(f"{ev2}-CCC", event=ev2, bid="0.28", ask="0.30"),
                _mkt(f"{ev2}-TIE", event=ev2, bid="0.30", ask="0.32"),
                _mkt(f"{ev2}-DDD", event=ev2, bid="0.28", ask="0.30")]
        sent = []
        _run_cycle(monkeypatch,
                   {"KXUCLGAME": self.ARB, "KXEPLGAME": arb2},
                   alerts=sent)
        assert len(sent) == 1          # second suppressed by the budget
        # both findings still recorded — the budget throttles the
        # channel, never the record
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").count() == 2)


# ---------------------------------------------------------------------------
class TestFindingsApi:
    def test_endpoint_serves_findings_with_denominators(
            self, live_session, monkeypatch):
        from fastapi.testclient import TestClient
        from api import main as api_main
        _run_cycle(monkeypatch, {"KXUCLGAME": [
            _mkt(f"{EV}-AAA", bid="0.28", ask="0.30"),
            _mkt(f"{EV}-TIE", bid="0.30", ask="0.32"),
            _mkt(f"{EV}-BBB", bid="0.28", ask="0.30")]}, alerts=[])
        client = TestClient(api_main.app)
        r = client.get("/api/hunter/findings")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["denominators"]["cycles_run"] == 1
        assert body["denominators"]["markets_scanned_total"] == 3
        assert body["findings_per_type"]["SUM_BELOW_ONE"]["open"] == 1
        assert "shadow mode" in body["framing"]
        assert "capture" in body["capture_clock"]
        f = [x for x in body["findings"]
             if x["finding_type"] == "SUM_BELOW_ONE"][0]
        assert f["net_margin_dollars"] == "0.0353"
        assert f["legs"]["sum_asks_dollars"] == "0.92"

        # filters
        r = client.get("/api/hunter/findings",
                       params={"type": "SUM_BELOW_ONE"})
        assert all(x["finding_type"] == "SUM_BELOW_ONE"
                   for x in r.json()["findings"])
        r = client.get("/api/hunter/findings",
                       params={"competition": "kxuclgame"})
        assert len(r.json()["findings"]) >= 1
        r = client.get("/api/hunter/findings",
                       params={"status": "expired"})
        assert r.json()["findings"] == []

    def test_dormant_plane_reports_not_ready(self, monkeypatch):
        from fastapi.testclient import TestClient
        from api import main as api_main
        monkeypatch.setattr(config, "LIVE_DATABASE_URL", "")
        r = TestClient(api_main.app).get("/api/hunter/findings")
        assert r.status_code == 200
        assert r.json()["ready"] is False


# ---------------------------------------------------------------------------
class TestSchemaParityGuard:
    def test_varchar_guard_fires_on_the_new_table(self, live_session):
        """Prove the PostgreSQL-parity guard is ACTIVE for hunter rows:
        an over-long provider string must raise here exactly as
        PostgreSQL would (StringDataRightTruncation class)."""
        live_session.add(HunterFinding(
            series="X" * 65, finding_type="SUM_BELOW_ONE",
            finding_key="k1", legs_json="{}", first_captured_at=NOW))
        with pytest.raises(ValueError, match="value too long"):
            live_session.commit()
        live_session.rollback()

    def test_generous_ticker_widths_accept_real_tickers(self, live_session):
        tick = "KXCONMEBOLLIBGAME-26JUL30CARSFE-CAR"
        live_session.add(HunterFinding(
            series="KXCONMEBOLLIBGAME",
            event_ticker="KXCONMEBOLLIBGAME-26JUL30CARSFE",
            market_ticker=tick,
            finding_type="SUM_BELOW_ONE",
            finding_key=f"SUM_BELOW_ONE|KXCONMEBOLLIBGAME|"
                        f"KXCONMEBOLLIBGAME-26JUL30CARSFE|{tick}",
            legs_json="{}", first_captured_at=NOW,
            fee_policy_version="kalshi-fee-2026-07-general"))
        live_session.commit()


class TestSchedulerRegistration:
    def test_hunter_job_registered_with_guards(self):
        import jobs.scheduler as sched

        class _Rec:
            def __init__(self, timezone=None):
                self.jobs = []

            def add_job(self, func, trigger=None, **kw):
                self.jobs.append((func, trigger, kw))

            def start(self):
                pass
        orig = sched.BackgroundScheduler
        sched.BackgroundScheduler = _Rec
        try:
            s = sched.start_scheduler()
        finally:
            sched.BackgroundScheduler = orig
        hunter_jobs = [(f, t, kw) for f, t, kw in s.jobs
                       if kw.get("id") == "hunter"]
        assert len(hunter_jobs) == 1
        _, trigger, kw = hunter_jobs[0]
        assert trigger == "interval"
        assert kw["minutes"] == config.HUNTER_POLL_MINUTES
        assert kw["coalesce"] is True and kw["max_instances"] == 1


# ---------------------------------------------------------------------------
def _arb(series):
    """A SUM_BELOW_ONE book under the given series' own event ticker."""
    ev = f"{series}-{TODAY_SEG}AAABBB"
    return [_mkt(f"{ev}-AAA", event=ev, bid="0.28", ask="0.30"),
            _mkt(f"{ev}-TIE", event=ev, bid="0.30", ask="0.32"),
            _mkt(f"{ev}-BBB", event=ev, bid="0.28", ask="0.30")]


def _last_cycle_outcomes(session):
    import src.live.models as _m
    row = (session.query(_m.HunterCycle)
           .order_by(_m.HunterCycle.id.desc()).first())
    return json.loads(row.series_outcomes_json or "{}"), row


class TestCompletenessFailClosed:
    """P0-1: "didn't look" must never be recordable as "no anomaly".
    Expiry requires a COMPLETE SUCCESS pass over the finding's series;
    failures are per-series recorded outcomes; failed series stay
    scheduled; the cycle degrades; the API exposes the incompleteness."""

    def test_transient_series_failure_degrades_and_keeps_scheduled(
            self, live_session, monkeypatch):
        import requests as _rq
        books1 = {"KXAAAGAME": _arb("KXAAAGAME"),
                  "KXBBBGAME": _arb("KXBBBGAME")}
        r1 = _run_cycle(monkeypatch, books1, alerts=[])
        assert r1["status"] == "complete" and r1["findings_new"] >= 2

        # cycle 2: series A fails transiently, B scans clean (its
        # anomaly gone)
        books2 = {"KXAAAGAME": _rq.RequestException("connection reset"),
                  "KXBBBGAME": _healthy_three_way()}
        r2 = _run_cycle(monkeypatch, books2, alerts=[])
        assert r2["status"] == "degraded"
        assert r2["series_failed"] == 1
        live_session.expire_all()
        a_row = (live_session.query(HunterFinding)
                 .filter_by(series="KXAAAGAME",
                            finding_type="SUM_BELOW_ONE").one())
        b_row = (live_session.query(HunterFinding)
                 .filter_by(series="KXBBBGAME",
                            finding_type="SUM_BELOW_ONE").one())
        # the failed series' finding CANNOT expire; the clean series'
        # finding does (control: expiry itself still works)
        assert a_row.status == "open"
        assert b_row.status == "expired"
        outcomes, cyc = _last_cycle_outcomes(live_session)
        assert outcomes["KXAAAGAME"]["outcome"] == "REQUEST_FAILED"
        assert "connection reset" in outcomes["KXAAAGAME"]["detail"]
        assert outcomes["KXBBBGAME"]["outcome"] == "SUCCESS"
        assert cyc.status == "degraded" and cyc.series_failed == 1
        # the failed series stays SCHEDULED (previously it silently
        # dropped from the active set until the 6h discovery)
        assert "KXAAAGAME" in hunter._roster["active"]
        # and the API exposes the incompleteness with its reason
        rep = hunter.findings_report()
        inc = rep["denominators"]["last_cycle"]["incomplete_series"]
        assert inc["KXAAAGAME"]["outcome"] == "REQUEST_FAILED"

    def test_detector_failure_poisons_only_that_series(
            self, live_session, monkeypatch):
        books1 = {"KXAAAGAME": _arb("KXAAAGAME"),
                  "KXBBBGAME": _arb("KXBBBGAME")}
        _run_cycle(monkeypatch, books1, alerts=[])

        real = hunter.detect_event_findings

        def flaky(series, ev, ms, cap, fees):
            if series == "KXAAAGAME":
                raise RuntimeError("detector exploded")
            return real(series, ev, ms, cap, fees)
        monkeypatch.setattr(hunter, "detect_event_findings", flaky)
        books2 = {"KXAAAGAME": _arb("KXAAAGAME"),
                  "KXBBBGAME": _healthy_three_way()}
        r2 = _run_cycle(monkeypatch, books2, alerts=[])
        assert r2["status"] == "degraded"
        live_session.expire_all()
        # A's finding survived the partial pass; B was still scanned
        # (the failure did NOT kill the loop) and expired cleanly
        assert (live_session.query(HunterFinding)
                .filter_by(series="KXAAAGAME",
                           finding_type="SUM_BELOW_ONE").one()
                .status == "open")
        assert (live_session.query(HunterFinding)
                .filter_by(series="KXBBBGAME",
                           finding_type="SUM_BELOW_ONE").one()
                .status == "expired")
        outcomes, _ = _last_cycle_outcomes(live_session)
        assert outcomes["KXAAAGAME"]["outcome"] == "DETECTION_FAILED"
        assert outcomes["KXBBBGAME"]["outcome"] == "SUCCESS"

    def test_pagination_cap_is_incomplete_scope_not_no_anomaly(
            self, live_session, monkeypatch):
        _run_cycle(monkeypatch, {"KXAAAGAME": _arb("KXAAAGAME")},
                   alerts=[])
        # cycle 2: the page cap hit with a SURVIVING cursor — the books
        # we did see look clean, but the scope is incomplete
        r2 = _run_cycle(monkeypatch,
                        {"KXAAAGAME": (_healthy_three_way(), True)},
                        alerts=[])
        assert r2["status"] == "degraded"
        live_session.expire_all()
        assert (live_session.query(HunterFinding)
                .filter_by(series="KXAAAGAME",
                           finding_type="SUM_BELOW_ONE").one()
                .status == "open")
        outcomes, _ = _last_cycle_outcomes(live_session)
        assert outcomes["KXAAAGAME"]["outcome"] == "PAGINATION_CAP"
        assert "INCOMPLETE" in outcomes["KXAAAGAME"]["detail"]
        rep = hunter.findings_report()
        inc = rep["denominators"]["last_cycle"]["incomplete_series"]
        assert inc["KXAAAGAME"]["outcome"] == "PAGINATION_CAP"

    def test_espn_failure_blocks_post_certainty_expiry(
            self, live_session, monkeypatch):
        import requests as _rq
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            lambda d: {"events": [_espn_event()]})
        books = {"KXMLSGAME": [_mkt(f"{ev}-HOM", event=ev, ask="0.93")]}
        r1 = _run_cycle(monkeypatch, books, alerts=[])
        assert r1["findings_new"] >= 1
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="POST_CERTAINTY").one())
        assert row.status == "open"

        # cycle 2: the ESPN re-read fails — the pass over MLS is
        # INCOMPLETE, so the POST_CERTAINTY finding must NOT expire
        def boom(date_str):
            raise _rq.RequestException("espn 503")
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard", boom)
        r2 = _run_cycle(monkeypatch, books, alerts=[])
        assert r2["status"] == "degraded"
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="POST_CERTAINTY").one())
        assert row.status == "open"
        outcomes, _ = _last_cycle_outcomes(live_session)
        assert outcomes["KXMLSGAME"]["outcome"] == "DETECTION_FAILED"
        assert "ESPN" in outcomes["KXMLSGAME"]["detail"]


class TestAlertDurability:
    """P0-2: alerted_at means a CONFIRMED transport acceptance after a
    durable claim — never a fire-and-forget guess, never before the
    finding itself is committed."""
    ARB = TestAlertDiscipline.ARB

    def test_all_transports_failing_leaves_finding_retryable(
            self, live_session, monkeypatch):
        sent = []
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB},
                   alerts=sent, accept=False)
        assert len(sent) == 1                 # an attempt WAS made
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.alerted_at is None         # NOT marked alerted
        assert row.alert_claimed_at is None   # claim released → retryable
        res = json.loads(row.alert_results_json)
        assert res["attempts"] == 1
        assert res["last"]["accepted"] is False
        assert res["last"]["results"] == {"discord_action": False,
                                          "ntfy": False}
        assert len(hunter._alert_times) == 0  # budget NOT consumed

        # next cycle retries the still-open finding...
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB},
                   alerts=sent, accept=False)
        assert len(sent) == 2
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert json.loads(row.alert_results_json)["attempts"] == 2
        assert row.alerted_at is None

        # ...until a transport finally accepts
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB},
                   alerts=sent, accept=True)
        assert len(sent) == 3
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.alerted_at is not None
        assert len(hunter._alert_times) == 1

    def test_unconfigured_transports_never_mark_alerted(
            self, live_session, monkeypatch):
        # REAL send_alert with nothing configured: the old code took
        # "no exception" as success and permanently muted the finding
        import src.alerts as alerts_mod
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL", "")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL", "")
        monkeypatch.setattr(config, "NTFY_TOPIC", "")

        def no_network(*a, **kw):             # nothing may leave at all
            raise AssertionError("network call with nothing configured")
        monkeypatch.setattr(alerts_mod.requests, "post", no_network)
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB})
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.alerted_at is None
        assert row.alert_claimed_at is None
        assert json.loads(
            row.alert_results_json)["last"]["accepted"] is False
        assert len(hunter._alert_times) == 0

    def test_db_failure_cannot_leak_an_alert(
            self, live_session, monkeypatch):
        # forced first-commit failure: nothing may reach a transport
        # before a durable claim exists
        real_get = hunter.get_session
        state = {"n": 0}

        def flaky_session():
            s = real_get()
            real_commit = s.commit

            def commit():
                state["n"] += 1
                if state["n"] == 1:
                    raise RuntimeError("forced commit failure")
                real_commit()
            s.commit = commit
            return s
        monkeypatch.setattr(hunter, "get_session", flaky_session)
        sent = []
        r = _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=sent)
        assert sent == []                     # NO external alert leaked
        assert r["status"] == "failed"
        live_session.expire_all()
        assert live_session.query(HunterFinding).count() == 0
        # the failure is still a visible heartbeat, not silence
        cyc = (live_session.query(HunterCycle)
               .order_by(HunterCycle.id.desc()).first())
        assert cyc.status == "failed"
        assert cyc.error.startswith("persist:")

    def test_send_alert_returns_per_transport_results(self, monkeypatch):
        # CONTROL for the new src.alerts contract (the old one returned
        # None): per-transport booleans, unconfigured == False
        import src.alerts as alerts_mod
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL",
                            "https://a.example")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL",
                            "https://d.example")
        monkeypatch.setattr(
            alerts_mod, "send_discord",
            lambda msg, channel="action": channel == "action")
        monkeypatch.setattr(alerts_mod, "send_ntfy",
                            lambda msg, title="x", **kw: False)
        assert alerts_mod.send_alert("m") == {"discord_action": True,
                                              "discord_detail": False,
                                              "ntfy": False}
        assert alerts_mod.send_alert("m", kind="detail") == {
            "discord_detail": False}


class TestCaptureClock:
    """P0-4: the per-series fetch-completion clock is the quotes' ONLY
    timing evidence and must survive to the row, even for detectors
    that run at cycle end on a later wall clock."""

    def test_mls_findings_keep_the_mls_fetch_clock(
            self, live_session, monkeypatch):
        # fake clock advancing 40s per call — the reviewer's measured
        # drift in a controlled scan
        state = {"t": NOW}

        def fake_now():
            state["t"] += timedelta(seconds=40)
            return state["t"]
        monkeypatch.setattr(hunter, "_now", fake_now)
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            lambda d: {"events": [_espn_event()]})
        ucl_ev = f"KXUCLGAME-{TODAY_SEG}CCCDDD"
        books = {
            # MLS scanned FIRST: its fetch clock is the earliest
            "KXMLSGAME": [_mkt(f"{ev}-HOM", event=ev, ask="0.93")],
            "KXUCLGAME": [
                _mkt(f"{ucl_ev}-CCC", event=ucl_ev, bid="0.28",
                     ask="0.30"),
                _mkt(f"{ucl_ev}-TIE", event=ucl_ev, bid="0.30",
                     ask="0.32"),
                _mkt(f"{ucl_ev}-DDD", event=ucl_ev, bid="0.28",
                     ask="0.30")],
        }
        _run_cycle(monkeypatch, books, alerts=[])
        live_session.expire_all()
        post = (live_session.query(HunterFinding)
                .filter_by(finding_type="POST_CERTAINTY").one())
        ucl = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        # SQLite hands back naive datetimes; every value stored was UTC
        def utc(dt):
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        legs = json.loads(post.legs_json)
        # the MLS SERIES fetch clock is the earliest thing in the cycle
        # and is retained verbatim as the pre-finality evidence, even
        # though the POST_CERTAINTY detector runs at cycle end
        assert legs["pre_finality_kalshi_captured_at"] \
            < utc(ucl.first_captured_at).isoformat()
        assert utc(post.detected_at) > utc(ucl.first_captured_at)
        # but the PRICED quote is the post-finality re-read (P0-2): its
        # clock is the row's capture clock, it matches the stored
        # arithmetic, and it is at or after the ESPN finality read
        assert utc(post.first_captured_at).isoformat() \
            == legs["kalshi_captured_at"]
        assert utc(post.first_captured_at) >= utc(post.espn_captured_at)
        assert legs["kalshi_captured_at"] \
            > legs["pre_finality_kalshi_captured_at"]
        assert utc(post.persisted_at) > utc(post.detected_at)
        # same discipline for the pure detector: capture == its series'
        # fetch clock, persistence later
        ucl_legs = json.loads(ucl.legs_json)
        assert utc(ucl.first_captured_at).isoformat() \
            == ucl_legs["captured_at"]
        assert utc(ucl.persisted_at) > utc(ucl.first_captured_at)


class TestCrossProcessIdempotency:
    """P1-5 (dialect-portable part): the partial unique index, the
    insert-on-conflict adoption path, and the atomic leased alert claim.
    The barriered two-thread PG race lives in
    tests/test_postgres_integration.py."""

    def _fdict(self, margin="0.05"):
        return {"finding_type": "SUM_BELOW_ONE", "series": "KXUCLGAME",
                "event_ticker": EV, "market_ticker": None,
                "is_context": False, "net_margin_dollars": margin,
                "captured_at": NOW, "detected_at": NOW,
                "alertable_liquidity": True, "limiting_quantity": "10",
                "legs": {"rule": "test"}}

    def test_partial_unique_index_one_open_row_per_key(
            self, live_session):
        from sqlalchemy.exc import IntegrityError
        f = self._fdict()
        key = hunter._finding_key(f)
        assert hunter._insert_finding_row(
            live_session, f, key, NOW) is not None
        live_session.commit()
        # a second OPEN row with the same key is refused BY THE DATABASE
        live_session.add(HunterFinding(
            series="KXUCLGAME", event_ticker=EV,
            finding_type="SUM_BELOW_ONE", finding_key=key,
            legs_json="{}", first_captured_at=NOW, status="open"))
        with pytest.raises(IntegrityError):
            live_session.commit()
        live_session.rollback()
        # an EXPIRED row with the same key is fine — append-only history
        live_session.add(HunterFinding(
            series="KXUCLGAME", event_ticker=EV,
            finding_type="SUM_BELOW_ONE", finding_key=key,
            legs_json="{}", first_captured_at=NOW, status="expired"))
        live_session.commit()

    def test_insert_race_surfaces_at_commit_loser_adopts(
            self, live_session):
        from sqlalchemy.exc import IntegrityError
        f = self._fdict()
        key = hunter._finding_key(f)
        hunter._insert_finding_row(live_session, f, key, NOW)
        live_session.commit()
        # a second session (≈ the overlapping container) inserting the
        # same open key is rejected BY THE DATABASE at commit; after
        # rollback it adopts the winner's row
        s2 = live_db.get_session()
        try:
            hunter._insert_finding_row(s2, f, key, NOW)
            with pytest.raises(IntegrityError):
                s2.commit()
            s2.rollback()
            assert (s2.query(HunterFinding)
                    .filter_by(finding_key=key, status="open")
                    .one()) is not None
        finally:
            s2.close()
        assert (live_session.query(HunterFinding)
                .filter_by(finding_key=key, status="open").count() == 1)

    def test_scan_cycle_retries_once_and_adopts_on_race(
            self, live_session, monkeypatch):
        # simulate the overlapping container winning the insert BETWEEN
        # this cycle's open-rows read and its commit. The injection
        # point is our transaction's FIRST write — the observation
        # watermark — which is precisely that window (SQLite allows the
        # other connection to write only while ours still holds no
        # write lock; PostgreSQL has no such restriction).
        real_wm = hunter._advance_watermark
        raced = {"done": False}

        def racing_watermark(s, key, clock):
            if not raced["done"] and key.startswith("SUM_BELOW_ONE"):
                raced["done"] = True
                other = live_db.get_session()
                try:
                    other.add(HunterFinding(
                        series="KXUCLGAME", event_ticker=EV,
                        finding_type="SUM_BELOW_ONE", finding_key=key,
                        legs_json="{}", first_captured_at=NOW,
                        last_seen_at=NOW, status="open"))
                    other.commit()          # the other container wins
                finally:
                    other.close()
            return real_wm(s, key, clock)
        monkeypatch.setattr(hunter, "_advance_watermark", racing_watermark)
        sent = []
        r = _run_cycle(monkeypatch,
                       {"KXUCLGAME": TestAlertDiscipline.ARB},
                       alerts=sent)
        live_session.expire_all()
        # ONE open row despite the race; the retry pass ADOPTED the
        # winner's row rather than counting a new finding
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE",
                           status="open").count() == 1)
        assert r["findings_new"] == 0
        # and the alert went out exactly once (via the claim)
        assert len(sent) == 1
        assert r["alerted"] == 1

    def test_alert_claim_single_winner_lease_and_finality(
            self, live_session):
        f = self._fdict()
        key = hunter._finding_key(f)
        row = hunter._insert_finding_row(live_session, f, key, NOW)
        live_session.commit()
        rid = row.id
        # exactly one claim wins
        assert hunter._try_claim_alert(live_session, rid, NOW) is True
        assert hunter._try_claim_alert(live_session, rid, NOW) is False
        # a dead claimant's lease expires rather than muting the finding
        later = NOW + timedelta(
            minutes=hunter.ALERT_CLAIM_LEASE_MINUTES + 1)
        assert hunter._try_claim_alert(live_session, rid, later) is True
        # confirmed delivery closes the claim permanently
        live_session.query(HunterFinding).filter_by(id=rid).update(
            {"alerted_at": later})
        live_session.commit()
        assert hunter._try_claim_alert(
            live_session, rid, later + timedelta(hours=2)) is False


class TestLiquidityUnknown:
    """P1-6: missing top-of-book size is UNKNOWN liquidity, never
    assumed depth; structural findings alert only with positive
    executable size proven on every required leg."""

    def _sizeless_arb(self):
        return [_mkt(f"{EV}-AAA", bid="0.28", ask="0.30",
                     ask_size=None, bid_size=None),
                _mkt(f"{EV}-TIE", bid="0.30", ask="0.32",
                     ask_size=None, bid_size=None),
                _mkt(f"{EV}-BBB", bid="0.28", ask="0.30",
                     ask_size=None, bid_size=None)]

    def test_sizeless_two_sided_book_is_liquidity_unknown(self):
        m = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52",
                 ask_size=None, bid_size=None)
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad()), "THIN_BOOK")
        assert len(fs) == 1
        assert fs[0]["is_context"] is True
        reasons = fs[0]["legs"]["reasons"]
        assert any("liquidity_unknown" in r for r in reasons)
        assert "yes_ask_size_missing:liquidity_unknown" in reasons
        assert "yes_bid_size_missing:liquidity_unknown" in reasons

    def test_missing_size_records_finding_but_never_alerts(
            self, live_session, monkeypatch):
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, self._sizeless_arb(), NOW, _quad()),
            "SUM_BELOW_ONE")
        assert len(fs) == 1
        assert fs[0]["alertable_liquidity"] is False
        assert fs[0]["limiting_quantity"] is None
        assert fs[0]["legs"]["liquidity"]["status"] == "liquidity_unknown"
        # full-cycle: recorded, above the margin bar, but NO alert
        sent = []
        _run_cycle(monkeypatch, {"KXUCLGAME": self._sizeless_arb()},
                   alerts=sent)
        assert sent == []
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.status == "open" and row.alerted_at is None

    def test_zero_size_not_alertable(self):
        legs = [_mkt(f"{EV}-AAA", bid="0.28", ask="0.30", ask_size="0"),
                _mkt(f"{EV}-TIE", bid="0.30", ask="0.32"),
                _mkt(f"{EV}-BBB", bid="0.28", ask="0.30")]
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad()), "SUM_BELOW_ONE")
        assert len(fs) == 1
        assert fs[0]["alertable_liquidity"] is False
        assert fs[0]["legs"]["liquidity"]["status"] == "zero_size"

    def test_positive_sizes_carry_limiting_quantity(
            self, live_session, monkeypatch):
        # CONTROL: sized on every leg → alertable, limited by the min
        legs = [_mkt(f"{EV}-AAA", bid="0.28", ask="0.30", ask_size="50"),
                _mkt(f"{EV}-TIE", bid="0.30", ask="0.32", ask_size="30"),
                _mkt(f"{EV}-BBB", bid="0.28", ask="0.30", ask_size="40")]
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _quad()), "SUM_BELOW_ONE")
        assert fs[0]["alertable_liquidity"] is True
        assert fs[0]["limiting_quantity"] == "30"
        assert fs[0]["legs"]["liquidity"]["limiting_quantity"] == "30"
        sent = []
        _run_cycle(monkeypatch, {"KXUCLGAME": legs}, alerts=sent)
        assert len(sent) == 1
        assert "up to 30 contracts" in sent[0][1]

    def test_crossed_book_no_side_size_only_via_verified_mirror(self):
        # mirror holds (no_bid == 1 - yes_ask): the no-side executable
        # size is readable from the yes_ask side
        m = _mkt(f"{EV}-AAA", bid="0.60", ask="0.55", no_bid="0.45",
                 ask_size="20", bid_size="10")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _quad()), "CROSSED_BOOK")
        assert len(fs) == 1
        assert fs[0]["alertable_liquidity"] is True
        assert fs[0]["limiting_quantity"] == "10"
        assert fs[0]["legs"]["liquidity"][
            "no_bid_size_basis"].startswith("mirrored")
        # mirror broken: the no-side size is UNKNOWN → not alertable,
        # regardless of the yes-side sizes
        m2 = _mkt(f"{EV}-BBB", bid="0.60", ask="0.62", no_bid="0.55",
                  ask_size="20", bid_size="10")
        fs2 = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m2], NOW, _quad()), "CROSSED_BOOK")
        assert len(fs2) == 1
        assert fs2[0]["alertable_liquidity"] is False
        liq = fs2[0]["legs"]["liquidity"]
        assert liq["status"] == "liquidity_unknown"
        assert liq["no_bid_size_basis"].startswith("UNKNOWN")

    def test_post_certainty_requires_positive_ask_size(
            self, live_session, monkeypatch):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            lambda d: {"events": [_espn_event()]})
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.93",
                                     ask_size=None)}
        _cann_requote(monkeypatch, markets)
        fs, complete = hunter.detect_post_certainty(
            live_session, markets, NOW, _quad("KXMLSGAME"))
        assert complete is True and len(fs) == 1
        assert fs[0]["alertable_liquidity"] is False
        assert not hunter._alert_eligible(fs[0])


class TestConfigBounds:
    def test_hunter_env_bounds_fail_fast(self, monkeypatch):
        """P2: every numeric HUNTER_* knob is positive-bound validated
        at import — misconfiguration fails loudly at boot."""
        import importlib
        try:
            monkeypatch.setenv("HUNTER_POLL_MINUTES", "0")
            with pytest.raises(ValueError, match="HUNTER_POLL_MINUTES"):
                importlib.reload(config)
            monkeypatch.setenv("HUNTER_POLL_MINUTES", "10")
            monkeypatch.setenv("HUNTER_WIDE_SPREAD_DOLLARS", "-0.10")
            with pytest.raises(ValueError,
                               match="HUNTER_WIDE_SPREAD_DOLLARS"):
                importlib.reload(config)
            monkeypatch.setenv("HUNTER_WIDE_SPREAD_DOLLARS", "junk")
            with pytest.raises(ValueError,
                               match="HUNTER_WIDE_SPREAD_DOLLARS"):
                importlib.reload(config)
            monkeypatch.setenv("HUNTER_WIDE_SPREAD_DOLLARS", "0.10")
            monkeypatch.setenv("HUNTER_ALERT_MAX_PER_HOUR", "-1")
            with pytest.raises(ValueError,
                               match="HUNTER_ALERT_MAX_PER_HOUR"):
                importlib.reload(config)
        finally:
            # ALWAYS leave the module fully re-initialized from a clean
            # environment, even when an assertion above fails
            for var in ("HUNTER_POLL_MINUTES",
                        "HUNTER_WIDE_SPREAD_DOLLARS",
                        "HUNTER_ALERT_MAX_PER_HOUR"):
                monkeypatch.delenv(var, raising=False)
            importlib.reload(config)
        assert config.HUNTER_POLL_MINUTES == 10
        assert config.HUNTER_WIDE_SPREAD_DOLLARS == "0.10"


class TestPairStoreBounds:
    def test_pair_store_bounded_in_age_and_size(self, monkeypatch):
        """P2: the process-local pair store is bounded — aged entries
        drop, and above the cap the oldest are evicted first."""
        monkeypatch.setattr(hunter, "_PAIR_STORE_MAX_ENTRIES", 3)

        def entry(minutes_ago):
            return {"yes_bid": "0.10", "yes_ask": "0.20",
                    "captured_at": (NOW - timedelta(
                        minutes=minutes_ago)).isoformat()}
        store = {
            "AGED": entry(hunter._PAIR_STORE_MAX_AGE_S // 60 + 1),
            "T50": entry(50), "T40": entry(40),
            "T30": entry(30), "T20": entry(20),
            "MALFORMED": {"yes_bid": "0.10", "yes_ask": "0.20"},
        }
        monkeypatch.setattr(hunter, "_pair_store", store)
        hunter._prune_pair_store(NOW)
        assert "AGED" not in store and "MALFORMED" not in store
        assert set(store) == {"T40", "T30", "T20"}   # oldest evicted


class TestHeartbeatExtras:
    def test_cycle_status_split_discovery_and_scope_labels(
            self, live_session, monkeypatch):
        """P2: per-status cycle counts, last-discovery visibility,
        global-scope labelling, and the documented roster lag."""
        import requests as _rq
        _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                   alerts=[])
        _run_cycle(monkeypatch,
                   {"KXUCLGAME": _rq.RequestException("boom")},
                   alerts=[])
        rep = hunter.findings_report()
        d = rep["denominators"]
        assert d["cycles_run"] == 2
        assert d["cycles_by_status"] == {"complete": 1, "degraded": 1,
                                         "failed": 0}
        lc = d["last_cycle"]
        assert lc["status"] == "degraded"
        assert lc["series_failed"] == 1
        assert lc["incomplete_series"]["KXUCLGAME"]["outcome"] \
            == "REQUEST_FAILED"
        assert lc["discovery_at"] is not None
        assert lc["discovery_error"] is None
        assert "global" in rep["findings_per_type_scope"]
        assert str(config.HUNTER_DISCOVERY_MINUTES) in d["roster_note"]


# ---------------------------------------------------------------------------
class TestProviderAwareFees:
    """P0-1: a structural finding is priced with ITS OWN series'
    provider-declared fee schedule (Kalshi publishes fee_type /
    fee_multiplier on the SERIES object; the market payload carries no
    fee field at all — verified 2026-07-29, archived in
    research_archive/kalshi_fee_schedules_2026-07-29.json).

    The soccer taxonomy is NOT uniform: 90 of 97 GAME series declare
    'quadratic' and 7 declare 'quadratic_with_maker_fees' — including
    KXUCLGAME, the series every fixture in this file uses. An
    unmodelled schedule is fee_unknown: gross structure recorded, no net
    margin asserted, never alertable."""

    def test_two_schedules_price_the_same_book_differently(self):
        legs = [_mkt(f"{EV}-AAA", bid="0.28", ask="0.30"),
                _mkt(f"{EV}-TIE", bid="0.30", ask="0.32"),
                _mkt(f"{EV}-BBB", bid="0.28", ask="0.30")]
        modeled = _of_type(hunter.detect_event_findings(
            "KXMLSGAME", EV, legs, NOW, _quad("KXMLSGAME")),
            "SUM_BELOW_ONE")[0]
        unknown = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW, _maker("KXUCLGAME")),
            "SUM_BELOW_ONE")[0]
        # same book, same gross — the fee schedule is the only variable
        assert modeled["gross_margin_dollars"] == "0.08"
        assert unknown["gross_margin_dollars"] == "0.08"
        # 'quadratic': exact per-leg Decimal fees and a NET margin
        assert modeled["fee_status"] == "modeled"
        assert modeled["legs"]["sum_fees_dollars"] == "0.0447"
        assert modeled["net_margin_dollars"] == "0.0353"
        assert modeled["fee_policy_version"] == "kalshi-fee-2026-07-general"
        assert [leg["fee_dollars"] for leg in modeled["legs"]["legs"]] \
            == ["0.0147", "0.0153", "0.0147"]
        # 'quadratic_with_maker_fees': NO fee number is invented, so no
        # net margin exists to be mistaken for an arbitrage
        assert unknown["fee_status"] == "fee_unknown"
        assert unknown["net_margin_dollars"] is None
        assert unknown["legs"]["sum_fees_dollars"] is None
        assert [leg["fee_dollars"] for leg in unknown["legs"]["legs"]] \
            == [None, None, None]
        assert unknown["fee_policy_version"] \
            == "kalshi-fee-unknown:quadratic_with_maker_fees"
        sched = unknown["legs"]["fee_schedule"]
        assert sched["provider_fee_type"] == "quadratic_with_maker_fees"
        assert "not modelled" in sched["reason"]
        assert "SERIES object" in sched["source"]
        assert "market payload carries no fee field" in sched["source"]
        # and eligibility refuses it outright
        assert hunter._alert_eligible(modeled) is True
        assert hunter._alert_eligible(unknown) is False

    def test_crossed_book_follows_its_series_schedule_too(self):
        m = _mkt(f"{EV}-AAA", bid="0.60", ask="0.62",
                 no_bid="0.55", no_ask="0.40")
        modeled = _of_type(hunter.detect_event_findings(
            "KXMLSGAME", EV, [m], NOW, _quad("KXMLSGAME")),
            "CROSSED_BOOK")[0]
        unknown = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW, _maker("KXUCLGAME")),
            "CROSSED_BOOK")[0]
        assert modeled["net_margin_dollars"] == "0.1158"
        assert modeled["legs"]["fee_leg_yes_dollars"] == "0.0168"
        assert unknown["net_margin_dollars"] is None
        assert unknown["legs"]["fee_leg_yes_dollars"] is None
        assert unknown["gross_margin_dollars"] == "0.15"
        assert hunter._alert_eligible(unknown) is False

    def test_unmodelled_multiplier_is_also_unknown(self):
        s = hunter.FeeSchedule("KXZZZGAME", fee_type="quadratic",
                               multiplier=2)
        assert s.modeled is False
        assert s.fee(Decimal("0.30")) is None
        assert "fee_multiplier" in s.reason
        # and the one schedule we DO model
        ok = hunter.FeeSchedule("KXZZZGAME", fee_type="quadratic",
                                multiplier=1)
        assert ok.modeled is True
        assert ok.fee(Decimal("0.30")) == Decimal("0.0147")

    def test_discovery_reads_schedules_with_no_extra_requests(
            self, monkeypatch):
        payload = {"series": [
            {"ticker": "KXMLSGAME", "tags": ["Soccer"],
             "fee_type": "quadratic", "fee_multiplier": 1},
            {"ticker": "KXUCLGAME", "tags": ["Soccer"],
             "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1},
            {"ticker": "KXNBAGAME", "tags": ["Basketball"],
             "fee_type": "quadratic", "fee_multiplier": 1},
        ]}
        monkeypatch.setattr(config, "HUNTER_SERIES", [])
        monkeypatch.setattr(hunter, "_get",
                            lambda url, params=None, **kw: payload)
        counter: dict = {}
        assert hunter.discover_roster(counter) == ["KXMLSGAME",
                                                   "KXUCLGAME"]
        # the schedules ride on the request discovery ALREADY makes
        assert counter["requests"] == 1
        assert hunter._fee_schedule("KXMLSGAME").modeled is True
        assert hunter._fee_schedule("KXUCLGAME").modeled is False
        # a series the listing never described is unknown, never
        # "assume the general schedule"
        never_seen = hunter._fee_schedule("KXEPLGAME")
        assert never_seen.modeled is False
        assert never_seen.version == "kalshi-fee-unknown:unread"

    def test_unknown_schedule_records_a_finding_but_never_alerts(
            self, live_session, monkeypatch):
        # ONE cycle, two series, IDENTICAL books: only the provider's
        # declared schedule differs. Exactly one alert may leave.
        sent = []
        r = _run_cycle(
            monkeypatch,
            {"KXUCLGAME": _arb("KXUCLGAME"),
             "KXAAAGAME": _arb("KXAAAGAME")},
            alerts=sent,
            fee_types={"KXUCLGAME": "quadratic_with_maker_fees"})
        assert r["findings_new"] == 2          # BOTH recorded
        assert len(sent) == 1                  # only the modelled one
        assert "KXAAAGAME" in sent[0][1]
        ucl = (live_session.query(HunterFinding)
               .filter_by(series="KXUCLGAME",
                          finding_type="SUM_BELOW_ONE").one())
        aaa = (live_session.query(HunterFinding)
               .filter_by(series="KXAAAGAME",
                          finding_type="SUM_BELOW_ONE").one())
        assert ucl.status == "open" and ucl.alerted_at is None
        assert ucl.net_margin_dollars is None
        assert ucl.fee_policy_version \
            == "kalshi-fee-unknown:quadratic_with_maker_fees"
        assert json.loads(ucl.legs_json)["fee_schedule"]["fee_status"] \
            == "fee_unknown"
        # CONTROL: same arithmetic, modelled schedule → net margin,
        # alert delivered
        assert aaa.net_margin_dollars == "0.0353"
        assert aaa.fee_policy_version == "kalshi-fee-2026-07-general"
        assert aaa.alerted_at is not None
        assert "kalshi-fee-2026-07-general" in sent[0][1]

    def test_unreadable_schedule_is_unknown_not_assumed_general(
            self, live_session, monkeypatch):
        # the provider read failed for this series: fail CLOSED
        sent = []
        _run_cycle(monkeypatch, {"KXAAAGAME": _arb("KXAAAGAME")},
                   alerts=sent, fee_types={"KXAAAGAME": None})
        assert sent == []
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.net_margin_dollars is None
        assert row.fee_policy_version == "kalshi-fee-unknown:unread"

    def test_report_states_the_resolution_rule(self, live_session,
                                               monkeypatch):
        _run_cycle(monkeypatch, {"KXAAAGAME": _arb("KXAAAGAME")},
                   alerts=[])
        rep = hunter.findings_report()
        assert "fee_unknown" in rep["fee_policy_note"]
        assert "never one policy assumed" in rep["fee_policy_note"]


# ---------------------------------------------------------------------------
class TestPostCertaintyFinalityOrder:
    """P0-2: finality is established BEFORE the priced quote is
    captured. Pairing a pre-final price with a settled outcome
    manufactures a margin that never existed."""

    def _seeded(self, live_session, monkeypatch, state="post"):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(
            hunter, "_fetch_espn_scoreboard",
            lambda d: {"events": [_espn_event(state=state)]})
        return fx, me, ev

    def test_repriced_after_finality_emits_nothing(
            self, live_session, monkeypatch):
        fx, me, ev = self._seeded(live_session, monkeypatch)
        tick = f"{ev}-HOM"
        # the cheap PRE-final quote the cycle scanned...
        pre = {tick: _mkt(tick, event=ev, ask="0.60")}
        # ...and the market as it stands once the whistle has gone
        after = {tick: _mkt(tick, event=ev, ask="0.9999")}
        _cann_requote(monkeypatch, after)
        fs, complete = hunter.detect_post_certainty(
            live_session, pre, NOW, _quad("KXMLSGAME"))
        assert complete is True
        assert fs == []                      # NO manufactured margin

        # DISCRIMINATING CONTROL: the identical setup where the market
        # really is still cheap AFTER finality does fire — so the empty
        # result above is the ordering, not an inert test
        _cann_requote(monkeypatch, pre)
        fs2, _ = hunter.detect_post_certainty(
            live_session, pre, NOW, _quad("KXMLSGAME"))
        assert len(fs2) == 1
        assert fs2[0]["net_margin_dollars"] == "0.3832"

    def test_every_emitted_row_has_quote_at_or_after_finality(
            self, live_session, monkeypatch):
        fx, me, ev = self._seeded(live_session, monkeypatch)
        tick = f"{ev}-HOM"
        books = {"KXMLSGAME": [_mkt(tick, event=ev, ask="0.93")]}
        _run_cycle(monkeypatch, books, alerts=[])
        live_session.expire_all()
        rows = (live_session.query(HunterFinding)
                .filter_by(finding_type="POST_CERTAINTY").all())
        assert len(rows) == 1
        for r in rows:
            cap = (r.first_captured_at if r.first_captured_at.tzinfo
                   else r.first_captured_at.replace(tzinfo=UTC))
            espn = (r.espn_captured_at if r.espn_captured_at.tzinfo
                    else r.espn_captured_at.replace(tzinfo=UTC))
            assert cap >= espn
        legs = json.loads(rows[0].legs_json)
        assert legs["pre_finality_yes_ask_dollars"] == "0.93"
        assert "AFTER finality" in legs["clock_order"]

    def test_full_cycle_with_a_repriced_market_records_nothing(
            self, live_session, monkeypatch):
        fx, me, ev = self._seeded(live_session, monkeypatch)
        tick = f"{ev}-HOM"
        sent = []
        r = _run_cycle(
            monkeypatch,
            {"KXMLSGAME": [_mkt(tick, event=ev, ask="0.60")]},
            alerts=sent,
            requote={tick: _mkt(tick, event=ev, ask="0.9999")})
        assert sent == []
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="POST_CERTAINTY").count() == 0)
        assert r["status"] == "complete"

    def test_failed_requote_blocks_expiry_rather_than_clearing(
            self, live_session, monkeypatch):
        fx, me, ev = self._seeded(live_session, monkeypatch)
        tick = f"{ev}-HOM"
        books = {"KXMLSGAME": [_mkt(tick, event=ev, ask="0.93")]}
        _run_cycle(monkeypatch, books, alerts=[])
        live_session.expire_all()
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="POST_CERTAINTY").one()
                .status == "open")
        # the post-finality re-read fails: we did NOT look, so the open
        # finding must not be cleared and the cycle degrades
        r2 = _run_cycle(monkeypatch, books, alerts=[],
                        requote_fail={tick})
        assert r2["status"] == "degraded"
        live_session.expire_all()
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="POST_CERTAINTY").one()
                .status == "open")
        outcomes, _ = _last_cycle_outcomes(live_session)
        assert outcomes["KXMLSGAME"]["outcome"] == "DETECTION_FAILED"

    def test_database_refuses_a_pre_finality_quote_row(self,
                                                       live_session):
        """The ordering is a TABLE invariant, not merely a detector
        habit: a row whose quote clock precedes its ESPN finality clock
        is refused by the database on both dialects."""
        from sqlalchemy.exc import IntegrityError
        live_session.add(HunterFinding(
            series="KXMLSGAME", event_ticker="KXMLSGAME-26JUL28HOMAWA",
            market_ticker="KXMLSGAME-26JUL28HOMAWA-HOM",
            finding_type="POST_CERTAINTY", finding_key="pc|stale",
            legs_json="{}", status="open",
            first_captured_at=NOW - timedelta(minutes=5),
            espn_captured_at=NOW))
        with pytest.raises(IntegrityError):
            live_session.commit()
        live_session.rollback()
        # CONTROL: the same row with the clocks the right way round
        live_session.add(HunterFinding(
            series="KXMLSGAME", event_ticker="KXMLSGAME-26JUL28HOMAWA",
            market_ticker="KXMLSGAME-26JUL28HOMAWA-HOM",
            finding_type="POST_CERTAINTY", finding_key="pc|ok",
            legs_json="{}", status="open",
            first_captured_at=NOW,
            espn_captured_at=NOW - timedelta(seconds=1)))
        live_session.commit()


# ---------------------------------------------------------------------------
class TestChronologicalReconciliation:
    """P0-3: Railway overlaps containers deliberately, so an OLDER
    scan can commit AFTER a newer one. Observation clocks are
    monotonic per finding key, enforced at the database."""

    ARB = TestAlertDiscipline.ARB

    def _at(self, monkeypatch, t):
        monkeypatch.setattr(hunter, "_now", lambda: t)

    def test_stale_anomaly_cannot_reopen_a_newer_clean_scan(
            self, live_session, monkeypatch):
        t1 = NOW
        t2 = NOW + timedelta(minutes=10)
        sent = []
        # cycle 1 @ t1: the anomaly is real, recorded, alerted
        self._at(monkeypatch, t1)
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=sent)
        assert len(sent) == 1
        # cycle 2 @ t2 (the NEW container): book is clean → expired
        self._at(monkeypatch, t2)
        _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                   alerts=sent)
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.status == "expired"

        # the OLD container finally commits its t1 observation
        self._at(monkeypatch, t1)
        r3 = _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB},
                        alerts=sent)
        live_session.expire_all()
        rows = (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").all())
        # no stale OPEN finding, no stale alert, and the rejection is
        # visible rather than silent
        assert [r.status for r in rows] == ["expired"]
        assert len(sent) == 1
        assert r3["findings_new"] == 0
        assert r3["stale_writes_rejected"] >= 1
        # clocks never regressed
        for r in rows:
            def utc(dt):
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            assert utc(r.last_seen_at) >= utc(r.first_captured_at)
            assert utc(r.first_captured_at) <= t2
        wm = (live_session.query(HunterObservationWatermark)
              .filter_by(finding_key=hunter._row_key(rows[0])).one())
        assert (wm.observed_through if wm.observed_through.tzinfo
                else wm.observed_through.replace(tzinfo=UTC)) == t2

    def test_a_genuinely_newer_observation_still_reopens(
            self, live_session, monkeypatch):
        """CONTROL: the guard is chronological, not a blanket refusal to
        re-open. The same straggler cycle with a NEWER clock does record
        a fresh finding and does alert."""
        t1, t2 = NOW, NOW + timedelta(minutes=10)
        t3 = NOW + timedelta(minutes=20)
        sent = []
        self._at(monkeypatch, t1)
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=sent)
        self._at(monkeypatch, t2)
        _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                   alerts=sent)
        self._at(monkeypatch, t3)
        r3 = _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB},
                        alerts=sent)
        live_session.expire_all()
        statuses = sorted(r.status for r in
                          live_session.query(HunterFinding)
                          .filter_by(finding_type="SUM_BELOW_ONE").all())
        assert statuses == ["expired", "open"]
        assert r3["findings_new"] == 1
        assert r3["stale_writes_rejected"] == 0
        assert len(sent) == 2

    def test_stale_last_seen_bump_is_refused(self, live_session,
                                             monkeypatch):
        """A straggler observing an anomaly that is STILL open may not
        drag last_seen_at backwards either."""
        t1, t2 = NOW, NOW + timedelta(minutes=10)
        self._at(monkeypatch, t2)
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=[])
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        cycles_before = row.observed_cycles
        self._at(monkeypatch, t1)
        r2 = _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=[])
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())

        def utc(dt):
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        assert utc(row.last_seen_at) == t2       # never dragged back
        assert row.observed_cycles == cycles_before
        assert r2["stale_writes_rejected"] >= 1

    def test_stale_expiry_cannot_close_a_newer_finding(
            self, live_session, monkeypatch):
        """The reverse straggler: an OLD clean scan arriving after a
        NEW anomaly must not expire it."""
        t1, t2 = NOW, NOW + timedelta(minutes=10)
        self._at(monkeypatch, t2)
        _run_cycle(monkeypatch, {"KXUCLGAME": self.ARB}, alerts=[])
        self._at(monkeypatch, t1)
        r2 = _run_cycle(monkeypatch, {"KXUCLGAME": _healthy_three_way()},
                        alerts=[])
        live_session.expire_all()
        row = (live_session.query(HunterFinding)
               .filter_by(finding_type="SUM_BELOW_ONE").one())
        assert row.status == "open"
        assert r2["findings_expired"] == 0
        assert r2["stale_writes_rejected"] >= 1

    def test_database_refuses_a_backwards_last_seen(self, live_session):
        from sqlalchemy.exc import IntegrityError
        live_session.add(HunterFinding(
            series="KXUCLGAME", event_ticker=EV,
            finding_type="SUM_BELOW_ONE", finding_key="mono|bad",
            legs_json="{}", status="open", first_captured_at=NOW,
            last_seen_at=NOW - timedelta(seconds=1)))
        with pytest.raises(IntegrityError):
            live_session.commit()
        live_session.rollback()


# ---------------------------------------------------------------------------
class TestIdentityFailClosed:
    """P0-4: a market with no provider-stable identity cannot be keyed,
    so it may never be swept into a synthetic event and alerted on."""

    def _broken(self, field):
        ms = [dict(m) for m in _arb("KXAAAGAME")]
        ms[0].pop(field)
        return ms

    @pytest.mark.parametrize("field", ["event_ticker", "ticker"])
    def test_missing_identity_degrades_and_yields_nothing(
            self, live_session, monkeypatch, field):
        sent = []
        r = _run_cycle(monkeypatch, {"KXAAAGAME": self._broken(field)},
                       alerts=sent)
        assert r["status"] == "degraded"
        assert r["findings_new"] == 0
        assert sent == []
        assert live_session.query(HunterFinding).count() == 0
        outcomes, cyc = _last_cycle_outcomes(live_session)
        assert outcomes["KXAAAGAME"]["outcome"] == "IDENTITY_FAILED"
        assert "provider-stable identity" in outcomes["KXAAAGAME"]["detail"]
        assert cyc.series_failed == 1
        # the series stays SCHEDULED and the API exposes the failure
        assert "KXAAAGAME" in hunter._roster["active"]
        rep = hunter.findings_report()
        inc = rep["denominators"]["last_cycle"]["incomplete_series"]
        assert inc["KXAAAGAME"]["outcome"] == "IDENTITY_FAILED"

    def test_whole_event_without_identity_cannot_alert_synthetically(
            self, live_session, monkeypatch):
        """The reviewer's case exactly: when the provider omits
        event_ticker for a WHOLE event, the pre-fix grouping swept all
        three legs into a synthetic ""-event that formed a valid 3-way
        partition, produced key 'SUM_BELOW_ONE|KXAAAGAME||' and was
        ALERTABLE. Nothing may be derived from rows with no identity."""
        ms = [dict(m) for m in _arb("KXAAAGAME")]
        for m in ms:
            m.pop("event_ticker")
        sent = []
        r = _run_cycle(monkeypatch, {"KXAAAGAME": ms}, alerts=sent)
        assert sent == []
        assert live_session.query(HunterFinding).count() == 0
        assert r["status"] == "degraded"
        outcomes, _ = _last_cycle_outcomes(live_session)
        assert outcomes["KXAAAGAME"]["outcome"] == "IDENTITY_FAILED"
        assert "3 of 3" in outcomes["KXAAAGAME"]["detail"]

    def test_control_same_book_with_identity_records_and_alerts(
            self, live_session, monkeypatch):
        sent = []
        r = _run_cycle(monkeypatch, {"KXAAAGAME": _arb("KXAAAGAME")},
                       alerts=sent)
        assert r["status"] == "complete"
        assert r["findings_new"] == 1
        assert len(sent) == 1

    def test_identity_failure_blocks_expiry_for_that_series(
            self, live_session, monkeypatch):
        _run_cycle(monkeypatch, {"KXAAAGAME": _arb("KXAAAGAME")},
                   alerts=[])
        live_session.expire_all()
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").one()
                .status == "open")
        # next cycle the provider returns an unidentifiable book: we did
        # not get a usable look, so nothing may expire
        clean = [dict(m) for m in _healthy_three_way()]
        clean[0].pop("event_ticker")
        r2 = _run_cycle(monkeypatch, {"KXAAAGAME": clean}, alerts=[])
        assert r2["status"] == "degraded"
        live_session.expire_all()
        assert (live_session.query(HunterFinding)
                .filter_by(finding_type="SUM_BELOW_ONE").one()
                .status == "open")

    def test_identity_failure_takes_the_mls_detectors_down_with_it(
            self, live_session, monkeypatch):
        """Fail-closed means the whole series' scan is unusable — the
        MLS-only detectors must not run off a half-identified book."""
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            lambda d: {"events": [_espn_event()]})
        ms = [_mkt(f"{ev}-HOM", event=ev, ask="0.93"),
              _mkt(f"{ev}-TIE", event=ev, ask="0.50")]
        ms[1].pop("ticker")
        sent = []
        r = _run_cycle(monkeypatch, {"KXMLSGAME": ms}, alerts=sent)
        assert r["status"] == "degraded"
        assert sent == []
        assert live_session.query(HunterFinding).count() == 0
        outcomes, _ = _last_cycle_outcomes(live_session)
        assert outcomes["KXMLSGAME"]["outcome"] == "IDENTITY_FAILED"
