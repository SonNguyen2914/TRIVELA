"""Team-news ingestion behaviour: the states, the words, the transition.

Every test here pins a distinction that a simpler implementation would
collapse. The collapses are not hypothetical — each one has a cost
recorded in this repo:

  empty vs unavailable      `results: 0` from API-Football was read as
                            "no match on" for a fixture that was live at
                            the 45th minute (src/live_feed.py:221).
  zero vs never-measured    `{"created": 0}` hid every paper fill for a
                            night behind a full production volume.
  container vs names        both providers ship lineup containers with no
                            players in them (measured 2026-07-30), so
                            counting arrays invents coverage.
  released vs unreleased    an absent XI read as an absence claim is the
                            defect the frontend's contract test already
                            pins from the other side.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.live import team_news
from src.live.models import (TeamAbsence, TeamNewsCapture, TeamNewsEvent,
                             TeamNewsLineup)
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc

# The measured ESPN shape. `rosters` present with TWO containers and ZERO
# named players — Atletico Madrid v Getafe, status FT, 2026-07-30, archived
# in research_archive/team_news_coverage_2026-07-30.json.
ESPN_EMPTY_CONTAINERS = {
    "header": {"competitions": [{"competitors": [
        {"homeAway": "home", "team": {"displayName": "Atletico Madrid"}},
        {"homeAway": "away", "team": {"displayName": "Getafe"}}]}]},
    "rosters": [{"homeAway": "home", "formation": None, "roster": []},
                {"homeAway": "away", "formation": None, "roster": []}],
}


def _espn_released(n_home=11, n_away=11):
    """A released XI in ESPN's shape, as club.friendly serves it."""
    def side(who, name, n):
        return {"homeAway": who, "formation": "4-2-3-1", "roster": [
            {"starter": True, "jersey": str(i + 1),
             "position": {"abbreviation": "G" if i == 0 else "M"},
             "athlete": {"id": f"{who}{i}", "displayName": f"{name} P{i}"}}
            for i in range(n)]}
    return {
        "header": {"competitions": [{"competitors": [
            {"homeAway": "home", "team": {"displayName": "Liverpool"}},
            {"homeAway": "away", "team": {"displayName": "Wrexham"}}]}]},
        "rosters": [side("home", "LIV", n_home), side("away", "WRX", n_away)],
    }


# API-Football's measured friendly shape: a NON-EMPTY array whose side
# objects carry no formation and no startXI.
APIF_EMPTY_CONTAINERS = [
    {"team": {"id": 1, "name": "Derby"}, "formation": None,
     "startXI": [], "substitutes": []},
    {"team": {"id": 2, "name": "Valencia"}, "formation": None,
     "startXI": [], "substitutes": []},
]

APIF_INJURIES = [
    {"player": {"id": 501, "name": "Star Striker", "type": "Missing Fixture",
                "reason": "Knee Injury"},
     "team": {"id": 11, "name": "New York City FC"}},
    {"player": {"id": 502, "name": "Backup Keeper", "type": "Questionable",
                "reason": "Illness"},
     "team": {"id": 11, "name": "New York City FC"}},
]


