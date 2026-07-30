"""League-derived xG ratings: the cross-competition refusal, the fit, and the
provider parsing that decides whether data exists at all.

The refusal tests are the point of this file. Each one asserts that a specific
arithmetic or ordering operation between two competitions RAISES — and there is
a matching control that the same operation inside one competition succeeds, so
the tests distinguish "refuses across leagues" from "refuses everything".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.live import apifootball
from src.live.xg_ratings import (CrossCompetitionArithmetic,
                                 CrossCompetitionComparison, LeagueXgRating,
                                 ScopedStrength, XgScope, fit_league_ratings,
                                 fixture_ratings, pair_for_pricing)

BUNDESLIGA = XgScope(source="api-football", league_id=78, season=2025)
J1 = XgScope(source="api-football", league_id=98, season=2026)
# Same league, different season: a DIFFERENT population, so the refusal must
# fire here too. A rating is relative to one season's mean.
BUNDESLIGA_PRIOR = XgScope(source="api-football", league_id=78, season=2024)
# Same league and season, different PROVIDER: pooling Sportec and API-Football
# numbers would silently mix two measurement systems.
MLS_SPORTEC = XgScope(source="sportec", league_id=253, season=2026)
MLS_APIF = XgScope(source="api-football", league_id=253, season=2026)


def _rating(scope: XgScope, atk: float = 1.2, dfc: float = 0.9,
            n: int = 20, name: str = "Club") -> LeagueXgRating:
    return LeagueXgRating(
        scope=scope, provider_team_id=1, team_name=name,
        attack=ScopedStrength(scope, atk), defence=ScopedStrength(scope, dfc),
        n_fixtures=n, window_start=None, window_end=None,
        league_mean_xg=1.4, shrink_games=6.0, half_life_days=90.0)


# --- THE CENTRAL REFUSAL --------------------------------------------------

@pytest.mark.parametrize("op,symbol", [
    (lambda a, b: a - b, "-"),
    (lambda a, b: a + b, "+"),
    (lambda a, b: a * b, "*"),
    (lambda a, b: a / b, "/"),
])
def test_cross_competition_arithmetic_refused(op, symbol):
    """A J1 strength and a Bundesliga strength cannot be combined at all."""
    dortmund = ScopedStrength(BUNDESLIGA, 1.31)
    cerezo = ScopedStrength(J1, 1.08)
    with pytest.raises(CrossCompetitionArithmetic) as exc:
        op(dortmund, cerezo)
    # the message must name both scopes: a refusal nobody can diagnose gets
    # worked around rather than understood
    assert "78" in str(exc.value) and "98" in str(exc.value)


@pytest.mark.parametrize("op", [
    lambda a, b: a < b,
    lambda a, b: a > b,
    lambda a, b: a <= b,
    lambda a, b: a >= b,
])
def test_cross_competition_ordering_refused(op):
    """'Dortmund is stronger than Cerezo' is the same unfounded claim as their
    difference, and spelling it with `>` does not make it sound."""
    with pytest.raises(CrossCompetitionComparison):
        op(ScopedStrength(BUNDESLIGA, 1.31), ScopedStrength(J1, 1.08))


def test_cross_season_arithmetic_refused():
    """Same league, different season is still a different population."""
    with pytest.raises(CrossCompetitionArithmetic):
        ScopedStrength(BUNDESLIGA, 1.2) - ScopedStrength(BUNDESLIGA_PRIOR, 1.1)


def test_cross_source_arithmetic_refused():
    """Same league-season, different provider: Sportec and API-Football xG are
    two measurement systems and must never be pooled silently."""
    with pytest.raises(CrossCompetitionArithmetic):
        ScopedStrength(MLS_SPORTEC, 1.2) - ScopedStrength(MLS_APIF, 1.2)


def test_pair_for_pricing_refuses_cross_competition():
    """The named gate anything fixture-shaped must pass."""
    with pytest.raises(CrossCompetitionArithmetic) as exc:
        pair_for_pricing(_rating(BUNDESLIGA), _rating(J1))
    assert "fabricated" in str(exc.value)


def test_scoped_strength_refuses_bare_float():
    """`rating.attack - 1.0` would be a scope-free number pretending to be a
    scoped one. TypeError, not a silent unwrap."""
    with pytest.raises(TypeError):
        ScopedStrength(BUNDESLIGA, 1.2) - 1.0
    with pytest.raises(TypeError):
        ScopedStrength(BUNDESLIGA, 1.2) + 0.5


# --- CONTROLS: the same operations WITHIN one competition succeed ----------
# Labelled controls, not evidence. They exist so the refusal tests above
# cannot pass by refusing everything.

def test_control_same_competition_arithmetic_allowed():
    a = ScopedStrength(BUNDESLIGA, 1.30)
    b = ScopedStrength(BUNDESLIGA, 1.10)
    assert (a - b).value == pytest.approx(0.20)
    assert (a + b).value == pytest.approx(2.40)
    assert (a * b).value == pytest.approx(1.43)
    assert (a / b).value == pytest.approx(1.181818, rel=1e-4)
    assert (a - b).scope == BUNDESLIGA


def test_control_same_competition_ordering_allowed():
    assert ScopedStrength(BUNDESLIGA, 1.3) > ScopedStrength(BUNDESLIGA, 1.1)
    assert ScopedStrength(BUNDESLIGA, 1.1) < ScopedStrength(BUNDESLIGA, 1.3)
    assert ScopedStrength(BUNDESLIGA, 1.1) <= ScopedStrength(BUNDESLIGA, 1.1)


def test_control_pair_for_pricing_allows_same_competition():
    h, a = pair_for_pricing(_rating(BUNDESLIGA), _rating(BUNDESLIGA))
    assert h.scope == a.scope == BUNDESLIGA


def test_for_display_is_the_only_unwrap():
    """A number has to reach a template somehow. `for_display` is that seam,
    named so a reviewer can grep every place a scoped value becomes a float —
    and there is no __float__ to make it happen implicitly."""
    s = ScopedStrength(BUNDESLIGA, 1.234)
    assert s.for_display() == 1.234
    with pytest.raises(TypeError):
        float(s)


def test_as_dict_declares_incomparability():
    d = _rating(BUNDESLIGA).as_dict()
    assert d["comparable_across_competitions"] is False
    assert d["competition_key"] == "api-football:78:2025"
    assert d["source"] == "api-football"


# --- the fit --------------------------------------------------------------

def _rows(n_per_team: int = 6, teams=(10, 20), xg=None, base=None):
    """Synthetic league rows: two teams meeting repeatedly."""
    base = base or datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = []
    fid = 1000
    for i in range(n_per_team):
        ko = base - timedelta(days=7 * i)
        a, b = teams
        av, bv = (xg or (2.0, 1.0))
        rows.append({"provider_fixture_id": fid, "side": "home",
                     "provider_team_id": a, "team_name": f"T{a}",
                     "xg": av, "xg_against": bv, "goals": 2,
                     "goals_conceded": 1, "kickoff_utc": ko,
                     "round_label": f"Regular Season - {i}"})
        rows.append({"provider_fixture_id": fid, "side": "away",
                     "provider_team_id": b, "team_name": f"T{b}",
                     "xg": bv, "xg_against": av, "goals": 1,
                     "goals_conceded": 2, "kickoff_utc": ko,
                     "round_label": f"Regular Season - {i}"})
        fid += 1
    return rows


def test_fit_produces_scoped_ratings_with_provenance():
    as_of = datetime(2026, 5, 2, tzinfo=timezone.utc)
    out = fit_league_ratings(_rows(), BUNDESLIGA, as_of=as_of, min_fixtures=5)
    assert set(out["ratings"]) == {10, 20}
    r = out["ratings"][10]
    assert r.scope == BUNDESLIGA
    assert r.n_fixtures == 6
    assert isinstance(r.attack, ScopedStrength)
    assert r.attack.scope == BUNDESLIGA
    # the better attacking side is above the league mean, the other below
    assert r.attack.for_display() > 1.0
    assert out["ratings"][20].attack.for_display() < 1.0
    assert r.window_start is not None and r.window_end is not None
    assert r.window_end >= r.window_start


def test_absent_xg_contributes_nothing_and_is_not_a_zero():
    """A fixture with no usable xG must not be counted as a 0. If it were, a
    league with partial coverage would have every rating biased downward — the
    exact 'missing evidence becomes a number' failure."""
    rows = _rows()
    clean = fit_league_ratings(rows, BUNDESLIGA,
                              as_of=datetime(2026, 5, 2, tzinfo=timezone.utc),
                              min_fixtures=5)
    holed = [dict(r) for r in rows]
    for r in holed[:2]:                      # blank one fixture, both sides
        r["xg"] = None
        r["xg_against"] = None
    out = fit_league_ratings(holed, BUNDESLIGA,
                             as_of=datetime(2026, 5, 2, tzinfo=timezone.utc),
                             min_fixtures=5)
    # n drops by exactly the blanked fixture, and the surviving rating is not
    # dragged toward zero
    assert out["ratings"][10].n_fixtures == clean["ratings"][10].n_fixtures - 1
    assert out["ratings"][10].attack.for_display() > 1.0


def test_below_sample_floor_is_refused_not_shrunk():
    """A club under the floor gets 'not enough fixtures', never a number that
    is mostly the prior."""
    out = fit_league_ratings(_rows(n_per_team=3), BUNDESLIGA,
                             as_of=datetime(2026, 5, 2, tzinfo=timezone.utc),
                             min_fixtures=5)
    assert out["ratings"] == {}
    assert set(out["refused"]) == {10, 20}
    assert all(v == "insufficient_fixtures" for v in out["refused"].values())


def test_recency_weighting_favours_recent_form():
    """The half-life must actually bite: the same fixtures with the recent ones
    reversed produce a different rating."""
    as_of = datetime(2026, 5, 2, tzinfo=timezone.utc)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recent_good = _rows(n_per_team=6, xg=(2.4, 0.8), base=base)
    # push the same fixtures a year back: weights collapse, rating pulls to 1
    old = [dict(r, kickoff_utc=r["kickoff_utc"] - timedelta(days=365))
           for r in recent_good]
    r_new = fit_league_ratings(recent_good, BUNDESLIGA, as_of=as_of,
                               min_fixtures=5)["ratings"][10]
    r_old = fit_league_ratings(old, BUNDESLIGA, as_of=as_of,
                               min_fixtures=5)["ratings"][10]
    assert r_new.attack.for_display() > r_old.attack.for_display()


def test_empty_input_reports_not_ingested():
    out = fit_league_ratings([], BUNDESLIGA)
    assert out["ratings"] == {}
    assert out["reason"] == "league_not_ingested"


# --- provider parsing: hollow values are absence, never zero --------------

@pytest.mark.parametrize("raw", [None, "", "-", "–", "null", "none", "n/a",
                                "abc", True, False, 0, 0.0, "0", "0.0"])
def test_parse_xg_treats_hollow_and_zero_as_absent(raw):
    """The statistic TYPE is present in responses where the VALUE is null, so a
    type check succeeds while carrying nothing. Exactly 0 is absent too: the
    provider uses null and 0 interchangeably as placeholders and the two cannot
    be distinguished."""
    assert apifootball.parse_xg(raw) is None


@pytest.mark.parametrize("raw,want", [("1.81", 1.81), (2, 2.0), (0.31, 0.31),
                                      ("2.48", 2.48)])
def test_parse_xg_reads_real_values(raw, want):
    assert apifootball.parse_xg(raw) == pytest.approx(want)


def test_xg_type_rejects_the_against_variant():
    """Accepting xGA would let coverage be claimed on the opponent's number."""
    assert apifootball.is_xg_type("expected_goals")
    assert apifootball.is_xg_type("xG")
    for bad in ("expected_goals_against", "xGA", "goals_prevented",
                "expected_goals_conceded"):
        assert not apifootball.is_xg_type(bad)


