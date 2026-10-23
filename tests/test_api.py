from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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
        forecast_stale_after_minutes=30.0,
        forecast_expire_after_minutes=120.0,
    )


def write_cached_forecast(tmp_path, generated_at: datetime) -> None:
    (tmp_path / "TR169.json").write_text(
        f"""
        {{
          "station": "TR169",
          "source_timestamp": "{generated_at.isoformat()}",
          "observation_timestamp": "{generated_at.isoformat()}",
          "current_fof2_available": true,
          "forecast_timestamp": "{(generated_at + timedelta(hours=4)).isoformat()}",
          "generated_at": "{generated_at.isoformat().replace("+00:00", "Z")}",
          "parameter": "foF2",
          "prediction": 6.378,
          "unit": "MHz",
          "horizon": "4h",
          "model": "HistGradientBoostingRegressor",
          "model_version": "0.1.0",
          "cached": false,
          "explanation": null
        }}
        """,
        encoding="utf-8",
    )


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ionoscope-api"}


def test_refresh_forecast_combines_data_ml_and_writes_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))

    source_time = datetime.now(UTC) - timedelta(minutes=1)

    async def fake_get_json(base_url, path, settings):
        assert path == "/api/v1/features/TR169/latest"
        return {
            "station": "TR169",
            "timestamp": source_time.isoformat(),
            "observation_timestamp": source_time.isoformat(),
            "current_fof2_available": True,
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
    assert payload.pop("freshness_status") == "fresh"
    assert payload.pop("freshness_message")
    assert payload == {
        "station": "TR169",
        "source_timestamp": source_time.isoformat().replace("+00:00", "Z"),
        "observation_timestamp": source_time.isoformat().replace("+00:00", "Z"),
        "current_fof2_available": True,
        "forecast_timestamp": (source_time + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
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
    write_cached_forecast(tmp_path, datetime.now(UTC))

    response = client.post("/api/v1/forecast", json={"station": "TR169"})

    assert response.status_code == 200
    assert response.json()["cached"] is True


def test_forecast_status_reports_missing_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))

    response = client.get("/api/v1/forecast/status/TR169")

    assert response.status_code == 200
    assert response.json() == {
        "station": "TR169",
        "status": "missing",
        "cache_available": False,
        "is_usable": False,
        "message": "No cached forecast is available.",
        "source_timestamp": None,
        "forecast_timestamp": None,
        "generated_at": None,
        "age_minutes": None,
        "source_age_minutes": None,
        "stale_after_minutes": 30.0,
        "expire_after_minutes": 120.0,
    }


def test_forecast_status_reports_fresh_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    write_cached_forecast(tmp_path, datetime.now(UTC) - timedelta(minutes=5))

    response = client.get("/api/v1/forecast/status/TR169")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "fresh"
    assert payload["cache_available"] is True
    assert payload["is_usable"] is True
    assert payload["age_minutes"] < 30


def test_forecast_status_reports_stale_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    write_cached_forecast(tmp_path, datetime.now(UTC) - timedelta(minutes=45))

    response = client.get("/api/v1/forecast/status/TR169")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "stale"
    assert payload["cache_available"] is True
    assert payload["is_usable"] is True


def test_forecast_status_reports_expired_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    write_cached_forecast(tmp_path, datetime.now(UTC) - timedelta(minutes=180))

    response = client.get("/api/v1/forecast/status/TR169")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "expired"
    assert payload["cache_available"] is True
    assert payload["is_usable"] is False


def test_upstream_error_maps_to_bad_gateway(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))

    async def fake_get_json(base_url, path, settings):
        request = httpx.Request("GET", "http://data-api.local/missing")
        response = httpx.Response(404, request=request, text="not found")
        raise clients.UpstreamServiceError(clients.parse_response(response))

    monkeypatch.setattr(clients, "get_json", fake_get_json)

    response = client.post("/api/v1/forecast", json={"station": "MISSING"})

    assert response.status_code == 502


@pytest.mark.parametrize("case", ["expired_cache", "old_observation", "passed_target", "missing_target"])
def test_unusable_cache_is_recomputed(monkeypatch, tmp_path, case):
    import json
    from app.schemas import ForecastResponse

    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    now = datetime.now(UTC)
    write_cached_forecast(tmp_path, now)
    path = tmp_path / "TR169.json"
    payload = json.loads(path.read_text())
    if case == "expired_cache":
        payload["generated_at"] = (now - timedelta(hours=3)).isoformat()
    elif case == "old_observation":
        payload["source_timestamp"] = (now - timedelta(days=30)).isoformat()
    elif case == "passed_target":
        payload["source_timestamp"] = (now - timedelta(minutes=20)).isoformat()
        payload["forecast_timestamp"] = (now - timedelta(minutes=5)).isoformat()
    else:
        payload["forecast_timestamp"] = None
    path.write_text(json.dumps(payload))
    status = client.get("/api/v1/forecast/status/TR169").json()
    assert status["status"] == "expired"
    assert status["is_usable"] is False
    calls = []

    async def build(station, settings):
        calls.append(station)
        return ForecastResponse.model_validate({**payload,
            "observation_timestamp": now - timedelta(minutes=1),
            "source_timestamp": now - timedelta(minutes=1),
            "forecast_timestamp": now + timedelta(minutes=14), "generated_at": now})

    monkeypatch.setattr("app.main.build_forecast", build)
    response = client.post("/api/v1/forecast", json={"station": "TR169"})
    assert response.status_code == 200
    assert calls == ["TR169"]
    assert response.json()["cached"] is False
    assert response.json()["freshness_status"] == "fresh"


