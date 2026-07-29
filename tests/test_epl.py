"""EPL data-layer parsers + fixture/book matching (canned payloads — no
network in tests). Shapes are reduced from the archived real responses
in research_archive/epl/ (fetched 2026-07-28)."""
import json
import os
from datetime import date, timedelta

from src import epl

ARCHIVE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "research_archive", "epl")


def _archived_game_events() -> list[dict]:
    """The REAL committed KXEPLGAME event list (387 events, 2025-26).
    Read from the archive rather than reduced, because the whole point of
    the horizon bound is what a full season of history costs."""
    with open(os.path.join(
            ARCHIVE,
            "kalshi_events_KXEPLGAME_full_2026-07-28T1015Z.json"),
            encoding="utf-8") as fh:
        return json.load(fh)["events"]


def _ticker_for(day: date, codes: str = "ARSCHE") -> str:
    return f"KXEPLGAME-{day.strftime('%y%b%d').upper()}{codes}"

# --- standings: the preseason zero-row trap --------------------------------
# Reduced from espn_standings_2026-07-28T1015Z.json: ESPN's preseason
# answer is one child with COMPLETE rows, every stat 0.0, and rank
# assigned alphabetically. Rendering it would assert an order out of
# zero information.


def _entry(team, abbrev, rank, played, points, wins=0, gd=0):
    return {"team": {"id": abbrev, "displayName": team,
                     "abbreviation": abbrev},
            "stats": [{"name": "rank", "value": rank},
                      {"name": "gamesPlayed", "value": played},
                      {"name": "points", "value": points},
                      {"name": "wins", "value": wins},
                      {"name": "pointDifferential", "value": gd}]}


_PRESEASON = {"children": [{
    "name": "2026-27 English Premier League",
    "standings": {"name": "overall", "entries": [
        _entry("AFC Bournemouth", "BOU", 1, 0, 0),
        _entry("Arsenal", "ARS", 2, 0, 0),
        _entry("Aston Villa", "AVL", 3, 0, 0),
    ]}}]}

_IN_SEASON = {"children": [{
    "name": "2026-27 English Premier League",
    "standings": {"name": "overall", "entries": [
        _entry("Arsenal", "ARS", 2, 3, 7, wins=2, gd=4),
        _entry("Liverpool", "LIV", 1, 3, 9, wins=3, gd=6),
        _entry("Everton", "EVE", 3, 3, 4, wins=1, gd=0),
    ]}}]}


class TestStandingsHonesty:
    def test_preseason_all_zero_rows_yield_an_explicit_empty(self):
        """ESPN's alphabetical zero-table must NOT become a standings
        table. (Proven to fail: with the zero-row guard removed this
        returns a 3-row table crowning AFC Bournemouth.)"""
        assert epl.parse_epl_standings(_PRESEASON) == []

    def test_no_children_is_empty(self):
        assert epl.parse_epl_standings({}) == []
        assert epl.parse_epl_standings({"children": []}) == []

    def test_a_real_table_renders_once_games_exist(self):
        out = epl.parse_epl_standings(_IN_SEASON)
        assert len(out) == 1
        t = out[0]
        assert t["table"] == "2026-27 English Premier League"
        assert [e["team"] for e in t["entries"]] == \
            ["Liverpool", "Arsenal", "Everton"]
        assert [e["rank"] for e in t["entries"]] == [1, 2, 3]

    def test_single_league_table_never_splits(self):
        """The EPL has no conferences: even if ESPN ever ships multiple
        children, clubs collapse to one freshest-row table."""
        doubled = {"children": [
            _PRESEASON["children"][0],
            _IN_SEASON["children"][0],
        ]}
        out = epl.parse_epl_standings(doubled)
        assert len(out) == 1
        names = [e["team"] for e in out[0]["entries"]]
        assert len(names) == len(set(names))
        # zero-played duplicates lost to the freshest rows; clubs with
        # no played games at all carry no information and are dropped
        # with the season-not-started rule applied per club? No —
        # played rows exist, so the table renders, and the zero-row
        # clubs keep their (zero) freshest rows ranked below.
        assert "Liverpool" in names

    def test_mixed_zero_and_played_rows_keep_the_table(self):
        """One completed matchday must produce a table even while some
        clubs still have 0 played (postponements)."""
        mixed = {"children": [{
            "name": "EPL", "standings": {"entries": [
                _entry("Arsenal", "ARS", 1, 1, 3, wins=1, gd=2),
                _entry("Hull City", "HUL", 2, 0, 0),
            ]}}]}
        out = epl.parse_epl_standings(mixed)
        assert len(out) == 1
        assert [e["team"] for e in out[0]["entries"]] == \
            ["Arsenal", "Hull City"]


