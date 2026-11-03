from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import httpx

from .schemas import ForecastResponse, Role
from .config import Settings


def cache_path(forecast_id: str, role: Role, settings: Settings) -> Path:
    return settings.forecast_cache_dir / "explanations" / f"{forecast_id}_{role.value}.json"


def read_cache(forecast_id, role, settings):
    try:
        result = json.loads(cache_path(forecast_id, role, settings).read_text(encoding="utf-8"))
        if isinstance(result.get("text"), str) and result["text"] and isinstance(result.get("model"), str):
            return result
    except (OSError, ValueError, AttributeError):
        pass
    return None


def cached_explanation(forecast_id, role, settings):
    result = read_cache(forecast_id, role, settings)
    return result["text"] if result else None


def event(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def save_cache(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


async def explain_stream(forecast: ForecastResponse, role: Role, settings: Settings):
    yield event("start", {"forecast_id": forecast.forecast_id, "role": role.value})
    cached = read_cache(forecast.forecast_id, role, settings)
    if cached:
        yield event("delta", {"text": cached["text"]})
        yield event("done", cached)
        return
    try:
        if not settings.llm_url or not settings.llm_model:
            raise ValueError("Explanation provider is not configured")
        # Only the computed summary is sent. No raw observations, features or client numbers.
        summary = forecast.model_dump(mode="json", include={"station", "parameter", "prediction", "unit",
            "forecast_timestamp", "freshness_status", "model", "model_version"})
        payload = {"model": settings.llm_model, "stream": True, "messages": [
            {"role": "system", "content": "Объясни прогноз ионосферы по-русски для роли " + role.value +
             ". Не выдумывай данные, интервалы неопределённости и гарантии безопасности. Это точечный прогноз."},
            {"role": "user", "content": json.dumps(summary, ensure_ascii=False)}]}
        headers = {"Authorization": "Bearer " + settings.llm_api_key} if settings.llm_api_key else {}
        text, model, completed = "", settings.llm_model, False
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            async with client.stream("POST", settings.llm_url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        completed = True
                        break
                    item = json.loads(data)
                    model = item.get("model") or model
                    for choice in item.get("choices", []):
                        chunk = choice.get("delta", {}).get("content")
                        if chunk:
                            if not isinstance(chunk, str):
                                raise ValueError("Invalid explanation chunk")
                            text += chunk
                            if len(text) > 64000:
                                raise ValueError("Explanation exceeds limit")
                            yield event("delta", {"text": chunk})
        if not completed or not text.strip():
            raise ValueError("Incomplete explanation stream")
        result = {"text": text, "model": model}
        save_cache(cache_path(forecast.forecast_id, role, settings), result)
        yield event("done", result)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError):
        yield event("error", {"code": "explanation_unavailable", "message": "Explanation is temporarily unavailable"})
