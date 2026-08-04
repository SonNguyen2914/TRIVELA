"""The ASEAN tournament simulation — what each class defends:

  - outcome probabilities: measured anchor at even ratings, zero draw at
    total mismatch, the three legs always a partition, and the points
    expectation EXACTLY the Elo expectation (the construction's claim);
  - group discovery from the fixture graph (the provider's rounds carry
    no group letter);
  - the forecast is a partition too — every simulated tournament crowns
    exactly one champion;
  - refusals: a third group, an unrated team, an empty fixture list all
    refuse by name rather than simulating something else.
"""
import pytest

from src import asean_tournament as at
from src.live import national_elo


def _fx(rnd, hid, aid, status="NS", gh=None, ga=None):
    return {"round": rnd, "status": status,
            "home": {"apif_team_id": hid, "name": f"N{hid}"},
            "away": {"apif_team_id": aid, "name": f"N{aid}"},
            "goals": {"home": gh, "away": ga}}


def _two_group_rows(finish_all=False):
    """Two groups of three: {1,2,3} and {4,5,6}, round-robin each."""
    rows = []
    for grp in ((1, 2, 3), (4, 5, 6)):
        for i, h in enumerate(grp):
            for a in grp[i + 1:]:
                if finish_all:
                    rows.append(_fx("Group Stage - 1", h, a,
                                    status="FT", gh=(1 if h < a else 0),
                                    ga=0))
                else:
                    rows.append(_fx("Group Stage - 1", h, a))
    return rows


class TestOutcomeProbs:
    def test_even_ratings_draw_at_the_measured_anchor(self):
        ph, pd, pa = at.outcome_probs(0.5)
        assert pd == pytest.approx(at.DRAW_RATE_EVEN)
        assert ph == pytest.approx(pa)

    def test_total_mismatch_has_no_draw_left(self):
        ph, pd, pa = at.outcome_probs(1.0)
        assert pd == pytest.approx(0.0)
        assert ph == pytest.approx(1.0) and pa == pytest.approx(0.0)

    @pytest.mark.parametrize("e", [0.5, 0.6, 0.75, 0.9512, 0.1787])
    def test_always_a_partition_and_points_expectation_is_exactly_e(self, e):
        ph, pd, pa = at.outcome_probs(e)
        assert min(ph, pd, pa) >= 0
        assert ph + pd + pa == pytest.approx(1.0)
        # the whole point of the construction: E is preserved
        assert ph + pd / 2 == pytest.approx(e)


class TestGroupDiscovery:
    def test_two_components_from_the_fixture_graph(self):
        groups = at._groups(_two_group_rows())
        assert sorted(map(tuple, groups)) == [(1, 2, 3), (4, 5, 6)]

    def test_qualifier_fixtures_do_not_join_the_graph(self):
        rows = _two_group_rows()
        # a qualifier between the two groups must NOT merge them
        rows.append(_fx("Qualification Round Group Stage - 1", 1, 4))
        assert len(at._groups(rows)) == 2


class TestTable:
    def test_standard_points_and_gd_ordering(self):
        rows = [
            _fx("Group Stage - 1", 1, 2, status="FT", gh=2, ga=0),
            _fx("Group Stage - 2", 2, 3, status="FT", gh=1, ga=1),
            _fx("Group Stage - 3", 3, 1, status="FT", gh=0, ga=1),
        ]
        t = at._table(rows, [1, 2, 3], {1: "A", 2: "B", 3: "C"})
        assert [r["team"] for r in t] == ["A", "C", "B"]
        assert t[0]["points"] == 6 and t[0]["gd"] == 3
        assert t[1]["points"] == 1


class TestBuild:
    @pytest.fixture(autouse=True)
    def rated(self, monkeypatch):
        monkeypatch.setattr(
            national_elo, "lookup",
            lambda name: {"rated": True, "rating": 1000 + int(name[1:]) * 50,
                          "source": "eloratings_national"})

    def test_forecast_is_a_partition_over_exactly_the_field(self):
        out = at.build(_two_group_rows())
        assert out["available"] is True
        ps = [r["p_champion"] for r in out["champion_forecast"]]
        # payload values are rounded to 4dp, so sums carry that precision
        assert sum(ps) == pytest.approx(1.0, abs=1e-3)
        assert len(ps) == 6
        # two finalists per sim, four semifinalists per sim
        assert sum(r["p_final"] for r in
                   out["champion_forecast"]) == pytest.approx(2.0, abs=1e-3)
        assert sum(r["p_semis"] for r in
                   out["champion_forecast"]) == pytest.approx(4.0, abs=1e-3)

    def test_no_champion_is_crowned_by_a_simulation(self):
        out = at.build(_two_group_rows())
        assert out["champion"] is None
        assert out["champion_forecast_leader"]["team"]

    def test_the_strongest_team_leads_a_symmetric_field(self):
        out = at.build(_two_group_rows())
        assert out["champion_forecast_leader"]["team"] == "N6"

    def test_forecast_kind_disclaims_a_model_in_the_payload_itself(self):
        out = at.build(_two_group_rows())
        assert "no model" in out["forecast_kind"].lower()
        assert "edge" not in str(out.get("champion_forecast")).lower()

    def test_finished_groups_still_simulate_the_knockouts(self):
        out = at.build(_two_group_rows(finish_all=True))
        assert out["available"] is True
        assert out["remaining_group_matches"] == 0
        assert sum(r["p_champion"]
                   for r in out["champion_forecast"]) == pytest.approx(1.0)

    def test_seed_is_fixed_so_two_reads_agree(self):
        a = at.build(_two_group_rows())
        b = at.build(_two_group_rows())
        assert a["champion_forecast"] == b["champion_forecast"]


