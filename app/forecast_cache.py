from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .schemas import ForecastResponse, ForecastStatusResponse


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


def get_forecast_status(station: str, settings: Settings) -> ForecastStatusResponse:
    try:
        forecast = load_forecast(station, settings)
    except ForecastCacheMiss:
        return ForecastStatusResponse(
            station=station.upper(),
            status="missing",
            cache_available=False,
            is_usable=False,
            message="No cached forecast is available.",
            stale_after_minutes=settings.forecast_stale_after_minutes,
            expire_after_minutes=settings.forecast_expire_after_minutes,
        )

    now = datetime.now(UTC)
    generated_at = ensure_aware_utc(forecast.generated_at)
    age_minutes = max((now - generated_at).total_seconds() / 60, 0.0)

    if age_minutes > settings.forecast_expire_after_minutes:
        status = "expired"
        is_usable = False
        message = "Cached forecast is expired and should not be shown as current."
    elif age_minutes > settings.forecast_stale_after_minutes:
        status = "stale"
        is_usable = True
        message = "Cached forecast is stale; show it with a freshness warning."
    else:
        status = "fresh"
        is_usable = True
        message = "Cached forecast is fresh."

    return ForecastStatusResponse(
        station=forecast.station,
        status=status,
        cache_available=True,
        is_usable=is_usable,
        message=message,
        source_timestamp=forecast.source_timestamp,
        forecast_timestamp=forecast.forecast_timestamp,
        generated_at=forecast.generated_at,
        age_minutes=round(age_minutes, 3),
        stale_after_minutes=settings.forecast_stale_after_minutes,
        expire_after_minutes=settings.forecast_expire_after_minutes,
    )


def cache_path(station: str, settings: Settings) -> Path:
    safe_station = re.sub(r"[^A-Z0-9_-]", "_", station.upper())
    return settings.forecast_cache_dir / f"{safe_station}.json"


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
