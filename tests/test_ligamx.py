"""Liga MX data-layer parsers + fixture/book matching (canned payloads —
no network in tests). Shapes are reduced from the archived REAL
responses in research_archive/ligamx_*_2026-07-29.json; the Kalshi
tickers and titles below are the live open Apertura events, not
hypotheticals."""
from src import ligamx

# --- standings: one table PER TOURNAMENT (the split-season rule) -----------
# Reduced from ligamx_espn_standings_2026-07-29.json: one child per
# tournament, named with the tournament ("2026 Torneo Apertura"),
# standings.name "overall", 18 complete rows.


def _entry(team, abbrev, rank, played, points, wins=0, gd=0):
    return {"team": {"id": abbrev, "displayName": team,
                     "abbreviation": abbrev},
            "stats": [{"name": "rank", "value": rank},
                      {"name": "gamesPlayed", "value": played},
                      {"name": "points", "value": points},
                      {"name": "wins", "value": wins},
                      {"name": "pointDifferential", "value": gd}]}


_APERTURA = {"name": "2026 Torneo Apertura", "standings": {
    "name": "overall", "seasonDisplayName": "2026-27 Liga BBVA MX",
    "entries": [
        _entry("Tijuana", "TIJ", 1, 2, 6, wins=2, gd=4),
        _entry("Cruz Azul", "CAZ", 2, 2, 6, wins=2, gd=3),
        _entry("América", "AME", 3, 2, 4, wins=1, gd=2),
    ]}}

# what a Clausura preseason child will look like (the EPL zero-row
# lesson: COMPLETE rows, every stat 0, rank assigned alphabetically)
_CLAUSURA_PRESEASON = {"name": "2027 Torneo Clausura", "standings": {
    "name": "overall", "entries": [
        _entry("América", "AME", 1, 0, 0),
        _entry("Atlas", "ATS", 2, 0, 0),
        _entry("Cruz Azul", "CAZ", 3, 0, 0),
    ]}}


class TestStandingsSplitSeason:
    def test_tables_carry_the_tournament_label(self):
        out = ligamx.parse_ligamx_standings({"children": [_APERTURA]})
        assert len(out) == 1
        t = out[0]
        assert t["table"] == "2026 Torneo Apertura"
        assert t["tournament"] == "2026 Torneo Apertura"
        assert [e["team"] for e in t["entries"]] == \
            ["Tijuana", "Cruz Azul", "América"]
        assert [e["rank"] for e in t["entries"]] == [1, 2, 3]

    def test_zero_row_tournament_is_dropped_not_fabricated(self):
        """A Clausura that has not kicked off arrives as complete
        all-zero rows ranked alphabetically. Rendering it would crown a
        club out of zero information. (Proven to fail: with the
        zero-row guard removed this returns TWO tables, the second
        putting América first in a tournament that has not started.)"""
        out = ligamx.parse_ligamx_standings(
            {"children": [_APERTURA, _CLAUSURA_PRESEASON]})
        assert len(out) == 1
        assert out[0]["tournament"] == "2026 Torneo Apertura"

    def test_tournaments_are_never_merged(self):
        """Apertura and Clausura are separate competitions with
        separate tables. Two played children must emit two tables in
        provider order — never one combined order nobody publishes."""
        clausura = {"name": "2027 Torneo Clausura", "standings": {
            "name": "overall", "entries": [
                _entry("Atlas", "ATS", 1, 3, 9, wins=3, gd=5),
                _entry("Tijuana", "TIJ", 2, 3, 4, wins=1, gd=0),
            ]}}
        out = ligamx.parse_ligamx_standings(
            {"children": [_APERTURA, clausura]})
        assert [t["tournament"] for t in out] == \
            ["2026 Torneo Apertura", "2027 Torneo Clausura"]
        # the same club appears in BOTH tables — per-tournament, by design
        assert "Tijuana" in [e["team"] for e in out[0]["entries"]]
        assert "Tijuana" in [e["team"] for e in out[1]["entries"]]

    def test_no_children_is_empty(self):
        assert ligamx.parse_ligamx_standings({}) == []
        assert ligamx.parse_ligamx_standings({"children": []}) == []

    def test_mixed_zero_and_played_rows_keep_the_table(self):
        """One completed matchday must produce a table even while some
        clubs still have 0 played (postponements)."""
        mixed = {"children": [{"name": "2026 Torneo Apertura",
                               "standings": {"entries": [
                                   _entry("Toluca", "TOL", 1, 1, 3,
                                          wins=1, gd=2),
                                   _entry("Puebla", "PUE", 2, 0, 0),
                               ]}}]}
        out = ligamx.parse_ligamx_standings(mixed)
        assert len(out) == 1
        assert [e["team"] for e in out[0]["entries"]] == \
            ["Toluca", "Puebla"]


