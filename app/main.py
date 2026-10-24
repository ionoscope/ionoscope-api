from __future__ import annotations

import httpx

from fastapi import FastAPI, HTTPException, Path

from .clients import InsufficientObservationError, UpstreamServiceError, build_forecast, fetch_stations, ensure_station_active
from .config import Settings, get_settings
from .forecast_cache import ForecastCacheMiss, evaluate_forecast, get_forecast_status, load_forecast, save_forecast
from .schemas import ErrorResponse, ForecastRequest, ForecastResponse, ForecastStatusResponse, HealthResponse, StationListResponse


app = FastAPI(
    title="IonoScope API",
    version="0.1.0",
    description="Main backend API that orchestrates prepared data and ML forecasts.",
    responses={code: {"model": ErrorResponse} for code in (404, 502, 503, 504)},
)


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ionoscope-api")


@app.get("/api/v1/forecast/stations", response_model=StationListResponse, tags=["forecast"])
async def forecast_stations() -> StationListResponse:
    try:
        return await fetch_stations(get_settings())
    except (UpstreamServiceError, httpx.HTTPError) as exc:
        raise upstream_http_error(exc) from exc


@app.post("/api/v1/forecast", response_model=ForecastResponse, tags=["forecast"])
async def forecast(request: ForecastRequest) -> ForecastResponse:
    settings = get_settings()
    try:
        await ensure_station_active(request.station, settings)
    except (UpstreamServiceError, httpx.HTTPError) as exc:
        raise upstream_http_error(exc) from exc
    try:
        cached = load_forecast(request.station, settings)
        status = evaluate_forecast(cached, settings)
        if status.is_usable:
            return with_freshness(cached, status)
    except ForecastCacheMiss:
        pass

    return await compute_current_forecast(request.station, settings)


@app.post("/api/v1/forecast/refresh", response_model=ForecastResponse, tags=["forecast"])
async def refresh_forecast(request: ForecastRequest) -> ForecastResponse:
    return await compute_current_forecast(request.station, get_settings())


async def compute_current_forecast(station: str, settings: Settings) -> ForecastResponse:
    try:
        fresh_forecast = await build_forecast(station, settings)
        status = evaluate_forecast(fresh_forecast, settings)
        if not status.is_usable:
            raise HTTPException(status_code=503, detail={
                "code": "forecast_unavailable", "message": status.message,
                "source_timestamp": fresh_forecast.source_timestamp.isoformat(),
                "forecast_timestamp": (fresh_forecast.forecast_timestamp.isoformat()
                                       if fresh_forecast.forecast_timestamp else None),
            })
        return save_forecast(with_freshness(fresh_forecast, status), settings)
    except InsufficientObservationError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "insufficient_observations", "message": str(exc),
        }) from exc
    except (UpstreamServiceError, httpx.HTTPError) as exc:
        raise upstream_http_error(exc) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"code": "cache_unavailable", "message": "Forecast cache cannot be written"}) from exc


@app.get("/api/v1/forecast/status/{station}", response_model=ForecastStatusResponse, tags=["forecast"])
async def forecast_status(station: str = Path(pattern=r"^[A-Za-z0-9_-]+$", max_length=32)) -> ForecastStatusResponse:
    station = ForecastRequest(station=station).station
    settings = get_settings()
    try:
        await ensure_station_active(station, settings)
    except (UpstreamServiceError, httpx.HTTPError) as exc:
        raise upstream_http_error(exc) from exc
    return get_forecast_status(station, settings)


def with_freshness(forecast: ForecastResponse, status: ForecastStatusResponse) -> ForecastResponse:
    return forecast.model_copy(update={
        "freshness_status": status.status,
        "freshness_message": status.message,
    })


def upstream_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UpstreamServiceError):
        return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(status_code=504, detail={"code": "upstream_timeout", "message": "Upstream service timed out"})
    return HTTPException(status_code=502, detail={"code": "upstream_unavailable", "message": "Upstream service is unavailable"})
