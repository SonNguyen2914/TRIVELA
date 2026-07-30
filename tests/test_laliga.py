"""La Liga surface: data layer, live-plane parameterization, dark-model
gates. All canned — the ESPN/Kalshi payloads come from the archived
2026-07-28 research probes (research_archive/laliga_*.json), so the
assertions are grounded in real provider bytes, not invented shapes.
No network anywhere."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from src import laliga, mls
from src.live import db as live_db
from src.live import (competitions, identity, laliga_live, markets,
                      model_laliga, runs)
from src.live.models import (Competition, Fixture, LiveBase, ModelVersion,
                             Team, TeamAlias)

UTC = timezone.utc
ARCHIVE = Path(__file__).resolve().parent.parent / "research_archive"


def _archived(name: str) -> dict:
    with open(ARCHIVE / name, encoding="utf-8") as fh:
        return json.load(fh)


# --- data layer: preseason standings guard --------------------------------

class TestPreseasonStandings:
    PAYLOAD = "laliga_espn_standings_preseason_2026-07-28.json"

    def test_control_naive_parser_fabricates_a_zero_table(self):
        """CONTROL, not evidence: the shared parser alone turns ESPN's
        preseason payload into a plausible 20-row ranked table of zeros
        — the exact fabricated surface the frontend rules forbid. This
        documents the failure mode the guard below exists to stop; if
        ESPN ever changes its preseason shape, this control goes red
        before the guard test silently loses its meaning."""
        tables = mls.parse_standings(_archived(self.PAYLOAD))
        rows = [r for t in tables for r in t["entries"]]
        assert len(rows) == 20
        assert all((r["played"] or 0) == 0 for r in rows)
        assert [r["rank"] for r in rows] == list(range(1, 21))

    def test_guard_suppresses_the_fabricated_table(self):
        out = laliga.standings_view(_archived(self.PAYLOAD))
        assert out == {"tables": [], "preseason": True}

    def test_real_numbers_pass_through(self):
        d = {"children": [{"name": "2026-27 LALIGA", "standings": {
            "entries": [
                {"team": {"id": "83", "displayName": "Barcelona",
                          "abbreviation": "BAR"},
                 "stats": [{"name": "rank", "value": 1},
                           {"name": "gamesPlayed", "value": 3},
                           {"name": "wins", "value": 3},
                           {"name": "losses", "value": 0},
                           {"name": "ties", "value": 0},
                           {"name": "points", "value": 9},
                           {"name": "pointsFor", "value": 7},
                           {"name": "pointsAgainst", "value": 1},
                           {"name": "pointDifferential", "value": 6}]},
                {"team": {"id": "86", "displayName": "Real Madrid",
                          "abbreviation": "RMA"},
                 "stats": [{"name": "rank", "value": 2},
                           {"name": "gamesPlayed", "value": 3},
                           {"name": "wins", "value": 2},
                           {"name": "losses", "value": 0},
                           {"name": "ties", "value": 1},
                           {"name": "points", "value": 7},
                           {"name": "pointsFor", "value": 5},
                           {"name": "pointsAgainst", "value": 2},
                           {"name": "pointDifferential", "value": 3}]},
            ]}}]}
        out = laliga.standings_view(d)
        assert out["preseason"] is False
        assert len(out["tables"]) == 1
        assert out["tables"][0]["entries"][0]["team"] == "Barcelona"

    def test_round_one_in_progress_is_not_preseason(self):
        """One completed match must already defeat the guard — points
        exist. The suppression can never hide a genuine matchday."""
        d = {"children": [{"name": "x", "standings": {"entries": [
            {"team": {"id": "1", "displayName": "A", "abbreviation": "A"},
             "stats": [{"name": "rank", "value": 1},
                       {"name": "gamesPlayed", "value": 1},
                       {"name": "wins", "value": 1},
                       {"name": "points", "value": 3},
                       {"name": "pointsFor", "value": 1}]},
            {"team": {"id": "2", "displayName": "B", "abbreviation": "B"},
             "stats": [{"name": "rank", "value": 2},
                       {"name": "gamesPlayed", "value": 0},
                       {"name": "wins", "value": 0},
                       {"name": "points", "value": 0},
                       {"name": "pointsFor", "value": 0}]},
        ]}}]}
        assert laliga.standings_view(d)["preseason"] is False


# --- data layer: the winner-first score trap exists in esp.1 too ----------

class TestWinnerFirstTrap:
    PAYLOAD = "laliga_espn_summary_pre_401882926_2026-07-28.json"

    def test_esp1_score_string_is_winner_first(self):
        """Ground the trap in the archived bytes: Alavés' 2-4 home loss
        to Athletic arrives as score='4-2' (winner-first), while the
        authoritative fields say home 2, away 4, atVs 'vs'."""
        d = _archived(self.PAYLOAD)
        games = d["lastFiveGames"][0]["events"]
        loss = next(g for g in games
                    if g["gameResult"] == "L"
                    and (g.get("opponent") or {}).get(
                        "abbreviation") == "ATH")
        assert loss["score"] == "4-2"          # rendered naively: a win
        assert loss["homeTeamScore"] == "2"
        assert loss["awayTeamScore"] == "4"
        assert loss["atVs"] == "vs"

    def test_parsed_scouting_derives_from_scores_not_the_string(self):
        d = _archived(self.PAYLOAD)
        parsed = mls.parse_summary(d)
        team = parsed["scouting"]["last_five"][0]
        loss = next(g for g in team["games"]
                    if g["opponent"] == "ATH" and g["result"] == "L")
        assert (loss["team_score"], loss["opponent_score"]) == (2, 4)
        # the letter agrees with the numbers shown beside it
        assert loss["team_score"] < loss["opponent_score"]

    def test_no_disagreement_on_archived_payload(self):
        assert mls.scoreline_disagreements(_archived(self.PAYLOAD)) == []


# --- data layer: encodings + fixtures -------------------------------------

class TestEncodingAndFixtures:
    def test_accented_names_survive_parsing_exactly(self):
        d = _archived("laliga_espn_scoreboard_preseason_2026-07-28.json")
        fixtures = [mls.parse_event(e) for e in d["events"]]
        names = {f["home"]["name"] for f in fixtures} \
            | {f["away"]["name"] for f in fixtures}
        assert "Alavés" in names               # é intact, not Alavés
        assert "Alavés" == "Alavés"       # composed form, exact
        sevilla = next(f for f in fixtures if f["home"]["name"] == "Sevilla")
        assert sevilla["venue"] == "Ramón Sánchez Pizjuán Stadium"
        # a Starlette-style dump must not ASCII-escape them
        assert "Alavés" in json.dumps(fixtures, ensure_ascii=False)

    def test_team_payload_has_20_clubs_with_promoted_sides(self):
        d = _archived("laliga_espn_teams_2026-27_2026-07-28.json")
        teams = [t["team"]["displayName"]
                 for t in d["sports"][0]["leagues"][0]["teams"]]
        assert len(teams) == 20
        for promoted in ("Deportivo La Coruña", "Málaga",
                         "Racing Santander"):
            assert promoted in teams


# --- data layer: cache isolation from the MLS module ----------------------

class TestCacheIsolation:
    def test_identical_keys_do_not_cross_leagues(self, monkeypatch):
        """Both modules key their standings cache "standings". If
        laliga ever shared mls's cache dict, this serves MLS tables on
        the La Liga page. Control first: prove the poisoned entry WOULD
        be served through mls's own path, then that laliga ignores it."""
        sentinel = {"tables": [{"conference": "EASTERN"}]}
        # Seed through mls's OWN writer rather than hand-building a cache
        # entry: the entry shape is private and has already widened once
        # ((mono, data) → (mono, wall, data) in the composed-freshness
        # fix), which broke this test while the product was fine.
        monkeypatch.setattr(mls, "_cache", {})
        assert mls._cached("standings", 300, lambda: sentinel) is sentinel
        assert mls._cached("standings", 300, lambda: None) is sentinel
        monkeypatch.setattr(
            laliga, "_get_json",
            lambda url, params=None: _archived(
                "laliga_espn_standings_preseason_2026-07-28.json"))
        monkeypatch.setattr(laliga, "_cache", {})
        out = laliga.standings()
        assert out == {"tables": [], "preseason": True}


