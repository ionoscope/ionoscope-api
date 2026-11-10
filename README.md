# IonoScope API

Main backend API for the IonoScope frontend.

Version `0.2.0` implements the first forecast orchestration path:

- accept a station selected by the user;
- call `ionoscope-data` for the latest prepared features;
- call `ionoscope-ml-service` for a fast `foF2` forecast;
- cache the generated forecast;
- return one frontend-ready response.

The frontend contract includes a 15-minute history grid (missing observations are null),
forecast points, CSV-ready table rows, roles and opaque forecast IDs. API version and
ML model version are independent: model_version reports the actual loaded model.

POST /api/v1/explain accepts only forecast_id and role. SSE emits start, delta,
done (text, model), or error (code, message). Explanations use a separate persistent
cache by (forecast_id, role). Refreshing a station does not discard still-valid IDs.
Expired IDs return HTTP 409 forecast_expired before opening the stream.
Without a configured LLM, the stream emits explanation_unavailable; forecasts still work.
The configured provider must implement an OpenAI-compatible streaming chat completions
endpoint (including the final [DONE] marker). No provider calls happen in /forecast.
Only the computed summary is sent to the provider, never raw features or observations.

Additional environment variables:

- IONOSCOPE_HISTORY_HOURS: 24 by default, 1–168 via environment configuration.
- IONOSCOPE_CORS_ORIGINS: comma-separated origins, default http://localhost:4200.
- IONOSCOPE_LLM_URL: full streaming chat completions URL (unset by default).
- IONOSCOPE_LLM_MODEL: provider model identifier.
- IONOSCOPE_LLM_API_KEY: optional bearer token, configure outside source control.
- IONOSCOPE_LLM_TIMEOUT_SECONDS: 60 by default.

All public timestamps are UTC with Z; naive input is interpreted as UTC.
Freshness deadlines follow the oldest calculation/source/observation timestamp;
valid_until is also capped at the forecast target. The API does not relabel old
source data as fresh based on a new calculation time.

Data API must include the allow_partial history option introduced with this contract.
Live workers request 192 hours initially; actual available history depends on collection
success and the station. Partial coverage produces null chart points, not invented data.

Machine-readable frontend files are in contracts/: openapi.json and forecast.example.json.
The compact example illustrates gaps and table correspondence, rather than a full day.
Errors always use {"detail":{"code":"...","message":"..."}}.
Stable forecast codes: invalid_station (422), station_not_found (404),
parameter_unavailable (409), forecast_unavailable / insufficient_observations (503),
forecast_expired (409), upstream_error (502), upstream_timeout (504).
Additional codes: invalid_request (422), station_inactive / cache_unavailable (503),
not_found (404), request_error (e.g. 405), internal_error (500), rate_limited (429 at nginx),
and explanation_unavailable in SSE. Display text should be selected by code.

GET /api/v1/forecast/parameters lists foF2, MUF, TEC. Station list keeps its stations
wrapper and legacy metadata, adding name, lat, lon, active, freshness_status and deadlines.

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
  "source_interval_end": "2026-09-27T00:15:00Z",
  "forecast_interval_start": "2026-09-27T00:15:00Z",
  "forecast_timestamp": "2026-09-27T00:30:00Z",
  "observation_timestamp": "2026-09-27T00:10:00Z",
  "current_fof2_available": true,
  "generated_at": "2026-09-27T00:16:00Z",
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
calculation time. For interval-averaged features, forecast target END is source interval end plus
model horizon. The target interval START is grid time plus model horizon.


## HTTP contract verification

Data API must provide `source_interval_end` exactly 15 minutes after the feature
grid timestamp. The backend accepts only completed source intervals and actual
observations inside them. For a 12:00–12:15 source interval and horizon 15min,
`forecast_interval_start` is 12:15 and `forecast_timestamp` (target END) is 12:30.
This agrees with the historical next-interval evaluation. Deploy the updated
Data API and backend together; older payloads lacking interval end are rejected.

Station codes are trimmed and normalized to uppercase. Invalid codes return
422; unknown stations return 404. Missing prepared data or current observations
return 503, malformed upstream payloads and connection failures return 502,
and upstream timeouts return 504. An unusable new result never overwrites cache.
Writes use atomic replacement; invalid/corrupt cache is treated as a miss.
`/health` is liveness, not a guarantee of upstream or data readiness.

From this repository, with sibling `ionoscope-data` and `ionoscope-ml-service`
checkouts and their dependencies installed:

```powershell
python scripts/check_stack.py --report ../output/api_verification/stack_checks.json
```

This starts three temporary local servers on unused ports, uses isolated
synthetic features and a real production ML artifact, checks all 17 routes in 54 scenarios,
and stops all servers. It does not alter production data, queue, cache or models.
Synthetic values check integration only; they do not validate forecast quality.


Full contract checking requires `requirements-dev.txt` (jsonschema). The checker
validates declared success/error response schemas, tests /docs and /redoc,
40 parallel cache reads, a complete monthly feature range and queue persistence.

For Linux containers and nginx, first build sibling services from infrastructure:

```powershell
docker compose -f docker-compose.dev.yml -p ionoscope-api-check build data-api ml-service api
```

Then run `python scripts/check_stack.py --docker --report ../output/api_verification/full_stack_docker.json`
from this repository. Docker mode also requires the sibling infrastructure
nginx configuration; generated containers/networks are removed after the check.
It reuses the shared `ionoscope-data:dev`, `ionoscope-ml-service:dev` and
`ionoscope-api:dev` images built by development Compose.

Add `--active-stations` to check the production activity guard as well (69
scenarios). This creates an isolated station registry and verifies fresh data,
low CS, absent measurements, source errors, inactive stations with previously
valid cache, and expired forecast targets. When the activity guard is enabled,
the Data API must be reachable even for cache reads; its outage returns 502.
The report uses synthetic data for failure scenarios and does not establish
the real-time operating status or forecast quality of those station codes.


## Durable historical forecasts and observed errors

GET /api/v1/forecast/history?station=TR169&start=2026-10-05T00:00:00Z&end=2026-11-03T23:59:59Z&limit=100
returns saved calculations, not retrospective model runs. Default window is 30 days;
maximum range is 31 days and limit is 1–500. Dates filter generated_at in UTC.
Forecasts are stored immutably in forecast_cache/forecasts.sqlite3, independently of
short-lived by_id cache expiration. Existing JSON snapshots are imported once.

IONOSCOPE_FORECAST_SHADOW_DB optionally points to a read-only shadow.sqlite3 control
journal. Local default is the sibling ML service's shadow_data/shadow.sqlite3;
an empty environment value disables this import. Docker mounts ml_shadow read-only.
Only existing original-model predictions are imported; candidate predictions and
missed slots are excluded. These rows are labelled shadow_original, not API results,
and retain the original model's SHA256 identity.

A fact is matched to forecast_interval_start, not to an adjacent observation or
source interval. The target interval must have ended and the observation must have
verified foF2 provenance within it. The first accepted fact is frozen; later data
revisions do not rewrite an earlier comparison. Historic data access explicitly uses
include_inactive=true without relaxing active-station requirements for live forecasts.

Each row includes actual, signed_error = prediction - actual, absolute_error,
percentage_error = absolute_error / abs(actual) * 100 and verification_status.
Percentage error is null when actual is zero. Missing/pending/unavailable observations
produce null errors, never artificial zeroes. MAE and RMSE describe only verified
rows in the returned page (verified_count); total counts all saved rows in the range.
Repeated calculations and control/API sources are retained as separate labelled runs.
If the Data API is unavailable, the archive and already frozen comparisons remain visible.
