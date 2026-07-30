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


@pytest.fixture()
def canned(monkeypatch):
    monkeypatch.setattr(clubelo, "day_ranking", lambda day: dict(RANKING))


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

    def test_no_draw_probability_is_ever_published(self, canned):
        """ClubElo's own 1X2 split comes from an unpublished histogram, so
        deriving a draw number here would be invention."""
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert out["available"] is True
        flat = repr(out).lower()
        assert "draw" not in out["expected_points_share"]
        assert "draw_prob" not in flat

    def test_the_field_is_not_called_a_win_probability(self, canned):
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert "expected_points_share" in out
        assert "win_probability" not in out
        assert "half" in out["semantics"].lower()      # draws count half

    def test_no_home_field_term_is_invented(self, canned):
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
        different truths and must not collapse."""
        monkeypatch.setattr(clubelo, "day_ranking", lambda day: {})
        out = cse.for_fixture({"home": {"name": "Man City"},
                               "away": {"name": "Mallorca"}}, day=DAY)
        assert out["home"]["reason"] == cse.REQUEST_FAILED
        assert out["home"]["reason"] != cse.NO_ELO_COVERAGE


class TestVocabularyAndIsolation:
    BANNED = ("prediction", "forecast our", "edge", "fair value",
              "expected winner", "advice")

    def test_language_never_claims_a_forecast(self, canned):
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

    def test_class_is_below_the_approval_hierarchy(self, canned):
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
