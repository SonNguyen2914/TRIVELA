"""Per-team playstyle vectors: the two structural refusals, and the fit.

Two properties in this file are load-bearing and everything else supports
them:

  1. THE CROSS-COMPETITION REFUSAL. A style axis is standardised against its
     own league-season's distribution, so differencing two leagues' axes has
     no referent. Reused from `xg_ratings` rather than reimplemented, and
     tested here so the reuse is proven rather than assumed.

  2. THE LABEL CANNOT BE AN INPUT. Coordinates are the representation; a label
     is a rendering. This is enforced by type, not by comment, and the tests
     enumerate every operation that must raise.

Every refusal test has a matching CONTROL that the same operation inside one
competition (or on the coordinates rather than the label) SUCCEEDS — so the
tests distinguish "refuses the wrong thing" from "refuses everything".
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from src.live import team_style as ts
from src.live.xg_ratings import (CrossCompetitionArithmetic, ScopedStrength,
                                 XgScope)

EPL = XgScope(source="api-football", league_id=39, season=2025)
LALIGA = XgScope(source="api-football", league_id=140, season=2025)
# Same league, different season: a DIFFERENT population (different mean,
# different promoted/relegated clubs), so the refusal must fire here too.
EPL_PRIOR = XgScope(source="api-football", league_id=39, season=2024)
# Same league and season, different PROVIDER — and for style this is not a
# nuance: the two sources carry DIFFERENT SETS of axes.
MLS_SPORTEC = XgScope(source="sportec", league_id=253, season=2026)

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def _obs(team: int, days_ago: int, *, fixture: str | None = None,
         possession=55.0, shots=12, inside=8, xg=1.3,
         drawn=2, own=1, saves=3, name=None) -> dict:
    """One team-fixture row in the shape `fit_style_vectors` consumes."""
    return {
        "provider_fixture_id": fixture or f"f{team}-{days_ago}",
        "side": "home", "provider_team_id": team,
        "team_name": name or f"Team{team}",
        "kickoff_utc": NOW - timedelta(days=days_ago),
        "possession_pct": possession, "shots_total": shots,
        "shots_inside_box": inside, "xg": xg,
        "offsides_drawn": drawn, "offsides_own": own, "gk_saves": saves,
    }


def _league(n_teams: int = 6, n_matches: int = 8, first_team: int = 1,
            **kw) -> list[dict]:
    """A synthetic league-season where each team differs on every axis, so the
    axes have something to separate.

    `first_team` exists so a test can add its OWN team without colliding with
    a generated one — a collision silently pools two teams' fixtures into one
    and quietly ruins the very effect the test is measuring."""
    rows = []
    for t in range(first_team, first_team + n_teams):
        i = t - first_team + 1
        for m in range(n_matches):
            rows.append(_obs(t, m * 7,
                             possession=35.0 + 5.0 * i,
                             shots=10 + i, inside=4 + i,
                             xg=0.6 + 0.15 * i,
                             drawn=i, own=i % 4, saves=8 - i, **kw))
    return rows


def _fit(rows, scope=EPL, **kw):
    return ts.fit_style_vectors(rows, scope, as_of=NOW, **kw)


# =========================================================================
# 1. THE CROSS-COMPETITION REFUSAL
# =========================================================================

@pytest.mark.parametrize("op,symbol", [
    (lambda a, b: a - b, "-"),
    (lambda a, b: a + b, "+"),
    (lambda a, b: a * b, "*"),
    (lambda a, b: a / b, "/"),
])
def test_style_axis_arithmetic_refused_across_competitions(op, symbol):
    """An EPL possession z and a La Liga possession z cannot be combined.

    55% possession is a different fact in each league; the z-scores are against
    different distributions, so the difference has no referent."""
    with pytest.raises(CrossCompetitionArithmetic) as exc:
        op(ScopedStrength(EPL, 1.4), ScopedStrength(LALIGA, 0.9))
    assert "39" in str(exc.value) and "140" in str(exc.value)


def test_style_axis_arithmetic_allowed_within_competition():
    """CONTROL: the same subtraction inside one league-season succeeds. Without
    this the refusal tests would pass against code that refused everything."""
    diff = ScopedStrength(EPL, 1.4) - ScopedStrength(EPL, 0.9)
    assert diff.for_display() == pytest.approx(0.5)
    assert diff.scope == EPL


def test_style_axis_refused_across_seasons_of_the_same_league():
    """Same league, different season is still a different population."""
    with pytest.raises(CrossCompetitionArithmetic):
        ScopedStrength(EPL, 1.0) - ScopedStrength(EPL_PRIOR, 1.0)


def test_style_axis_refused_across_sources():
    """Sportec and API-Football measure different things AND carry different
    axis sets; pooling them would silently change which axes exist."""
    with pytest.raises(CrossCompetitionArithmetic):
        ScopedStrength(MLS_SPORTEC, 1.0) - ScopedStrength(
            XgScope(source="api-football", league_id=253, season=2026), 1.0)


def test_pair_for_style_model_refuses_cross_competition():
    """The named gate any fixture-level use must pass, reachable by a caller
    that never touches an AxisCoordinate directly."""
    home = _fit(_league(), EPL)["vectors"][1]
    away = _fit(_league(), LALIGA)["vectors"][2]
    with pytest.raises(CrossCompetitionArithmetic) as exc:
        ts.pair_for_style_model(home, away)
    assert "39" in str(exc.value) and "140" in str(exc.value)


def test_pair_for_style_model_allows_same_competition():
    """CONTROL: two teams from the same league-season pass the gate."""
    fit = _fit(_league(), EPL)
    h, a = ts.pair_for_style_model(fit["vectors"][1], fit["vectors"][2])
    assert h.provider_team_id == 1 and a.provider_team_id == 2


def test_covariates_refuse_cross_competition():
    """The refusal must fire on the ONE route from vectors to model inputs,
    not merely on the raw operators."""
    home = _fit(_league(), EPL)["vectors"][1]
    away = _fit(_league(), LALIGA)["vectors"][2]
    with pytest.raises(CrossCompetitionArithmetic):
        ts.style_covariates(home, away)


# =========================================================================
# 2. THE LABEL CANNOT BE AN INPUT
# =========================================================================

def test_style_vector_has_no_label_field():
    """Structural: there is nothing to read.

    If a label were stored on the vector it would be one attribute access away
    from becoming a model input, and the next author would take it. This test
    fails the moment somebody adds one."""
    names = {f.name for f in dataclasses.fields(ts.StyleVector)}
    for banned in ("label", "style_label", "display_label", "style",
                   "style_name", "category", "bucket"):
        assert banned not in names, (
            f"StyleVector gained a {banned!r} field — coordinates are the "
            "representation; a label must be rendered, never stored")


@pytest.mark.parametrize("op,name", [
    (lambda l: l == "possession-dominant", "=="),
    (lambda l: l != "x", "!="),
    (lambda l: hash(l), "hash"),
    (lambda l: bool(l), "bool"),
    (lambda l: l < "x", "<"),
    (lambda l: l > "x", ">"),
    (lambda l: l <= "x", "<="),
    (lambda l: l >= "x", ">="),
    (lambda l: l + "x", "+"),
    (lambda l: "x" + l, "radd"),
    (lambda l: l * 2, "*"),
    (lambda l: 2 * l, "rmul"),
    (lambda l: l - "x", "-"),
    (lambda l: float(l), "float"),
    (lambda l: int(l), "int"),
    (lambda l: len(l), "len"),
    (lambda l: "x" in l, "contains"),
    (lambda l: list(l), "iter"),
    (lambda l: {l: 1}, "dict key"),
    (lambda l: {l}, "set member"),
])
def test_label_refuses_every_route_to_becoming_an_input(op, name):
    """Enumerated, because each dunder is a separate route.

    Comparison and hashing are how a label becomes a CATEGORY (a dict key, a
    group-by, a one-hot column). Truthiness is how it silently gates
    behaviour. Arithmetic and coercion are how it becomes a number. All of
    them raise."""
    label = ts.DisplayLabel("dominates the ball", ("dominates the ball",))
    with pytest.raises(ts.LabelIsNotAnInput):
        op(label)


def test_label_display_exits_work():
    """CONTROL: the label is still usable for the one thing it exists for.

    Without this the refusal tests would pass against a label that was simply
    broken. `str()` and `for_display()` are the single greppable seam."""
    label = ts.DisplayLabel("dominates the ball", ("dominates the ball",))
    assert str(label) == "dominates the ball"
    assert label.for_display() == "dominates the ball"
    assert f"{label}" == "dominates the ball"
    assert "dominates" in repr(label)


def test_render_label_is_derived_from_coordinates():
    """The label is a function of the vector, produced on demand."""
    fit = _fit(_league())
    top = fit["vectors"][6]        # highest on possession, xG/shot, line height
    bottom = fit["vectors"][1]
    assert isinstance(ts.render_label(top), ts.DisplayLabel)
    assert str(ts.render_label(top)) != str(ts.render_label(bottom))


def test_render_label_never_invents_an_axis_the_team_lacks():
    """A team whose source publishes two axes gets a two-axis description, not
    a five-axis one with three silent zeros."""
    rows = [{**r, "possession_pct": None, "offsides_drawn": None,
             "gk_saves": None} for r in _league()]
    fit = _fit(rows)
    for v in fit["vectors"].values():
        parts = ts.render_label(v).parts
        for p in parts:
            assert p not in (ts.AXES["ball_dominance"]["high"],
                             ts.AXES["ball_dominance"]["low"],
                             ts.AXES["defensive_load"]["high"],
                             ts.AXES["defensive_load"]["low"])


def test_covariates_refuse_a_label_by_type():
    """Handing the renderer's output to the model-input builder raises."""
    fit = _fit(_league())
    label = ts.render_label(fit["vectors"][1])
    with pytest.raises(ts.LabelIsNotAnInput):
        ts.style_covariates(label, fit["vectors"][2])
    with pytest.raises(ts.LabelIsNotAnInput):
        ts.style_covariates(fit["vectors"][1], label)