# --- data layer: Kalshi matching on the verified vocabulary ---------------

class TestKalshiMatching:
    def _books(self):
        mk = [{"ticker": "T", "yes_sub_title": "x", "status": "active",
               "yes_ask_dollars": "0.5", "yes_bid_dollars": "0.4"}]
        return [
            {"event_ticker": "KXLALIGAGAME-26AUG15ALAGET",
             "title": "Alaves vs Getafe", "markets": mk},
            {"event_ticker": "KXLALIGAGAME-26AUG15ATHBAR",
             "title": "Bilbao vs Barcelona", "markets": mk},
            # the real anomaly from the archived slate — no " vs "
            {"event_ticker": "KXLALIGAGAME-26AUG15RMAX",
             "title": "Real Madrid: Game Winner?", "markets": mk},
        ]

    def test_accent_normalized_containment(self):
        b = laliga.find_book("2026-08-15T17:30Z", "Alavés", "Getafe",
                             books=self._books())
        assert b and b["event_ticker"] == "KXLALIGAGAME-26AUG15ALAGET"

    def test_bilbao_bridge(self):
        b = laliga.find_book("2026-08-15T19:30Z", "Athletic Club",
                             "Barcelona", books=self._books())
        assert b and b["event_ticker"] == "KXLALIGAGAME-26AUG15ATHBAR"

    def test_anomalous_title_is_skipped_never_guessed(self):
        assert laliga.find_book("2026-08-15T17:30Z", "Real Madrid",
                                "Getafe", books=self._books()) is None

    def test_wrong_date_no_match(self):
        assert laliga.find_book("2026-08-16T17:30Z", "Alavés", "Getafe",
                                books=self._books()) is None

    def test_model_keys_parse_laliga_series(self):
        mk = laliga.model_key_for
        codes = "ATHBAR"
        assert mk("KXLALIGATOTAL", "KXLALIGATOTAL-26AUG15ATHBAR-3",
                  codes) == "over_2_5"
        assert mk("KXLALIGABTTS", "KXLALIGABTTS-26AUG15ATHBAR-BTTS",
                  codes) == "btts"
        assert mk("KXLALIGASPREAD", "KXLALIGASPREAD-26AUG15ATHBAR-ATH2",
                  codes) == "home_margin_2"
        assert mk("KXLALIGASPREAD", "KXLALIGASPREAD-26AUG15ATHBAR-BAR2",
                  codes) == "away_margin_2"
        assert mk("KXLALIGATEAMTOTAL",
                  "KXLALIGATEAMTOTAL-26AUG15ATHBAR-BAR2",
                  codes) == "away_team_over_1_5"
        assert mk("KXLALIGASCORE", "KXLALIGASCORE-26AUG15ATHBAR-ATH2BAR1",
                  codes) == "score_2_1"
        assert mk("KXLALIGAFTTS", "KXLALIGAFTTS-26AUG15ATHBAR-ATH",
                  codes) == "home_first_goal"
        assert mk("KXLALIGAMOV", "KXLALIGAMOV-26AUG15ATHBAR-X",
                  codes) is None            # market-only family

    def test_series_names_come_from_config_prefix(self):
        assert laliga.KALSHI_GAME == \
            config.LALIGA_KALSHI_SERIES_PREFIX + "GAME"
        assert len(laliga.MATCH_FAMILIES) == 12


