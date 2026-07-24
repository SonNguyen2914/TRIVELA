"""MLS data-layer parsers (canned payloads — no network in tests)."""
from src import mls

_EVENT = {
    "id": "740245", "date": "2026-07-22T23:30Z",
    "status": {"type": {"state": "pre", "shortDetail": "7/22 - 7:30 PM EDT"},
               "displayClock": "0'"},
    "competitions": [{
        "venue": {"fullName": "Chase Stadium"},
        "competitors": [
            {"homeAway": "home", "score": "0",
             "records": [{"summary": "12-3-5"}],
             "team": {"displayName": "Inter Miami CF", "abbreviation": "MIA",
                      "shortDisplayName": "Miami", "logo": "http://x/mia.png"}},
            {"homeAway": "away", "score": "0", "records": [],
             "team": {"displayName": "Chicago Fire FC", "abbreviation": "CHI",
                      "shortDisplayName": "Chicago", "logo": "http://x/chi.png"}},
        ]}],
}

_STANDINGS = {"children": [{
    "name": "Eastern Conference",
    "standings": {"entries": [
        {"team": {"displayName": "Inter Miami CF", "abbreviation": "MIA"},
         "stats": [{"name": "rank", "value": 1}, {"name": "points", "value": 41},
                   {"name": "gamesPlayed", "value": 20}, {"name": "wins", "value": 12},
                   {"name": "losses", "value": 3}, {"name": "ties", "value": 5},
                   {"name": "pointsFor", "value": 40},
                   {"name": "pointsAgainst", "value": 21},
                   {"name": "pointDifferential", "value": 19},
                   {"name": "ppg", "value": 2.05}]},
        {"team": {"displayName": "Chicago Fire FC", "abbreviation": "CHI"},
         "stats": [{"name": "rank", "value": 9}, {"name": "points", "value": 25}]},
    ]}}]}


class TestParsers:
    def test_parse_event(self):
        f = mls.parse_event(_EVENT)
        assert f["home"]["name"] == "Inter Miami CF"
        assert f["home"]["record"] == "12-3-5"
        assert f["away"]["abbrev"] == "CHI"
        assert f["state"] == "pre" and f["venue"] == "Chase Stadium"

    def test_parse_event_tolerates_missing_fields(self):
        f = mls.parse_event({"id": "x"})
        assert f["home"] == {} and f["away"] == {} and f["state"] is None

    def test_parse_standings_orders_by_rank(self):
        out = mls.parse_standings(_STANDINGS)
        assert out[0]["conference"] == "Eastern Conference"
        assert [e["rank"] for e in out[0]["entries"]] == [1, 9]
        assert out[0]["entries"][0]["points"] == 41
        assert out[0]["entries"][0]["goal_diff"] == 19

    def test_parse_game_books_keeps_both_sides(self):
        evs = [{"event_ticker": "KXMLSGAME-26JUL25SJLAG",
                "title": "San Jose vs Los Angeles G"}]
        mkts = {"KXMLSGAME-26JUL25SJLAG": [
            {"ticker": "KXMLSGAME-26JUL25SJLAG-SJ", "yes_sub_title": "San Jose",
             "yes_ask_dollars": "0.5900", "yes_bid_dollars": "0.5600",
             "status": "open"}]}
        out = mls.parse_game_books(evs, mkts)
        row = out[0]["markets"][0]
        assert row["yes_ask"] == "0.5900" and row["yes_bid"] == "0.5600"


class TestEndpoints:
    def test_routes_registered_and_read_only(self):
        from api.main import app
        paths = {r.path for r in app.routes}
        for p in ("/api/mls/scoreboard", "/api/mls/schedule",
                  "/api/mls/standings", "/api/mls/markets"):
            assert p in paths
        for r in app.routes:
            if str(getattr(r, "path", "")).startswith("/api/mls"):
                assert set(r.methods) == {"GET"}   # archive-compatible


