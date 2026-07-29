"""Liga MX live plane: competition-keyed identity/ingest/markets/runs
plus the DARK-model fail-closed contract. All canned — no network
anywhere; the Kalshi shapes are the LIVE open Apertura events of
2026-07-29 (research_archive/ligamx_*_2026-07-29.json).

The load-bearing pair here is TestLigamxModelIsDark: the same seeded
state produces ZERO runs while liga-mx-2026-v0 is unapproved and >=1
run the moment the F3 flag alone is flipped — proving the gate (not a
missing model or fixture) is what keeps Liga MX dark even though its
markets are OPEN. Flipping that flag in production requires the
operator approval path that does not exist for Liga MX; no code path in
this build can do it."""
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
    s.add(Competition(slug="liga-mx-2026", name="Liga MX", season=2026))
    s.commit()
    yield s
    event.remove(_Session, "before_flush", _enforce_varchar_lengths)
    s.close()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


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
        ko = datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
        fx = Fixture(competition_slug="liga-mx-2026",
                     espn_event_id="401877027",
                     home_team_id=pue.id, away_team_id=gdl.id,
                     current_kickoff_utc=ko, original_kickoff_utc=ko,
                     status="pre")
        ligamx_session.add(fx)
        ligamx_session.commit()

        et = "KXLIGAMXGAME-26JUL31PUECDG"
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
        ko = datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
        fx = Fixture(competition_slug="liga-mx-2026",
                     espn_event_id="401877027",
                     home_team_id=pue.id, away_team_id=gdl.id,
                     current_kickoff_utc=ko, original_kickoff_utc=ko,
                     status="pre")
        ligamx_session.add(fx)
        ligamx_session.commit()
        game_et = "KXLIGAMXGAME-26JUL31PUECDG"
        tot_et = "KXLIGAMXTOTAL-26JUL31PUECDG"
        spr_et = "KXLIGAMXSPREAD-26JUL31PUECDG"
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
        ko = datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
        ligamx_session.add(Fixture(
            competition_slug="liga-mx-2026", espn_event_id="401877027",
            home_team_id=pue.id, away_team_id=gdl.id,
            current_kickoff_utc=ko, original_kickoff_utc=ko,
            status="pre"))
        ligamx_session.commit()
        et = "KXLIGAMXGAME-26JUL31MAZPUE"
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

    def test_approval_status_reports_dark(self, ligamx_session):
        model_ligamx.ensure_model_version(approved_for_shadow=False)
        st = ligamx_plane.approval_status()
        assert st["mode"] == "dark"
        assert st["approved_for_shadow"] is False
        assert st["approval_decision_missing"] is True
        assert st["model_version_registered"] is True

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