# --- live plane fixture ----------------------------------------------------

@pytest.fixture()
def dual_session(tmp_path, monkeypatch):
    """Throwaway live DB seeded with BOTH competitions, so cross-league
    scoping is testable. Same VARCHAR enforcement as the MLS suite."""
    from tests.test_mls_shadow import _enforce_varchar_lengths
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
    s.add(Competition(slug="la-liga-2026", name="La Liga", season=2026))
    s.commit()
    yield s
    event.remove(_Session, "before_flush", _enforce_varchar_lengths)
    s.close()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


CANNED_LALIGA_TEAMS = [
    {"id": 93, "displayName": "Athletic Club",
     "shortDisplayName": "Athletic", "abbreviation": "ATH"},
    {"id": 83, "displayName": "Barcelona",
     "shortDisplayName": "Barcelona", "abbreviation": "BAR"},
    {"id": 96, "displayName": "Alavés",
     "shortDisplayName": "Alavés", "abbreviation": "ALA"},
]


def _seed_laliga(with_kalshi=True):
    """Seeds through the SHARED competition-keyed entry point. Since the
    EPL build that is `seed_league_teams(slug, teams_url, bridges)` —
    `seed_teams()` is now the MLS-only alias that delegates to it — so
    this passes the same three inputs positionally. `teams_url` is
    unused while `espn_teams` is supplied: still no network."""
    return identity.seed_league_teams(
        "la-liga-2026",
        laliga_live.ESPN_TEAMS_URL,
        (laliga_live.KALSHI_BRIDGES if with_kalshi else {}),
        espn_teams=CANNED_LALIGA_TEAMS)


