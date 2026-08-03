"""A European provider must never rate a North American pairing.

Measured 2026-08-03, live on the Leagues Cup board: ClubElo's single-token
rows Inter (ITA, 1888.6) and Atletico (ESP, 1827.7) absorbed "Inter
Miami" and "Atletico San Luis" through the row-subset containment tier,
and pair-level selection let the false pair SHADOW a correct
worldclubratings one. Two European giants' ratings published under two
North American clubs.

Three defences, each tested: the containment tier refuses single-token
rows (the five measured legitimate cases are pinned as aliases); the
competition registry pins non-European competitions to worldclubratings
so ClubElo is never consulted at all; and the near-miss probe tokenizes
the club NAME, not the provider's ID key — the first version tokenized
the key, returned zero candidates for every query, and an absence claim
built on it ("all 79 unmapped friendlies clubs are genuinely absent",
2026-07-31) is hereby retracted pending re-measurement.
"""
import pytest

from src.live import club_strength_estimate as cse
from src.live import clubelo, worldclubratings as wcr

CANNED = {"Rank": None}  # not used; clubelo table is name -> row
ELO_TABLE = {
    "Inter": {"club": "Inter", "elo": 1888.6, "country": "ITA"},
    "Atletico": {"club": "Atletico", "elo": 1827.7, "country": "ESP"},
    "Leeds": {"club": "Leeds", "elo": 1797.4, "country": "ENG"},
    "Real Sociedad": {"club": "Real Sociedad", "elo": 1800.0,
                      "country": "ESP"},
}
WCR_TABLE = {
    "id1": {"id": "id1", "club": "Deportivo Toluca", "country": "Mexico",
            "points": 1415.0},
    "id2": {"id": "id2", "club": "UNAM Pumas", "country": "Mexico",
            "points": 1284.0},
    "id3": {"id": "id3", "club": "Inter Miami CF",
            "country": "United States", "points": 1500.0},
    # the six alias targets from the 2026-08-03 re-measurement, verbatim
    # provider strings — recorded here so the alias-target test stays
    # hermetic while still checking against the provider's real spellings
    "id4": {"id": "id4", "club": "RSC Charleroi", "country": "Belgium",
            "points": 1300.0},
    "id5": {"id": "id5", "club": "KRC Genk", "country": "Belgium",
            "points": 1350.0},
    "id6": {"id": "id6", "club": "OSC Lille", "country": "France",
            "points": 1500.0},
    "id7": {"id": "id7", "club": "Stade Rennes", "country": "France",
            "points": 1450.0},
    "id8": {"id": "id8", "club": "1. FC Union Berlin", "country": "Germany",
            "points": 1400.0},
    "id9": {"id": "id9", "club": "SV Zulte Waregem", "country": "Belgium",
            "points": 1200.0},
}


@pytest.fixture(autouse=True)
def _canned(monkeypatch):
    monkeypatch.setattr(clubelo, "day_ranking", lambda day: ELO_TABLE)
    clubelo._idx_cache = None
    monkeypatch.setattr(wcr, "ratings", lambda force=False: WCR_TABLE)
    yield
    clubelo._idx_cache = None


def test_single_token_rows_cannot_absorb_longer_names():
    assert clubelo.lookup("Inter Miami", "2026-08-03") is None
    assert clubelo.lookup("Atletico San Luis", "2026-08-03") is None


def test_the_five_measured_matches_survive_as_aliases():
    r = clubelo.lookup("Leeds United", "2026-08-03")
    assert r and r["match_tier"] == "alias" and r["country"] == "ENG"


def test_multi_token_row_subsets_still_match():
    r = clubelo.lookup("Real Sociedad de Futbol", "2026-08-03")
    assert r and r["club"] == "Real Sociedad"


def test_near_misses_tokenizes_the_club_name_not_the_id_key():
    d = wcr.near_misses("Toluca", limit=3)
    names = [c["provider_name"] for c in d["candidates"]]
    assert "Deportivo Toluca" in names, (
        "zero candidates for a club the table holds — the probe is "
        "tokenizing the provider's ID keys again")


def test_wcr_aliases_resolve_the_measured_misses():
    assert wcr.lookup("Toluca")["club"] == "Deportivo Toluca"
    assert wcr.lookup("U.N.A.M. - Pumas")["club"] == "UNAM Pumas"


def test_every_wcr_alias_target_is_a_row_the_provider_serves():
    """The LAFC lesson, applied here: an alias pointing at a spelling the
    provider does not use is dead, and dead is invisible."""
    live = wcr.ratings()
    clubs = {(r or {}).get("club") for r in live.values()}
    for target in wcr.ALIASES.values():
        assert target in clubs, f"alias target {target!r} matches no row"


def test_non_european_competitions_never_consult_clubelo(monkeypatch):
    from src import competitions
    for key in ("leagues-cup", "brasileirao", "argentina", "usl"):
        assert competitions.VIEWERS[key].strength_sources == (
            "worldclubratings",), key

    def boom(*a, **k):
        raise AssertionError("clubelo consulted for a non-European pair")

    monkeypatch.setattr(clubelo, "lookup", boom)
    f = {"league_country": "World",
         "home": {"name": "Inter Miami"},
         "away": {"name": "UNAM Pumas"},
         "kickoff_utc": "2026-08-05T23:30:00+00:00"}
    out = cse.for_fixture(f, sources=("worldclubratings",))
    assert out.get("available"), out


def test_european_competitions_still_reach_clubelo():
    from src import competitions
    for key in ("ecl", "uel", "ucl"):
        assert competitions.VIEWERS[key].strength_sources is None, key
