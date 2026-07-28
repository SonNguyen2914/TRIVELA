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
                             HunterFinding, LiveBase, MarketContract,
                             MarketEvent, ModelApprovalDecision,
                             ModelVersion, PredictionContract,
                             PredictionRun, Team)

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
    """Process-local hunter state must not leak between tests."""
    from collections import deque
    monkeypatch.setattr(hunter, "_pair_store", {})
    monkeypatch.setattr(hunter, "_alert_times", deque())
    monkeypatch.setattr(hunter, "_roster",
                        {"at": None, "series": [], "active": set()})


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
            "KXUCLGAME", EV, legs, NOW), "SUM_BELOW_ONE")
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
            "KXUCLGAME", EV, _healthy_three_way(), NOW)
        assert _of_type(fs, "SUM_BELOW_ONE") == []

    def test_fees_erase_a_gross_margin(self):
        # asks sum 0.98 (< $1) but exact fees are 0.0463 — a fee-blind
        # detector would call this a win; the finding must NOT exist
        legs = [
            _mkt(f"{EV}-AAA", bid="0.30", ask="0.32"),
            _mkt(f"{EV}-TIE", bid="0.31", ask="0.33"),
            _mkt(f"{EV}-BBB", bid="0.31", ask="0.33"),
        ]
        fs = hunter.detect_event_findings("KXUCLGAME", EV, legs, NOW)
        assert _of_type(fs, "SUM_BELOW_ONE") == []

    def test_partition_guards(self):
        # no TIE leg → not a provable 3-way partition, however cheap
        legs = [_mkt(f"{EV}-AAA", ask="0.20"),
                _mkt(f"{EV}-BBB", ask="0.20"),
                _mkt(f"{EV}-CCC", ask="0.20")]
        assert _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW), "SUM_BELOW_ONE") == []
        # two legs only
        legs = [_mkt(f"{EV}-AAA", ask="0.20"),
                _mkt(f"{EV}-TIE", ask="0.20")]
        assert _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW), "SUM_BELOW_ONE") == []
        # a leg with no ask
        legs = [_mkt(f"{EV}-AAA", ask="0.30"),
                _mkt(f"{EV}-TIE", ask="0.32"),
                _mkt(f"{EV}-BBB")]
        assert _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, legs, NOW), "SUM_BELOW_ONE") == []


class TestCrossedBook:
    def test_fires_and_stores_both_sides(self):
        m = _mkt(f"{EV}-AAA", bid="0.60", ask="0.62",
                 no_bid="0.55", no_ask="0.40")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW), "CROSSED_BOOK")
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
        fs = hunter.detect_event_findings("KXUCLGAME", EV, [m], NOW)
        assert _of_type(fs, "CROSSED_BOOK") == []

    def test_fees_erase_a_thin_cross(self):
        # gross +0.02 but two taker fee legs at 0.49 cost 0.0350
        m = _mkt(f"{EV}-AAA", bid="0.51", ask="0.53",
                 no_bid="0.51", no_ask="0.53")
        fs = hunter.detect_event_findings("KXUCLGAME", EV, [m], NOW)
        assert _of_type(fs, "CROSSED_BOOK") == []


class TestLiquidityContext:
    def test_wide_spread_is_context_only(self):
        m = _mkt(f"{EV}-AAA", bid="0.05", ask="0.80")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [m], NOW), "WIDE_SPREAD")
        assert len(fs) == 1
        assert fs[0]["is_context"] is True
        assert fs[0]["net_margin_dollars"] is None
        assert "not a win" in fs[0]["legs"]["rule"]

    def test_tight_spread_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52")
        fs = hunter.detect_event_findings("KXUCLGAME", EV, [m], NOW)
        assert _of_type(fs, "WIDE_SPREAD") == []

    def test_thin_book_fires_on_small_size_and_one_sided(self):
        thin = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52", ask_size="3.00")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [thin], NOW), "THIN_BOOK")
        assert len(fs) == 1 and fs[0]["is_context"] is True
        one_sided = _mkt(f"{EV}-BBB", ask="0.52")
        fs = _of_type(hunter.detect_event_findings(
            "KXUCLGAME", EV, [one_sided], NOW), "THIN_BOOK")
        assert len(fs) == 1
        assert "one_sided_or_empty_book" in fs[0]["legs"]["reasons"]

    def test_deep_two_sided_book_does_not_fire(self):
        m = _mkt(f"{EV}-AAA", bid="0.48", ask="0.52",
                 ask_size="50", bid_size="50")
        fs = hunter.detect_event_findings("KXUCLGAME", EV, [m], NOW)
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
def _seed_mls_event(s, kickoff, espn_id="401", tickers=None):
    home = Team(competition_slug="mls-2026", canonical_name="Home FC")
    away = Team(competition_slug="mls-2026", canonical_name="Away FC")
    s.add_all([home, away])
    s.flush()
    fx = Fixture(competition_slug="mls-2026", espn_event_id=espn_id,
                 home_team_id=home.id, away_team_id=away.id,
                 current_kickoff_utc=kickoff, status="in")
    s.add(fx)
    s.flush()
    ev_ticker = f"KXMLSGAME-{TODAY_SEG}HOMAWA"
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
        fs = hunter.detect_post_certainty(live_session, markets, NOW)
        assert len(fs) == 1
        f = fs[0]
        # 1 - 0.93 - fee(0.93)=0.0046 → 0.0654
        assert f["net_margin_dollars"] == "0.0654"
        assert f["legs"]["certain_outcome"] == "home_win"
        assert f["legs"]["final_score_home"] == 2
        assert f["legs"]["final_score_away"] == 1
        # BOTH capture stamps present
        assert f["espn_captured_at"] is not None
        assert f["legs"]["kalshi_captured_at"] == NOW.isoformat()
        assert f["legs"]["espn_captured_at"]
        assert calls == [NOW.astimezone().strftime("%Y%m%d")] or calls

        # the re-read happens EVERY detection — never cached from a
        # previous cycle
        hunter.detect_post_certainty(live_session, markets, NOW)
        assert len(calls) == 2

    def test_in_play_match_does_not_fire(self, live_session, monkeypatch):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(
            hunter, "_fetch_espn_scoreboard",
            lambda d: {"events": [_espn_event(state="in")]})
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.60")}
        assert hunter.detect_post_certainty(
            live_session, markets, NOW) == []

    def test_price_already_at_certainty_does_not_fire(
            self, live_session, monkeypatch):
        # ask 0.9999: margin = 0.0001 - fee(0.0001) = 0 → no finding
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(hunter, "_fetch_espn_scoreboard",
                            lambda d: {"events": [_espn_event()]})
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.9999")}
        assert hunter.detect_post_certainty(
            live_session, markets, NOW) == []

    def test_draw_outcome_derived_from_score_numbers(
            self, live_session, monkeypatch):
        fx, me, ev = _seed_mls_event(
            live_session, NOW - timedelta(hours=3))
        monkeypatch.setattr(
            hunter, "_fetch_espn_scoreboard",
            lambda d: {"events": [_espn_event(home_goals="1",
                                              away_goals="1")]})
        markets = {f"{ev}-TIE": _mkt(f"{ev}-TIE", event=ev, ask="0.90")}
        fs = hunter.detect_post_certainty(live_session, markets, NOW)
        assert len(fs) == 1
        assert fs[0]["legs"]["certain_outcome"] == "draw"


