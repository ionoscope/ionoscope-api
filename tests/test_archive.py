from datetime import UTC, datetime, timedelta
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.archive import archive_forecast, read_archive
from app.config import Settings
from app.main import app
from app.schemas import ForecastResponse


def forecast(now, *, id='TR169_0123456789abcdef0123456789abcdef', prediction=6):
    return ForecastResponse(forecast_id=id, station='TR169', prediction=prediction, generated_at=now-timedelta(minutes=20),
        source_timestamp=now-timedelta(minutes=40), forecast_interval_start=now-timedelta(minutes=25),
        forecast_timestamp=now-timedelta(minutes=10), parameter='foF2', unit='MHz', horizon='15min', model='test', model_version='1')


@pytest.mark.parametrize('actual,expected,percent', [(5, 1, 20), (0, 6, None), (7, 1, 100/7)])
def test_matches_interval_and_computes_error(monkeypatch, tmp_path, actual, expected, percent):
    import asyncio
    now = datetime.now(UTC); item = forecast(now); settings = Settings(forecast_cache_dir=tmp_path, shadow_journal_path=None)
    archive_forecast(item, settings)
    async def observations(*args):
        return {'station':'TR169', 'records': [
            {'station':'TR169', 'timestamp':(item.forecast_interval_start-timedelta(minutes=15)).isoformat(), 'features':{'foF2':99}, 'current_fof2_available':True, 'observation_timestamp':(item.forecast_interval_start-timedelta(minutes=10)).isoformat()},
            {'station':'TR169', 'timestamp':item.forecast_interval_start.isoformat(), 'features':{'foF2':actual}, 'current_fof2_available':True, 'observation_timestamp':(item.forecast_interval_start+timedelta(minutes=5)).isoformat()}]}
    monkeypatch.setattr('app.archive.get_json', observations)
    result = asyncio.run(read_archive('TR169', now-timedelta(days=1), now, 100, settings, now=now))
    row = result.items[0]
    assert row.verification_status == 'verified'
    assert row.actual == actual and row.absolute_error == expected
    assert row.signed_error == 6-actual
    assert row.percentage_error == pytest.approx(percent) if percent is not None else row.percentage_error is None
    assert result.mae == expected and result.rmse == expected


def test_predictions_and_first_fact_are_immutable(monkeypatch, tmp_path):
    import asyncio
    now = datetime.now(UTC); original = forecast(now); settings = Settings(forecast_cache_dir=tmp_path, shadow_journal_path=None)
    archive_forecast(original, settings)
    archive_forecast(original.model_copy(update={'prediction':999}), settings)
    async def observations(*args):
        return {'station':'TR169','records':[{'station':'TR169','timestamp':original.forecast_interval_start.isoformat(), 'features':{'foF2':5},'current_fof2_available':True,'observation_timestamp':original.forecast_interval_start.isoformat()}]}
    monkeypatch.setattr('app.archive.get_json', observations)
    first = asyncio.run(read_archive('TR169', now-timedelta(days=1), now, 100, settings))
    async def fail(*args): raise AssertionError('Frozen fact must not be fetched again')
    monkeypatch.setattr('app.archive.get_json', fail)
    second = asyncio.run(read_archive('TR169', now-timedelta(days=1), now, 100, settings))
    assert second.items[0].prediction == 6
    assert second.items[0].actual == first.items[0].actual == 5


@pytest.mark.parametrize('case', ['missing', 'invalid_provenance', 'offline', 'future', 'late'])
def test_missing_or_unqualified_fact_is_not_zero(monkeypatch, tmp_path, case):
    import asyncio
    now = datetime.now(UTC); item = forecast(now); settings = Settings(forecast_cache_dir=tmp_path, shadow_journal_path=None)
    if case == 'future': item = item.model_copy(update={'forecast_timestamp':now+timedelta(minutes=15)})
    if case == 'late': item = item.model_copy(update={'generated_at':item.forecast_timestamp})
    archive_forecast(item, settings)
    async def observations(*args):
        if case == 'offline': raise httpx.ConnectError('offline')
        records = [] if case == 'missing' else [{'station':'TR169','timestamp':item.forecast_interval_start.isoformat(),'features':{'foF2':5}, 'current_fof2_available':True, 'observation_timestamp':(item.forecast_interval_start+timedelta(minutes=20)).isoformat()}]
        return {'station':'TR169','records':records}
    monkeypatch.setattr('app.archive.get_json', observations)
    result = asyncio.run(read_archive('TR169', now-timedelta(days=1), now, 100, settings, now=now))
    row = result.items[0]
    assert row.absolute_error is None and result.mae is None and result.verified_count == 0
    assert row.verification_status == {'missing':'observation_missing','invalid_provenance':'observation_missing','offline':'observations_unavailable','future':'pending','late':'unverifiable'}[case]


