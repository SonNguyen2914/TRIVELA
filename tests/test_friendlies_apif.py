"""API-Football fixture backbone for club friendlies (canned payloads —
no network in tests).

Shapes are reduced from the archived real responses in
research_archive/friendlies_apif_*_2026-07-29.json. Three things are the
feature here, so three things are tested:

  COVERAGE. The sweep must be date-scoped, because the fixtures Son needs
  live in leagues and seasons a 667+2026 sweep cannot reach — Liverpool v
  Wrexham is league 1022, season 2025. A league-scoped sweep is not a
  smaller answer, it is a structurally blind one.

  IDENTITY IS AN INTEGER. Name matching PROPOSES, a recorded id CONFIRMS.
  The reserve-side case is the load-bearing one: `Real Madrid Castilla`
  must reach 9575 and must never reach 541.

  DENOMINATORS. A count without its denominator is a defect here, and
  fraction_bridged must be None rather than 0.0 when there is nothing to
  divide by. An empty market is not zero coverage.

Plus the invariants inherited from src/friendlies.py, re-asserted because
this module is a second door onto the same surface: no model, no price
for an unresolved identity, and `unavailable` / `stale` /
`no_open_markets` keeping their distinct meanings.
"""
from datetime import datetime, timezone

import pytest

from src import friendlies, friendlies_apif as fa


# --- fixture rows, reduced from the archived date-scoped sweep -------------
# f1589333 is the one that proves the point: league 1022, SEASON 2025.

def _fx(fid, hid, hname, aid, aname, kickoff,
        league=667, lname="Friendlies Clubs", season=2026, status="NS",
        goals=(None, None)):
    return {"fixture": {"id": fid, "date": kickoff,
                        "status": {"short": status, "long": status,
                                   "elapsed": None}},
            "league": {"id": league, "name": lname, "season": season},
            "teams": {"home": {"id": hid, "name": hname},
                      "away": {"id": aid, "name": aname}},
            "goals": {"home": goals[0], "away": goals[1]}}


RAW_LIVERPOOL = _fx(1589333, 40, "Liverpool", 1837, "Wrexham",
                    "2026-07-29T23:30:00+00:00",
                    league=1022, lname="Premier League - Summer Series",
                    season=2025, status="1H")
RAW_AUGSBURG = _fx(1583494, 170, "FC Augsburg", 35, "Bournemouth",
                   "2026-07-30T14:00:00+00:00")
RAW_CASTILLA = _fx(1598568, 722, "Albacete", 9575, "Real Madrid II",
                   "2026-07-31T18:00:00+00:00")
RAW_SENIOR_RM = _fx(1583506, 541, "Real Madrid", 96, "Toulouse",
                    "2026-07-31T17:00:00+00:00")
RAW_JUVE = _fx(1585004, 496, "Juventus", 84, "Nice",
               "2026-07-30T18:00:00+00:00")

ALL_RAW = [RAW_LIVERPOOL, RAW_AUGSBURG, RAW_CASTILLA, RAW_SENIOR_RM, RAW_JUVE]


def _swept(raw=None, complete=True, failures=None):
    """A sweep dict as `sweep()` returns one, without touching a network."""
    rows = [fa.parse_fixture(r) for r in (ALL_RAW if raw is None else raw)]
    return {"fixtures": [r for r in rows if r], "utc_dates": ["2026-07-29"],
            "complete": complete, "failures": failures or [],
            "requests_used": 0, "key_source": "test", "problems": [],
            "cached": False}


def _ev(ticker, title, statuses=("active",)):
    return {"event_ticker": ticker, "title": title,
            "markets": [{"ticker": f"{ticker}-TIE", "status": s}
                        for s in statuses]}


@pytest.fixture(autouse=True)
def _clean_cache():
    fa.reset_cache()
    yield
    fa.reset_cache()