def test_covariates_refuse_an_unregistered_axis():
    """A post-hoc axis cannot arrive as a string at the call site. A new axis
    is a new hypothesis owed its own pre-registration (S-10's falsifier)."""
    fit = _fit(_league())
    with pytest.raises(KeyError) as exc:
        ts.style_covariates(fit["vectors"][1], fit["vectors"][2],
                            axes=("ball_dominance", "vibes"))
    assert "vibes" in str(exc.value)


def test_covariates_succeed_on_coordinates():
    """CONTROL: the sanctioned route works, and produces one difference per
    axis both teams are placed on."""
    fit = _fit(_league())
    out = ts.style_covariates(fit["vectors"][1], fit["vectors"][6])
    assert out["n_axes"] == len(ts.ALL_AXES)
    assert out["covariates"]["style_diff_ball_dominance"] < 0   # 1 < 6
    assert out["competition_key"] == EPL.key
    assert out["absent"] == {}


def test_covariates_omit_an_absent_axis_rather_than_zeroing_it():
    """A 0.0 difference would assert the two teams were identical on the axis.
    Absence is reported by name instead."""
    rows = _league()
    rows = [r if r["provider_team_id"] != 1
            else {**r, "possession_pct": None} for r in rows]
    fit = _fit(rows)
    out = ts.style_covariates(fit["vectors"][1], fit["vectors"][6])
    assert "style_diff_ball_dominance" not in out["covariates"]
    assert "ball_dominance" in out["absent"]