# --- fixture <-> Kalshi book matching --------------------------------------
# Ticker + title shapes verified against the archived 25/26 season
# (kalshi_events_KXEPLGAME_full_2026-07-28T1015Z.json).

def _book(ticker, title, markets=None):
    return {"event_ticker": ticker, "title": title,
            "markets": markets if markets is not None else [
                {"ticker": f"{ticker}-X", "label": "x",
                 "yes_ask": "0.50", "yes_bid": "0.45", "status": "active"}]}


class TestFindBook:
    def test_verified_grammar_matches(self):
        books = [_book("KXEPLGAME-26MAY24WHULEE",
                       "West Ham vs Leeds United")]
        got = epl.find_book("2026-05-24T14:00Z", "West Ham United",
                            "Leeds United", books=books)
        assert got is not None
        assert got["event_ticker"] == "KXEPLGAME-26MAY24WHULEE"

    def test_man_utd_alias_bridges(self):
        """'Man Utd' is the one 25/26 title side substring matching
        cannot cross (research_archive/epl/). (Proven to fail: with
        _KALSHI_ALIASES emptied this returns None.)"""
        books = [_book("KXEPLGAME-26AUG22MUNCHE",
                       "Man Utd vs Chelsea")]
        got = epl.find_book("2026-08-22T16:30Z", "Manchester United",
                            "Chelsea", books=books)
        assert got is not None

    def test_date_disambiguates(self):
        books = [_book("KXEPLGAME-26AUG22ARSCOV", "Arsenal vs Coventry"),
                 _book("KXEPLGAME-26DEC26ARSCOV", "Arsenal vs Coventry")]
        got = epl.find_book("2026-12-26T15:00Z", "Arsenal",
                            "Coventry City", books=books)
        assert got is not None
        assert got["event_ticker"] == "KXEPLGAME-26DEC26ARSCOV"

    def test_game_winner_titles_are_skipped_not_guessed(self):
        """Some 25/26 events title '{Team}: Game Winner?' — no ' vs '
        split, so no name-verified match is possible. They must be
        skipped, never fuzzy-attached."""
        books = [_book("KXEPLGAME-26AUG22ARSCOV",
                       "Arsenal: Game Winner?")]
        assert epl.find_book("2026-08-22T14:00Z", "Arsenal",
                             "Coventry City", books=books) is None

    def test_no_match_returns_none(self):
        books = [_book("KXEPLGAME-26AUG22ARSCOV", "Arsenal vs Coventry")]
        assert epl.find_book("2026-08-22T14:00Z", "Liverpool",
                             "Everton", books=books) is None

    def test_wrong_side_order_does_not_match(self):
        books = [_book("KXEPLGAME-26AUG22ARSCOV", "Arsenal vs Coventry")]
        assert epl.find_book("2026-08-22T14:00Z", "Coventry City",
                             "Arsenal", books=books) is None


# --- ticker-tail -> model key (grammar carried from MLS; EPL-unverified,
# exercised only when 26/27 markets list; unparseable tails -> None) --------

