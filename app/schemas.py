from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal
from .examples import FORECAST_EXAMPLE
from pydantic import AfterValidator

from pydantic import BaseModel, Field, field_validator, ConfigDict


class APIModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

UTCDateTime = Annotated[datetime, AfterValidator(utc)]

class Role(str, Enum):
    drone_pilot = "drone_pilot"
    geodesist = "geodesist"
    aviation_emergency = "aviation_emergency"

class Parameter(APIModel):
    model_config = {"json_schema_extra": {"examples": [{"id": "foF2", "label": "foF2", "unit": "MHz", "available": True}]}}
    id: str
    label: str
    unit: str
    available: bool


class ArchivedForecast(APIModel):
    forecast_id: str
    station: str
    parameter: str = "foF2"
    unit: str = "MHz"
    prediction: float = Field(allow_inf_nan=False)
    generated_at: UTCDateTime
    forecast_interval_start: UTCDateTime | None
    forecast_timestamp: UTCDateTime | None
    model: str
    model_version: str
    source: Literal["api", "shadow_original"] = "api"
    actual: float | None = Field(default=None, allow_inf_nan=False)
    actual_observation_timestamp: UTCDateTime | None = None
    signed_error: float | None = None
    absolute_error: float | None = None
    percentage_error: float | None = None
    verification_status: Literal["verified", "pending", "observation_missing", "observations_unavailable", "unverifiable"] = "pending"


class ForecastArchiveResponse(APIModel):
    station: str
    start: UTCDateTime
    end: UTCDateTime
    items: list[ArchivedForecast]
    total: int
    limit: int
    verified_count: int
    mae: float | None
    rmse: float | None
    unit: str = "MHz"
    observations_available: bool = True

class ObservedPoint(APIModel):
    t: UTCDateTime
    value: float | None = Field(default=None, allow_inf_nan=False)

class ForecastPoint(APIModel):
    t_start: UTCDateTime
    t_end: UTCDateTime
    value: float = Field(allow_inf_nan=False)
    lower: float | None = None
    upper: float | None = None

class Series(APIModel):
    step_minutes: Literal[15] = 15
    observed: list[ObservedPoint] = Field(default_factory=list)
    forecast: list[ForecastPoint] = Field(default_factory=list)

class Column(APIModel):
    key: str
    label: str
    unit: str | None = None

class TableRow(APIModel):
    time: UTCDateTime
    kind: Literal["observed", "forecast"]
    foF2: float | None = Field(default=None, allow_inf_nan=False)

class Table(APIModel):
    columns: list[Column] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)

class ExplainRequest(APIModel):
    model_config = {"extra": "forbid", "json_schema_extra": {"examples": [{"forecast_id": "TR169_0123456789abcdef0123456789abcdef", "role": "geodesist"}]}}
    forecast_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9_-]+_[0-9a-f]{32}$")
    role: Role = Role.geodesist

class HealthResponse(APIModel):
    model_config = {"json_schema_extra": {"examples": [{"status": "ok", "service": "ionoscope-api"}]}}
    status: str
    service: str


class ErrorDetail(APIModel):
    code: Literal["invalid_station", "station_not_found", "parameter_unavailable", "forecast_unavailable",
                  "insufficient_observations", "forecast_expired", "explanation_unavailable", "upstream_error",
                  "upstream_timeout", "invalid_request", "station_inactive", "cache_unavailable", "not_found",
                  "request_error", "internal_error", "rate_limited"]
    message: str


class ErrorResponse(APIModel):
    model_config = {"json_schema_extra": {"examples": [{"detail": {"code": "forecast_unavailable", "message": "Forecast is unavailable"}}]}}
    detail: ErrorDetail


class ForecastRequest(APIModel):
    role: Role = Role.geodesist
    parameter: str = "foF2"
    model_config = {"json_schema_extra": {"examples": [{"station": "TR169", "parameter": "foF2", "role": "geodesist"}]}}
    station: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Z0-9_-]+$")

    @field_validator("station", mode="before")
    @classmethod
    def normalize_station(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class ForecastResponse(APIModel):
    model_config = {"json_schema_extra": {"examples": [FORECAST_EXAMPLE]}}
    forecast_id: str = ""
    role: Role = Role.geodesist
    series: Series = Field(default_factory=Series)
    table: Table = Field(default_factory=Table)
    stale_after: UTCDateTime | None = None
    valid_until: UTCDateTime | None = None
    station: str
    source_timestamp: UTCDateTime
    source_interval_end: UTCDateTime | None = None
    forecast_interval_start: UTCDateTime | None = None
    observation_timestamp: UTCDateTime | None = None
    current_fof2_available: bool = False
    forecast_timestamp: UTCDateTime | None = None
    generated_at: UTCDateTime
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


class ForecastStatusResponse(APIModel):
    stale_after: UTCDateTime | None = None
    valid_until: UTCDateTime | None = None
    station: str
    status: str
    cache_available: bool
    is_usable: bool
    message: str
    source_timestamp: UTCDateTime | None = None
    forecast_timestamp: UTCDateTime | None = None
    generated_at: UTCDateTime | None = None
    age_minutes: float | None = None
    source_age_minutes: float | None = None
    stale_after_minutes: float
    expire_after_minutes: float


class Station(APIModel):
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    active: bool = False
    freshness_status: str = "missing"
    stale_after: UTCDateTime | None = None
    valid_until: UTCDateTime | None = None
    code: str
    country: str | None = None
    location: str | None = None
    system_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    longitude_180: float | None = None
    data_available: bool = False
    forecast_available: bool = False
    is_active: bool | None = None
    activity_reason: str | None = None
    last_observation: UTCDateTime | None = None


class StationListResponse(APIModel):
    model_config = {"json_schema_extra": {"examples": [{"stations": [{"code": "TR169", "name": "Tromso", "lat": 69.6, "lon": 19.2, "active": True, "freshness_status": "missing"}]}]}}
    stations: list[Station]


# Schema examples include defaults exactly as public responses serialize them.
StationListResponse.model_config["json_schema_extra"]["examples"] = [
    StationListResponse.model_validate(example).model_dump(mode="json")
    for example in StationListResponse.model_config["json_schema_extra"]["examples"]
]
ForecastStatusResponse.model_config = {
    **ForecastStatusResponse.model_config,
    "json_schema_extra": {"examples": [ForecastStatusResponse(
        station="TR169", status="missing", cache_available=False, is_usable=False,
        message="No cached forecast is available.", stale_after_minutes=30,
        expire_after_minutes=120).model_dump(mode="json")]},
}