class TestTheThreeLineupStatesNeverCollapse:

    def test_present_but_empty_espn_rosters_are_not_released(self):
        """rosters=2, named=0 is NOT RELEASED — never 'no players are
        playing', never an absence claim. Counting containers here would
        have reported a completed fixture as covered."""
        st = team_news.espn_lineup_states(ESPN_EMPTY_CONTAINERS)
        for side in ("home", "away"):
            assert st[side]["container_present"] is True
            assert st[side]["named"] == 0
            assert st[side]["released"] is False
            assert team_news._lineup_state_word(
                st[side]["container_present"],
                st[side]["released"]) == "not_released"

    def test_a_missing_roster_container_is_no_coverage_not_unreleased(self):
        """Absent container and empty container are different answers: one
        means this provider carries nothing for the fixture, the other that
        the XI has not landed yet."""
        st = team_news.espn_lineup_states({"rosters": []})
        assert st["home"]["container_present"] is False
        assert team_news._lineup_state_word(False, False) == "no_coverage"

    def test_a_full_espn_xi_is_released(self):
        st = team_news.espn_lineup_states(_espn_released())
        assert st["home"]["released"] is True
        assert st["home"]["named"] == 11
        assert st["home"]["formation"] == "4-2-3-1"
        assert st["home"]["team_name"] == "Liverpool"

    def test_ten_named_starters_is_not_a_released_xi(self):
        """The threshold is 11, matching src/live/lineups.py's `confirmed`
        so two modules cannot disagree about what released means."""
        st = team_news.espn_lineup_states(_espn_released(n_home=10))
        assert st["home"]["released"] is False
        assert st["away"]["released"] is True

    def test_apifootball_empty_containers_are_not_released(self):
        """A NON-EMPTY response array whose sides carry no XI. 4 of 5
        completed friendlies looked exactly like this on 2026-07-30, so an
        array-length check would have claimed 80% coverage."""
        assert len(APIF_EMPTY_CONTAINERS) == 2        # the array is not empty
        st = team_news.apifootball_lineup_states(APIF_EMPTY_CONTAINERS)
        for side in ("home", "away"):
            assert st[side]["container_present"] is True
            assert st[side]["named"] == 0
            assert st[side]["released"] is False

    def test_apifootball_full_xi_is_released(self):
        items = [{"team": {"id": 1, "name": "Alpha"}, "formation": "4-3-3",
                  "startXI": [{"player": {"id": i, "name": f"P{i}"}}
                              for i in range(11)]}]
        st = team_news.apifootball_lineup_states(items)
        assert st["home"]["released"] is True
        assert st["away"]["container_present"] is False    # only one side sent

    def test_a_side_object_with_unnamed_players_is_not_released(self):
        """Eleven startXI entries, none carrying a name. The leaves are
        what count."""
        items = [{"team": {"id": 1, "name": "Alpha"}, "formation": "4-3-3",
                  "startXI": [{"player": {"id": i, "name": None}}
                              for i in range(11)]}]
        st = team_news.apifootball_lineup_states(items)
        assert st["home"]["named"] == 0
        assert st["home"]["released"] is False


class TestFreshnessStatesAreDistinguishable:

    def test_never_captured_is_its_own_state(self):
        f = team_news.freshness("absences", None, "unavailable")
        assert f["state"] == "never_captured"
        assert f["age_seconds"] is None
        assert f["stale"] is None

    def test_unavailable_says_unknown_not_empty(self):
        f = team_news.freshness("absences", datetime.now(UTC), "unavailable")
        assert f["state"] == "unavailable"
        assert "UNKNOWN" in f["means"]

    def test_empty_is_not_a_claim_that_the_list_is_truly_empty(self):
        f = team_news.freshness("absences", datetime.now(UTC), "empty")
        assert f["state"] == "empty"
        assert "not a claim" in f["means"]

    def test_a_stale_read_says_stale(self):
        old = datetime.now(UTC) - timedelta(seconds=400)
        f = team_news.freshness("events", old, "ok")     # events: 120s
        assert f["state"] == "stale"
        assert f["stale"] is True
        assert f["raw_state"] == "ok"      # the underlying read is preserved

    def test_a_fresh_read_is_ok(self):
        f = team_news.freshness("events", datetime.now(UTC), "ok")
        assert f["state"] == "ok"
        assert f["stale"] is False

    def test_staleness_never_overrides_unavailable(self):
        """An old failure is still a failure, not a stale reading: there is
        no reading to be stale."""
        old = datetime.now(UTC) - timedelta(days=2)
        f = team_news.freshness("events", old, "unavailable")
        assert f["state"] == "unavailable"

    def test_all_four_states_are_mutually_distinct(self):
        now = datetime.now(UTC)
        states = {
            team_news.freshness("events", None, "unavailable")["state"],
            team_news.freshness("events", now, "unavailable")["state"],
            team_news.freshness("events", now, "empty")["state"],
            team_news.freshness("events", now - timedelta(hours=1),
                                "ok")["state"],
            team_news.freshness("events", now, "ok")["state"],
        }
        assert states == {"never_captured", "unavailable", "empty", "stale",
                          "ok"}


