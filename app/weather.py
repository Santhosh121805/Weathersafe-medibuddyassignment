

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

CURRENT_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "wind_speed_10m",
    "wind_gusts_10m",
    "precipitation",
    "is_day",
    "weather_code",
]

HOURLY_FIELDS = [
    "precipitation",
    "precipitation_probability",
    "uv_index",
    "wind_gusts_10m",
    "visibility",
]


THUNDERSTORM_CODES = {95, 96, 99}

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class WeatherError(RuntimeError):
    """Typed failure. `kind` is what the graph branches on."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  
        self.message = message


@dataclass
class Location:
    name: str
    country: str
    admin1: str | None
    latitude: float
    longitude: float
    timezone: str
    alternatives: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ", ".join([b for b in (self.name, self.admin1, self.country) if b])


def resolve_location(city: str, client: httpx.Client | None = None) -> Location:
    """City name -> coordinates.

    A geocoding miss is the same class of failure as the forecast API being
    down: we never guess a location. Ambiguous names take the first result
    (Open-Meteo ranks by population) but alternatives are carried through so
    the answer can name which place it used.
    """
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.get(GEOCODE_URL, params={"name": city, "count": 5, "language": "en"})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise WeatherError("location_lookup_failed", f"geocoding request failed: {exc}") from exc
    except ValueError as exc:
        raise WeatherError("bad_response", f"geocoding returned non-JSON: {exc}") from exc
    finally:
        if own:
            client.close()

    results = data.get("results") or []
    if not results:
        raise WeatherError("location_not_found", f"no coordinates found for {city!r}")

    top = results[0]
    alts = [
        ", ".join(filter(None, [r.get("name"), r.get("admin1"), r.get("country")]))
        for r in results[1:4]
    ]
    return Location(
        name=top.get("name", city),
        country=top.get("country", ""),
        admin1=top.get("admin1"),
        latitude=float(top["latitude"]),
        longitude=float(top["longitude"]),
        timezone=top.get("timezone", "auto"),
        alternatives=alts,
    )


def fetch_forecast(loc: Location, client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    params = {
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "current": ",".join(CURRENT_FIELDS),
        "hourly": ",".join(HOURLY_FIELDS),
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        resp = client.get(FORECAST_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise WeatherError("forecast_failed", f"forecast request failed: {exc}") from exc
    except ValueError as exc:
        raise WeatherError("bad_response", f"forecast returned non-JSON: {exc}") from exc
    finally:
        if own:
            client.close()

    if "current" not in data or "hourly" not in data:
        raise WeatherError(
            "bad_response",
            "forecast response missing current/hourly blocks - field list likely rejected",
        )
    return data


def _hour_index(times: list[str], current_time: str) -> int:
    stamp = current_time[:13]  # YYYY-MM-DDTHH
    for i, t in enumerate(times):
        if t[:13] == stamp:
            return i
    now = datetime.fromisoformat(current_time)
    for i in range(len(times) - 1, -1, -1):
        if datetime.fromisoformat(times[i]) <= now:
            return i
    return 0


def _window(series: list[Any], start: int, hours: int) -> list[float]:
    return [v for v in series[start : start + hours] if isinstance(v, (int, float))]


def build_facts(data: dict[str, Any], loc: Location) -> dict[str, Any]:
    """Flatten the API response into the fact dict the policy layer checks.

    Every key here comes from THIS response. Nothing is remembered, estimated
    or defaulted. A field the API did not return stays None, and policy.py
    treats a None fact as "condition not satisfied" rather than guessing.
    """
    current = data["current"]
    hourly = data["hourly"]
    times = hourly.get("time", [])
    idx = _hour_index(times, current["time"])

    def at_hour(name: str) -> Any:
        series = hourly.get(name) or []
        return series[idx] if idx < len(series) else None

    precip_24h = _window(hourly.get("precipitation") or [], idx, 24)
    gust_24h = _window(hourly.get("wind_gusts_10m") or [], idx, 24)
    visibility = at_hour("visibility")
    code = current.get("weather_code")

    facts: dict[str, Any] = {
      
        "temperature_2m": current.get("temperature_2m"),
        "apparent_temperature": current.get("apparent_temperature"),
        "relative_humidity_2m": current.get("relative_humidity_2m"),
        "wind_speed_10m": current.get("wind_speed_10m"),
        "wind_gusts_10m": current.get("wind_gusts_10m"),
        "precipitation": current.get("precipitation"),
        "is_day": current.get("is_day"),
        "weather_code": code,
        
        "precipitation_probability": at_hour("precipitation_probability"),
        "uv_index": at_hour("uv_index"),
        
        "is_thunderstorm": code in THUNDERSTORM_CODES if code is not None else False,
        "visibility_poor": visibility is not None and visibility < 2000,
        "hour_local": int(current["time"][11:13]),
        "precip_next_24h_mm": round(sum(precip_24h), 1) if precip_24h else None,
        "max_gust_next_24h": max(gust_24h) if gust_24h else None,
    }

    facts["_meta"] = {
        "location": loc.label,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "observation_time_local": current["time"],
        "timezone": data.get("timezone", loc.timezone),
        "visibility_m": visibility,
        "alternatives_considered": loc.alternatives,
    }
    return facts


def get_weather(city: str, client: httpx.Client | None = None) -> tuple[Location, dict]:
    loc = resolve_location(city, client=client)
    data = fetch_forecast(loc, client=client)
    return loc, build_facts(data, loc)


def numeric_facts(facts: dict[str, Any]) -> dict[str, float]:
    """The numbers the bot is permitted to state. Used by the verifier node."""
    out: dict[str, float] = {}
    for k, v in facts.items():
        if k.startswith("_") or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = float(v)
    meta = facts.get("_meta", {})
    for k in ("visibility_m",):
        if isinstance(meta.get(k), (int, float)):
            out[k] = float(meta[k])
    return out


def describe_facts(facts: dict[str, Any]) -> str:
    """Render the facts block that goes into the composer prompt."""
    meta = facts.get("_meta", {})
    lines = [
        f"location: {meta.get('location')}",
        f"local observation time: {meta.get('observation_time_local')}",
    ]
    units = {
        "temperature_2m": "C",
        "apparent_temperature": "C",
        "relative_humidity_2m": "%",
        "wind_speed_10m": "km/h",
        "wind_gusts_10m": "km/h",
        "max_gust_next_24h": "km/h",
        "precipitation": "mm this hour",
        "precip_next_24h_mm": "mm over 24h",
        "precipitation_probability": "%",
        "uv_index": "",
    }
    for key, unit in units.items():
        val = facts.get(key)
        if val is not None:
            lines.append(f"{key}: {val} {unit}".strip())
    if meta.get("visibility_m") is not None:
        lines.append(f"visibility: {meta['visibility_m']} m")
    lines.append(f"is_day: {facts.get('is_day')}")
    lines.append(f"thunderstorm_reported: {facts.get('is_thunderstorm')}")
    return "\n".join(lines)