class TestIdentityParameterization:
    def test_seed_scopes_by_competition(self, dual_session):
        r = _seed_laliga()
        assert r["teams"] == 3
        s = dual_session
        assert s.query(Team).filter_by(
            competition_slug="la-liga-2026").count() == 3
        assert s.query(Team).filter_by(
            competition_slug="mls-2026").count() == 0

    def test_bilbao_bridge_attaches_to_athletic_club(self, dual_session):
        _seed_laliga()
        t = identity.resolve("kalshi", "Bilbao",
                             competition_slug="la-liga-2026")
        assert t is not None and t.canonical_name == "Athletic Club"

    def test_promoted_clubs_have_no_kalshi_alias(self, dual_session):
        """The three promoted clubs appear in no historical Kalshi
        event; their names must NOT be guessed into the bridge table."""
        for name in ("Deportivo", "Malaga", "Málaga", "Racing",
                     "Racing Santander", "Deportivo La Coruna"):
            assert name not in laliga_live.KALSHI_BRIDGES

    def test_alias_table_is_globally_unique_per_source(self, dual_session):
        """SCHEMA FINDING (encoded, not fought): team_alias is UNIQUE on
        (source, alias) across ALL competitions — two leagues cannot
        hold the same Kalshi title name. Today's vocabularies don't
        collide (checked below), and a future collision fails loudly at
        seed time rather than silently mis-bridging; the loser stays
        unmapped and the unmapped metric reports it."""
        from sqlalchemy.exc import IntegrityError
        s = dual_session
        a = Team(competition_slug="mls-2026", canonical_name="Atlanta",
                 abbrev="ATL", espn_id="1")
        b = Team(competition_slug="la-liga-2026", canonical_name="Betis",
                 abbrev="BET", espn_id="2")
        s.add_all([a, b])
        s.flush()
        s.add(TeamAlias(team_id=a.id, alias="Shared", source="kalshi",
                        approved=True))
        s.commit()
        s.add(TeamAlias(team_id=b.id, alias="Shared", source="kalshi",
                        approved=True))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # scoping still matters for RESOLUTION: the alias belongs to
        # MLS, so a la-liga-scoped resolve must return None, never the
        # other league's club
        assert identity.resolve(
            "kalshi", "Shared",
            competition_slug="mls-2026").canonical_name == "Atlanta"
        assert identity.resolve(
            "kalshi", "Shared", competition_slug="la-liga-2026") is None

    def test_no_alias_collision_between_mls_and_laliga_bridges(self):
        from src.live.identity import KALSHI_BRIDGES as MLS_BRIDGES
        overlap = set(MLS_BRIDGES) & set(laliga_live.KALSHI_BRIDGES)
        assert overlap == set()


