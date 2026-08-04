"""The sharp-anchor spike measures two books against each other.

Everything asserted here is about the ways such a script lies quietly:
a missing credential that becomes an empty result, a sport key guessed
from a competition name, a fixture bridged on one club, an orientation
silently inverted so the largest divergences point the wrong way, and a
mean over six fixtures presented as a finding.

The credential tests matter most. A measurement script that produces a
plausible document without a key is worse than one that crashes.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "measure_sharp_anchor", _ROOT / "scripts" / "measure_sharp_anchor.py")
sa = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_sharp_anchor"] = sa
_SPEC.loader.exec_module(sa)

SECRET = "s3cret-odds-key-value"


# --- the credential --------------------------------------------------------

class TestWithoutAKeyItRefuses:
    def test_no_key_anywhere_is_a_named_refusal_not_a_result(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv(sa.KEY_ENV, raising=False)
        monkeypatch.setattr(sa, "KEY_FILE", tmp_path / "absent")
        monkeypatch.setattr(sys, "argv", ["measure_sharp_anchor.py"])
        assert sa.main() == 2
        doc = json.loads(capsys.readouterr().out)
        assert doc["error"] == "no_odds_api_key"
        assert doc["measured"] is False
        assert "how_to_fix" in doc
        # not an empty slate dressed as a run
        assert "ranked_by_max_abs_divergence" not in doc

    def test_a_world_readable_key_file_is_refused_and_says_why(
            self, monkeypatch, tmp_path):
        p = tmp_path / ".odds_api_key"
        p.write_text(SECRET)
        os.chmod(p, 0o644)
        monkeypatch.delenv(sa.KEY_ENV, raising=False)
        monkeypatch.setattr(sa, "KEY_FILE", p)
        key, source, problems = sa.load_key()
        assert key == ""
        assert any("must be 600" in x for x in problems)
        assert SECRET not in " ".join(problems)

    def test_the_key_is_never_printed_in_the_refusal(
            self, monkeypatch, tmp_path, capsys):
        p = tmp_path / ".odds_api_key"
        p.write_text("")                       # present, empty -> refusal
        os.chmod(p, 0o600)
        monkeypatch.delenv(sa.KEY_ENV, raising=False)
        monkeypatch.setattr(sa, "KEY_FILE", p)
        monkeypatch.setattr(sys, "argv", ["measure_sharp_anchor.py"])
        assert sa.main() == 2
        assert SECRET not in capsys.readouterr().out

    def test_a_newline_from_a_dashboard_paste_is_stripped(
            self, monkeypatch, tmp_path):
        p = tmp_path / ".odds_api_key"
        p.write_text(SECRET + "\n")
        os.chmod(p, 0o600)
        monkeypatch.delenv(sa.KEY_ENV, raising=False)
        monkeypatch.setattr(sa, "KEY_FILE", p)
        assert sa.load_key()[0] == SECRET

    def test_the_env_var_wins_and_the_source_is_a_name_not_a_value(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv(sa.KEY_ENV, SECRET)
        monkeypatch.setattr(sa, "KEY_FILE", tmp_path / "unused")
        key, source, problems = sa.load_key()
        assert key == SECRET
        assert source == sa.KEY_ENV and SECRET not in source
        assert problems == []


# --- sport keys are discovered, never guessed ------------------------------

SPORTS = [
    {"key": "soccer_usa_mls", "group": "Soccer", "active": True,
     "title": "MLS", "description": "Major League Soccer"},
    {"key": "soccer_uefa_champs_league", "group": "Soccer", "active": True,
     "title": "UEFA Champions League", "description": "UEFA Champions League"},
    {"key": "soccer_uefa_europa_league", "group": "Soccer", "active": True,
     "title": "UEFA Europa League", "description": "UEFA Europa League"},
    {"key": "soccer_uefa_europa_conference_league", "group": "Soccer",
     "active": True, "title": "UEFA Europa Conference League",
     "description": "UEFA Europa Conference League"},
    {"key": "soccer_brazil_campeonato", "group": "Soccer", "active": True,
     "title": "Brazil Série A", "description": "Brasileirão Série A"},
    {"key": "soccer_brazil_serie_b", "group": "Soccer", "active": True,
     "title": "Brazil Série B", "description": "Brazil Série B"},
    {"key": "basketball_nba", "group": "Basketball", "active": True,
     "title": "NBA", "description": "US Basketball"},
]


class TestSportKeyResolution:
    def test_europa_league_does_not_swallow_the_conference_league(self):
        out = sa.resolve_sport_keys(SPORTS, sa.SPORT_MATCHERS)
        assert out["uel"]["sport_key"] == "soccer_uefa_europa_league"
        assert (out["ecl"]["sport_key"]
                == "soccer_uefa_europa_conference_league")

    def test_serie_b_is_not_mistaken_for_serie_a(self):
        out = sa.resolve_sport_keys(SPORTS, sa.SPORT_MATCHERS)
        assert out["brasileirao"]["sport_key"] == "soccer_brazil_campeonato"

    def test_a_competition_the_key_cannot_see_is_a_named_absence(self):
        out = sa.resolve_sport_keys(SPORTS, sa.SPORT_MATCHERS)
        assert out["leagues-cup"]["absent"] == "sport_not_listed"
        assert "no odds exist" in out["leagues-cup"]["means"]
        assert "sport_key" not in out["leagues-cup"]

    def test_two_candidates_refuse_rather_than_pick_the_first(self):
        twin = SPORTS + [{"key": "soccer_usa_mls_next", "group": "Soccer",
                          "active": True, "title": "MLS Next Pro",
                          "description": "Major League Soccer Next Pro"}]
        out = sa.resolve_sport_keys(twin, {"mls": sa.SPORT_MATCHERS["mls"]})
        assert out["mls"]["absent"] == "sport_key_ambiguous"
        assert len(out["mls"]["candidates"]) == 2

    def test_a_non_soccer_group_is_never_considered(self):
        out = sa.resolve_sport_keys(
            SPORTS, {"mls": (frozenset({"nba"}), frozenset())})
        assert out["mls"]["absent"] == "sport_not_listed"

    def test_an_inactive_sport_is_not_matched(self):
        off = [dict(s, active=False) for s in SPORTS]
        out = sa.resolve_sport_keys(off, {"mls": sa.SPORT_MATCHERS["mls"]})
        assert out["mls"]["absent"] == "sport_not_listed"


# --- devig -----------------------------------------------------------------

class TestDevig:
    def test_a_fair_book_devigs_to_itself(self):
        d = sa.devig_decimal({"home": 2.0, "tie": 4.0, "away": 4.0})
        assert d["normalised"] == {"home": 0.5, "tie": 0.25, "away": 0.25}
        assert d["overround"] == 0.0

    def test_the_overround_is_reported_and_removed_proportionally(self):
        d = sa.devig_decimal({"home": 1.9, "tie": 3.8, "away": 3.8})
        assert d["overround"] > 0
        assert abs(sum(d["normalised"].values()) - 1.0) < 1e-6

    def test_fewer_than_three_legs_is_none_not_a_two_way_partition(self):
        assert sa.devig_decimal({"home": 1.9, "away": 2.1}) is None

    def test_an_impossible_price_is_refused(self):
        assert sa.devig_decimal({"home": 1.0, "tie": 4.0, "away": 4.0}) is None
        assert sa.devig_decimal({"home": "x", "tie": 4.0, "away": 4.0}) is None


def _book(key, home_p, tie_p, away_p, home="Columbus Crew", away="Atlas"):
    return {"key": key, "title": key.title(), "markets": [
        {"key": "h2h", "outcomes": [{"name": home, "price": home_p},
                                    {"name": "Draw", "price": tie_p},
                                    {"name": away, "price": away_p}]}]}


class TestAnchorSelection:
    def test_a_named_sharp_book_wins_and_the_rule_is_recorded(self):
        a = sa.pick_anchor([_book("draftkings", 1.8, 3.6, 4.5),
                            _book("pinnacle", 1.95, 3.9, 4.2)],
                           "Columbus Crew", "Atlas")
        assert a["book"] == "pinnacle"
        assert a["rule"] == "named_sharp:pinnacle"
        assert a["books_priced"] == 2

    def test_without_one_the_thinnest_margin_stands_in_and_says_so(self):
        wide = _book("wide", 1.7, 3.2, 4.0)
        thin = _book("thin", 1.95, 3.95, 4.3)
        a = sa.pick_anchor([wide, thin], "Columbus Crew", "Atlas")
        assert a["book"] == "thin"
        assert a["rule"] == "lowest_overround"
        assert "not sharpness" in a["rule_note"]

    def test_a_book_missing_a_leg_is_skipped_not_partially_used(self):
        half = {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Columbus Crew", "price": 1.9}]}]}
        a = sa.pick_anchor([half, _book("thin", 1.95, 3.95, 4.3)],
                           "Columbus Crew", "Atlas")
        assert a["book"] == "thin"
        assert a["books_priced"] == 1

    def test_no_priced_book_at_all_is_none(self):
        assert sa.pick_anchor([], "Columbus Crew", "Atlas") is None

    def test_a_spread_market_is_not_read_as_a_three_way(self):
        spreads = {"key": "pinnacle", "markets": [{"key": "spreads",
                   "outcomes": [{"name": "Columbus Crew", "price": 1.9},
                                {"name": "Draw", "price": 3.9},
                                {"name": "Atlas", "price": 4.2}]}]}
        assert sa.pick_anchor([spreads], "Columbus Crew", "Atlas") is None


# --- bridging --------------------------------------------------------------

KICK = "2026-08-04T23:45:00+00:00"        # ours carries an offset
START = "2026-08-04T23:45:00Z"            # the provider's carries Z


def _row(home, away, hp=0.50, tp=0.25, ap=0.25, kickoff=KICK):
    return {"home": {"name": home}, "away": {"name": away},
            "kickoff_utc": kickoff,
            "kalshi_event": "KXLEAGUESCUPGAME-26AUG04CLBATL",
            "market_vs_read": {"market_vig": 0.01, "market_three_way": {
                "home": {"p": hp}, "tie": {"p": tp}, "away": {"p": ap}}}}


def _event(home, away, books=None, commence=START):
    if books is None:                     # [] means "listed, nothing priced"
        books = [_book("pinnacle", 2.0, 4.0, 4.0, home=home, away=away)]
    return {"home_team": home, "away_team": away, "bookmakers": books,
            "commence_time": commence}


class TestBridging:
    def test_both_clubs_must_match(self):
        pairs, un = sa.bridge([_row("Columbus Crew", "Atlas")],
                              [_event("Columbus Crew", "Pachuca")])
        assert pairs == []
        assert "matches both clubs" in un[0]["reason"]

    def test_a_corporate_suffix_does_not_block_a_real_match(self):
        pairs, un = sa.bridge([_row("FC Cincinnati", "CF Pachuca")],
                              [_event("Cincinnati", "Pachuca")])
        assert len(pairs) == 1 and un == []

    def test_two_candidate_events_refuse_rather_than_guess(self):
        pairs, un = sa.bridge(
            [_row("Columbus Crew", "Atlas")],
            [_event("Columbus Crew", "Atlas"),
             _event("Columbus Crew", "Atlas")])
        assert pairs == []
        assert "none is attached rather than one guessed" in un[0]["reason"]

    def test_a_reversed_orientation_still_bridges(self):
        pairs, un = sa.bridge([_row("Columbus Crew", "Atlas")],
                              [_event("Atlas", "Columbus Crew")])
        assert len(pairs) == 1 and un == []


class TestTheTwoLeaksTheFirstLiveRunFound:
    """Reconstructed from research_archive/sharp_anchor_2026-08-04.json,
    which bridged 49 Argentine rows out of 33 anchor events. Each of
    these fixtures BRIDGED under the original rule — that is the point of
    reproducing them rather than describing them."""

    # --- leak 1: a city token shared by two different clubs ------------
    #
    # In the archive these two of our rows carry byte-identical anchor
    # legs (0.2582 divergence each), because 'cordoba' matched both.
    COLLIDING = [_row("Union Santa Fe", "Instituto Cordoba",
                      kickoff="2026-09-06T20:00:00+00:00"),
                 _row("Union Santa Fe", "Central Cordoba de Santiago",
                      kickoff="2026-08-11T22:00:00+00:00")]

    def test_the_token_rule_alone_still_matches_both_rows(self):
        """The guard being tested is NOT the token rule — this asserts the
        leak is still present in it, so the fix below is doing the work."""
        ev = _event("Union Santa Fe", "Central Cordoba de Santiago",
                    commence="2026-08-11T22:00:00Z")
        toks = sa._club_toks
        for row in self.COLLIDING:
            ht = toks((row["home"] or {})["name"])
            at = toks((row["away"] or {})["name"])
            eh, ea = toks(ev["home_team"]), toks(ev["away_team"])
            assert (ht & eh) and (at & ea), "the name rule still collides"

    def test_the_wrong_club_is_refused_by_the_window(self):
        """Instituto Cordoba's meeting is in September; the anchor lists
        August's. Same tokens, different match."""
        ev = _event("Union Santa Fe", "Central Cordoba de Santiago",
                    commence="2026-08-11T22:00:00Z")
        pairs, un = sa.bridge([self.COLLIDING[0]], [ev])
        assert pairs == []
        assert "different meeting" in un[0]["reason"]
        assert "36h" in un[0]["reason"]

    def test_a_collision_inside_the_window_refuses_BOTH_rows(self):
        """The window cannot catch a collision between clubs playing the
        same night. Reverse uniqueness is what closes it, and it must
        refuse both rather than keep the first."""
        near = [_row("Union Santa Fe", "Instituto Cordoba",
                     kickoff="2026-08-11T20:00:00+00:00"),
                _row("Union Santa Fe", "Central Cordoba de Santiago",
                     kickoff="2026-08-11T22:00:00+00:00")]
        ev = _event("Union Santa Fe", "Central Cordoba de Santiago",
                    commence="2026-08-11T22:00:00Z")
        pairs, un = sa.bridge(near, [ev])
        assert pairs == [], "neither may be attached"
        assert len(un) == 2
        for u in un:
            assert "sole in-window match for 2 of our fixtures" in u["reason"]
            assert "all are refused rather than one attached" in u["reason"]
            assert "Union Santa Fe" in u["anchor_event"]

    # --- leak 2: the same pairing, a different round -------------------

    def test_the_reverse_fixture_no_longer_attaches_to_this_weeks_book(self):
        """A round-robin league plays every pairing twice. Our list
        carries the season; the anchor carries this week."""
        october = _row("Boca Juniors", "Velez Sarsfield",
                       kickoff="2026-10-28T22:00:00+00:00")
        ev = _event("Velez Sarsfield", "Boca Juniors",
                    commence="2026-08-08T22:00:00Z")
        pairs, un = sa.bridge([october], [ev])
        assert pairs == []
        assert "same pairing, a different meeting" in un[0]["reason"]
        assert un[0]["kickoff_utc"] == "2026-10-28T22:00:00+00:00"

    def test_the_right_round_still_bridges(self):
        """Green half: the fix must not simply refuse everything."""
        august = _row("Boca Juniors", "Velez Sarsfield",
                      kickoff="2026-08-08T22:00:00+00:00")
        ev = _event("Velez Sarsfield", "Boca Juniors",
                    commence="2026-08-08T22:00:00Z")
        pairs, un = sa.bridge([august], [ev])
        assert len(pairs) == 1 and un == []

    def test_both_meetings_listed_attach_to_their_own_round(self):
        """The real end state: two rounds, two events, each row on its
        own book — no refusal at all."""
        rows = [_row("Boca Juniors", "Velez Sarsfield",
                     kickoff="2026-08-08T22:00:00+00:00"),
                _row("Velez Sarsfield", "Boca Juniors",
                     kickoff="2026-10-28T22:00:00+00:00")]
        events = [_event("Velez Sarsfield", "Boca Juniors",
                         commence="2026-08-08T22:00:00Z"),
                  _event("Boca Juniors", "Velez Sarsfield",
                         commence="2026-10-28T22:00:00Z")]
        pairs, un = sa.bridge(rows, events)
        assert un == []
        assert len(pairs) == 2
        assert {p[1]["commence_time"] for p in pairs} == {
            "2026-08-08T22:00:00Z", "2026-10-28T22:00:00Z"}


