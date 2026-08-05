"""The Sportec xG plausibility guard, and what it is allowed to change.

Sportec's own xG collapsed on the 2026-07-22/23 MLS restart slates while
its own shot volume held: total match xG per own shot on target ran
0.328-0.334 Feb-May, then 0.246 in July. Seven matches carry xG the SAME
payload's shot and goal counts contradict, and the provider labels every
one of them data_status="postmatch" — FINAL. There is no provider flag to
reject them by, so the guard is derived from the payload's internals.

Evidence: docs/XG-SOURCE-COMPARISON.md §4 and
research_archive/xg_source_comparison_2026-07-30.json (key `attribution`)
on branch research-xg-source-comparison, plus
docs/MLS-XG-QUARANTINE.md in this tree.

The real measured values are used as fixtures deliberately. A guard tested
only against invented numbers can pass while failing on the data that
motivated it.
"""
from datetime import UTC, datetime

import pytest

import config
from src.live import db as live_db
from src.live import identity, model_mls, mls_stats
from src.live.models import (Competition, Fixture, LiveBase,
                             MlsTeamMatchStat)
from tests.test_mls_shadow import CANNED_ESPN


@pytest.fixture
def live_session(tmp_path, monkeypatch):
    from tests import _livedb
    url, _livedb_done = _livedb.provision(tmp_path, monkeypatch)
    LiveBase.metadata.create_all(live_db.get_engine())
    s = live_db.get_session()
    s.add(Competition(slug="mls-2026", name="MLS", season=2026))
    s.commit()
    yield s
    s.close()
    _livedb_done()
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)


def _sides(xg_h, g_h, sot_h, xg_a, g_a, sot_a):
    """The two-element list xg_plausibility screens (as _team_fields emits)."""
    return [{"xg": xg_h, "goals": g_h, "shots_on_target": sot_h},
            {"xg": xg_a, "goals": g_a, "shots_on_target": sot_a}]


# the seven matches the guard must reject, at their REAL provider values
# (per-team xG / goals / shots-on-target, from the archived payloads)
BROKEN = {
    "CLT-ATL 2:2": _sides(0.7975, 2, 10, 0.1616, 2, 6),
    "SKC-MIN 2:1": _sides(0.1309, 2, 7, 0.2029, 1, 8),
    "COL-SD 1:0": _sides(0.0942, 1, 2, 0.0894, 0, 2),
    "SJ-ORL 0:4": _sides(0.1295, 0, 7, 0.3021, 4, 7),
    "ATX-SEA 3:1": _sides(0.3200, 3, 5, 0.2227, 1, 4),
    "PHI-RBNY 3:1": _sides(0.7500, 3, 8, 0.5517, 1, 7),
    "CIN-VAN 4:3": _sides(0.8000, 4, 6, 0.5711, 3, 6),
}

# real Feb-May matches that must pass. The clean window's tightest cases:
# its minimum match ratio was 0.1443 and its minimum Poisson tail 0.0228,
# so these sit ABOVE both thresholds and pin the guard's other edge.
CLEAN = {
    "ordinary 2:1": _sides(1.50, 2, 5, 1.20, 1, 6),
    "sparse 0:0 high ratio": _sides(1.10, 0, 2, 0.59, 0, 1),
    "goal-heavy 5:2": _sides(2.90, 5, 9, 1.60, 2, 7),
    "clean-window minimum ratio 0:0": _sides(0.30, 0, 2, 0.18, 0, 1),
}