def test_read_fixture_stats_reports_type_present_without_value():
    """The hollow form must be visible, not dropped: this is what a coverage
    measurement is for."""
    payload = {"response": [
        {"team": {"id": 165, "name": "Borussia Dortmund"},
         "statistics": [{"type": "expected_goals", "value": None}]},
        {"team": {"id": 172, "name": "Stuttgart"},
         "statistics": [{"type": "expected_goals", "value": None}]}]}
    rows = apifootball.read_fixture_stats(payload)
    assert len(rows) == 2
    assert all(r["xg_type"] == "expected_goals" for r in rows)
    assert all(r["xg"] is None for r in rows)


def test_provider_errors_inside_http_200():
    """API-Football reports plan and parameter problems in the body, so a 200
    is not a success signal. The accented-search failure was exactly this."""
    body = {"errors": {"search": "The Search field may only contain "
                                 "alpha-numeric characters and spaces."},
            "response": []}
    assert apifootball.provider_errors(body)
    assert apifootball.ascii_search_term("Atlético Madrid") == "atletico madrid"
    assert apifootball.ascii_search_term("Almería") == "almeria"


def test_spread_sample_is_not_most_recent():
    """A most-recent-N sample hits the end-of-season playoff block, which
    carries no xG — that alone called three fully covered leagues 'partial'."""
    seq = list(range(100))
    picked = apifootball.spread_sample(seq, 5)
    assert picked[0] == 0 and picked[-1] == 99
    assert picked != seq[-5:]
    assert apifootball.spread_sample(seq, 200) == seq
    assert apifootball.spread_sample([], 5) == []