class TestTheWindowItself:
    def test_a_reschedule_inside_the_window_still_bridges(self):
        """A Saturday-night match moved to Sunday afternoon is the same
        match. Refusing it would cost coverage silently."""
        pairs, un = sa.bridge(
            [_row("Columbus Crew", "Atlas",
                  kickoff="2026-08-04T23:45:00+00:00")],
            [_event("Columbus Crew", "Atlas",
                    commence="2026-08-06T00:00:00Z")])       # +24.25h
        assert len(pairs) == 1 and un == []

    def test_just_outside_the_window_is_refused(self):
        pairs, un = sa.bridge(
            [_row("Columbus Crew", "Atlas",
                  kickoff="2026-08-04T23:45:00+00:00")],
            [_event("Columbus Crew", "Atlas",
                    commence="2026-08-06T12:00:00Z")])       # +36.25h
        assert pairs == []
        assert "different meeting" in un[0]["reason"]

    def test_the_window_is_symmetric(self):
        """An anchor event BEFORE our kickoff is the same distance away."""
        pairs, un = sa.bridge(
            [_row("Columbus Crew", "Atlas",
                  kickoff="2026-08-06T12:00:00+00:00")],
            [_event("Columbus Crew", "Atlas",
                    commence="2026-08-04T23:45:00Z")])
        assert pairs == []
        assert "different meeting" in un[0]["reason"]

    def test_a_row_with_no_kickoff_is_refused_and_named(self):
        pairs, un = sa.bridge([_row("Columbus Crew", "Atlas", kickoff=None)],
                              [_event("Columbus Crew", "Atlas")])
        assert pairs == []
        assert "no readable kickoff" in un[0]["reason"]

    def test_an_unparseable_kickoff_is_refused_not_crashed(self):
        pairs, un = sa.bridge([_row("Columbus Crew", "Atlas", kickoff="soon")],
                              [_event("Columbus Crew", "Atlas")])
        assert pairs == [] and "no readable kickoff" in un[0]["reason"]

    def test_an_event_with_no_commence_time_is_refused_and_counted(self):
        ev = _event("Columbus Crew", "Atlas")
        ev.pop("commence_time")
        pairs, un = sa.bridge([_row("Columbus Crew", "Atlas")], [ev])
        assert pairs == []
        assert "1 carried no commence_time" in un[0]["reason"]

    def test_a_naive_provider_timestamp_is_read_as_utc(self):
        """Not a hypothetical: a provider dropping the Z would otherwise
        turn every fixture into an unmatched row overnight."""
        pairs, un = sa.bridge(
            [_row("Columbus Crew", "Atlas")],
            [_event("Columbus Crew", "Atlas",
                    commence="2026-08-04T23:45:00")])
        assert len(pairs) == 1 and un == []

    def test_the_window_is_a_named_constant_the_archive_can_carry(self):
        assert sa.KICKOFF_WINDOW_HOURS == 36
        assert sa.KICKOFF_WINDOW_SECONDS == 36 * 3600


