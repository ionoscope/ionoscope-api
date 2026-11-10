from __future__ import annotations

import httpx
import sqlite3
from datetime import UTC, datetime, timedelta
from .archive import read_archive
from .schemas import ForecastArchiveResponse
from .forecast_cache import ensure_aware_utc
from uuid import uuid5, NAMESPACE_URL
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from .schemas import Parameter, ExplainRequest
from .explanations import explain_stream, cached_explanation

from fastapi import FastAPI, HTTPException, Path, Query

from .clients import InsufficientObservationError, UpstreamServiceError, build_forecast, fetch_stations, ensure_station_active
from .config import Settings, get_settings
from .forecast_cache import ForecastCacheMiss, evaluate_forecast, get_forecast_status, load_forecast, save_forecast, load_forecast_by_id
from .schemas import ErrorResponse, ForecastRequest, ForecastResponse, ForecastStatusResponse, HealthResponse, StationListResponse


app = FastAPI(
    title="IonoScope API",
    version="0.2.0",
    description="Main backend API that orchestrates prepared data and ML forecasts.",
    responses={code: {"model": ErrorResponse} for code in (404, 405, 409, 422, 500, 502, 503, 504)},
)


app.add_middleware(CORSMiddleware, allow_origins=list(get_settings().cors_origins),
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    detail = exc.detail if isinstance(exc.detail, dict) else {
        "code": "not_found" if exc.status_code == 404 else "request_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"detail": {
        "code": detail.get("code", "request_error"), "message": detail.get("message", "Request failed")}}, headers=exc.headers)

@app.exception_handler(Exception)
async def internal_error(request, exc):
    return JSONResponse(status_code=500, content={"detail": {"code": "internal_error", "message": "Internal server error"}})

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    code = "invalid_station" if any("station" in e["loc"] for e in exc.errors()) else "invalid_request"
    return JSONResponse(status_code=422, content={"detail": {"code": code, "message": "Request validation failed"}})

@app.get("/api/v1/forecast/parameters", response_model=list[Parameter], operation_id="listForecastParameters")
def parameters() -> list[Parameter]:
    return [Parameter(id="foF2", label="foF2", unit="MHz", available=True),
            Parameter(id="MUF", label="МПЧ / MUF", unit="MHz", available=False),
            Parameter(id="TEC", label="ПЭС / TEC", unit="TECU", available=False)]

@app.get("/api/v1/forecast/history", response_model=ForecastArchiveResponse, operation_id="listHistoricalForecasts", tags=["forecast"])
async def forecast_history(
    station: str = Query(..., pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=32),
    start: datetime | None = None, end: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> ForecastArchiveResponse:
    end = ensure_aware_utc(end or datetime.now(UTC))
    start = ensure_aware_utc(start or end - timedelta(days=30))
    if end < start or end - start > timedelta(days=31):
        raise HTTPException(422, detail={"code": "invalid_request", "message": "Date range must be between 0 and 31 days"})
    try:
        return await read_archive(ForecastRequest(station=station).station, start, end, limit, get_settings())
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        raise HTTPException(503, detail={"code": "cache_unavailable", "message": "Forecast archive is unavailable"}) from exc


@app.post("/api/v1/explain", response_class=StreamingResponse, operation_id="explainForecast",
          responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"},
                      "example": 'event: delta\ndata: {"text":"Explanation"}\n\n'}}, "description": "SSE events: start, delta, done (text, model), error (code, message)"}})