class TestUnavailableIsNeverStoredAsZero:

    def test_fetched_count_is_none_when_unavailable(self):
        assert team_news.Fetched("unavailable").count is None
        assert team_news.Fetched("empty", []).count == 0
        assert team_news.Fetched("ok", [1, 2]).count == 2

    def test_the_capture_row_stores_null_not_zero(self, live_session):
        team_news.capture_absences("777001", items=[])
        row = (live_session.query(TeamNewsCapture)
               .filter_by(subject_ref="777001", feed="absences").one())
        assert row.state == "empty"
        assert row.record_count == 0        # we SAW an empty list

        # now a genuine failure on the same subject
        team_news._record_capture(
            live_session, team_news.PROVIDER_APIFOOTBALL, "777001",
            "absences", team_news.Fetched("unavailable", note="HTTP 500"))
        live_session.commit()
        row = (live_session.query(TeamNewsCapture)
               .filter_by(subject_ref="777001", feed="absences").one())
        assert row.state == "unavailable"
        assert row.record_count is None, (
            "an unavailable fetch stored 0 — that is a claim we saw an "
            "empty list, and this repo has already paid for that reading")

    def test_a_non_numeric_provider_count_is_absent_not_zero(self):
        """A provider `results` field that is not an int is recorded as
        absent rather than coerced."""
        f = team_news.Fetched("ok", [1], declared=None)
        assert f.declared is None


class TestProviderWordsAreVerbatim:

    def test_type_and_reason_survive_ingestion_unchanged(self, live_session):
        team_news.capture_absences("777002", competition="mls-2026",
                                   items=APIF_INJURIES)
        rows = {r.player_name: r for r in
                live_session.query(TeamAbsence).filter_by(
                    subject_ref="777002")}
        assert rows["Star Striker"].provider_type == "Missing Fixture"
        assert rows["Star Striker"].provider_reason == "Knee Injury"
        assert rows["Backup Keeper"].provider_type == "Questionable"
        assert rows["Backup Keeper"].provider_reason == "Illness"

    def test_no_severity_or_impact_is_derived(self, live_session):
        team_news.capture_absences("777003", items=APIF_INJURIES)
        cols = {c.name for c in TeamAbsence.__table__.columns}
        assert not (cols & {"severity", "impact", "weight", "score",
                            "expected_minutes_lost"})

    def test_the_read_surface_labels_the_words_as_the_providers(
            self, live_session):
        team_news.capture_absences("777004", items=APIF_INJURIES)
        out = team_news.fixture_news("777004")
        assert "provider" in out["_provider_words"]
        rec = out["absences"]["records"][0]
        assert "provider_type" in rec and "provider_reason" in rec
        assert rec["provider"] == "apifootball"

    def test_an_absence_needs_no_provider_player_id(self, live_session):
        """The uniqueness key falls back to a normalised name, because on
        PostgreSQL a NULL key would make every row distinct and admit
        duplicates that SQLite's suite would never show."""
        items = [{"player": {"id": None, "name": "Nameless Nine",
                             "type": "Missing Fixture", "reason": "Knock"},
                  "team": {"id": 3, "name": "Alpha"}}]
        team_news.capture_absences("777005", items=items)
        team_news.capture_absences("777005", items=items)      # re-ingest
        rows = live_session.query(TeamAbsence).filter_by(
            subject_ref="777005").all()
        assert len(rows) == 1, "a re-ingest duplicated a keyless absence"
        assert rows[0].player_key == "name:namelessnine"