class TestMarketConfig:
    """Where the competition's market grammar comes from. The lookup this
    class was written against (`markets.market_config_for(slug) -> dict`,
    raising ValueError on an unknown slug) was dropped in favour of EPL's
    parameterization, which splits the same job in two: the registry in
    `src/live/competitions.py` owns "is this slug known" and raises
    KeyError if not, and each league module builds its own
    `CompetitionMarketSpec`. The behaviours asserted below are the ones
    this class always asserted; only where they are read from moved."""

    def test_unknown_competition_fails_explicitly(self):
        # The stand-in unknown slug used to be "epl-2026" — EPL is now a
        # REGISTERED competition, so it can no longer play that role, and
        # the assertion below pins that it doesn't (a regression that
        # unregistered EPL would otherwise hide inside this test).
        assert competitions.spec("epl-2026")["label"] == "EPL"
        with pytest.raises(KeyError):
            competitions.spec("ligue-1-2026")

    def test_laliga_config_derives_from_league_module(self):
        # la-liga-2026 must be REGISTERED — an unregistered slug raises
        # here rather than silently resolving to MLS
        assert competitions.spec("la-liga-2026")["model_module"] == \
            "src.live.model_laliga"
        assert competitions.espn_league("la-liga-2026") == "esp.1"
        cfg = laliga_live.MARKET_SPEC
        assert cfg.game_series == "KXLALIGAGAME"
        assert len(cfg.family_series) == 12
        assert cfg.model_key_fn is laliga.model_key_for

    def test_mls_config_unchanged(self):
        cfg = markets.MLS_SPEC
        assert cfg.game_series == "KXMLSGAME"
        assert cfg.family_series == markets.FAMILY_SERIES

    def test_discover_maps_laliga_event_via_bridge(self, dual_session,
                                                   monkeypatch):
        _seed_laliga()
        s = dual_session
        ath = identity.resolve("kalshi", "Bilbao",
                               competition_slug="la-liga-2026")
        bar = identity.resolve("kalshi", "Barcelona",
                               competition_slug="la-liga-2026")
        s.add(Fixture(competition_slug="la-liga-2026",
                      espn_event_id="401882999",
                      home_team_id=ath.id, away_team_id=bar.id,
                      original_kickoff_utc=datetime(2026, 8, 15, 19, 30,
                                                    tzinfo=UTC),
                      current_kickoff_utc=datetime(2026, 8, 15, 19, 30,
                                                   tzinfo=UTC),
                      status="pre"))
        s.commit()

        def fake_paged(url, params, key, **kw):
            meta = kw.get("meta")
            if meta is not None:
                meta.update({"pages": 1, "complete": True,
                             "cap_reached": False})
            if key == "events" and params.get(
                    "series_ticker") == "KXLALIGAGAME":
                return [{"event_ticker": "KXLALIGAGAME-26AUG15ATHBAR",
                         "title": "Bilbao vs Barcelona"}]
            if key == "events":
                return []
            return [{"ticker": "KXLALIGAGAME-26AUG15ATHBAR-ATH",
                     "yes_sub_title": "Bilbao"},
                    {"ticker": "KXLALIGAGAME-26AUG15ATHBAR-BAR",
                     "yes_sub_title": "Barcelona"},
                    {"ticker": "KXLALIGAGAME-26AUG15ATHBAR-TIE",
                     "yes_sub_title": "Tie"}]
        monkeypatch.setattr(markets, "_kalshi_paged", fake_paged)
        out = markets.discover_and_map(spec=laliga_live.MARKET_SPEC)
        assert out["newly_mapped"] == 1
        assert out["discovery_complete"] is True
        from src.live.models import MarketContract, MarketEvent
        me = s.query(MarketEvent).filter_by(
            kalshi_event_ticker="KXLALIGAGAME-26AUG15ATHBAR").first()
        assert me.competition_slug == "la-liga-2026"
        assert me.mapping_approved and me.fixture_id is not None
        keys = {c.outcome_key for c in s.query(MarketContract)
                .filter_by(market_event_id=me.id)}
        assert keys == {"home_win", "away_win", "draw"}


def _completed_laliga_fixtures(s, n=12):
    """Two clubs alternating home/away, enough history for MIN_GAMES."""
    ath = s.query(Team).filter_by(competition_slug="la-liga-2026",
                                  abbrev="ATH").first()
    bar = s.query(Team).filter_by(competition_slug="la-liga-2026",
                                  abbrev="BAR").first()
    base = datetime.now(UTC) - timedelta(days=120)
    for i in range(n):
        home, away = (ath, bar) if i % 2 == 0 else (bar, ath)
        s.add(Fixture(competition_slug="la-liga-2026",
                      espn_event_id=f"7000{i:02d}",
                      home_team_id=home.id, away_team_id=away.id,
                      original_kickoff_utc=base + timedelta(days=7 * i),
                      current_kickoff_utc=base + timedelta(days=7 * i),
                      status="post", home_goals=(2 if i % 3 else 1),
                      away_goals=(0 if i % 2 else 1)))
    s.add(Fixture(competition_slug="la-liga-2026",
                  espn_event_id="401882926",
                  home_team_id=ath.id, away_team_id=bar.id,
                  original_kickoff_utc=datetime.now(UTC)
                  + timedelta(hours=30),
                  current_kickoff_utc=datetime.now(UTC)
                  + timedelta(hours=30),
                  status="pre"))
    s.commit()


