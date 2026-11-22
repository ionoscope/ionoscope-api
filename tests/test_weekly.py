from fastapi.testclient import TestClient
from app import clients
from app.main import app


def test_weekly_forecast_reads_ml_snapshot_without_modifying_production(monkeypatch):
    async def snapshot(base,path,settings):
        assert path == '/api/v1/weekly/AL945'
        return {'station':'AL945','points':[],'automatic_promotion':False}
    monkeypatch.setattr('app.main.get_json',snapshot)
    response = TestClient(app).get('/api/v1/forecast/weekly/al945')
    assert response.status_code == 200
    assert response.json()['automatic_promotion'] is False


def test_weekly_failure_is_independent_and_controlled(monkeypatch):
    async def unavailable(*args):
        raise clients.UpstreamServiceError('not trained')
    monkeypatch.setattr('app.main.get_json',unavailable)
    response = TestClient(app).get('/api/v1/forecast/weekly/AL945')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'forecast_unavailable'
