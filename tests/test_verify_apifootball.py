"""The API-Football harness's own logic, against recorded payloads.

The point of these tests is narrow and specific: **prove each check FIRES
before it ever meets the real API**, so that a PASS in a live run means the
check looked and found something, not that the check is broken and silent.

Every payload lives in `tests/fixtures/apifootball/` with a `_provenance`
block naming what it is:

  RECORDED  captured verbatim from a live API-Football PRO response on
            2026-07-29 and extracted from
            research_archive/apifootball_verification_2026-07-29.run2.json.
  DERIVED   built from a RECORDED payload by deleting or re-pairing part of
            it. Used only where the live plan never produced the failure mode
            — there was no xG-less payload, no 200-with-empty-body and no
            populated pre-match xG to capture, which is the good news and also
            why those three have to be constructed to prove the checks fire.

That distinction is not pedantry. `AGENTS.md` §6 forbids blurring evidence
classes, and calling a hand-paired payload "recorded" would be exactly that.

No network. The pure check functions take payloads and return verdicts, so
the whole decision layer is testable without a key.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from datetime import datetime, timezone

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FIXTURES = _ROOT / "tests" / "fixtures" / "apifootball"


def _load_harness():
    """Import scripts/verify_apifootball.py by path — it is a standalone
    script, not a package member, and imports nothing from this repo."""
    p = _ROOT / "scripts" / "verify_apifootball.py"
    spec = importlib.util.spec_from_file_location("verify_apifootball", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vaf = _load_harness()


def payload(name: str):
    doc = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return doc["payload"]


def provenance(name: str) -> dict:
    doc = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return doc["_provenance"]


NOW = datetime(2026, 7, 29, 23, 30, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


# ==========================================================================
# the four recorded payloads that must each FAIL their check
# ==========================================================================

class TestEachRecordedFixtureFailsItsCheck:
    """The core proof. Four payloads, four checks, four failures — plus the
    positive control that keeps a failure from being indistinguishable from a
    parser that never finds anything."""

    def test_positive_control_payload_with_xg_passes_check1(self):
        """Recorded MLS statistics with real xG 3.27 / 1.28. If this did not
        PASS, every other CHECK 1 failure below would be meaningless."""
        r = vaf.check1_league("mls", 2026, 1490359,
                              payload("recorded_statistics_with_xg"))
        assert r["status"] == vaf.PASS, r["reasons"]
        vals = sorted(t["xg_value"] for t in r["measurements"]["teams"])
        assert vals == [1.28, 3.27]
        assert all(t["xg_present"] for t in r["measurements"]["teams"])

    def test_payload_without_xg_fails_check1_and_says_type_is_absent(self):
        r = vaf.check1_league("epl", 2025, 1379342,
                              payload("derived_statistics_no_xg_type"))
        assert r["status"] == vaf.FAIL
        assert r["measurements"]["xg_statistic_type_seen"] is False
        # the wording matters: "absent entirely" is a different finding from
        # "present but null", and the brief calls it the most important thing
        # to report cleanly
        joined = " ".join(r["reasons"])
        assert "ABSENT FROM THE RESPONSE ENTIRELY" in joined
        # and it must show what WAS on offer, not just what was missing
        assert "Shots on Goal" in joined
        assert r["measurements"]["statistic_types_offered"]

    def test_200_with_empty_body_fails_check2_in_those_words(self):
        r = vaf.check2_league("epl", 2026, payload("derived_200_empty_plan_error"))
        assert r["status"] == vaf.FAIL
        assert r["measurements"]["fixtures_counted"] == 0
        joined = " ".join(r["reasons"])
        assert "HTTP 200 WITH AN EMPTY response ARRAY" in joined
        assert "THIS IS A FAILURE, NOT A SUCCESS" in joined
        # the provider's own error text is surfaced, not swallowed
        assert any("plan" in n for n in r["notes"])

    def test_populated_pre_match_xg_is_a_critical_fail_naming_leakage(self):
        ns = payload("recorded_fixture_not_started")["response"][0]
        r = vaf.check4_league(
            "mls", ns, payload("derived_statistics_prematch_populated_xg"),
            NOW_TS)
        assert r["status"] == vaf.CRITICAL_FAIL
        joined = " ".join(r["reasons"])
        assert "CRITICAL FAIL" in joined
        assert "PRE-MATCH xG IS POPULATED" in joined
        assert "DISQUALIFYING DEFECT" in joined
        assert "leakage" in joined.lower()
        # exit code 3 is reserved for exactly this
        rep = {"checks": {"check1": {"mls": {"status": vaf.PASS}},
                          "check2": {"mls": {"status": vaf.PASS}},
                          "check3": {"team_id": {"status": vaf.PASS},
                                     "player_id": {"status": vaf.PASS},
                                     "rosters": {}},
                          "check4": {"mls": r}}}
        v, code, failing = vaf.verdict(rep)
        assert (v, code) == ("DROP", 3)
        assert "CHECK 4" in failing[0]

    def test_every_fixture_declares_its_evidence_class(self):
        """A DERIVED payload described as RECORDED would be exactly the
        evidence-class blurring AGENTS.md §6 forbids."""
        for f in sorted(_FIXTURES.glob("*.json")):
            prov = json.loads(f.read_text(encoding="utf-8"))["_provenance"]
            assert prov["kind"] in ("RECORDED", "DERIVED"), f.name
            assert prov.get("note"), f.name
            if prov["kind"] == "DERIVED":
                assert f.name.startswith("derived_"), f.name
            else:
                assert f.name.startswith("recorded_"), f.name


# ==========================================================================
# CHECK 1 — xG presence, and everything that must NOT count as presence
# ==========================================================================

class TestCheck1XgPresence:
    def _with_xg(self, home_value, away_value, type_name="expected_goals"):
        base = json.loads(json.dumps(payload("recorded_statistics_with_xg")))
        for entry, v in zip(base["response"], (home_value, away_value)):
            for st in entry["statistics"]:
                t = vaf.norm_stat_type(st.get("type"))
                if "expectedgoals" in t or t == "xg":
                    st["type"], st["value"] = type_name, v
        return base

    @pytest.mark.parametrize("bad", [None, "", "-", "null", "n/a", "abc"])
    def test_hollow_values_are_absence_not_presence(self, bad):
        r = vaf.check1_league("mls", 2026, 1, self._with_xg(bad, bad))
        assert r["status"] == vaf.FAIL
        assert "absence, not zero" in " ".join(r["reasons"])

    @pytest.mark.parametrize("zero", [0, 0.0, "0", "0.0", "0.00"])
    def test_zero_is_not_presence(self, zero):
        r = vaf.check1_league("mls", 2026, 1, self._with_xg(zero, zero))
        assert r["status"] == vaf.FAIL
        assert "a zero is not presence" in " ".join(r["reasons"])

    def test_one_team_short_still_fails(self):
        """'Both teams' means both. A single-sided xG is not coverage."""
        r = vaf.check1_league("mls", 2026, 1, self._with_xg("1.9", None))
        assert r["status"] == vaf.FAIL

    @pytest.mark.parametrize("name", ["expected_goals", "Expected Goals",
                                      "expected goals (xG)", "xG"])
    def test_accepted_type_spellings(self, name):
        r = vaf.check1_league("mls", 2026, 1, self._with_xg("1.5", "0.8", name))
        assert r["status"] == vaf.PASS, (name, r["reasons"])

    @pytest.mark.parametrize("name", ["expected_goals_against",
                                      "Expected Goals Conceded",
                                      "goals_prevented", "xGA"])
    def test_the_opponents_number_does_not_count_as_coverage(self, name):
        """Accepting xGA would let 'xG coverage' be claimed on the strength of
        a statistic that is not the team's own expected goals."""
        r = vaf.check1_league("mls", 2026, 1, self._with_xg("1.5", "0.8", name))
        assert r["status"] == vaf.FAIL
        assert "ABSENT FROM THE RESPONSE ENTIRELY" in " ".join(r["reasons"])

    def test_empty_statistics_for_a_completed_fixture_fails(self):
        r = vaf.check1_league("mls", 2026, 1, {"response": [], "results": 0})
        assert r["status"] == vaf.FAIL
        assert "HTTP 200 WITH AN EMPTY response ARRAY" in " ".join(r["reasons"])

    def test_same_team_twice_cannot_pass_as_two_teams(self):
        base = self._with_xg("1.5", "0.8")
        base["response"][1]["team"] = dict(base["response"][0]["team"])
        r = vaf.check1_league("mls", 2026, 1, base)
        assert r["status"] == vaf.FAIL
        assert "distinct teams" in " ".join(r["reasons"])


