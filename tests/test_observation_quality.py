from datetime import UTC, datetime, timedelta

import pytest

from app.observation_quality import observation_quality
from app.forecast_cache import evaluate_forecast
from app.main import with_freshness
from app.schemas import ForecastResponse
from app.config import Settings


NOW = datetime(2026, 11, 18, 18, 23, tzinfo=UTC)


def test_daily_coverage_includes_trailing_gaps_and_ignores_other_days():
    end = NOW.replace(minute=15)
    start = end - timedelta(hours=24)
    points = {start+timedelta(minutes=15*i): 5.0 for i in range(96) if i not in (5, 6, 94, 95)}
    points[start-timedelta(minutes=15)] = 5.0
    points[end] = 5.0  # An incomplete current interval must not improve coverage.
    quality = observation_quality(points, now=NOW)
    assert quality.expected_intervals == 96
    assert quality.available_intervals == 92
    assert quality.missing_intervals == 4
    assert quality.coverage_percent == 95.83
    assert quality.longest_gap_minutes == quality.trailing_gap_minutes == 30


@pytest.mark.parametrize('points,available,gap', [({}, 0, 1440),
    ({NOW.replace(minute=15)-timedelta(minutes=15*i): 5.0 for i in range(1,97)}, 96, 0)])
def test_empty_and_complete_daily_windows(points, available, gap):
    quality = observation_quality(points, now=NOW)
    assert quality.available_intervals == available
    assert quality.longest_gap_minutes == gap


def make_forecast():
    return ForecastResponse(
        station='BVJ03', source_timestamp=NOW-timedelta(minutes=38),
        source_interval_end=NOW-timedelta(minutes=23),
        observation_timestamp=NOW-timedelta(minutes=33), current_fof2_available=True,
        generated_at=NOW, forecast_timestamp=NOW+timedelta(hours=1),
        parameter='foF2', prediction=5, unit='MHz', horizon='1h', model='test', model_version='1',
        observation_age_at_generation_minutes=33,
    )


def test_recalculation_does_not_make_old_observations_fresh_and_cache_age_grows():
    forecast = make_forecast()
    status = evaluate_forecast(forecast, Settings(), now=NOW)
    assert status.status == 'stale'
    assert status.age_minutes == 0
    assert status.observation_age_minutes == 33
    later = evaluate_forecast(forecast, Settings(), now=NOW+timedelta(minutes=5))
    response = with_freshness(forecast, later)
    assert response.observation_age_minutes == 38
    assert response.observation_age_at_generation_minutes == 33
    assert response.forecast_timestamp == forecast.forecast_timestamp


def test_past_target_cannot_be_relabelled_as_a_future_forecast():
    forecast = make_forecast().model_copy(update={'forecast_timestamp': NOW-timedelta(minutes=1)})
    status = evaluate_forecast(forecast, Settings(), now=NOW)
    assert status.status == 'expired'
    assert status.is_usable is False


def test_expired_input_is_rejected_before_calling_ml(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import clients
    from app.main import app

    monkeypatch.setattr('app.main.get_settings', lambda: Settings(forecast_cache_dir=tmp_path))
    source = datetime.now(UTC)-timedelta(minutes=130)

    async def features(*args):
        return {'station': 'BVJ03', 'timestamp': source.isoformat(),
                'source_interval_end': (source+timedelta(minutes=15)).isoformat(),
                'observation_timestamp': (source+timedelta(minutes=5)).isoformat(),
                'current_fof2_available': True, 'features': {'foF2': 5.0}}

    async def predict(*args):
        pytest.fail('Expired observations must not reach inference')

    monkeypatch.setattr(clients, 'get_json', features)
    monkeypatch.setattr(clients, 'post_json', predict)
    response = TestClient(app).post('/api/v1/forecast/refresh', json={'station': 'BVJ03'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'insufficient_observations'
    assert not (tmp_path/'BVJ03.json').exists()