class TestScopeIsUnchanged:
    def test_no_model_key_anywhere_in_a_row(self):
        """Friendlies get no model and none is planned. A model_key field
        would be the first step to a shape that implies otherwise."""
        swept = _swept()
        row = fa.bridge_event(_ev("KXCLUBFGAME-26JUL29LFCWRE",
                                  "Liverpool vs Wrexham"), swept)
        assert "model_key" not in row and "prediction" not in row
        assert "edge" not in row and "forecast" not in row

    def test_module_declares_no_scheduler_or_db_write(self):
        import inspect
        src = inspect.getsource(fa)
        for banned in ("session.add", "session.commit", "add_job",
                       "REAL_MONEY", "place_order"):
            assert banned not in src, f"{banned} must not appear here"

    def test_framing_note_refuses_forecast_vocabulary(self):
        note = fa.FRAMING_NOTE.lower()
        assert "no model runs on friendlies" in note
        for banned in ("prediction", "forecast", "edge", "recommend"):
            assert banned not in note

    def test_no_bare_take_buy_sell_reaches_a_consumer(self):
        """GUARD. Checks EMITTED strings, not source prose — an earlier
        draft of this test scanned the source and failed on the docstring
        sentence that promises the words are absent, which is a test
        measuring the wrong surface. What matters is what ships."""
        swept = _swept()
        payload = fa.coverage(
            [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham"),
             _ev("KXCLUBFGAME-26JUL30FCREBMU",
                 "FC Rottach-Egern vs Bayern Munich")], swept)

        def walk(node):
            if isinstance(node, str):
                up = node.upper()
                for banned in ("TAKE ", "BUY ", "SELL ", "VALUE BET",
                               "RIPE", "EASY WIN"):
                    assert banned not in up, f"{banned!r} in {node!r}"
            elif isinstance(node, dict):
                for k, v in node.items():
                    walk(k)
                    walk(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    walk(v)

        walk(payload)
        walk(fa.FRAMING_NOTE)


class TestOwnCache:
    def test_cache_is_not_shared_with_the_espn_viewer(self):
        """A shared dict would cross-serve on identical keys — the trap
        src/friendlies.py already documents against src.mls."""
        assert fa._cache is not friendlies._cache


class TestParseFixture:
    def test_parses_the_archived_shape(self):
        f = fa.parse_fixture(RAW_LIVERPOOL)
        assert f["fixture_id"] == 1589333
        assert f["home"]["apif_team_id"] == 40
        assert f["away"]["apif_team_id"] == 1837
        assert f["league_id"] == 1022 and f["league_season"] == 2025

    def test_a_row_without_team_ids_is_absent_not_a_fixture(self):
        """GUARD. Identity is an integer; a row missing one cannot be
        joined, so it must vanish rather than arrive with a null id that
        a downstream set-join would happily match against another null."""
        broken = _fx(1, None, "A", 2, "B", "2026-07-29T12:00:00+00:00")
        assert fa.parse_fixture(broken) is None

    def test_a_row_without_a_kickoff_is_absent(self):
        assert fa.parse_fixture(_fx(1, 10, "A", 20, "B", None)) is None

    def test_null_score_stays_null_and_never_becomes_nil_nil(self):
        """GUARD. A not-started fixture has NO score. Rendering absent as
        0-0 invents a result — the repo's standing rule that a null
        provider value is ABSENT, never zero."""
        f = fa.parse_fixture(RAW_LIVERPOOL)
        assert f["goals"] == {"home": None, "away": None}

    def test_a_real_nil_nil_is_still_reported_as_zero(self):
        """Control, both ways: absent must not read as zero, and a
        genuine zero must not read as absent."""
        f = fa.parse_fixture(_fx(9, 1, "A", 2, "B",
                                 "2026-07-29T12:00:00+00:00",
                                 status="FT", goals=(0, 0)))
        assert f["goals"] == {"home": 0, "away": 0}

    def test_classification_flag_tracks_the_recorded_league_set(self):
        assert fa.parse_fixture(RAW_JUVE)["is_classified_friendly"] is True
        stray = _fx(2, 1, "A", 2, "B", "2026-07-29T12:00:00+00:00",
                    league=71, lname="Serie A")
        assert fa.parse_fixture(stray)["is_classified_friendly"] is False


class TestProviderPayloadRules:
    def test_the_array_is_counted_not_the_declared_results_field(self):
        """GUARD, the governing rule of docs/APIFOOTBALL-TRIAL.md §2: the
        provider's own `results` count is never the answer. `results: 0`
        on a fixture that was live at the 45th minute is recorded in this
        repo at src/live_feed.py:221."""
        lying = {"results": 0, "response": [RAW_JUVE], "errors": []}
        assert len(fa.response_items(lying)) == 1
        other_way = {"results": 99, "response": [], "errors": []}
        assert fa.response_items(other_way) == []

    def test_a_non_list_response_is_empty_not_an_item(self):
        assert fa.response_items({"response": {"a": 1}}) == []
        assert fa.response_items("nonsense") == []

    def test_errors_inside_a_200_are_surfaced(self):
        """API-Football reports plan problems INSIDE a 200 body."""
        assert fa.provider_errors(
            {"errors": {"season": "The Season field is required."}}) == [
            "season: The Season field is required."]
        assert fa.provider_errors({"errors": []}) == []


class TestSecrets:
    def test_a_group_readable_key_file_is_refused_not_read(self, tmp_path,
                                                           monkeypatch):
        """GUARD. Reading a group-readable secret would normalise leaving
        it readable."""
        p = tmp_path / "key"
        p.write_text("SECRET123", encoding="utf-8")
        p.chmod(0o640)
        monkeypatch.delenv(fa.APIF_KEY_ENV, raising=False)
        monkeypatch.setattr(fa, "APIF_KEY_FILE", p)
        key, _source, problems = fa.load_key()
        assert key == ""
        assert any("must be 600" in x for x in problems)

    def test_a_mode_600_key_file_is_read(self, tmp_path, monkeypatch):
        p = tmp_path / "key"
        p.write_text("SECRET123\n", encoding="utf-8")
        p.chmod(0o600)
        monkeypatch.delenv(fa.APIF_KEY_ENV, raising=False)
        monkeypatch.setattr(fa, "APIF_KEY_FILE", p)
        key, _source, problems = fa.load_key()
        assert key == "SECRET123" and problems == []

    def test_the_key_is_redacted_from_error_text(self):
        """GUARD. The key travels only in a header, but a provider error
        or a requests exception can echo one back."""
        msg = fa.redact("ConnectionError: bad key SECRET123 rejected",
                        "SECRET123")
        assert "SECRET123" not in msg and fa.REDACTED in msg

    def test_free_tier_variable_is_not_a_fallback(self, tmp_path,
                                                  monkeypatch):
        """GUARD. config.API_FOOTBALL_KEY may hold the free-tier key, and
        the free plan was measured season-blind. Measuring the wrong plan
        confidently is the failure this prevents."""
        monkeypatch.delenv(fa.APIF_KEY_ENV, raising=False)
        monkeypatch.setenv("API_FOOTBALL_KEY", "FREE-TIER-KEY")
        monkeypatch.setattr(fa, "APIF_KEY_FILE", tmp_path / "absent")
        key, source, _ = fa.load_key()
        assert key == "" and source == "nowhere"


class TestSweepWindow:
    def test_covering_n_eastern_dates_needs_n_plus_one_utc_dates(self):
        """GUARD. ET date D spans UTC D 04:00 -> D+1 04:00. Without the
        trailing pad every late-evening Eastern kickoff falls out of the
        window — on the measured slate that was most of the Jul 31 card."""
        now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        assert fa.utc_dates_for(3, now, lookback_days=0) == [
            "2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01"]
        assert fa.utc_dates_for(1, now, lookback_days=0) == [
            "2026-07-29", "2026-07-30"]

    def test_the_sweep_reaches_backward_for_in_play_events(self):
        """GUARD, from a live observation at 00:02 UTC. A Kalshi event
        stays tradeable while the match is IN PLAY, so a forward-only
        window loses the still-trading book whose fixture sat on
        yesterday's Eastern date. Measured: Liverpool v Wrexham reported
        `outside_horizon` — correct, and a real coverage hole."""
        now = datetime(2026, 7, 30, 0, 2, tzinfo=timezone.utc)
        dates = fa.utc_dates_for(3, now)
        assert dates[0] == "2026-07-29", "yesterday must be swept"
        assert "26JUL29" in fa.covered_et_dates(dates), \
            "the in-play event's Eastern date must be reachable"

    def test_lookback_default_is_one_day(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        assert fa.APIF_LOOKBACK_DAYS == 1
        assert fa.utc_dates_for(3, now) == [
            "2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01",
            "2026-08-02"]

    def test_horizon_and_lookback_are_clamped(self):
        now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        assert len(fa.utc_dates_for(0, now, lookback_days=0)) == 2
        assert len(fa.utc_dates_for(99, now, lookback_days=0)) == 15
        assert len(fa.utc_dates_for(1, now, lookback_days=99)) == 2 + 7
        assert len(fa.utc_dates_for(1, now, lookback_days=-5)) == 2

    def test_late_eastern_kickoff_buckets_to_the_previous_et_date(self):
        """A 2026-07-30T02:00Z kickoff is 22:00 ET on Jul 29."""
        assert fa.et_date_of_iso("2026-07-30T02:00:00+00:00") == "26JUL29"
        assert fa.et_date_of_iso("2026-07-30T14:00:00+00:00") == "26JUL30"

    def test_an_unparseable_kickoff_is_absent_not_today(self):
        assert fa.et_date_of_iso("not-a-date") is None
        assert fa.et_date_of_iso(None) is None

    def test_a_missing_key_is_an_inability_to_look_not_an_empty_slate(
            self, tmp_path, monkeypatch):
        """GUARD. No key must NOT produce complete=True with zero
        fixtures — that would render an inability to measure as a
        measurement of an empty world."""
        monkeypatch.delenv(fa.APIF_KEY_ENV, raising=False)
        monkeypatch.setattr(fa, "APIF_KEY_FILE", tmp_path / "absent")
        out = fa.sweep(1, datetime(2026, 7, 29, tzinfo=timezone.utc))
        assert out["fixtures"] == []
        assert out["complete"] is False
        assert out["failures"] and "key" in out["failures"][0]["reason"]

    def test_an_incomplete_sweep_is_never_cached(self, monkeypatch):
        """GUARD. Caching a partial sweep would make the next read serve
        the same hole for the whole TTL."""
        calls = []

        def flaky(_session, _key, utc_date):
            calls.append(utc_date)
            return None, "HTTP 500"

        monkeypatch.setattr(fa, "load_key", lambda: ("K", "test", []))
        monkeypatch.setattr(fa, "_fetch_date", flaky)
        monkeypatch.setattr(fa, "APIF_DELAY", 0)
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        a = fa.sweep(1, now)
        b = fa.sweep(1, now)
        assert a["complete"] is False and b["complete"] is False
        n = len(fa.utc_dates_for(1, now))
        assert len(calls) == 2 * n, \
            "the failed sweep must be retried, not cached"

    def test_a_complete_sweep_is_cached(self, monkeypatch):
        calls = []

        def ok(_session, _key, utc_date):
            calls.append(utc_date)
            return [fa.parse_fixture(RAW_JUVE)], None

        monkeypatch.setattr(fa, "load_key", lambda: ("K", "test", []))
        monkeypatch.setattr(fa, "_fetch_date", ok)
        monkeypatch.setattr(fa, "APIF_DELAY", 0)
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        assert fa.sweep(1, now)["cached"] is False
        assert fa.sweep(1, now)["cached"] is True
        assert len(calls) == len(fa.utc_dates_for(1, now)), \
            "the second read must be served from cache, not refetched"

    def test_a_partial_sweep_keeps_what_it_found(self, monkeypatch):
        """A fixture found in a partial sweep still bridges; only ABSENCE
        claims are downgraded."""
        def half(_session, _key, utc_date):
            if utc_date.endswith("29"):
                return [fa.parse_fixture(RAW_JUVE)], None
            return None, "HTTP 500"

        monkeypatch.setattr(fa, "load_key", lambda: ("K", "test", []))
        monkeypatch.setattr(fa, "_fetch_date", half)
        monkeypatch.setattr(fa, "APIF_DELAY", 0)
        out = fa.sweep(1, datetime(2026, 7, 29, tzinfo=timezone.utc))
        assert len(out["fixtures"]) == 1 and out["complete"] is False

    def test_a_fixture_returned_on_two_utc_dates_is_counted_once(
            self, monkeypatch):
        """GUARD, added because mutation E01 SURVIVED without it.

        Two adjacent UTC date buckets can both carry the same fixture —
        a provider re-list, or a kickoff nudged across midnight between
        the two calls. Counting it twice inflates `fixtures_known`, which
        is a DENOMINATOR, and a denominator that overstates makes the
        coverage fraction understate. The de-dup was previously
        unexercised: removing it left the suite green."""
        def twice(_session, _key, _utc_date):
            return [fa.parse_fixture(RAW_JUVE)], None

        monkeypatch.setattr(fa, "load_key", lambda: ("K", "test", []))
        monkeypatch.setattr(fa, "_fetch_date", twice)
        monkeypatch.setattr(fa, "APIF_DELAY", 0)
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        out = fa.sweep(3, now)
        assert out["requests_used"] == len(fa.utc_dates_for(3, now)) > 1
        assert len(out["fixtures"]) == 1, \
            "the same fixture id must appear once, not once per bucket"

    def test_the_sweep_is_kickoff_ordered(self, monkeypatch):
        def rows(_session, _key, utc_date):
            if utc_date.endswith("29"):
                return [fa.parse_fixture(RAW_CASTILLA)], None   # Jul 31 18:00
            return [fa.parse_fixture(RAW_AUGSBURG)], None       # Jul 30 14:00

        monkeypatch.setattr(fa, "load_key", lambda: ("K", "test", []))
        monkeypatch.setattr(fa, "_fetch_date", rows)
        monkeypatch.setattr(fa, "APIF_DELAY", 0)
        out = fa.sweep(1, datetime(2026, 7, 29, tzinfo=timezone.utc))
        assert [f["fixture_id"] for f in out["fixtures"]] == [1583494, 1598568]

    def test_a_200_carrying_provider_errors_is_a_failure(self, monkeypatch):
        """GUARD. A 200 with an empty payload and a plan error is not an
        answer about the world."""
        class FakeResp:
            status_code = 200

            @staticmethod
            def json():
                return {"results": 0, "response": [],
                        "errors": {"token": "invalid"}}

        class FakeSession:
            @staticmethod
            def get(*_a, **_k):
                return FakeResp()

        rows, reason = fa._fetch_date(FakeSession(), "K", "2026-07-29")
        assert rows is None and "token: invalid" in reason

    def test_a_key_echoed_in_a_provider_error_is_redacted(self, monkeypatch):
        class FakeResp:
            status_code = 403

            @staticmethod
            def json():
                return {"errors": {"token": "key SUPERSECRET is invalid"}}

        class FakeSession:
            @staticmethod
            def get(*_a, **_k):
                return FakeResp()

        rows, reason = fa._fetch_date(FakeSession(), "SUPERSECRET",
                                      "2026-07-29")
        assert rows is None
        assert "SUPERSECRET" not in reason and fa.REDACTED in reason


class TestTheLeagueScopedSweepWasStructurallyBlind:
    def test_the_fixture_son_needs_is_not_in_league_667(self):
        """THE MEASURED ROOT CAUSE. Liverpool v Wrexham — the ONE
        tradeable friendly ESPN could name — is league 1022, season 2025.
        A 667+2026 sweep cannot see it, so this is recorded as a test
        rather than a comment."""
        f = fa.parse_fixture(RAW_LIVERPOOL)
        assert f["league_id"] == 1022
        assert f["league_id"] != 667
        assert f["league_season"] == 2025
        assert f["league_season"] != 2026

    def test_league_1022_is_now_classified_because_evidence_put_it_there(self):
        """1022 earned its place in FRIENDLY_LEAGUES by being measured, not
        by being guessed — which is the only way this set may grow."""
        assert fa.FRIENDLY_LEAGUES[1022] == "Premier League - Summer Series"
        assert fa.parse_fixture(RAW_LIVERPOOL)["is_classified_friendly"] is True

    def test_an_unclassified_league_still_bridges(self):
        """GUARD, the mechanism that found 1022. The bridge searches the
        WHOLE sweep, not the classified subset — so a Kalshi event can
        reach a competition this module has never heard of. Uses a league
        id deliberately absent from FRIENDLY_LEAGUES."""
        unknown = _fx(1589333, 40, "Liverpool", 1837, "Wrexham",
                      "2026-07-29T23:30:00+00:00",
                      league=99999, lname="Some Unclassified Cup",
                      season=2025)
        swept = _swept([unknown])
        assert 99999 not in fa.FRIENDLY_LEAGUES
        assert fa.friendly_fixtures(swept) == [], \
            "the display list must exclude it — the CANDIDATE POOL must not"
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL29LFCWRE",
                                  "Liverpool vs Wrexham"), swept)
        assert out["state"] == "bridged"
        assert out["fixture"]["fixture_id"] == 1589333

    def test_a_bridge_outside_the_classified_set_is_reported_as_discovery(self):
        """GUARD. A league we have not classified is a FINDING, not a row
        to drop silently. This is exactly how 1022 surfaced."""
        unknown = _fx(1589333, 40, "Liverpool", 1837, "Wrexham",
                      "2026-07-29T23:30:00+00:00",
                      league=99999, lname="Some Unclassified Cup",
                      season=2025)
        cov = fa.coverage([_ev("KXCLUBFGAME-26JUL29LFCWRE",
                               "Liverpool vs Wrexham")], _swept([unknown]))
        assert cov["events_bridged"] == 1
        assert cov["league_discoveries"], "the discovery must be reported"
        assert cov["league_discoveries"][0]["league_id"] == 99999
        assert cov["league_discoveries"][0]["league_name"] == \
            "Some Unclassified Cup"

    def test_a_bridge_inside_the_classified_set_is_not_a_discovery(self):
        """Control: the discovery list must stay empty when nothing new
        was found, or it means nothing when it is populated."""
        cov = fa.coverage([_ev("KXCLUBFGAME-26JUL30JUVNIC",
                               "Juventus vs Nice")], _swept([RAW_JUVE]))
        assert cov["events_bridged"] == 1
        assert cov["league_discoveries"] == []