def test_as_dict_label_is_flagged_display_only():
    """The serialized payload carries the label through the single sanctioned
    exit and says, explicitly, that it is not an input."""
    fit = _fit(_league())
    d = fit["vectors"][1].as_dict()
    assert isinstance(d["display_label"], str)
    assert d["label_is_display_only"] is True
    assert d["comparable_across_competitions"] is False


# =========================================================================
# 3. ABSENT IS NEVER ZERO
# =========================================================================

@pytest.mark.parametrize("raw", [None, "", "-", "–", "null", "none", "N/A",
                                 "abc", True, False, "101%", "-1%"])
def test_parse_percent_absence(raw):
    assert ts.parse_percent(raw) is None


@pytest.mark.parametrize("raw,want", [("27%", 27.0), ("0%", 0.0),
                                      ("100%", 100.0), (55, 55.0),
                                      ("48.5%", 48.5)])
def test_parse_percent_values(raw, want):
    """CONTROL: real values parse, INCLUDING 0%. Unlike xG, the provider does
    not use 0 as a possession placeholder, and it is physically meaningful."""
    assert ts.parse_percent(raw) == pytest.approx(want)


@pytest.mark.parametrize("raw", [None, "", "-", "null", "abc", True, -1,
                                 1.5, "2.5"])