# ---------------------------------------------------------------------------
def _seed_approval(s, approved=True):
    mv = ModelVersion(name="mls-2026-v0", approved_for_shadow=approved)
    s.add(mv)
    s.flush()
    if approved:
        s.add(ModelApprovalDecision(
            model_version_id=mv.id, model_version_name="mls-2026-v0",
            eval_version="model-eval-v1",
            policy_version="shadow-approval-v1",
            approved_mode="shadow", approved=True, n_scored=177,
            edge_json=json.dumps({"delta_log_loss": 0.0269,
                                  "ci95": [-0.0050, 0.0596],
                                  "significant": False}),
            decision_document="{}", content_hash="h" * 64,
            created_at=NOW))
    s.commit()


class TestModelEdge:
    def _seed_run(self, s, fx, p_home=0.55):
        run = PredictionRun(fixture_id=fx.id, run_type="scheduled",
                            status="complete", created_at=NOW)
        s.add(run)
        s.flush()
        s.add(PredictionContract(prediction_run_id=run.id,
                                 outcome_key="home_win",
                                 raw_probability=p_home,
                                 anchored_probability=p_home))
        s.commit()
        return run

    def test_readout_carries_the_standing_qualifier(self, live_session):
        _seed_approval(live_session)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        self._seed_run(live_session, fx)
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.45")}
        fs = hunter.detect_model_edge(live_session, markets, NOW)
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

    def test_no_approval_means_no_numbers(self, live_session):
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        self._seed_run(live_session, fx)
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.45")}
        assert hunter.detect_model_edge(live_session, markets, NOW) == []
        rep = hunter.findings_report()
        assert rep["model_status"]["mls-2026"] == "no model"

    def test_edge_below_threshold_does_not_fire(self, live_session):
        _seed_approval(live_session)
        fx, me, ev = _seed_mls_event(
            live_session, NOW + timedelta(hours=5))
        self._seed_run(live_session, fx)
        # 0.55 - 0.54 - fee = negative
        markets = {f"{ev}-HOM": _mkt(f"{ev}-HOM", event=ev, ask="0.54")}
        assert hunter.detect_model_edge(live_session, markets, NOW) == []


# ---------------------------------------------------------------------------
def _run_cycle(monkeypatch, books, alerts=None):
    """One scan_cycle against canned per-series market lists."""
    monkeypatch.setattr(config, "HUNTER_SERIES", list(books.keys()))
    monkeypatch.setattr(hunter, "_paged_markets",
                        lambda series, counter: books.get(series, []))
    if alerts is not None:
        import src.alerts as alerts_mod
        monkeypatch.setattr(alerts_mod, "send_alert",
                            lambda msg, title="x", **kw:
                            alerts.append((title, msg)))
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
        assert row.alerted_at is not None

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
            legs_json="{}", first_captured_at=NOW))
        with pytest.raises(ValueError, match="value too long"):
            live_session.commit()
        live_session.rollback()

    def test_generous_ticker_widths_accept_real_tickers(self, live_session):
        live_session.add(HunterFinding(
            series="KXCONMEBOLLIBGAME",
            event_ticker="KXCONMEBOLLIBGAME-26JUL30CARSFE",
            market_ticker="KXCONMEBOLLIBGAME-26JUL30CARSFE-CAR",
            finding_type="SUM_BELOW_ONE", legs_json="{}",
            first_captured_at=NOW,
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
