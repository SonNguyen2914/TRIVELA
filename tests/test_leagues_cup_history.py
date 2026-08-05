"""The history measurement's honesty properties, each asserted.

The ways a script like this lies quietly: an AET score counted as a
90-minute result, a club guessed into a league on one shared token, a
same-league knockout pairing polluting the cross-league balance, a
2023 season scored against ratings that contain 2023's outcomes, and a
conclusion drawn from a sample below its own floor.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "measure_leagues_cup_history",
    _ROOT / "scripts" / "measure_leagues_cup_history.py")
lh = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_leagues_cup_history"] = lh
_SPEC.loader.exec_module(lh)


def _fx(home, away, hg, ag, status="FT", rnd="Group Stage - 1",
        strength_home=None):
    row = {"home": {"name": home}, "away": {"name": away},
           "goals": {"home": hg, "away": ag}, "status": status,
           "round": rnd}
    if strength_home is not None:
        row["strength"] = {"available": True,
                           "expected_points_share": {"home": strength_home}}
    return row


ROSTERS = {
    "mls": [(frozenset({"columbus", "crew"}), "Columbus Crew"),
            (frozenset({"austin"}), "Austin FC"),
            (frozenset({"lafc", "los", "angeles"}), "LAFC"),
            (frozenset({"galaxy"}), "LA Galaxy")],
    "ligamx": [(frozenset({"atlas"}), "Atlas"),
               (frozenset({"leon"}), "León"),
               (frozenset({"tigres"}), "Tigres UANL")],
}


class TestDrawCounting:
    def test_a_pen_row_with_equal_goals_is_a_draw(self):
        out = lh.draw_rate({2025: [_fx("A", "B", 1, 1, status="PEN"),
                                   _fx("A", "B", 2, 0)]})
        assert out["editions"][2025]["draws"] == 1
        assert out["editions"][2025]["n"] == 2

    def test_an_aet_row_is_excluded_and_counted_never_miscounted(self):
        """Stored AET goals include extra time — the 90' result is not
        recoverable, so the row must not enter the rate at all."""
        out = lh.draw_rate({2025: [_fx("A", "B", 2, 1, status="AET"),
                                   _fx("A", "B", 0, 0, status="PEN")]})
        assert out["editions"][2025]["n"] == 1
        assert out["aet_excluded"]["n"] == 1
        assert "not recoverable" in out["aet_excluded"]["why"]

    def test_the_phase_split_reads_the_round_prefix(self):
        rows = [_fx("A", "B", 1, 1, status="PEN", rnd="Group Stage - 2"),
                _fx("A", "B", 0, 0, status="PEN", rnd="Quarter-finals"),
                _fx("A", "B", 3, 1, rnd="Final")]
        ph = lh.draw_rate({2023: rows})["editions"][2023]["by_phase"]
        assert ph["group"] == {"n": 1, "draw_rate": 1.0}
        assert ph["knockout"]["n"] == 2

    def test_below_twenty_rows_no_ci_is_reported(self):
        out = lh.draw_rate({2025: [_fx("A", "B", 1, 1, status="PEN")] * 5})
        assert out["editions"][2025]["ci95"] is None

    def test_the_pooled_ci_exists_at_scale_and_brackets_the_mean(self):
        rows = ([_fx("A", "B", 1, 1, status="PEN")] * 30
                + [_fx("A", "B", 2, 0)] * 70)
        p = lh.draw_rate({2024: rows})["pooled"]
        assert p["n"] == 100 and p["draw_rate"] == 0.3
        lo, hi = p["ci95"]
        assert lo < 0.3 < hi


class TestLeagueResolution:
    def test_an_exact_club_matches_its_league(self):
        assert lh.club_league("Columbus Crew", ROSTERS) == ("mls", "matched")
        assert lh.club_league("Atlas", ROSTERS) == ("ligamx", "matched")

    def test_word_form_drift_still_resolves_the_league(self):
        """'Los Angeles Galaxy' overlaps LAFC's tokens more than
        Galaxy's — and that is FINE, because only the league is being
        resolved and both are MLS."""
        lg, why = lh.club_league("Los Angeles Galaxy", ROSTERS)
        assert lg == "mls"

    def test_a_club_in_neither_roster_is_refused_not_guessed(self):
        lg, why = lh.club_league("Mazatlán", ROSTERS)
        assert lg is None and why == "no roster match"

    def test_a_cross_league_tie_is_refused(self):
        rosters = {"mls": [(frozenset({"united"}), "X United")],
                   "ligamx": [(frozenset({"united"}), "Y United")]}
        lg, why = lh.club_league("United", rosters)
        assert lg is None and "both rosters" in why


class TestCrossLeagueBalance:
    def test_the_away_mls_side_is_counted_from_its_own_perspective(self):
        """León 0-2 away loss at Columbus is MLS points share 1.0 from
        the MLS-hosted row; Tigres 1-1 home vs Austin is 0.5 on the
        ligamx-hosted row."""
        rows = [_fx("Columbus Crew", "Leon", 2, 0),
                _fx("Tigres UANL", "Austin", 1, 1, status="PEN")]
        out = lh.cross_league_balance({2025: rows}, ROSTERS)
        ed = out["editions"][2025]
        assert ed["mls_hosted"] == {"n": 1, "mls_points_share": 1.0,
                                    "ci95": None}
        assert ed["ligamx_hosted"]["mls_points_share"] == 0.5

    def test_a_same_league_pairing_never_enters_the_balance(self):
        rows = [_fx("Columbus Crew", "Austin", 1, 0, rnd="Final")]
        out = lh.cross_league_balance({2023: rows}, ROSTERS)
        assert out["same_league_pairings_excluded"] == 1
        assert out["editions"][2023]["mls_hosted"]["n"] == 0

    def test_an_unresolved_club_excludes_the_fixture_with_names(self):
        rows = [_fx("Mazatlán", "Austin", 3, 0)]
        out = lh.cross_league_balance({2023: rows}, ROSTERS)
        assert out["unresolved_count"] == 1
        u = out["unresolved"][0]
        assert u["home"] == "Mazatlán" and "no roster match" in u["reason"]
        assert out["editions"][2023]["ligamx_hosted"]["n"] == 0

    def test_the_all_venues_row_carries_its_mix_caveat(self):
        out = lh.cross_league_balance({2025: []}, ROSTERS)
        assert "venue mix" in out["pooled"]["all_venues"]["caveat"]


class TestReadScoring:
    def test_2023_and_2024_are_refused_by_name(self):
        rows = {2023: [_fx("A", "B", 1, 0, strength_home=0.6)] * 30}
        out = lh.score_read(rows)
        assert "Temporal leakage" in out["refused"]["2023_and_2024"]
        assert "n_2025_scored" in out and out["n_2025_scored"] == 0

    def test_2025_is_scored_but_labeled_contaminated(self):
        rows = {2025: [_fx("A", "B", 1, 0, strength_home=0.6)] * 25}
        out = lh.score_read(rows)
        assert out["evidence_class"] == "contaminated_retrospective"
        assert out["n_2025_scored"] == 25
        assert out["brier_read"] == round((0.6 - 1.0) ** 2, 4)
        assert "brier_home_rate_baseline" in out
        assert "brier_coin_flip" in out

    def test_below_the_floor_the_conclusion_is_withheld(self):
        rows = {2025: [_fx("A", "B", 1, 0, strength_home=0.6)] * 5}
        out = lh.score_read(rows)
        assert out["conclusion"] == "withheld"
        assert "brier_read" not in out

    def test_a_fixture_without_a_read_reduces_coverage_not_accuracy(self):
        rows = {2025: ([_fx("A", "B", 1, 0, strength_home=0.6)] * 20
                       + [_fx("A", "B", 1, 0)] * 5)}
        out = lh.score_read(rows)
        assert out["n_2025_scored"] == 20
        assert out["coverage"] == 0.8


class TestTheWholeRun:
    def _run(self, monkeypatch, tmp_path, payloads):
        monkeypatch.setenv("TRIVELA_API_BASE", "https://example.test")

        def fake_get(base, path, params=None):
            if "standings" in path:
                key = "mls" if "/mls/" in path else "ligamx"
                return {"teams": [{"name": n} for _t, n in ROSTERS[key]]}
            season = int((params or {}).get("season", 0))
            return payloads.get(season)

        monkeypatch.setattr(lh, "_get", fake_get)
        out = tmp_path / "doc.json"
        monkeypatch.setattr(sys, "argv",
                            ["x", "--out", str(out)])
        rc = lh.main()
        return rc, (json.loads(out.read_text()) if out.exists() else None)

    def test_the_archive_names_the_closeness_refusal(self, monkeypatch,
                                                     tmp_path):
        rows = {y: {"fixtures": [_fx("Columbus Crew", "Leon", 2, 0)]}
                for y in (2023, 2024, 2025)}
        rc, doc = self._run(monkeypatch, tmp_path, rows)
        assert rc == 0
        ref = doc["REFUSED_draw_rate_by_closeness"]
        assert "leakage" in ref["why"]
        assert "T-60" in ref["where_it_lives_instead"]

    def test_a_failed_season_is_named_not_silently_absent(self, monkeypatch,
                                                          tmp_path):
        rows = {2024: {"fixtures": [_fx("Columbus Crew", "Leon", 2, 0)]},
                2025: {"fixtures": [_fx("Columbus Crew", "Leon", 2, 0)]}}
        rc, doc = self._run(monkeypatch, tmp_path, rows)   # 2023 -> None
        assert rc == 0
        assert "FAILED" in doc["season_fetch_failures"]["2023"]
        assert "not 'had no matches'" in doc["season_fetch_failures"]["2023"]
        assert doc["seasons_measured"] == [2024, 2025]

    def test_no_seasons_at_all_is_a_refusal_not_an_empty_doc(
            self, monkeypatch, tmp_path, capsys):
        rc, doc = self._run(monkeypatch, tmp_path, {})
        assert rc == 2 and doc is None
        assert json.loads(capsys.readouterr().out)["error"] == \
            "no_season_data"

    def test_no_api_base_refuses(self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("TRIVELA_API_BASE", raising=False)
        monkeypatch.setattr(sys, "argv", ["x"])
        assert lh.main() == 2
        assert json.loads(capsys.readouterr().out)["error"] == "no_api_base"

    def test_the_doc_disclaims_edges_and_constants(self, monkeypatch,
                                                   tmp_path):
        rows = {2025: {"fixtures": [_fx("Columbus Crew", "Leon", 2, 0)]}}
        rc, doc = self._run(monkeypatch, tmp_path, rows)
        assert "edge" in doc["what_this_is_NOT"]
        assert "constant" in doc["what_this_is_NOT"]