class TestEmptyIsNotNobodyInjured:

    def test_an_empty_absence_list_says_so_explicitly(self, live_session):
        team_news.capture_absences("777006", items=[])
        out = team_news.fixture_news("777006")
        assert out["absences"]["records"] == []
        assert out["absences"]["freshness"]["state"] == "empty"
        assert "NOT evidence" in out["absences"][
            "_empty_is_not_nobody_injured"]

    def test_a_never_fetched_fixture_is_not_reported_as_clean(
            self, live_session):
        out = team_news.fixture_news("777007")
        assert out["absences"]["freshness"]["state"] == "never_captured"
        assert out["absences"]["record_count"] is None


class TestTheReleaseTransition:

    def test_first_released_at_is_recorded_when_the_xi_lands(
            self, live_session):
        ko = datetime.now(UTC) + timedelta(minutes=75)
        # before: containers exist, no XI
        r1 = team_news.capture_friendly_lineup(
            "401896388", kickoff_utc=ko, summary=ESPN_EMPTY_CONTAINERS)
        assert r1["sides"]["home"]["lineup_state"] == "not_released"
        assert r1["sides"]["home"]["first_released_at"] is None

        # then: the XI drops — the information event
        r2 = team_news.capture_friendly_lineup(
            "401896388", kickoff_utc=ko, summary=_espn_released())
        assert r2["sides"]["home"]["lineup_state"] == "released"
        assert r2["sides"]["home"]["newly_released"] is True
        assert r2["sides"]["home"]["first_released_at"] is not None
        mins = r2["sides"]["home"]["released_minutes_before_kickoff"]
        assert 70 <= mins <= 76, mins

    def test_the_transition_clock_is_never_moved_by_a_later_capture(
            self, live_session):
        ko = datetime.now(UTC) + timedelta(minutes=60)
        team_news.capture_friendly_lineup("401896389", kickoff_utc=ko,
                                          summary=_espn_released())
        row = (live_session.query(TeamNewsLineup)
               .filter_by(subject_ref="401896389", side="home").one())
        first, mins = row.first_released_at, \
            row.released_minutes_before_kickoff

        # a later re-read, and a RESCHEDULE that moves kickoff
        later_ko = ko + timedelta(hours=3)
        r = team_news.capture_friendly_lineup("401896389",
                                              kickoff_utc=later_ko,
                                              summary=_espn_released())
        live_session.expire_all()
        row = (live_session.query(TeamNewsLineup)
               .filter_by(subject_ref="401896389", side="home").one())
        assert row.first_released_at == first, "the transition clock moved"
        assert row.released_minutes_before_kickoff == mins, (
            "a reschedule retroactively changed how early the news landed")
        assert r["sides"]["home"]["newly_released"] is False

    def test_provider_is_part_of_lineup_identity(self, live_session):
        """ESPN and API-Football rows for the same fixture coexist and stay
        attributable — pooling them would make coverage unreadable."""
        team_news.capture_friendly_lineup("401896390",
                                          summary=_espn_released())
        team_news.capture_league_lineup("401896390",
                                        items=APIF_EMPTY_CONTAINERS)
        out = team_news.fixture_news("401896390")
        by = out["lineup"]["by_provider"]
        assert set(by) == {"espn", "apifootball"}
        assert by["espn"]["sides"]["home"]["lineup_state"] == "released"
        assert by["apifootball"]["sides"]["home"]["lineup_state"] == \
            "not_released"

    def test_an_unreleased_lineup_carries_no_absence_claim(self, live_session):
        """The server-side half of the frontend's contract test: with no XI
        released, nothing in the payload asserts anyone is out."""
        team_news.capture_friendly_lineup("401896391",
                                          summary=ESPN_EMPTY_CONTAINERS)
        out = team_news.fixture_news("401896391")
        home = out["lineup"]["by_provider"]["espn"]["sides"]["home"]
        assert home["lineup_state"] == "not_released"
        assert home["released"] is False
        # no absences were ingested, so none may be implied
        assert out["absences"]["records"] == []
        assert "never" in out["lineup"]["_unreleased_licenses_nothing"]


