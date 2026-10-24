from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clients
from app.config import Settings
from app.main import app


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    settings = Settings(data_api_url='http://data.local', ml_service_url='http://ml.local', forecast_cache_dir=tmp_path)
    monkeypatch.setattr('app.main.get_settings', lambda: settings)
    real_client = httpx.AsyncClient
    state = {'case': '', 'ml_calls': 0}
    start = datetime.now(UTC) - timedelta(minutes=16)
    state['start'] = start

    def handler(request):
        case = state['case']
        if request.url.host == 'data.local':
            if case == 'station_missing':
                return httpx.Response(404, json={'detail': {'code': 'station_not_found'}})
            if case == 'data_missing':
                return httpx.Response(404, json={'detail': {'code': 'features_unavailable'}})
            if case == 'bad_json': return httpx.Response(200, text='<html>error</html>')
            if case == 'non_object': return httpx.Response(200, json=[])
            if case == 'timeout': raise httpx.ReadTimeout('timeout', request=request)
            payload = {'station': 'TR169', 'timestamp': start.isoformat(),
                       'source_interval_end': (start+timedelta(minutes=15)).isoformat(),
                       'observation_timestamp': (start+timedelta(minutes=10)).isoformat(),
                       'current_fof2_available': True, 'features': {'foF2': 4.05}}
            if case == 'mismatched_data_station': payload['station'] = 'OTHER'
            if case == 'bad_timestamp': payload['timestamp'] = 'not-a-date'
            if case == 'incomplete_interval': payload['source_interval_end'] = (start+timedelta(minutes=30)).isoformat()
            if case == 'missing_interval_end': payload.pop('source_interval_end')
            return httpx.Response(200, json=payload)
        state['ml_calls'] += 1
        if case == 'ml_down': raise httpx.ConnectError('offline', request=request)
        if case == 'ml_error': return httpx.Response(503, json={'detail': 'unavailable'})
        payload = {'station': 'TR169', 'parameter': 'foF2', 'prediction': 4.1,
                   'unit': 'MHz', 'horizon': '15min', 'model': 'test', 'model_version': '1'}
        if case == 'mismatched_ml_station': payload['station'] = 'OTHER'
        if case == 'bad_horizon': payload['horizon'] = '0min'
        if case == 'bad_parameter': payload['parameter'] = 'TEC'
        if case == 'bad_unit': payload['unit'] = 'km'
        if case == 'nan_prediction': payload['prediction'] = 'NaN'
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(clients.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return TestClient(app), state


@pytest.mark.parametrize('case,status', [
    ('station_missing', 404), ('data_missing', 503), ('bad_json', 502),
    ('non_object', 502), ('timeout', 504), ('mismatched_data_station', 502),
    ('mismatched_ml_station', 502), ('bad_timestamp', 502), ('incomplete_interval', 503),
    ('missing_interval_end', 502), ('ml_down', 502), ('ml_error', 502),
    ('bad_horizon', 502), ('nan_prediction', 502),
    ('bad_parameter', 502), ('bad_unit', 502),
])
def test_upstream_failures_have_controlled_http_responses(upstream, tmp_path, case, status):
    client, state = upstream
    state['case'] = case
    response = client.post('/api/v1/forecast/refresh', json={'station': 'TR169'})
    assert response.status_code == status
    assert 'code' in response.json()['detail']
    assert not (tmp_path / 'TR169.json').exists()
    if case == 'incomplete_interval': assert state['ml_calls'] == 0


def test_interval_times_and_cache_reuse(upstream):
    client, state = upstream
    response = client.post('/api/v1/forecast', json={'station': ' tr169 '})
    assert response.status_code == 200
    payload = response.json()
    assert datetime.fromisoformat(payload['forecast_interval_start'].replace('Z','+00:00')) == state['start']+timedelta(minutes=15)
    assert datetime.fromisoformat(payload['forecast_timestamp'].replace('Z','+00:00')) == state['start']+timedelta(minutes=30)
    assert payload['cached'] is False
    assert client.post('/api/v1/forecast', json={'station': 'TR169'}).json()['cached'] is True
    assert state['ml_calls'] == 1


@pytest.mark.parametrize('station', ['', '../escape', 'a/b', 'x'*33])
def test_invalid_station_rejected(upstream, station):
    client, state = upstream
    assert client.post('/api/v1/forecast', json={'station': station}).status_code == 422
    assert state['ml_calls'] == 0


@pytest.mark.parametrize('payload', ['{broken', '[]', '{"station":"OTHER"}'])
def test_invalid_cache_is_recomputed(upstream, tmp_path, payload):
    client, state = upstream
    (tmp_path/'TR169.json').write_text(payload)
    result = client.post('/api/v1/forecast', json={'station': 'TR169'})
    assert result.status_code == 200
    assert result.json()['cached'] is False
    assert state['ml_calls'] == 1


def test_failed_atomic_cache_write_keeps_previous_result(upstream, monkeypatch, tmp_path):
    client, _ = upstream
    assert client.post('/api/v1/forecast', json={'station': 'TR169'}).status_code == 200
    before = (tmp_path/'TR169.json').read_bytes()
    def fail(*args): raise PermissionError('disk failure')
    monkeypatch.setattr('app.forecast_cache.os.replace', fail)
    response = client.post('/api/v1/forecast/refresh', json={'station': 'TR169'})
    assert response.status_code == 503
    assert (tmp_path/'TR169.json').read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))