def test_parse_count_absence(raw):
    assert ts.parse_count(raw) is None


@pytest.mark.parametrize("raw,want", [(0, 0), ("0", 0), (4, 4), ("12", 12)])
def test_parse_count_keeps_a_genuine_zero(raw, want):
    """A team really can draw no offsides and make no saves, and those are the
    informative tails of three axes. Treating 0 as absent would discard exactly
    the teams the axis is trying to find — the OPPOSITE of parse_xg's rule, and
    deliberately so."""
    assert ts.parse_count(raw) == want


def test_absent_axis_contributes_nothing_not_zero():
    """A fixture missing possession must not drag the team's mean toward 0."""
    rows = [_obs(1, d, possession=60.0) for d in (0, 7, 14, 21, 28)]
    rows += [_obs(1, 35, possession=None)]
    rows += [_obs(2, d, possession=40.0) for d in range(0, 35, 7)]
    fit = _fit(rows)
    c = fit["vectors"][1].axes["ball_dominance"]
    assert c.n_obs == 5, "the absent fixture must not count toward n_obs"
    assert c.raw == pytest.approx(60.0), (
        "the absent fixture must not pull the mean below 60")


def test_axis_with_no_observations_is_refused_by_name():
    rows = [{**r, "gk_saves": None} for r in _league()]
    fit = _fit(rows)
    v = fit["vectors"][1]
    assert "defensive_load" not in v.axes
    assert v.refused["defensive_load"] == ts.AXIS_NO_OBSERVATIONS
    assert ts.REFUSAL_WORDS[ts.AXIS_NO_OBSERVATIONS]


def test_zero_shots_does_not_produce_a_zero_rate():
    """'inside-box share of no shots' is undefined, not 0."""
    assert ts.axis_terms({"shots_inside_box": 0, "shots_total": 0},
                         "shot_location") is None
    assert ts.axis_terms({"xg": None, "shots_total": 10},
                         "chance_quality") is None
    # CONTROL: a real pair yields terms
    assert ts.axis_terms({"shots_inside_box": 6, "shots_total": 10},
                         "shot_location") == (6.0, 10.0)


def test_below_the_fixture_floor_is_refused_per_axis():
    """The floor is per axis, not per team: coverage of two statistics is not
    the same, so a team can be placed on one and refused on another."""
    rows = _league(n_teams=5, n_matches=8, first_team=10)  # a real league
    rows += [_obs(2, d, possession=48.0) for d in (0, 7)]  # 2 — below floor
    fit = _fit(rows, min_fixtures=5)
    assert fit["vectors"][10].axes, "a team above the floor must be placed"
    assert fit["vectors"][2].axes == {}
    assert set(fit["vectors"][2].refused.values()) == {
        ts.AXIS_BELOW_SAMPLE_FLOOR}


def test_a_thin_team_does_not_shift_the_other_teams_coordinates():
    """A below-floor team is excluded from the standardisation population too,
    not merely from the output — otherwise two fixtures of an extreme outlier
    would move every other team's z."""
    rows = _league(n_teams=5, n_matches=8, first_team=10)
    without = _fit(rows, min_fixtures=5)
    with_thin = _fit(rows + [_obs(2, d, possession=99.0) for d in (0, 7)],
                     min_fixtures=5)
    for tid in without["vectors"]:
        a = without["vectors"][tid].axes["ball_dominance"].z.for_display()
        b = with_thin["vectors"][tid].axes["ball_dominance"].z.for_display()
        assert a == pytest.approx(b)


# =========================================================================
# 4. offsides DRAWN is the OPPONENT's count
# =========================================================================

def test_pair_offsides_drawn_takes_the_opponent_count():
    """A high line forces the OPPONENT offside, so the count belongs to the
    team that forced it. Attributing a team its own offsides here would invert
    the axis — a deep-defending team that runs a lot would read as high-line."""
    teams = [{"provider_team_id": 1, "offsides_own": 4},
             {"provider_team_id": 2, "offsides_own": 1}]
    a, b = ts.pair_offsides_drawn(teams)
    assert a["offsides_drawn"] == 1 and a["offsides_own"] == 4
    assert b["offsides_drawn"] == 4 and b["offsides_own"] == 1


