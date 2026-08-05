"""The hosting-league offset measurement, and the ways it could lie.

This script exists because two observations pointed the same way — the
history's cross-league balance, and matchday 1's four MLS hosts — and a
hypothesis pointed at twice is still a hypothesis. Everything asserted
here is a way the script could turn that hypothesis into a constant it
has not earned:

  - a mean signed error computed over a handful of fixtures and reported
    as if it were a coefficient;
  - the CONTAMINATED arm quietly filling in for the clean one when no
    prospective corpus exists — a number wearing a label it did not earn
    is worse than no number;
  - 2023/2024 scored against ratings that already contain their
    outcomes (AGENTS.md section 13);
  - a same-league pairing polluting a cross-league comparison, or a club
    guessed into a league on one shared token;
  - the sign convention drifting, so "the read is too low on MLS hosts"
    and "too high" swap places silently.
"""
import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "measure_league_offset",
    _ROOT / "scripts" / "measure_league_offset.py")
lo = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_league_offset"] = lo
_SPEC.loader.exec_module(lo)

ROSTERS = {
    "mls": [(frozenset({"columbus", "crew"}), "Columbus Crew"),
            (frozenset({"austin"}), "Austin FC"),
            (frozenset({"nashville"}), "Nashville SC")],
    "ligamx": [(frozenset({"atlas"}), "Atlas"),
               (frozenset({"leon"}), "León"),
               (frozenset({"tigres"}), "Tigres UANL")],
}


def _row(home, away, hg, ag, read=None):
    return {"home": {"name": home}, "away": {"name": away},
            "goals": {"home": hg, "away": ag}, "_read": read}


def _many(home, away, hg, ag, read, n):
    return [_row(home, away, hg, ag, read) for _ in range(n)]


class TestTheSignConvention:
    """Positive means the read was TOO HIGH on the home side. A silent
    flip here would invert the correction anyone later fits."""

    def test_a_read_below_a_home_win_is_negative(self):
        out = lo._arm(_many("Columbus Crew", "Leon", 2, 0, 0.60, 25),
                      ROSTERS, "t")
        assert out["by_host"]["mls_hosted"]["mean_signed_error"] == -0.4

    def test_a_read_above_a_home_loss_is_positive(self):
        out = lo._arm(_many("Tigres UANL", "Austin", 0, 1, 0.70, 25),
                      ROSTERS, "t")
        assert out["by_host"]["ligamx_hosted"]["mean_signed_error"] == 0.7

    def test_a_draw_scores_as_half_a_point(self):
        out = lo._arm(_many("Columbus Crew", "Atlas", 1, 1, 0.50, 25),
                      ROSTERS, "t")
        assert out["by_host"]["mls_hosted"]["mean_signed_error"] == 0.0

    def test_the_two_buckets_are_split_by_WHO_HOSTED_not_who_won(self):
        rows = (_many("Columbus Crew", "Leon", 2, 0, 0.6, 21)
                + _many("Tigres UANL", "Austin", 2, 0, 0.6, 22))
        out = lo._arm(rows, ROSTERS, "t")
        assert out["by_host"]["mls_hosted"]["n"] == 21
        assert out["by_host"]["ligamx_hosted"]["n"] == 22


