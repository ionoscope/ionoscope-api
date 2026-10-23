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
$env:IONOSCOPE_FORECAST_STALE_AFTER_MINUTES = "30"
$env:IONOSCOPE_FORECAST_EXPIRE_AFTER_MINUTES = "120"
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
- `GET /api/v1/forecast/status/{station}`
- `POST /api/v1/forecast`
- `POST /api/v1/forecast/refresh`

`POST /api/v1/forecast` is the user-facing endpoint. It returns a cached
forecast only while it remains usable. Missing or expired forecasts are
recomputed through the data and ML services. Expired cache is never returned
as a fallback when those services fail.

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

`GET /api/v1/forecast/status/{station}` returns cache freshness without
recomputing the forecast. Status values are:

- `missing` - no cached forecast exists;
- `fresh` - cached forecast is within the freshness window;
- `stale` - cached forecast can be shown with a warning;
- `expired` - cached forecast should not be shown as current.


## Forecast freshness

Freshness uses both calculation age (`generated_at`) and observation age
(`source_timestamp`), with the configured 30/120 minute defaults. Reaching a
threshold changes the status to stale/expired. A forecast is also expired as
soon as its target time is reached, regardless of calculation age. Missing or
invalid target times and future observation/calculation timestamps are unusable.
Naive timestamps are interpreted as UTC.

Both forecast endpoints validate newly calculated results before saving them.
If upstream data cannot produce a usable forecast, the API returns HTTP 503 with
`detail.code = "forecast_unavailable"`, without overwriting the existing cache.
Upstream HTTP and connection errors return HTTP 502.

Successful responses include `freshness_status` and `freshness_message`.
A usable stale forecast is returned with an explicit warning. The status endpoint
also reports `source_age_minutes`; `age_minutes` remains the calculation age.
Historical prepared data must be updated before it can produce a current forecast.


Forecast creation also requires current finite `foF2` and verified
`observation_timestamp` from Data API. Missing measurements or provenance return
HTTP 503 with `detail.code = "insufficient_observations"` before ML is called;
cache is not overwritten. `foF2_state` alone is not a substitute.
Forecast responses retain `source_timestamp` as the feature-grid label and add
`observation_timestamp` plus `current_fof2_available`. Freshness checks require
verified observation provenance; legacy cache without it must be recomputed.
The observation must belong to the forecast interval and cannot be later than
calculation time. Forecast target time is still grid time plus model horizon.