# ==========================================================================
# CHECK 2 — counts, never status codes, never the provider's own field
# ==========================================================================

class TestCheck2CountsNotStatusCodes:
    def test_non_zero_count_passes(self):
        r = vaf.check2_league("mls", 2026,
                              {"results": 2, "errors": [],
                               "response": [{"fixture": {}}, {"fixture": {}}]})
        assert r["status"] == vaf.PASS

    def test_results_field_lying_high_is_reported_and_the_count_wins(self):
        """`results: 380` beside an empty array is the incident shape. The
        array is what exists, so it fails — and the disagreement is printed."""
        r = vaf.check2_league("mls", 2026, {"results": 380, "errors": [],
                                            "response": []})
        assert r["status"] == vaf.FAIL
        assert any("the count wins" in n for n in r["notes"])

    def test_results_field_lying_low_is_reported_but_the_count_still_wins(self):
        r = vaf.check2_league("mls", 2026,
                              {"results": 0, "errors": [],
                               "response": [{"fixture": {}}]})
        assert r["status"] == vaf.PASS
        assert any("the count wins" in n for n in r["notes"])

    def test_errors_dict_inside_a_200_is_surfaced(self):
        errs = vaf.errors_of({"errors": {"token": "invalid"}})
        assert errs == ["token: invalid"]

    def test_a_non_dict_body_is_not_mistaken_for_data(self):
        assert vaf.response_items("<html>403</html>") == []
        assert vaf.response_items(None) == []
        r = vaf.check2_league("mls", 2026, "<html>403</html>")
        assert r["status"] == vaf.FAIL