class TestOrientation:
    def test_a_reversed_anchor_is_flipped_onto_our_orientation(self):
        # anchor lists Atlas at home and prices it 2.0 (0.5 devigged);
        # on OUR orientation that 0.5 belongs to the AWAY leg.
        ev = _event("Atlas", "Columbus Crew",
                    [_book("pinnacle", 2.0, 4.0, 4.0,
                           home="Atlas", away="Columbus Crew")])
        out = sa.compare_one("leagues-cup",
                             _row("Columbus Crew", "Atlas"), ev)
        assert out["orientation_flipped"] is True
        assert out["anchor"] == {"home": 0.25, "tie": 0.25, "away": 0.5}
        assert out["delta_ours_minus_anchor"]["home"] == 0.25

    def test_the_same_orientation_is_not_flipped(self):
        out = sa.compare_one("leagues-cup", _row("Columbus Crew", "Atlas"),
                             _event("Columbus Crew", "Atlas"))
        assert out["orientation_flipped"] is False
        assert out["delta_ours_minus_anchor"] == {"home": 0.0, "tie": 0.0,
                                                  "away": 0.0}
        assert out["max_abs_divergence"] == 0.0


class TestWhatCannotBeComparedIsNamed:
    def test_a_fixture_without_our_three_way_is_named_not_dropped(self):
        row = _row("Columbus Crew", "Atlas")
        row["market_vs_read"] = {"available": False}
        out = sa.compare_one("leagues-cup", row,
                             _event("Columbus Crew", "Atlas"))
        assert out["skipped"] == "no_kalshi_three_way"
        assert "not 'the books agree'" in out["means"]
        assert "max_abs_divergence" not in out

    def test_an_event_with_no_priced_book_is_named_not_dropped(self):
        ev = _event("Columbus Crew", "Atlas", books=[])
        out = sa.compare_one("leagues-cup",
                             _row("Columbus Crew", "Atlas"), ev)
        assert out["skipped"] == "no_anchor_three_way"


