"""TASK-12: the M3 quarantine variant — a number, not a flag flip.

What must hold, each asserted: the frozen production ladder is untouched
(the approval rests on its byte-identity), the paired walk-forward
actually pairs, excluding quarantined xG actually changes the fitted
input (else the delta is a tautological zero), the frozen-market section
computes only from the canonical lock chain and REFUSES by name when
that chain is absent, and nothing anywhere flips
MLS_XG_QUARANTINE_EXCLUDE.
"""
from datetime import datetime, timedelta, timezone

import pytest

import config
from src.live import model_eval
from src.live.models import (Fixture, MarketContract, MarketEvent,
                             MarketQuote, MlsTeamMatchStat,
                             PredictionContract, PredictionRun, Team)
from tests import _livedb
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc
# derived, not pinned — the absolute-dates guard is right that a
# literal here rots; these are completed historical fixtures
T0 = (datetime.now(UTC) - timedelta(days=90)).replace(
    minute=0, second=0, microsecond=0)


def _seed_history(s, n=14, quarantine=(3, 4)):
    """Two clubs, n meetings, alternating venue, every fixture carrying
    per-side xG rows; `quarantine` marks those fixtures' home rows
    quarantined. xG deliberately disagrees with goals so the xG blend
    moves ratings and the two maps genuinely differ."""
    _livedb.add_ordered(
        s,
        Team(id=1, competition_slug="mls-2026", canonical_name="Alpha"),
        Team(id=2, competition_slug="mls-2026", canonical_name="Beta"))
    rows = []
    for i in range(n):
        home, away = (1, 2) if i % 2 == 0 else (2, 1)
        hg, ag = (2, 1) if i % 3 else (1, 1)
        fx = Fixture(id=100 + i, competition_slug="mls-2026",
                     espn_event_id=f"e{i}", status="post",
                     home_team_id=home, away_team_id=away,
                     home_goals=hg, away_goals=ag,
                     current_kickoff_utc=T0 + timedelta(days=3 * i))
        s.add(fx)
        rows.append(fx)
    s.flush()
    for i, fx in enumerate(rows):
        q = i in quarantine
        s.add_all([
            MlsTeamMatchStat(fixture_id=fx.id, team_id=fx.home_team_id,
                             side="home", goals=fx.home_goals,
                             goals_conceded=fx.away_goals,
                             xg=2.4 if q else 1.4, xg_against=0.6,
                             shots_on_target=5,
                             xg_quarantined=q,
                             xg_quarantine_reason=("implausible" if q
                                                   else None)),
            MlsTeamMatchStat(fixture_id=fx.id, team_id=fx.away_team_id,
                             side="away", goals=fx.away_goals,
                             goals_conceded=fx.home_goals,
                             xg=0.6, xg_against=2.4 if q else 1.4,
                             shots_on_target=3)])
    s.commit()
    return rows


def _freeze_market(s, fx, asks=(45, 28, 33)):
    """The canonical chain: t10 run -> three game-leg contracts -> the
    frozen quotes each cites."""
    ev = MarketEvent(competition_slug="mls-2026",
                     kalshi_event_ticker=f"KXMLSGAME-{fx.espn_event_id}",
                     series="KXMLSGAME", fixture_id=fx.id,
                     mapping_approved=True)
    s.add(ev)
    s.flush()
    run = PredictionRun(id=f"lock-{fx.id}", fixture_id=fx.id,
                        run_type="t10", status="complete", canonical=True,
                        captured_at=fx.current_kickoff_utc
                        - timedelta(minutes=10))
    s.add(run)
    s.flush()
    for key, ask in zip(("home_win", "draw", "away_win"), asks):
        mc = MarketContract(market_event_id=ev.id,
                            ticker=f"{ev.kalshi_event_ticker}-{key}",
                            outcome_key=key)
        s.add(mc)
        s.flush()
        q = MarketQuote(market_contract_id=mc.id,
                        captured_at=run.captured_at,
                        yes_ask_c=ask, yes_bid_c=ask - 1)
        s.add(q)
        s.flush()
        s.add(PredictionContract(prediction_run_id=run.id,
                                 market_contract_id=mc.id,
                                 market_quote_id=q.id, outcome_key=key,
                                 raw_probability=0.4))
    s.commit()


@pytest.fixture(autouse=True)
def _xg_alpha_on(monkeypatch):
    monkeypatch.setattr(config, "MLS_XG_RATING_ALPHA", 0.5)


class TestTheFrozenLadderIsUntouched:
    def test_no_new_rung_entered_the_production_ladder(self):
        """tests/test_ladder_parity.py pins the report; the approval
        rests on it. The variant must live OUTSIDE."""
        assert "M3Q" not in model_eval.LADDER
        assert all("M3Q" not in pair
                   for pair in model_eval.MLS_EDGE_PAIRS)

    def test_the_flag_is_not_flipped_by_running_the_evaluation(
            self, live_session):
        _seed_history(live_session)
        before = config.MLS_XG_QUARANTINE_EXCLUDE
        model_eval.evaluate_xg_quarantine_variant(n_boot=50)
        assert config.MLS_XG_QUARANTINE_EXCLUDE == before