class TestEventsCarryBothClocks:

    def test_provider_minute_and_capture_time_are_both_stored(
            self, live_session):
        items = [{"time": {"elapsed": 90, "extra": 4},
                  "team": {"id": 7, "name": "Alpha"},
                  "player": {"id": 22, "name": "Late Scorer"},
                  "assist": {"id": 23, "name": "Provider"},
                  "type": "Goal", "detail": "Normal Goal"}]
        team_news.capture_events("777008", items=items)
        row = live_session.query(TeamNewsEvent).filter_by(
            subject_ref="777008").one()
        assert row.provider_minute == 90
        # 90+4 is not minute 94 in the provider's model, so extra is kept
        # apart rather than folded in
        assert row.provider_minute_extra == 4
        assert row.captured_at is not None
        assert row.event_type == "Goal"
        assert row.event_detail == "Normal Goal"
        assert row.assist_name == "Provider"

    def test_re_reading_a_live_feed_refreshes_and_never_duplicates(
            self, live_session):
        items = [{"time": {"elapsed": 61}, "team": {"id": 7, "name": "A"},
                  "player": {"id": 9, "name": "Booked"},
                  "type": "Card", "detail": "Yellow Card"}]
        team_news.capture_events("777009", items=items)
        team_news.capture_events("777009", items=items)
        team_news.capture_events("777009", items=items)
        assert live_session.query(TeamNewsEvent).filter_by(
            subject_ref="777009").count() == 1

    def test_a_red_card_is_stored_verbatim_and_not_interpreted(
            self, live_session):
        items = [{"time": {"elapsed": 33}, "team": {"id": 7, "name": "A"},
                  "player": {"id": 9, "name": "Dismissed"},
                  "type": "Card", "detail": "Red Card"}]
        team_news.capture_events("777010", items=items)
        row = live_session.query(TeamNewsEvent).filter_by(
            subject_ref="777010").one()
        assert (row.event_type, row.event_detail) == ("Card", "Red Card")
        cols = {c.name for c in TeamNewsEvent.__table__.columns}
        # no derived match-state or advantage field: interpreting a red
        # card is the detector's job on its own branch, not this module's
        assert not (cols & {"red_card_count", "man_advantage", "impact"})


class TestRetractionsAreVisibleNotDeleted:

    def test_a_record_the_provider_stopped_reporting_is_flagged(
            self, live_session):
        team_news.capture_absences("777011", items=APIF_INJURIES)
        # the provider drops one name on the next read
        team_news.capture_absences("777011", items=APIF_INJURIES[:1])
        out = team_news.fixture_news("777011")
        by = {r["player_name"]: r for r in out["absences"]["records"]}
        assert by["Star Striker"]["still_reported"] is True
        assert by["Backup Keeper"]["still_reported"] is False, (
            "a dropped record must be flagged, not silently deleted — a "
            "retraction is information too")
        assert len(out["absences"]["records"]) == 2


class TestDormantPlaneIsNotAnEmptyResult:

    def test_capture_reports_dormant_rather_than_empty(self, monkeypatch):
        monkeypatch.setattr(team_news, "plane_ready", lambda: False)
        for fn, args in ((team_news.capture_absences, ("1",)),
                         (team_news.capture_events, ("1",)),
                         (team_news.capture_friendly_lineup, ("1",)),
                         (team_news.capture_league_lineup, ("1",))):
            out = fn(*args)
            assert out["state"] == "plane_dormant"
            assert "not an empty result" in out["means"]

    def test_the_read_surface_reports_dormant(self, monkeypatch):
        monkeypatch.setattr(team_news, "plane_ready", lambda: False)
        out = team_news.fixture_news("1")
        assert out["availability"]["state"] == "plane_dormant"
        assert "absences" not in out


