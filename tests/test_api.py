from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app import clients
from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ionoscope-api"}


def test_forecast_combines_data_and_ml(monkeypatch) -> None:
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

    response = client.post("/api/v1/forecast", json={"station": "TR169"})

    assert response.status_code == 200
    assert response.json() == {
        "station": "TR169",
        "source_timestamp": "2026-09-27T00:00:00Z",
        "forecast_timestamp": "2026-09-27T00:15:00Z",
        "parameter": "foF2",
        "prediction": 6.378,
        "unit": "MHz",
        "horizon": "15min",
        "model": "HistGradientBoostingRegressor",
        "model_version": "0.1.0",
        "explanation": None,
    }


def test_upstream_error_maps_to_bad_gateway(monkeypatch) -> None:
    async def fake_get_json(base_url, path, settings):
        request = httpx.Request("GET", "http://data-api.local/missing")
        response = httpx.Response(404, request=request, text="not found")
        raise clients.UpstreamServiceError(clients.parse_response(response))

    monkeypatch.setattr(clients, "get_json", fake_get_json)

    response = client.post("/api/v1/forecast", json={"station": "MISSING"})

    assert response.status_code == 502