class TestTheFloor:
    """The whole purpose of this number is to justify or refuse a
    constant, so a sample too small to do either must say so."""

    def test_below_the_floor_a_bucket_reports_no_interval(self):
        out = lo._arm(_many("Columbus Crew", "Leon", 2, 0, 0.6,
                            lo.MIN_PER_ARM - 1), ROSTERS, "t")
        b = out["by_host"]["mls_hosted"]
        assert b["ci95"] is None and b["below_floor"] is True
        assert "mean_signed_error" in b     # reported, just not trusted

    def test_at_the_floor_an_interval_appears_and_brackets_the_mean(self):
        rows = (_many("Columbus Crew", "Leon", 2, 0, 0.6, 10)
                + _many("Columbus Crew", "Leon", 0, 2, 0.6, 10))
        out = lo._arm(rows, ROSTERS, "t")
        b = out["by_host"]["mls_hosted"]
        assert b["n"] == lo.MIN_PER_ARM and b["below_floor"] is False
        lo_ci, hi_ci = b["ci95"]
        assert lo_ci <= b["mean_signed_error"] <= hi_ci

    def test_one_thin_bucket_refuses_the_whole_comparison(self):
        """The asymmetry is a DIFFERENCE — one solid arm cannot carry
        it, and reporting the solid arm's mean as the answer would be
        the comparison by another name."""
        rows = (_many("Columbus Crew", "Leon", 2, 0, 0.6, 40)
                + _many("Tigres UANL", "Austin", 2, 0, 0.6, 3))
        out = lo._arm(rows, ROSTERS, "t")
        ha = out["host_asymmetry"]
        assert "refused" in ha
        assert "3 Liga MX-hosted" in ha["refused"]
        assert "delta_mean_signed_error" not in ha

    def test_an_empty_bucket_is_named_never_a_zero(self):
        """n=0 reported as mean 0.0 would read as 'the read is unbiased
        on Liga MX hosts', which is the opposite of what it means."""
        out = lo._arm(_many("Columbus Crew", "Leon", 2, 0, 0.6, 25),
                      ROSTERS, "t")
        b = out["by_host"]["ligamx_hosted"]
        assert b == {"n": 0, "refused": "no scorable fixture in this bucket"}

    def test_both_arms_full_produces_a_delta_with_a_significance_flag(self):
        rows = (_many("Columbus Crew", "Leon", 2, 0, 0.60, 25)
                + _many("Tigres UANL", "Austin", 2, 0, 0.60, 25))
        ha = lo._arm(rows, ROSTERS, "t")["host_asymmetry"]
        assert ha["delta_mean_signed_error"] == 0.0
        assert isinstance(ha["significant"], bool)
        assert "correction would be fitted" in ha["means"]

    def test_a_real_separation_is_flagged_significant(self):
        """Constant errors of -0.4 and +0.4 have zero within-bucket
        variance, so every resample reproduces the gap."""
        rows = (_many("Columbus Crew", "Leon", 2, 0, 0.60, 25)
                + _many("Tigres UANL", "Austin", 0, 1, 0.40, 25))
        ha = lo._arm(rows, ROSTERS, "t")["host_asymmetry"]
        assert ha["delta_mean_signed_error"] == -0.8
        assert ha["significant"] is True


class TestWhatNeverEntersTheBuckets:
    def test_a_same_league_pairing_is_excluded_and_counted(self):
        out = lo._arm([_row("Columbus Crew", "Austin", 1, 0, 0.6)],
                      ROSTERS, "t")
        assert out["same_league_excluded"] == 1
        assert out["by_host"]["mls_hosted"]["n"] == 0

    def test_an_unresolved_club_is_named_not_guessed(self):
        out = lo._arm([_row("Mazatlán", "Austin", 3, 0, 0.6)], ROSTERS, "t")
        assert out["unresolved"] == 1
        assert out["unresolved_names"] == ["Mazatlán v Austin"]

    def test_a_cross_roster_tie_resolves_to_nothing(self):
        rosters = {"mls": [(frozenset({"united"}), "X United")],
                   "ligamx": [(frozenset({"united"}), "Y United")]}
        assert lo._league_of("United", rosters) is None

    def test_a_fixture_without_a_read_reduces_COVERAGE_not_accuracy(self):
        """Scoring a missing read as zero error would flatter the read
        by exactly the fixtures it had no opinion about."""
        rows = (_many("Columbus Crew", "Leon", 2, 0, 0.6, 20)
                + _many("Columbus Crew", "Leon", 2, 0, None, 5))
        out = lo._arm(rows, ROSTERS, "t")
        assert out["fixtures_without_a_read"] == 5
        assert out["by_host"]["mls_hosted"]["n"] == 20

    def test_the_unresolved_name_list_is_bounded(self):
        out = lo._arm([_row("Mazatlán", "Austin", 1, 0, 0.6)] * 40,
                      ROSTERS, "t")
        assert out["unresolved"] == 40 and len(out["unresolved_names"]) == 10


