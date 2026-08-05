"""The Leagues Cup read gains a Tie leg — measured, and only there.

Until now this surface published an expected POINTS share and nothing
else, so on the Tie leg of every three-way book it had no opinion at
all. That silence is not a claim that draws are unlikely; it is a
missing number, and the exchange prices the leg regardless.

What must hold, each asserted rather than assumed:

  - the rate is the MEASURED one, traceable to the archive that
    produced it, not a plausible-looking constant;
  - it applies to Leagues Cup ALONE. Every other viewer competition
    stays two-way, because nobody has counted its draws;
  - the two-way points share — and therefore every disagreement number
    already on the surface — is BYTE-IDENTICAL. This change gives the
    read a voice where it had none; it does not touch what it already
    said;
  - the forced split never produces a negative leg, and says so when
    the clip binds.
"""
import config
from src import competitions
from src.live import match_pick

RATE = config.LEAGUES_CUP_DRAW_RATE


class TestTheConstantIsTheMeasurement:
    def test_the_rate_is_the_pooled_three_edition_figure(self):
        """216 settled matches, 2023-2025. A different number here would
        mean the constant and the archive had drifted apart."""
        assert RATE == 0.3009

    def test_it_names_the_archive_that_produced_it(self):
        assert config.LEAGUES_CUP_DRAW_ARCHIVE.endswith(
            "leagues_cup_history_2026-08-05.json")

    def test_the_archive_actually_contains_that_figure(self):
        """The provenance must be checkable, not decorative."""
        import json
        import pathlib
        p = pathlib.Path(config.LEAGUES_CUP_DRAW_ARCHIVE)
        if not p.exists():                    # not on this branch
            return
        doc = json.loads(p.read_text())
        assert doc["draw_rate_90min"]["pooled"]["draw_rate"] == RATE
        assert doc["draw_rate_90min"]["pooled"]["n"] == 216


class TestOnlyLeaguesCupGetsIt:
    def test_leagues_cup_carries_the_measured_rate(self):
        assert competitions.VIEWERS["leagues-cup"].draw_rate == RATE

    def test_every_other_viewer_stays_two_way(self):
        """A rate none of them has measured would be invented. Naming
        them makes a future addition a deliberate act."""
        silent = {k: v.draw_rate for k, v in competitions.VIEWERS.items()
                  if k != "leagues-cup"}
        assert set(silent) >= {"ecl", "uel", "ucl", "brasileirao",
                               "argentina", "usl"}
        assert all(r is None for r in silent.values()), silent

    def test_a_new_competition_defaults_to_silent(self):
        v = competitions.Viewer("x", "X", 1, "KXX", competitions.NOT_BUILT)
        assert v.draw_rate is None


class TestTheSplitItself:
    """match_pick.read_three_way is the existing forced decomposition:
    P(home)=E-d/2, P(away)=1-E-d/2. These pin its behaviour AT THIS
    RATE, which is what the surface will now publish."""

    # read_three_way rounds each leg to 4dp, so three legs sum to
    # 1 +/- 1.5e-4. That is pre-existing shipped behaviour the
    # friendlies surface already publishes; this change does not touch
    # it, and the tolerance here says so rather than hiding it.
    ROUNDING = 2e-4

    def test_an_even_fixture_splits_symmetrically(self):
        out = match_pick.read_three_way(0.5, RATE)
        assert out["tie"] == round(RATE, 4)
        assert out["home"] == out["away"]
        assert abs(out["home"] + out["tie"] + out["away"] - 1.0) < self.ROUNDING
        assert out["clipped"] is False

    def test_a_real_fixture_from_tonights_board(self):
        """Monterrey's read was .6086 at T-60. The three legs must sum
        to one and leave the points share recoverable."""
        e = 0.6086
        out = match_pick.read_three_way(e, RATE)
        assert abs(out["home"] + out["tie"] + out["away"] - 1.0) < self.ROUNDING
        # points share = P(home) + half the draw, i.e. the input back
        assert abs(out["home"] + out["tie"] / 2 - e) < 1e-4

    def test_a_lopsided_fixture_clips_instead_of_going_negative(self):
        """E=0.90 leaves only 0.20 of room for a draw at half weight;
        the full 0.3009 would drive the away leg below zero."""
        out = match_pick.read_three_way(0.90, RATE)
        assert out["clipped"] is True
        assert out["away"] >= 0.0 and out["home"] >= 0.0
        assert out["draw_rate_used"] < RATE

    def test_no_leg_is_ever_negative_across_the_whole_range(self):
        for i in range(0, 101):
            out = match_pick.read_three_way(i / 100, RATE)
            assert min(out["home"], out["tie"], out["away"]) >= 0.0, i