def test_pair_offsides_drawn_refuses_a_one_sided_response():
    """Inventing a 0 for the missing side would fabricate a deep defensive
    line for whichever team the provider happened to drop."""
    only = ts.pair_offsides_drawn([{"provider_team_id": 1, "offsides_own": 4}])
    assert only[0]["offsides_drawn"] is None


def test_own_and_drawn_offsides_are_different_axes():
    """The sixth candidate must not be the fourth axis wearing a new name."""
    assert (ts.AXES["defensive_line_height"]["field"] == "offsides_drawn")
    assert (ts.CANDIDATE_AXES["verticality"]["field"] == "offsides_own")
    assert "verticality" not in ts.AXES


def test_read_fixture_style_parses_the_measured_type_names():
    """The provider type strings were MEASURED against the live feed on
    2026-07-29, not guessed. This pins them."""
    payload = {"response": [
        {"team": {"id": 65, "name": "Nottingham Forest"},
         "statistics": [{"type": "Shots on Goal", "value": 5},
                        {"type": "Total Shots", "value": 10},
                        {"type": "Shots insidebox", "value": 6},
                        {"type": "Offsides", "value": 4},
                        {"type": "Ball Possession", "value": "27%"},
                        {"type": "Goalkeeper Saves", "value": 1},
                        {"type": "expected_goals", "value": "0.89"}]},
        {"team": {"id": 42, "name": "Arsenal"},
         "statistics": [{"type": "Total Shots", "value": 20},
                        {"type": "Shots insidebox", "value": 16},
                        {"type": "Offsides", "value": None},
                        {"type": "Ball Possession", "value": "73%"},
                        {"type": "Goalkeeper Saves", "value": 4},
                        {"type": "expected_goals", "value": "2.44"}]}]}
    teams = ts.pair_offsides_drawn(ts.read_fixture_style(payload))
    forest, arsenal = teams
    assert forest["possession_pct"] == pytest.approx(27.0)
    assert forest["shots_total"] == 10 and forest["shots_inside_box"] == 6
    assert forest["xg"] == pytest.approx(0.89)
    assert forest["offsides_own"] == 4 and forest["gk_saves"] == 1
    # Arsenal's offsides is NULL -> absent, so Forest draws an unknown number
    assert arsenal["offsides_own"] is None
    assert forest["offsides_drawn"] is None
    assert arsenal["offsides_drawn"] == 4


# =========================================================================
# 5. THE FIT: recency, shrinkage, within-league standardisation
# =========================================================================

def test_recency_weighting_favours_recent_fixtures():
    """A team that recently changed style must read closer to the new one."""
    old = [_obs(1, d, possession=30.0) for d in range(200, 400, 20)]
    new = [_obs(1, d, possession=70.0) for d in range(0, 40, 7)]
    # first_team=10 so the generated league cannot collide with team 1
    fit = _fit(old + new + _league(n_teams=4, first_team=10))
    raw = fit["vectors"][1].axes["ball_dominance"].raw
    assert raw > 60.0, f"recency weighting did not bite: {raw}"
    # CONTROL: with recency OFF (an enormous half-life) the same rows average
    # toward the unweighted mean, so the assertion above is measuring recency
    # and not merely the fact that there are more recent rows.
    flat = ts.fit_style_vectors(old + new + _league(n_teams=4, first_team=10),
                                EPL, as_of=NOW, half_life=10_000_000.0)
    assert flat["vectors"][1].axes["ball_dominance"].raw < raw


def test_shrinkage_pulls_a_thin_team_toward_the_league_mean():
    rows = _league(n_teams=6, n_matches=8)
    thin = [_obs(9, d, possession=95.0, name="Thin") for d in range(0, 40, 7)]
    fit = _fit(rows + thin, shrink_games=6.0)
    c = fit["vectors"][9].axes["ball_dominance"]
    assert c.raw == pytest.approx(95.0)
    assert c.shrunk < c.raw, "shrinkage must pull toward the league mean"
    assert c.shrunk > c.league_mean