# --- the tournament, derived from provider data ----------------------------
# Shape from ligamx_espn_scoreboard_2026-07-29.json.

_SCOREBOARD = {
    "leagues": [{"name": "Mexican Liga BBVA MX", "slug": "mex.1",
                 "season": {"year": 2026,
                            "displayName": "2026-27 Liga BBVA MX",
                            "type": {"id": "1", "type": 14277,
                                     "name": "Torneo Apertura",
                                     "abbreviation":
                                     "2026 Liga MX Apertura"}}}],
    "events": [{
        "id": "401877027", "date": "2026-08-01T01:00Z",
        "season": {"year": 2026, "type": 14277,
                   "slug": "torneo-apertura"},
        "competitions": [{
            "venue": {"fullName": "Estadio Cuauhtémoc"},
            "competitors": [
                {"homeAway": "home", "score": "0",
                 "team": {"displayName": "Puebla", "abbreviation": "PUE"}},
                {"homeAway": "away", "score": "0",
                 "team": {"displayName": "Guadalajara",
                          "abbreviation": "GDL"}},
            ]}],
        "status": {"type": {"state": "pre", "shortDetail": "8/1"}},
    }],
}


class TestTournamentDerivation:
    def test_current_tournament_parsed_from_leagues_block(self):
        t = ligamx.parse_current_tournament(_SCOREBOARD)
        assert t == {"name": "Torneo Apertura",
                     "label": "2026 Liga MX Apertura",
                     "season_display": "2026-27 Liga BBVA MX"}

    def test_missing_season_type_is_none_not_a_guess(self):
        assert ligamx.parse_current_tournament({}) is None
        assert ligamx.parse_current_tournament(
            {"leagues": [{"season": {}}]}) is None

    def test_event_cards_carry_the_tournament(self):
        card = ligamx.event_card(_SCOREBOARD["events"][0])
        assert card["tournament"] == "torneo-apertura"
        assert card["home"]["name"] == "Puebla"
        assert card["venue"] == "Estadio Cuauhtémoc"


# --- fixture <-> Kalshi book matching --------------------------------------
# Tickers + titles are the LIVE open events of 2026-07-29
# (ligamx_kalshi_events_open_2026-07-29.json), and the ESPN names are
# the real mex.1 displayNames (accents included).

def _book(ticker, title, markets=None):
    return {"event_ticker": ticker, "title": title,
            "markets": markets if markets is not None else [
                {"ticker": f"{ticker}-X", "label": "x",
                 "yes_ask": "0.50", "yes_bid": "0.45", "status": "active"}]}


class TestFindBook:
    def test_live_verified_grammar_matches(self):
        """The real PUECDG open event: ESPN kickoff 2026-08-01T01:00Z is
        Jul 31 US-Eastern, so the ET date join is exercised for real."""
        books = [_book("KXLIGAMXGAME-26JUL31PUECDG",
                       "Puebla vs Guadalajara")]
        got = ligamx.find_book("2026-08-01T01:00Z", "Puebla",
                               "Guadalajara", books=books)
        assert got is not None
        assert got["event_ticker"] == "KXLIGAMXGAME-26JUL31PUECDG"

    def test_accented_espn_names_cross_ascii_kalshi_titles(self):
        """Kalshi titles are ASCII; ESPN names carry accents. The
        normalizer must bridge América and Querétaro without aliases."""
        books = [_book("KXLIGAMXGAME-26AUG02AMESLA",
                       "America vs Santos Laguna"),
                 _book("KXLIGAMXGAME-26AUG01QUETIG",
                       "Queretaro vs Tigres")]
        assert ligamx.find_book("2026-08-02T23:00Z", "América",
                                "Santos", books=books) is not None
        assert ligamx.find_book("2026-08-02T01:00Z", "Querétaro",
                                "Tigres UANL", books=books) is not None

    def test_alias_bridges_the_long_forms(self):
        """'Tijuana de Caliente' and 'Santos Laguna' are the two open-
        event forms substring matching cannot cross (ESPN says 'Tijuana'
        and 'Santos'). (Proven to fail: with _KALSHI_ALIASES emptied the
        ASLTIJ match returns None.)"""
        books = [_book("KXLIGAMXGAME-26JUL31ASLTIJ",
                       "San Luis vs Tijuana de Caliente")]
        got = ligamx.find_book("2026-08-01T03:00Z",
                               "Atlético de San Luis", "Tijuana",
                               books=books)
        assert got is not None

    def test_date_disambiguates_repeat_pairings(self):
        books = [_book("KXLIGAMXGAME-26JUL31PUECDG",
                       "Puebla vs Guadalajara"),
                 _book("KXLIGAMXGAME-26NOV07PUECDG",
                       "Puebla vs Guadalajara")]
        got = ligamx.find_book("2026-11-08T01:00Z", "Puebla",
                               "Guadalajara", books=books)
        assert got is not None
        assert got["event_ticker"] == "KXLIGAMXGAME-26NOV07PUECDG"

    def test_game_winner_titles_are_skipped_not_guessed(self):
        books = [_book("KXLIGAMXGAME-26JUL31PUECDG",
                       "Puebla: Game Winner?")]
        assert ligamx.find_book("2026-08-01T01:00Z", "Puebla",
                                "Guadalajara", books=books) is None

    def test_no_match_returns_none(self):
        books = [_book("KXLIGAMXGAME-26JUL31PUECDG",
                       "Puebla vs Guadalajara")]
        assert ligamx.find_book("2026-08-01T01:00Z", "Toluca",
                                "Necaxa", books=books) is None

    def test_wrong_side_order_does_not_match(self):
        books = [_book("KXLIGAMXGAME-26JUL31PUECDG",
                       "Puebla vs Guadalajara")]
        assert ligamx.find_book("2026-08-01T01:00Z", "Guadalajara",
                                "Puebla", books=books) is None

    def test_relegated_mazatlan_has_no_alias(self):
        """Mazatlán appeared in 25/26 Kalshi titles but is not an ESPN
        mex.1 club; its name must NOT be bridged anywhere."""
        assert "mazatlan" not in ligamx._KALSHI_ALIASES
        assert not any("mazatlan" in k for k in ligamx._KALSHI_ALIASES)