def test_value_hash_distinguishes_a_changed_reading():
    """The discriminator that turns a silent overwrite into a second row."""
    a = apifootball.value_hash("1.81", "0.31", 2, 1)
    assert a == apifootball.value_hash("1.81", "0.31", 2, 1)
    assert a != apifootball.value_hash("1.82", "0.31", 2, 1)
    # a formatting change is still a change in what we were told
    assert a != apifootball.value_hash("1.810", "0.31", 2, 1)


def test_mls_is_refused_by_name():
    """MLS keeps Sportec: its xG is free, measured, and inside the engine
    signature. This provider must never supply it."""
    with pytest.raises(apifootball.ApiFootballError) as exc:
        apifootball.measure_coverage(apifootball.MLS_LEAGUE_ID, 2026)
    assert "Sportec" in str(exc.value)
    with pytest.raises(apifootball.ApiFootballError):
        apifootball.ingest_league_xg(apifootball.MLS_LEAGUE_ID, 2026)


# --- club -> league resolution -------------------------------------------

def _index():
    return ({
        (39, 2025): [{"provider_team_id": 42, "name": "Arsenal",
                      "country": "England", "league_id": 39, "season": 2025},
                     {"provider_team_id": 34, "name": "Newcastle",
                      "country": "England", "league_id": 39, "season": 2025}],
        (116, 2026): [{"provider_team_id": 999, "name": "Arsenal",
                       "country": "Belarus", "league_id": 116,
                       "season": 2026}],
        (135, 2025): [{"provider_team_id": 505, "name": "Inter",
                       "country": "Italy", "league_id": 135, "season": 2025}],
    }, {(39, 2025): {"league_name": "Premier League"},
        (116, 2026): {"league_name": "Premier League"},
        (135, 2025): {"league_name": "Serie A"}})