def test_standardisation_is_within_league_season():
    """z is against THIS league's own distribution, so the same raw value in
    two leagues yields two different z's. That is the whole reason the
    cross-competition refusal exists."""
    strong = _fit(_league(n_teams=6), EPL)
    # a league whose teams all sit higher: the same absolute possession is
    # league-typical here and league-leading there
    shifted = [{**r, "possession_pct": r["possession_pct"] + 20.0}
               for r in _league(n_teams=6)]
    other = _fit(shifted, LALIGA)
    z_epl = strong["vectors"][6].axes["ball_dominance"].z.for_display()
    z_other = other["vectors"][1].axes["ball_dominance"].z.for_display()
    assert z_epl > 0 > z_other
    assert strong["vectors"][6].axes["ball_dominance"].scope == EPL
    assert other["vectors"][1].axes["ball_dominance"].scope == LALIGA


def test_raw_is_stored_beside_z_so_a_reader_can_restandardise():
    fit = _fit(_league())
    c = fit["vectors"][3].axes["ball_dominance"]
    assert c.raw > 0 and c.league_sd > 0
    # z is recoverable from the stored moments
    assert c.z.for_display() == pytest.approx(
        (c.shrunk - _mean_shrunk(fit, "ball_dominance")) / c.league_sd)


def _mean_shrunk(fit, axis) -> float:
    vals = [v.axes[axis].shrunk for v in fit["vectors"].values()
            if axis in v.axes]
    return sum(vals) / len(vals)


def test_a_flat_axis_is_refused_not_divided_by_zero():
    """Where every team scores identically the axis carries no information.
    Dividing by a zero sd would produce infinities; naming the refusal makes
    the flat league visible."""
    rows = []
    for t in range(1, 6):
        for m in range(8):
            rows.append(_obs(t, m * 7, saves=3))     # identical for everyone
    fit = _fit(rows)
    for v in fit["vectors"].values():
        assert "defensive_load" not in v.axes
        assert v.refused["defensive_load"] == ts.AXIS_DOES_NOT_SEPARATE


def test_rate_axis_is_a_ratio_of_sums_not_a_mean_of_ratios():
    """A 1-shot match must not count as much as a 25-shot one."""
    rows = [_obs(1, 0, shots=1, inside=1), _obs(1, 7, shots=1, inside=1),
            _obs(1, 14, shots=1, inside=1), _obs(1, 21, shots=1, inside=1),
            _obs(1, 28, shots=1, inside=1),
            _obs(1, 35, shots=40, inside=0)]
    rows += _league(n_teams=4)
    fit = _fit(rows)
    raw = fit["vectors"][1].axes["shot_location"].raw
    assert raw < 0.4, ("a mean of per-fixture ratios would be ~0.83; a ratio "
                       f"of weighted sums must be far lower, got {raw}")


def test_fit_is_pure_and_deterministic():
    rows = _league()
    a, b = _fit(rows), _fit(rows)
    for tid, va in a["vectors"].items():
        for axis, ca in va.axes.items():
            assert ca.z.for_display() == pytest.approx(
                b["vectors"][tid].axes[axis].z.for_display())


def test_empty_input_reports_not_ingested():
    fit = _fit([])
    assert fit["vectors"] == {}
    assert fit["reason"] == ts.NOT_INGESTED


# =========================================================================
# 6. SOURCE AXIS AVAILABILITY — 'not published' != 'measured flat'
# =========================================================================

def test_sportec_cannot_carry_three_of_the_five_axes():
    """MLS's provider publishes no possession, no offsides and no goalkeeper
    saves. An MLS style vector is structurally a 2-axis vector, and reporting
    it as five with three zeros would be missing evidence turned into data."""
    sportec = set(ts.SOURCE_AXES["sportec"])
    for axis in ("ball_dominance", "defensive_line_height", "defensive_load",
                 "verticality"):
        assert axis not in sportec
    assert {"shot_location", "chance_quality"} <= sportec