class TestSeasonDerivation:
    """CHECK 2 asks about the season an active slate lives in, derived from
    the calendar — not the season the provider flags 'current', whose list is
    plan-filtered."""

    @pytest.mark.parametrize("style,month,year,expected", [
        ("split_year", 7, 2026, 2026),
        ("split_year", 12, 2026, 2026),
        ("split_year", 1, 2027, 2026),
        ("split_year", 6, 2026, 2025),
        ("calendar_year", 1, 2026, 2026),
        ("calendar_year", 11, 2026, 2026),
    ])
    def test_calendar_season(self, style, month, year, expected):
        now = datetime(year, month, 15, tzinfo=timezone.utc)
        assert vaf.calendar_season(style, now) == expected


# ==========================================================================
# CHECK 3 — identity, on recorded payloads
# ==========================================================================

class TestCheck3Identity:
    def test_recorded_team_id_is_stable_across_two_fixtures(self):
        r = vaf.check3_team_id("San Jose Earthquakes",
                               payload("recorded_fixture_a"),
                               payload("recorded_fixture_b"), 1490359, 1490344)
        assert r["status"] == vaf.PASS
        assert r["measurements"]["team_id_a"] == r["measurements"]["team_id_b"]
        assert r["measurements"]["team_id_a"] == 1596

    def test_an_unstable_team_id_fails_and_prints_both(self):
        b = json.loads(json.dumps(payload("recorded_fixture_b")))
        for side in ("home", "away"):
            t = b["response"][0]["teams"][side]
            if t["id"] == 1596:
                t["id"] = 999999
        r = vaf.check3_team_id("San Jose Earthquakes",
                               payload("recorded_fixture_a"), b, 1490359, 1490344)
        assert r["status"] == vaf.FAIL
        assert "UNSTABLE TEAM ID" in " ".join(r["reasons"])
        assert "999999" in " ".join(r["reasons"])

    def test_a_team_absent_from_one_fixture_is_indeterminate_not_stable(self):
        r = vaf.check3_team_id("Nobody FC", payload("recorded_fixture_a"),
                               payload("recorded_fixture_b"), 1, 2)
        assert r["status"] == vaf.INDETERMINATE
        assert "not the same as stable" in " ".join(r["reasons"])

    def test_recorded_player_id_is_stable(self):
        r = vaf.check3_player_id(payload("recorded_players_a"),
                                 payload("recorded_players_b"), 1596)
        assert r["status"] == vaf.PASS
        assert r["measurements"]["shared_by_name"] >= 1
        assert not r["measurements"]["mismatched"]

    def test_an_unstable_player_id_fails(self):
        b = json.loads(json.dumps(payload("recorded_players_b")))
        for entry in b["response"]:
            if (entry.get("team") or {}).get("id") == 1596:
                entry["players"][0]["player"]["id"] = 424242
        r = vaf.check3_player_id(payload("recorded_players_a"), b, 1596)
        assert r["status"] == vaf.FAIL
        assert "UNSTABLE PLAYER ID" in " ".join(r["reasons"])

    def test_no_shared_player_is_indeterminate_which_counts_as_fail(self):
        r = vaf.check3_player_id({"response": []}, {"response": []}, 1596)
        assert r["status"] == vaf.INDETERMINATE
        assert r["status"] in vaf.FAILING
        assert "missing evidence is not confidence" in " ".join(r["reasons"])