class TestCoverageCarriesItsDenominator:

    def test_coverage_reports_states_with_a_denominator(self, live_session):
        team_news.capture_friendly_lineup("801", competition="club-friendly",
                                          summary=_espn_released())
        team_news.capture_friendly_lineup("802", competition="club-friendly",
                                          summary=ESPN_EMPTY_CONTAINERS)
        team_news.capture_friendly_lineup("803", competition="club-friendly",
                                          summary={"rosters": []})
        cov = team_news.coverage()
        fr = cov["by_source"]["espn"]["lineups_by_competition"][
            "club-friendly"]
        assert fr == {"released": 1, "not_released": 1, "no_coverage": 1,
                      "total": 3}
        assert "denominator" in cov["_denominator_note"]

    def test_a_half_released_fixture_never_counts_as_released(
            self, live_session):
        """One side named, the other not. Partial is not released."""
        team_news.capture_friendly_lineup(
            "804", competition="club-friendly",
            summary=_espn_released(n_home=11, n_away=0))
        cov = team_news.coverage()
        fr = cov["by_source"]["espn"]["lineups_by_competition"][
            "club-friendly"]
        assert fr["released"] == 0
        assert fr["not_released"] == 1

    def test_budget_is_labelled_as_a_courtesy_counter(self, live_session):
        cov = team_news.coverage()
        assert "NOT an accounting record" in cov["budget"]["_note"]


class TestParsersToleratePoorShapes:

    @pytest.mark.parametrize("body", [None, {}, {"response": None},
                                      {"response": "nope"}, "string",
                                      {"rosters": None}])
    def test_no_shape_raises(self, body):
        """A provider shape we did not expect must yield ABSENCE, not an
        exception and not a fabricated record."""
        assert team_news._response_items(body) == []
        if isinstance(body, dict) or body is None:
            st = team_news.espn_lineup_states(body or {})
            assert st["home"]["container_present"] is False

    def test_a_non_dict_item_is_skipped_not_counted(self):
        assert team_news.parse_absences(["nope", None, 5]) == []
        assert team_news.parse_events([None, "x"]) == []

    def test_a_dict_response_is_not_counted_as_one_item(self):
        """The `status` endpoint returns an object. Counting it as one
        element would invent a record out of a shape mismatch."""
        assert team_news._response_items({"response": {"a": 1}}) == []


# ===========================================================================
# The read surface. GET only, additive, and it must never turn an absent
# section into a reassuring one.
# ===========================================================================