class TestGuardVerdicts:
    @pytest.mark.parametrize("name", sorted(BROKEN))
    def test_rejects_every_measured_broken_match(self, name):
        v = mls_stats.xg_plausibility(BROKEN[name])
        assert v["quarantined"] is True, f"{name} must be quarantined"
        assert v["reason"], "a quarantine must always say why"

    @pytest.mark.parametrize("name", sorted(CLEAN))
    def test_passes_plausible_matches(self, name):
        v = mls_stats.xg_plausibility(CLEAN[name])
        assert v["quarantined"] is False, f"{name} must pass"
        assert v["reason"] is None

    def test_thresholds_stay_below_the_clean_window_extremes(self):
        """The thresholds were DERIVED from the clean Feb-May window's
        empirical extremes (ratio min 0.1443, tail min 0.0228 over n=218),
        which is what makes 0/218 false positives a property and not luck.

        Loosening either past its clean-window extreme would start
        quarantining data measured to be fine — silently shrinking the xG
        the ratings are fitted on. This test is the tripwire for that."""
        assert config.MLS_XG_GUARD_MIN_XG_PER_SOT < 0.1443
        assert config.MLS_XG_GUARD_MIN_GOALS_TAIL_P < 0.0228

    def test_absent_xg_is_neither_a_pass_nor_a_failure(self):
        """No xG means the fixture already contributes nothing to the xG
        rating (fit() needs xg AND xg_against), so there is nothing to
        screen — and saying so is different from saying it passed."""
        v = mls_stats.xg_plausibility(_sides(None, 1, 5, 1.2, 1, 6))
        assert v["quarantined"] is False and v["reason"] is None
        assert v["checks"]["applicable"] is False
        assert v["checks"]["why"] == "xg_absent"

    def test_zero_xg_with_a_goal_scored_is_impossible_not_unlikely(self):
        """A goal is a shot with non-zero xG. Zero expected goals producing
        one is not a tail event, it is a contradiction — the Poisson check
        must return exactly 0, not a small number that a threshold could
        one day sit under."""
        assert mls_stats._poisson_sf(1, 0.0) == 0.0
        v = mls_stats.xg_plausibility(_sides(0.0, 1, 4, 0.0, 0, 3))
        assert v["quarantined"] is True

    def test_sparse_match_is_not_quarantined_on_the_ratio_alone(self):
        """Below MLS_XG_GUARD_MIN_SOT the ratio's denominator is too small
        to carry evidence, and the clean window's sparse matches have HIGH
        ratios, not low ones. A 1-shot 0-0 must not be quarantined."""
        v = mls_stats.xg_plausibility(_sides(0.02, 0, 1, 0.01, 0, 1))
        assert v["quarantined"] is False
        assert v["checks"]["ratio"]["applicable"] is False

    def test_each_check_records_its_own_verdict(self):
        """Which signal fired has to stay visible: the ratio check alone
        currently catches all seven, so if the goals check ever becomes the
        one doing the work, that is a change in the provider's failure mode
        and must be readable rather than inferred."""
        v = mls_stats.xg_plausibility(BROKEN["CIN-VAN 4:3"])
        assert v["checks"]["ratio"]["passed"] is False
        assert v["checks"]["goals"]["passed"] is False
        assert v["checks"]["ratio"]["xg_per_own_sot"] == pytest.approx(
            1.3711 / 12, abs=1e-4)
        # 4:3 on 1.37 expected goals — the scoreline the xG cannot explain
        assert v["checks"]["goals"]["total_goals"] == 7
        assert v["checks"]["goals"]["tail_p"] < 0.001

    def test_degrades_to_the_goals_check_without_shot_counts(self):
        """A row carrying xG and goals but no shot volume — the shape
        `ApiFootballFixtureXg` stores for the other leagues — must apply the
        goals check and skip the ratio one, rather than silently passing
        everything. This is what makes the function reusable on that path
        without a rewrite (see docs/MLS-XG-QUARANTINE.md §7)."""
        v = mls_stats.xg_plausibility([{"xg": 0.19, "goals": 2},
                                       {"xg": 0.14, "goals": 1}])
        assert v["quarantined"] is True
        assert v["checks"]["ratio"]["applicable"] is False
        assert v["checks"]["goals"]["passed"] is False

    def test_disabling_the_guard_still_measures_and_records(self, monkeypatch):
        """An off switch that also stops measuring would hide the next
        incident the way the last one hid behind {"created": 0}. Disabled
        withholds the VERDICT only — the checks still run and the failure is
        still recorded in the reason."""
        monkeypatch.setattr(config, "MLS_XG_GUARD_ENABLED", False)
        v = mls_stats.xg_plausibility(BROKEN["SKC-MIN 2:1"])
        assert v["quarantined"] is False
        assert v["reason"].startswith("guard_disabled: ")
        assert v["checks"]["ratio"]["passed"] is False


# --- ingestion and what the ratings consume ------------------------------

def _schedule(ko, match_id="MLS-MAT-TEST01", goals=(2, 1)):
    return {"schedule": [{
        "match_id": match_id,
        "planned_kickoff_time": ko.isoformat().replace("+00:00", "Z"),
        "home_team_three_letter_code": "CLB",
        "away_team_three_letter_code": "NYC",
        "home_team_id": "MLS-CLU-CLB", "away_team_id": "MLS-CLU-NYC",
        "home_team_name": "Columbus Crew", "away_team_name": "NYC FC",
        "home_team_goals": goals[0], "away_team_goals": goals[1],
        "match_status": "finalWhistle"}], "next_page_token": None}