class TestRosterResolution:
    ROSTER = json.loads(
        (_ROOT / "scripts" / "apifootball_espn_roster.json").read_text(
            encoding="utf-8"))

    def test_the_recorded_mls_roster_resolves_one_to_one(self):
        cfg = self.ROSTER["leagues"]["mls"]
        names = [i["team"]["name"]
                 for i in payload("recorded_teams_mls")["response"]]
        res = vaf.resolve_roster(names, cfg["espn_team_names"], cfg["aliases"])
        assert res["apifootball_teams"] == 30
        assert res["resolved"] == 30
        assert res["rate"] == 1.0
        assert res["ambiguous"] == []
        assert res["espn_name_collisions"] == {}
        # nothing rests on the fuzzy tiers
        assert res["tiers"]["token_set"] == 0
        assert res["tiers"]["containment"] == 0

    def test_an_ambiguous_match_fails_explicitly_and_never_picks(self):
        """AGENTS.md §6: an ambiguous identity match must fail explicitly,
        never silently pick a team."""
        res = vaf.resolve_roster(["United"], ["United FC", "United SC"], {})
        assert res["resolved"] == 0
        assert res["tiers"]["ambiguous"] == 1
        assert res["rows"][0]["espn_name"] is None
        assert sorted(res["ambiguous"][0]["candidates"]) == ["United FC",
                                                             "United SC"]
        r = vaf.check3_roster("mls", res)
        assert r["status"] == vaf.FAIL
        assert "AMBIGUOUS IDENTITY" in " ".join(r["reasons"])

    def test_two_provider_teams_claiming_one_espn_name_fails(self):
        res = vaf.resolve_roster(["Arsenal", "Arsenal FC"], ["Arsenal"], {})
        assert res["espn_name_collisions"] == {"Arsenal": ["Arsenal",
                                                           "Arsenal FC"]}
        r = vaf.check3_roster("mls", res)
        assert r["status"] == vaf.FAIL
        assert "claim one ESPN name" in " ".join(r["reasons"])

    def test_unresolved_names_are_named_not_just_counted(self):
        res = vaf.resolve_roster(["Mazatlan", "Toluca"], ["Toluca"], {})
        assert res["unresolved_apifootball_names"] == ["Mazatlan"]
        r = vaf.check3_roster("ligamx", res)
        assert "Mazatlan" in " ".join(r["notes"])

    def test_an_empty_team_list_fails_in_the_counts_words(self):
        res = vaf.resolve_roster([], ["Toluca"], {})
        r = vaf.check3_roster("ligamx", res)
        assert r["status"] == vaf.FAIL
        assert "HTTP 200 WITH AN EMPTY response ARRAY" in " ".join(r["reasons"])

    def test_mls_threshold_is_stricter_than_the_others(self):
        """The MLS reference is current; the other three are measured stale.
        The asymmetry is deliberate and pre-registered."""
        t = vaf.CRITERIA["check3_identity"]["min_resolution_rate"]
        assert t["mls"] == 1.0
        assert t["epl"] == t["laliga"] == t["ligamx"] == 0.75

    def test_alias_targets_all_exist_in_their_own_reference(self):
        """A frozen alias pointing at a name the roster does not contain is
        dead config, and the harness reports it."""
        for lk, cfg in self.ROSTER["leagues"].items():
            names = set(cfg["espn_team_names"])
            dead = {k: v for k, v in cfg["aliases"].items() if v not in names}
            assert not dead, (lk, dead)


