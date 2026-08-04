"""ASEAN Championship: the first NATIONAL-TEAM viewer competition.

What is actually at stake in each class:
  - the registry pins strength_kind="national" — a national team run
    through a club provider can only false-match;
  - season resolution — the provider files the Aug-2026 edition under
    2025, and a 2026 default would read a live tournament as empty;
  - two-leg detection without the word "Leg" — ASEAN's semis and final
    are two-legged under bare round names, and the reversed-pairing-in-
    same-round signature must catch them WITHOUT catching round-robin
    reverse meetings;
  - the Elo source refuses what it cannot know, by name.
"""
import pytest

from src import competitions
from src.live import national_elo


# --- registry -------------------------------------------------------------

class TestRegistry:
    def test_asean_is_registered_with_probed_constants(self):
        v = competitions.VIEWERS["asean"]
        assert v.apif_league_id == 24
        assert v.kalshi_series == "KXASEANGAME"
        assert v.no_model == competitions.BY_DESIGN

    def test_strength_kind_is_national_and_unique_to_asean(self):
        assert competitions.VIEWERS["asean"].strength_kind == "national"
        for k, v in competitions.VIEWERS.items():
            if k != "asean":
                assert v.strength_kind == "club", k

    def test_the_why_owns_the_missing_model_rather_than_blaming_the_cup(self):
        why = competitions.VIEWERS["asean"].why or ""
        assert "national" in why.lower()

    def test_season_default_is_the_providers_label_not_the_calendar(self):
        assert competitions.VIEWERS["asean"].season_default == 2025
        assert competitions.VIEWERS["ucl"].season_default is None


# --- season resolution ----------------------------------------------------

class TestSeasonResolution:
    @pytest.fixture
    def captured(self, monkeypatch):
        calls = {}

        def fake_raw(league_id, season):
            calls["league_id"], calls["season"] = league_id, season
            return []
        monkeypatch.setattr(competitions, "_fixtures_raw", fake_raw)
        monkeypatch.setattr(competitions, "_cache", {})
        monkeypatch.setattr(competitions, "markets", lambda k: {})
        return calls

    def test_no_season_resolves_to_the_pinned_edition(self, captured):
        competitions.fixtures("asean")
        assert captured["season"] == 2025

    def test_an_explicit_season_still_wins(self, captured):
        competitions.fixtures("asean", season=2024)
        assert captured["season"] == 2024

    def test_unpinned_competitions_keep_the_calendar_default(self, captured):
        competitions.fixtures("ucl")
        assert captured["season"] == 2026

    def test_the_payload_reports_the_resolved_season(self, captured):
        out = competitions.fixtures("asean")
        assert out["season"] == 2025


# --- the national strength path -------------------------------------------

class TestNationalPath:
    def test_asean_rows_read_national_elo_never_a_club_provider(
            self, monkeypatch):
        row = {"home": {"name": "Vietnam", "apif_team_id": 1},
               "away": {"name": "Thailand", "apif_team_id": 2},
               "fixture_id": 9, "kickoff_utc": "2099-01-01T00:00:00+00:00"}
        monkeypatch.setattr(competitions, "_fixtures_raw",
                            lambda lid, s: [dict(row)])
        monkeypatch.setattr(competitions, "_cache", {})
        monkeypatch.setattr(competitions, "markets", lambda k: {})
        seen = {}
        monkeypatch.setattr(national_elo, "for_fixture",
                            lambda f: seen.setdefault("called", True) and {
                                "available": True,
                                "expected_points_share": {"home": 0.5,
                                                          "away": 0.5}})

        def club_forbidden(*a, **k):
            raise AssertionError("club provider consulted for a "
                                 "national-team fixture")
        from src.live import club_strength_estimate as cse
        monkeypatch.setattr(cse, "for_fixture", club_forbidden)
        out = competitions.fixtures("asean")
        assert seen.get("called") is True
        assert out["fixtures"][0]["strength"]["available"] is True


# --- meaning: two legs without the word "Leg" ------------------------------

def _fx(rnd, hid, aid, status="NS", gh=None, ga=None,
        hname=None, aname=None):
    return {"round": rnd, "status": status,
            "home": {"apif_team_id": hid, "name": hname or f"T{hid}"},
            "away": {"apif_team_id": aid, "name": aname or f"T{aid}"},
            "goals": {"home": gh, "away": ga}}


class TestTwoLegDetection:
    def test_bare_semifinal_round_with_reverse_fixture_is_a_tie_leg1(self):
        a = _fx("Semi-finals", 1, 2)
        b = _fx("Semi-finals", 2, 1)
        m = competitions._meaning(a, [a, b], "asean")
        assert m["tie"]["leg"] == 1

    def test_finished_first_leg_makes_the_return_leg_2_with_aggregate(self):
        first = _fx("Semi-finals", 2, 1, status="FT", gh=3, ga=1)
        ret = _fx("Semi-finals", 1, 2)
        m = competitions._meaning(ret, [first, ret], "asean")
        assert m["tie"]["leg"] == 2
        assert m["tie"]["first_leg"] == "3-1"
        assert "trail" in m["tie"]["means"]

    def test_round_robin_reverse_meetings_are_not_read_as_a_tie(self):
        # the reverse meeting of a round-robin lives in a DIFFERENT round
        a = _fx("Group Stage - 1", 1, 2)
        b = _fx("Group Stage - 3", 2, 1)
        m = competitions._meaning(a, [a, b], "asean")
        assert "tie" not in m

    def test_same_round_reversed_inside_a_group_round_is_not_a_tie(self):
        # defensive: even a same-round reverse inside a "Group" round
        # (a data quirk) must not be read as a two-legged tie
        a = _fx("Group Stage - 2", 1, 2)
        b = _fx("Group Stage - 2", 2, 1)
        m = competitions._meaning(a, [a, b], "asean")
        assert "tie" not in m


