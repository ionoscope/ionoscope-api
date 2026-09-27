# IonoScope API

Main backend API for the IonoScope frontend.

Version `0.1.0` implements the first forecast orchestration path:

- accept a station selected by the user;
- call `ionoscope-data` for the latest prepared features;
- call `ionoscope-ml-service` for a fast `foF2` forecast;
- cache the generated forecast;
- return one frontend-ready response.

LLM explanations are not connected yet. The response already keeps an
`explanation` field so a separate explanation service can be added later without
changing the forecast contract.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Configuration

The API reads upstream service URLs from environment variables:

```powershell
$env:IONOSCOPE_DATA_API_URL = "http://127.0.0.1:8001"
$env:IONOSCOPE_ML_SERVICE_URL = "http://127.0.0.1:8002"
$env:IONOSCOPE_FORECAST_CACHE_DIR = ".\forecast_cache"
```

Defaults use the same local URLs.

## Run

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Open API docs:

```text
http://127.0.0.1:8000/docs
```

## Endpoints

- `GET /health`
- `GET /api/v1/forecast/stations`
- `POST /api/v1/forecast`
- `POST /api/v1/forecast/refresh`

`POST /api/v1/forecast` is the user-facing endpoint. It returns a cached
forecast when one exists, and only computes a new forecast if the cache is
empty.

`POST /api/v1/forecast/refresh` is the worker endpoint. A scheduler can call it
after prepared data updates, for example every 15 minutes, so users receive a
ready forecast without waiting for data and ML calls.

Forecast request:

```json
{
  "station": "TR169"
}
```

Forecast response:

```json
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
  "cached": true,
  "explanation": null
}
```

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Refresh Forecast Cache

Run a one-off refresh for all stations with prepared data:

```powershell
python .\scripts\refresh_forecast_cache.py --api-url http://127.0.0.1:8000
```

Refresh selected stations:

```powershell
python .\scripts\refresh_forecast_cache.py --api-url http://127.0.0.1:8000 --stations TR169
```

On a server this command should be scheduled after the prepared data update
step, for example every 15 minutes. The user-facing `/api/v1/forecast` endpoint
then returns the already cached forecast.
