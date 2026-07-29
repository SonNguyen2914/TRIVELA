"""Club Friendlies viewer layer (canned payloads — no network in tests).

Shapes are reduced from the archived real responses in
research_archive/friendlies_*_2026-07-28.json. The scope line IS the
feature here, so the scope is tested: no standings surface, no model
keys on market rows, and FAIL-CLOSED identity/evidence semantics:

  - name identity is exact (normalized/token-set/evidence-backed alias),
    never substring containment — a lone near-miss candidate is
    `unresolved_name`, priced never (P0-1, review 2026-07-29);
  - missing evidence is never rendered as authority: a failed registry
    page is `registry_incomplete`, a failed market fetch `unavailable`,
    and a stale cache serves visibly stale with its age (P0-2).
"""
import threading
import time as _time

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

_ROW = [{"ticker": "T-X", "yes_sub_title": "x", "yes_ask_dollars": "0.50",
         "yes_bid_dollars": "0.45", "status": "active"}]


def _ev(ticker, title, markets="default"):
    e = {"event_ticker": ticker, "title": title}
    if markets == "default":
        e["markets"] = list(_ROW)
    elif markets is not None:
        e["markets"] = markets
    return e


_EVENTS = [
    _ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham"),
    _ev("KXCLUBFGAME-26JUL29ATMGET", "Atletico vs Getafe"),
    _ev("KXCLUBFGAME-26JUL28STKEVE", "Stoke vs Everton"),
    _ev("KXCLUBFGAME-26JUL29CERBVB", "Cerezo vs Dortmund"),
]


class TestNameIdentity:
    """P0-1: identity is exact or it does not price. Substring
    containment is banned — it attached a B-team fixture to the senior
    club's market."""

    def test_exact_name_maps(self):
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", _EVENTS)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"] == "KXCLUBFGAME-26JUL29LFCWRE"
        assert m["candidates"] == ["KXCLUBFGAME-26JUL29LFCWRE"]

    def test_token_set_equality_ignores_generic_suffixes(self):
        events = [_ev("KXCLUBFGAME-26JUL29SEVGET", "Sevilla vs Getafe")]
        m = friendlies.match_fixture_book(
            "2026-07-29T19:30Z", "Sevilla FC", "Getafe CF", events)
        assert m["status"] == "mapped"

    def test_evidence_backed_aliases_map(self):
        """The alias bridge is built ONLY from archived Kalshi titles
        cross-checked against archived ESPN buckets (both committed
        2026-07-28): Atletico/Cerezo/Dortmund."""
        m = friendlies.match_fixture_book(
            "2026-07-29T19:30Z", "Atlético Madrid", "Getafe", _EVENTS)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"].endswith("ATMGET")
        m = friendlies.match_fixture_book(
            "2026-07-29T10:00Z", "Cerezo Osaka", "Borussia Dortmund",
            _EVENTS)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"].endswith("CERBVB")

    def test_b_team_fixture_never_prices_the_senior_book(self):
        """THE P0-1 hazard, reproduced before the fix: an ESPN 'Real
        Madrid Castilla' fixture whose only same-day candidate is the
        SENIOR club's book must refuse — unresolved_name, no price."""
        events = [_ev("KXCLUBFGAME-26JUL31XRMA", "Tirol vs Real Madrid")]
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Tirol", "Real Madrid Castilla", events)
        assert m["status"] == "unresolved_name"
        assert m["book"] is None
        assert m["candidates"] == ["KXCLUBFGAME-26JUL31XRMA"]

    def test_senior_fixture_never_prices_the_b_team_book(self):
        """Reviewer's stated direction: a 'Real Madrid' fixture vs a
        book titled 'Real Madrid Castilla' -> unresolved, no price."""
        events = [_ev("KXCLUBFGAME-26JUL31ALBRMA",
                      "Albacete vs Real Madrid Castilla")]
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Albacete", "Real Madrid", events)
        assert m["status"] == "unresolved_name"
        assert m["book"] is None

    def test_barcelona_b_case(self):
        events = [_ev("KXCLUBFGAME-26JUL31GIRBAR", "Girona vs Barcelona B")]
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Girona", "Barcelona", events)
        assert m["status"] == "unresolved_name"
        assert m["book"] is None

    def test_exact_beats_a_loose_neighbour(self):
        """CONTROL (passes before and after): with the senior club's
        own exact-titled book present, the senior fixture maps to it
        even though the Castilla title is a token-superset neighbour."""
        events = [
            _ev("KXCLUBFGAME-26JUL31TIRRMA", "Tirol vs Real Madrid"),
            _ev("KXCLUBFGAME-26JUL31ALBRMA",
                "Albacete vs Real Madrid Castilla"),
        ]
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Tirol", "Real Madrid", events)
        assert m["status"] == "mapped"
        assert m["book"]["event_ticker"] == "KXCLUBFGAME-26JUL31TIRRMA"

    def test_ambiguity_fails_explicit_never_first_match(self):
        """Two same-day exact-tier titles matching one fixture must
        refuse and show what collided — never first-match-wins."""
        events = [
            _ev("KXCLUBFGAME-26JUL31ALBRM1", "Albacete vs Real Madrid"),
            _ev("KXCLUBFGAME-26JUL31ALBRM2", "Albacete vs Real Madrid"),
        ]
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Albacete", "Real Madrid", events)
        assert m["status"] == "ambiguous"
        assert m["book"] is None
        assert set(m["candidates"]) == {"KXCLUBFGAME-26JUL31ALBRM1",
                                        "KXCLUBFGAME-26JUL31ALBRM2"}

    def test_two_loose_candidates_are_ambiguous(self):
        """A senior-club fixture with TWO same-day reserve-side titles
        as loose neighbours and no exact book: refuse with both shown."""
        events = [
            _ev("KXCLUBFGAME-26JUL31XRMA",
                "Tirol vs Real Madrid Castilla"),
            _ev("KXCLUBFGAME-26JUL31XRMB", "Tirol vs Real Madrid B"),
        ]
        m = friendlies.match_fixture_book(
            "2026-07-31T18:00Z", "Tirol", "Real Madrid", events)
        assert m["status"] == "ambiguous"
        assert m["book"] is None
        assert set(m["candidates"]) == {"KXCLUBFGAME-26JUL31XRMA",
                                        "KXCLUBFGAME-26JUL31XRMB"}

    def test_et_date_disambiguates(self):
        # 2026-07-30T02:30Z is still Jul 29 in ET — must match the
        # 26JUL29 ticker, not a 26JUL30 one
        assert friendlies._fixture_et_date("2026-07-30T02:30Z") == "26JUL29"
        m = friendlies.match_fixture_book(
            "2026-07-28T18:00Z", "Liverpool", "Wrexham", _EVENTS)
        assert m["status"] == "unmapped"        # right names, wrong day

    def test_unmapped_is_explicit(self):
        m = friendlies.match_fixture_book(
            "2026-07-29T18:00Z", "PSV Eindhoven", "Heracles", _EVENTS)
        assert m == {"status": "unmapped", "book": None, "candidates": [],
                     "freshness": None}

    def test_single_match_with_no_tradeable_markets_is_explicit(self):
        events = [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham",
                      markets=[])]
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", events)
        assert m["status"] == "no_open_markets"
        assert m["book"] is None
        assert m["candidates"] == ["KXCLUBFGAME-26JUL29LFCWRE"]