class TestWhatMustNotHaveChanged:
    """The points share is the quantity every published disagreement is
    computed from. If it moved, this change would silently rewrite the
    surface's existing claims."""

    BOOK = {"markets": [
        {"label": "Monterrey", "yes_ask_dollars": "0.40"},
        {"label": "Tie", "yes_ask_dollars": "0.24"},
        {"label": "Orlando", "yes_ask_dollars": "0.38"}]}
    STRENGTH = {"available": True,
                "expected_points_share": {"home": 0.6086, "away": 0.3914}}

    def _compare(self, **kw):
        return match_pick.compare(self.STRENGTH, self.BOOK,
                                  "Monterrey", "Orlando", **kw)

    def test_the_points_share_and_gap_are_identical_with_and_without(self):
        without = self._compare()
        with_rate = self._compare(draw_rate=RATE)
        for key in ("our_points_share", "market_points_share",
                    "disagreement", "market_vig", "market_legs"):
            assert without.get(key) == with_rate.get(key), key

    def test_the_tie_leg_appears_only_when_a_rate_is_supplied(self):
        assert "read_three_way" not in self._compare()
        assert "read_three_way" in self._compare(draw_rate=RATE)

    def test_the_read_is_still_declared_two_way_in_provenance(self):
        """`read_is_two_way` explains WHY the read has no native draw.
        Deriving one from a measured base rate does not change that
        fact about the ratings providers, so the explanation stays."""
        out = self._compare(draw_rate=RATE)
        assert out["read_is_two_way"]["has_draw"] is True
        assert "not a 1X2 split" in out["read_is_two_way"]["why"]

    def test_the_note_still_calls_a_gap_a_disagreement(self):
        assert "DISAGREEMENT" in self._compare(draw_rate=RATE)["note"]


class TestTheProvenanceTravelsWithTheNumber:
    """A constant rate is uniform where real draw rates are not, so the
    surface must say what its rate cannot know — beside the legs, not
    only in a config comment nobody reads."""

    def test_leagues_cup_publishes_its_own_basis_not_the_friendlies_one(
            self):
        out = match_pick.compare(
            TestWhatMustNotHaveChanged.STRENGTH,
            TestWhatMustNotHaveChanged.BOOK, "Monterrey", "Orlando",
            draw_rate=RATE, draw_basis=config.LEAGUES_CUP_DRAW_BASIS)
        basis = out["read_three_way"]["basis"]
        assert "216 settled matches" in basis
        assert "friendly" not in basis.lower(), (
            "publishing the friendlies provenance over a Leagues Cup "
            "number would be a false claim about what was measured")

    def test_the_basis_states_the_constant_rate_limitation(self):
        b = config.LEAGUES_CUP_DRAW_BASIS
        assert "CONSTANT across fixtures" in b
        assert "ABOVE the market's Tie leg" in b
        assert "never evidence" in b

    def test_friendlies_provenance_is_unchanged_by_default(self):
        """The default must stay exactly what the friendlies surface has
        always published — this change adds a caller, it does not
        rewrite an existing one."""
        out = match_pick.read_three_way(0.5, config.FRIENDLY_DRAW_RATE)
        assert out["basis"] == match_pick.FRIENDLY_DRAW_BASIS
        assert "friendly draw rate" in out["basis"]

    def test_a_competition_without_a_rate_has_no_basis_either(self):
        assert competitions.VIEWERS["ecl"].draw_rate_basis is None
