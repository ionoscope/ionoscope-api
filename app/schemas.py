from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class ForecastRequest(BaseModel):
    station: str = Field(..., min_length=1)


class ForecastResponse(BaseModel):
    station: str
    source_timestamp: datetime
    forecast_timestamp: datetime | None = None
    generated_at: datetime
    parameter: str
    prediction: float
    unit: str
    horizon: str
    model: str
    model_version: str
    cached: bool = False
    explanation: str | None = None


class Station(BaseModel):
    code: str
    country: str | None = None
    location: str | None = None
    system_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    longitude_180: float | None = None
    data_available: bool = False


class StationListResponse(BaseModel):
    stations: list[Station]