_SUMMARY = {
    "header": {"id": "761668", "competitions": [{
        "date": "2026-07-22T23:30Z",
        "status": {"type": {"state": "in", "shortDetail": "38'"},
                   "displayClock": "38'"},
        "competitors": [
            {"homeAway": "home", "score": "1",
             "team": {"id": "183", "displayName": "Columbus Crew",
                      "abbreviation": "CLB"}},
            {"homeAway": "away", "score": "0",
             "team": {"id": "9668", "displayName": "New York City FC",
                      "abbreviation": "NYC"}}]}]},
    "gameInfo": {"venue": {"fullName": "Field"}},
    "boxscore": {"teams": [
        {"team": {"id": "9668"},
         "statistics": [{"name": "possessionPct", "displayValue": "41.0"},
                        {"name": "totalShots", "displayValue": "3"}]},
        {"team": {"id": "183"},
         "statistics": [{"name": "possessionPct", "displayValue": "59.0"},
                        {"name": "totalShots", "displayValue": "8"}]}]},
    "keyEvents": [
        {"clock": {"displayValue": "23'"}, "scoringPlay": True,
         "type": {"text": "Goal"}, "team": {"displayName": "Columbus Crew"},
         "text": "Goal! Header from the corner."}],
}


class TestSummaryParser:
    def test_sides_mapped_by_team_id_not_order(self):
        out = mls.parse_summary(_SUMMARY)
        # boxscore lists AWAY first here; mapping must use team ids
        stat = out["stats"][0]
        assert stat["label"] == "Possession %"
        assert stat["home"] == "59.0" and stat["away"] == "41.0"

    def test_header_and_events(self):
        out = mls.parse_summary(_SUMMARY)
        assert out["home"]["abbrev"] == "CLB" and out["home"]["score"] == "1"
        assert out["state"] == "in" and out["minute"] == "38'"
        ev = out["events"][0]
        assert ev["scoring"] and ev["minute"] == "23'"
        assert ev["team"] == "Columbus Crew"

    def test_tolerates_prematch_empty_boxscore(self):
        out = mls.parse_summary({"header": {}})
        assert out["stats"] == [] and out["events"] == []


class TestBookMatcher:
    _ROW = [{"ticker": "X", "label": "X", "yes_ask": "0.50",
             "yes_bid": "0.48", "status": "active"}]
    _BOOKS = [
        {"event_ticker": "KXMLSGAME-26JUL25SJLAG",
         "title": "San Jose vs Los Angeles G", "markets": _ROW},
        {"event_ticker": "KXMLSGAME-26JUL22SJORL",
         "title": "San Jose vs Orlando", "markets": _ROW},
        {"event_ticker": "KXMLSGAME-26JUL22LAGSTL",
         "title": "Los Angeles G vs Saint Louis", "markets": _ROW},
        {"event_ticker": "KXMLSGAME-26JUL22NYRBCLT",
         "title": "New York RB vs Charlotte", "markets": _ROW},
    ]

    def test_date_disambiguates_double_fixtures(self):
        # San Jose appears Jul 22 AND Jul 25 — the ET date must decide.
        b = mls.find_book("2026-07-22T23:30Z", "San Jose Earthquakes",
                          "Orlando City SC", self._BOOKS)
        assert b["event_ticker"].endswith("26JUL22SJORL")
        b = mls.find_book("2026-07-26T02:30Z", "San Jose Earthquakes",
                          "LA Galaxy", self._BOOKS)     # 02:30Z = Jul 25 ET
        assert b["event_ticker"].endswith("26JUL25SJLAG")

    def test_aliases_bridge_kalshi_names(self):
        b = mls.find_book("2026-07-22T23:30Z", "LA Galaxy",
                          "St. Louis CITY SC", self._BOOKS)
        assert b["event_ticker"].endswith("LAGSTL")
        # ESPN's real displayName (verified live Jul 23) — the test
        # originally encoded "New York Red Bulls" and hid the bug
        b = mls.find_book("2026-07-22T23:30Z", "Red Bull New York",
                          "Charlotte FC", self._BOOKS)
        assert b["event_ticker"].endswith("NYRBCLT")

    def test_no_match_returns_none(self):
        assert mls.find_book("2026-07-22T23:30Z", "Inter Miami CF",
                             "Chicago Fire FC", self._BOOKS) is None