class TestEvidenceHonesty:
    """P0-2: missing evidence must never be rendered as authority."""

    def test_market_fetch_failure_is_unavailable_not_closed(self, monkeypatch):
        """An exact candidate whose order book we COULD NOT FETCH is
        `unavailable` — never `no_open_markets` (the truth is 'we
        couldn't look', not 'nothing trades')."""
        events = [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham",
                      markets=None)]           # not injected -> live path
        monkeypatch.setattr(friendlies, "event_markets_state",
                            lambda t: (None, {"state": "missing",
                                              "age_seconds": None}))
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", events)
        assert m["status"] == "unavailable"
        assert m["book"] is None
        assert m["candidates"] == ["KXCLUBFGAME-26JUL29LFCWRE"]

    def test_stale_book_is_served_visibly_stale_with_age(self, monkeypatch):
        events = [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham",
                      markets=None)]
        monkeypatch.setattr(
            friendlies, "event_markets_state",
            lambda t: (list(_ROW), {"state": "stale", "age_seconds": 42}))
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", events)
        assert m["status"] == "mapped"
        assert m["freshness"] == {"state": "stale", "age_seconds": 42}

    def test_fresh_book_carries_no_stale_marker(self, monkeypatch):
        events = [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham",
                      markets=None)]
        monkeypatch.setattr(
            friendlies, "event_markets_state",
            lambda t: (list(_ROW), {"state": "fresh", "age_seconds": 0}))
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham", events)
        assert m["status"] == "mapped" and m["freshness"] is None

    def test_incomplete_registry_never_claims_unmapped(self, monkeypatch):
        """No candidate found + a registry we KNOW is partial ->
        registry_incomplete, because 'unmapped' asserts an absence the
        evidence cannot support."""
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": [], "complete": False, "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham")
        assert m["status"] == "registry_incomplete"
        assert m["book"] is None

    def test_truncated_registry_never_claims_unmapped(self, monkeypatch):
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": [], "complete": True, "truncated": True},
                     {"state": "fresh", "age_seconds": 0}))
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham")
        assert m["status"] == "registry_incomplete"

    def test_a_found_candidate_survives_an_incomplete_registry(self,
                                                               monkeypatch):
        """CONTROL: incompleteness only downgrades ABSENCE claims — a
        fixture actually found in the partial registry still maps."""
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": [_ev("KXCLUBFGAME-26JUL29LFCWRE",
                                     "Liverpool vs Wrexham",
                                     markets=None)],
                      "complete": False, "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        monkeypatch.setattr(
            friendlies, "event_markets_state",
            lambda t: (list(_ROW), {"state": "fresh", "age_seconds": 0}))
        m = friendlies.match_fixture_book(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham")
        assert m["status"] == "mapped"

    def test_pagination_failure_marks_registry_incomplete(self, monkeypatch):
        pages = [
            {"events": [_ev("KXCLUBFGAME-26JUL29AAABBB", "A vs B",
                            markets=None)], "cursor": "next-1"},
            None,                                  # page 2 fails
        ]
        calls = []

        def fake_get(url, params=None):
            calls.append(params or {})
            return pages[len(calls) - 1]
        monkeypatch.setattr(friendlies, "_get_json", fake_get)
        friendlies._cache.pop("events", None)
        reg, _meta = friendlies._game_events_state()
        assert reg["complete"] is False
        assert [e["event_ticker"] for e in reg["events"]] == [
            "KXCLUBFGAME-26JUL29AAABBB"]           # partial kept, marked
        friendlies._cache.pop("events", None)

    def test_incomplete_registry_is_not_cached_as_complete(self, monkeypatch):
        seq = [
            {"events": [], "cursor": "next-1"}, None,   # attempt 1: partial
            {"events": [_ev("KXCLUBFGAME-26JUL29AAABBB", "A vs B",
                            markets=None)], "cursor": None},  # attempt 2: ok
        ]
        calls = []

        def fake_get(url, params=None):
            calls.append(1)
            return seq[len(calls) - 1]
        monkeypatch.setattr(friendlies, "_get_json", fake_get)
        friendlies._cache.pop("events", None)
        reg1, _ = friendlies._game_events_state()
        assert reg1["complete"] is False
        reg2, _ = friendlies._game_events_state()   # must retry, not reuse
        assert reg2["complete"] is True
        assert len(reg2["events"]) == 1
        friendlies._cache.pop("events", None)


class TestKalshiPaging:
    def test_pages_follow_the_cursor(self, monkeypatch):
        """One probe returned exactly 200 events WITH a cursor — a
        single-call fetcher silently loses fixtures."""
        pages = [
            {"events": [_ev("KXCLUBFGAME-26JUL29AAABBB", "A vs B",
                            markets=None)], "cursor": "next-1"},
            {"events": [_ev("KXCLUBFGAME-26JUL30CCCDDD", "C vs D",
                            markets=None)], "cursor": None},
        ]
        calls = []

        def fake_get(url, params=None):
            calls.append(params or {})
            return pages[len(calls) - 1]
        monkeypatch.setattr(friendlies, "_get_json", fake_get)
        friendlies._cache.pop("events", None)
        reg, _ = friendlies._game_events_state()
        assert [e["event_ticker"] for e in reg["events"]] == [
            "KXCLUBFGAME-26JUL29AAABBB", "KXCLUBFGAME-26JUL30CCCDDD"]
        assert reg["complete"] is True and reg["truncated"] is False
        assert calls[1].get("cursor") == "next-1"
        friendlies._cache.pop("events", None)

    def test_first_page_failure_is_empty_incomplete_not_a_crash(
            self, monkeypatch):
        monkeypatch.setattr(friendlies, "_get_json",
                            lambda url, params=None: None)
        friendlies._cache.pop("events", None)
        reg, meta = friendlies._game_events_state()
        assert reg["events"] == [] and reg["complete"] is False
        friendlies._cache.pop("events", None)


class TestSingleFlight:
    def test_concurrent_cold_reads_fetch_upstream_once(self, monkeypatch):
        """Two concurrent cold requests for the same key must produce
        ONE provider fetch (cold-cache stampede guard)."""
        calls = []

        def slow_fetch(url, params=None):
            calls.append(1)
            _time.sleep(0.15)
            return {"events": [], "cursor": None}
        monkeypatch.setattr(friendlies, "_get_json", slow_fetch)
        friendlies._cache.pop("events", None)
        results = []

        def worker():
            reg, _ = friendlies._game_events_state()
            results.append(reg)
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        assert len(results) == 2
        assert len(calls) == 1                 # one upstream hit, shared
        friendlies._cache.pop("events", None)


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
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": [_ev("KXCLUBFGAME-26JUL29LFCWRE",
                                     "Liverpool vs Wrexham")],
                      "complete": True, "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
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
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": [], "complete": True, "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        out = friendlies.find_all_books(
            "2026-07-29T23:30Z", "Liverpool", "Wrexham")
        assert out["status"] == "unmapped"
        assert out["families"] == []


