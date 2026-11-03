from dataclasses import replace
from datetime import datetime, UTC, timedelta
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ForecastResponse
from test_api_contracts import upstream


def test_roles_share_forecast_and_table_matches_chart(upstream):
    client, state = upstream
    a = client.post('/api/v1/forecast', json={'station': 'TR169', 'role': 'geodesist'}).json()
    b = client.post('/api/v1/forecast', json={'station': 'TR169', 'role': 'drone_pilot'}).json()
    assert a['forecast_id'] == b['forecast_id']
    assert b['role'] == 'drone_pilot'
    assert state['ml_calls'] == 1
    assert len(a['series']['observed']) == 97
    assert a['series']['observed'][-1]['value'] == 4.05
    assert a['series']['observed'][0]['value'] is None
    assert a['series']['forecast'][0]['lower'] is None
    assert a['table']['rows'] == [
        {'time': p['t'], 'kind': 'observed', 'foF2': p['value']} for p in a['series']['observed']
    ] + [{'time': a['forecast_timestamp'], 'kind': 'forecast', 'foF2': a['prediction']}]
    for key in ('source_timestamp', 'generated_at', 'stale_after', 'valid_until'):
        assert a[key].endswith('Z')
    assert a['valid_until'] == a['forecast_timestamp']
    assert datetime.fromisoformat(a['stale_after']) == state['start'] + timedelta(minutes=30)


@pytest.mark.parametrize('body,status,code', [
    ({'station': '../x'}, 422, 'invalid_station'),
    ({'station': 'TR169', 'parameter': 'TEC'}, 409, 'parameter_unavailable'),
    ({'station': 'TR169', 'role': 'unknown'}, 422, 'invalid_request'),
])
def test_errors(upstream, body, status, code):
    client, state = upstream
    response = client.post('/api/v1/forecast', json=body)
    assert response.status_code == status
    assert set(response.json()['detail']) == {'code', 'message'}
    assert response.json()['detail']['code'] == code
    assert state['ml_calls'] == 0


def test_openapi_and_cors(upstream):
    client, _ = upstream
    schema = client.get('/openapi.json').json()
    assert schema['info']['version'] == '0.2.0'
    from jsonschema import Draft202012Validator
    for name, definition in schema['components']['schemas'].items():
        for example in definition.get('examples', []):
            Draft202012Validator({'$ref': f'#/components/schemas/{name}',
                                  'components': schema['components']}).validate(example)
    assert schema['components']['schemas']['ForecastResponse']['examples'][0]['explanation'] is None
    operations = [op for path in schema['paths'].values() for op in path.values()]
    assert len({op['operationId'] for op in operations}) == len(operations)
    assert schema['paths']['/api/v1/forecast']['post']['responses']['422']['content']['application/json']['schema']['$ref'].endswith('/ErrorResponse')
    assert 'text/event-stream' in schema['paths']['/api/v1/explain']['post']['responses']['200']['content']
    response = client.options('/api/v1/forecast', headers={'Origin': 'http://localhost:4200', 'Access-Control-Request-Method': 'POST'})
    assert response.headers['access-control-allow-origin'] == 'http://localhost:4200'
    assert client.get('/missing').json()['detail']['code'] == 'not_found'
    assert [p['available'] for p in client.get('/api/v1/forecast/parameters').json()] == [True, False, False]


def test_unconfigured_explanation_does_not_break_forecast(upstream):
    client, _ = upstream
    forecast = client.post('/api/v1/forecast', json={'station': 'TR169'}).json()
    response = client.post('/api/v1/explain', json={'forecast_id': forecast['forecast_id'], 'role': 'geodesist'})
    assert response.status_code == 200
    assert response.headers['x-accel-buffering'] == 'no'
    assert 'event: start' in response.text
    assert 'event: error' in response.text
    assert 'explanation_unavailable' in response.text
    assert 'event: done' not in response.text
    assert client.post('/api/v1/forecast', json={'station': 'TR169'}).json()['prediction'] == forecast['prediction']


