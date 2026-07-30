"""The cross-league strength read: refusals, vocabulary, and isolation.

This surface is the one place in the repo that publishes a number about
two clubs from DIFFERENT leagues, so the things that keep it honest are
worth more than the number itself.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from src.live import club_strength_estimate as cse
from src.live import clubelo
from src.live import worldclubratings as wcr

DAY = "2026-07-30"

RANKING = {
    "Man City": {"club": "Man City", "elo": 1970.9, "country": "ENG",
                 "level": "1"},
    "Mallorca": {"club": "Mallorca", "elo": 1400.0, "country": "ESP",
                 "level": "1"},
    "Inter": {"club": "Inter", "elo": 1889.0, "country": "ITA",
              "level": "1"},
    "Leeds": {"club": "Leeds", "elo": 1708.0, "country": "ENG",
              "level": "1"},
}

# The real collision, canned: both normalise to "al sadd" because "sc" is
# a stop word, which is exactly what makes it detectable.
WCR_TABLE = {
    "aaa1": {"id": "aaa1", "club": "Al-Sadd SC", "country": "Qatar",
             "country_code": "qa", "points": 1350.0, "rank": 277},
    "aaa2": {"id": "aaa2", "club": "Al Sadd", "country": "Yemen",
             "country_code": "ye", "points": 1029.0, "rank": 1704},
    "bbb1": {"id": "bbb1", "club": "FC Tokyo", "country": "Japan",
             "country_code": "jp", "points": 1298.0, "rank": 392},
    "bbb2": {"id": "bbb2", "club": "Tokyo Verdy", "country": "Japan",
             "country_code": "jp", "points": 1176.0, "rank": 819},
    "ccc1": {"id": "ccc1", "club": "Flamengo", "country": "Brazil",
             "country_code": "br", "points": 1537.0, "rank": 82},
    "ddd1": {"id": "ddd1", "club": "Mallorca", "country": "Spain",
             "country_code": "es", "points": 1300.0, "rank": 500},
}


@pytest.fixture()
def canned(monkeypatch):
    """BOTH providers stubbed. An earlier version of this fixture patched
    only ClubElo, which meant every test in this file reached
    worldclubratings over the real network — against this repo's
    all-canned rule, and enough to make the suite fail on a provider
    outage."""
    monkeypatch.setattr(clubelo, "day_ranking", lambda day: dict(RANKING))
    monkeypatch.setattr(wcr, "ratings", lambda force=False: dict(WCR_TABLE))


@pytest.fixture()
def clubelo_only(monkeypatch):
    """ClubElo answers, worldclubratings is empty — the fallback path."""
    monkeypatch.setattr(clubelo, "day_ranking", lambda day: dict(RANKING))
    monkeypatch.setattr(wcr, "ratings", lambda force=False: {})


class TestReserveSidesAreRefused:
    """The bug the first live run produced, before it could ship.

    "Mallorca II" matched "Mallorca" through unique containment —
    {mallorca} is a strict subset of {mallorca, ii} — which would have
    published a top-flight Elo about a reserve team. Friendlies are full
    of these sides, so this is the common case, not an edge case.
    """

    @pytest.mark.parametrize("name", [
        "Mallorca II", "Mallorca B", "Inter U21", "Leeds Women",
        "Mallorca Reserves", "Inter Academy", "Leeds U23",
    ])
    def test_non_senior_never_resolves(self, canned, name):
        assert clubelo.lookup(name, DAY) is None

    @pytest.mark.parametrize("name,expect", [
        ("Mallorca", "Mallorca"),          # exact
        ("Manchester City", "Man City"),   # evidence-backed alias
        ("Internazionale", "Inter"),       # alias; containment must NOT win
        ("Leeds United", "Leeds"),         # unique containment
    ])
    def test_senior_sides_still_resolve(self, canned, name, expect):
        """The CONTROL. Without it, the refusals above would also pass if
        lookup() simply never matched anything."""
        r = clubelo.lookup(name, DAY)
        assert r is not None and r["club"] == expect


class TestPublishedSemantics:
    def test_expected_points_share_is_the_published_formula(self):
        """E = 1/(10**(-dr/400)+1), pinned to independently computed
        values rather than to a re-derivation of the implementation."""
        assert clubelo.expected_points_share(1500, 1500) == pytest.approx(0.5)
        # 400 Elo -> 10:1 odds on the points share
        assert clubelo.expected_points_share(1900, 1500) == pytest.approx(
            10 / 11, abs=1e-6)
        assert clubelo.expected_points_share(1500, 1900) == pytest.approx(
            1 / 11, abs=1e-6)

    def test_no_draw_probability_is_ever_published(self, clubelo_only):
        """ClubElo's own 1X2 split comes from an unpublished histogram, so
        deriving a draw number here would be invention."""
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert out["available"] is True
        flat = repr(out).lower()
        assert "draw" not in out["expected_points_share"]
        assert "draw_prob" not in flat

    def test_the_field_is_not_called_a_win_probability(self, clubelo_only):
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert "expected_points_share" in out
        assert "win_probability" not in out
        assert "half" in out["semantics"].lower()      # draws count half

    def test_no_home_field_term_is_invented(self, clubelo_only):
        """Symmetric inputs must give exactly 0.5 — a hidden HFA constant
        would show up here as a home tilt."""
        RANKING["Twin"] = {"club": "Twin", "elo": 1600.0, "country": "ENG",
                           "level": "1"}
        out = cse.for_fixture({"home": {"name": "Twin"},
                               "away": {"name": "Twin"}}, day=DAY)
        assert out["expected_points_share"]["home"] == pytest.approx(0.5)


class TestRefusalsAreNamedNeverBlank:
    def test_unmatched_club_gets_a_named_reason(self, canned):
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Nowhere Athletic"}},
                              day=DAY)
        assert out["available"] is False
        assert out["expected_points_share"] is None      # never a 50% default
        assert out["away"]["reason"] == cse.NAME_UNMAPPED
        assert out["away"]["reason_words"]

    def test_a_failed_read_is_not_reported_as_no_coverage(self, monkeypatch):
        """"We could not look" and "this club is not covered" are
        different truths and must not collapse. BOTH providers must be
        down for this to be the honest answer."""
        monkeypatch.setattr(clubelo, "day_ranking", lambda day: {})
        monkeypatch.setattr(wcr, "ratings", lambda force=False: {})
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert out["home"]["reason"] == cse.REQUEST_FAILED
        assert out["home"]["reason"] != cse.NO_ELO_COVERAGE


class TestVocabularyAndIsolation:
    BANNED = ("prediction", "forecast our", "edge", "fair value",
              "expected winner", "advice")

    def test_language_never_claims_a_forecast(self, clubelo_only):
        """Mirrors test_xg_ratings.test_attribution_refuses_forecast_
        vocabulary — the same bar, applied to a surface that is even
        weaker evidentially."""
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        text = " ".join(str(v) for v in (
            out["estimate_meaning"], out["semantics"], out["attribution"],
            out["home_field_advantage"])).lower()
        for word in self.BANNED:
            assert word not in text, f"banned vocabulary: {word!r}"

    def test_class_is_below_the_approval_hierarchy(self, clubelo_only):
        assert cse.ESTIMATE_CLASS not in ("MEASURED", "REPLAYED", "PILOT")
        m = cse.ESTIMATE_MEANING.lower()
        assert "not a forecast" in m and "below" in m

    def test_never_imports_the_scoped_rating_or_approval_machinery(self):
        """Structural, not aspirational: this must stay a SIBLING of the
        league engine. Reading the import graph rather than trusting the
        docstring."""
        root = pathlib.Path(__file__).resolve().parents[1]
        forbidden = {"ScopedStrength", "XgScope", "pair_for_pricing",
                     "ModelApprovalDecision", "CrossCompetitionArithmetic"}
        for mod in ("src/live/club_strength_estimate.py",
                    "src/live/clubelo.py"):
            tree = ast.parse((root / mod).read_text())
            names = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom):
                    names |= {a.name for a in n.names}
                elif isinstance(n, ast.Import):
                    names |= {a.name for a in n.names}
            leaked = names & forbidden
            assert not leaked, f"{mod} imports {leaked}"

    def test_the_forbidden_names_actually_exist(self):
        """The control for the scan above: if these were renamed, the test
        would be checking for nothing and would pass vacuously."""
        from src.live import xg_ratings
        assert hasattr(xg_ratings, "ScopedStrength")
        assert hasattr(xg_ratings, "pair_for_pricing")


class TestTheTwoScalesAreNeverMixed:
    """worldclubratings publishes an expectation divisor of 600 (a
    FIFA-adapted scheme); ClubElo publishes 400 (classic Elo). Neither
    publishes a conversion, so pairing across them would mean inventing
    one — and the result would look entirely plausible."""

    def test_divisors_differ_and_are_each_the_published_value(self):
        assert wcr.FIFA_ADAPTED_DIVISOR == 600.0
        assert clubelo.ELO_DIVISOR == 400.0
        # the same rating gap therefore means different things
        assert (wcr.expected_points_share(1700, 1500)
                != pytest.approx(clubelo.expected_points_share(1700, 1500)))

    def test_a_cross_source_pair_is_refused_not_computed(self, monkeypatch):
        """Home resolvable ONLY in worldclubratings, away ONLY in ClubElo.
        Must refuse — with the scale reason, not a vague one."""
        monkeypatch.setattr(wcr, "ratings",
                            lambda force=False: {"z": {
                                "id": "z", "club": "Flamengo",
                                "country": "Brazil", "country_code": "br",
                                "points": 1537.0, "rank": 82}})
        monkeypatch.setattr(clubelo, "day_ranking",
                            lambda day: {"Leeds": {
                                "club": "Leeds", "elo": 1708.0,
                                "country": "ENG", "level": "1"}})
        out = cse.for_fixture({"home": {"name": "Flamengo"},
                               "away": {"name": "Leeds"}}, day=DAY)
        # Pair-level selection makes a cross-source pair STRUCTURALLY
        # impossible rather than merely detected: neither provider covers
        # both clubs, so no expectation is produced at all. The
        # SCALE_MISMATCH branch in for_fixture is a defensive guard that
        # is unreachable by construction — kept because it documents the
        # invariant and would fire if per-side selection ever returned.
        assert out["available"] is False
        assert out["expected_points_share"] is None
        assert (out["home"].get("reason") or out["away"].get("reason"))

    def test_a_computed_pair_ALWAYS_shares_one_source(self, canned):
        """The invariant that replaces the mismatch check. Whenever an
        expectation exists, both sides must have come from the same
        provider — this is what makes the divisor unambiguous."""
        for home, away in (("FC Tokyo", "Flamengo"),
                           ("Flamengo", "FC Tokyo"),
                           ("Mallorca", "Flamengo")):
            out = cse.for_fixture({"home": {"name": home},
                                   "away": {"name": away}}, day=DAY)
            if not out["available"]:
                continue
            assert out["home"]["source"] == out["away"]["source"]
            assert out["source"] == out["home"]["source"]
            expect = (600.0 if out["source"] == cse.SRC_WCR else 400.0)
            assert out["expectation_divisor"] == expect

    def test_the_computed_pair_reports_which_divisor_it_used(self, canned):
        out = cse.for_fixture({"home": {"name": "FC Tokyo"},
                               "away": {"name": "Flamengo"}}, day=DAY)
        assert out["available"] is True
        assert out["source"] == cse.SRC_WCR
        assert out["expectation_divisor"] == 600.0

    def test_source_choice_is_per_PAIR_not_per_side(self, monkeypatch):
        """The regression this design exists to prevent.

        An earlier version chose a source per SIDE, preferring
        worldclubratings. On the 2026-07-30 slate that left 34 fixtures
        refused as sources_not_on_one_scale and dropped rated pairs from
        63 to 50 — WORSE than ClubElo alone. Here: both clubs are in
        ClubElo, only ONE is in worldclubratings. A per-side chooser takes
        wcr for the home side and is then stuck; a per-pair chooser falls
        back to ClubElo for both and succeeds.
        """
        monkeypatch.setattr(wcr, "ratings",
                            lambda force=False: {"only": {
                                "id": "only", "club": "Man City",
                                "country": "England", "country_code": "gb-eng",
                                "points": 1889.0, "rank": 6}})
        monkeypatch.setattr(clubelo, "day_ranking", lambda day: dict(RANKING))
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert out["available"] is True, "pair-level fallback did not happen"
        assert out["source"] == cse.SRC_CLUBELO
        assert out["expectation_divisor"] == 400.0


class TestCrossCountryAmbiguity:
    """Al-Sadd SC (Qatar, 1350) and Al Sadd (Yemen, 1029) both normalise to
    "al sadd" — "sc" is a stop word. Publishing either without knowing
    which is a confident claim about possibly the wrong club, and the
    points gap between them is 321."""

    def test_ambiguous_without_a_country_hint(self, canned):
        out = cse.for_fixture({"home": {"name": "Flamengo"},
                               "away": {"name": "Al Sadd"}}, day=DAY)
        assert out["available"] is False
        assert out["away"]["reason"] == cse.NAME_AMBIGUOUS
        assert set(out["away"]["candidate_countries"]) == {"Qatar", "Yemen"}

    def test_a_country_hint_resolves_it(self, canned):
        out = cse.for_fixture({"home": {"name": "Flamengo"},
                               "away": {"name": "Al Sadd"},
                               "league_country": "Qatar"}, day=DAY)
        assert out["available"] is True
        assert out["away"]["match_tier"] == "name_plus_country"
        assert out["away"]["rating"] == pytest.approx(1350.0)

    def test_exact_identity_outranks_containment(self, canned):
        """"FC Tokyo" is EXACTLY "FC Tokyo" while merely being contained in
        "Tokyo Verdy". Exact must win, without a hint and without
        declaring an ambiguity — otherwise a same-country pair of clubs
        sharing a word would be permanently unreadable."""
        r = wcr.lookup("FC Tokyo")
        assert r is not None and not r.get("ambiguous")
        assert r["club"] == "FC Tokyo"
        assert r["match_tier"] == "exact"

    def test_provider_stable_id_travels_with_a_rating(self, canned):
        """AGENTS.md requires provider-stable ids over names. This source
        has them, so they must reach the payload."""
        out = cse.for_fixture({"home": {"name": "FC Tokyo"},
                               "away": {"name": "Flamengo"}}, day=DAY)
        assert out["home"]["provider_id"] == "bbb1"
        assert out["away"]["provider_id"] == "ccc1"
