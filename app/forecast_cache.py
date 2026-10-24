from __future__ import annotations

import json
import re
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from pydantic import ValidationError

from .config import Settings
from .schemas import ForecastResponse, ForecastStatusResponse


class ForecastCacheMiss(LookupError):
    pass


def load_forecast(station: str, settings: Settings) -> ForecastResponse:
    path = cache_path(station, settings)
    if not path.exists():
        raise ForecastCacheMiss(f"No cached forecast for station {station.upper()}.")

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["cached"] = True
        result = ForecastResponse.model_validate(payload)
        if result.station.upper() != station.upper():
            raise ForecastCacheMiss("Cached station does not match request")
        return result
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        raise ForecastCacheMiss("Cached forecast is unreadable or invalid") from exc


def save_forecast(forecast: ForecastResponse, settings: Settings) -> ForecastResponse:
    settings.forecast_cache_dir.mkdir(parents=True, exist_ok=True)
    payload = forecast.model_dump(mode="json")
    payload["cached"] = False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=settings.forecast_cache_dir,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, cache_path(forecast.station, settings))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
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

    return evaluate_forecast(forecast, settings)


def evaluate_forecast(
    forecast: ForecastResponse, settings: Settings, *, now: datetime | None = None
) -> ForecastStatusResponse:
    now = ensure_aware_utc(now or datetime.now(UTC))
    generated_at = ensure_aware_utc(forecast.generated_at)
    source_timestamp = ensure_aware_utc(forecast.source_timestamp)
    forecast_timestamp = (
        ensure_aware_utc(forecast.forecast_timestamp) if forecast.forecast_timestamp else None
    )
    age_minutes = max((now - generated_at).total_seconds() / 60, 0.0)
    source_age_minutes = max((now - source_timestamp).total_seconds() / 60, 0.0)
    observation = (ensure_aware_utc(forecast.observation_timestamp)
                   if forecast.observation_timestamp else None)
    observation_age = max((now - observation).total_seconds() / 60, 0.0) if observation else 0
    effective_age = max(age_minutes, source_age_minutes, observation_age)

    interval_end = ensure_aware_utc(forecast.source_interval_end) if forecast.source_interval_end else None
    if not forecast.current_fof2_available or observation is None or interval_end is None:
        status, is_usable = "expired", False
        message = "Current foF2 observation provenance is missing; recompute the forecast."
    elif interval_end > generated_at or not source_timestamp <= observation < interval_end:
        status, is_usable = "expired", False
        message = "foF2 observation does not belong to the forecast interval."
    elif generated_at > now or source_timestamp > now or observation > generated_at:
        status, is_usable = "expired", False
        message = "Forecast has a calculation or observation timestamp in the future."
    elif forecast_timestamp is None or forecast_timestamp <= source_timestamp:
        status, is_usable = "expired", False
        message = "Forecast target time is missing or invalid."
    elif forecast_timestamp <= now:
        status, is_usable = "expired", False
        message = "Forecast target time has passed."
    elif effective_age >= settings.forecast_expire_after_minutes:
        status, is_usable = "expired", False
        message = "Forecast or source observations have expired."
    elif effective_age >= settings.forecast_stale_after_minutes:
        status, is_usable = "stale", True
        message = "Forecast or source observations are stale; show a freshness warning."
    else:
        status, is_usable = "fresh", True
        message = "Forecast and source observations are fresh."

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
        source_age_minutes=round(source_age_minutes, 3),
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