class TestScoutingParsers:
    def test_last_five_and_h2h(self):
        d = {"lastFiveGames": [{"team": {"displayName": "Columbus Crew",
                                         "abbreviation": "CLB"},
                                "events": [{"gameResult": "L", "score": "3-0",
                                            "atVs": "@", "gameDate": "2026-05-10",
                                            "opponent": {"abbreviation": "NYC"}}]}],
             "headToHeadGames": [{"team": {"abbreviation": "CLB"},
                                  "events": [{"gameResult": "W",
                                              "homeTeamScore": "1",
                                              "awayTeamScore": "0",
                                              "atVs": "vs", "gameDate": "2026-05-20",
                                              "opponent": {"abbreviation": "NYC"}}]}]}
        lf = mls._parse_last_five(d)
        assert lf[0]["form"] == "L" and lf[0]["games"][0]["opponent"] == "NYC"
        h = mls._parse_h2h(d)
        assert h[0]["result"] == "W" and h[0]["perspective"] == "CLB"

    # ESPN dropped `headToHeadGames` and moved the data to `seasonseries`
    # (observed live Jul 24, 2026) — H2H vanished from the match page. The
    # parser reads BOTH shapes; these pin the new one so a silent upstream
    # rename can't empty the section again unnoticed.
    _SERIES = {"seasonseries": [{
        "type": "head-to-head", "seriesLabel": "Head-to-Head",
        "events": [
            {"date": "2025-11-08T23:00:00Z",
             "statusType": {"completed": True},
             "competitors": [
                 {"homeAway": "home", "winner": True, "score": "2",
                  "team": {"abbreviation": "CIN"}},
                 {"homeAway": "away", "winner": False, "score": "1",
                  "team": {"abbreviation": "CLB"}}]},
            {"date": "2025-11-02T23:30:00Z",
             "statusType": {"completed": True},
             "competitors": [
                 {"homeAway": "home", "winner": True, "score": "4",
                  "team": {"abbreviation": "CLB"}},
                 {"homeAway": "away", "winner": False, "score": "0",
                  "team": {"abbreviation": "CIN"}}]},
            {"date": "2026-08-01T23:30:00Z",       # future, not played
             "statusType": {"completed": False},
             "competitors": [
                 {"homeAway": "home", "score": None,
                  "team": {"abbreviation": "CIN"}},
                 {"homeAway": "away", "score": None,
                  "team": {"abbreviation": "CLB"}}]}]}]}

    def test_h2h_reads_seasonseries_after_espn_rename(self):
        h = mls._parse_h2h(self._SERIES)
        # only COMPLETED meetings (the scheduled one is excluded)
        assert len(h) == 2
        # perspective = home side of the most recent meeting
        assert {g["perspective"] for g in h} == {"CIN"}
        # home win for the perspective team, scores in match home-away order
        assert h[0]["result"] == "W" and h[0]["at_vs"] == "vs"
        assert h[0]["home_score"] == "2" and h[0]["away_score"] == "1"
        assert h[0]["opponent"] == "CLB"
        # away loss: CLB were home and won 4-0
        assert h[1]["result"] == "L" and h[1]["at_vs"] == "@"
        assert h[1]["home_score"] == "4" and h[1]["away_score"] == "0"

    def test_h2h_legacy_field_still_wins_when_present(self):
        both = dict(self._SERIES)
        both["headToHeadGames"] = [
            {"team": {"abbreviation": "CLB"},
             "events": [{"gameResult": "W", "homeTeamScore": "1",
                         "awayTeamScore": "0", "atVs": "vs",
                         "gameDate": "2026-05-20",
                         "opponent": {"abbreviation": "NYC"}}]}]
        h = mls._parse_h2h(both)
        assert len(h) == 1 and h[0]["perspective"] == "CLB"

    def test_h2h_draw_and_empty_cases(self):
        draw = {"seasonseries": [{"type": "head-to-head", "events": [
            {"date": "2026-05-17T23:15:00Z",
             "statusType": {"completed": True},
             "competitors": [
                 {"homeAway": "home", "winner": False, "score": "1",
                  "team": {"abbreviation": "CLB"}},
                 {"homeAway": "away", "winner": False, "score": "1",
                  "team": {"abbreviation": "CIN"}}]}]}]}
        assert mls._parse_h2h(draw)[0]["result"] == "D"
        assert mls._parse_h2h({}) == []
        assert mls._parse_h2h({"seasonseries": []}) == []
        # a series with no completed meetings yields nothing, not a crash
        assert mls._parse_h2h({"seasonseries": [{"type": "head-to-head",
                                                 "events": []}]}) == []