class TestModelKeys:
    SUFFIX = "ARSCOV"        # home-first: Arsenal home, Coventry away

    def test_totals_ladder(self):
        assert epl.model_key_for("KXEPLTOTAL",
                                 "KXEPLTOTAL-26AUG22ARSCOV-3",
                                 self.SUFFIX) == "over_2_5"

    def test_btts(self):
        assert epl.model_key_for("KXEPLBTTS",
                                 "KXEPLBTTS-26AUG22ARSCOV-BTTS",
                                 self.SUFFIX) == "btts"

    def test_spread_sides(self):
        assert epl.model_key_for("KXEPLSPREAD",
                                 "KXEPLSPREAD-26AUG22ARSCOV-ARS2",
                                 self.SUFFIX) == "home_margin_2"
        assert epl.model_key_for("KXEPLSPREAD",
                                 "KXEPLSPREAD-26AUG22ARSCOV-COV2",
                                 self.SUFFIX) == "away_margin_2"

    def test_team_total_and_score(self):
        assert epl.model_key_for("KXEPLTEAMTOTAL",
                                 "KXEPLTEAMTOTAL-26AUG22ARSCOV-COV3",
                                 self.SUFFIX) == "away_team_over_2_5"
        assert epl.model_key_for("KXEPLSCORE",
                                 "KXEPLSCORE-26AUG22ARSCOV-ARS2COV1",
                                 self.SUFFIX) == "score_2_1"

    def test_first_team_to_score(self):
        assert epl.model_key_for("KXEPLFTTS",
                                 "KXEPLFTTS-26AUG22ARSCOV-ARS",
                                 self.SUFFIX) == "home_first_goal"
        assert epl.model_key_for("KXEPLFTTS",
                                 "KXEPLFTTS-26AUG22ARSCOV-NONE",
                                 self.SUFFIX) == "no_goal"

    def test_unmodeled_and_unparseable_are_market_only(self):
        assert epl.model_key_for("KXEPL1H",
                                 "KXEPL1H-26AUG22ARSCOV-ARS",
                                 self.SUFFIX) is None
        assert epl.model_key_for("KXEPLSCORE",
                                 "KXEPLSCORE-26AUG22ARSCOV-OTHER",
                                 self.SUFFIX) is None


# --- summary parsing rides the shared (drift-hardened) parser --------------

class TestSummaryReuse:
    def test_eng1_summary_shape_parses_with_derived_letters(self):
        """Reduced from espn_summary_740966 (Brighton 2-0 Man United):
        the derived W/L letter must come from the scores, and the
        shared audit must cover eng.1 payloads unchanged."""
        d = {
            "header": {"id": "740966", "competitions": [{
                "date": "2026-05-24T15:00Z",
                "status": {"type": {"state": "post",
                                    "shortDetail": "FT"}},
                "competitors": [
                    {"homeAway": "home", "score": "2",
                     "team": {"id": "331", "displayName":
                              "Brighton & Hove Albion",
                              "abbreviation": "BHA"}},
                    {"homeAway": "away", "score": "0",
                     "team": {"id": "360", "displayName":
                              "Manchester United",
                              "abbreviation": "MAN"}},
                ]}]},
            "lastFiveGames": [{
                "team": {"displayName": "Manchester United",
                         "abbreviation": "MAN"},
                "events": [{
                    # winner-first provider string says "2-0"; the team
                    # LOST 0-2 away. The letter must derive from the
                    # authoritative fields, not the string.
                    "homeTeamScore": "2", "awayTeamScore": "0",
                    "atVs": "@", "score": "2-0", "gameResult": "L",
                    "opponent": {"abbreviation": "BHA"},
                    "gameDate": "2026-05-24T15:00Z"}],
            }],
        }
        out = epl.match_summary  # not called (network); parse directly
        from src.mls import parse_summary
        parsed = parse_summary(d)
        assert parsed["home"]["name"] == "Brighton & Hove Albion"
        g = parsed["scouting"]["last_five"][0]["games"][0]
        assert (g["team_score"], g["opponent_score"]) == (0, 2)
        assert g["result"] == "L"
        assert epl.scoreline_disagreements(d) == []


# --- P0-1: the public route is bounded, with an explicit call budget -------
# KXEPLGAME's archived 2025-26 season is 387 events and there are no
# current listings. Unbounded, `game_books` issues one /markets call PER
# EVENT — a full-season fan-out that returns nothing.


class _Provider:
    """A counting stand-in for epl._get_json. Serves the archived event
    list on /events and an empty book on /markets, and records exactly
    how many per-event market calls were made."""

    def __init__(self, events):
        self.events = events
        self.market_calls: list[str] = []

    def __call__(self, url, params=None):
        params = params or {}
        if url.endswith("/events"):
            return {"events": list(self.events)}
        if url.endswith("/markets"):
            self.market_calls.append(params.get("event_ticker"))
            return {"markets": []}
        return None