class TestIdentityIsAnInteger:
    def test_reserve_side_reaches_the_reserve_id_and_never_the_senior_one(self):
        """THE LOAD-BEARING CASE. Kalshi's `Real Madrid Castilla` is
        API-Football's Real Madrid II, id 9575. Senior Real Madrid is
        541. A laxer matcher bridges this to the SENIOR club's book with
        full confidence — the exact hazard friendlies.py's P0-1 rule
        exists for."""
        side = fa.bridge_side("Real Madrid Castilla")
        assert side["apif_team_id"] == 9575
        assert side["apif_team_id"] != 541
        assert side["state"] == "recorded"

    def test_the_reserve_fixture_is_chosen_over_the_senior_one(self):
        """Both fixtures are in the same pool on the same ET date. The id
        join must land on the reserve fixture."""
        swept = _swept([RAW_CASTILLA, RAW_SENIOR_RM])
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL31ALBRMA",
                                  "Albacete vs Real Madrid Castilla"), swept)
        assert out["state"] == "bridged"
        assert out["fixture"]["fixture_id"] == 1598568
        assert out["fixture"]["away"]["apif_team_id"] == 9575

    def test_no_name_tier_can_reach_the_reserve_side_at_all(self):
        """Which is WHY it needs a recorded id: the names share no
        identity token, so every proposal tier returns None."""
        assert fa.name_tier("Real Madrid Castilla", "Real Madrid II") is None

    def test_every_bridge_entry_has_evidence(self):
        """GUARD. An entry with no archived fixture behind it is a guess,
        which is what this whole mechanism refuses."""
        for k, (tid, aname, evidence) in fa.TEAM_BRIDGE.items():
            assert isinstance(tid, int) and tid > 0, k
            assert aname, k
            assert evidence and evidence.startswith("f"), k

    def test_no_two_kalshi_sides_claim_the_same_team_id(self):
        """GUARD. Two sides mapping to one club would silently merge two
        different matches."""
        seen = {}
        for k, (tid, _n, _e) in fa.TEAM_BRIDGE.items():
            assert tid not in seen, f"{k} and {seen.get(tid)} both claim {tid}"
            seen[tid] = k

    def test_bridge_keys_are_stored_normalized(self):
        """A key that is not already normalized can never be looked up."""
        for k in fa.TEAM_BRIDGE:
            assert fa.norm_name(k) == k

    def test_an_unrecorded_side_is_explicitly_unrecorded(self):
        s = fa.bridge_side("Some Club Nobody Curated")
        assert s["state"] == "unrecorded" and s["apif_team_id"] is None

    @pytest.mark.parametrize("side", [
        "Real Madrid",            # the SENIOR club, never listed by Kalshi
        "Real Madrid B",          # a side that does not exist in the table
        "Real Madrid Castilla II",
        "Real Madrid W",
        "Liverpool U21",          # a real APIF team, deliberately unrecorded
        "Wrexham Res.",
        "Bayern Munchen II",
        "Leeds",                  # APIF's name, not Kalshi's title side
    ])
    def test_a_side_that_merely_RESEMBLES_a_recorded_club_is_refused(self,
                                                                     side):
        """GUARD, added because mutation M09 SURVIVED without it.

        The original suite proved that `Real Madrid Castilla` reaches
        9575, but every such assertion is satisfied by the table lookup
        alone. Injecting a name-similarity fallback into `bridge_side`
        therefore left the suite fully green — an UNRECORDED side that
        merely looks like a recorded club could have been resolved to the
        nearby club's id, which is the senior-vs-reserve confusion the id
        bridge exists to prevent, reintroduced one layer lower.

        `bridge_side` must be a pure table lookup: recorded, a measured
        provider absence, or unrecorded. Never a similarity judgement."""
        s = fa.bridge_side(side)
        assert s["apif_team_id"] is None, \
            f"{side!r} is not in TEAM_BRIDGE and must resolve to no id"
        assert s["state"] in ("unrecorded", "provider_absence")

    def test_bridge_side_consults_nothing_but_the_two_tables(self):
        """GUARD. Same hole from the other direction: with both tables
        emptied, EVERY side must come back unrecorded. Any id surviving
        that is coming from a similarity path that should not exist."""
        import unittest.mock as m
        with m.patch.dict(fa.TEAM_BRIDGE, {}, clear=True), \
             m.patch.dict(fa.KNOWN_PROVIDER_ABSENCES, {}, clear=True):
            for side in ("Real Madrid Castilla", "Liverpool", "Juventus",
                         "Bayern Munich", "Sevilla"):
                s = fa.bridge_side(side)
                assert s["apif_team_id"] is None, side
                assert s["state"] == "unrecorded", side

    def test_a_measured_provider_absence_says_what_was_found(self):
        """The residual must be nameable, with its evidence."""
        s = fa.bridge_side("Bayern Munich")
        assert s["state"] == "provider_absence"
        assert s["apif_team_id"] is None
        assert "157" in s["note"]


