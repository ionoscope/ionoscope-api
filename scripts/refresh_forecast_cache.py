from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class RefreshResult:
    station: str
    status: str
    message: str


def main() -> int:
    args = parse_args()
    api_url = args.api_url.rstrip("/")

    try:
        stations = load_stations(api_url, args.timeout)
    except httpx.HTTPError as exc:
        print(f"failed to load station list: {exc}", file=sys.stderr)
        return 1

    selected = select_stations(stations, args.stations, args.only_available)
    if not selected:
        print("no stations selected for forecast refresh")
        return 0

    results = [refresh_station(api_url, station, args.timeout) for station in selected]
    for result in results:
        print(f"{result.station}: {result.status} - {result.message}")

    failed = [result for result in results if result.status == "failed"]
    print(f"summary: refreshed={len(results) - len(failed)} failed={len(failed)} total={len(results)}")
    return 1 if failed and args.fail_on_error else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh cached forecasts through ionoscope-api.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="Base URL of ionoscope-api.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--stations",
        default="",
        help="Comma-separated station codes. Empty value means all stations from the API.",
    )
    parser.add_argument(
        "--all-stations",
        action="store_true",
        help="Refresh every station returned by the API, including stations without prepared data.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with code 1 when at least one station refresh fails.",
    )
    args = parser.parse_args()
    args.only_available = not args.all_stations
    return args


def load_stations(api_url: str, timeout: float) -> list[dict]:
    response = httpx.get(f"{api_url}/api/v1/forecast/stations", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("stations", []))


def select_stations(stations: list[dict], station_filter: str, only_available: bool) -> list[str]:
    requested = {item.strip().upper() for item in station_filter.split(",") if item.strip()}
    selected: list[str] = []
    for station in stations:
        code = str(station.get("code", "")).upper()
        if not code:
            continue
        if requested and code not in requested:
            continue
        if only_available and not station.get("data_available", False):
            continue
        selected.append(code)
    return selected


def refresh_station(api_url: str, station: str, timeout: float) -> RefreshResult:
    try:
        response = httpx.post(
            f"{api_url}/api/v1/forecast/refresh",
            json={"station": station},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        forecast_for = payload.get("forecast_timestamp", "unknown forecast time")
        prediction = payload.get("prediction", "unknown prediction")
        unit = payload.get("unit", "")
        return RefreshResult(station=station, status="ok", message=f"{prediction} {unit} for {forecast_for}")
    except httpx.HTTPStatusError as exc:
        return RefreshResult(
            station=station,
            status="failed",
            message=f"HTTP {exc.response.status_code}: {exc.response.text}",
        )
    except httpx.HTTPError as exc:
        return RefreshResult(station=station, status="failed", message=str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
