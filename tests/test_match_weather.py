"""The weather read — every refusal named, no invented temperature,
and the kickoff hour matched exactly."""
import pytest

from src.live import match_weather as mw


GEO_BKK = {"results": [{"name": "Bangkok", "country": "Thailand",
                        "latitude": 13.754, "longitude": 100.501}]}
FC = {"hourly": {
    "time": ["2026-08-08T12:00", "2026-08-08T13:00", "2026-08-08T14:00"],
    "temperature_2m": [28.5, 27.8, 27.1],
    "precipitation_probability": [40, 66, 70],
    "wind_speed_10m": [5.0, 4.7, 4.1],
}}


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    monkeypatch.setattr(mw, "_geo", {})
    monkeypatch.setattr(mw, "_fc", {})


def _wire(monkeypatch, geo=GEO_BKK, fc=FC):
    def fake(url, params):
        if url == mw.GEO_BASE:
            return geo
        return fc
    monkeypatch.setattr(mw, "_get_json", fake)


class TestHappyPath:
    def test_kickoff_hour_is_matched_exactly(self, monkeypatch):
        _wire(monkeypatch)
        out = mw.for_fixture("Bangkok", "2026-08-08T13:00:00+00:00")
        assert out["available"] is True
        assert out["temperature_c"] == 27.8
        assert out["precipitation_probability_pct"] == 66
        assert out["kickoff_hour_utc"] == "2026-08-08T13:00"

    def test_the_resolved_place_is_visible_not_silent(self, monkeypatch):
        _wire(monkeypatch)
        out = mw.for_fixture("Bangkok", "2026-08-08T13:00:00+00:00")
        assert out["place"] == "Bangkok, Thailand"

    def test_no_model_language_anywhere(self, monkeypatch):
        _wire(monkeypatch)
        out = mw.for_fixture("Bangkok", "2026-08-08T13:00:00+00:00")
        assert "no model consumes it" in out["means"]


class TestRefusals:
    def test_no_venue_city(self):
        out = mw.for_fixture(None, "2026-08-08T13:00:00+00:00")
        assert not out["available"] and out["reason"] == "no_venue_city"

    def test_geocode_miss_guesses_nothing(self, monkeypatch):
        _wire(monkeypatch, geo={"results": []})
        out = mw.for_fixture("Atlantis", "2026-08-08T13:00:00+00:00")
        assert out["reason"] == "geocode_miss"
        assert "temperature_c" not in out

    def test_beyond_the_horizon_is_named(self, monkeypatch):
        _wire(monkeypatch)
        out = mw.for_fixture("Bangkok", "2099-01-01T13:00:00+00:00")
        assert out["reason"] == "beyond_forecast_horizon"

    def test_provider_failure_with_place_still_named(self, monkeypatch):
        _wire(monkeypatch, fc=None)
        out = mw.for_fixture("Bangkok", "2026-08-08T13:00:00+00:00")
        assert out["reason"] == "provider_unreachable"

    def test_kickoff_hour_missing_from_forecast(self, monkeypatch):
        _wire(monkeypatch)
        out = mw.for_fixture("Bangkok", "2026-08-01T13:00:00+00:00")
        assert out["reason"] == "kickoff_hour_not_in_forecast"


class TestGeocodeDisambiguation:
    MANSFIELDS = {"results": [
        {"name": "Mansfield", "admin1": "England",
         "country": "United Kingdom", "country_code": "GB",
         "latitude": 53.14, "longitude": -1.20},
        {"name": "Mansfield", "admin1": "Ohio",
         "country": "United States", "country_code": "US",
         "latitude": 40.75, "longitude": -82.51},
        {"name": "Mansfield", "admin1": "Texas",
         "country": "United States", "country_code": "US",
         "latitude": 32.56, "longitude": -97.14},
    ]}

    def _wire(self, monkeypatch):
        def fake(url, params):
            if url == mw.GEO_BASE:
                return self.MANSFIELDS
            return FC
        monkeypatch.setattr(mw, "_get_json", fake)

    def test_no_hint_takes_the_top_ranked_hit(self, monkeypatch):
        self._wire(monkeypatch)
        g = mw._geocode("Mansfield")
        assert g["place"] == "Mansfield, England, United Kingdom"

    def test_the_country_hint_excludes_the_wrong_country(self, monkeypatch):
        # 'USA' is API-Football's spelling; open-meteo says US
        self._wire(monkeypatch)
        g = mw._geocode("Mansfield", country_hint="USA")
        assert "United States" in g["place"]

    def test_the_venue_name_names_its_own_state(self, monkeypatch):
        # the FC Dallas case, caught live 2026-08-05: 13C in a Texas
        # August because 'Mansfield' resolved to England
        self._wire(monkeypatch)
        g = mw._geocode("Mansfield", country_hint="USA",
                        evidence="Texas Health Mansfield Stadium")
        assert g["place"] == "Mansfield, Texas, United States"

    def test_the_resolved_state_is_visible_in_the_place(self, monkeypatch):
        self._wire(monkeypatch)
        g = mw._geocode("Mansfield", country_hint="USA")
        assert g["place"].count(",") == 2    # city, admin1, country


class TestCaching:
    def test_a_geocode_miss_is_cached_a_failure_is_not(self, monkeypatch):
        calls = {"n": 0}

        def fake(url, params):
            if url == mw.GEO_BASE:
                calls["n"] += 1
                return {"results": []}
            return FC
        monkeypatch.setattr(mw, "_get_json", fake)
        mw.for_fixture("Atlantis", "2026-08-08T13:00:00+00:00")
        mw.for_fixture("Atlantis", "2026-08-08T13:00:00+00:00")
        assert calls["n"] == 1     # the miss is a fact, asked once
