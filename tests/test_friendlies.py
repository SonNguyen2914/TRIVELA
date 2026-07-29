"""Club Friendlies viewer layer (canned payloads — no network in tests).

Shapes are reduced from the archived real responses in
research_archive/friendlies_*_2026-07-28.json. The scope line IS the
feature here, so the scope is tested: no standings surface, no model
keys on market rows, ambiguity refusing to pick a book.
"""
from src import friendlies, mls

# --- ESPN parser reuse (reduced from friendlies_espn_scoreboard_*) ---------
# Real Madrid 4-1 Leganes, completed — the archived 2026-07-28 bucket.

_EVENT = {
    "id": "401898187", "date": "2026-07-28T16:00Z",
    "status": {"type": {"state": "post", "shortDetail": "FT"},
               "displayClock": "90'"},
    "competitions": [{
        "venue": {"fullName": "Estadio Santiago Bernabeu"},
        "competitors": [
            {"homeAway": "home", "score": "4", "records": [],
             "team": {"displayName": "Real Madrid", "abbreviation": "RMA",
                      "shortDisplayName": "Real Madrid",
                      "logo": "http://x/rma.png"}},
            {"homeAway": "away", "score": "1", "records": [],
             "team": {"displayName": "Leganés", "abbreviation": "LEG",
                      "shortDisplayName": "Leganés",
                      "logo": "http://x/leg.png"}},
        ]}],
}


class TestEspnParsing:
    def test_parse_event_on_a_friendly(self):
        f = friendlies.parse_event(_EVENT)
        assert f["home"]["name"] == "Real Madrid"
        assert f["home"]["score"] == "4" and f["away"]["score"] == "1"
        assert f["state"] == "post"
        assert f["venue"] == "Estadio Santiago Bernabeu"

    def test_summary_tolerates_empty_boxscore(self):
        """The archived Real Madrid summary carried a boxscore with NO
        statistics while PSV's was complete — both are normal for
        friendlies and the parser renders what exists."""
        out = friendlies.parse_summary({"header": {}})
        assert out["stats"] == [] and out["events"] == []

    def test_scoreline_audit_is_the_shared_one(self):
        """Result letters must be derived from the scores beside them —
        the same drift audit as every other league surface."""
        clean = {"lastFiveGames": [{
            "team": {"displayName": "Real Madrid", "abbreviation": "RMA"},
            "events": [{"gameResult": "W", "homeTeamScore": "0",
                        "awayTeamScore": "2", "atVs": "@",
                        "opponent": {"abbreviation": "ESP"}}]}]}
        assert friendlies.scoreline_disagreements(clean) == []
        drifted = {"lastFiveGames": [{
            "team": {"displayName": "Real Madrid", "abbreviation": "RMA"},
            "events": [{"gameResult": "W", "homeTeamScore": "2",
                        "awayTeamScore": "0", "atVs": "@",
                        "opponent": {"abbreviation": "ESP"}}]}]}
        bad = friendlies.scoreline_disagreements(drifted)
        assert bad and bad[0]["derived"] == "L"


class TestCacheIsolation:
    def test_own_cache_never_shared_with_mls(self):
        """Shared cache dicts cross-serve leagues on identical keys
        ("sb:<date>", "team_colors") — a known trap. The friendlies
        cache must be its own object."""
        assert friendlies._cache is not mls._cache


class TestFraming:
    def test_framing_states_no_model_and_promises_none(self):
        f = friendlies.FRAMING.lower()
        assert "no model" in f
        assert "none is planned" in f
        # never imply a model is arriving
        for banned in ("coming soon", "not yet", "pending", "dark",
                       "for now"):
            assert banned not in f


# --- fixture <-> book matching (reduced from the archived KXCLUBFGAME
# event list; grammar {YYMONDD}{HOME}{AWAY}, " vs " titles) -----------------

def _ev(ticker, title):
    return {"event_ticker": ticker, "title": title}


_ROW = [{"ticker": "T-X", "yes_sub_title": "x", "yes_ask_dollars": "0.50",
         "yes_bid_dollars": "0.45", "status": "active"}]

_EVENTS = [
    _ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham"),
    _ev("KXCLUBFGAME-26JUL29ATMGET", "Atletico vs Getafe"),
    _ev("KXCLUBFGAME-26JUL28STKEVE", "Stoke vs Everton"),
    _ev("KXCLUBFGAME-26JUL29CERBVB", "Cerezo vs Dortmund"),
]
for _e in _EVENTS:
    _e["markets"] = list(_ROW)