class TestScoutingScoreOrientation:
    """The scouting block showed a LOSS as a win (reported Jul 24, 2026).

    ESPN's lastFiveGames `score` string is WINNER-FIRST — a 0-1 home
    defeat arrives as "1-0" — so rendering it beside the perspective
    result letter inverted every defeat. Wins and draws looked fine,
    which is why it survived. These tests pin the derived, perspective-
    first scores AND the self-consistency invariant that catches any
    future provider reordering automatically."""

    # a real-shaped payload: MIN lost 0-1 at home, VAN lost 3-4 away —
    # both delivered by ESPN winner-first
    _PAYLOAD = {"lastFiveGames": [
        {"team": {"displayName": "Minnesota United FC", "abbreviation": "MIN"},
         "events": [
             {"gameResult": "L", "score": "1-0", "homeTeamScore": "0",
              "awayTeamScore": "1", "atVs": "vs", "gameDate": "2026-05-14",
              "opponent": {"abbreviation": "COL"}},
             {"gameResult": "D", "score": "2-2", "homeTeamScore": "2",
              "awayTeamScore": "2", "atVs": "vs", "gameDate": "2026-05-10",
              "opponent": {"abbreviation": "ATX"}},
             {"gameResult": "L", "score": "2-1", "homeTeamScore": "2",
              "awayTeamScore": "1", "atVs": "@", "gameDate": "2026-05-16",
              "opponent": {"abbreviation": "NE"}}]},
        {"team": {"displayName": "Vancouver Whitecaps", "abbreviation": "VAN"},
         "events": [
             {"gameResult": "W", "score": "3-2", "homeTeamScore": "2",
              "awayTeamScore": "3", "atVs": "@", "gameDate": "2026-05-14",
              "opponent": {"abbreviation": "DAL"}},
             {"gameResult": "L", "score": "4-3", "homeTeamScore": "4",
              "awayTeamScore": "3", "atVs": "@", "gameDate": "2026-07-22",
              "opponent": {"abbreviation": "CIN"}}]}]}

    def test_scores_are_perspective_first_not_winner_first(self):
        five = mls._parse_last_five(self._PAYLOAD)
        min_games = {g["opponent"]: g for g in five[0]["games"]}
        # the reported bug: shown as "1-0" beside an L
        assert min_games["COL"]["team_score"] == 0
        assert min_games["COL"]["opponent_score"] == 1
        # away defeat, ESPN said "2-1"
        assert min_games["NE"]["team_score"] == 1
        assert min_games["NE"]["opponent_score"] == 2
        assert min_games["ATX"]["team_score"] == 2      # draw unchanged
        van = {g["opponent"]: g for g in five[1]["games"]}
        assert van["DAL"]["team_score"] == 3            # away win
        assert van["CIN"]["team_score"] == 3            # away defeat 3-4
        assert van["CIN"]["opponent_score"] == 4

    def test_result_letter_always_agrees_with_shown_scores(self):
        """The systemic guard: W/L/D must match the two numbers beside it,
        in EVERY scouting row. Any provider reordering fails here."""
        assert mls.scoreline_disagreements(self._PAYLOAD) == []

    def test_audit_catches_provider_drift(self):
        """The guard must fire when the provider's own letter disagrees
        with its scores — the early warning that ESPN changed semantics
        again. The PAGE stays correct regardless (the letter it renders is
        derived from these same scores)."""
        broken = {"lastFiveGames": [
            {"team": {"abbreviation": "MIN"},
             "events": [{"gameResult": "W", "score": "1-0",
                         "homeTeamScore": "0", "awayTeamScore": "1",
                         "atVs": "vs",             # home, scored 0, conceded 1
                         "opponent": {"abbreviation": "COL"}}]}]}
        bad = mls.scoreline_disagreements(broken)
        assert len(bad) == 1
        assert bad[0]["provider_result"] == "W"
        assert bad[0]["derived"] == "L" and bad[0]["team_score"] == 0
        # and the rendered row shows the DERIVED result, not the bad label
        g = mls._parse_last_five(broken)[0]["games"][0]
        assert g["result"] == "L" and g["team_score"] == 0

    def test_displayed_letter_cannot_contradict_displayed_scores(self):
        """The structural guarantee: whatever the provider says, the W/L/D
        the page renders is computed from the two numbers beside it."""
        for payload in (self._PAYLOAD, {"lastFiveGames": [
                {"team": {"abbreviation": "X"},
                 "events": [{"gameResult": "W", "homeTeamScore": "1",
                             "awayTeamScore": "3", "atVs": "vs",
                             "opponent": {"abbreviation": "Y"}}]}]}):
            for t in mls._parse_last_five(payload):
                for g in t["games"]:
                    ts, os_ = g["team_score"], g["opponent_score"]
                    if ts is None or os_ is None:
                        continue
                    expect = "W" if ts > os_ else "L" if ts < os_ else "D"
                    assert g["result"] == expect

    def test_audit_covers_h2h_rows_too(self):
        series = {"seasonseries": [{"type": "head-to-head", "events": [
            {"date": "2026-03-15T00:00:00Z",
             "statusType": {"completed": True},
             "competitors": [
                 {"homeAway": "home", "winner": True, "score": "6",
                  "team": {"abbreviation": "VAN"}},
                 {"homeAway": "away", "winner": False, "score": "0",
                  "team": {"abbreviation": "MIN"}}]}]}]}
        assert mls.scoreline_disagreements(series) == []

    def test_missing_scores_are_skipped_not_guessed(self):
        payload = {"lastFiveGames": [
            {"team": {"abbreviation": "MIN"},
             "events": [{"gameResult": "L", "score": "1-0", "atVs": "vs",
                         "opponent": {"abbreviation": "COL"}}]}]}
        g = mls._parse_last_five(payload)[0]["games"][0]
        assert g["team_score"] is None and g["opponent_score"] is None
        # unknown scores must not be reported as a disagreement
        assert mls.scoreline_disagreements(payload) == []