class TestDarkModelGates:
    def test_default_flag_is_off(self):
        """LALIGA_SHADOW_ENABLED fails toward OFF — flipping nothing
        must produce a plane that does nothing."""
        assert config.LALIGA_SHADOW_ENABLED is False

    def test_runs_refused_while_flag_off(self, dual_session):
        assert laliga_live.scheduled_runs() == {"skipped": "off"}
        assert laliga_live.t10_locks() == {"skipped": "off"}

    def test_runs_refused_by_approval_gate_when_enabled(self, dual_session,
                                                        monkeypatch):
        """Flag on, history present, model fits — and the F3 gate still
        refuses because laliga-2026-v0 has no approved ModelVersion.
        THIS is the dark-model posture: enforced, not asserted."""
        monkeypatch.setattr(config, "LALIGA_SHADOW_ENABLED", True)
        _seed_laliga()
        _completed_laliga_fixtures(dual_session)
        assert model_laliga.current_model() is not None   # gate, not data
        out = laliga_live.scheduled_runs()
        assert out == {"skipped": "model not approved for shadow (F3 gate)"}

    def test_machinery_would_run_if_approved(self, dual_session,
                                             monkeypatch):
        """The prove-the-gate pair: identical setup plus an approved
        ModelVersion row produces a real run — so the refusal above is
        the gate, not a broken pipeline. (No code path on this branch
        creates such an approval; this test writes the row directly.)"""
        monkeypatch.setattr(config, "LALIGA_SHADOW_ENABLED", True)
        monkeypatch.setattr(config, "N_SIMULATIONS", 200)
        _seed_laliga()
        _completed_laliga_fixtures(dual_session)
        s = dual_session
        mv = ModelVersion(name=model_laliga.MODEL_NAME,
                          description="test-only approval",
                          created_at=datetime.now(UTC))
        mv.approved_for_shadow = True
        s.add(mv)
        s.commit()
        out = laliga_live.scheduled_runs()
        assert out.get("created") == 1
        from src.live.models import PredictionContract, PredictionRun
        run = (s.query(PredictionRun).join(
            Fixture, PredictionRun.fixture_id == Fixture.id)
            .filter(Fixture.competition_slug == "la-liga-2026").one())
        assert run.status == "complete" and run.canonical is False
        probs = {c.outcome_key: c.raw_probability
                 for c in s.query(PredictionContract)
                 .filter_by(prediction_run_id=run.id)
                 if c.outcome_key in ("home_win", "draw", "away_win")}
        assert abs(sum(probs.values()) - 1.0) < 0.011

    def test_t10_needs_an_approval_decision_too(self, dual_session,
                                                monkeypatch):
        """Even an approved ModelVersion cannot lock: canonical locks
        additionally require the immutable approval DECISION (F9),
        which nothing on this branch can create for La Liga."""
        monkeypatch.setattr(config, "LALIGA_SHADOW_ENABLED", True)
        _seed_laliga()
        _completed_laliga_fixtures(dual_session)
        s = dual_session
        mv = ModelVersion(name=model_laliga.MODEL_NAME,
                          description="test-only approval",
                          created_at=datetime.now(UTC))
        mv.approved_for_shadow = True
        s.add(mv)
        s.commit()
        out = laliga_live.t10_locks()
        assert out == {"skipped":
                       "no approved model-approval decision (F9)"}

    def test_seed_domain_is_league_distinct(self):
        from types import SimpleNamespace
        fx = SimpleNamespace(espn_event_id="401882926")
        from src.live import model_mls
        assert model_laliga.seed_for(fx, "scheduled") != \
            model_mls.seed_for(fx, "scheduled")
        assert 0 <= model_laliga.seed_for(fx, "scheduled") <= 0x7FFFFFFF

    def test_engine_signature_hashes_laliga_source(self):
        sig = model_laliga.engine_signature()
        assert sig["source_sha256"]["src.live.model_laliga"] is not None
        from src.live import model_mls
        assert sig["signature_hash"] != \
            model_mls.engine_signature()["signature_hash"]