def test_axis_not_in_source_is_a_distinct_refusal():
    rows = [{"provider_fixture_id": f"m{m}", "side": "home",
             "provider_team_id": t, "team_name": f"T{t}",
             "kickoff_utc": NOW - timedelta(days=m * 7),
             "shots_total": 10 + t, "shots_inside_box": 4 + t,
             "xg": 0.8 + 0.1 * t,
             # the three Sportec does not publish
             "possession_pct": None, "offsides_drawn": None,
             "offsides_own": None, "gk_saves": None}
            for t in range(1, 6) for m in range(8)]
    fit = ts.fit_style_vectors(rows, MLS_SPORTEC, as_of=NOW)
    v = fit["vectors"][1]
    assert set(v.axes) == {"shot_location", "chance_quality"}
    assert v.refused["ball_dominance"] == ts.AXIS_NOT_IN_SOURCE
    assert v.refused["defensive_load"] == ts.AXIS_NOT_IN_SOURCE
    assert sorted(fit["axes_not_in_source"]) == sorted(
        ["ball_dominance", "defensive_line_height", "defensive_load",
         "verticality"])


def test_every_registered_axis_is_claimed_by_at_least_one_source():
    for axis in ts.ALL_AXES:
        assert any(axis in v for v in ts.SOURCE_AXES.values()), axis


def test_source_axes_names_only_registered_axes():
    for src, axes in ts.SOURCE_AXES.items():
        for axis in axes:
            assert axis in ts.ALL_AXES, f"{src} claims unknown axis {axis}"


# =========================================================================
# 7. SEPARATION AND CORRELATION REPORTS
# =========================================================================

def test_separation_report_shows_spread_and_sd_per_axis():
    rep = ts.separation_report(_fit(_league()))
    r = rep["axes"]["ball_dominance"]
    assert r["n_teams"] == 6 and r["separates"] is True
    assert r["raw_spread"] == pytest.approx(25.0, abs=1e-6)
    assert r["raw_sd"] > 0 and r["shrunk_sd"] > 0
    assert r["min_team"] == "Team1" and r["max_team"] == "Team6"


def test_separation_report_marks_a_flat_axis_as_not_separating():
    rows = []
    for t in range(1, 6):
        for m in range(8):
            rows.append(_obs(t, m * 7, saves=3))
    rep = ts.separation_report(_fit(rows))
    assert rep["axes"]["defensive_load"]["separates"] is False
    assert rep["axes"]["defensive_load"]["refused_reason"] == (
        ts.AXIS_DOES_NOT_SEPARATE)


def test_separation_report_marks_the_candidate_axis():
    rep = ts.separation_report(_fit(_league()))
    assert rep["axes"]["verticality"]["candidate"] is True
    assert rep["axes"]["ball_dominance"].get("candidate") is False


def test_correlation_report_reports_n_behind_every_coefficient():
    rep = ts.correlation_report(_fit(_league()))
    assert rep["threshold_abs_r"] == ts.COLLINEAR_ABS_R
    for p in rep["pairs"]:
        assert p["n_teams"] >= 0
        assert p["r"] is None or -1.0 <= p["r"] <= 1.0


def test_correlation_report_flags_a_collinear_pair():
    """In the synthetic league every axis is a linear function of t, so several
    pairs are perfectly correlated and MUST be flagged. This proves the flag
    fires, which a real league may or may not trigger."""
    rep = ts.correlation_report(_fit(_league()))
    assert rep["flagged"], "perfectly collinear axes were not flagged"
    for f in rep["flagged"]:
        assert abs(f["r"]) >= ts.COLLINEAR_ABS_R
        assert f["flag"] == "may_be_one_axis_in_this_league"


def test_correlation_undefined_is_not_reported_as_zero():
    """A constant series has no correlation. Returning 0.0 would read as
    'measured independent', which is a different and false claim."""
    assert ts._pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert ts._pearson([1.0, 2.0], [1.0, 2.0]) is None
    assert ts._pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)


def test_correlation_is_pairwise_complete():
    """A team missing one axis is dropped from that PAIR only, not from all."""
    rows = _league()
    rows = [r if r["provider_team_id"] != 1 else {**r, "gk_saves": None}
            for r in rows]
    rep = ts.correlation_report(_fit(rows))
    by = {(p["a"], p["b"]): p for p in rep["pairs"]}
    assert by[("ball_dominance", "defensive_load")]["n_teams"] == 5
    assert by[("ball_dominance", "shot_location")]["n_teams"] == 6


# =========================================================================
# 8. THE APPEND-ONLY DISCRIMINATOR
# =========================================================================

