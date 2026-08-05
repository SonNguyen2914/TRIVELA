"""Liga MX live plane: competition-keyed identity/ingest/markets/runs
plus the DARK-model fail-closed contract. All canned — no network
anywhere; the Kalshi shapes are the LIVE open Apertura events of
2026-07-29 (research_archive/ligamx_*_2026-07-29.json).

The load-bearing pair here is TestLigamxModelIsDark: the same seeded
state produces ZERO runs while liga-mx-2026-v0 is unapproved and >=1
run the moment the F3 flag alone is flipped — proving the gate (not a
missing model or fixture) is what keeps Liga MX dark even though its
markets are OPEN. Flipping that flag in production is an explicit
operator action (POST /api/admin/liga-mx-2026/replay-approval/activate,
the same generalized replay-approval route EPL uses) — never boot,
never a request path, and the decision it writes records its own
REPLAYED-evidence weakness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from src.live import (identity, ingest, ligamx_plane, markets,
                      model_ligamx, runs)
from src.live import db as live_db
from src.live.models import (Competition, Fixture, LiveBase,
                             MarketContract, MarketEvent, ModelVersion,
                             PredictionRun, RegistryDiscovery, Team)
from tests.test_mls_shadow import _enforce_varchar_lengths

UTC = timezone.utc

# real mex.1 identities (ligamx_espn_teams_2026-07-29.json) — accents
# and all, because the accents are exactly what must survive
CANNED_LIGAMX = [
    {"id": 231, "displayName": "Puebla",
     "shortDisplayName": "Puebla", "abbreviation": "PUE"},
    {"id": 219, "displayName": "Guadalajara",
     "shortDisplayName": "Guadalajara", "abbreviation": "GDL"},
    {"id": 15720, "displayName": "Atlético de San Luis",
     "shortDisplayName": "Atl. San Luis", "abbreviation": "ASL"},
    {"id": 10125, "displayName": "Tijuana",
     "shortDisplayName": "Tijuana", "abbreviation": "TIJ"},
]


@pytest.fixture()
def ligamx_session(tmp_path, monkeypatch):
    """The live plane on a throwaway sqlite file, with the three
    competitions seeded — the multi-league world every test here
    assumes — and the PostgreSQL-grade VARCHAR guard on."""
    from tests import _livedb
    url, _livedb_done = _livedb.provision(tmp_path, monkeypatch)
    LiveBase.metadata.create_all(live_db.get_engine())
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _Session
    if _livedb.SIMULATE_VARCHAR:
        event.listen(_Session, "before_flush", _enforce_varchar_lengths)
    s = live_db.get_session()
    s.add(Competition(slug="mls-2026", name="MLS", season=2026))
    s.add(Competition(slug="epl-2026", name="Premier League", season=2026))
    s.add(Competition(slug="liga-mx-2026", name="Liga MX", season=2026))
    s.commit()
    yield s
    if _livedb.SIMULATE_VARCHAR:
        event.remove(_Session, "before_flush", _enforce_varchar_lengths)
    s.close()
    _livedb_done()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)



def _upcoming_cross_midnight() -> tuple["datetime", str]:
    """A kickoff at 01:00Z that is still in the FUTURE, plus the Kalshi
    date segment it must join to.

    These tests used to hardcode 2026-08-01T01:00Z with a `26JUL31`
    ticker. That is the cross-midnight case the ET-date join exists for —
    01:00Z is the previous day in US-Eastern — but as an ABSOLUTE date it
    rotted: discovery only maps upcoming fixtures, so once 1 Aug passed
    the fixture became historical, discovery saw nothing, and three tests
    failed on the calendar rather than on any code change.

    Derived relative to now, so the property under test (a UTC kickoff
    whose Eastern date is the day before) holds forever.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    ko = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
        hour=1, minute=0, second=0, microsecond=0)
    et_day = ko.astimezone(ZoneInfo("America/New_York"))
    assert et_day.date() < ko.date(), "not the cross-midnight case"
    return ko, et_day.strftime("%y%b%d").upper()