class TestDailyBooks:
    def test_rows_carry_explicit_status_and_freshness(self, monkeypatch):
        monkeypatch.setattr(friendlies, "scoreboard", lambda date=None: [
            {"id": "401898187", "date": "2026-07-29T23:30Z",
             "home": {"name": "Liverpool"}, "away": {"name": "Wrexham"}},
            {"id": "401898016", "date": "2026-07-29T17:00Z",
             "home": {"name": "Al Nassr"}, "away": {"name": "Mérida"}},
        ])
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": list(_EVENTS), "complete": True,
                      "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        rows = friendlies.daily_books()
        by_id = {r["fixture_id"]: r for r in rows}
        assert by_id["401898187"]["status"] == "mapped"
        assert by_id["401898187"]["book"]["event_ticker"].endswith("LFCWRE")
        assert by_id["401898187"]["freshness"] is None
        assert by_id["401898016"]["status"] == "unmapped"
        assert by_id["401898016"]["book"] is None

    def test_registry_incomplete_rows_say_so(self, monkeypatch):
        monkeypatch.setattr(friendlies, "scoreboard", lambda date=None: [
            {"id": "401898016", "date": "2026-07-29T17:00Z",
             "home": {"name": "Al Nassr"}, "away": {"name": "Mérida"}},
        ])
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": [], "complete": False, "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        rows = friendlies.daily_books()
        assert rows[0]["status"] == "registry_incomplete"


