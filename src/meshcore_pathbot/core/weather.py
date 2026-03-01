"""Weather service: fetch current conditions and forecasts via Open-Meteo and Nominatim."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("pathbot.weather")

# WMO weather interpretation codes → human-readable description
WMO_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Icy fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}

LIGHTNING_CODES: frozenset[int] = frozenset({95, 96, 99})  # WMO thunderstorm codes

_OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
_NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {"User-Agent": "MeshCore-PathBot/1.0 (weather command)"}


def _wmo_desc(code: int) -> str:
    """Return a human-readable description for a WMO weather code."""
    return WMO_DESCRIPTIONS.get(code, f"Code {code}")


async def _fetch_json(url: str, headers: dict[str, str] | None = None) -> dict | list:
    """Fetch JSON from *url* in a thread-pool executor (non-blocking)."""
    def _do_fetch() -> dict | list:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_fetch)


async def get_coords_for_postcode(postcode: str) -> tuple[float, float, str] | None:
    """Return (lat, lon, place_name) for an Australian postcode, or None on failure."""
    url = (
        f"{_NOMINATIM_BASE}"
        f"?postalcode={urllib.parse.quote(postcode)}"
        f"&countrycodes=au&format=json&limit=1&addressdetails=1"
    )
    try:
        data = await _fetch_json(url, headers=_NOMINATIM_HEADERS)
        if not isinstance(data, list) or not data:
            return None
        hit = data[0]
        lat = float(hit["lat"])
        lon = float(hit["lon"])
        # Build a short display name: suburb/town, state
        addr = hit.get("address", {})
        suburb = (
            addr.get("suburb")
            or addr.get("town")
            or addr.get("city")
            or addr.get("county")
            or hit.get("display_name", postcode).split(",")[0]
        )
        state = addr.get("state_code") or addr.get("state", "")
        name = f"{suburb}, {state}".strip(", ") if state else suburb
        return lat, lon, name
    except Exception as exc:
        log.warning("Nominatim lookup failed for postcode %s: %s", postcode, exc)
        return None


async def get_current_weather(lat: float, lon: float) -> dict:
    """Return raw Open-Meteo current-weather dict for the given coordinates."""
    url = (
        f"{_OPEN_METEO_BASE}"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,weather_code,wind_speed_10m"
        f"&timezone=auto"
    )
    data = await _fetch_json(url)
    if not isinstance(data, dict):
        raise ValueError("Unexpected response from Open-Meteo")
    return data


async def get_forecast(lat: float, lon: float) -> dict:
    """Return raw Open-Meteo 3-day forecast dict for the given coordinates."""
    url = (
        f"{_OPEN_METEO_BASE}"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min"
        f"&forecast_days=3"
        f"&timezone=auto"
    )
    data = await _fetch_json(url)
    if not isinstance(data, dict):
        raise ValueError("Unexpected response from Open-Meteo")
    return data


async def check_lightning(lat: float, lon: float) -> bool:
    """Return True if current weather at the given coordinates is a thunderstorm."""
    try:
        data = await get_current_weather(lat, lon)
        current = data.get("current", {})
        code = current.get("weather_code")
        if code is None:
            return False
        return int(code) in LIGHTNING_CODES
    except Exception as exc:
        log.warning("Lightning check failed: %s", exc)
        return False


async def current_weather_reply(sender: str, lat: float, lon: float, location_name: str) -> str:
    """Fetch current weather and return a formatted bot reply string."""
    data = await get_current_weather(lat, lon)
    current = data.get("current", {})
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    wind = current.get("wind_speed_10m")

    if temp is None or code is None:
        return f"@[{sender}] Weather data unavailable for {location_name}"

    desc = _wmo_desc(int(code))
    parts = [f"{location_name}: {temp:.0f}°C, {desc}"]
    if wind is not None:
        parts.append(f"wind {wind:.0f} km/h")
    return f"@[{sender}] {', '.join(parts)}"


async def forecast_reply(sender: str, lat: float, lon: float, location_name: str) -> str:
    """Fetch 3-day forecast and return a formatted bot reply string."""
    data = await get_forecast(lat, lon)
    daily = data.get("daily", {})
    times = daily.get("time", [])
    codes = daily.get("weather_code", [])
    maxes = daily.get("temperature_2m_max", [])
    mins = daily.get("temperature_2m_min", [])

    if not times:
        return f"@[{sender}] Forecast unavailable for {location_name}"

    day_parts = []
    for i in range(min(3, len(times))):
        try:
            date_obj = datetime.strptime(times[i], "%Y-%m-%d")
            day_label = date_obj.strftime("%a")
        except ValueError:
            day_label = times[i]
        lo = mins[i] if i < len(mins) else "?"
        hi = maxes[i] if i < len(maxes) else "?"
        desc = _wmo_desc(int(codes[i])) if i < len(codes) else "?"
        lo_s = f"{lo:.0f}" if isinstance(lo, float) else str(lo)
        hi_s = f"{hi:.0f}" if isinstance(hi, float) else str(hi)
        day_parts.append(f"{day_label} {lo_s}-{hi_s}°C {desc}")

    return f"@[{sender}] {location_name} 3-day: {', '.join(day_parts)}"


async def forecast_broadcast(lat: float, lon: float, location_name: str) -> str:
    """Fetch 3-day forecast and return a formatted broadcast string (no @sender prefix)."""
    data = await get_forecast(lat, lon)
    daily = data.get("daily", {})
    times = daily.get("time", [])
    codes = daily.get("weather_code", [])
    maxes = daily.get("temperature_2m_max", [])
    mins = daily.get("temperature_2m_min", [])

    if not times:
        return f"Forecast unavailable for {location_name}"

    day_parts = []
    for i in range(min(3, len(times))):
        try:
            date_obj = datetime.strptime(times[i], "%Y-%m-%d")
            day_label = date_obj.strftime("%a")
        except ValueError:
            day_label = times[i]
        lo = mins[i] if i < len(mins) else "?"
        hi = maxes[i] if i < len(maxes) else "?"
        desc = _wmo_desc(int(codes[i])) if i < len(codes) else "?"
        lo_s = f"{lo:.0f}" if isinstance(lo, float) else str(lo)
        hi_s = f"{hi:.0f}" if isinstance(hi, float) else str(hi)
        day_parts.append(f"{day_label} {lo_s}-{hi_s}°C {desc}")

    return f"{location_name} 3-day forecast: {', '.join(day_parts)}"


