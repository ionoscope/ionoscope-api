from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    data_api_url: str = "http://127.0.0.1:8001"
    ml_service_url: str = "http://127.0.0.1:8002"
    request_timeout_seconds: float = 10.0


def get_settings() -> Settings:
    return Settings(
        data_api_url=os.getenv("IONOSCOPE_DATA_API_URL", Settings.data_api_url).rstrip("/"),
        ml_service_url=os.getenv("IONOSCOPE_ML_SERVICE_URL", Settings.ml_service_url).rstrip("/"),
        request_timeout_seconds=float(
            os.getenv("IONOSCOPE_REQUEST_TIMEOUT_SECONDS", str(Settings.request_timeout_seconds))
        ),
    )
