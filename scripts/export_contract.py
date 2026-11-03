"""Run from the API directory: python -m scripts.export_contract."""
import json
from pathlib import Path

from app.examples import FORECAST_EXAMPLE
from app.main import app
from app.schemas import ForecastResponse


def main():
    ForecastResponse.model_validate(FORECAST_EXAMPLE)
    output = Path(__file__).resolve().parents[1] / 'contracts'
    output.mkdir(exist_ok=True)
    for filename, payload in [('openapi.json', app.openapi()), ('forecast.example.json', FORECAST_EXAMPLE)]:
        (output / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
