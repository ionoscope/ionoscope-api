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

Add `--active-stations` to check the production activity guard as well (69
scenarios). This creates an isolated station registry and verifies fresh data,
low CS, absent measurements, source errors, inactive stations with previously
valid cache, and expired forecast targets. When the activity guard is enabled,
the Data API must be reachable even for cache reads; its outage returns 502.
The report uses synthetic data for failure scenarios and does not establish
the real-time operating status or forecast quality of those station codes.