class TestAseanGroupStakes:
    def test_points_are_standard_and_exact_no_shootout_range(self):
        rows = [
            _fx("Group Stage - 1", 1, 2, status="FT", gh=2, ga=0),
            _fx("Group Stage - 2", 3, 1, status="FT", gh=1, ga=1),
            _fx("Group Stage - 3", 1, 4),
        ]
        m = competitions._meaning(rows[2], rows, "asean")
        rec = m["stakes"]["home"]
        assert (rec["w"], rec["d"], rec["l"]) == (1, 1, 0)
        assert rec["points"] == 4
        assert "points_range" not in rec

    def test_qualifier_results_do_not_leak_into_group_records(self):
        rows = [
            _fx("Qualification Round Group Stage - 1", 1, 2,
                status="FT", gh=5, ga=0),
            _fx("Group Stage - 1", 1, 3),
        ]
        m = competitions._meaning(rows[1], rows, "asean")
        assert m["stakes"]["home"]["played"] == 0

    def test_qualifier_itself_is_still_read_as_a_two_legged_tie(self):
        a = _fx("Qualification Round Group Stage - 1", 1, 2)
        b = _fx("Qualification Round Group Stage - 1", 2, 1)
        m = competitions._meaning(a, [a, b], "asean")
        assert m["tie"]["leg"] == 1

    def test_a_lone_published_semi_leg_is_a_tie_not_a_group_record(self):
        # the provider publishes leg 1 before leg 2 exists; in that gap
        # the reversed-fixture signature cannot fire, and the fixture was
        # falling through to GROUP stakes — wrong competition phase, wrong
        # scoring. ASEAN's semis are two-legged BY FORMAT.
        a = _fx("Semi-finals", 1, 2)
        m = competitions._meaning(a, [a], "asean")
        assert m["tie"]["leg"] == 1
        assert "stakes" not in m

    def test_a_knockout_fixture_never_wears_a_group_record(self):
        rows = [
            _fx("Group Stage - 1", 1, 2, status="FT", gh=2, ga=0),
            _fx("Final", 1, 3),
        ]
        m = competitions._meaning(rows[1], rows, "asean")
        assert "stakes" not in m
        assert m["tie"]["leg"] == 1


# --- the Elo source -------------------------------------------------------

TEAMS_TSV = ("VN\tVietnam\n"
             "TH\tThailand\n"
             "TL\tEast Timor\n"
             "NV\tNorth Vietnam\tN Vietnam\n"
             "XA\tAmbiguia\n"
             "XB\tAmbiguia\n")
WORLD_TSV = ("1\t1\tTH\t1395\n"
             "2\t2\tVN\t1389\n"
             "3\t3\tTL\t712\n"
             "4\t4\tXA\t1000\n")


class TestNationalElo:
    @pytest.fixture(autouse=True)
    def snapshot(self, monkeypatch):
        data = {"names": national_elo._parse_teams(TEAMS_TSV),
                "ratings": national_elo._parse_world(WORLD_TSV),
                "fetched_utc": "2026-08-04T00:00:00+00:00"}
        monkeypatch.setattr(national_elo, "_snapshot", lambda: (data, None))

    def test_lookup_finds_a_team_and_names_its_source(self):
        r = national_elo.lookup("Vietnam")
        assert r["rated"] and r["rating"] == 1389
        assert r["source"] == national_elo.SRC_NATIONAL

    def test_the_provider_spelling_gap_is_bridged_by_a_pinned_override(self):
        assert national_elo.lookup("Timor-Leste")["rating"] == 712

    def test_an_unknown_name_is_refused_never_fuzzy_matched(self):
        r = national_elo.lookup("Atlantis")
        assert not r["rated"] and r["reason"] == "name_unmapped"

    def test_a_name_two_codes_claim_is_dropped_not_coin_flipped(self):
        r = national_elo.lookup("Ambiguia")
        assert not r["rated"] and r["reason"] == "name_unmapped"

    def test_a_known_name_missing_from_the_ranking_is_its_own_refusal(self):
        r = national_elo.lookup("North Vietnam")
        assert not r["rated"] and r["reason"] == "not_in_ranking"

    def test_expectation_is_symmetric_and_anchored(self):
        e = national_elo.expected_points_share(1395, 1389)
        assert e + national_elo.expected_points_share(1389, 1395) == 1.0
        assert 0.5 < e < 0.52
        assert national_elo.expected_points_share(1500, 1500) == 0.5

    def test_for_fixture_shape_matches_the_club_read(self):
        out = national_elo.for_fixture(
            {"home": {"name": "Thailand"}, "away": {"name": "Vietnam"}})
        assert out["available"] is True
        eps = out["expected_points_share"]
        assert round(eps["home"] + eps["away"], 4) == 1.0
        assert out["elo_difference"] == 6
        assert "draw" not in str(eps).lower()

    def test_one_unrated_side_makes_the_pair_unavailable_with_reasons(self):
        out = national_elo.for_fixture(
            {"home": {"name": "Thailand"}, "away": {"name": "Atlantis"}})
        assert out["available"] is False
        assert out["expected_points_share"] is None
        assert out["away"]["reason"] == "name_unmapped"


class TestNationalEloUnreachable:
    def test_a_dead_provider_is_a_named_refusal_never_a_default(
            self, monkeypatch):
        monkeypatch.setattr(national_elo, "_snapshot",
                            lambda: (None, "ConnectionError: down"))
        r = national_elo.lookup("Vietnam")
        assert not r["rated"] and r["reason"] == "provider_unreachable"
        assert "UNKNOWN" in r["reason_words"]