class TestNameNormalisation:
    @pytest.mark.parametrize("a,b", [
        ("D.C. United", "DC United"),              # amendment A1
        ("U.N.A.M. - Pumas", "UNAM Pumas"),        # amendment A1
        ("CF Montréal", "CF Montreal"),            # accent fold
        ("Atlético Madrid", "Atletico Madrid"),
        ("Columbus Crew", "Columbus Crew SC"),     # generic token
        ("Houston Dynamo FC", "Houston Dynamo"),
    ])
    def test_names_that_must_normalise_together(self, a, b):
        assert vaf.norm_team(a) == vaf.norm_team(b), (vaf.norm_team(a),
                                                      vaf.norm_team(b))

    @pytest.mark.parametrize("a,b", [
        ("Manchester City", "Manchester United"),
        ("Real Madrid", "Real Sociedad"),
        ("LAFC", "LA Galaxy"),
        ("New York City FC", "Red Bull New York"),
    ])
    def test_names_that_must_stay_apart(self, a, b):
        assert vaf.norm_team(a) != vaf.norm_team(b)

    def test_a_name_is_never_normalised_away_to_nothing(self):
        """'FC' is a generic token, but a club literally called FC must not
        normalise to the empty string and then match everything."""
        assert vaf.norm_team("FC") != ""
        assert vaf.norm_team("SC") != ""

    def test_lafc_survives_generic_token_stripping(self):
        """'fc' is dropped as a whole TOKEN, never as a substring."""
        assert vaf.norm_team("LAFC") == "lafc"


# ==========================================================================
# CHECK 4 — leakage
# ==========================================================================

