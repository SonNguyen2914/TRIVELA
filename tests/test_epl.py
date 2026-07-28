"""EPL data-layer parsers + fixture/book matching (canned payloads — no
network in tests). Shapes are reduced from the archived real responses
in research_archive/epl/ (fetched 2026-07-28)."""
from src import epl

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
