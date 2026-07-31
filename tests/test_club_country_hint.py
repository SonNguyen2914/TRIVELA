"""An ambiguous club name is disambiguated by the CLUB's country, not the
competition's — and the country is fetched only when it can matter.

`league_country` reads "World" on 314 of 316 club friendlies, so the hint
that exists to separate FC Barcelona (Spain) from Barcelona SC (Ecuador)
disambiguated nothing on the surface that needs it most.

Resolution is by API-Football team ID, never a name search: id 529 IS
Barcelona of Spain and id 1152 IS Barcelona SC of Ecuador, so this carries
no collision surface. A name search would reintroduce the ambiguity it
exists to remove.
"""
from src.live import club_strength_estimate as cse


def _fixture(hint="World"):
    return {"league_country": hint,
            "home": {"name": "Barcelona", "apif_team_id": 529},
            "away": {"name": "Birmingham", "apif_team_id": 54},
            "kickoff_utc": "2026-07-31T11:45:00+00:00"}


def test_country_is_not_fetched_when_nothing_is_ambiguous(monkeypatch):
    """The expensive call must not fire on the common path — it would be
    ~630 provider requests a slate to fix the four fixtures that need it.
    """
    calls = []
    import src.friendlies_apif as fa
    monkeypatch.setattr(fa, "club_country",
                        lambda tid: calls.append(tid) or "Spain")
    monkeypatch.setattr(cse, "_side_from",
                        lambda src, name, day, hint: {"rated": True, "club": name,
                                                      "rating": 1800.0,
                                                      "source": "clubelo"})
    cse.for_fixture(_fixture())
    assert calls == [], f"country fetched {len(calls)} times with no ambiguity"


def test_country_is_fetched_and_retried_on_an_ambiguous_name(monkeypatch):
    calls, seen_hints = [], []
    import src.friendlies_apif as fa
    monkeypatch.setattr(fa, "club_country",
                        lambda tid: calls.append(tid) or "Spain")

    def fake(src, name, day, hint):
        seen_hints.append((name, hint))
        if name == "Barcelona" and hint != "Spain":
            return {"rated": False, "reason": "name_ambiguous", "club": name}
        return {"rated": True, "club": name, "rating": 1800.0,
                "source": "clubelo"}

    monkeypatch.setattr(cse, "_side_from", fake)
    cse.for_fixture(_fixture())
    assert 529 in calls, "an ambiguous name did not trigger a country lookup"
    assert ("Barcelona", "Spain") in seen_hints, "the retry used no hint"


def test_world_is_never_used_as_a_country(monkeypatch):
    """"World" is the competition's country on a friendly. Passing it as a
    club country would be a hint that matches nothing, or worse, a country
    named World in some provider table."""
    seen = []
    monkeypatch.setattr(cse, "_side_from",
                        lambda src, name, day, hint:
                        seen.append(hint) or {"rated": True, "club": name,
                                              "rating": 1800.0,
                                              "source": "clubelo"})
    cse.for_fixture(_fixture("World"))
    assert all(h != "World" for h in seen), seen


def test_a_real_league_country_is_still_used(monkeypatch):
    seen = []
    monkeypatch.setattr(cse, "_side_from",
                        lambda src, name, day, hint:
                        seen.append(hint) or {"rated": True, "club": name,
                                              "rating": 1800.0,
                                              "source": "clubelo"})
    cse.for_fixture(_fixture("England"))
    assert "England" in seen, seen
