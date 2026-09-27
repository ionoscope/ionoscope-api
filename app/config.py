from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_api_url: str = "http://127.0.0.1:8001"
    ml_service_url: str = "http://127.0.0.1:8002"
    request_timeout_seconds: float = 10.0
    forecast_cache_dir: Path = PROJECT_ROOT / "forecast_cache"
    forecast_stale_after_minutes: float = 30.0
    forecast_expire_after_minutes: float = 120.0


def get_settings() -> Settings:
    return Settings(
        data_api_url=os.getenv("IONOSCOPE_DATA_API_URL", Settings.data_api_url).rstrip("/"),
        ml_service_url=os.getenv("IONOSCOPE_ML_SERVICE_URL", Settings.ml_service_url).rstrip("/"),
        request_timeout_seconds=float(
            os.getenv("IONOSCOPE_REQUEST_TIMEOUT_SECONDS", str(Settings.request_timeout_seconds))
        ),
        forecast_cache_dir=Path(os.getenv("IONOSCOPE_FORECAST_CACHE_DIR", str(Settings.forecast_cache_dir))),
        forecast_stale_after_minutes=float(
            os.getenv("IONOSCOPE_FORECAST_STALE_AFTER_MINUTES", str(Settings.forecast_stale_after_minutes))
        ),
        forecast_expire_after_minutes=float(
            os.getenv("IONOSCOPE_FORECAST_EXPIRE_AFTER_MINUTES", str(Settings.forecast_expire_after_minutes))
        ),
    )