class TestBookMatcher:
    def test_maps_by_date_and_both_title_sides(self):
        # ESPN 2026-07-29T23:30Z = Jul 29 in ET
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", _EVENTS)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"] == "KXCLUBFGAME-26JUL29LFCWRE"
        assert m["candidates"] == ["KXCLUBFGAME-26JUL29LFCWRE"]

    def test_accents_normalize(self):
        # ESPN says "Atlético Madrid"; Kalshi titles "Atletico"
        m = friendlies.match_fixture_book(
            "2026-07-29T19:30Z", "Atlético Madrid", "Getafe", _EVENTS)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"].endswith("ATMGET")

    def test_et_date_disambiguates(self):
        # 2026-07-30T02:30Z is still Jul 29 in ET — must match the
        # 26JUL29 ticker, not a 26JUL30 one
        assert friendlies._fixture_et_date("2026-07-30T02:30Z") == "26JUL29"
        m = friendlies.match_fixture_book(
            "2026-07-28T18:00Z", "Liverpool", "Wrexham", _EVENTS)
        assert m["status"] == "unmapped"        # right names, wrong day

    def test_unmapped_is_explicit(self):
        m = friendlies.match_fixture_book(
            "2026-07-29T18:00Z", "PSV Eindhoven", "FC Eindhoven", _EVENTS)
        assert m == {"status": "unmapped", "book": None, "candidates": []}

    def test_ambiguity_fails_explicit_never_first_match(self):
        """THE scope guard. Same ET date, two titles that both match the
        fixture's names ("Real Madrid" is a substring of "Real Madrid
        Castilla" — the B-team case, live in the archived event list as
        26JUL31ALBRMA). A first-match-wins matcher returns whichever
        Kalshi listed first; this must refuse and show what collided."""
        events = [
            _ev("KXCLUBFGAME-26JUL31ALBRM", "Albacete vs Real Madrid"),
            _ev("KXCLUBFGAME-26JUL31ALBRMA",
                "Albacete vs Real Madrid Castilla"),
        ]
        for e in events:
            e["markets"] = list(_ROW)
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Albacete", "Real Madrid Castilla", events)
        assert m["status"] == "ambiguous"
        assert m["book"] is None
        assert set(m["candidates"]) == {"KXCLUBFGAME-26JUL31ALBRM",
                                        "KXCLUBFGAME-26JUL31ALBRMA"}

    def test_unambiguous_b_team_reverse_direction_still_maps(self):
        """CONTROL (passes both ways): the reverse containment is safe.
        An ESPN "Real Madrid" fixture cannot match the Castilla title
        ("real madrid castilla" is not a substring of "real madrid"),
        so the senior club's fixture maps cleanly."""
        events = [
            _ev("KXCLUBFGAME-26JUL31XRMA", "Tirol vs Real Madrid"),
            _ev("KXCLUBFGAME-26JUL31ALBRMA",
                "Albacete vs Real Madrid Castilla"),
        ]
        for e in events:
            e["markets"] = list(_ROW)
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Tirol", "Real Madrid", events)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"] == "KXCLUBFGAME-26JUL31XRMA"

    def test_single_match_with_no_tradeable_markets_is_explicit(self):
        events = [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham")]
        events[0]["markets"] = []
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", events)
        assert m["status"] == "no_open_markets"
        assert m["book"] is None
        assert m["candidates"] == ["KXCLUBFGAME-26JUL29LFCWRE"]


class TestMarketRowsAreMarketOnly:
    def test_no_model_key_anywhere(self):
        """Scope line as a test: friendlies market rows carry NO
        model_key field — there is no model to key into, ever. (The MLS
        layer's match-family rows do carry one; copying that row shape
        here would quietly imply a model is arriving.)"""
        book = friendlies.parse_game_books(
            [{"event_ticker": "KXCLUBFGAME-26JUL29LFCWRE",
              "title": "Liverpool vs Wrexham"}],
            {"KXCLUBFGAME-26JUL29LFCWRE": _ROW})[0]
        for row in book["markets"]:
            assert "model_key" not in row

    def test_family_rows_are_market_only_too(self, monkeypatch):
        """The rows find_all_books builds ITSELF (total/btts/spread)
        must be market-only as well — this is where copying the MLS row
        shape would reintroduce model_key."""
        monkeypatch.setattr(friendlies, "_game_events", lambda: [
            dict(_EVENTS[0])])
        monkeypatch.setattr(
            friendlies, "event_markets",
            lambda ticker: [{"ticker": f"{ticker}-3",
                             "yes_sub_title": "Over 2.5 goals scored",
                             "yes_ask_dollars": "0.73",
                             "yes_bid_dollars": "0.70",
                             "status": "active"}])
        monkeypatch.setattr(friendlies.time, "sleep", lambda s: None)
        friendlies._cache.pop("allbooks:26JUL29LFCWRE", None)
        out = friendlies.find_all_books(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham")
        friendlies._cache.pop("allbooks:26JUL29LFCWRE", None)
        assert out["status"] == "mapped"
        keys = [f["key"] for f in out["families"]]
        assert keys[0] == "winner"
        assert {"total", "btts", "spread"} <= set(keys)
        for fam in out["families"]:
            for row in fam["markets"]:
                assert "model_key" not in row
                assert set(row) == {"ticker", "label", "yes_ask",
                                    "yes_bid", "status"}

    def test_find_all_books_short_circuits_when_not_mapped(self, monkeypatch):
        monkeypatch.setattr(friendlies, "_game_events", lambda: [])
        out = friendlies.find_all_books(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham")
        assert out == {"status": "unmapped", "candidates": [],
                       "families": []}


class TestKalshiPaging:
    def test_pages_follow_the_cursor(self, monkeypatch):
        """One probe returned exactly 200 events WITH a cursor — a
        single-call fetcher silently loses fixtures."""
        pages = [
            {"events": [_ev("KXCLUBFGAME-26JUL29AAABBB", "A vs B")],
             "cursor": "next-1"},
            {"events": [_ev("KXCLUBFGAME-26JUL30CCCDDD", "C vs D")],
             "cursor": None},
        ]
        calls = []

        def fake_get(url, params=None):
            calls.append(params or {})
            return pages[len(calls) - 1]
        monkeypatch.setattr(friendlies, "_get_json", fake_get)
        friendlies._cache.pop("events", None)
        evs = friendlies._game_events()
        assert [e["event_ticker"] for e in evs] == [
            "KXCLUBFGAME-26JUL29AAABBB", "KXCLUBFGAME-26JUL30CCCDDD"]
        assert calls[1].get("cursor") == "next-1"
        friendlies._cache.pop("events", None)

    def test_first_page_failure_is_empty_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(friendlies, "_get_json",
                            lambda url, params=None: None)
        friendlies._cache.pop("events", None)
        assert friendlies._game_events() == []
        friendlies._cache.pop("events", None)


class TestDailyBooks:
    def test_rows_carry_explicit_status(self, monkeypatch):
        monkeypatch.setattr(friendlies, "scoreboard", lambda date=None: [
            {"id": "401898187", "date": "2026-07-29T23:30Z",
             "home": {"name": "Liverpool"}, "away": {"name": "Wrexham"}},
            {"id": "401898016", "date": "2026-07-29T17:00Z",
             "home": {"name": "Al Nassr"}, "away": {"name": "Mérida"}},
        ])
        monkeypatch.setattr(friendlies, "_game_events", lambda: _EVENTS)
        rows = friendlies.daily_books()
        by_id = {r["fixture_id"]: r for r in rows}
        assert by_id["401898187"]["status"] == "mapped"
        assert by_id["401898187"]["book"]["event_ticker"].endswith("LFCWRE")
        assert by_id["401898016"]["status"] == "unmapped"
        assert by_id["401898016"]["book"] is None


class TestEndpoints:
    def test_routes_registered_and_get_only(self):
        from api.main import app
        paths = {r.path for r in app.routes}
        for p in ("/api/friendlies/scoreboard", "/api/friendlies/schedule",
                  "/api/friendlies/markets",
                  "/api/friendlies/match/{event_id}"):
            assert p in paths
        for r in app.routes:
            if str(getattr(r, "path", "")).startswith("/api/friendlies"):
                assert set(r.methods) == {"GET"}   # viewer surface

    def test_no_standings_route_exists(self):
        """Standings do not exist for friendlies; a standings route
        would have to fabricate a table."""
        from api.main import app
        for r in app.routes:
            path = str(getattr(r, "path", ""))
            if path.startswith("/api/friendlies"):
                assert "standings" not in path

    def test_no_friendlies_admin_or_mutation_surface(self):
        """No sweep, no backfill, no approval — the evidence machinery
        does not exist here and no route should suggest otherwise."""
        from api.main import app
        for r in app.routes:
            path = str(getattr(r, "path", ""))
            assert not path.startswith("/api/admin/friendlies")
