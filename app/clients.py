from __future__ import annotations

import math

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Settings
from .schemas import ForecastResponse, StationListResponse


class InsufficientObservationError(RuntimeError):
    pass


class UpstreamServiceError(RuntimeError):
    pass


async def fetch_stations(settings: Settings) -> StationListResponse:
    payload = await get_json(settings.data_api_url, "/api/v1/stations", settings)
    return StationListResponse.model_validate(payload)


async def build_forecast(station: str, settings: Settings) -> ForecastResponse:
    feature_payload = await get_json(settings.data_api_url, f"/api/v1/features/{station}/latest", settings)
    features = feature_payload.get("features", {})
    value = features.get("foF2")
    observation = feature_payload.get("observation_timestamp")
    if (feature_payload.get("current_fof2_available") is not True
            or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not observation):
        raise InsufficientObservationError(
            "Current foF2 with a verified observation timestamp is required. "
            f"Latest valid observation: {feature_payload.get('latest_fof2_timestamp') or 'unknown'}."
        )
    observation_timestamp = datetime.fromisoformat(observation.replace("Z", "+00:00"))
    prediction_payload = await post_json(
        settings.ml_service_url,
        "/api/v1/predict",
        {"station": feature_payload["station"], "features": feature_payload.get("features", {})},
        settings,
    )

    source_timestamp = datetime.fromisoformat(feature_payload["timestamp"].replace("Z", "+00:00"))
    horizon = str(prediction_payload["horizon"])
    horizon_delta = parse_horizon(horizon)
    return ForecastResponse(
        station=str(prediction_payload["station"]),
        source_timestamp=source_timestamp,
        observation_timestamp=observation_timestamp,
        current_fof2_available=True,
        forecast_timestamp=source_timestamp + horizon_delta if horizon_delta is not None else None,
        generated_at=datetime.now(UTC),
        parameter=str(prediction_payload["parameter"]),
        prediction=float(prediction_payload["prediction"]),
        unit=str(prediction_payload["unit"]),
        horizon=horizon,
        model=str(prediction_payload["model"]),
        model_version=str(prediction_payload["model_version"]),
        cached=False,
        explanation=None,
    )


async def get_json(base_url: str, path: str, settings: Settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.get(f"{base_url}{path}")
    return parse_response(response)


async def post_json(base_url: str, path: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.post(f"{base_url}{path}", json=payload)
    return parse_response(response)


def parse_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise UpstreamServiceError(f"{response.request.url} returned {response.status_code}: {response.text}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise UpstreamServiceError(f"{response.request.url} returned a non-object response.")
    return payload


def parse_horizon(value: str) -> timedelta | None:
    normalized = value.strip().lower()
    if normalized.endswith("min"):
        return timedelta(minutes=int(normalized[:-3]))
    if normalized.endswith("h"):
        return timedelta(hours=int(normalized[:-1]))
    return None
