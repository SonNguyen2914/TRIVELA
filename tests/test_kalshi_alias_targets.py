"""Every Kalshi alias must point at a name ESPN ACTUALLY USES.

This defect has now shipped twice, identically. An alias is written
against an assumed ESPN name, matches nothing, and the fixture silently
shows "no open book":

  - "new york rb" -> "new york red bulls"  (ESPN says "Red Bull New York")
  - "los angeles f" -> "los angeles fc"    (ESPN says "LAFC")

Both were caught in production on a slate, not by the suite, because
nothing asserted the RIGHT-HAND SIDE was real. A wrong alias is
indistinguishable from no alias: `_side_matches` just returns False and
the book vanishes.

The frozen list below is ESPN's own displayName for all 30 MLS clubs. It
is data, not a guess — anything added to the alias table must resolve
against it.
"""
import pytest

from src import mls

# ESPN displayName, verified from the live feed 2026-08-01. Deliberately
# a literal: a test that fetched this could never fail on a bad alias,
# because it would fetch whatever the alias expects.
ESPN_MLS_NAMES = {
    "Atlanta United FC", "Austin FC", "CF Montréal", "Charlotte FC",
    "Chicago Fire FC", "FC Cincinnati", "Colorado Rapids",
    "Columbus Crew", "D.C. United", "FC Dallas", "Houston Dynamo FC",
    "Sporting Kansas City", "LA Galaxy", "LAFC", "Inter Miami CF",
    "Minnesota United FC", "CF Montreal", "Nashville SC",
    "New England Revolution", "New York City FC", "Red Bull New York",
    "Orlando City SC", "Philadelphia Union", "Portland Timbers",
    "Real Salt Lake", "San Diego FC", "San Jose Earthquakes",
    "Seattle Sounders FC", "St. Louis City SC", "Toronto FC",
    "Vancouver Whitecaps",
}


@pytest.mark.parametrize("kalshi,target", sorted(mls._KALSHI_ALIASES.items()))
def test_every_alias_target_is_a_real_espn_name(kalshi, target):
    """The right-hand side must MATCH some club ESPN actually names.

    `_side_matches` does `target in normalised_espn_name`, so the target
    has to be a substring of a real one. If it is not, the alias is dead
    and the fixture shows no book.
    """
    hits = [n for n in ESPN_MLS_NAMES if target in mls._norm_name(n)]
    assert hits, (
        f"alias {kalshi!r} -> {target!r} matches NO ESPN club name. "
        f"This is the RBNY/LAFC defect: the alias is dead and every "
        f"fixture for that club will show no open book.")


@pytest.mark.parametrize("kalshi,target", sorted(mls._KALSHI_ALIASES.items()))
def test_no_alias_target_is_ambiguous(kalshi, target):
    """It must resolve to exactly ONE club. `los angeles` would match
    both LA Galaxy and LAFC, and attaching the wrong club's book to a
    fixture is worse than attaching none."""
    hits = [n for n in ESPN_MLS_NAMES if target in mls._norm_name(n)]
    assert len(hits) == 1, (
        f"alias {kalshi!r} -> {target!r} matches {hits} — ambiguous")


def test_the_two_los_angeles_clubs_stay_separable():
    """The specific collision. LAFC and LA Galaxy share 'los angeles' in
    the Kalshi title but are different clubs."""
    assert mls._side_matches("Los Angeles F", "LAFC")
    assert not mls._side_matches("Los Angeles F", "LA Galaxy")
    assert mls._side_matches("Los Angeles G", "LA Galaxy")
    assert not mls._side_matches("Los Angeles G", "LAFC")


def test_the_two_new_york_clubs_stay_separable():
    assert mls._side_matches("New York RB", "Red Bull New York")
    assert not mls._side_matches("New York RB", "New York City FC")
    assert mls._side_matches("New York City", "New York City FC")
    assert not mls._side_matches("New York City", "Red Bull New York")