# --- ticker-tail -> model key (grammar VERIFIED LIVE 2026-07-29 against
# the open PUECDG family books — every ticker below except the
# defensive-unparseable case is a real market that was trading) -------------

class TestModelKeys:
    SUFFIX = "PUECDG"        # home-first: Puebla home, Guadalajara away

    def test_totals_ladder(self):
        # live label check: tail 2 was titled "Over 1.5 goals scored"
        assert ligamx.model_key_for("KXLIGAMXTOTAL",
                                    "KXLIGAMXTOTAL-26JUL31PUECDG-2",
                                    self.SUFFIX) == "over_1_5"
        assert ligamx.model_key_for("KXLIGAMXTOTAL",
                                    "KXLIGAMXTOTAL-26JUL31PUECDG-6",
                                    self.SUFFIX) == "over_5_5"

    def test_btts(self):
        assert ligamx.model_key_for("KXLIGAMXBTTS",
                                    "KXLIGAMXBTTS-26JUL31PUECDG-BTTS",
                                    self.SUFFIX) == "btts"

    def test_spread_sides(self):
        # live labels: PUE2 "Puebla wins by more than 1.5 goals"
        assert ligamx.model_key_for("KXLIGAMXSPREAD",
                                    "KXLIGAMXSPREAD-26JUL31PUECDG-PUE2",
                                    self.SUFFIX) == "home_margin_2"
        assert ligamx.model_key_for("KXLIGAMXSPREAD",
                                    "KXLIGAMXSPREAD-26JUL31PUECDG-CDG3",
                                    self.SUFFIX) == "away_margin_3"

    def test_team_total_and_score(self):
        assert ligamx.model_key_for(
            "KXLIGAMXTEAMTOTAL", "KXLIGAMXTEAMTOTAL-26JUL31PUECDG-CDG2",
            self.SUFFIX) == "away_team_over_1_5"
        # live label: PUE1CDG2 "CD Guadalajara wins 2-1" (home-first)
        assert ligamx.model_key_for(
            "KXLIGAMXSCORE", "KXLIGAMXSCORE-26JUL31PUECDG-PUE1CDG2",
            self.SUFFIX) == "score_1_2"

    def test_first_team_to_score(self):
        assert ligamx.model_key_for("KXLIGAMXFTTS",
                                    "KXLIGAMXFTTS-26JUL31PUECDG-CDG",
                                    self.SUFFIX) == "away_first_goal"
        assert ligamx.model_key_for("KXLIGAMXFTTS",
                                    "KXLIGAMXFTTS-26JUL31PUECDG-NONE",
                                    self.SUFFIX) == "no_goal"

    def test_unmodeled_and_unparseable_are_market_only(self):
        assert ligamx.model_key_for("KXLIGAMX1H",
                                    "KXLIGAMX1H-26JUL31PUECDG-PUE",
                                    self.SUFFIX) is None
        assert ligamx.model_key_for("KXLIGAMXMOV",
                                    "KXLIGAMXMOV-26JUL31PUECDG-XYZ",
                                    self.SUFFIX) is None
        assert ligamx.model_key_for("KXLIGAMXSCORE",
                                    "KXLIGAMXSCORE-26JUL31PUECDG-OTHER",
                                    self.SUFFIX) is None