class TestTheLeakageBoundary:
    """The retrospective arm is contaminated by construction; the point
    is that it can never be mistaken for the clean one."""

    def _run(self, monkeypatch, tmp_path, fixtures, corpus=None):
        seasons = []

        def fake_get(base, path, params=None):
            if "standings" in path:
                key = "mls" if "/mls/" in path else "ligamx"
                return {"teams": [{"name": n} for _t, n in ROSTERS[key]]}
            seasons.append((params or {}).get("season"))
            return {"fixtures": fixtures}

        monkeypatch.setattr(lo, "_get", fake_get)
        out = tmp_path / "doc.json"
        argv = ["x", "--api-base", "https://example.test", "--out", str(out)]
        if corpus is not None:
            p = tmp_path / "corpus.json"
            p.write_text(json.dumps(corpus))
            argv += ["--prospective-corpus", str(p)]
        monkeypatch.setattr(sys, "argv", argv)
        rc = lo.main()
        return rc, json.loads(out.read_text()), seasons

    def _settled(self, home, away, hg, ag, read):
        return {"home": {"name": home}, "away": {"name": away},
                "goals": {"home": hg, "away": ag}, "status": "FT",
                "strength": {"available": True,
                             "expected_points_share": {"home": read}}}

    def test_only_2025_is_ever_fetched(self, monkeypatch, tmp_path):
        """2023 and 2024 are refused by not being asked for — a refusal
        stated in prose but implemented as a request would still leak
        the moment someone reads the response."""
        rc, doc, seasons = self._run(
            monkeypatch, tmp_path,
            [self._settled("Columbus Crew", "Leon", 2, 0, 0.6)])
        assert rc == 0
        assert seasons == [2025]
        assert "REFUSED" in doc["leakage_policy"]["2023_2024"]

    def test_the_retrospective_arm_carries_its_contamination(
            self, monkeypatch, tmp_path):
        rc, doc, _ = self._run(
            monkeypatch, tmp_path,
            [self._settled("Columbus Crew", "Leon", 2, 0, 0.6)])
        r = doc["retrospective_2025"]
        assert r["evidence_class"] == "contaminated_retrospective"
        assert "flatters the read" in r["contamination"]

    def test_no_corpus_refuses_the_clean_arm_BY_NAME(self, monkeypatch,
                                                     tmp_path):
        rc, doc, _ = self._run(
            monkeypatch, tmp_path,
            [self._settled("Columbus Crew", "Leon", 2, 0, 0.6)])
        p = doc["prospective_2026"]
        assert "no pre-kickoff read corpus" in p["refused"]
        assert p["evidence_class"] == "not_yet_collectable"
        # the contaminated arm must NOT have stood in for it
        assert "by_host" not in p

    def test_a_supplied_corpus_is_scored_and_labeled_clean(
            self, monkeypatch, tmp_path):
        corpus = _many("Columbus Crew", "Leon", 2, 0, 0.6, 3)
        rc, doc, _ = self._run(
            monkeypatch, tmp_path,
            [self._settled("Columbus Crew", "Leon", 2, 0, 0.6)],
            corpus=corpus)
        p = doc["prospective_2026"]
        assert p["evidence_class"] == "prospective_clean"
        assert p["by_host"]["mls_hosted"]["n"] == 3
        # still below the floor, so still no comparison
        assert "refused" in p["host_asymmetry"]

    def test_an_unfinished_fixture_never_enters_the_retrospective_arm(
            self, monkeypatch, tmp_path):
        live = self._settled("Columbus Crew", "Leon", 1, 0, 0.6)
        live["status"] = "1H"
        rc, doc, _ = self._run(monkeypatch, tmp_path, [live])
        assert doc["retrospective_2025"]["by_host"]["mls_hosted"]["n"] == 0

    def test_a_settled_row_with_no_score_is_not_scored_as_a_loss(
            self, monkeypatch, tmp_path):
        row = self._settled("Columbus Crew", "Leon", None, None, 0.6)
        rc, doc, _ = self._run(monkeypatch, tmp_path, [row])
        assert doc["retrospective_2025"]["by_host"]["mls_hosted"]["n"] == 0


class TestTheDocumentSaysWhatItIsNot:
    def test_no_api_base_refuses_rather_than_measuring_nothing(
            self, monkeypatch, capsys):
        monkeypatch.delenv("TRIVELA_API_BASE", raising=False)
        monkeypatch.setattr(sys, "argv", ["x"])
        assert lo.main() == 2
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "no_api_base" and out["measured"] is False

    def test_the_prior_it_cites_is_the_outcome_only_history(self):
        """The hypothesis came from somewhere; the document names it so
        a reader can check whether the two arms agree."""
        import inspect
        src = inspect.getsource(lo.main)
        assert "leagues_cup_history_2026-08-05.json" in src

    def test_the_conclusion_withholds_a_correction_until_the_clean_arm(
            self, monkeypatch, tmp_path):
        rc, doc, _ = TestTheLeakageBoundary()._run(
            monkeypatch, tmp_path,
            [TestTheLeakageBoundary()._settled(
                "Columbus Crew", "Leon", 2, 0, 0.6)])
        c = doc["conclusion"]
        assert "prospective arm" in c and "CI excluding zero" in c
        assert "not a coefficient" in c
        assert "edge" in doc["what_this_is_NOT"]
        assert "constant" in doc["what_this_is_NOT"]