# --- the summary refuses to conclude from a handful ------------------------

def _scored(n, delta):
    return [{"competition": "leagues-cup",
             "max_abs_divergence": abs(delta),
             "delta_ours_minus_anchor": {"home": delta, "tie": 0.0,
                                         "away": -delta}}
            for _ in range(n)]


class TestSummaryFloor:
    def test_below_the_floor_the_conclusion_is_withheld(self):
        s = sa.summarise(_scored(6, 0.04))
        assert s["conclusion"] == "withheld"
        assert "below the floor" in s["why"]
        assert "mean_abs_divergence" not in s

    def test_above_the_floor_every_mean_carries_a_ci(self):
        s = sa.summarise(_scored(20, 0.04))
        assert s["n_compared"] == 20
        assert len(s["ci95_signed_home_leg"]) == 2
        assert "significant" in s

    def test_a_significant_positive_mean_is_not_reported_as_a_finding(self):
        s = sa.summarise(_scored(20, 0.04))
        assert s["significant"] is True
        assert "yes_ask" in s["significance_caveat"]

    def test_skipped_rows_are_not_counted_as_agreement(self):
        rows = _scored(20, 0.04) + [{"competition": "leagues-cup",
                                     "skipped": "no_anchor_three_way"}]
        assert sa.summarise(rows)["n_compared"] == 20

    def test_the_summary_never_calls_a_divergence_an_edge(self):
        s = sa.summarise(_scored(20, 0.04))
        assert "recommends nothing" in s["what_a_divergence_is"]