class TestNameMatchingOnlyProposes:
    def test_a_single_name_hit_without_a_recorded_id_is_proposed_not_bridged(
            self):
        """GUARD. This is the whole review finding: a name+date match must
        not be allowed to resolve a fixture."""
        stray = _fx(777, 3001, "Newtown Rangers", 3002, "Oldtown Athletic",
                    "2026-07-30T18:00:00+00:00")
        swept = _swept([stray])
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30NEWOLD",
                                  "Newtown Rangers vs Oldtown Athletic"),
                             swept)
        assert out["state"] == "proposed"
        assert out["fixture"] is None, "a proposal must not fill `fixture`"
        assert out["proposal"]["fixture"]["fixture_id"] == 777

    def test_two_name_hits_are_ambiguous_and_neither_is_picked(self):
        a = _fx(801, 3001, "Newtown Rangers", 3002, "Oldtown Athletic",
                "2026-07-30T14:00:00+00:00")
        b = _fx(802, 3003, "Newtown Rangers FC", 3004, "Oldtown Athletic FC",
                "2026-07-30T18:00:00+00:00")
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30NEWOLD",
                                  "Newtown Rangers vs Oldtown Athletic"),
                             _swept([a, b]))
        assert out["state"] == "ambiguous"
        assert out["fixture"] is None
        assert sorted(out["candidates"]) == [801, 802]

    def test_substring_containment_is_not_a_tier(self):
        """GUARD. Bare string containment must match NOTHING. `Ajax` is a
        substring of `Ajaxville` but not a token of it, so a containment
        matcher would fire and this one must not."""
        assert fa.name_tier("Ajax", "Ajaxville") is None
        assert fa.name_tier("Inter", "Interlaken") is None
        assert fa.name_tier("Union", "Reunion") is None

    def test_a_token_subset_is_loose_which_is_only_a_proposal(self):
        """`Real Madrid` vs `Real Madrid Castilla` IS a token subset, so
        it reaches the `loose` tier — and `loose` is a proposal, never a
        bridge. That is the safety property, not the tier's absence."""
        assert fa.name_tier("Real Madrid", "Real Madrid Castilla") == "loose"
        assert fa.name_tier("Madrid", "Real Madrid") == "loose"

    def test_generic_suffixes_are_ignored_but_identity_is_not(self):
        assert fa.name_tier("Sevilla", "Sevilla FC") == "tokenset"
        assert fa.name_tier("Real Madrid", "Real Madrid II") == "loose"

    def test_identity_tokens_are_never_stripped(self):
        """GUARD, from a recorded negative control: a probe run that added
        `united`, `real`, `city`, `town` and `sporting` to the generic set
        broke four bridges that had worked."""
        for t in ("united", "real", "city", "town", "sporting", "athletic",
                  "castilla", "b", "ii"):
            assert t not in fa._GENERIC_TOKENS


