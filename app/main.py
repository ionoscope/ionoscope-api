from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .clients import UpstreamServiceError, build_forecast, fetch_stations
from .config import get_settings
from .schemas import ForecastRequest, ForecastResponse, HealthResponse, StationListResponse


app = FastAPI(
    title="IonoScope API",
    version="0.1.0",
    description="Main backend API that orchestrates prepared data and ML forecasts.",
)


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ionoscope-api")


@app.get("/api/v1/forecast/stations", response_model=StationListResponse, tags=["forecast"])
async def forecast_stations() -> StationListResponse:
    try:
        return await fetch_stations(get_settings())
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/forecast", response_model=ForecastResponse, tags=["forecast"])
async def forecast(request: ForecastRequest) -> ForecastResponse:
    try:
        return await build_forecast(request.station, get_settings())
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
