"""Venue-city fallback: used only when defensible, refusal named."""
import pytest

from src.live import venue_city as vc


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(vc, "_cache", {})


def _wire(monkeypatch, vname="Chase Stadium", city="Fort Lauderdale, Florida"):
    monkeypatch.setattr(
        vc, "_team_venue",
        lambda tid: {"vname": vname,
                     "city": str(city).split(",")[0].strip()})


class TestCityFor:
    def test_no_fixture_venue_uses_registered_ground_and_says_so(
            self, monkeypatch):
        _wire(monkeypatch)
        city, basis = vc.city_for(9568, None)
        assert city == "Fort Lauderdale"
        assert "assumed" in basis and "Chase Stadium" in basis

    def test_matching_venue_name_fills_the_city(self, monkeypatch):
        _wire(monkeypatch, vname="TQL Stadium", city="Cincinnati")
        city, basis = vc.city_for(1, "TQL  stadium")
        assert city == "Cincinnati"
        assert "matches" in basis

    def test_a_different_venue_refuses_rather_than_guessing(
            self, monkeypatch):
        _wire(monkeypatch)
        city, basis = vc.city_for(9568, "Nu Stadium")
        assert city is None
        assert "undecidable" in basis
        assert "Nu Stadium" in basis and "Chase Stadium" in basis

    def test_no_team_id_is_a_named_refusal(self):
        city, basis = vc.city_for(None, "Anywhere Arena")
        assert city is None and "no home team id" in basis

    def test_provider_failure_is_named_and_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(vc, "_team_venue", lambda tid: None)
        city, basis = vc.city_for(5, None)
        assert city is None and "could not be read" in basis


class TestStateSanitation:
    def test_state_suffix_is_stripped_for_the_geocoder(self, monkeypatch):
        calls = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            class R:
                status_code = 200
                def json(self):
                    return {"response": [{
                        "team": {"country": "USA"},
                        "venue": {"name": "Chase Stadium",
                                  "city": "Fort Lauderdale, Florida"}}]}
            return R()
        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr("src.friendlies_apif.load_key",
                            lambda: ("k", "env", []))
        out = vc._team_venue(42)
        assert out == {"vname": "Chase Stadium", "city": "Fort Lauderdale",
                       "country": "USA"}