class TestOrientation:
    def test_a_reversed_fixture_still_bridges(self):
        """Kalshi titles `Bournemouth vs Augsburg`; API-Football has
        f1583494 as FC Augsburg v Bournemouth. Same match."""
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30BOUFCA",
                                  "Bournemouth vs Augsburg"),
                              _swept([RAW_AUGSBURG]))
        assert out["state"] == "bridged"
        assert out["fixture"]["fixture_id"] == 1583494

    def test_the_disagreement_is_recorded_never_silently_swapped(self):
        """GUARD. A consumer reading home/away off the fixture must be
        able to see that the two providers disagreed."""
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30BOUFCA",
                                  "Bournemouth vs Augsburg"),
                              _swept([RAW_AUGSBURG]))
        assert out["orientation"] == "reversed"
        assert out["fixture"]["home"]["apif_team_id"] == 170, \
            "the fixture must keep the PROVIDER's orientation, unedited"

    def test_an_agreeing_orientation_says_same(self):
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30JUVNIC",
                                  "Juventus vs Nice"), _swept([RAW_JUVE]))
        assert out["state"] == "bridged" and out["orientation"] == "same"


class TestFailClosed:
    def test_two_fixtures_with_the_same_id_pair_are_refused(self):
        """GUARD. Measured 0 collisions across 1611 fixtures, but a
        doubleheader or a provider duplicate must refuse, not pick."""
        dupe = _fx(1585005, 84, "Nice", 496, "Juventus",
                   "2026-07-30T20:00:00+00:00")
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30JUVNIC",
                                  "Juventus vs Nice"),
                              _swept([RAW_JUVE, dupe]))
        assert out["state"] == "ambiguous"
        assert out["fixture"] is None
        assert sorted(out["candidates"]) == [1585004, 1585005]

    def test_no_candidate_in_an_incomplete_sweep_is_not_an_absence(self):
        """GUARD. `unbridged` asserts the provider has no such fixture. A
        partial sweep cannot support that — the same distinction
        src/friendlies.py draws with `registry_incomplete`."""
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30JUVNIC",
                                  "Juventus vs Nice"),
                              _swept([], complete=False,
                                     failures=[{"utc_date": "2026-07-30",
                                                "reason": "HTTP 500"}]))
        assert out["state"] == "sweep_incomplete"

    def test_no_candidate_in_a_complete_sweep_is_a_real_absence(self):
        """Control, the other way: with a complete sweep the same input
        MUST read as unbridged, or the incomplete state means nothing."""
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30JUVNIC",
                                  "Juventus vs Nice"), _swept([]))
        assert out["state"] == "unbridged"
        assert "no fixture" in out["reason"]

    def test_an_event_beyond_the_horizon_is_not_reported_as_an_absence(self):
        """GUARD, from a defect caught in LIVE verification rather than by
        reasoning. `GET /api/friendlies/coverage?days=1` reported 17
        events `unbridged` with the reason "API-Football lists no fixture
        pairing them on this date" — for dates the sweep had never
        requested. That is missing evidence rendered as authority, the one
        thing this surface exists to refuse."""
        swept = _swept()
        swept["utc_dates"] = ["2026-07-29", "2026-07-30"]
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL31ALBRMA",
                                  "Albacete vs Real Madrid Castilla"), swept)
        assert out["state"] == "outside_horizon"
        assert out["state"] != "unbridged"
        assert "26JUL31" in out["reason"]
        assert "never" in out["reason"]

    def test_a_date_inside_the_horizon_still_resolves_normally(self):
        """Control: the horizon guard must not swallow real dates."""
        swept = _swept()
        swept["utc_dates"] = ["2026-07-29", "2026-07-30", "2026-07-31",
                              "2026-08-01"]
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL31ALBRMA",
                                  "Albacete vs Real Madrid Castilla"), swept)
        assert out["state"] == "bridged"

    def test_the_last_utc_bucket_is_a_partial_tail_and_is_not_covered(self):
        """ET date E needs UTC E and E+1. The final bucket has no
        successor, so its ET date is only partly fetched."""
        assert fa.covered_et_dates(
            ["2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01"]) == {
            "26JUL29", "26JUL30", "26JUL31"}
        assert fa.covered_et_dates(["2026-07-29"]) == set()
        assert fa.covered_et_dates([]) == set()

    def test_a_malformed_title_is_refused_not_split(self):
        out = fa.bridge_event(_ev("KXCLUBFGAME-26JUL30XXX",
                                  "some single name"), _swept())
        assert out["state"] == "not_a_match_title"
        assert fa.kalshi_sides("no separator here") is None
        assert fa.kalshi_sides(" vs Wrexham") is None

    def test_a_non_kxclubfgame_ticker_is_refused(self):
        out = fa.bridge_event(_ev("KXMLSGAME-26JUL30ABCDEF", "A vs B"),
                              _swept())
        assert out["state"] == "not_a_match_title"