class TestProjectedBracket:
    @pytest.fixture(autouse=True)
    def rated(self, monkeypatch):
        monkeypatch.setattr(
            national_elo, "lookup",
            lambda name: {"rated": True, "rating": 1000 + int(name[1:]) * 50,
                          "source": "eloratings_national"})

    def test_slots_carry_distributions_from_the_right_group(self):
        out = at.build(_two_group_rows())
        b = out["bracket"]
        assert b["projected"] is True
        a1 = b["semifinals"][0]["home_slot"]
        assert a1["label"] == "Group A winner"
        # group A is {N1,N2,N3}; its winner slot may contain only those
        assert {d["team"] for d in a1["dist"]} <= {"N1", "N2", "N3"}
        assert sum(d["p"] for d in a1["dist"]) == pytest.approx(1.0,
                                                               abs=1e-3)

    def test_the_final_slots_are_the_semi_winner_distributions(self):
        b = at.build(_two_group_rows())["bracket"]
        assert b["final"]["home_slot"]["dist"] == \
            b["semifinals"][0]["winner_dist"]

    def test_the_basis_says_projected_not_drawn(self):
        b = at.build(_two_group_rows())["bracket"]
        assert "NOT drawn" in b["basis"]


class TestRealBracket:
    @pytest.fixture(autouse=True)
    def rated(self, monkeypatch):
        monkeypatch.setattr(
            national_elo, "lookup",
            lambda name: {"rated": True, "rating": 1000 + int(name[1:]) * 50,
                          "source": "eloratings_national"})

    def _with_semis(self):
        rows = _two_group_rows(finish_all=True)
        leg1 = _fx("Semi-finals", 3, 5, status="FT", gh=2, ga=1)
        leg1["kickoff_utc"] = "2026-08-11T13:00:00+00:00"
        leg2 = _fx("Semi-finals", 5, 3)
        leg2["kickoff_utc"] = "2026-08-15T13:00:00+00:00"
        return rows + [leg1, leg2]

    def test_published_semis_replace_the_projection(self):
        b = at.build(self._with_semis())["bracket"]
        assert b["projected"] is False
        tie = b["semifinals"][0]
        assert {t["team"] for t in tie["teams"]} == {"N3", "N5"}

    def test_aggregate_counts_only_finished_legs_and_names_no_winner(self):
        b = at.build(self._with_semis())["bracket"]
        tie = b["semifinals"][0]
        assert tie["legs_settled"] == 1
        assert tie["aggregate"] == "2-1"          # N3 first alphabetically
        assert "winner" not in tie

    def test_p_advance_is_a_partition(self):
        tie = at.build(self._with_semis())["bracket"]["semifinals"][0]
        ps = [t["p_advance"] for t in tie["teams"]]
        assert sum(ps) == pytest.approx(1.0, abs=1e-3)

    def test_semi_legs_never_double_as_final_legs(self):
        # "final" is a substring of "Semi-finals" — a plain contains-match
        # renders every semi twice, once as itself and once as the final
        b = at.build(self._with_semis())["bracket"]
        assert len(b["semifinals"]) == 1
        assert b["final_ties"] == []

    def test_a_real_final_still_matches(self):
        rows = self._with_semis()
        f1 = _fx("Final", 3, 6, status="FT", gh=1, ga=0)
        f1["kickoff_utc"] = "2026-08-22T13:00:00+00:00"
        rows.append(f1)
        b = at.build(rows)["bracket"]
        assert len(b["final_ties"]) == 1
        assert {t["team"] for t in b["final_ties"][0]["teams"]} == \
            {"N3", "N6"}


class TestRefusals:
    def test_three_groups_refuse_by_name(self, monkeypatch):
        monkeypatch.setattr(
            national_elo, "lookup",
            lambda name: {"rated": True, "rating": 1000})
        rows = _two_group_rows()
        rows.append(_fx("Group Stage - 1", 7, 8))
        out = at.build(rows)
        assert out["available"] is False
        assert "3" in out["reason"]
        assert "champion_forecast" not in out

    def test_an_unrated_team_withholds_the_whole_forecast(self, monkeypatch):
        def lk(name):
            if name == "N3":
                return {"rated": False, "reason": "name_unmapped"}
            return {"rated": True, "rating": 1200}
        monkeypatch.setattr(national_elo, "lookup", lk)
        out = at.build(_two_group_rows())
        assert out["available"] is False
        assert "N3" in out["reason"]
        assert "champion_forecast" not in out