class TestModelKeyParser:
    """Ticker-tail -> model probability key (never label text)."""

    def test_totals_ladder(self):
        assert mls.model_key_for("KXMLSTOTAL",
                                 "KXMLSTOTAL-26JUL25CLBCIN-1",
                                 "CLBCIN") == "over_0_5"
        assert mls.model_key_for("KXMLSTOTAL",
                                 "KXMLSTOTAL-26JUL25CLBCIN-6",
                                 "CLBCIN") == "over_5_5"

    def test_btts_and_spread_sides(self):
        assert mls.model_key_for("KXMLSBTTS",
                                 "KXMLSBTTS-26JUL25CLBCIN-BTTS",
                                 "CLBCIN") == "btts"
        assert mls.model_key_for("KXMLSSPREAD",
                                 "KXMLSSPREAD-26JUL25CLBCIN-CLB2",
                                 "CLBCIN") == "home_margin_2"
        assert mls.model_key_for("KXMLSSPREAD",
                                 "KXMLSSPREAD-26JUL25CLBCIN-CIN3",
                                 "CLBCIN") == "away_margin_3"

    def test_team_totals_and_score(self):
        assert mls.model_key_for("KXMLSTEAMTOTAL",
                                 "KXMLSTEAMTOTAL-26JUL25CLBCIN-CIN2",
                                 "CLBCIN") == "away_team_over_1_5"
        assert mls.model_key_for("KXMLSSCORE",
                                 "KXMLSSCORE-26JUL25CLBCIN-CLB4CIN2",
                                 "CLBCIN") == "score_4_2"

    def test_near_collision_codes(self):
        # NYRB (home) vs NYC (away): "NYC" must not read as a prefix
        assert mls.model_key_for("KXMLSSPREAD",
                                 "KXMLSSPREAD-26MAY16NYRBNYC-NYRB2",
                                 "NYRBNYC") == "home_margin_2"
        assert mls.model_key_for("KXMLSSPREAD",
                                 "KXMLSSPREAD-26MAY16NYRBNYC-NYC2",
                                 "NYRBNYC") == "away_margin_2"

    def test_unmodeled_families_are_market_only(self):
        assert mls.model_key_for("KXMLSMOV",
                                 "KXMLSMOV-26JUL25CLBCIN-REG",
                                 "CLBCIN") is None
        assert mls.model_key_for("KXMLS1H",
                                 "KXMLS1H-26JUL25CLBCIN-CLB",
                                 "CLBCIN") is None