class TestCoverageIsDenominated:
    def _events(self):
        return [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham"),
                _ev("KXCLUBFGAME-26JUL30BOUFCA", "Bournemouth vs Augsburg"),
                _ev("KXCLUBFGAME-26JUL30FCREBMU",
                    "FC Rottach-Egern vs Bayern Munich")]

    def test_every_count_ships_with_its_denominator(self):
        """GUARD, the repo rule: a count without its denominator is a
        defect. "18 bridged" alone is not acceptable output."""
        cov = fa.coverage(self._events(), _swept())
        for k in ("kalshi_open_events", "fixtures_known",
                  "fixtures_known_all_leagues", "events_bridged",
                  "events_unbridged", "fraction_bridged"):
            assert k in cov, k
        assert cov["kalshi_open_events"] == 3
        assert cov["events_bridged"] + cov["events_unbridged"] == 3

    def test_the_unbridged_events_are_named_not_counted(self):
        """GUARD. A residual Son cannot name is not an answer."""
        cov = fa.coverage(self._events(), _swept())
        titles = [u["title"] for u in cov["unbridged"]]
        assert "FC Rottach-Egern vs Bayern Munich" in titles
        for u in cov["unbridged"]:
            assert u["event_ticker"] and u["reason"], u

    def test_fraction_is_none_not_zero_when_there_is_no_market(self):
        """GUARD. An empty market is NOT 0% coverage — dividing by
        nothing and reporting 0.0 converts absence into a measurement."""
        cov = fa.coverage([], _swept())
        assert cov["fraction_bridged"] is None
        assert cov["kalshi_open_events"] == 0

    def test_fraction_is_reported_when_there_is_a_denominator(self):
        cov = fa.coverage(self._events(), _swept())
        assert cov["fraction_bridged"] == round(2 / 3, 4)

    def test_never_looked_is_counted_apart_from_is_not_there(self):
        """GUARD. A short horizon must not be able to masquerade as
        provider absence in the headline numbers."""
        swept = _swept()
        swept["utc_dates"] = ["2026-07-29", "2026-07-30"]
        cov = fa.coverage(
            [_ev("KXCLUBFGAME-26JUL29LFCWRE", "Liverpool vs Wrexham"),
             _ev("KXCLUBFGAME-26JUL31ALBRMA",
                 "Albacete vs Real Madrid Castilla")], swept)
        assert cov["kalshi_open_events"] == 2
        assert cov["events_outside_horizon"] == 1
        assert cov["kalshi_open_events_in_horizon"] == 1
        assert cov["events_bridged"] == 1
        # both fractions reported, against their own denominators
        assert cov["fraction_bridged"] == 0.5
        assert cov["fraction_bridged_in_horizon"] == 1.0

    def test_the_in_horizon_fraction_is_none_when_nothing_is_in_horizon(self):
        swept = _swept()
        swept["utc_dates"] = ["2026-07-29", "2026-07-30"]
        cov = fa.coverage([_ev("KXCLUBFGAME-26JUL31ALBRMA",
                               "Albacete vs Real Madrid Castilla")], swept)
        assert cov["kalshi_open_events_in_horizon"] == 0
        assert cov["fraction_bridged_in_horizon"] is None

    def test_the_breakdown_sums_to_the_denominator(self):
        cov = fa.coverage(self._events(), _swept())
        assert sum(cov["breakdown"].values()) == cov["kalshi_open_events"]

    def test_sweep_completeness_travels_with_the_census(self):
        """A census computed off a partial sweep must say so."""
        cov = fa.coverage(self._events(), _swept(complete=False,
                                                 failures=[{"x": 1}]))
        assert cov["sweep"]["complete"] is False
        assert cov["sweep"]["failures"]

    def test_the_framing_travels_with_the_census(self):
        cov = fa.coverage([], _swept())
        assert "no model runs on friendlies" in cov["framing_note"].lower()