def test_imports_existing_cache_and_original_control_predictions(monkeypatch, tmp_path):
    import asyncio
    now = datetime.now(UTC); item = forecast(now)
    (tmp_path/'TR169.json').write_text(item.model_dump_json())
    journal=tmp_path/'shadow.sqlite3'
    with sqlite3.connect(journal) as db:
        db.executescript('CREATE TABLE run(id INTEGER, manifest TEXT); CREATE TABLE slots(station TEXT,issue TEXT,target_end TEXT,generated_at TEXT,original REAL,candidate REAL,actual REAL);')
        db.execute('INSERT INTO run VALUES(1,?)',(json.dumps({'original_sha256':'abc'}),))
        db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',('TR169',(now-timedelta(minutes=30)).isoformat(),(now-timedelta(minutes=15)).isoformat(),(now-timedelta(minutes=25)).isoformat(),7,999,6))
        db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',('TR169',(now-timedelta(minutes=60)).isoformat(),(now-timedelta(minutes=45)).isoformat(),None,None,None,5))
    async def observations(*args): return {'station':'TR169','records':[]}
    monkeypatch.setattr('app.archive.get_json', observations)
    result=asyncio.run(read_archive('TR169',now-timedelta(days=1),now,100,Settings(forecast_cache_dir=tmp_path,shadow_journal_path=journal)))
    assert result.total == 2
    control=next(row for row in result.items if row.source=='shadow_original')
    assert control.prediction == 7 and control.absolute_error == 1
    assert all(row.prediction != 999 for row in result.items)


@pytest.mark.parametrize("schema", ["", "CREATE TABLE run(id INTEGER, manifest TEXT);",
                                    "CREATE TABLE run(id INTEGER, manifest TEXT); CREATE TABLE slots(station TEXT);"])
def test_uninitialized_shadow_journal_preserves_api_archive(monkeypatch, tmp_path, schema):
    journal = tmp_path / "shadow.sqlite3"
    with closing(sqlite3.connect(journal)) as db:
        db.executescript(schema)
    settings = Settings(forecast_cache_dir=tmp_path, shadow_journal_path=journal)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    now = datetime.now(UTC)
    saved = forecast(now).model_copy(update={"forecast_timestamp": now + timedelta(minutes=5)})
    archive_forecast(saved, settings)

    response = TestClient(app).get("/api/v1/forecast/history", params={"station": "TR169"})

    assert response.status_code == 200
    assert [item["forecast_id"] for item in response.json()["items"]] == [saved.forecast_id]


def test_history_endpoint_validates_range_and_pagination(monkeypatch, tmp_path):
    settings=Settings(forecast_cache_dir=tmp_path,shadow_journal_path=None)
    monkeypatch.setattr('app.main.get_settings',lambda:settings)
    now=datetime.now(UTC)
    archive_forecast(forecast(now),settings)
    archive_forecast(forecast(now,id='TR169_1123456789abcdef0123456789abcdef'),settings)
    async def observations(*args): return {'station':'TR169','records':[]}
    monkeypatch.setattr('app.archive.get_json',observations)
    client=TestClient(app)
    response=client.get('/api/v1/forecast/history',params={'station':'tr169','limit':1})
    assert response.status_code == 200
    assert response.json()['total'] == 2 and len(response.json()['items']) == 1
    assert response.json()['items'][0]['generated_at'].endswith('Z')
    for params in ({'station':'../bad'},{'station':'TR169','limit':501},{'station':'TR169','start':'2026-01-01','end':'2026-03-01'}):
        assert client.get('/api/v1/forecast/history',params=params).status_code == 422
