from __future__ import annotations

import httpx

from scripts.refresh_forecast_cache import refresh_station, select_stations


def test_select_stations_uses_available_stations_by_default() -> None:
    stations = [
        {"code": "TR169", "data_available": True},
        {"code": "AA343", "data_available": False},
    ]

    assert select_stations(stations, station_filter="", only_available=True) == ["TR169"]


def test_select_stations_can_use_explicit_filter() -> None:
    stations = [
        {"code": "TR169", "data_available": True},
        {"code": "AA343", "data_available": False},
    ]

    assert select_stations(stations, station_filter="aa343", only_available=False) == ["AA343"]


def test_refresh_station_reports_success(monkeypatch) -> None:
    def fake_post(url, json, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"prediction": 5.4, "unit": "MHz", "forecast_timestamp": "2026-09-27T00:15:00Z"},
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = refresh_station("http://api.local", "TR169", timeout=1.0)

    assert result.station == "TR169"
    assert result.status == "ok"
    assert result.message == "5.4 MHz for 2026-09-27T00:15:00Z"


def test_refresh_station_reports_http_failure(monkeypatch) -> None:
    def fake_post(url, json, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(502, request=request, text="bad gateway")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = refresh_station("http://api.local", "TR169", timeout=1.0)

    assert result.station == "TR169"
    assert result.status == "failed"
    assert result.message == "HTTP 502: bad gateway"