class TestShadowCountScoping:
    def test_counts_do_not_bleed_between_competitions(self, dual_session,
                                                      monkeypatch):
        """PROVEN-TO-FAIL against the unparameterized runs.py: the
        original shadow_counts counted runs/events GLOBALLY, so a
        second league's rows inflated MLS readiness (verified red by
        stashing the runs.py change — see the branch report)."""
        monkeypatch.setattr(config, "LALIGA_SHADOW_ENABLED", True)
        monkeypatch.setattr(config, "N_SIMULATIONS", 200)
        _seed_laliga()
        _completed_laliga_fixtures(dual_session)
        s = dual_session
        mv = ModelVersion(name=model_laliga.MODEL_NAME,
                          description="test-only approval",
                          created_at=datetime.now(UTC))
        mv.approved_for_shadow = True
        s.add(mv)
        s.commit()
        assert laliga_live.scheduled_runs().get("created") == 1
        mls_counts = runs.shadow_counts()                  # default = MLS
        assert mls_counts["complete_runs"] == 0
        assert mls_counts["fixtures"] == 0
        # the La Liga spec carries the slug, the model module and the
        # expected-teams count that used to be three separate kwargs
        assert laliga_live.RUNS_SPEC.model is model_laliga
        assert laliga_live.RUNS_SPEC.expected_teams == \
            laliga_live.EXPECTED_TEAMS
        la = runs.shadow_counts(spec=laliga_live.RUNS_SPEC)
        assert la["complete_runs"] == 1
        assert la["fixtures"] == 13
        assert "teams 3/20" in la["blockers"]

    def test_status_declares_dark_model_and_unverified_coverage(
            self, dual_session, monkeypatch):
        monkeypatch.setattr(
            laliga, "series_probe",
            lambda: {"series": "KXLALIGAGAME", "series_exists": True,
                     "open_events": 0, "coverage_verified": False})
        out = laliga_live.shadow_status()
        assert out["model_dark"] is True
        assert out["xg_source"] is None
        assert out["kalshi"]["coverage_verified"] is False
        assert out["counts"]["shadow_ready"] is False
        # dark is DERIVED here, so it must agree with the approval facts
        # printed beneath it in the same payload
        assert out["counts"]["approval_decision_present"] is False
        assert "no approval decision exists" in out["model_dark_note"]

    def test_status_model_dark_is_derived_not_asserted(
            self, dual_session, monkeypatch):
        """La Liga's copy of the 2026-07-30 /api/ligamx/status defect:
        model_dark and model_dark_note were hardcoded to the dark state,
        so an approved La Liga model would have been reported dark right
        beside counts.approval_decision_present=true in the SAME dict.
        Latent when found (La Liga is genuinely unapproved) — fixed with
        Liga MX's live instance rather than left to fire later."""
        import hashlib as _h

        from src.live import model_laliga
        from src.live.models import ModelApprovalDecision, ModelVersion

        monkeypatch.setattr(
            laliga, "series_probe",
            lambda: {"series": "KXLALIGAGAME", "series_exists": True,
                     "open_events": 0, "coverage_verified": False})

        model_laliga.ensure_model_version()
        mv = dual_session.query(ModelVersion).filter_by(
            name=model_laliga.MODEL_NAME).one()
        mv.approved_for_shadow = True
        doc = '{"model":"laliga-2026-v0","note":"test-only approval"}'
        dec = ModelApprovalDecision(
            model_version_id=mv.id,
            model_version_name=model_laliga.MODEL_NAME,
            approved_mode="shadow", approved=True,
            policy_version="shadow-approval-replay-v1",
            decision_document=doc,
            content_hash=_h.sha256(doc.encode()).hexdigest())
        dual_session.add(dec)
        dual_session.commit()

        out = laliga_live.shadow_status()
        assert out["counts"]["approval_decision_present"] is True
        assert out["counts"]["model_approved_for_shadow"] is True
        assert out["model_dark"] is False
        assert "no approval decision exists" not in out["model_dark_note"]
        assert "approved model-approval decision" in out["model_dark_note"]