class TestCheck4Leakage:
    NS = payload("recorded_fixture_not_started")["response"][0]

    def test_recorded_empty_pre_match_statistics_passes(self):
        r = vaf.check4_league("mls", self.NS,
                              payload("recorded_statistics_prematch_empty"),
                              NOW_TS)
        assert r["status"] == vaf.PASS
        # the asymmetry is stated in the output, not left implicit
        assert any("desired result" in n for n in r["notes"])

    def test_a_zero_placeholder_is_reported_but_not_failed(self):
        base = json.loads(json.dumps(
            payload("derived_statistics_prematch_populated_xg")))
        for entry in base["response"]:
            for st in entry["statistics"]:
                if vaf.is_xg_type(st.get("type")):
                    st["value"] = 0
        r = vaf.check4_league("mls", self.NS, base, NOW_TS)
        assert r["status"] == vaf.PASS
        assert any("carry no outcome information" in n for n in r["notes"])

    def test_a_null_placeholder_is_reported_but_not_failed(self):
        base = json.loads(json.dumps(
            payload("derived_statistics_prematch_populated_xg")))
        for entry in base["response"]:
            for st in entry["statistics"]:
                if vaf.is_xg_type(st.get("type")):
                    st["value"] = None
        r = vaf.check4_league("mls", self.NS, base, NOW_TS)
        assert r["status"] == vaf.PASS

    def test_a_provider_claiming_NS_after_kickoff_is_not_trusted(self):
        """status.short is a provider claim; the timestamp is checked too."""
        past = json.loads(json.dumps(self.NS))
        past["fixture"]["timestamp"] = NOW_TS - 3600
        r = vaf.check4_league("mls", past,
                              payload("recorded_statistics_prematch_empty"),
                              NOW_TS)
        assert r["status"] == vaf.INDETERMINATE
        assert "never as clean" in " ".join(r["reasons"])

    def test_a_missing_not_started_fixture_is_indeterminate_which_fails(self):
        r = vaf.check4_league("mls", None, {"response": []}, NOW_TS)
        assert r["status"] == vaf.INDETERMINATE
        assert r["status"] in vaf.FAILING

    def test_xg_shaped_keys_on_the_fixture_object_are_reported(self):
        """An empty statistics endpoint only proves the statistics endpoint is
        empty. A post-hoc field on the fixture would leak just as hard."""
        leaky = json.loads(json.dumps(self.NS))
        leaky["score"]["expected_goals"] = {"home": 1.4, "away": 0.9}
        r = vaf.check4_league("mls", leaky,
                              payload("recorded_statistics_prematch_empty"),
                              NOW_TS)
        assert r["measurements"]["fixture_object_xg_keys"]
        assert any("fixture object itself" in n for n in r["notes"])

    def test_the_recorded_not_started_fixture_has_no_xg_keys(self):
        assert vaf.scan_fixture_for_xg_keys(self.NS) == []


# ==========================================================================
# verdict wiring
# ==========================================================================

class TestVerdict:
    def _all_pass(self):
        ok = {"status": vaf.PASS}
        return {"checks": {
            "check1": {lk: ok for lk in ("epl", "laliga", "ligamx", "mls")},
            "check2": {lk: ok for lk in ("epl", "laliga", "ligamx", "mls")},
            "check3": {"team_id": ok, "player_id": ok,
                       "rosters": {lk: ok for lk in ("epl", "laliga",
                                                      "ligamx", "mls")}},
            "check4": {lk: ok for lk in ("epl", "laliga", "ligamx", "mls")}}}

    def test_keep_requires_all_four(self):
        assert vaf.verdict(self._all_pass()) == ("KEEP", 0, [])

    @pytest.mark.parametrize("check,label", [
        ("check1", "CHECK 1"), ("check2", "CHECK 2"), ("check4", "CHECK 4")])
    def test_any_single_failure_drops_and_names_the_check(self, check, label):
        rep = self._all_pass()
        rep["checks"][check]["laliga"] = {"status": vaf.FAIL}
        v, code, failing = vaf.verdict(rep)
        assert (v, code) == ("DROP", 1)
        assert label in failing[0] and "laliga" in failing[0]

    def test_a_roster_failure_drops_check3(self):
        rep = self._all_pass()
        rep["checks"]["check3"]["rosters"]["mls"] = {"status": vaf.FAIL}
        v, code, failing = vaf.verdict(rep)
        assert (v, code) == ("DROP", 1)
        assert "roster:mls" in failing[0]

    def test_indeterminate_never_reads_as_clean(self):
        rep = self._all_pass()
        rep["checks"]["check1"]["mls"] = {"status": vaf.INDETERMINATE}
        assert vaf.verdict(rep)[0] == "DROP"

    def test_critical_leakage_gets_its_own_exit_code(self):
        rep = self._all_pass()
        rep["checks"]["check4"]["mls"] = {"status": vaf.CRITICAL_FAIL}
        assert vaf.verdict(rep)[1] == 3


# ==========================================================================
# tamper-evidence and secret handling
# ==========================================================================