class TestTheExcludedMap:
    def test_quarantined_fixtures_are_dropped_entirely(self, live_session):
        rows = _seed_history(live_session, quarantine=(3, 4))
        base = model_eval._mls_xg_map()
        excl = model_eval._mls_xg_map_quarantine_excluded()
        assert len(base) == 14
        assert len(excl) == 12
        assert rows[3].id not in excl and rows[4].id not in excl
        # BOTH sides go: dropping one would leave the opponent consuming
        # the rejected number as its xg_against
        assert rows[3].id in base and "away" in base[rows[3].id]

    def test_no_quarantine_means_identical_maps(self, live_session):
        _seed_history(live_session, quarantine=())
        assert (model_eval._mls_xg_map()
                == model_eval._mls_xg_map_quarantine_excluded())


class TestThePairedEvaluation:
    def test_the_payload_carries_the_number_and_its_ci(self, live_session):
        _seed_history(live_session)
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=200)
        assert out["n_paired"] > 0
        edge = out["m3_vs_m3q"]
        assert isinstance(edge["delta_log_loss"], float)
        lo, hi = edge["ci95"]
        assert lo <= hi
        assert isinstance(edge["significant"], bool)
        assert out["xg_map_fixtures"] == 14
        assert out["xg_map_fixtures_after_exclusion"] == 12
        assert len(out["quarantined_fixture_ids"]) == 2

    def test_the_two_policies_actually_differ(self, live_session):
        """If exclusion never changed a prediction the delta would be
        exactly zero and the evaluation tautological. The quarantined
        rows carry a deliberately implausible xg=2.4, so the fitted
        ratings must move."""
        rows = _seed_history(live_session)
        base = model_eval._mls_xg_map()
        excl = model_eval._mls_xg_map_quarantine_excluded()
        # SQLite hands back naive datetimes; normalize as the eval does
        as_of = model_eval._utc(rows[-1].current_kickoff_utc)
        cfg = dict(model_eval.LADDER["M3"])
        m_in = model_eval.fit_variant(rows[:-1], as_of, cfg,
                                      xg_by_fixture=base)
        m_ex = model_eval.fit_variant(rows[:-1], as_of, cfg,
                                      xg_by_fixture=excl)
        assert m_in["ratings"][1]["attack"] != m_ex["ratings"][1]["attack"]

    def test_dormant_plane_is_an_error_not_a_number(self, monkeypatch):
        monkeypatch.setattr(model_eval, "plane_ready", lambda: False)
        assert model_eval.evaluate_xg_quarantine_variant() == {
            "error": "dormant"}

    def test_no_xg_at_all_is_a_named_refusal(self, live_session):
        """A goals-only database cannot measure an xG question."""
        _livedb.add_ordered(
            live_session,
            Team(id=1, competition_slug="mls-2026", canonical_name="A"),
            Team(id=2, competition_slug="mls-2026", canonical_name="B"))
        for i in range(6):
            live_session.add(Fixture(
                id=200 + i, competition_slug="mls-2026",
                espn_event_id=f"g{i}", status="post",
                home_team_id=1, away_team_id=2, home_goals=1, away_goals=0,
                current_kickoff_utc=T0 + timedelta(days=i)))
        live_session.commit()
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=50)
        assert "no xG map" in out["refused"]


class TestTheFrozenMarketSection:
    def test_without_a_single_lock_the_section_refuses_by_name(
            self, live_session):
        _seed_history(live_session)
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=100)
        fm = out["frozen_market"]
        assert "canonical complete t10" in fm["refused"]
        assert "what_would_be_needed" in fm

    def test_with_a_lock_the_market_is_scored_beside_both_policies(
            self, live_session):
        rows = _seed_history(live_session)
        _freeze_market(live_session, rows[-1])
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=100)
        fm = out["frozen_market"]
        assert fm["n_with_frozen_market"] == 1
        assert set(fm["log_loss"]) == {"market", "m3_included",
                                       "m3_excluded"}
        assert "not an edge claim" in \
            fm["m3_included_vs_market"]["means"].lower()

    def test_a_two_leg_lock_contributes_nothing(self, live_session):
        """A partition missing a leg is a different quantity, not a
        weaker market view."""
        rows = _seed_history(live_session)
        _freeze_market(live_session, rows[-1])
        # break one leg: un-cite its quote
        pc = (live_session.query(PredictionContract)
              .filter_by(outcome_key="draw")
              .filter(PredictionContract.market_quote_id.isnot(None))
              .one())
        pc.market_quote_id = None
        live_session.commit()
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=100)
        assert "refused" in out["frozen_market"]

    def test_the_devig_basis_is_stated(self, live_session):
        rows = _seed_history(live_session)
        _freeze_market(live_session, rows[-1])
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=100)
        assert "yes_ask" in out["frozen_market"]["devig"]
        assert "assumption" in out["frozen_market"]["devig"]


class TestFlagStateIsEchoed:
    def test_the_payload_names_the_deployed_flag_and_whose_call_it_is(
            self, live_session):
        _seed_history(live_session)
        out = model_eval.evaluate_xg_quarantine_variant(n_boot=50)
        fs = out["flag_state"]
        assert fs["MLS_XG_QUARANTINE_EXCLUDE"] == \
            config.MLS_XG_QUARANTINE_EXCLUDE
        assert "operator" in fs["decision"]
