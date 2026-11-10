from __future__ import annotations

import math
from urllib.parse import urlencode
from uuid import uuid4
import re

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings
from .schemas import ForecastResponse, StationListResponse, Series, ObservedPoint, ForecastPoint, Table, Column, TableRow


class InsufficientObservationError(RuntimeError):
    pass


class UpstreamServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.status_code, self.code = status_code, code


async def ensure_station_active(station: str, settings: Settings) -> None:
    if settings.require_active_stations:
        payload = await get_json(settings.data_api_url, f'/api/v1/stations/{station}', settings)
        if payload.get('is_active') is not True:
            raise UpstreamServiceError('Station has no recent verified foF2 observations', 503, 'station_inactive')


async def fetch_stations(settings: Settings) -> StationListResponse:
    payload = await get_json(settings.data_api_url, "/api/v1/stations", settings)
    try:
        response = StationListResponse.model_validate(payload)
        response.stations = [station for station in response.stations
                             if (station.is_active is True if settings.require_active_stations
                                 else station.is_active is not False)]
        return response
    except ValidationError as exc:
        raise UpstreamServiceError("Data API returned an invalid station list") from exc


async def build_forecast(station: str, settings: Settings) -> ForecastResponse:
    try:
        return await _build_forecast(station, settings)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise UpstreamServiceError("Upstream service returned an invalid forecast payload") from exc


def utc_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


async def _build_forecast(station: str, settings: Settings) -> ForecastResponse:
    feature_payload = await get_json(settings.data_api_url, f"/api/v1/features/{station}/latest", settings)
    features = feature_payload.get("features", {})
    value = features.get("foF2")
    observation = feature_payload.get("observation_timestamp")
    if (feature_payload.get("current_fof2_available") is not True
            or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not observation):
        raise InsufficientObservationError(
            "Current foF2 with a verified observation timestamp is required. "
            f"Latest valid observation: {feature_payload.get('latest_fof2_timestamp') or 'unknown'}."
        )
    source_timestamp = utc_timestamp(feature_payload["timestamp"])
    interval_end = utc_timestamp(feature_payload["source_interval_end"])
    observation_timestamp = utc_timestamp(observation)
    if str(feature_payload["station"]).upper() != station.upper():
        raise UpstreamServiceError("Data API returned a different station")
    now = datetime.now(UTC)
    if (interval_end != source_timestamp + timedelta(minutes=15) or interval_end > now
            or not source_timestamp <= observation_timestamp < interval_end):
        raise InsufficientObservationError("A completed 15-minute interval with valid observation provenance is required")
    prediction_payload = await post_json(
        settings.ml_service_url,
        "/api/v1/predict",
        {"station": feature_payload["station"], "features": feature_payload.get("features", {})},
        settings,
    )

    if str(prediction_payload["station"]).upper() != station.upper():
        raise UpstreamServiceError("ML service returned a different station")
    if prediction_payload["parameter"] != "foF2" or prediction_payload["unit"] != "MHz":
        raise UpstreamServiceError("ML service returned an unexpected parameter or unit")
    horizon = str(prediction_payload["horizon"])
    horizon_delta = parse_horizon(horizon)
    result = ForecastResponse(
        forecast_id=f"{station}_{uuid4().hex}",
        station=str(prediction_payload["station"]),
        source_timestamp=source_timestamp,
        source_interval_end=interval_end,
        forecast_interval_start=source_timestamp + horizon_delta,
        observation_timestamp=observation_timestamp,
        current_fof2_available=True,
        forecast_timestamp=interval_end + horizon_delta,
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

    await add_presentation(result, settings, feature_payload)
    return result


async def add_presentation(result: ForecastResponse, settings: Settings, latest: dict) -> None:
    end = result.source_timestamp.replace(minute=(result.source_timestamp.minute // 15)*15, second=0, microsecond=0)
    start = end - timedelta(hours=settings.history_hours)
    query = urlencode({"start": start.isoformat(), "end": end.isoformat(), "allow_partial": "true"})
    payload = await get_json(settings.data_api_url, f"/api/v1/features/{result.station}?{query}", settings)
    if str(payload.get("station", "")).upper() != result.station.upper() or not isinstance(payload.get("records"), list):
        raise UpstreamServiceError("Data API returned an invalid observation history")
    points = {}
    for record in payload["records"] + [latest]:
        if str(record.get("station", "")).upper() != result.station.upper():
            raise UpstreamServiceError("Data API returned a different history station")
        t = utc_timestamp(record["timestamp"])
        value = record.get("features", {}).get("foF2")
        observation = record.get("observation_timestamp")
        if (record.get("current_fof2_available") is True and observation
                and t <= utc_timestamp(observation) < t + timedelta(minutes=15)
                and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)):
            points[t.replace(minute=(t.minute // 15)*15, second=0, microsecond=0)] = value
    observed = [ObservedPoint(t=start+timedelta(minutes=15*i), value=points.get(start+timedelta(minutes=15*i)))
                for i in range(settings.history_hours*4+1)]
    result.series = Series(observed=observed, forecast=[ForecastPoint(
        t_start=result.forecast_interval_start, t_end=result.forecast_timestamp, value=result.prediction)])
    result.table = Table(columns=[Column(key="time", label="Время (UTC)"), Column(key="kind", label="Тип"),
                                 Column(key="foF2", label="foF2 (MHz)", unit="MHz")],
                         rows=[TableRow(time=p.t, kind="observed", foF2=p.value) for p in observed]
                         + [TableRow(time=result.forecast_timestamp, kind="forecast", foF2=result.prediction)])


async def get_json(base_url: str, path: str, settings: Settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.get(f"{base_url}{path}")
    if base_url == settings.data_api_url and response.status_code == 404:
        try:
            error_payload = response.json()
            detail = error_payload.get("detail", {}) if isinstance(error_payload, dict) else {}
        except ValueError:
            detail = {}
        if isinstance(detail, dict) and detail.get("code") == "station_not_found":
            raise UpstreamServiceError("Station was not found", 404, "station_not_found")
        raise UpstreamServiceError("Prepared data is unavailable for this station", 503, "forecast_unavailable")
    return parse_response(response)


async def post_json(base_url: str, path: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.post(f"{base_url}{path}", json=payload)
    return parse_response(response)


def parse_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise UpstreamServiceError(f"Upstream service returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamServiceError("Upstream service returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UpstreamServiceError(f"{response.request.url} returned a non-object response.")
    return payload


def parse_horizon(value: str) -> timedelta:
    normalized = value.strip().lower()
    match = re.fullmatch(r"([1-9][0-9]*)(min|h)", normalized)
    if not match:
        raise ValueError("Unsupported forecast horizon")
    delta = timedelta(minutes=int(match[1])) if match[2] == "min" else timedelta(hours=int(match[1]))
    if delta < timedelta(minutes=15) or delta % timedelta(minutes=15):
        raise ValueError("Horizon must be a multiple of 15 minutes")
    return delta