class TestEndpoints:
    def test_laliga_routes_registered_and_get_only(self):
        from api.main import app
        paths = {r.path for r in app.routes}
        for p in ("/api/laliga/scoreboard", "/api/laliga/schedule",
                  "/api/laliga/standings", "/api/laliga/markets",
                  "/api/laliga/match/{event_id}", "/api/laliga/odds",
                  "/api/laliga/status"):
            assert p in paths
        for r in app.routes:
            if str(getattr(r, "path", "")).startswith("/api/laliga"):
                assert set(r.methods) == {"GET"}
        assert "/api/admin/laliga/sweep" in paths

    def test_match_routes_are_not_spliced_into_each_other(self, monkeypatch):
        """REGRESSION (this rebase): the EPL and La Liga route blocks were
        interleaved rather than concatenated, and the seam fell INSIDE
        both match handlers — epl_match lost its return and answered
        `null`, laliga_match inherited EPL's body and asked the EPL plane
        for a La Liga event. Nothing failed: route registration does not
        need a body, so a production endpoint returning null stayed
        green. This exercises both handlers past the seam.

        Each league's handler must reach ITS OWN league's return shape:
        La Liga's carries `lineups: None` and no `book_match` (no Sportec
        equivalent exists for esp.1), EPL's carries `book_match`."""
        from fastapi.testclient import TestClient

        from api.main import app
        from src import epl
        summary = {"date": "2026-08-15T19:30Z",
                   "home": {"name": "Athletic Club"},
                   "away": {"name": "Barcelona"}}
        monkeypatch.setattr(laliga, "match_summary", lambda eid: summary)
        monkeypatch.setattr(epl, "match_summary", lambda eid: summary)
        c = TestClient(app)

        la = c.get("/api/laliga/match/401882926")
        assert la.status_code == 200
        la_body = la.json()
        assert la_body is not None            # the truncation symptom
        assert la_body["match"] == summary
        assert la_body["lineups"] is None
        assert "book_match" not in la_body    # EPL's key, not La Liga's

        ep = c.get("/api/epl/match/401882926")
        assert ep.status_code == 200
        ep_body = ep.json()
        assert ep_body is not None            # the truncation symptom
        assert ep_body["match"] == summary
        assert "book_match" in ep_body

    def test_standings_route_serves_the_explicit_empty_state(
            self, monkeypatch):
        from fastapi.testclient import TestClient

        from api.main import app
        monkeypatch.setattr(
            laliga, "standings",
            lambda: {"tables": [], "preseason": True})
        c = TestClient(app)
        r = c.get("/api/laliga/standings")
        assert r.status_code == 200
        body = r.json()
        assert body["tables"] == [] and body["preseason"] is True

    def test_odds_route_is_shadow_labeled_and_dark(self, monkeypatch):
        from fastapi.testclient import TestClient

        from api.main import app
        c = TestClient(app)
        r = c.get("/api/laliga/odds")
        assert r.status_code == 200
        body = r.json()
        assert body["odds"] == []              # dormant plane: honest empty
        assert body["shadow"] is True
        assert body["model_dark"] is True
        assert body["real_money_signals"] is False

    def test_admin_sweep_requires_operator_token(self, monkeypatch):
        from fastapi.testclient import TestClient

        from api.main import app
        monkeypatch.setattr(config, "PUBLIC_READ_ONLY", True)
        monkeypatch.setattr(config, "ADMIN_TOKEN", "secret-token")
        c = TestClient(app)
        assert c.post("/api/admin/laliga/sweep").status_code == 403

    def test_accented_names_not_ascii_escaped_on_the_wire(
            self, monkeypatch):
        from fastapi.testclient import TestClient

        from api.main import app
        d = _archived("laliga_espn_scoreboard_preseason_2026-07-28.json")
        fixtures = [mls.parse_event(e) for e in d["events"]]
        monkeypatch.setattr(laliga, "scoreboard", lambda date=None: fixtures)
        c = TestClient(app)
        r = c.get("/api/laliga/scoreboard")
        assert "Alavés".encode("utf-8") in r.content
        assert b"Alav\\u00e9s" not in r.content
