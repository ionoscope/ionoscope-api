from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings
from .schemas import ForecastResponse


class ForecastCacheMiss(LookupError):
    pass


def load_forecast(station: str, settings: Settings) -> ForecastResponse:
    path = cache_path(station, settings)
    if not path.exists():
        raise ForecastCacheMiss(f"No cached forecast for station {station.upper()}.")

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["cached"] = True
    return ForecastResponse.model_validate(payload)


def save_forecast(forecast: ForecastResponse, settings: Settings) -> ForecastResponse:
    settings.forecast_cache_dir.mkdir(parents=True, exist_ok=True)
    payload = forecast.model_dump(mode="json")
    payload["cached"] = False
    with cache_path(forecast.station, settings).open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return forecast


def cache_path(station: str, settings: Settings) -> Path:
    safe_station = re.sub(r"[^A-Z0-9_-]", "_", station.upper())
    return settings.forecast_cache_dir / f"{safe_station}.json"
