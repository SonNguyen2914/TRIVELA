"""Kickoff-hour weather for near fixtures — a READ attached beside the
news, never an input to anything.

WHY IT EXISTS. Heavy rain compresses scoring and wind wrecks long balls;
an operator staring at a price wants to know it is monsoon season in
Bangkok. That is CONTEXT, the same class as team news — so it follows
the same rules: attached to near fixtures only, a read never a fetch at
render time... except this provider is free, keyless and fast, so the
fetch happens inline with an in-process cache rather than a capture
table. No model consumes it; nothing here reaches a lock path.

SOURCE. Open-Meteo (open-meteo.com): no key, no signup, hourly forecast
16 days out, and a geocoding endpoint for the venue city. Both probed
2026-08-04. The venue CITY is geocoded, not the stadium name — city
strings resolve reliably, stadium names do not.

WHAT A MIS-GEOCODE LOOKS LIKE. Geocoding picks the most prominent match
for the city string, so a wrong pick is possible (there are many places
called San Jose). The resolved place and country are therefore part of
every payload — a reader can SEE "Bangkok, Thailand" next to the number
instead of trusting a silent resolution.

REFUSALS, named as always: no venue city on the fixture, a geocode
miss, a kickoff beyond the 16-day horizon, a provider failure. Never a
default temperature.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

GEO_BASE = "https://geocoding-api.open-meteo.com/v1/search"
FC_BASE = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 10
FORECAST_TTL = 3 * 3600.0       # forecasts refresh hourly; 3h is plenty
HORIZON_DAYS = 16               # the provider's own hourly horizon
ATTRIBUTION = "Open-Meteo (open-meteo.com), hourly forecast, kickoff hour"

_lock = threading.Lock()
_geo: dict[str, dict | None] = {}                 # city -> result | None
_fc: dict[tuple, tuple[float, dict]] = {}         # (lat,lon) -> (mono, body)


def _get_json(url: str, params: dict) -> dict | None:
    import requests
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _geocode(city: str) -> dict | None:
    with _lock:
        if city in _geo:
            return _geo[city]
    body = _get_json(GEO_BASE, {"name": city, "count": 1})
    hit = ((body or {}).get("results") or [None])[0]
    out = None
    if hit and hit.get("latitude") is not None:
        out = {"lat": round(float(hit["latitude"]), 3),
               "lon": round(float(hit["longitude"]), 3),
               "place": ", ".join(p for p in (hit.get("name"),
                                              hit.get("country")) if p)}
    with _lock:
        # a geocode MISS is cached too — the city string will not start
        # resolving mid-process, and re-asking per render is waste. A
        # provider FAILURE (body None) is not cached, so it retries.
        if body is not None:
            _geo[city] = out
    return out


def _forecast(lat: float, lon: float) -> dict | None:
    key = (lat, lon)
    now = time.monotonic()
    with _lock:
        hit = _fc.get(key)
        if hit and now - hit[0] < FORECAST_TTL:
            return hit[1]
    body = _get_json(FC_BASE, {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
        "forecast_days": HORIZON_DAYS, "timezone": "UTC"})
    if body and (body.get("hourly") or {}).get("time"):
        with _lock:
            _fc[key] = (now, body)
        return body
    return None


def for_fixture(venue_city: str | None, kickoff_utc: str | None) -> dict:
    base = {"available": False, "source": ATTRIBUTION}
    if not venue_city:
        return {**base, "reason": "no_venue_city",
                "means": "the fixture feed carries no venue city, so "
                         "there is nowhere to look up"}
    if not kickoff_utc:
        return {**base, "reason": "no_kickoff"}
    try:
        ko = datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
    except ValueError:
        return {**base, "reason": "unreadable_kickoff"}
    days_out = (ko - datetime.now(timezone.utc)).total_seconds() / 86400.0
    if days_out > HORIZON_DAYS - 1:
        return {**base, "reason": "beyond_forecast_horizon",
                "means": f"kickoff is {days_out:.0f} days out; the "
                         f"provider's hourly horizon is {HORIZON_DAYS} days"}
    geo = _geocode(venue_city)
    if geo is None:
        return {**base, "reason": "geocode_miss", "city": venue_city,
                "means": "the venue city did not resolve to coordinates; "
                         "nothing was guessed in its place"}
    fc = _forecast(geo["lat"], geo["lon"])
    if fc is None:
        return {**base, "reason": "provider_unreachable",
                "place": geo["place"]}
    h = fc["hourly"]
    want = ko.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")
    try:
        i = h["time"].index(want)
    except ValueError:
        return {**base, "reason": "kickoff_hour_not_in_forecast",
                "place": geo["place"]}
    return {
        "available": True,
        "place": geo["place"],
        "kickoff_hour_utc": want,
        "temperature_c": h["temperature_2m"][i],
        "precipitation_probability_pct": h["precipitation_probability"][i],
        "wind_speed_kmh": h["wind_speed_10m"][i],
        "source": ATTRIBUTION,
        "means": ("forecast for the kickoff hour at the venue CITY — a "
                  "context read; no model consumes it"),
    }
