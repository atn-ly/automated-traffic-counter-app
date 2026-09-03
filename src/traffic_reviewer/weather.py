from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True)
class DailyWeather:
    day: date
    high_c: float
    low_c: float
    weather_code: int
    condition: str
    precipitation_mm: float
    location_name: str
    source: str = "Open-Meteo"


@dataclass(frozen=True)
class WeatherFetchResult:
    location_name: str
    latitude: float
    longitude: float
    timezone: str
    records: tuple[DailyWeather, ...]


def condition_from_weather_code(code: int) -> str:
    if code == 0:
        return "Sunny"
    if code in {1, 2}:
        return "Partly cloudy"
    if code == 3:
        return "Cloudy"
    if code in {45, 48}:
        return "Foggy"
    if code in {51, 53, 55, 56, 57}:
        return "Drizzle"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "Rainy"
    if code in {71, 73, 75, 77, 85, 86}:
        return "Snowy"
    if code in {95, 96, 99}:
        return "Thunderstorm"
    return "Unknown"


def _read_json(url: str, timeout: float = 20.0) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_daily_weather(
    location_query: str,
    days: list[date],
    timeout: float = 20.0,
) -> WeatherFetchResult:
    clean_location = " ".join(location_query.split())
    if not clean_location:
        raise ValueError("Enter a weather location")
    if not days:
        raise ValueError("Add timestamped recordings before loading weather")

    geocoding_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(
        {
            "name": clean_location,
            "count": 1,
            "language": "en",
            "format": "json",
        }
    )
    geocoding = _read_json(geocoding_url, timeout)
    results = geocoding.get("results") or []
    if not results:
        raise ValueError(f'Weather location "{clean_location}" was not found')
    location = results[0]
    latitude = float(location["latitude"])
    longitude = float(location["longitude"])
    timezone = str(location.get("timezone") or "auto")
    location_parts = [
        str(location.get("name") or clean_location),
        str(location.get("admin1") or ""),
        str(location.get("country") or ""),
    ]
    location_name = ", ".join(part for part in location_parts if part)

    requested_days = sorted(set(days))
    weather_url = "https://archive-api.open-meteo.com/v1/archive?" + urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": requested_days[0].isoformat(),
            "end_date": requested_days[-1].isoformat(),
            "daily": (
                "temperature_2m_max,temperature_2m_min,weather_code,"
                "precipitation_sum"
            ),
            "temperature_unit": "celsius",
            "precipitation_unit": "mm",
            "timezone": timezone,
        }
    )
    payload = _read_json(weather_url, timeout)
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    precipitation = daily.get("precipitation_sum") or []
    weather_by_day = {}
    for day_text, high, low, code, rain in zip(
        dates,
        highs,
        lows,
        codes,
        precipitation,
        strict=False,
    ):
        if high is None or low is None or code is None:
            continue
        weather_day = date.fromisoformat(day_text)
        weather_by_day[weather_day] = DailyWeather(
            weather_day,
            float(high),
            float(low),
            int(code),
            condition_from_weather_code(int(code)),
            float(rain or 0),
            location_name,
        )
    records = tuple(
        weather_by_day[requested_day]
        for requested_day in requested_days
        if requested_day in weather_by_day
    )
    if not records:
        raise ValueError(
            "No historical weather is available for the recording dates yet"
        )
    return WeatherFetchResult(
        location_name,
        latitude,
        longitude,
        timezone,
        records,
    )