def test_directional_containment_matches_the_espn_name_form():
    """ESPN's 'Newcastle United' against the provider's 'Newcastle'."""
    index, meta = _index()
    r = apifootball.resolve_from_index("Newcastle United", index, meta)
    assert r["resolution"] == "resolved"
    assert r["match_tier"] == "unique_containment"
    assert r["league_id"] == 39


def test_containment_is_directional_so_a_reserve_side_never_matches():
    """The reverse direction is what makes 'Real Madrid' match 'Real Madrid
    Castilla' — the P0-1 hazard. A longer roster name must NOT match."""
    index = {(140, 2025): [{"provider_team_id": 1, "league_id": 140,
                            "season": 2025, "name": "Real Madrid Castilla"}]}
    r = apifootball.resolve_from_index("Real Madrid", index, {})
    assert r["resolution"] == "league_not_indexed"
    assert apifootball.match_tier("Real Madrid", "Real Madrid Castilla") is None


def test_country_homonym_is_ambiguous_before_narrowing():
    index, meta = _index()
    r = apifootball.resolve_from_index("Arsenal", index, meta)
    assert r["resolution"] == "ambiguous_team"
    assert r["tier_attempted"] == "exact"


def test_slate_narrowing_resolves_the_homonym_it_can_and_only_that_one():
    """A league another club resolved into unambiguously is a league this slate
    draws on. Arsenal resolves to England because Newcastle confirmed league
    39; nothing confirms the Belarusian league."""
    index, meta = _index()
    out = {r["query_name"]: r for r in apifootball.resolve_all_from_index(
        ["Newcastle United", "Arsenal"], index, meta)}
    assert out["Arsenal"]["resolution"] == "resolved"
    assert out["Arsenal"]["league_id"] == 39
    assert out["Arsenal"]["match_tier"] == "slate_confirmed_league"
    assert len(out["Arsenal"]["narrowed_from"]) == 2