@pytest.mark.parametrize("endpoint", ["/api/v1/forecast", "/api/v1/forecast/refresh"])
def test_old_upstream_data_rejected_without_overwriting_cache(monkeypatch, tmp_path, endpoint):
    from app.schemas import ForecastResponse
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    write_cached_forecast(tmp_path, datetime.now(UTC) - timedelta(hours=3))
    path = tmp_path / "TR169.json"
    original = path.read_bytes()

    async def build(station, settings):
        import json
        payload = json.loads(original)
        payload["generated_at"] = datetime.now(UTC)
        return ForecastResponse.model_validate(payload)

    monkeypatch.setattr("app.main.build_forecast", build)
    response = client.post(endpoint, json={"station": "TR169"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "forecast_unavailable"
    assert path.read_bytes() == original


def test_stale_cache_has_warning_in_forecast_response(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    write_cached_forecast(tmp_path, datetime.now(UTC) - timedelta(minutes=45))
    response = client.post("/api/v1/forecast", json={"station": "TR169"})
    assert response.status_code == 200
    assert response.json()["freshness_status"] == "stale"
    assert "warning" in response.json()["freshness_message"]


def test_expired_cache_not_returned_on_network_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    write_cached_forecast(tmp_path, datetime.now(UTC) - timedelta(hours=3))

    async def build(*args):
        raise httpx.ConnectError("upstream unavailable")

    monkeypatch.setattr("app.main.build_forecast", build)
    assert client.post("/api/v1/forecast", json={"station": "TR169"}).status_code == 502


@pytest.mark.parametrize("case", ["expiry_boundary", "target_boundary", "future_source", "future_calculation", "invalid_target", "naive_utc", "source_stale"])
def test_freshness_time_rules(tmp_path, case):
    from app.forecast_cache import evaluate_forecast
    from app.schemas import ForecastResponse
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    payload = dict(station="TR169", source_timestamp=now-timedelta(minutes=1),
        forecast_timestamp=now+timedelta(hours=4), generated_at=now,
        parameter="foF2", prediction=5, unit="MHz", horizon="4h", model="test", model_version="test")
    expected = "expired"
    if case == "expiry_boundary":
        payload["source_timestamp"] = now-timedelta(minutes=120)
    elif case == "target_boundary":
        payload["forecast_timestamp"] = now
    elif case == "future_source":
        payload["source_timestamp"] = now+timedelta(minutes=1)
    elif case == "future_calculation":
        payload["generated_at"] = now+timedelta(minutes=1)
    elif case == "invalid_target":
        payload["forecast_timestamp"] = payload["source_timestamp"]
    elif case == "source_stale":
        payload["source_timestamp"] = now-timedelta(minutes=30)
        expected = "stale"
    else:
        payload = {key: value.replace(tzinfo=None) if isinstance(value, datetime) else value for key, value in payload.items()}
        expected = "fresh"
    payload["observation_timestamp"] = payload["source_timestamp"]
    payload["current_fof2_available"] = True
    result = evaluate_forecast(ForecastResponse(**payload), isolated_settings(tmp_path), now=now)
    assert result.status == expected
    assert result.is_usable == (expected != "expired")


@pytest.mark.parametrize("case", ["missing_fof2", "missing_provenance", "legacy_response", "nonfinite"])
@pytest.mark.parametrize("endpoint", ["/api/v1/forecast", "/api/v1/forecast/refresh"])
def test_missing_observation_blocks_ml_and_cache(monkeypatch, tmp_path, case, endpoint):
    monkeypatch.setattr("app.main.get_settings", lambda: isolated_settings(tmp_path))
    now = datetime.now(UTC).isoformat()
    payload = {"station": "TR169", "timestamp": now, "observation_timestamp": now,
               "current_fof2_available": True, "features": {"foF2": 4.05}}
    if case == "missing_fof2":
        payload["features"] = {"foF2_state": 4.05}
        payload["current_fof2_available"] = False
    elif case == "missing_provenance":
        payload["observation_timestamp"] = None
    elif case == "nonfinite":
        payload["features"]["foF2"] = float("nan")
    else:
        payload.pop("observation_timestamp")
        payload.pop("current_fof2_available")
    async def get(*args): return payload
    async def fail(*args): raise AssertionError("ML must not run")
    monkeypatch.setattr(clients, "get_json", get)
    monkeypatch.setattr(clients, "post_json", fail)
    response = client.post(endpoint, json={"station": "TR169"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "insufficient_observations"
    assert not (tmp_path / "TR169.json").exists()


def test_legacy_cache_requires_recalculation(tmp_path):
    import json
    from app.forecast_cache import get_forecast_status
    write_cached_forecast(tmp_path, datetime.now(UTC))
    path = tmp_path / "TR169.json"
    payload = json.loads(path.read_text())
    payload.pop("observation_timestamp")
    payload.pop("current_fof2_available")
    path.write_text(json.dumps(payload))
    assert get_forecast_status("TR169", isolated_settings(tmp_path)).is_usable is False