def test_value_hash_changes_when_any_raw_value_changes():
    base = {"raw": {"possession_pct": "55%", "shots_total": "12",
                    "shots_inside_box": "8", "xg": "1.30",
                    "offsides_own": "1", "gk_saves": "3"},
            "offsides_drawn": 2}
    h0 = ts.style_value_hash(base)
    assert ts.style_value_hash(dict(base)) == h0          # idempotent
    for key in base["raw"]:
        moved = {"raw": {**base["raw"], key: "99"},
                 "offsides_drawn": base["offsides_drawn"]}
        assert ts.style_value_hash(moved) != h0, key
    assert ts.style_value_hash({**base, "offsides_drawn": 3}) != h0


def test_value_hash_sees_a_formatting_only_change():
    """A provider that changes '27%' to '27.0%' changed what it told us, even
    though the parse is identical. Hashing the raw strings catches it."""
    a = {"raw": {"possession_pct": "27%"}, "offsides_drawn": None}
    b = {"raw": {"possession_pct": "27.0%"}, "offsides_drawn": None}
    assert ts.style_value_hash(a) != ts.style_value_hash(b)
    assert ts.parse_percent("27%") == ts.parse_percent("27.0%")


# =========================================================================
# 9. NOTHING PRICES OFF STYLE
# =========================================================================

def test_module_publishes_no_probability():
    """S-10 is unfitted by design. Nothing here may produce a forecast."""
    for name in dir(ts):
        assert not any(w in name.lower()
                       for w in ("predict", "probability", "forecast",
                                 "price", "odds")), name


def test_mls_style_from_apifootball_is_refused_by_name():
    with pytest.raises(Exception) as exc:
        ts._refuse_mls_from_apifootball(253, "api-football")
    assert "253" in str(exc.value)
    # CONTROL: another league is not refused, and neither is Sportec MLS
    assert ts._refuse_mls_from_apifootball(39, "api-football") is None
    assert ts._refuse_mls_from_apifootball(253, "sportec") is None


def test_summary_states_the_label_policy_and_parameter_provenance():
    rows = ts.summary()
    assert "display" in rows["label_policy"].lower()
    assert "NOT swept" in rows["parameters"]["provenance"]
    assert rows["parameters"]["half_life_days"] > 0
    assert set(rows["axes"]) == set(ts.AXES)


# =========================================================================
# 10. THE ENGINE IS UNTOUCHED
# =========================================================================

# sha256 of every module inside the MLS engine signature, captured from base
# commit 3bd31df BEFORE any of this branch's work. The style build is purely
# additive — a new table, a new module, a new script — and must not perturb the
# engine, because a source change there invalidates the live MLS approval and
# halts shadow evidence collection until an operator reactivates it
# (AGENTS.md §4).
#
# This is a GUARD, not a snapshot to refresh when it goes red. A failure means
# an engine module changed; the correct response is to find out why and decide
# deliberately, never to paste in the new hash.
ENGINE_SOURCE_SHA256 = {
    "src.live.model_mls":
        "bbb0eaff0dcd13066ff425372e2920494f39b60408190d4542ecb3b6d2344c08",
    "src.models.simulator":
        "fe1b02445648cafd1dcc6f62029b38ca960eb1dadfe1cdfa877f2f13a4e6542e",
    "src.models.xg_model":
        "61f8753d1e7f09009c1b3bcd7dc3400f5b47e2c66a8424f3e33b8d60d33bfeea",
    "src.models.features":
        "65ecdd7892205590a64a034ed2628d84463bb504e4a90fe999c4460fe4b8bfc1",
}


def test_engine_signature_modules_are_byte_identical_to_base():
    """Style work touches no module inside the MLS engine signature."""
    from src.live import model_mls
    got = model_mls.engine_signature()["source_sha256"]
    assert got == ENGINE_SOURCE_SHA256, (
        "an MLS engine-signature module changed on this branch. That "
        "invalidates the live approval and halts shadow collection until an "
        "operator reactivates it — do not update these literals to make the "
        "test pass.")


def test_team_style_is_not_imported_by_the_engine():
    """The engine must not gain a dependency on style, or style would end up
    inside the signature and every axis tweak would invalidate approval."""
    import inspect

    from src.live import model_mls
    src = inspect.getsource(model_mls)
    assert "team_style" not in src