# --- summary parsing rides the shared (drift-hardened) parser --------------

class TestSummaryReuse:
    def test_mex1_summary_parses_with_derived_letters_and_accents(self):
        """Reduced from ligamx_espn_summary_401877031 (Guadalajara 1-0
        FC Juarez): accented names must survive parsing untouched, the
        W/L letter must derive from the scores (winner-first trap), and
        the shared audit must cover mex.1 payloads unchanged."""
        d = {
            "header": {"id": "401877031", "competitions": [{
                "date": "2026-07-25T23:07Z",
                "status": {"type": {"state": "post",
                                    "shortDetail": "FT"}},
                "competitors": [
                    {"homeAway": "home", "score": "1",
                     "team": {"id": "219", "displayName": "Guadalajara",
                              "abbreviation": "GDL"}},
                    {"homeAway": "away", "score": "0",
                     "team": {"id": "17851", "displayName": "FC Juarez",
                              "abbreviation": "JUA"}},
                ]}]},
            "lastFiveGames": [{
                "team": {"displayName": "Atlético de San Luis",
                         "abbreviation": "ASL"},
                "events": [{
                    # winner-first provider string says "2-0"; the team
                    # LOST 0-2 away. The letter must derive from the
                    # authoritative fields, not the string.
                    "homeTeamScore": "2", "awayTeamScore": "0",
                    "atVs": "@", "score": "2-0", "gameResult": "L",
                    "opponent": {"abbreviation": "AME"},
                    "gameDate": "2026-07-19T01:00Z"}],
            }],
        }
        from src.mls import parse_summary
        parsed = parse_summary(d)
        assert parsed["home"]["name"] == "Guadalajara"
        team = parsed["scouting"]["last_five"][0]
        assert team["team"] == "Atlético de San Luis"   # accents intact
        g = team["games"][0]
        assert (g["team_score"], g["opponent_score"]) == (0, 2)
        assert g["result"] == "L"
        assert ligamx.scoreline_disagreements(d) == []


# --- API surface -----------------------------------------------------------

class TestLigamxRoutes:
    def test_routes_registered_and_read_only(self):
        from api.main import app
        paths = {r.path for r in app.routes}
        for p in ("/api/ligamx/scoreboard", "/api/ligamx/schedule",
                  "/api/ligamx/standings", "/api/ligamx/markets",
                  "/api/ligamx/markets/discovery", "/api/ligamx/odds",
                  "/api/ligamx/approval", "/api/ligamx/status",
                  "/api/ligamx/match/{event_id}"):
            assert p in paths
        for r in app.routes:
            if str(getattr(r, "path", "")).startswith("/api/ligamx"):
                # READ-ONLY surface: no Liga MX mutation exists here
                assert set(r.methods) == {"GET"}

    def test_odds_board_reports_dark_empty(self):
        """With the live plane dormant the board must be an explicit
        empty carrying the shadow + dark flags — never a zero-forecast."""
        from fastapi.testclient import TestClient
        from api.main import app
        with TestClient(app) as c:
            d = c.get("/api/ligamx/odds").json()
        assert d["odds"] == []
        assert d["shadow"] is True and d["model_dark"] is True
        assert d["real_money_signals"] is False

    def test_approval_route_reports_dark(self):
        from fastapi.testclient import TestClient
        from api.main import app
        with TestClient(app) as c:
            d = c.get("/api/ligamx/approval").json()
        assert d["mode"] == "dark"
        assert d["approved_for_shadow"] is False
        assert d["approval_decision_missing"] is True

    def test_status_states_goals_only_and_the_tournament(self, monkeypatch):
        """The status page must say IN WORDS that the model is dark and
        goals-only, and must carry the tournament derived from data
        (canned here — no network in tests)."""
        from fastapi.testclient import TestClient
        from api.main import app
        monkeypatch.setattr(
            ligamx, "current_tournament",
            lambda: {"name": "Torneo Apertura",
                     "label": "2026 Liga MX Apertura",
                     "season_display": "2026-27 Liga BBVA MX"})
        with TestClient(app) as c:
            d = c.get("/api/ligamx/status").json()
        assert d["competition"] == "liga-mx-2026"
        assert d["tournament"]["name"] == "Torneo Apertura"
        assert d["model"]["mode"] == "dark"
        assert d["model"]["goals_only"] is True
        assert d["model"]["xg_source"] is None
        assert d["shadow_enabled"] is False      # the default-off plane
        assert d["real_money_signals"] is False