class TestMarketRetrievalIsBounded:
    def _install(self, monkeypatch, events):
        prov = _Provider(events)
        monkeypatch.setattr(epl, "_get_json", prov)
        monkeypatch.setattr(epl, "_cache", {})
        return prov

    def test_archived_history_costs_zero_per_event_market_calls(
            self, monkeypatch):
        """THE budget assertion. With a full archived season listed and
        nothing current, the public route must make NO per-event market
        calls at all — and in no case more than the declared budget.
        (Proven to fail: without the horizon bound this issues 60.)"""
        prov = self._install(monkeypatch, _archived_game_events())
        state = epl.game_books_state()
        assert state["events_seen"] == 387
        assert state["events_in_horizon"] == 0
        assert len(prov.market_calls) == 0
        assert len(prov.market_calls) <= epl.EVENT_MARKET_CALL_BUDGET
        assert state["games"] == []
        assert state["budget_exhausted"] is False

    def test_a_current_event_is_still_retrieved(self, monkeypatch):
        """CONTROL: the bound is on the DATE, not on the archive — a
        today-dated event inside the same archived list is fetched."""
        today = _ticker_for(date.today())
        prov = self._install(
            monkeypatch,
            _archived_game_events() + [{"event_ticker": today,
                                        "title": "Arsenal vs Chelsea"}])
        state = epl.game_books_state()
        assert state["events_in_horizon"] == 1
        assert prov.market_calls == [today]

    def test_a_recent_non_open_event_is_still_included(self, monkeypatch):
        """The MLS lesson, kept: an in-play or just-settled fixture stops
        reporting status 'open' while its markets still trade. The bound
        must never be a status filter — a yesterday-dated event whose
        provider status is 'closed' is still retrieved."""
        yday = _ticker_for(date.today() - timedelta(days=1))
        prov = self._install(monkeypatch, [
            {"event_ticker": yday, "title": "Arsenal vs Chelsea",
             "status": "closed"}])
        state = epl.game_books_state()
        assert state["events_in_horizon"] == 1
        assert prov.market_calls == [yday]

    def test_the_budget_caps_a_crowded_horizon_and_says_so(
            self, monkeypatch):
        crowded = [{"event_ticker": _ticker_for(
            date.today() + timedelta(days=i % 14), f"A{i:02d}CHE"),
            "title": "Arsenal vs Chelsea"}
            for i in range(epl.EVENT_MARKET_CALL_BUDGET + 16)]
        prov = self._install(monkeypatch, crowded)
        state = epl.game_books_state()
        assert len(prov.market_calls) == epl.EVENT_MARKET_CALL_BUDGET
        assert state["market_calls"] == epl.EVENT_MARKET_CALL_BUDGET
        assert state["budget_exhausted"] is True

    def test_an_unparseable_ticker_date_is_kept_not_discarded(
            self, monkeypatch):
        """Missing evidence is never silently converted into a
        conclusion: a ticker whose date segment cannot be read is not
        proof the event is history."""
        prov = self._install(monkeypatch, [
            {"event_ticker": "KXEPLGAME-WEIRD", "title": "A vs B"}])
        state = epl.game_books_state()
        assert state["events_in_horizon"] == 1
        assert prov.market_calls == ["KXEPLGAME-WEIRD"]


# --- API surface -----------------------------------------------------------

class TestEplRoutes:
    def test_routes_registered_and_read_only(self):
        from api.main import app
        paths = {r.path for r in app.routes}
        for p in ("/api/epl/scoreboard", "/api/epl/schedule",
                  "/api/epl/standings", "/api/epl/markets",
                  "/api/epl/markets/discovery", "/api/epl/odds",
                  "/api/epl/approval", "/api/epl/match/{event_id}"):
            assert p in paths
        for r in app.routes:
            if str(getattr(r, "path", "")).startswith("/api/epl"):
                # READ-ONLY surface: no EPL mutation exists in this phase
                assert set(r.methods) == {"GET"}

    def test_odds_board_reports_dark_empty(self, monkeypatch):
        """With the live plane dormant the board must be an explicit
        empty carrying the shadow + dark flags — never a zero-forecast."""
        from fastapi.testclient import TestClient
        from api.main import app
        with TestClient(app) as c:
            d = c.get("/api/epl/odds").json()
        assert d["odds"] == []
        assert d["shadow"] is True and d["model_dark"] is True
        assert d["real_money_signals"] is False

    def test_approval_route_reports_dark(self):
        from fastapi.testclient import TestClient
        from api.main import app
        with TestClient(app) as c:
            d = c.get("/api/epl/approval").json()
        assert d["mode"] == "dark"
        assert d["approved_for_shadow"] is False
        assert d["approval_decision_missing"] is True