def test_expired_and_client_values_rejected(upstream, tmp_path):
    client, _ = upstream
    forecast = client.post('/api/v1/forecast', json={'station': 'TR169'}).json()
    body = {'forecast_id': forecast['forecast_id'], 'role': 'geodesist'}
    assert client.post('/api/v1/explain', json={**body, 'prediction': 100}).status_code == 422
    path = tmp_path / 'by_id' / (forecast['forecast_id'] + '.json')
    payload = json.loads(path.read_text())
    payload['forecast_timestamp'] = datetime.now(UTC).isoformat()
    path.write_text(json.dumps(payload))
    response = client.post('/api/v1/explain', json=body)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'forecast_expired'


def test_refresh_preserves_still_valid_forecast_ids(upstream):
    client, _ = upstream
    first = client.post('/api/v1/forecast', json={'station': 'TR169'}).json()
    second = client.post('/api/v1/forecast/refresh', json={'station': 'TR169'}).json()
    assert first['forecast_id'] != second['forecast_id']
    response = client.post('/api/v1/explain', json={'forecast_id': first['forecast_id']})
    assert response.status_code == 200
    assert 'event: start' in response.text


def test_station_list_contains_frontend_fields(upstream, monkeypatch):
    client, _ = upstream
    from app.schemas import Station, StationListResponse

    async def stations(settings):
        return StationListResponse(stations=[Station(code='TR169', location='Tromso', latitude=69.6,
                                                      longitude_180=19.2, is_active=True)])
    monkeypatch.setattr('app.main.fetch_stations', stations)
    client.post('/api/v1/forecast', json={'station': 'TR169'})
    result = client.get('/api/v1/forecast/stations').json()['stations'][0]
    assert (result['name'], result['lat'], result['lon'], result['active']) == ('Tromso', 69.6, 19.2, True)
    assert result['freshness_status'] == 'fresh'
    assert result['valid_until'].endswith('Z')


@pytest.mark.parametrize('truncated', [False, True])
def test_stream_and_role_cache(upstream, monkeypatch, tmp_path, truncated):
    client, _ = upstream
    from app.main import get_settings
    settings = replace(get_settings(), llm_url='http://llm.local/chat/completions', llm_model='test-model')
    monkeypatch.setattr('app.main.get_settings', lambda: settings)
    forecast = client.post('/api/v1/forecast', json={'station': 'TR169'}).json()
    requests = []
    real_client = httpx._client.AsyncClient

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        summary = json.loads(payload['messages'][1]['content'])
        assert 'features' not in summary and 'series' not in summary
        assert summary['prediction'] == forecast['prediction']
        data = 'data: ' + json.dumps({'model': 'served-model', 'choices': [{'delta': {'content': 'Hello'}}]}) + '\n\n'
        if not truncated:
            data += 'data: [DONE]\n\n'
        return httpx.Response(200, text=data)

    monkeypatch.setattr('app.explanations.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    body = {'forecast_id': forecast['forecast_id'], 'role': 'geodesist'}
    response = client.post('/api/v1/explain', json=body)
    assert 'event: delta' in response.text
    assert ('event: done' in response.text) == (not truncated)
    assert ('event: error' in response.text) == truncated
    if not truncated:
        assert 'served-model' in response.text
        assert 'event: done' in client.post('/api/v1/explain', json=body).text
        assert len(requests) == 1
        assert client.post('/api/v1/forecast', json={'station': 'TR169'}).json()['explanation'] == 'Hello'
        assert client.post('/api/v1/forecast', json={'station': 'TR169', 'role': 'drone_pilot'}).json()['explanation'] is None


def test_timestamps_normalize_naive_and_offsets():
    result = ForecastResponse(station='TR169', source_timestamp='2026-01-01T03:00:00+03:00', generated_at='2026-01-01T00:00:00', parameter='foF2', prediction=1, unit='MHz', horizon='15min', model='test', model_version='1')
    assert result.model_dump(mode='json')['source_timestamp'] == '2026-01-01T00:00:00Z'
    assert result.model_dump(mode='json')['generated_at'] == '2026-01-01T00:00:00Z'