class TestMarketRowsPreserveTheHonestyInvariants:
    def _patch(self, monkeypatch, events, book_rows, meta=None):
        monkeypatch.setattr(fa, "sweep", lambda *_a, **_k: _swept())
        monkeypatch.setattr(fa, "tradeable_events",
                            lambda *_a, **_k: {
                                "events": events, "listed_total": 312,
                                "complete": True, "truncated": False,
                                "cached": False})
        monkeypatch.setattr(
            friendlies, "event_markets_state",
            lambda _t: (book_rows,
                        meta or {"state": "fresh", "age_seconds": 0}))

    def test_book_fetches_are_throttled_between_bridged_rows(self,
                                                             monkeypatch):
        """GUARD, from a LIVE failure rather than a hypothesis. The first
        real `market_rows` run fetched 22 books back to back and Kalshi
        answered one with a 429. The fail-closed path was correct — that
        row read `unavailable`, not `no_open_markets` — but the refusal
        still cost a book we could have had."""
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice"),
                     _ev("KXCLUBFGAME-26JUL30BOUFCA",
                         "Bournemouth vs Augsburg"),
                     _ev("KXCLUBFGAME-26JUL31ALBRMA",
                         "Albacete vs Real Madrid Castilla")],
                    [{"ticker": "X-JUV", "status": "active",
                      "yes_ask_dollars": "0.50"}])
        out = fa.market_rows()
        bridged = sum(1 for r in out["rows"] if r["bridge_state"] == "bridged")
        assert bridged == 3
        # N fetches -> N-1 waits. Never before the first.
        assert len(sleeps) == bridged - 1
        assert all(s == fa.KALSHI_BOOK_DELAY for s in sleeps)

    def test_an_unbridged_row_costs_no_book_fetch_and_no_wait(self,
                                                              monkeypatch):
        """Control: the throttle must not fire for rows that never fetch."""
        sleeps = []
        monkeypatch.setattr(fa.time, "sleep", lambda s: sleeps.append(s))
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30FCREBMU",
                         "FC Rottach-Egern vs Bayern Munich"),
                     _ev("KXCLUBFGAME-26JUL30LUATER", "Luanda vs Teruel")],
                    [])
        out = fa.market_rows()
        assert all(r["bridge_state"] == "unbridged" for r in out["rows"])
        assert sleeps == []

    def test_an_unbridged_row_renders_no_price(self, monkeypatch):
        """GUARD. Ambiguous or unresolved identity must render nothing."""
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30FCREBMU",
                         "FC Rottach-Egern vs Bayern Munich")], [])
        out = fa.market_rows()
        row = out["rows"][0]
        assert row["bridge_state"] == "unbridged"
        assert row["book"] is None and row["implied"] is None
        assert row["book_state"] == "not_bridged"
        assert row["bridge_reason"]

    def test_a_failed_book_fetch_is_unavailable_not_closed(self, monkeypatch):
        """GUARD, inherited P0-2: "we couldn't look" is not "no market"."""
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    None, {"state": "missing", "age_seconds": None})
        row = fa.market_rows()["rows"][0]
        assert row["bridge_state"] == "bridged"
        assert row["book_state"] == "unavailable"
        assert row["book"] is None
        assert "could not look" in row["implied_unavailable_reason"]

    def test_an_empty_book_is_no_open_markets_and_keeps_its_own_wording(
            self, monkeypatch):
        """The two failure shapes must stay distinguishable."""
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    [])
        row = fa.market_rows()["rows"][0]
        assert row["book_state"] == "no_open_markets"
        assert row["book_state"] != "unavailable"

    def test_a_stale_book_is_served_visibly_stale_with_its_age(
            self, monkeypatch):
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    [{"ticker": "KXCLUBFGAME-26JUL30JUVNIC-JUV",
                      "status": "active", "yes_ask_dollars": "0.50"}],
                    {"state": "stale", "age_seconds": 42})
        row = fa.market_rows()["rows"][0]
        assert row["freshness"] == {"state": "stale", "age_seconds": 42}

    def test_a_fresh_book_carries_no_stale_marker(self, monkeypatch):
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    [{"ticker": "KXCLUBFGAME-26JUL30JUVNIC-JUV",
                      "status": "active", "yes_ask_dollars": "0.50"}])
        assert fa.market_rows()["rows"][0]["freshness"] is None

    def test_the_espn_framing_is_served_verbatim(self, monkeypatch):
        self._patch(monkeypatch, [], [])
        assert fa.market_rows()["framing"] == friendlies.FRAMING

    def test_the_response_carries_the_denominated_census(self, monkeypatch):
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    [])
        out = fa.market_rows()
        assert out["coverage"]["kalshi_open_events"] == 1
        assert out["kalshi_registry"]["listed_total"] == 312

    def test_the_implied_block_is_used_when_present(self, monkeypatch):
        """The market-implied block lives on another branch. When it is
        present it must be called for a bridged, open book."""
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    [{"ticker": "KXCLUBFGAME-26JUL30JUVNIC-JUV",
                      "status": "active", "yes_ask_dollars": "0.50"}])
        monkeypatch.setattr(friendlies, "market_implied",
                            lambda *_a, **_k: {"state": "priced"},
                            raising=False)
        row = fa.market_rows()["rows"][0]
        assert row["implied"] == {"state": "priced"}
        assert row["implied_unavailable_reason"] is None

    def test_its_absence_is_stated_in_words_not_silently_omitted(
            self, monkeypatch):
        """GUARD. A missing field reads as "no data"; a stated reason
        reads as "not in this build"."""
        self._patch(monkeypatch,
                    [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                    [{"ticker": "KXCLUBFGAME-26JUL30JUVNIC-JUV",
                      "status": "active", "yes_ask_dollars": "0.50"}])
        monkeypatch.delattr(friendlies, "market_implied", raising=False)
        row = fa.market_rows()["rows"][0]
        assert "implied" in row and row["implied"] is None
        assert "not present in this build" in row["implied_unavailable_reason"]


class TestTradeableRegistry:
    def test_only_events_with_a_tradeable_market_count_as_open(self,
                                                               monkeypatch):
        """GUARD. `listed` counts every event Kalshi has ever listed;
        `open_events` is the denominator Son actually trades against."""
        payload = {"events": [
            _ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice",
                ("active",)),
            _ev("KXCLUBFGAME-26JUN01OLDGAME", "Old vs Game",
                ("finalized",)),
        ], "cursor": None}
        monkeypatch.setattr(friendlies, "_get_json",
                            lambda *_a, **_k: payload)
        reg = fa.tradeable_events()
        assert reg["listed_total"] == 2
        assert len(reg["events"]) == 1
        assert reg["events"][0]["title"] == "Juventus vs Nice"

    def test_a_failed_registry_page_is_incomplete_and_uncached(self,
                                                               monkeypatch):
        calls = []

        def fail(*_a, **_k):
            calls.append(1)
            return None

        monkeypatch.setattr(friendlies, "_get_json", fail)
        assert fa.tradeable_events()["complete"] is False
        assert fa.tradeable_events()["complete"] is False
        assert len(calls) == 2, "an incomplete registry must be retried"

    def test_the_pager_follows_the_cursor(self, monkeypatch):
        pages = [
            {"events": [_ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
             "cursor": "c2"},
            {"events": [_ev("KXCLUBFGAME-26JUL30BOUFCA",
                            "Bournemouth vs Augsburg")], "cursor": None},
        ]
        seen = []

        def paged(_url, params):
            seen.append(params.get("cursor"))
            return pages[len(seen) - 1]

        monkeypatch.setattr(friendlies, "_get_json", paged)
        monkeypatch.setattr(fa.time, "sleep", lambda _s: None)
        reg = fa.tradeable_events()
        assert seen == [None, "c2"]
        assert len(reg["events"]) == 2 and reg["truncated"] is False

    def test_a_capped_pager_reports_truncated(self, monkeypatch):
        monkeypatch.setattr(
            friendlies, "_get_json",
            lambda *_a, **_k: {"events": [
                _ev("KXCLUBFGAME-26JUL30JUVNIC", "Juventus vs Nice")],
                "cursor": "always-more"})
        monkeypatch.setattr(fa.time, "sleep", lambda _s: None)
        reg = fa.tradeable_events(max_pages=2)
        assert reg["truncated"] is True


class TestRequestBudget:
    def test_a_sweep_costs_one_request_per_utc_date(self, monkeypatch):
        """The plan is 7500/day and 300/min. The defaults — 1 day of
        lookback plus a 3-day horizon — are 5 requests; at the default
        600s TTL that is 720/day, 9.6%. The budget assertion is derived,
        not a literal, so widening the window cannot silently outgrow the
        plan without turning this red."""
        calls = []
        monkeypatch.setattr(fa, "load_key", lambda: ("K", "test", []))
        monkeypatch.setattr(fa, "APIF_DELAY", 0)
        monkeypatch.setattr(fa, "_fetch_date",
                            lambda _s, _k, d: (calls.append(d), ([], None))[1])
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        out = fa.sweep(3, now)
        n = len(fa.utc_dates_for(3, now))
        assert n == 5, "1 lookback + 3 horizon + 1 trailing pad"
        assert len(calls) == n and out["requests_used"] == n
        per_day = (86400 / fa.APIF_SWEEP_TTL) * n
        assert per_day < fa.REQUEST_BUDGET["per_day"], (
            f"{per_day}/day exceeds the plan's "
            f"{fa.REQUEST_BUDGET['per_day']}")
        assert n < fa.REQUEST_BUDGET["per_minute"]


class TestHubIsUsable:
    """The three things Son reported on 2026-07-30: a table full of
    yesterday's results, no Kalshi, and 399 undifferentiated rows."""

    def _swept(self, statuses):
        """A canned sweep with one fixture per given (status, iso) pair."""
        return {"fixtures": [
            {"fixture_id": 1000 + i, "kickoff_utc": iso, "status": st,
             "is_classified_friendly": True, "league_name": "Friendlies Clubs",
             "league_country": "World", "round": "Club Friendlies",
             "venue": None, "venue_city": None,
             "home": {"apif_team_id": i, "name": f"H{i}", "crest": None},
             "away": {"apif_team_id": 900 + i, "name": f"A{i}", "crest": None},
             "goals": {"home": None, "away": None}}
            for i, (st, iso) in enumerate(statuses)],
            "utc_dates": [], "complete": True, "failures": [],
            "requests_used": 0, "key_source": "test", "problems": []}

    def test_finished_and_past_are_hidden_by_default(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        import src.friendlies_apif as m
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=6)).isoformat()
        soon = (now + timedelta(hours=6)).isoformat()
        swept = self._swept([("FT", past), ("CANC", soon),
                             ("NS", past), ("NS", soon)])
        monkeypatch.setattr(m, "sweep", lambda *a, **k: swept)
        monkeypatch.setattr(m, "tradeable_events",
                            lambda *a, **k: {"events": [], "listed_total": 0,
                                             "complete": True,
                                             "truncated": False})
        monkeypatch.setattr(m, "_strength_for", lambda f: {"available": False})
        out = m.fixture_rows(2)
        # only the future NS fixture survives: FT is over, CANC will never
        # happen, and a past NS kickoff is stale even without a final status
        assert out["count"] == 1
        assert out["fixtures"][0]["status"] == "NS"
        assert out["fixtures"][0]["kickoff_utc"] == soon
        assert out["finished_hidden"] == 3

    def test_include_finished_is_the_escape_hatch(self, monkeypatch):
        """The CONTROL: the rows are filtered, not lost."""
        from datetime import datetime, timedelta, timezone
        import src.friendlies_apif as m
        now = datetime.now(timezone.utc)
        swept = self._swept([("FT", (now - timedelta(hours=6)).isoformat()),
                             ("NS", (now + timedelta(hours=6)).isoformat())])
        monkeypatch.setattr(m, "sweep", lambda *a, **k: swept)
        monkeypatch.setattr(m, "tradeable_events",
                            lambda *a, **k: {"events": [], "listed_total": 0,
                                             "complete": True,
                                             "truncated": False})
        monkeypatch.setattr(m, "_strength_for", lambda f: {"available": False})
        assert m.fixture_rows(2, include_finished=True)["count"] == 2

    def test_tradeable_events_with_no_fixture_are_still_surfaced(
            self, monkeypatch):
        """"Show me every Kalshi market" cannot quietly drop an event just
        because our fixture feed lacks the club. Measured 2026-07-30: two
        such events exist (Schwarz-Weiss Essen and FC Rottach-Egern are
        genuinely absent from API-Football, per KNOWN_PROVIDER_ABSENCES)."""
        import src.friendlies_apif as m
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        monkeypatch.setattr(m, "sweep", lambda *a, **k: self._swept(
            [("NS", soon)]))
        monkeypatch.setattr(m, "tradeable_events", lambda *a, **k: {
            "events": [{"event_ticker": "KX-ORPHAN", "title": "X vs Y"}],
            "listed_total": 312, "complete": True, "truncated": False})
        monkeypatch.setattr(m, "_bridge_by_fixture_id", lambda s, e: {})
        monkeypatch.setattr(m, "_strength_for", lambda f: {"available": False})
        out = m.fixture_rows(2)
        assert len(out["kalshi_only"]) == 1
        assert out["kalshi_only"][0]["event_ticker"] == "KX-ORPHAN"
        # and the two Kalshi counts stay APART: 312 listed is mostly settled
        assert out["kalshi_listed_total"] == 312
        assert out["kalshi_tradeable_total"] == 1
        assert "settled" in out["kalshi_counts_note"].lower()