class TestLigamxIdentity:
    def test_seed_is_idempotent_and_competition_keyed(self, ligamx_session):
        out1 = identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        out2 = identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        assert out1["added_teams"] == 4 and out2["added_teams"] == 0
        assert (ligamx_session.query(Team)
                .filter_by(competition_slug="liga-mx-2026").count()) == 4

    def test_open_event_forms_resolve_through_approved_aliases(
            self, ligamx_session):
        """The two live open-event forms substring matching cannot
        cross, plus the accent bridge: ASCII Kalshi names must reach
        accented ESPN canonical names via the CURATED map only."""
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        t = identity.resolve("kalshi", "Tijuana de Caliente",
                             competition_slug="liga-mx-2026")
        assert t is not None and t.canonical_name == "Tijuana"
        t = identity.resolve("kalshi", "San Luis",
                             competition_slug="liga-mx-2026")
        assert t is not None
        assert t.canonical_name == "Atlético de San Luis"   # accent intact

    def test_cross_competition_resolution_fails_explicitly(
            self, ligamx_session):
        """The alias table is global (source, alias); a scoped resolve
        must refuse a hit that belongs to another league rather than
        silently attaching it. The UNSCOPED call is the CONTROL: it
        shows what the guard prevents."""
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        # control: without a scope the (Liga MX) team comes back
        assert identity.resolve("kalshi", "Guadalajara") is not None
        # guard: scoped to MLS or EPL, the Liga MX club must NOT attach
        assert identity.resolve("kalshi", "Guadalajara",
                                competition_slug="mls-2026") is None
        assert identity.resolve("kalshi", "Guadalajara",
                                competition_slug="epl-2026") is None

    def test_relegated_mazatlan_has_no_bridge(self):
        """Mazatlán appeared in 25/26 Kalshi titles but is not a mex.1
        club (Atlante replaced it) — the curated map must NOT carry it;
        any reappearance surfaces as unmapped instead."""
        assert not any("mazatlan" in k.lower()
                       for k in ligamx_plane.KALSHI_BRIDGES)

    def test_every_bridge_targets_a_real_espn_club(self):
        """Every bridge value must be one of the 18 archived ESPN
        displayNames — a typo here would silently strand a club."""
        espn_names = {
            "América", "Atlante", "Atlas", "Atlético de San Luis",
            "Cruz Azul", "FC Juarez", "Guadalajara", "León", "Monterrey",
            "Necaxa", "Pachuca", "Puebla", "Pumas UNAM", "Querétaro",
            "Santos", "Tigres UANL", "Tijuana", "Toluca"}
        assert set(ligamx_plane.KALSHI_BRIDGES.values()) <= espn_names


class TestLigamxIngest:
    def test_upsert_keys_fixtures_by_competition(self, ligamx_session):
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        now = datetime.now(UTC)
        f = {"espn_event_id": "401877027",
             "kickoff": now + timedelta(days=3),
             "home_name": "Puebla", "away_name": "Guadalajara",
             "status": "pre", "home_goals": None, "away_goals": None,
             "venue": "Estadio Cuauhtémoc"}
        created, _ = ingest._upsert_fixture(
            ligamx_session, f, now, competition_slug="liga-mx-2026")
        ligamx_session.commit()
        assert created
        row = (ligamx_session.query(Fixture)
               .filter_by(competition_slug="liga-mx-2026",
                          espn_event_id="401877027").one())
        assert row.home_team_id is not None
        assert (ligamx_session.get(Team, row.home_team_id).canonical_name
                == "Puebla")
        assert row.venue == "Estadio Cuauhtémoc"       # accents intact
        # and nothing leaked into the other competitions
        assert (ligamx_session.query(Fixture)
                .filter_by(competition_slug="mls-2026").count()) == 0
        assert (ligamx_session.query(Fixture)
                .filter_by(competition_slug="epl-2026").count()) == 0


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