async def explain(request: ExplainRequest):
    settings = get_settings()
    try:
        result = load_forecast_by_id(request.forecast_id, settings)
        if result.forecast_id != request.forecast_id or not evaluate_forecast(result, settings).is_usable:
            raise ForecastCacheMiss()
    except ForecastCacheMiss:
        raise HTTPException(409, detail={"code": "forecast_expired", "message": "Forecast has expired; request a new forecast"})
    return StreamingResponse(explain_stream(with_freshness(result, evaluate_forecast(result, settings)), request.role, settings), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


def for_role(result: ForecastResponse, request: ForecastRequest, settings: Settings) -> ForecastResponse:
    return result.model_copy(update={"role": request.role, "explanation": cached_explanation(result.forecast_id, request.role, settings)})


def check_parameter(request: ForecastRequest) -> None:
    if request.parameter != "foF2":
        raise HTTPException(409, detail={"code": "parameter_unavailable", "message": "Only foF2 is available"})


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ionoscope-api")


@app.get("/api/v1/forecast/stations", response_model=StationListResponse, tags=["forecast"])
async def forecast_stations() -> StationListResponse:
    try:
        settings = get_settings()
        result = await fetch_stations(settings)
        for station in result.stations:
            status = get_forecast_status(station.code, settings)
            station.name = station.location or station.code
            station.lat = station.latitude
            station.lon = station.longitude_180 if station.longitude_180 is not None else station.longitude
            station.active = station.is_active is True
            station.forecast_available = station.active and (station.data_available or status.is_usable)
            station.freshness_status = status.status
            station.stale_after, station.valid_until = status.stale_after, status.valid_until
        return result
    except (UpstreamServiceError, httpx.HTTPError) as exc:
        raise upstream_http_error(exc) from exc


@app.post("/api/v1/forecast", response_model=ForecastResponse, tags=["forecast"])
async def forecast(request: ForecastRequest) -> ForecastResponse:
    check_parameter(request)
    settings = get_settings()
    try:
        await ensure_station_active(request.station, settings)
    except (UpstreamServiceError, httpx.HTTPError) as exc:
        raise upstream_http_error(exc) from exc
    try:
        cached = load_forecast(request.station, settings)
        status = evaluate_forecast(cached, settings)
        if status.is_usable and cached.forecast_id and cached.series.forecast:
            return for_role(with_freshness(cached, status), request, settings)
    except ForecastCacheMiss:
        pass

    return for_role(await compute_current_forecast(request.station, settings), request, settings)


@app.post("/api/v1/forecast/refresh", response_model=ForecastResponse, tags=["forecast"])
async def refresh_forecast(request: ForecastRequest) -> ForecastResponse:
    check_parameter(request)
    settings = get_settings()
    return for_role(await compute_current_forecast(request.station, settings), request, settings)


async def compute_current_forecast(station: str, settings: Settings) -> ForecastResponse:
    try:
        await ensure_station_active(station, settings)
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
    except (OSError, sqlite3.Error) as exc:
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
        "forecast_id": forecast.forecast_id or f"{forecast.station}_{uuid5(NAMESPACE_URL, forecast.station + forecast.generated_at.isoformat()).hex}",
        "stale_after": status.stale_after,
        "valid_until": status.valid_until,
        "freshness_status": status.status,
        "freshness_message": status.message,
    })


def upstream_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UpstreamServiceError):
        return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(status_code=504, detail={"code": "upstream_timeout", "message": "Upstream service timed out"})
    return HTTPException(status_code=502, detail={"code": "upstream_error", "message": "Upstream service is unavailable"})


# Stable operation IDs are used by generated frontend clients.
for route in app.routes:
    if hasattr(route, "operation_id") and not route.operation_id:
        route.operation_id = route.name


_default_openapi = app.openapi


def contract_openapi():
    schema = _default_openapi()
    # FastAPI drops None while encoding schemas, including intentional nulls in examples.
    # Restore the complete examples so frontend mocks match nullable response fields.
    for model in (ForecastResponse, ForecastStatusResponse, StationListResponse, HealthResponse, ErrorResponse,
                  ForecastRequest, ExplainRequest, Parameter):
        definition = schema.get("components", {}).get("schemas", {}).get(model.__name__)
        examples = model.model_config.get("json_schema_extra", {}).get("examples")
        if definition is not None and examples:
            definition["examples"] = examples
    return schema


app.openapi = contract_openapi