class TestListedSummary:
    def test_census_carries_completeness(self, monkeypatch):
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": list(_EVENTS), "complete": True,
                      "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        out = friendlies.listed_events_summary()
        assert out["count"] == 4
        assert out["complete"] is True and out["truncated"] is False

    def test_partial_census_is_marked_incomplete(self, monkeypatch):
        monkeypatch.setattr(
            friendlies, "_game_events_state",
            lambda: ({"events": list(_EVENTS[:1]), "complete": False,
                      "truncated": False},
                     {"state": "fresh", "age_seconds": 0}))
        out = friendlies.listed_events_summary()
        assert out["complete"] is False
        assert out["count"] == 1               # a floor, and marked as one


class TestCachedState:
    def test_failure_with_a_warm_cache_serves_stale_with_age(
            self, monkeypatch):
        key = "test:stale"
        friendlies._cache.pop(key, None)
        data, meta = friendlies._cached_state(key, 0.0, lambda: ["v1"])
        assert data == ["v1"] and meta["state"] == "fresh"
        # ttl 0 => the hit is instantly stale; the refetch fails
        data, meta = friendlies._cached_state(key, 0.0, lambda: None)
        assert data == ["v1"]
        assert meta["state"] == "stale"
        assert isinstance(meta["age_seconds"], int)
        assert meta["age_seconds"] >= 0
        friendlies._cache.pop(key, None)

    def test_failure_with_no_cache_is_missing(self):
        key = "test:missing"
        friendlies._cache.pop(key, None)
        data, meta = friendlies._cached_state(key, 60, lambda: None)
        assert data is None and meta["state"] == "missing"
        friendlies._cache.pop(key, None)


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

    def test_match_route_book_failure_reads_unavailable(self, monkeypatch):
        """The route's book-section fallback must be `unavailable` (we
        could not look), never an unmapped claim (P0-2 at the composed
        boundary)."""
        from fastapi.testclient import TestClient

        from api.main import app
        monkeypatch.setattr(
            friendlies, "match_summary",
            lambda eid: {"id": eid, "date": "2026-07-29T23:30Z",
                         "state": "pre", "detail": "Pre", "minute": None,
                         "venue": "X", "home": {"name": "Liverpool"},
                         "away": {"name": "Wrexham"}, "stats": [],
                         "events": [], "scouting": {}})

        def boom(*a, **k):
            raise RuntimeError("kalshi down")
        monkeypatch.setattr(friendlies, "find_all_books", boom)
        client = TestClient(app)
        r = client.get("/api/friendlies/match/401863532")
        assert r.status_code == 200
        assert r.json()["books"]["status"] == "unavailable"
