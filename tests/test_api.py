from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app import clients
from app.config import Settings
from app.forecast_cache import load_forecast
from app.main import app


client = TestClient(app)


def isolated_settings(tmp_path) -> Settings:
    return Settings(
        data_api_url="http://data-api.local",
        ml_service_url="http://ml-service.local",
        request_timeout_seconds=1.0,
        forecast_cache_dir=tmp_path,
    )


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ionoscope-api"}


def test_refresh_forecast_combines_data_ml_and_writes_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))

    async def fake_get_json(base_url, path, settings):
        assert path == "/api/v1/features/TR169/latest"
        return {
            "station": "TR169",
            "timestamp": "2026-09-27T00:00:00Z",
            "features": {"foF2": 5.84, "gfz_Kp": 3.0},
        }

    async def fake_post_json(base_url, path, payload, settings):
        assert path == "/api/v1/predict"
        assert payload == {"station": "TR169", "features": {"foF2": 5.84, "gfz_Kp": 3.0}}
        return {
            "station": "TR169",
            "parameter": "foF2",
            "prediction": 6.378,
            "unit": "MHz",
            "horizon": "15min",
            "model": "HistGradientBoostingRegressor",
            "model_version": "0.1.0",
        }

    monkeypatch.setattr(clients, "get_json", fake_get_json)
    monkeypatch.setattr(clients, "post_json", fake_post_json)

    response = client.post("/api/v1/forecast/refresh", json={"station": "TR169"})

    assert response.status_code == 200
    payload = response.json()
    generated_at = payload.pop("generated_at")
    assert generated_at.startswith("202")
    assert payload == {
        "station": "TR169",
        "source_timestamp": "2026-09-27T00:00:00Z",
        "forecast_timestamp": "2026-09-27T00:15:00Z",
        "parameter": "foF2",
        "prediction": 6.378,
        "unit": "MHz",
        "horizon": "15min",
        "model": "HistGradientBoostingRegressor",
        "model_version": "0.1.0",
        "cached": False,
        "explanation": None,
    }
    cached = load_forecast("TR169", isolated_settings(tmp_path))
    assert cached.cached is True
    assert cached.prediction == 6.378


def test_forecast_returns_cache_without_upstream_calls(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))

    def fail_upstream(*args, **kwargs):
        raise AssertionError("forecast endpoint should use cache when it exists")

    monkeypatch.setattr(clients, "get_json", fail_upstream)
    monkeypatch.setattr(clients, "post_json", fail_upstream)
    (tmp_path / "TR169.json").write_text(
        """
        {
          "station": "TR169",
          "source_timestamp": "2026-09-27T00:00:00Z",
          "forecast_timestamp": "2026-09-27T00:15:00Z",
          "generated_at": "2026-09-27T00:01:00Z",
          "parameter": "foF2",
          "prediction": 6.378,
          "unit": "MHz",
          "horizon": "15min",
          "model": "HistGradientBoostingRegressor",
          "model_version": "0.1.0",
          "cached": false,
          "explanation": null
        }
        """,
        encoding="utf-8",
    )

    response = client.post("/api/v1/forecast", json={"station": "TR169"})

    assert response.status_code == 200
    assert response.json()["cached"] is True


def test_upstream_error_maps_to_bad_gateway(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))

    async def fake_get_json(base_url, path, settings):
        request = httpx.Request("GET", "http://data-api.local/missing")
        response = httpx.Response(404, request=request, text="not found")
        raise clients.UpstreamServiceError(clients.parse_response(response))

    monkeypatch.setattr(clients, "get_json", fake_get_json)

    response = client.post("/api/v1/forecast", json={"station": "MISSING"})

    assert response.status_code == 502
