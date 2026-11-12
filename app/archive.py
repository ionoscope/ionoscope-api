"""Durable, immutable forecast archive and matching against qualified interval observations."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
import math
import sqlite3
import httpx
from uuid import uuid5, NAMESPACE_URL
from urllib.parse import urlencode

from .config import Settings
from .schemas import ArchivedForecast, ForecastArchiveResponse, ForecastResponse
from .clients import get_json, utc_timestamp, UpstreamServiceError


def connect(settings: Settings):
    settings.forecast_cache_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.forecast_cache_dir / "forecasts.sqlite3", timeout=15)
    db.execute("CREATE TABLE IF NOT EXISTS forecasts (id TEXT PRIMARY KEY, station TEXT NOT NULL, generated REAL NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS station_generated ON forecasts(station, generated)")
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY)")
    return db


def insert(db, item: ArchivedForecast):
    db.execute("INSERT OR IGNORE INTO forecasts VALUES(?,?,?,?)", (item.forecast_id, item.station, item.generated_at.timestamp(), item.model_dump_json()))


def summary(forecast: ForecastResponse) -> ArchivedForecast:
    return ArchivedForecast(
        forecast_id=forecast.forecast_id or f"{forecast.station}_{uuid5(NAMESPACE_URL, forecast.station + forecast.generated_at.isoformat()).hex}",
        station=forecast.station, parameter=forecast.parameter, unit=forecast.unit,
        prediction=forecast.prediction, generated_at=forecast.generated_at,
        forecast_interval_start=forecast.forecast_interval_start,
        forecast_timestamp=forecast.forecast_timestamp, model=forecast.model, model_version=forecast.model_version)


def archive_forecast(forecast: ForecastResponse, settings: Settings):
    with closing(connect(settings)) as db, db:
        insert(db, summary(forecast))


def import_existing(db, settings: Settings, station: str, start: datetime, end: datetime):
    if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
        for path in list(settings.forecast_cache_dir.glob("*.json")) + list((settings.forecast_cache_dir / "by_id").glob("*.json")):
            try:
                insert(db, summary(ForecastResponse.model_validate_json(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        db.execute("INSERT OR IGNORE INTO metadata VALUES('legacy_imported')")
    path = settings.shadow_journal_path
    if path is None or not path.exists():
        return
    # The control journal is read-only. Only real original-model predictions are imported;
    # missed intervals and the candidate model are not presented as API forecasts.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as shadow:
        shadow.row_factory = sqlite3.Row
        tables = shadow.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('run', 'slots')"
        ).fetchone()[0]
        if tables != 2:
            return
        run = shadow.execute("SELECT manifest FROM run WHERE id=1").fetchone()
        if run is None:
            return
        manifest = json.loads(run["manifest"])
        model_version = "sha256:" + manifest["original_sha256"]
        for row in shadow.execute("SELECT * FROM slots WHERE station=? AND original IS NOT NULL AND generated_at IS NOT NULL", (station,)):
            generated = utc_timestamp(row["generated_at"])
            if not start <= generated <= end:
                continue
            item = ArchivedForecast(
                forecast_id="shadow_" + uuid5(NAMESPACE_URL, model_version + station + row["issue"]).hex,
                station=station, prediction=row["original"], generated_at=generated,
                forecast_interval_start=utc_timestamp(row["issue"]), forecast_timestamp=utc_timestamp(row["target_end"]),
                model="Original model (control journal)", model_version=model_version,
                source="shadow_original", actual=row["actual"])
            insert(db, item)
            if item.actual is not None:
                freeze_actual(db, item.forecast_id, item.actual, None)


def freeze_actual(db, forecast_id: str, actual: float, observed: datetime | None):
    row = db.execute("SELECT payload FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
    if row:
        item = ArchivedForecast.model_validate_json(row[0])
        if item.actual is None:
            item.actual, item.actual_observation_timestamp = actual, observed
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (item.model_dump_json(), forecast_id))


async def read_archive(station: str, start: datetime, end: datetime, limit: int, settings: Settings, *, now=None) -> ForecastArchiveResponse:
    now = now or datetime.now(UTC)
    with closing(connect(settings)) as db, db:
        import_existing(db, settings, station, start, end)
        total = db.execute("SELECT COUNT(*) FROM forecasts WHERE station=? AND generated BETWEEN ? AND ?", (station, start.timestamp(), end.timestamp())).fetchone()[0]
        rows = db.execute("SELECT payload FROM forecasts WHERE station=? AND generated BETWEEN ? AND ? ORDER BY generated DESC, id LIMIT ?", (station, start.timestamp(), end.timestamp(), limit)).fetchall()
        items = [ArchivedForecast.model_validate_json(row[0]) for row in rows]
    pending = [item for item in items if item.actual is None and item.forecast_interval_start and item.forecast_timestamp and item.forecast_timestamp <= now]
    observations_available = True
    facts = {}
    if pending:
        query = urlencode({"start": min(item.forecast_interval_start for item in pending).isoformat(),
                           "end": max(item.forecast_timestamp for item in pending).isoformat(), "allow_partial": "true", "include_inactive": "true"})
        try:
            payload = await get_json(settings.data_api_url, f"/api/v1/features/{station}?{query}", settings)
            if payload.get("station") != station or not isinstance(payload.get("records"), list):
                raise ValueError("Invalid observation history")
            for row in payload["records"]:
                timestamp = utc_timestamp(row["timestamp"])
                observed = utc_timestamp(row["observation_timestamp"]) if row.get("observation_timestamp") else None
                value = row.get("features", {}).get("foF2")
                if (row.get("station") == station and row.get("current_fof2_available") is True and observed
                        and timestamp <= observed < timestamp + timedelta(minutes=15)
                        and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)):
                    facts[timestamp] = (value, observed)
        except (httpx.HTTPError, UpstreamServiceError, ValueError, KeyError, TypeError, AttributeError):
            observations_available = False
    with closing(connect(settings)) as db, db:
        for index, item in enumerate(items):
            if not item.forecast_timestamp or not item.forecast_interval_start or item.generated_at >= item.forecast_timestamp:
                item.verification_status = "unverifiable"
            elif item.forecast_timestamp > now:
                item.verification_status = "pending"
            else:
                if item.actual is None and item.forecast_interval_start in facts:
                    value, observed = facts[item.forecast_interval_start]
                    freeze_actual(db, item.forecast_id, value, observed)
                    stored = db.execute("SELECT payload FROM forecasts WHERE id=?", (item.forecast_id,)).fetchone()
                    item = items[index] = ArchivedForecast.model_validate_json(stored[0])
                if item.actual is not None:
                    item.signed_error = item.prediction - item.actual
                    item.absolute_error = abs(item.signed_error)
                    item.percentage_error = item.absolute_error / abs(item.actual) * 100 if item.actual != 0 else None
                    item.verification_status = "verified"
                else:
                    item.verification_status = "observation_missing" if observations_available else "observations_unavailable"
    verified = [item for item in items if item.verification_status == "verified"]
    return ForecastArchiveResponse(station=station, start=start, end=end, items=items, total=total, limit=limit,
        verified_count=len(verified), mae=sum(item.absolute_error for item in verified) / len(verified) if verified else None,
        rmse=math.sqrt(sum(item.signed_error ** 2 for item in verified) / len(verified)) if verified else None,
        observations_available=observations_available)