class TestTheNewsRoute:

    @pytest.fixture()
    def client(self, monkeypatch, live_session):
        from fastapi.testclient import TestClient

        import config as _config
        from api import main as api_main
        monkeypatch.setattr(_config, "DEMO_MODE", True)
        api_main._rate_last.clear()
        return TestClient(api_main.app)

    def test_it_serves_absences_with_provenance_and_freshness(self, client,
                                                              live_session):
        team_news.capture_absences("777100", competition="mls-2026",
                                   items=APIF_INJURIES)
        r = client.get("/api/news/fixture/777100")
        assert r.status_code == 200
        body = r.json()
        # keyed by name, not by index: the surface sorts by player name and
        # an index-based assertion would pass or fail on the sort order
        by = {r_["player_name"]: r_ for r_ in body["absences"]["records"]}
        rec = by["Star Striker"]
        assert rec["provider_type"] == "Missing Fixture"
        assert rec["provider_reason"] == "Knee Injury"
        assert rec["captured_at"] and rec["provider"] == "apifootball"
        assert by["Backup Keeper"]["provider_type"] == "Questionable"
        f = body["absences"]["freshness"]
        assert f["state"] == "ok" and f["age_seconds"] is not None

    def test_an_unreleased_lineup_is_labelled_not_released(self, client,
                                                           live_session):
        team_news.capture_friendly_lineup("777101",
                                          summary=ESPN_EMPTY_CONTAINERS)
        body = client.get("/api/news/fixture/777101").json()
        home = body["lineup"]["by_provider"]["espn"]["sides"]["home"]
        assert home["lineup_state"] == "not_released"
        assert home["released"] is False
        assert home["named_count"] == 0

    def test_a_never_captured_fixture_is_not_served_as_clean(self, client,
                                                             live_session):
        body = client.get("/api/news/fixture/777102").json()
        assert body["absences"]["freshness"]["state"] == "never_captured"
        assert body["absences"]["record_count"] is None
        assert body["lineup"]["by_provider"] == {}

    def test_an_unresolvable_ref_says_so_rather_than_inventing_a_fixture(
            self, client, live_session):
        body = client.get("/api/news/fixture/777103").json()
        assert body["resolved_fixture"]["fixture_id"] is None
        assert "by design" in body["resolved_fixture"]["note"]

    def test_the_payload_states_it_is_not_a_model_input(self, client,
                                                       live_session):
        body = client.get("/api/news/fixture/777104").json()
        assert "not significant" in body["_not_a_model_input"]

    def test_a_non_numeric_ref_is_404_not_a_lookup(self, client):
        assert client.get("/api/news/fixture/abc").status_code == 404
        assert client.get("/api/news/fixture/" + "9" * 40).status_code == 404

    def test_the_route_is_get_only(self, client):
        for verb in ("post", "put", "patch", "delete"):
            r = getattr(client, verb)("/api/news/fixture/777105")
            assert r.status_code in (404, 405), (
                f"{verb.upper()} reached the news route — it must be "
                f"read-only")

    def test_coverage_reports_states_and_a_denominator(self, client,
                                                      live_session):
        team_news.capture_friendly_lineup("777106",
                                          competition="club-friendly",
                                          summary=_espn_released())
        body = client.get("/api/news/coverage").json()
        assert body["by_source"]["espn"]["lineups_by_competition"][
            "club-friendly"]["released"] == 1
        assert body["by_source"]["espn"]["fixtures_attempted"] == 1
        assert "denominator" in body["_denominator_note"]

    def test_a_failure_is_503_not_an_empty_news_section(self, client,
                                                       monkeypatch):
        """'There is no team news' is a claim a crash cannot support."""
        def boom(_ref):
            raise RuntimeError("database gone")
        monkeypatch.setattr(team_news, "fixture_news", boom)
        r = client.get("/api/news/fixture/777107")
        assert r.status_code == 503
        assert "unavailable" in r.json()["detail"]

    def test_the_route_writes_nothing(self, client, live_session):
        """A read surface that ingests on read would spend a shared key's
        budget on every page view."""
        before = (live_session.query(TeamAbsence).count(),
                  live_session.query(TeamNewsCapture).count(),
                  live_session.query(TeamNewsLineup).count(),
                  live_session.query(TeamNewsEvent).count())
        client.get("/api/news/fixture/777108")
        client.get("/api/news/coverage")
        live_session.expire_all()
        after = (live_session.query(TeamAbsence).count(),
                 live_session.query(TeamNewsCapture).count(),
                 live_session.query(TeamNewsLineup).count(),
                 live_session.query(TeamNewsEvent).count())
        assert before == after


class TestProviderDuplicationIsVisibleNotAbsorbed:
    """API-Football duplicates injury rows.

    MEASURED 2026-07-30 on MLS fixture 1490361: 26 records for 13 players,
    each listed exactly twice. The brief for this build cited "26 records"
    as the coverage figure; the honest figure is 13 absences. Dedupe is
    correct, but silent dedupe would make a 2x provider artefact invisible,
    so BOTH numbers are reported and this test pins the gap.
    """

    def test_duplicate_provider_rows_collapse_to_distinct_players(
            self, live_session):
        doubled = APIF_INJURIES + APIF_INJURIES        # exactly the shape seen
        r = team_news.capture_absences("777200", items=doubled)
        assert r["count"] == 4, "the RAW array length must be preserved"
        rows = live_session.query(TeamAbsence).filter_by(
            subject_ref="777200").all()
        assert len(rows) == 2, "distinct players were not deduplicated"

    def test_both_counts_are_visible_on_the_read_surface(self, live_session):
        team_news.capture_absences("777201", items=APIF_INJURIES * 2)
        out = team_news.fixture_news("777201")
        assert out["absences"]["record_count"] == 4       # raw
        assert out["absences"]["stored_records"] == 2     # distinct
        assert "double-count" in out["absences"][
            "_record_count_vs_stored_records"]