class TestAWholeRunEndToEnd:
    """No network. Everything the run touches is stubbed, so what this
    asserts is the DOCUMENT — the thing that lands in research_archive
    and outlives the session that produced it."""

    def _run(self, monkeypatch, tmp_path, extra=()):
        monkeypatch.setenv(sa.KEY_ENV, SECRET)
        seen = []

        def fake_get(path, key, params=None):
            seen.append((path, key))
            if path == "/sports":
                return SPORTS, {"remaining": "480", "used": "20"}, None
            return ([_event("Columbus Crew", "Atlas",
                            [_book("pinnacle", 1.6, 4.2, 5.5)])],
                    {"remaining": "479", "used": "21"}, None)

        monkeypatch.setattr(sa, "_get", fake_get)
        monkeypatch.setattr(sa, "our_slate", lambda c, b, s: {
            "fixtures": [_row("Columbus Crew", "Atlas", 0.62, 0.22, 0.16)]})
        out = tmp_path / "archive.json"
        monkeypatch.setattr(sys, "argv", ["measure_sharp_anchor.py",
                                          "--competitions", "mls",
                                          "--out", str(out), *extra])
        assert sa.main() == 0
        return json.loads(out.read_text()), seen

    def test_the_archive_carries_its_measured_at_date(self, monkeypatch,
                                                      tmp_path):
        doc, _ = self._run(monkeypatch, tmp_path)
        assert doc["measured_at"] and doc["measured_at_utc"]
        assert doc["anchor_provider"] == "the-odds-api.com v4"

    def test_the_comparison_is_ranked_and_carries_its_rule(self, monkeypatch,
                                                           tmp_path):
        doc, _ = self._run(monkeypatch, tmp_path)
        rows = doc["ranked_by_max_abs_divergence"]
        assert len(rows) == 1
        assert rows[0]["anchor_rule"] == "named_sharp:pinnacle"
        assert rows[0]["ours"]["home"] == 0.62
        assert rows[0]["max_abs_divergence"] > 0

    def test_one_fixture_yields_no_conclusion(self, monkeypatch, tmp_path):
        doc, _ = self._run(monkeypatch, tmp_path)
        assert doc["summary"]["conclusion"] == "withheld"

    def test_the_archive_never_contains_the_key(self, monkeypatch, tmp_path):
        doc, seen = self._run(monkeypatch, tmp_path)
        assert SECRET not in json.dumps(doc)
        assert doc["key_source"] == sa.KEY_ENV
        assert seen and all(k == SECRET for _p, k in seen)   # it WAS used

    def test_list_sports_spends_no_odds_quota(self, monkeypatch, tmp_path,
                                              capsys):
        monkeypatch.setenv(sa.KEY_ENV, SECRET)
        seen = []

        def fake_get(path, key, params=None):
            seen.append(path)
            return SPORTS, {"remaining": "480"}, None

        monkeypatch.setattr(sa, "_get", fake_get)
        monkeypatch.setattr(sys, "argv", ["measure_sharp_anchor.py",
                                          "--list-sports"])
        assert sa.main() == 0
        assert seen == ["/sports"]
        doc = json.loads(capsys.readouterr().out)
        assert {s["key"] for s in doc["soccer_sports"]} == {
            s["key"] for s in SPORTS if s["group"] == "Soccer"}

    def test_a_failed_odds_fetch_is_named_not_silently_empty(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv(sa.KEY_ENV, SECRET)
        monkeypatch.setattr(sa, "_get", lambda path, key, params=None: (
            (SPORTS, {}, None) if path == "/sports"
            else (None, {}, "HTTP 429 on /sports/soccer_usa_mls/odds")))
        out = tmp_path / "a.json"
        monkeypatch.setattr(sys, "argv", ["measure_sharp_anchor.py",
                                          "--competitions", "mls",
                                          "--out", str(out)])
        assert sa.main() == 0
        doc = json.loads(out.read_text())
        assert "429" in doc["competitions"]["mls"]["odds_error"]
        assert "not 'the books agree'" in doc["competitions"]["mls"]["means"]
        assert doc["ranked_by_max_abs_divergence"] == []


class TestTheDocumentSaysWhatItIsNot:
    def test_the_module_docstring_disclaims_an_edge(self):
        assert "not an edge" in sa.__doc__
        assert "yes_ask" in sa.__doc__

    @pytest.mark.parametrize("comp", sorted(sa.SPORT_MATCHERS))
    def test_every_matcher_covers_a_competition_we_actually_carry(self, comp):
        from src import competitions
        assert comp == "mls" or comp in competitions.VIEWERS
