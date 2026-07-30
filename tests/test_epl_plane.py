"""EPL live plane: competition-keyed identity/ingest/markets/runs plus
the DARK-model fail-closed contract. All canned — no network anywhere.

The load-bearing pair here is TestEplModelIsDark: the same seeded state
produces ZERO runs while epl-2026-v0 is unapproved and >=1 run the
moment the F3 flag alone is flipped — proving the gate (not a missing
model or fixture) is what keeps the EPL dark. Flipping that flag in
production requires the operator approval path that does not exist for
EPL; no code path in this build can do it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from src.live import epl_plane, identity, ingest, markets, model_epl, runs
from src.live.db import get_session
from src.live import db as live_db
from src.live.models import (Competition, Fixture, LiveBase, MarketContract,
                             MarketEvent, ModelVersion, PredictionRun,
                             RegistryDiscovery, Team)
from tests.test_mls_shadow import _enforce_varchar_lengths

UTC = timezone.utc

CANNED_EPL = [
    {"id": 359, "displayName": "Arsenal",
     "shortDisplayName": "Arsenal", "abbreviation": "ARS"},
    {"id": 360, "displayName": "Manchester United",
     "shortDisplayName": "Man United", "abbreviation": "MUN"},
    {"id": 363, "displayName": "Chelsea",
     "shortDisplayName": "Chelsea", "abbreviation": "CHE"},
    {"id": 364, "displayName": "Liverpool",
     "shortDisplayName": "Liverpool", "abbreviation": "LIV"},
]


@pytest.fixture()
def epl_session(tmp_path, monkeypatch):
    """The live plane on a throwaway sqlite file, with BOTH competitions
    seeded — the two-league world every test here assumes — and the
    PostgreSQL-grade VARCHAR guard on."""
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
    s.add(Competition(slug="epl-2026", name="Premier League", season=2026))
    s.commit()
    yield s
    event.remove(_Session, "before_flush", _enforce_varchar_lengths)
    s.close()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


class TestEplIdentity:
    def test_seed_is_idempotent_and_competition_keyed(self, epl_session):
        out1 = identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        out2 = identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        assert out1["added_teams"] == 4 and out2["added_teams"] == 0
        assert (epl_session.query(Team)
                .filter_by(competition_slug="epl-2026").count()) == 4

    def test_man_utd_bridge_resolves_through_approved_alias(
            self, epl_session):
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        t = identity.resolve("kalshi", "Man Utd",
                             competition_slug="epl-2026")
        assert t is not None and t.canonical_name == "Manchester United"

    def test_cross_competition_resolution_fails_explicitly(
            self, epl_session):
        """The alias table is global (source, alias); once two leagues
        coexist, a scoped resolve must refuse a hit that belongs to the
        other league rather than silently attaching it. The UNSCOPED
        call below is the CONTROL: it shows what the guard prevents."""
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        # control: without a scope the (EPL) team comes back
        assert identity.resolve("kalshi", "Arsenal") is not None
        # guard: scoped to MLS, the EPL club must NOT attach
        assert identity.resolve("kalshi", "Arsenal",
                                competition_slug="mls-2026") is None

    def test_promoted_clubs_have_no_kalshi_bridge(self):
        """Coventry/Hull/Ipswich have no 25/26 Kalshi history — their
        names are unknowable until 26/27 listings, so the curated map
        must NOT guess them (they surface as unmapped instead)."""
        for club in ("Coventry", "Hull", "Ipswich"):
            assert not any(club.lower() in k.lower()
                           for k in epl_plane.KALSHI_BRIDGES)


class TestEplIngest:
    def test_upsert_keys_fixtures_by_competition(self, epl_session):
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        now = datetime.now(UTC)
        f = {"espn_event_id": "401879301",
             "kickoff": now + timedelta(days=3),
             "home_name": "Arsenal", "away_name": "Chelsea",
             "status": "pre", "home_goals": None, "away_goals": None,
             "venue": "Emirates Stadium"}
        created, _ = ingest._upsert_fixture(
            epl_session, f, now, competition_slug="epl-2026")
        epl_session.commit()
        assert created
        row = (epl_session.query(Fixture)
               .filter_by(competition_slug="epl-2026",
                          espn_event_id="401879301").one())
        assert row.home_team_id is not None
        assert (epl_session.get(Team, row.home_team_id).canonical_name
                == "Arsenal")
        # and nothing leaked into the MLS competition
        assert (epl_session.query(Fixture)
                .filter_by(competition_slug="mls-2026").count()) == 0


class TestSeasonIsPinnedAndValidated:
    """The provider decides which season it calls "current"; we must not
    let it decide which season the ratings are fitted on."""

    def _run(self, s, monkeypatch, events, expect_year=2026):
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL[:1])
        seen: list[dict] = []

        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"season": {"year": 2026,
                                   "displayName":
                                   "2026-27 English Premier League"},
                        "events": events}

        def fake(url, params=None, timeout=None):
            seen.append(params or {})
            return _R()
        monkeypatch.setattr(ingest.requests, "get", fake)
        out = epl_plane.ingest_season()
        return out, seen

    def _event(self, eid, year, label):
        now = datetime.now(UTC) + timedelta(days=30)
        return {"id": eid, "date": now.isoformat(),
                "season": {"year": year, "displayName": label},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": None,
                     "team": {"displayName": "Arsenal"}},
                    {"homeAway": "away", "score": None,
                     "team": {"displayName": "Arsenal"}}],
                    "status": {"type": {"state": "pre"}}}]}

    def test_the_request_is_pinned_to_the_configured_season(
            self, epl_session, monkeypatch):
        out, seen = self._run(epl_session, monkeypatch, [
            self._event("1", 2026, "2026-27 English Premier League")])
        assert seen and all(p.get("season") == "2026" for p in seen)
        assert out["season_pinned"] == 2026
        assert out["season_label"] == "2026-27"
        assert "error" not in out
        assert out["created"] == 1

    def test_a_wrong_season_event_is_refused_explicitly(
            self, epl_session, monkeypatch):
        """(Proven to fail: without validation the 2025-26 fixture is
        ingested as epl-2026 history and silently becomes training
        data.)"""
        out, _ = self._run(epl_session, monkeypatch, [
            self._event("1", 2026, "2026-27 English Premier League"),
            self._event("2", 2025, "2025-26 English Premier League")])
        assert out["created"] == 1
        # two requests per club (played + fixture=true), so the
        # same refused event is counted once per answer
        assert out["season_rejected"] == {"season_2025": 2}
        assert "provider season mismatch" in out["error"]
        assert (epl_session.query(Fixture)
                .filter_by(competition_slug="epl-2026",
                           espn_event_id="2").count()) == 0

    def test_an_event_with_no_season_block_is_refused_not_assumed(
            self, epl_session, monkeypatch):
        ev = self._event("3", 2026, "2026-27")
        ev.pop("season")
        out, _ = self._run(epl_session, monkeypatch, [ev])
        assert out["created"] == 0
        assert out["season_rejected"] == {"season_unknown": 2}
        assert "error" in out

    def test_a_mismatched_season_label_is_refused(self, epl_session,
                                                  monkeypatch):
        """The year alone is not the season: ESPN labels 2026-27 under
        year 2026, so a payload whose label disagrees with the pin is a
        provider change we must not absorb."""
        out, _ = self._run(epl_session, monkeypatch, [
            self._event("4", 2026, "2027-28 English Premier League")])
        assert out["created"] == 0
        assert out["season_rejected"] == {"season_label_mismatch": 2}

    def test_mls_ingest_is_unpinned_and_unchanged(self, epl_session,
                                                  monkeypatch):
        """CONTROL: the pin is opt-in. The MLS call site passes no
        expected season, so nothing about its behaviour moves."""
        captured: list[dict] = []

        class _R:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"events": []}

        def fake(url, params=None, timeout=None):
            captured.append(params or {})
            return _R()
        identity.seed_teams([{"id": 1, "displayName": "Austin FC",
                              "shortDisplayName": "Austin",
                              "abbreviation": "ATX"}])
        monkeypatch.setattr(ingest.requests, "get", fake)
        out = ingest.ingest_season_schedules()
        assert all("season" not in p for p in captured)
        assert out["season_pinned"] is None
        assert "error" not in out


def _fake_kalshi_paged(events_by_series, markets_by_event):
    """A cursor-complete _kalshi_paged double for both the events and
    markets endpoints, always reporting a clean full sweep."""
    def fake(url, params, key, page_limit=200, max_pages=30, meta=None):
        if meta is not None:
            meta.update({"pages": 1, "complete": True,
                         "cap_reached": False})
        if key == "events":
            return list(events_by_series.get(
                params.get("series_ticker"), []))
        if key == "markets":
            return list(markets_by_event.get(
                params.get("event_ticker"), []))
        return []
    return fake


class TestEplDiscovery:
    def test_discovery_maps_via_verified_bridges_and_records_slug(
            self, epl_session, monkeypatch):
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        ars = identity.resolve_espn_name("Arsenal",
                                         competition_slug="epl-2026")
        mun = identity.resolve_espn_name("Manchester United",
                                         competition_slug="epl-2026")
        ko = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
        fx = Fixture(competition_slug="epl-2026", espn_event_id="e1",
                     home_team_id=ars.id, away_team_id=mun.id,
                     current_kickoff_utc=ko, original_kickoff_utc=ko,
                     status="pre")
        epl_session.add(fx)
        epl_session.commit()

        et = "KXEPLGAME-26AUG22ARSMUN"
        fake = _fake_kalshi_paged(
            {"KXEPLGAME": [{"event_ticker": et,
                            "title": "Arsenal vs Man Utd"}]},
            {et: [
                {"ticker": f"{et}-ARS", "yes_sub_title": "Arsenal"},
                {"ticker": f"{et}-TIE", "yes_sub_title": "Tie"},
                {"ticker": f"{et}-MUN", "yes_sub_title": "Man Utd"},
            ]})
        monkeypatch.setattr(markets, "_kalshi_paged", fake)
        out = epl_plane.discover_and_map()
        assert out["events_seen"] == 1
        assert out["newly_mapped"] == 1
        assert out["discovery_complete"] is True

        ev = (epl_session.query(MarketEvent)
              .filter_by(kalshi_event_ticker=et).one())
        assert ev.competition_slug == "epl-2026"
        assert ev.mapping_approved and ev.fixture_id == fx.id
        keys = {c.ticker: c.outcome_key for c in
                epl_session.query(MarketContract)
                .filter_by(market_event_id=ev.id)}
        assert keys[f"{et}-ARS"] == "home_win"
        assert keys[f"{et}-TIE"] == "draw"
        assert keys[f"{et}-MUN"] == "away_win"     # via the Man Utd bridge

        reg = (epl_session.query(RegistryDiscovery)
               .order_by(RegistryDiscovery.id.desc()).first())
        assert reg.competition_slug == "epl-2026"
        assert reg.complete is True

    def test_archived_history_creates_no_rows(self, epl_session,
                                              monkeypatch):
        """P0-1, the persistence half: the provider answers with a full
        archived season (387 real KXEPLGAME events, 2025-26) and no
        current listings. Discovery must record NOTHING — a settled event
        is not registry material, and writing 387 of them costs the rate
        budget the slate needs. (Proven to fail: without the horizon
        bound this writes 387 MarketEvent rows.)"""
        from tests.test_epl import _archived_game_events
        events = _archived_game_events()
        assert len(events) == 387
        monkeypatch.setattr(markets, "_kalshi_paged",
                            _fake_kalshi_paged({"KXEPLGAME": events}, {}))
        out = epl_plane.discover_and_map()
        assert out["events_seen"] == 387
        assert out["historical_skipped"] == 387
        assert out["newly_mapped"] == 0 and out["unmapped"] == 0
        assert out["contracts_filled"] == 0
        assert epl_session.query(MarketEvent).count() == 0

    def test_a_current_event_in_the_same_sweep_is_recorded(
            self, epl_session, monkeypatch):
        """CONTROL for the test above: the bound is the DATE, not the
        sweep — one current event inside the same archived answer is
        recorded normally."""
        from datetime import date as _date
        from tests.test_epl import _archived_game_events, _ticker_for
        cur = _ticker_for(_date.today(), "ARSMUN")
        monkeypatch.setattr(
            markets, "_kalshi_paged",
            _fake_kalshi_paged(
                {"KXEPLGAME": _archived_game_events() + [
                    {"event_ticker": cur, "title": "Arsenal vs Man Utd"}]},
                {cur: []}))
        out = epl_plane.discover_and_map()
        assert out["historical_skipped"] == 387
        assert epl_session.query(MarketEvent).count() == 1
        assert (epl_session.query(MarketEvent).one()
                .kalshi_event_ticker) == cur

    def test_two_same_date_fixtures_refuse_to_attach(self, epl_session,
                                                     monkeypatch):
        """P0-4, the persistence half: (teams, date) is not a unique key
        by construction, and the mapping loop took the FIRST hit. Two
        fixtures for the same clubs on the same ET date must leave the
        event unmapped. (Proven to fail: the first-hit loop attaches to
        whichever fixture the query returned first.)"""
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        ars = identity.resolve_espn_name("Arsenal",
                                         competition_slug="epl-2026")
        mun = identity.resolve_espn_name("Manchester United",
                                         competition_slug="epl-2026")
        from datetime import date as _date
        day = _date.today()
        for n, hour in (("dup1", 12), ("dup2", 17)):
            ko = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            epl_session.add(Fixture(
                competition_slug="epl-2026", espn_event_id=n,
                home_team_id=ars.id, away_team_id=mun.id,
                current_kickoff_utc=ko, original_kickoff_utc=ko,
                status="pre"))
        epl_session.commit()
        from tests.test_epl import _ticker_for
        et = _ticker_for(day, "ARSMUN")
        monkeypatch.setattr(markets, "_kalshi_paged", _fake_kalshi_paged(
            {"KXEPLGAME": [{"event_ticker": et,
                            "title": "Arsenal vs Man Utd"}]}, {et: []}))
        out = epl_plane.discover_and_map()
        assert out["newly_mapped"] == 0 and out["unmapped"] == 1
        ev = (epl_session.query(MarketEvent)
              .filter_by(kalshi_event_ticker=et).one())
        assert ev.fixture_id is None and not ev.mapping_approved

    def test_unbridged_promoted_club_stays_unmapped(
            self, epl_session, monkeypatch):
        """A 26/27 Kalshi title for a promoted club (no verified bridge)
        must stay unmapped — an explicit state, never a fuzzy attach."""
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL + [
                {"id": 392, "displayName": "Coventry City",
                 "shortDisplayName": "Coventry", "abbreviation": "COV"}])
        cov = identity.resolve_espn_name("Coventry City",
                                         competition_slug="epl-2026")
        ars = identity.resolve_espn_name("Arsenal",
                                         competition_slug="epl-2026")
        ko = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)
        fx = Fixture(competition_slug="epl-2026", espn_event_id="e2",
                     home_team_id=ars.id, away_team_id=cov.id,
                     current_kickoff_utc=ko, original_kickoff_utc=ko,
                     status="pre")
        epl_session.add(fx)
        epl_session.commit()
        et = "KXEPLGAME-26AUG21ARSCOV"
        fake = _fake_kalshi_paged(
            {"KXEPLGAME": [{"event_ticker": et,
                            # a Kalshi name we have NO evidence for
                            "title": "Arsenal vs Coventry"}]},
            {et: []})
        monkeypatch.setattr(markets, "_kalshi_paged", fake)
        out = epl_plane.discover_and_map()
        assert out["newly_mapped"] == 0 and out["unmapped"] == 1
        ev = (epl_session.query(MarketEvent)
              .filter_by(kalshi_event_ticker=et).one())
        assert not ev.mapping_approved and ev.fixture_id is None


def _seed_epl_history(s, upcoming_in_hours: float = 20.0):
    """Round-robin completed history so every club clears MIN_GAMES,
    plus one upcoming fixture — mirrors the MLS test seed, WITHOUT any
    approval (the dark default)."""
    identity.seed_league_teams(
        "epl-2026", epl_plane.ESPN_TEAMS_URL,
        epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
    teams = {t.canonical_name: t.id for t in
             s.query(Team).filter_by(competition_slug="epl-2026")}
    ids = list(teams.values())
    now = datetime.now(UTC)
    k = 0
    for rnd in range(6):
        for a, b in ((0, 1), (2, 3), (0, 2), (1, 3)):
            k += 1
            s.add(Fixture(
                competition_slug="epl-2026", espn_event_id=f"eh{k}",
                home_team_id=ids[a], away_team_id=ids[b],
                current_kickoff_utc=now - timedelta(days=3 * rnd + 2),
                original_kickoff_utc=now - timedelta(days=3 * rnd + 2),
                status="post", home_goals=(a + 1) % 3,
                away_goals=b % 2))
    up = Fixture(competition_slug="epl-2026", espn_event_id="e9001",
                 home_team_id=ids[0], away_team_id=ids[1],
                 current_kickoff_utc=now + timedelta(hours=upcoming_in_hours),
                 original_kickoff_utc=now + timedelta(hours=upcoming_in_hours),
                 status="pre")
    s.add(up)
    s.commit()
    return up


class TestEplModelIsDark:
    """THE dark contract, proven as a pair: identical seeded state, zero
    runs unapproved (guard) / runs the moment F3 alone flips (control) —
    so the approval gate, and nothing else, is what keeps EPL dark."""

    def test_scheduled_runs_refuse_while_unapproved(
            self, epl_session, monkeypatch):
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        _seed_epl_history(epl_session)
        # register the model version DARK, exactly as the boot does
        model_epl.ensure_model_version(approved_for_shadow=False)
        r = epl_plane.scheduled_runs()
        assert "not approved" in r["skipped"]
        assert (epl_session.query(PredictionRun).count()) == 0
        # the odds board is an explicit empty, never a zero-bar
        assert epl_plane.latest_odds() == []

    def test_flipping_f3_alone_would_produce_runs(
            self, epl_session, monkeypatch):
        """CONTROL for the test above (and the machinery-parity proof):
        the pipeline is fully wired — approve the version and the same
        state creates a run. In production no code path can perform
        this flip for EPL; it exists only to prove the gate is the
        blocker."""
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        up = _seed_epl_history(epl_session)
        model_epl.ensure_model_version(approved_for_shadow=True)
        r = epl_plane.scheduled_runs()
        assert r["created"] >= 1
        board = epl_plane.latest_odds()
        row = next(o for o in board if o["espn_event_id"] == "e9001")
        assert row["model_version"] == "epl-2026-v0"
        assert sum(row["outcomes"].values()) == pytest.approx(1.0,
                                                              abs=0.01)
        # deterministic provider-keyed seed, epl-scoped
        hub = epl_plane.model_for_event("e9001")
        assert hub["model_version"] == "epl-2026-v0"
        assert hub["latest"]["seed"] == model_epl.seed_for(up, "scheduled")

    def test_t10_needs_the_approval_decision_too(
            self, epl_session, monkeypatch):
        """Even with F3 flipped, a canonical lock still requires the
        immutable approval DECISION (F9) — which nothing in this build
        can create for EPL."""
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        _seed_epl_history(epl_session, upcoming_in_hours=0.15)
        model_epl.ensure_model_version(approved_for_shadow=True)
        r = epl_plane.t10_locks()
        assert "approval decision" in r["skipped"]
        assert (epl_session.query(PredictionRun)
                .filter_by(canonical=True).count()) == 0

    def test_approval_status_reports_dark(self, epl_session):
        model_epl.ensure_model_version(approved_for_shadow=False)
        st = epl_plane.approval_status()
        assert st["mode"] == "dark"
        assert st["approved_for_shadow"] is False
        assert st["approval_decision_missing"] is True
        assert st["model_version_registered"] is True

    def test_empty_board_reason_names_the_horizon_not_the_history(
            self, epl_session):
        """EPL's empty board has a DIFFERENT cause than Liga MX's, and the
        shared reason must tell them apart. Seeded with plenty of history
        but the only upcoming fixture far outside the 168h sweep horizon —
        which is EPL's real 2026-07-30 state (first fixture 2026-08-21,
        22 days out). Must report no_fixtures_in_horizon, NOT
        insufficient_team_history."""
        _seed_epl_history(epl_session, upcoming_in_hours=24 * 22)
        model_epl.ensure_model_version(approved_for_shadow=True)
        r = epl_plane.empty_board_reason()
        assert r["state"] == "no_fixtures_in_horizon"
        assert r["fixtures_in_horizon"] == 0
        assert r["clubs_rated"] > 0          # history is NOT the problem

    def test_empty_board_reason_names_history_when_that_is_the_cause(
            self, epl_session):
        """The CONTROL for the test above: same function, other cause.
        Without this pair, either label could be the only one it ever
        emits."""
        identity.seed_league_teams(
            "epl-2026", epl_plane.ESPN_TEAMS_URL,
            epl_plane.KALSHI_BRIDGES, espn_teams=CANNED_EPL)
        r = epl_plane.empty_board_reason()
        assert r["state"] in ("no_completed_fixtures",
                             "insufficient_team_history")
        assert r["clubs_rated"] == 0

    def test_no_epl_xg_knob_exists(self):
        """The xG gap is documented, not papered over: no EPL analogue
        of MLS_XG_RATING_ALPHA exists anywhere in config, and the EPL
        model module carries no xG rating machinery."""
        assert not hasattr(config, "EPL_XG_RATING_ALPHA")
        assert not hasattr(model_epl, "XG_SHRINK_GAMES")


class TestCompetitionIsolation:
    def test_mls_counts_do_not_absorb_epl_state(self, epl_session):
        _seed_epl_history(epl_session)
        model_epl.ensure_model_version(approved_for_shadow=False)
        mls_counts = runs.shadow_counts()               # default = MLS
        epl_counts = epl_plane.shadow_counts()
        assert mls_counts["teams"] == 0                 # no MLS teams here
        assert epl_counts["teams"] == 4
        assert mls_counts["fixtures"] == 0
        assert epl_counts["fixtures"] == 25
        # the MLS default sweep must not touch EPL fixtures
        assert runs.scheduled_runs() == {
            "skipped": "no model (no completed fixtures ingested)"}

    def test_epl_seed_for_differs_from_mls_seed_for(self):
        from types import SimpleNamespace
        fx = SimpleNamespace(espn_event_id="12345")
        from src.live import model_mls
        assert (model_epl.seed_for(fx, "t10")
                != model_mls.seed_for(fx, "t10"))


# --- P0-3: the COMPOSED EPL T-10 lock path --------------------------------
# The dark contract stops EPL locks today, so the future path was never
# exercised end to end. These tests grant EPL an approval decision (the
# one thing production cannot do) and then walk the whole chain, asserting
# that every frozen artefact is EPL's and no MLS state is consulted.

_ENG1_SUMMARY = {
    # reduced from research_archive/epl/espn_summary_740966_*.json —
    # eng.1 summaries carry `rosters` with the same shape usa.1 does
    "rosters": [
        {"homeAway": "home", "formation": "4-3-3", "roster": [
            {"starter": i == 0, "jersey": str(i + 1),
             "position": {"abbreviation": "G" if i == 0 else "M"},
             "athlete": {"id": f"e{i}", "displayName": f"EPL Home {i}"}}
            for i in range(11)]},
        {"homeAway": "away", "formation": "4-2-3-1", "roster": [
            {"starter": i == 0, "jersey": str(i + 1),
             "position": {"abbreviation": "G" if i == 0 else "M"},
             "athlete": {"id": f"a{i}", "displayName": f"EPL Away {i}"}}
            for i in range(11)]},
    ],
}


def _epl_approval_decision(s):
    """The immutable EPL approval decision. Production has NO path that
    can create one — this is the test granting what the dark contract
    withholds, so the future T-10 path can be exercised at all."""
    import hashlib as _h

    from src.live.models import ModelApprovalDecision, ModelVersion
    mv = s.query(ModelVersion).filter_by(
        name=model_epl.MODEL_NAME).one()
    doc = ('{"model":"epl-2026-v0","mode":"shadow",'
           '"note":"test-only approval"}')
    dec = ModelApprovalDecision(
        model_version_id=mv.id, model_version_name=model_epl.MODEL_NAME,
        approved_mode="shadow", approved=True, decision_document=doc,
        content_hash=_h.sha256(doc.encode()).hexdigest(),
        created_at=datetime.now(UTC) - timedelta(hours=1))
    s.add(dec)
    s.commit()
    return dec


def _mls_decoy(s):
    """MLS state that a competition-blind path would reach for: an
    approved MLS model version and its own approval decision."""
    import hashlib as _h

    from src.live.models import ModelApprovalDecision, ModelVersion
    from src.live import model_mls
    mv = ModelVersion(name=model_mls.MODEL_NAME, description="decoy",
                      approved_for_shadow=True,
                      created_at=datetime.now(UTC))
    s.add(mv)
    s.flush()
    doc = '{"model":"mls-2026-v0","mode":"shadow","note":"decoy"}'
    dec = ModelApprovalDecision(
        model_version_id=mv.id, model_version_name=model_mls.MODEL_NAME,
        approved_mode="shadow", approved=True, decision_document=doc,
        content_hash=_h.sha256(doc.encode()).hexdigest(),
        created_at=datetime.now(UTC) - timedelta(hours=2))
    s.add(dec)
    s.commit()
    return mv, dec


class TestEplT10LockIsFullyEplKeyed:
    TICKER = "KXEPLGAME-26AUG21ARSMUN"
    LEGS = (("-ARS", "home_win"), ("-TIE", "draw"), ("-MUN", "away_win"))

    def _arm(self, s, monkeypatch):
        from src.live.models import (MarketContract, MarketEvent,
                                     RegistryDiscovery)
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        monkeypatch.setattr(config, "PAPER_TRADING_ENABLED", True)
        up = _seed_epl_history(s, upcoming_in_hours=0.15)
        model_epl.ensure_model_version(approved_for_shadow=True)
        _epl_approval_decision(s)
        _mls_decoy(s)
        ev = MarketEvent(competition_slug="epl-2026",
                         kalshi_event_ticker=self.TICKER,
                         series="KXEPLGAME", fixture_id=up.id,
                         mapping_approved=True, mapped_via="alias",
                         title="Arsenal vs Man Utd")
        s.add(ev)
        s.flush()
        for tail, okey in self.LEGS:
            s.add(MarketContract(market_event_id=ev.id,
                                 ticker=f"{self.TICKER}{tail}",
                                 outcome_key=okey))
        s.add(RegistryDiscovery(competition_slug="epl-2026",
                                provider="kalshi", complete=True,
                                events_seen=1,
                                completed_at=datetime.now(UTC)))
        s.commit()

        def fake_get(url, **kw):
            if "orderbook" in url:
                return {"orderbook": {"yes": [[24, 500], [25, 900]],
                                      "no": [[70, 400], [74, 800]]}}
            return {"markets": [
                {"ticker": f"{self.TICKER}{tail}", "yes_bid": 25,
                 "yes_ask": 26, "no_bid": 74, "no_ask": 75,
                 "yes_bid_size": 500, "yes_ask_size": 500,
                 "status": "active"}
                for tail, _k in self.LEGS]}
        monkeypatch.setattr(markets, "_kalshi_get", fake_get)

        seen_urls: list[str] = []

        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return _ENG1_SUMMARY

        def fake_espn(url, **kw):
            seen_urls.append(url)
            return _R()
        from src.live import lineups
        monkeypatch.setattr(lineups.requests, "get", fake_espn)
        from src import alerts
        monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: None)
        return up, seen_urls

    def test_the_lock_freezes_epl_approval_engine_lineup_and_paper(
            self, epl_session, monkeypatch):
        """(Proven to fail: with the MLS league restored in lineups.py
        the lineup is fetched from usa.1; with the MLS engine restored in
        paper.capture_evaluation_context the frozen context carries the
        MLS signature.)"""
        from src.live.models import (LineupSnapshot, ModelApprovalDecision,
                                     PaperEvaluationContext, PredictionRun)
        up, seen_urls = self._arm(epl_session, monkeypatch)
        assert epl_plane.t10_locks() == {"locked": 1}

        lock = (epl_session.query(PredictionRun)
                .filter_by(fixture_id=up.id, run_type="t10",
                           canonical=True, status="complete").one())
        # 1. the EPL approval decision, not the MLS decoy
        epl_dec = (epl_session.query(ModelApprovalDecision)
                   .filter_by(model_version_name="epl-2026-v0").one())
        assert lock.model_approval_decision_id == epl_dec.id
        # 2. the EPL engine signature, frozen in the input artifact
        from src.live.models import ModelInputArtifact
        art = epl_session.get(ModelInputArtifact,
                              lock.model_input_artifact_id)
        doc = json.loads(art.document_json)
        assert doc["model"] == "epl-2026-v0"
        assert doc["schema_version"] == model_epl.INPUT_ARTIFACT_SCHEMA
        assert (doc["engine"]["signature_hash"]
                == model_epl.engine_signature()["signature_hash"])
        from src.live import model_mls
        assert (doc["engine"]["signature_hash"]
                != model_mls.engine_signature()["signature_hash"])
        # 3. the EPL lineup source — eng.1, and recorded as such
        assert seen_urls and all("eng.1" in u for u in seen_urls)
        assert not any("usa.1" in u for u in seen_urls)
        snap = epl_session.get(LineupSnapshot, lock.lineup_snapshot_id)
        assert snap is not None
        from src.live.models import SourceObservation
        obs = epl_session.get(SourceObservation, snap.source_observation_id)
        assert "league=eng.1" in obs.endpoint
        # 4. the EPL paper context
        ctx = (epl_session.query(PaperEvaluationContext)
               .filter_by(prediction_run_id=lock.id).one())
        assert (ctx.engine_signature_hash
                == model_epl.engine_signature()["signature_hash"])
        assert ctx.model_approval_decision_id == epl_dec.id
        assert json.loads(ctx.exec_policy_json)["version"]

    def test_epl_replay_and_audit_pass_under_the_epl_engine(
            self, epl_session, monkeypatch):
        from src.live import audit as live_audit
        self._arm(epl_session, monkeypatch)
        assert epl_plane.t10_locks() == {"locked": 1}
        from src.live.models import PredictionRun
        lock = (epl_session.query(PredictionRun)
                .filter_by(run_type="t10", canonical=True).one())

        rep = live_audit.verify_replay(lock.id)
        assert rep["competition_slug"] == "epl-2026"
        assert rep["model_version"] == "epl-2026-v0"
        assert rep["engine_match"] is True
        assert rep["replayable"] is True, rep

        aud = live_audit.lock_audit(competition_slug="epl-2026")
        assert aud["competition_slug"] == "epl-2026"
        assert aud["model_version"] == "epl-2026-v0"
        assert aud["summary"]["canonical_locks"] == 1
        failures = {k: v for k, v
                    in aud["locks"][0]["checks"].items() if not v}
        assert failures == {}, failures
        assert aud["summary"]["clean"] is True
        # the MLS audit must not see the EPL lock at all
        mls = live_audit.lock_audit(competition_slug="mls-2026")
        assert mls["summary"]["canonical_locks"] == 0

    def test_a_reboot_preserves_the_valid_approval(self, epl_session,
                                                   monkeypatch):
        """A restart must not destroy the immutable decision. It DOES
        reset ModelVersion.approved_for_shadow — the fail-closed boot
        rule (AGENTS.md §4), deliberate and asserted here so the
        distinction between the durable DECISION and the runtime FLAG
        stays explicit."""
        import hashlib as _h

        from src.live.models import ModelApprovalDecision
        self._arm(epl_session, monkeypatch)
        before = (epl_session.query(ModelApprovalDecision)
                  .filter_by(model_version_name="epl-2026-v0").one())
        before_id, before_hash = before.id, before.content_hash

        monkeypatch.setattr(epl_plane, "seed_teams", lambda: {"ok": True})
        monkeypatch.setattr(epl_plane, "ingest_season",
                            lambda: {"ok": True})
        monkeypatch.setattr(epl_plane, "discover_and_map",
                            lambda: {"ok": True})
        epl_plane.boot()
        epl_session.expire_all()

        after = (epl_session.query(ModelApprovalDecision)
                 .filter_by(model_version_name="epl-2026-v0").one())
        assert after.id == before_id
        assert after.content_hash == before_hash
        assert (_h.sha256(after.decision_document.encode()).hexdigest()
                == after.content_hash)
        st = epl_plane.approval_status()
        assert st["approval_decision_missing"] is False
        assert st["decision_id"] == before_id
        # the FLAG is fail-closed on boot, by design
        assert st["approved_for_shadow"] is False
        # the note is user-facing text an operator reads directly (it is
        # what /api/epl/approval showed in production on 2026-07-30 right
        # after activation) — it must not keep asserting "no approval
        # decision exists" next to a live decision_id in the same payload
        assert "UNAPPROVED" not in st["note"]
        assert str(before_id) in st["note"]


# --- P0-2: corpus / observability / audit are competition-scoped ----------
# Nine corpus sections were whole-table dumps, and every metric was a
# whole-table count. With two competitions in one database that means EPL
# state inside MLS outputs — and a changing MLS manifest hash.

def _seed_league(s, slug, *, team_name, alias, model_name, espn_id,
                 ticker, hashseed):
    """One competition's minimum end-to-end row set, disjoint from any
    other league's: team + approved alias, fixture, model version +
    approval decision, market event, lock snapshot, registry sweep,
    prediction run, paper context + signal + fill, and a player."""
    from src.live.models import (MarketEvent, MarketSnapshot,
                                 ModelApprovalDecision, ModelVersion,
                                 PaperEvaluationContext, PaperFill,
                                 PaperSignal, Player, PredictionRun,
                                 RegistryDiscovery, TeamAlias)
    now = datetime.now(UTC)
    team = Team(competition_slug=slug, canonical_name=team_name,
                abbrev=team_name[:3].upper(), espn_id=espn_id)
    s.add(team)
    s.flush()
    s.add(TeamAlias(team_id=team.id, alias=alias, source="kalshi",
                    approved=True))
    fx = Fixture(competition_slug=slug, espn_event_id=f"{espn_id}001",
                 home_team_id=team.id, away_team_id=team.id,
                 current_kickoff_utc=now - timedelta(hours=2),
                 original_kickoff_utc=now - timedelta(hours=2),
                 status="post", home_goals=1, away_goals=0)
    s.add(fx)
    mv = ModelVersion(name=model_name, description=slug, created_at=now)
    s.add(mv)
    s.add(Player(competition_slug=slug, espn_id=f"p{espn_id}",
                 name=f"{team_name} Keeper", position="G"))
    s.flush()
    dec = ModelApprovalDecision(
        model_version_id=mv.id, model_version_name=model_name,
        approved_mode="shadow", approved=True,
        decision_document=f"decision for {slug}",
        content_hash=f"{hashseed}" * 8, created_at=now)
    s.add(dec)
    s.add(MarketEvent(competition_slug=slug, kalshi_event_ticker=ticker,
                      series=ticker.split("-")[0], title=f"{team_name} vs X",
                      mapping_approved=True, fixture_id=fx.id))
    s.add(RegistryDiscovery(competition_slug=slug, provider="kalshi",
                            complete=True, events_seen=1,
                            completed_at=now))
    s.flush()
    snap = MarketSnapshot(fixture_id=fx.id, captured_at=now,
                          status="failed", policy_version=f"{slug}-lock",
                          failure_reason=f"{slug} snapshot failure")
    s.add(snap)
    run = PredictionRun(fixture_id=fx.id, run_type="t10", canonical=True,
                        status="complete", captured_at=now,
                        created_at=now, model_version_id=mv.id,
                        model_approval_decision_id=dec.id,
                        seconds_before_kickoff=600)
    s.add(run)
    s.flush()
    ctx = PaperEvaluationContext(prediction_run_id=run.id, captured_at=now,
                                 model_approved=True,
                                 engine_signature_hash=f"{hashseed}" * 8,
                                 content_hash=f"{hashseed}" * 8)
    s.add(ctx)
    s.flush()
    sig = PaperSignal(prediction_run_id=run.id, fixture_id=fx.id,
                      outcome_key="home_win", policy_version=f"{slug}-exec",
                      decision="fill", created_at=now,
                      paper_evaluation_context_id=ctx.id)
    s.add(sig)
    s.flush()
    s.add(PaperFill(paper_signal_id=sig.id, requested_contracts=100,
                    filled_contracts=100, status="settled", pnl_c=11,
                    cost_dollars="1.00", pnl_dollars="0.11",
                    execution_class="bounded_depth", created_at=now))
    s.commit()
    return {"team": team, "fixture": fx, "run": run, "model": mv}


class TestCompetitionScopedOutputs:
    """P0-2 acceptance. Disjoint MLS and EPL state; the MLS corpus must
    contain no EPL identifier, stay referentially closed, keep its
    manifest hash when EPL-only rows land, and keep its metrics."""

    EPL_MARKERS = ("epl-2026", "Everton FC EPL", "Everton EPL",
                   "KXEPLGAME-26AUG21EVEARS", "epl-2026-v0",
                   "epl-2026-lock", "epl-2026-exec")

    def _both(self, s):
        _seed_league(s, "mls-2026", team_name="Austin FC MLS",
                     alias="Austin MLS", model_name="mls-2026-v0",
                     espn_id="9001", ticker="KXMLSGAME-26AUG21AUSPOR",
                     hashseed="a")
        _seed_league(s, "epl-2026", team_name="Everton FC EPL",
                     alias="Everton EPL", model_name="epl-2026-v0",
                     espn_id="9002", ticker="KXEPLGAME-26AUG21EVEARS",
                     hashseed="b")

    def test_mls_corpus_holds_no_epl_identifier_and_stays_closed(
            self, epl_session):
        """(Proven to fail: with the whole-table dumps restored, the MLS
        corpus carries the EPL alias, model version, approval decision,
        registry sweep, player, paper context, signal and fill.)"""
        import json as _json

        from src.live import corpus
        self._both(epl_session)
        bundle = corpus.build_corpus("scope-test",
                                     competition_slug="mls-2026")
        body = _json.dumps(bundle["sections"], sort_keys=True,
                           default=str)
        for marker in self.EPL_MARKERS:
            assert marker not in body, marker
        assert bundle["manifest"]["competition_slug"] == "mls-2026"
        assert bundle["manifest"]["model_versions"] == ["mls-2026-v0"]

        sec = bundle["sections"]
        ids = {name: {r["id"] for r in rows}
               for name, rows in sec.items()
               if isinstance(rows, list) and rows and "id" in rows[0]}

        def resolves(section, rows, field, target):
            for r in rows:
                if r.get(field) is not None:
                    assert r[field] in ids.get(target, set()), \
                        f"{section}.{field}={r[field]} dangles"

        resolves("team_aliases.json", sec["team_aliases.json"],
                 "team_id", "teams.json")
        resolves("fixtures.json", sec["fixtures.json"],
                 "home_team_id", "teams.json")
        resolves("prediction_runs.json", sec["prediction_runs.json"],
                 "model_version_id", "model_versions.json")
        resolves("prediction_runs.json", sec["prediction_runs.json"],
                 "model_approval_decision_id",
                 "model_approval_decisions.json")
        resolves("paper_signals.json", sec["paper_signals.json"],
                 "paper_evaluation_context_id",
                 "paper_evaluation_contexts.json")
        resolves("paper_fills.json", sec["paper_fills.json"],
                 "paper_signal_id", "paper_signals.json")
        # the audit's retained failure evidence must be MLS's, not both
        reasons = [f["failure_reason"] for f in
                   sec["audit.json"]["failed_snapshots"]]
        assert reasons == ["mls-2026 snapshot failure"]

    def test_an_epl_only_row_leaves_the_mls_manifest_hash_unchanged(
            self, epl_session):
        from src.live import corpus
        from src.live.models import RegistryDiscovery, TeamAlias
        self._both(epl_session)
        before = corpus.build_corpus(
            "scope-test", competition_slug="mls-2026")["manifest"]
        epl_team = (epl_session.query(Team)
                    .filter_by(competition_slug="epl-2026").one())
        epl_session.add(TeamAlias(team_id=epl_team.id,
                                  alias="Everton EPL 2", source="kalshi",
                                  approved=True))
        epl_session.add(RegistryDiscovery(
            competition_slug="epl-2026", provider="kalshi", complete=True,
            events_seen=7, completed_at=datetime.now(UTC)))
        epl_session.commit()
        after = corpus.build_corpus(
            "scope-test", competition_slug="mls-2026")["manifest"]
        assert after["manifest_hash"] == before["manifest_hash"]
        # CONTROL: an MLS-only row DOES move it — the hash is not inert
        mls_team = (epl_session.query(Team)
                    .filter_by(competition_slug="mls-2026").one())
        epl_session.add(TeamAlias(team_id=mls_team.id, alias="Austin MLS 2",
                                  source="kalshi", approved=True))
        epl_session.commit()
        moved = corpus.build_corpus(
            "scope-test", competition_slug="mls-2026")["manifest"]
        assert moved["manifest_hash"] != before["manifest_hash"]

    def test_mls_metrics_are_unchanged_by_epl_state(self, epl_session):
        from src.live import observability
        _seed_league(epl_session, "mls-2026", team_name="Austin FC MLS",
                     alias="Austin MLS", model_name="mls-2026-v0",
                     espn_id="9001", ticker="KXMLSGAME-26AUG21AUSPOR",
                     hashseed="a")
        before = observability.metrics(competition_slug="mls-2026")
        _seed_league(epl_session, "epl-2026", team_name="Everton FC EPL",
                     alias="Everton EPL", model_name="epl-2026-v0",
                     espn_id="9002", ticker="KXEPLGAME-26AUG21EVEARS",
                     hashseed="b")
        after = observability.metrics(competition_slug="mls-2026")
        for key in ("data", "locks", "runs", "paper"):
            assert after[key] == before[key], key
        # and the EPL surface reports its own, non-zero state
        epl = observability.metrics(competition_slug="epl-2026")
        assert epl["data"]["mapped_market_events"] == 1
        assert epl["paper"]["signals"] == 1
        assert epl["locks"]["failed_snapshots"] == 1

    def test_the_manifest_hash_is_stable_for_an_unchanged_database(
            self, epl_session):
        """corpus.py promises "the same database state exports to the
        same manifest_hash". It did not: the embedded lock audit carried
        its own wall-clock `generated_at`, so two back-to-back builds of
        an UNCHANGED database hashed differently. Found while proving
        P0-2 — the EPL-isolation assertion could not distinguish
        contamination from clock drift. (Proven to fail: restore
        generated_at inside the exported audit section.)"""
        from src.live import corpus
        self._both(epl_session)
        a = corpus.build_corpus("scope-test",
                                competition_slug="mls-2026")["manifest"]
        b = corpus.build_corpus("scope-test",
                                competition_slug="mls-2026")["manifest"]
        assert a["manifest_hash"] == b["manifest_hash"]

    def test_an_unknown_competition_fails_explicitly(self):
        from src.live import competitions
        with pytest.raises(KeyError):
            competitions.spec("serie-a-2026")
