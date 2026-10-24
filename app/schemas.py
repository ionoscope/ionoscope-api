from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str
    service: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    source_timestamp: datetime | None = None
    forecast_timestamp: datetime | None = None


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class ForecastRequest(BaseModel):
    station: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Z0-9_-]+$")

    @field_validator("station", mode="before")
    @classmethod
    def normalize_station(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class ForecastResponse(BaseModel):
    station: str
    source_timestamp: datetime
    source_interval_end: datetime | None = None
    forecast_interval_start: datetime | None = None
    observation_timestamp: datetime | None = None
    current_fof2_available: bool = False
    forecast_timestamp: datetime | None = None
    generated_at: datetime
    parameter: str
    prediction: float = Field(allow_inf_nan=False)
    unit: str
    horizon: str
    model: str
    model_version: str
    cached: bool = False
    explanation: str | None = None
    freshness_status: str = "fresh"
    freshness_message: str | None = None


class ForecastStatusResponse(BaseModel):
    station: str
    status: str
    cache_available: bool
    is_usable: bool
    message: str
    source_timestamp: datetime | None = None
    forecast_timestamp: datetime | None = None
    generated_at: datetime | None = None
    age_minutes: float | None = None
    source_age_minutes: float | None = None
    stale_after_minutes: float
    expire_after_minutes: float


class Station(BaseModel):
    code: str
    country: str | None = None
    location: str | None = None
    system_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    longitude_180: float | None = None
    data_available: bool = False
    is_active: bool | None = None
    activity_reason: str | None = None
    last_observation: datetime | None = None


class StationListResponse(BaseModel):
    stations: list[Station]