def test_narrowing_refuses_when_nothing_confirms_either_league():
    """The narrowing breaks ties; it never invents one."""
    index, meta = _index()
    out = {r["query_name"]: r for r in
           apifootball.resolve_all_from_index(["Arsenal"], index, meta)}
    assert out["Arsenal"]["resolution"] == "ambiguous_team"


def test_alias_applies_to_roster_matching_not_only_search():
    """An alias that only reached the search term fixed discovery while leaving
    the authoritative resolution broken — which is what happened first."""
    index, meta = _index()
    r = apifootball.resolve_from_index("Internazionale", index, meta)
    assert r["resolution"] == "resolved"
    assert r["league_id"] == 135


def test_development_and_friendly_competitions_are_not_domestic_leagues():
    """Indexing these manufactures reserve-side ambiguity, and a club's league
    form does not come from its U18 side or a pre-season tournament."""
    for name in ("Premier League - Summer Series", "Premier League 2 Division One",
                 "U18 Premier League - North", "Women's Championship",
                 "National League - Play-offs", "Campeonato de Portugal - Group B"):
        assert not apifootball._looks_domestic_league(name), name
    for name in ("Premier League", "Serie A", "J1 League", "Eredivisie",
                 "Süper Lig", "Championship"):
        assert apifootball._looks_domestic_league(name), name


def test_propose_leagues_excludes_cups_and_uses_the_current_flag():
    payload = {"response": [
        {"league": {"id": 39, "name": "Premier League", "type": "League"},
         "country": {"name": "England"},
         "seasons": [{"year": 2025, "current": False},
                     {"year": 2026, "current": True}]},
        {"league": {"id": 45, "name": "FA Cup", "type": "Cup"},
         "country": {"name": "England"},
         "seasons": [{"year": 2026, "current": True}]},
        {"league": {"id": 1022, "name": "Premier League - Summer Series",
                    "type": "League"},
         "country": {"name": "England"},
         "seasons": [{"year": 2025, "current": True}]}]}
    got = apifootball.propose_leagues(payload)
    assert [g["league_id"] for g in got] == [39]
    assert got[0]["season"] == 2026


# --- the fixture surface never produces a forecast ------------------------

def test_fixture_ratings_publishes_no_forecast(monkeypatch):
    monkeypatch.setattr("src.live.apifootball.club_league",
                        lambda name: None)
    out = fixture_ratings("Cerezo Osaka", "Borussia Dortmund")
    assert out["forecast"] is None
    assert out["forecast_available"] is False
    assert out["comparable"] is False
    assert out["home"]["rated"] is False
    assert out["home"]["reason"] == "league_not_looked_up"
    # every refusal carries words a surface can render
    assert out["home"]["reason_words"]


def test_attribution_refuses_forecast_vocabulary():
    """The same discipline friendlies.IMPLIED_ATTRIBUTION follows: inside this
    surface's own copy, forecast words would relabel league form as a
    prediction."""
    from src.live.xg_ratings import (NOT_COMPARABLE_NOTE, RATINGS_ATTRIBUTION,
                                     REFUSAL_WORDS)
    blob = " ".join([RATINGS_ATTRIBUTION, NOT_COMPARABLE_NOTE,
                     *REFUSAL_WORDS.values()]).lower()
    for banned in ("prediction", "forecast our", "edge", "fair value",
                   "expected winner", "advice"):
        assert banned not in blob, banned
    assert "not comparable" in RATINGS_ATTRIBUTION.lower()