def _payload(xg_h, g_h, sot_h, xg_a, g_a, sot_a, data_status="postmatch"):
    def team(code, cid, role, xg, goals, conceded, sot):
        return {"team_id": cid, "team_three_letter_code": code,
                "team_role": role, "goals": goals, "goals_conceded": conceded,
                "xG": xg, "shots_at_goal_sum": sot + 4,
                "shots_at_goal_inside_box": sot,
                "shots_at_goal_outside_box": 4,
                "shots_on_target": sot, "corner_kicks_sum": 5,
                "passes_and_crosses_successful_sum": 400,
                "passes_and_crosses_sum": 480}
    return {"match_statistics_list": [{"match_statistics": {
        "match_id": "MLS-MAT-TEST01", "data_status": data_status,
        "scope": "match",
        "team_statistics": [
            team("CLB", "MLS-CLU-CLB", "home", xg_h, g_h, g_a, sot_h),
            team("NYC", "MLS-CLU-NYC", "away", xg_a, g_a, g_h, sot_a)]}}]}


class TestIngestAndConsumption:
    def _seed(self, s, goals=(2, 1)):
        identity.seed_teams(CANNED_ESPN)
        clb = identity.resolve_espn_name("Columbus Crew")
        nyc = identity.resolve_espn_name("New York City FC")
        ko = datetime(2026, 7, 23, 0, 30, tzinfo=UTC)
        fx = Fixture(competition_slug="mls-2026", espn_event_id="e901",
                     home_team_id=clb.id, away_team_id=nyc.id,
                     current_kickoff_utc=ko, status="post",
                     home_goals=goals[0], away_goals=goals[1])
        s.add(fx)
        s.commit()
        return clb, nyc, fx, ko

    def _patch(self, monkeypatch, ko, payload, goals=(2, 1)):
        monkeypatch.setattr(mls_stats, "THROTTLE_SECONDS", 0)
        sched = _schedule(ko, goals=goals)

        def fake_get(path, params=None, quiet_404=False):
            if path.startswith("matches/seasons"):
                return sched
            if path.startswith("statistics/clubs/matches"):
                return payload
            return None
        monkeypatch.setattr(mls_stats, "_get", fake_get)

    def _ingest_broken(self, s, monkeypatch, goals=(2, 1)):
        """The SKC-MIN shape: 3 goals on 0.334 total xG over 15 SoT."""
        _, _, fx, ko = self._seed(s, goals=goals)
        self._patch(monkeypatch, ko,
                    _payload(0.1309, goals[0], 7, 0.2029, goals[1], 8),
                    goals=goals)
        res = mls_stats.ingest_match_stats(gte="2026-07-01", lte="2026-08-01",
                                          with_players=False)
        return fx, res

    def test_ingest_marks_both_rows_and_keeps_the_raw_xg(
            self, live_session, monkeypatch):
        """The verdict is recorded BESIDE the provider's number, never
        substituted for it: historical evidence is not rewritten to improve
        a result, and a later threshold change has to be re-derivable from
        what was actually received."""
        fx, res = self._ingest_broken(live_session, monkeypatch)
        assert res["ingested"] == 1
        assert res["xg_quarantined"] == 1
        rows = {r.side: r for r in live_session.query(MlsTeamMatchStat).all()}
        assert set(rows) == {"home", "away"}
        for side in ("home", "away"):
            assert rows[side].xg_quarantined is True
            assert "xg_per_own_sot=" in rows[side].xg_quarantine_reason
        # raw provider values intact
        assert rows["home"].xg == pytest.approx(0.1309)
        assert rows["away"].xg == pytest.approx(0.2029)

    def test_records_the_providers_own_final_claim(
            self, live_session, monkeypatch):
        """data_status="postmatch" on a row the guard rejected is the whole
        argument for deriving the guard instead of reading a flag. Storing
        it makes that argument auditable from the table."""
        self._ingest_broken(live_session, monkeypatch)
        rows = live_session.query(MlsTeamMatchStat).all()
        assert {r.data_status for r in rows} == {"postmatch"}
        assert {r.scope for r in rows} == {"match"}

    def test_a_plausible_match_is_marked_screened_not_null(
            self, live_session, monkeypatch):
        """False means "screened and passed"; NULL means "never screened".
        Conflating them converts missing evidence into confidence."""
        _, _, fx, ko = self._seed(live_session)
        self._patch(monkeypatch, ko, _payload(1.80, 2, 6, 0.90, 1, 3))
        mls_stats.ingest_match_stats(gte="2026-07-01", lte="2026-08-01",
                                     with_players=False)
        rows = live_session.query(MlsTeamMatchStat).all()
        assert all(r.xg_quarantined is False for r in rows)
        assert all(r.xg_quarantine_reason is None for r in rows)

    def test_default_config_does_not_change_what_the_ratings_consume(
            self, live_session, monkeypatch):
        """THE REGRESSION GUARD. Detection lands without touching a single
        model output: with MLS_XG_QUARANTINE_EXCLUDE at its default, a
        quarantined fixture is still in the xG map, exactly as today.

        Flipping the flag is a modelling change owed an evaluation and
        sequenced between slates (AGENTS.md §12). If this test ever starts
        failing because the default moved, that sequencing was skipped."""
        assert config.MLS_XG_QUARANTINE_EXCLUDE is False
        fx, _ = self._ingest_broken(live_session, monkeypatch)
        xg = mls_stats.team_xg_map()
        assert fx.id in xg
        assert xg[fx.id]["home"]["xg"] == pytest.approx(0.1309)

    def test_excluding_drops_the_whole_fixture_not_one_side(
            self, live_session, monkeypatch):
        """Dropping a single side would leave the opponent consuming the
        rejected number through its own `xg_against`, which is the
        denormalized copy of it. The fixture has to leave the map entirely
        to land in the no-xG state fit() already handles."""
        fx, _ = self._ingest_broken(live_session, monkeypatch)
        monkeypatch.setattr(config, "MLS_XG_QUARANTINE_EXCLUDE", True)
        xg = mls_stats.team_xg_map()
        assert fx.id not in xg

    def test_excluded_fixture_falls_back_to_the_goals_rating(
            self, live_session, monkeypatch):
        """The end-to-end property: a quarantined fixture must degrade to
        goals, not vanish. fit() still counts the match in `games` and in
        the goals ratings — only its xG stops contributing."""
        fx, _ = self._ingest_broken(live_session, monkeypatch)
        monkeypatch.setattr(config, "MLS_XG_QUARANTINE_EXCLUDE", True)
        s = live_db.get_session()
        rows = model_mls._completed(s)
        s.close()
        as_of = datetime(2026, 8, 1, tzinfo=UTC)
        fit = model_mls.fit(rows, as_of,
                            xg_by_fixture=mls_stats.team_xg_map(),
                            xg_alpha=1.0)
        # the fixture is still fitted; it simply carries no xG
        assert fit["n_fixtures"] == len(rows) == 1
        assert fit["xg_coverage"] == 0.0
        # and the ratings equal the pure-goals fit for it
        goals_only = model_mls.fit(rows, as_of, xg_alpha=0.0)
        assert fit["ratings"] == goals_only["ratings"]

    def test_coverage_does_not_count_broken_xg_as_covered(
            self, live_session, monkeypatch):
        """A coverage number that counts a provably-wrong xG as covered is
        a coverage number that lies."""
        self._ingest_broken(live_session, monkeypatch)
        cov = mls_stats.coverage()["team_stats"]
        assert cov["matches_with_xg"] == 1
        assert cov["matches_xg_quarantined"] == 1
        assert cov["matches_with_usable_xg"] == 0

    def test_quarantine_report_separates_never_screened_from_passed(
            self, live_session, monkeypatch):
        """Rows written before the guard existed read NULL. They are not
        passes, and the report must not present them as such."""
        fx, _ = self._ingest_broken(live_session, monkeypatch)
        # a row from before the guard: verdict NULL
        live_session.add(MlsTeamMatchStat(
            fixture_id=fx.id + 1, team_id=1, side="home", xg=1.1,
            xg_against=0.9, goals=1, goals_conceded=0, shots_on_target=5))
        live_session.commit()

        rep = mls_stats.quarantine_report()
        assert rep["rows_total"] == 3
        assert rep["rows_screened"] == 2
        assert rep["rows_never_screened"] == 1
        assert rep["rows_quarantined"] == 2
        assert rep["fixtures_quarantined"] == 1
        assert rep["excluded_from_ratings"] is False
        assert rep["provider_data_status_on_quarantined"] == {"postmatch": 2}
        assert len(rep["quarantined"]) == 2

    def test_quarantine_count_is_reported_even_when_zero(
            self, live_session, monkeypatch):
        """An absent key reads as "the guard did not run", which is the
        claim that hides a regression. Zero must be stated."""
        _, _, fx, ko = self._seed(live_session)
        self._patch(monkeypatch, ko, _payload(1.80, 2, 6, 0.90, 1, 3))
        res = mls_stats.ingest_match_stats(gte="2026-07-01", lte="2026-08-01",
                                          with_players=False)
        assert res["xg_quarantined"] == 0
        assert res["xg_quarantine_excluded_from_ratings"] is False