class TestLigamxDiscovery:
    def test_discovery_maps_the_live_open_event_shape(
            self, ligamx_session, monkeypatch):
        """The REAL open event (PUECDG) with its REAL market labels
        ('Puebla'/'Tie'/'Guadalajara' — archived 2026-07-29) must map
        via the curated bridges and record the liga-mx-2026 slug."""
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        pue = identity.resolve_espn_name("Puebla",
                                         competition_slug="liga-mx-2026")
        gdl = identity.resolve_espn_name("Guadalajara",
                                         competition_slug="liga-mx-2026")
        # ESPN kickoff 2026-08-01T01:00Z = Jul 31 US-Eastern (the real
        # cross-midnight case the ET-date join exists for)
        ko, seg = _upcoming_cross_midnight()
        fx = Fixture(competition_slug="liga-mx-2026",
                     espn_event_id="401877027",
                     home_team_id=pue.id, away_team_id=gdl.id,
                     current_kickoff_utc=ko, original_kickoff_utc=ko,
                     status="pre")
        ligamx_session.add(fx)
        ligamx_session.commit()

        et = f"KXLIGAMXGAME-{seg}PUECDG"
        fake = _fake_kalshi_paged(
            {"KXLIGAMXGAME": [{"event_ticker": et,
                               "title": "Puebla vs Guadalajara"}]},
            {et: [
                {"ticker": f"{et}-PUE", "yes_sub_title": "Puebla"},
                {"ticker": f"{et}-TIE", "yes_sub_title": "Tie"},
                {"ticker": f"{et}-CDG", "yes_sub_title": "Guadalajara"},
            ]})
        monkeypatch.setattr(markets, "_kalshi_paged", fake)
        out = ligamx_plane.discover_and_map()
        assert out["events_seen"] == 1
        assert out["newly_mapped"] == 1
        assert out["discovery_complete"] is True

        ev = (ligamx_session.query(MarketEvent)
              .filter_by(kalshi_event_ticker=et).one())
        assert ev.competition_slug == "liga-mx-2026"
        assert ev.mapping_approved and ev.fixture_id == fx.id
        keys = {c.ticker: c.outcome_key for c in
                ligamx_session.query(MarketContract)
                .filter_by(market_event_id=ev.id)}
        assert keys[f"{et}-PUE"] == "home_win"
        assert keys[f"{et}-TIE"] == "draw"
        assert keys[f"{et}-CDG"] == "away_win"

        reg = (ligamx_session.query(RegistryDiscovery)
               .order_by(RegistryDiscovery.id.desc()).first())
        assert reg.competition_slug == "liga-mx-2026"
        assert reg.complete is True

    def test_family_suffix_join_inherits_the_game_mapping(
            self, ligamx_session, monkeypatch):
        """A non-game family event (live-verified: every family listed
        real PUECDG markets) must inherit the game event's fixture by
        exact suffix join, and its contract keys must come from the
        verified tail grammar."""
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        pue = identity.resolve_espn_name("Puebla",
                                         competition_slug="liga-mx-2026")
        gdl = identity.resolve_espn_name("Guadalajara",
                                         competition_slug="liga-mx-2026")
        ko, seg = _upcoming_cross_midnight()
        fx = Fixture(competition_slug="liga-mx-2026",
                     espn_event_id="401877027",
                     home_team_id=pue.id, away_team_id=gdl.id,
                     current_kickoff_utc=ko, original_kickoff_utc=ko,
                     status="pre")
        ligamx_session.add(fx)
        ligamx_session.commit()
        game_et = f"KXLIGAMXGAME-{seg}PUECDG"
        tot_et = f"KXLIGAMXTOTAL-{seg}PUECDG"
        spr_et = f"KXLIGAMXSPREAD-{seg}PUECDG"
        fake = _fake_kalshi_paged(
            {"KXLIGAMXGAME": [{"event_ticker": game_et,
                               "title": "Puebla vs Guadalajara"}],
             "KXLIGAMXTOTAL": [{"event_ticker": tot_et,
                                "title": "Puebla vs Guadalajara"}],
             "KXLIGAMXSPREAD": [{"event_ticker": spr_et,
                                 "title": "Puebla vs Guadalajara"}]},
            {game_et: [{"ticker": f"{game_et}-PUE",
                        "yes_sub_title": "Puebla"}],
             tot_et: [{"ticker": f"{tot_et}-3",
                       "yes_sub_title": "Over 2.5 goals scored"}],
             spr_et: [{"ticker": f"{spr_et}-CDG2",
                       "yes_sub_title":
                       "Guadalajara wins by more than 1.5 goals"}]})
        monkeypatch.setattr(markets, "_kalshi_paged", fake)
        out = ligamx_plane.discover_and_map()
        assert out["newly_mapped"] == 3
        for et in (tot_et, spr_et):
            ev = (ligamx_session.query(MarketEvent)
                  .filter_by(kalshi_event_ticker=et).one())
            assert ev.fixture_id == fx.id and ev.mapped_via == "suffix"
        keys = {c.ticker: c.outcome_key for c in
                ligamx_session.query(MarketContract).all()}
        assert keys[f"{tot_et}-3"] == "over_2_5"
        assert keys[f"{spr_et}-CDG2"] == "away_margin_2"

    def test_unbridged_name_stays_unmapped(
            self, ligamx_session, monkeypatch):
        """A Kalshi title side with no curated bridge (Mazatlán — a
        REAL archived 25/26 name, deliberately unbridged) must stay
        unmapped — an explicit state, never a fuzzy attach."""
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        pue = identity.resolve_espn_name("Puebla",
                                         competition_slug="liga-mx-2026")
        gdl = identity.resolve_espn_name("Guadalajara",
                                         competition_slug="liga-mx-2026")
        ko, seg = _upcoming_cross_midnight()
        ligamx_session.add(Fixture(
            competition_slug="liga-mx-2026", espn_event_id="401877027",
            home_team_id=pue.id, away_team_id=gdl.id,
            current_kickoff_utc=ko, original_kickoff_utc=ko,
            status="pre"))
        ligamx_session.commit()
        et = f"KXLIGAMXGAME-{seg}MAZPUE"
        fake = _fake_kalshi_paged(
            {"KXLIGAMXGAME": [{"event_ticker": et,
                               "title": "Mazatlan vs Puebla"}]},
            {et: []})
        monkeypatch.setattr(markets, "_kalshi_paged", fake)
        out = ligamx_plane.discover_and_map()
        assert out["newly_mapped"] == 0 and out["unmapped"] == 1
        ev = (ligamx_session.query(MarketEvent)
              .filter_by(kalshi_event_ticker=et).one())
        assert not ev.mapping_approved and ev.fixture_id is None


