

class TestLeaguesCup:
    """MLS v Liga MX. Both models cover these clubs; neither may price a
    single fixture — the ratings are relative to each league's own mean
    and cross-league arithmetic is refused by construction."""

    def test_registered_with_probed_ticker(self):
        from src import competitions
        v = competitions.VIEWERS["leagues-cup"]
        assert v.kalshi_series == "KXLEAGUESCUPGAME"   # probed, not guessed
        assert v.apif_league_id == 772
        assert v.no_model == competitions.BY_DESIGN

    def test_the_reason_is_its_own_not_the_uefa_cups(self):
        """The shared BY_DESIGN text claims qualifying rounds starve the
        sample. That is false here — every club is well-rated — and one
        wrong sentence under a heading is worse than none."""
        from src import competitions
        why = competitions.VIEWERS["leagues-cup"].model_block()["why"]
        assert "qualifying" not in why
        assert "own league" in why
        assert "cross-league" in why or "conversion" in why

    def test_the_custom_why_does_not_leak_to_other_viewers(self):
        from src import competitions
        for key, v in competitions.VIEWERS.items():
            if key == "leagues-cup":
                continue
            assert "confident about nothing" not in v.model_block()["why"], key