class TestTamperEvidence:
    def test_the_committed_criteria_fingerprint_matches_the_criteria(self):
        assert vaf.criteria_fingerprint() == vaf.CRITERIA_FINGERPRINT

    def test_the_committed_roster_fingerprint_matches_the_roster(self):
        assert vaf.roster_fingerprint() == vaf.ROSTER_FINGERPRINT

    def test_moving_a_threshold_changes_the_fingerprint(self):
        """The guard is only worth having if it actually detects a retune."""
        import copy
        tuned = copy.deepcopy(vaf.CRITERIA)
        tuned["check3_identity"]["min_resolution_rate"]["mls"] = 0.5
        assert vaf.criteria_fingerprint(tuned) != vaf.CRITERIA_FINGERPRINT

    def test_editing_the_roster_changes_its_fingerprint(self, tmp_path):
        """This is the guard that was missing on 2026-07-29, when two aliases
        were added to the frozen roster — precisely the two names that had
        just failed CHECK 3 — flipping DROP to KEEP with the criteria
        fingerprint unchanged. See
        research_archive/apifootball_tuning_incident_2026-07-29.json."""
        doc = json.loads(vaf.ROSTER_PATH.read_text(encoding="utf-8"))
        doc["leagues"]["mls"]["aliases"]["DC United"] = "D.C. United"
        p = tmp_path / "tuned.json"
        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        assert vaf.roster_fingerprint(p) != vaf.ROSTER_FINGERPRINT

    def test_archive_path_never_overwrites_an_earlier_run(self):
        base = "apifootball_verification_2026-07-29.json"
        seen = {base}
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        p2 = vaf.archive_path(now, exists=lambda q: q.name in seen)
        assert p2.name == "apifootball_verification_2026-07-29.run2.json"
        seen.add(p2.name)
        p3 = vaf.archive_path(now, exists=lambda q: q.name in seen)
        assert p3.name == "apifootball_verification_2026-07-29.run3.json"

    def test_archive_path_is_the_plain_dated_name_when_free(self):
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        p = vaf.archive_path(now, exists=lambda q: False)
        assert p.name == "apifootball_verification_2026-07-29.json"


class TestSecretHandling:
    KEY = "abc123deadbeefsecretkey"

    def test_redaction_covers_the_key_and_a_whitespace_damaged_copy(self):
        out = vaf._redact(f"boom {self.KEY} bang", self.KEY + "\n")
        assert self.KEY not in out
        assert vaf.REDACTED in out

    def test_redaction_is_a_no_op_without_a_key(self):
        assert vaf._redact("hello", "") == "hello"

    def test_the_status_account_block_is_dropped_before_archiving(self):
        """The first live run put the account holder's name and email into a
        file destined for this repo. Scrubbed at the record layer, so any
        endpoint that starts returning an account block is covered."""
        body = {"response": {"account": {"firstname": "Someone",
                                         "email": "someone@example.com"},
                             "subscription": {"plan": "Pro"}}}
        out = vaf.scrub_pii(body)
        blob = json.dumps(out)
        assert "firstname" not in blob
        assert "someone@example.com" not in blob
        assert out["response"]["subscription"] == {"plan": "Pro"}
        # and the original is not mutated in place
        assert body["response"]["account"]["firstname"] == "Someone"

    def test_scrub_pii_passes_ordinary_bodies_through(self):
        body = {"response": [{"team": {"id": 1}}], "results": 1}
        assert vaf.scrub_pii(body) is body

    def test_write_archive_refuses_to_emit_the_key(self, tmp_path, monkeypatch):
        p = tmp_path / "a.json"
        vaf.write_archive(p, {"leaked": f"x{self.KEY}y"}, self.KEY)
        text = p.read_text(encoding="utf-8")
        assert self.KEY not in text
        assert vaf.REDACTED in text

    def test_a_group_readable_key_file_is_refused_not_used(self, tmp_path,
                                                            monkeypatch):
        f = tmp_path / ".apifootball_key"
        f.write_text("secret\n", encoding="utf-8")
        f.chmod(0o644)
        monkeypatch.setattr(vaf, "KEY_FILE", f)
        monkeypatch.delenv("APIFOOTBALL_KEY", raising=False)
        key, source, problems = vaf.load_key()
        assert key == ""
        assert any("600" in p for p in problems)

    def test_a_mode_600_key_file_is_read_and_trimmed(self, tmp_path,
                                                      monkeypatch):
        f = tmp_path / ".apifootball_key"
        f.write_text("  secret \n", encoding="utf-8")
        f.chmod(0o600)
        monkeypatch.setattr(vaf, "KEY_FILE", f)
        monkeypatch.delenv("APIFOOTBALL_KEY", raising=False)
        key, source, problems = vaf.load_key()
        assert key == "secret"
        assert problems == []

    def test_the_env_var_wins_over_the_file(self, tmp_path, monkeypatch):
        f = tmp_path / ".apifootball_key"
        f.write_text("from-file\n", encoding="utf-8")
        f.chmod(0o600)
        monkeypatch.setattr(vaf, "KEY_FILE", f)
        monkeypatch.setenv("APIFOOTBALL_KEY", "from-env")
        key, source, _ = vaf.load_key()
        assert (key, source) == ("from-env", "APIFOOTBALL_KEY")

    def test_the_repos_older_variable_is_never_used_as_a_fallback(
            self, tmp_path, monkeypatch):
        """API_FOOTBALL_KEY may hold the free-tier key, and the free tier was
        measured season-blind. Reading it would answer confidently about the
        wrong plan."""
        monkeypatch.setattr(vaf, "KEY_FILE", tmp_path / "absent")
        monkeypatch.delenv("APIFOOTBALL_KEY", raising=False)
        monkeypatch.setenv("API_FOOTBALL_KEY", "free-tier-key")
        key, source, _ = vaf.load_key()
        assert key == ""
        assert source == "nowhere"