def _seed_ligamx_history(s, upcoming_in_hours: float = 20.0):
    """Round-robin completed history so every club clears MIN_GAMES,
    plus one upcoming fixture — mirrors the MLS/EPL test seed, WITHOUT
    any approval (the dark default)."""
    identity.seed_league_teams(
        "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
        ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
    teams = {t.canonical_name: t.id for t in
             s.query(Team).filter_by(competition_slug="liga-mx-2026")}
    ids = list(teams.values())
    now = datetime.now(UTC)
    k = 0
    for rnd in range(6):
        for a, b in ((0, 1), (2, 3), (0, 2), (1, 3)):
            k += 1
            s.add(Fixture(
                competition_slug="liga-mx-2026", espn_event_id=f"lmh{k}",
                home_team_id=ids[a], away_team_id=ids[b],
                current_kickoff_utc=now - timedelta(days=3 * rnd + 2),
                original_kickoff_utc=now - timedelta(days=3 * rnd + 2),
                status="post", home_goals=(a + 1) % 3,
                away_goals=b % 2))
    up = Fixture(competition_slug="liga-mx-2026", espn_event_id="lm9001",
                 home_team_id=ids[0], away_team_id=ids[1],
                 current_kickoff_utc=now + timedelta(hours=upcoming_in_hours),
                 original_kickoff_utc=now + timedelta(
                     hours=upcoming_in_hours),
                 status="pre")
    s.add(up)
    s.commit()
    return up


class TestHistoryFloorIsVisible:
    """The state that was invisible in production on 2026-07-30.

    Liga MX was approved, enabled and fully mapped, and the odds board
    served `[]` with a bare `model_dark: true`. Nothing distinguished
    "the sweep has not fired" from what was actually happening: the sweep
    fired every 15 minutes and `_raw` refused every club, because the
    Apertura the slug is named for was two rounds old and no club had
    MIN_GAMES completed matches. These tests make that state legible."""

    def test_census_names_the_history_floor_when_clubs_are_too_new(
            self, ligamx_session):
        """Two rounds played — under the floor — must report
        insufficient_team_history, not a reassuring empty."""
        identity.seed_league_teams(
            "liga-mx-2026", ligamx_plane.ESPN_TEAMS_URL,
            ligamx_plane.KALSHI_BRIDGES, espn_teams=CANNED_LIGAMX)
        teams = [t.id for t in ligamx_session.query(Team)
                 .filter_by(competition_slug="liga-mx-2026")]
        now = datetime.now(UTC)
        for i, (a, b) in enumerate(((0, 1), (2, 3))):
            ligamx_session.add(Fixture(
                competition_slug="liga-mx-2026", espn_event_id=f"thin{i}",
                home_team_id=teams[a], away_team_id=teams[b],
                current_kickoff_utc=now - timedelta(days=2 + i),
                original_kickoff_utc=now - timedelta(days=2 + i),
                status="post", home_goals=1, away_goals=0))
        ligamx_session.commit()

        c = ligamx_plane.empty_board_reason()
        assert c["state"] == "insufficient_team_history"
        assert c["clubs_rated"] == 0
        assert c["min_games"] == model_ligamx.MIN_GAMES
        assert c["max_games_seen"] < model_ligamx.MIN_GAMES

    def test_census_reports_ok_once_the_floor_is_cleared(
            self, ligamx_session):
        """The CONTROL. Without it, `insufficient_team_history` above
        could be what this function always says."""
        _seed_ligamx_history(ligamx_session)
        c = ligamx_plane.empty_board_reason()
        assert c["state"] == "ok"
        assert c["clubs_rated"] > 0
        assert c["max_games_seen"] >= model_ligamx.MIN_GAMES

    def test_history_ingest_pins_the_previous_season(self):
        """The fix for the empty board: pull the season BEFORE the one
        this slug is named for. Pinned, because an unpinned ingest lets
        the provider's idea of 'current' decide, which is what left every
        club under the floor. Measured on ESPN 2026-07-30: season=2025
        returns 40 completed fixtures for Guadalajara."""
        seen = {}

        def _fake(**kw):
            seen.update(kw)
            return {"created": 0}

        orig = ingest.ingest_season_schedules
        ingest.ingest_season_schedules = _fake
        try:
            ligamx_plane.ingest_history()
        finally:
            ingest.ingest_season_schedules = orig
        assert seen["expected_season_year"] == 2025
        assert seen["competition_slug"] == "liga-mx-2026"

    def test_history_lands_in_the_same_slug_so_team_ids_survive(self):
        """Not a style point. `Team` rows are competition-keyed, so
        ingesting history under its own slug would mint a second set of
        team ids, and ratings fitted on those could never be looked up
        for a current-season fixture."""
        assert ligamx_plane.HISTORY_SEASON_YEAR == 2025
        import inspect
        src = inspect.getsource(ligamx_plane.ingest_history)
        assert "LIGAMX_SLUG" in src


class TestLigamxModelIsDark:
    """THE dark contract, proven as a pair: identical seeded state, zero
    runs unapproved (guard) / runs the moment F3 alone flips (control) —
    so the approval gate, and nothing else, is what keeps Liga MX dark
    even with OPEN markets."""

    def test_scheduled_runs_refuse_while_unapproved(
            self, ligamx_session, monkeypatch):
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        monkeypatch.setattr(config, "LIGAMX_SHADOW_ENABLED", True)
        _seed_ligamx_history(ligamx_session)
        # register the model version DARK, exactly as the boot does
        model_ligamx.ensure_model_version(approved_for_shadow=False)
        r = ligamx_plane.scheduled_runs()
        assert "not approved" in r["skipped"]
        assert (ligamx_session.query(PredictionRun).count()) == 0
        # the odds board is an explicit empty, never a zero-bar
        assert ligamx_plane.latest_odds() == []

    def test_plane_switch_off_refuses_before_anything_else(
            self, ligamx_session, monkeypatch):
        """LIGAMX_SHADOW_ENABLED defaults FALSE: even a (hypothetically)
        approved model must not run while the plane switch is off."""
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        monkeypatch.setattr(config, "LIGAMX_SHADOW_ENABLED", False)
        _seed_ligamx_history(ligamx_session)
        model_ligamx.ensure_model_version(approved_for_shadow=True)
        r = ligamx_plane.scheduled_runs()
        assert "skipped" in r
        assert (ligamx_session.query(PredictionRun).count()) == 0

    def test_flipping_f3_alone_would_produce_runs(
            self, ligamx_session, monkeypatch):
        """CONTROL for the tests above (and the machinery-parity proof):
        the pipeline is fully wired — approve the version and the same
        state creates a run. In production no code path can perform
        this flip for Liga MX; it exists only to prove the gate is the
        blocker."""
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        monkeypatch.setattr(config, "LIGAMX_SHADOW_ENABLED", True)
        up = _seed_ligamx_history(ligamx_session)
        model_ligamx.ensure_model_version(approved_for_shadow=True)
        r = ligamx_plane.scheduled_runs()
        assert r["created"] >= 1
        board = ligamx_plane.latest_odds()
        row = next(o for o in board if o["espn_event_id"] == "lm9001")
        assert row["model_version"] == "liga-mx-2026-v0"
        assert sum(row["outcomes"].values()) == pytest.approx(1.0,
                                                              abs=0.01)
        # deterministic provider-keyed seed, liga-mx-scoped
        hub = ligamx_plane.model_for_event("lm9001")
        assert hub["model_version"] == "liga-mx-2026-v0"
        assert hub["latest"]["seed"] == model_ligamx.seed_for(
            up, "scheduled")

    def test_t10_needs_the_approval_decision_too(
            self, ligamx_session, monkeypatch):
        """Even with F3 flipped, a canonical lock still requires the
        immutable approval DECISION (F9) — which nothing in this build
        can create for Liga MX."""
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        monkeypatch.setattr(config, "LIGAMX_SHADOW_ENABLED", True)
        _seed_ligamx_history(ligamx_session, upcoming_in_hours=0.15)
        model_ligamx.ensure_model_version(approved_for_shadow=True)
        r = ligamx_plane.t10_locks()
        assert "approval decision" in r["skipped"]
        assert (ligamx_session.query(PredictionRun)
                .filter_by(canonical=True).count()) == 0

    def test_boot_registers_the_model_dark(self, ligamx_session,
                                           monkeypatch):
        """The boot's model registration must leave approved_for_shadow
        FALSE — a boot that quietly approved would hand the F3 gate a
        pass nobody earned. Network steps are stubbed; only the model
        registration runs for real."""
        monkeypatch.setattr(config, "LIGAMX_SHADOW_ENABLED", True)
        monkeypatch.setattr(ligamx_plane, "seed_teams",
                            lambda: {"stubbed": True})
        monkeypatch.setattr(ligamx_plane, "ingest_season",
                            lambda: {"stubbed": True})
        monkeypatch.setattr(ligamx_plane, "discover_and_map",
                            lambda: {"stubbed": True})
        ligamx_plane.boot()
        row = (ligamx_session.query(ModelVersion)
               .filter_by(name="liga-mx-2026-v0").one())
        assert row.approved_for_shadow is False
        assert ligamx_plane.approval_status()["mode"] == "dark"

    def test_boot_is_a_noop_while_the_plane_switch_is_off(
            self, ligamx_session, monkeypatch):
        """The default-off contract: with LIGAMX_SHADOW_ENABLED false
        (the shipped default) boot must do NOTHING — no teams, no model
        row, no network attempts."""
        monkeypatch.setattr(config, "LIGAMX_SHADOW_ENABLED", False)
        out = ligamx_plane.boot()
        assert out == {"skipped": "LIGAMX_SHADOW_ENABLED off"}
        assert (ligamx_session.query(ModelVersion)
                .filter_by(name="liga-mx-2026-v0").count()) == 0
        assert (ligamx_session.query(Team)
                .filter_by(competition_slug="liga-mx-2026").count()) == 0

    def test_approval_status_reports_dark(self, ligamx_session):
        model_ligamx.ensure_model_version(approved_for_shadow=False)
        st = ligamx_plane.approval_status()
        assert st["mode"] == "dark"
        assert st["approved_for_shadow"] is False
        assert st["approval_decision_missing"] is True
        assert st["model_version_registered"] is True

    def test_approval_status_note_reflects_a_real_decision(
            self, ligamx_session):
        """Regression guard for what /api/ligamx/approval showed in
        production on 2026-07-30 right after the first-ever Liga MX
        activation: mode/decision_id flipped correctly but `note` still
        read the DARK boilerplate ('no approval decision exists') sitting
        directly beside a populated decision_id in the same payload."""
        import hashlib as _h

        from src.live.models import ModelApprovalDecision

        model_ligamx.ensure_model_version(approved_for_shadow=True)
        mv = ligamx_session.query(ModelVersion).filter_by(
            name=model_ligamx.MODEL_NAME).one()
        doc = ('{"model":"liga-mx-2026-v0","mode":"shadow",'
               '"note":"test-only approval"}')
        dec = ModelApprovalDecision(
            model_version_id=mv.id,
            model_version_name=model_ligamx.MODEL_NAME,
            approved_mode="shadow", approved=True,
            policy_version="shadow-approval-replay-v1",
            decision_document=doc,
            content_hash=_h.sha256(doc.encode()).hexdigest(),
            created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        ligamx_session.add(dec)
        ligamx_session.commit()

        st = ligamx_plane.approval_status()
        assert st["mode"] == "approved_decision_present"
        assert st["decision_id"] == dec.id
        assert "UNAPPROVED" not in st["note"]
        assert str(dec.id) in st["note"]

    def test_status_endpoint_model_mode_tracks_a_real_decision(
            self, ligamx_session, monkeypatch):
        """Regression guard for the SECOND instance of the 724ef54 shape,
        found on 2026-07-30: /api/ligamx/approval read decision 214
        correctly while /api/ligamx/status (handler in api/main.py) kept
        reporting model.mode "dark" from a literal seeded at dict
        initialization. An operator comparing the two summaries for the
        same competition got opposite answers.

        This asserts the ENDPOINT, not just approval_status(), because
        approval_status() was already right — the route was the liar."""
        import hashlib as _h

        from fastapi.testclient import TestClient

        from api.main import app
        from src import ligamx as ligamx_data
        from src.live.models import ModelApprovalDecision

        monkeypatch.setattr(ligamx_data, "current_tournament",
                            lambda: {"name": "Torneo Apertura"})

        with TestClient(app) as c:
            dark = c.get("/api/ligamx/status").json()
        # unapproved: dark is the CORRECT answer, and must still be given
        assert dark["model"]["mode"] == "dark"
        assert dark["model"]["approved_for_shadow"] is False

        model_ligamx.ensure_model_version(approved_for_shadow=True)
        mv = ligamx_session.query(ModelVersion).filter_by(
            name=model_ligamx.MODEL_NAME).one()
        doc = ('{"model":"liga-mx-2026-v0","mode":"shadow",'
               '"note":"test-only approval"}')
        dec = ModelApprovalDecision(
            model_version_id=mv.id,
            model_version_name=model_ligamx.MODEL_NAME,
            approved_mode="shadow", approved=True,
            policy_version="shadow-approval-replay-v1",
            decision_document=doc,
            content_hash=_h.sha256(doc.encode()).hexdigest(),
            created_at=datetime.now(UTC) - timedelta(hours=1))
        ligamx_session.add(dec)
        ligamx_session.commit()

        with TestClient(app) as c:
            live = c.get("/api/ligamx/status").json()
        # the summary endpoint must not contradict the approval endpoint
        assert live["model"]["mode"] == "approved_decision_present"
        assert live["model"]["mode"] == (
            ligamx_plane.approval_status()["mode"])
        assert live["model"]["approval_decision_id"] == dec.id
        assert live["model"]["approved_for_shadow"] is True
        # money is NOT what this field means, and never was
        assert live["real_money_signals"] is False
        # the xG verdict is about data availability, not approval
        assert live["model"]["goals_only"] is True
        assert live["model"]["xg_source"] is None

    def test_no_ligamx_xg_knob_exists(self):
        """The xG gap is documented, not papered over: no Liga MX
        analogue of MLS_XG_RATING_ALPHA exists anywhere in config, and
        the Liga MX model module carries no xG rating machinery."""
        assert not hasattr(config, "LIGAMX_XG_RATING_ALPHA")
        assert not hasattr(model_ligamx, "XG_SHRINK_GAMES")


class TestNonAsciiEndToEnd:
    def test_canonical_artifact_preserves_accents_bytewise(
            self, ligamx_session, monkeypatch):
        """The ensure_ascii trap: a default json.dumps once produced
        false corpus mismatches by escaping non-ASCII names. The
        canonical form must carry 'América' as UTF-8 characters, never
        as \\u escapes, and the hash must be computed over those bytes."""
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        _seed_ligamx_history(ligamx_session)
        s = ligamx_session
        from src.live.db import get_session
        rows = model_ligamx._completed(get_session())
        model = model_ligamx.fit(rows, datetime.now(UTC))
        assert model is not None
        fx = (s.query(Fixture).filter_by(espn_event_id="lm9001").one())
        doc, canon, digest = model_ligamx.build_input_artifact(
            fx, model, "scheduled")
        # smuggle an accented name through the same canonicalizer the
        # artifact uses and prove it survives raw
        probe = model_ligamx._canonical(
            {"team": "Atlético de San Luis", "club": "América"})
        assert "América" in probe and "\\u" not in probe
        import hashlib as _h
        assert digest == _h.sha256(canon.encode()).hexdigest()
        assert doc["schema_version"] == "model-input-ligamx-v1"

    def test_replay_reproduces_from_the_stored_document(
            self, ligamx_session, monkeypatch):
        monkeypatch.setattr(config, "N_SIMULATIONS", 400)
        _seed_ligamx_history(ligamx_session)
        from src.live.db import get_session
        rows = model_ligamx._completed(get_session())
        model = model_ligamx.fit(rows, datetime.now(UTC))
        fx = (ligamx_session.query(Fixture)
              .filter_by(espn_event_id="lm9001").one())
        doc, _, _ = model_ligamx.build_input_artifact(fx, model,
                                                      "scheduled")
        a = model_ligamx.replay_from_artifact(doc, n_sims=400)
        b = model_ligamx.replay_from_artifact(doc, n_sims=400)
        assert a == b                     # deterministic, seed-pinned


class TestCompetitionIsolation:
    def test_other_league_counts_do_not_absorb_ligamx_state(
            self, ligamx_session):
        _seed_ligamx_history(ligamx_session)
        model_ligamx.ensure_model_version(approved_for_shadow=False)
        mls_counts = runs.shadow_counts()               # default = MLS
        lig_counts = ligamx_plane.shadow_counts()
        assert mls_counts["teams"] == 0                 # no MLS teams here
        assert lig_counts["teams"] == 4
        assert mls_counts["fixtures"] == 0
        assert lig_counts["fixtures"] == 25
        # the MLS default sweep must not touch Liga MX fixtures
        assert runs.scheduled_runs() == {
            "skipped": "no model (no completed fixtures ingested)"}

    def test_ligamx_seed_domain_is_its_own(self):
        from types import SimpleNamespace
        fx = SimpleNamespace(espn_event_id="401877027")
        from src.live import model_epl, model_mls
        seeds = {model_ligamx.seed_for(fx, "t10"),
                 model_mls.seed_for(fx, "t10"),
                 model_epl.seed_for(fx, "t10")}
        assert len(seeds) == 3            # three distinct seed domains


def test_no_test_in_this_file_pins_an_absolute_kickoff():
    """Time-rotted tests fail on the calendar, not on a code change.

    Three tests here hardcoded 2026-08-01T01:00Z with a `26JUL31`
    ticker. Discovery only maps UPCOMING fixtures, so the day after that
    date they began failing on main with `newly_mapped == 0` — a red
    suite that had nothing to do with any commit, and which masks the
    next real failure.
    """
    import re
    src = open(__file__, encoding="utf-8").read()
    body = src[:src.index("def test_no_test_in_this_file_pins_an_absolute")]
    hard = re.findall(r"datetime\(20\d\d,\s*\d+,\s*\d+", body)
    assert not hard, (
        f"absolute kickoff(s) {hard} — derive from now() instead, or this "
        f"file goes red on a date rather than on a defect")