class TestFixtureSelectionIsDeterministic:
    """Selection rules are pre-registered so a fixture cannot be cherry-picked
    into a favourable answer."""

    def _fx(self, fid, ts, short, home="A", away="B", hid=1, aid=2):
        return {"fixture": {"id": fid, "timestamp": ts,
                            "status": {"short": short}},
                "teams": {"home": {"id": hid, "name": home},
                          "away": {"id": aid, "name": away}}}

    def test_prefers_the_most_recent_completed_fixture_over_24h_old(self):
        now = 1_000_000
        fixtures = [self._fx(1, now - 200_000, "FT"),
                    self._fx(2, now - 100_000, "FT"),
                    self._fx(3, now - 600, "FT")]        # too fresh
        chosen, how = vaf.pick_completed_fixture(fixtures, now)
        assert chosen["fixture"]["id"] == 2
        assert "at least 24h old" in how

    def test_falls_back_to_a_fresh_completed_fixture_and_flags_it(self):
        now = 1_000_000
        chosen, how = vaf.pick_completed_fixture(
            [self._fx(3, now - 600, "FT")], now)
        assert chosen["fixture"]["id"] == 3
        assert "LESS THAN 24h OLD" in how

    def test_no_completed_fixture_is_reported_not_guessed(self):
        chosen, how = vaf.pick_completed_fixture(
            [self._fx(1, 2_000_000, "NS")], 1_000_000)
        assert chosen is None
        assert "no completed fixture" in how

    def test_picks_the_soonest_genuinely_unplayed_fixture(self):
        now = 1_000_000
        fixtures = [self._fx(1, now + 500_000, "NS"),
                    self._fx(2, now + 100, "NS"),
                    self._fx(3, now - 100, "NS")]     # NS but already past
        ns = vaf.pick_not_started_fixture(fixtures, now)
        assert ns["fixture"]["id"] == 2

    def test_fixture_pair_is_one_team_across_two_fixtures(self):
        now = 1_000_000
        fixtures = [self._fx(10, now - 100_000, "FT", "Home", "Away", 1, 2),
                    self._fx(11, now - 200_000, "FT", "Other", "Home", 3, 1),
                    self._fx(12, now - 300_000, "FT", "Nope", "Nada", 8, 9)]
        pair = vaf.pick_fixture_pair_for_one_team(fixtures, now)
        assert pair == ("Home", 1, 10, 11)

    def test_no_pair_when_the_team_played_only_once(self):
        now = 1_000_000
        assert vaf.pick_fixture_pair_for_one_team(
            [self._fx(10, now - 100_000, "FT")], now) is None
