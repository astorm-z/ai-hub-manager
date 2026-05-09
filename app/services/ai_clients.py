from __future__ import annotations

import time
from typing import Any

import httpx

from app.models import Channel
from app.schemas import ModelInfo, ProbeResult


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


async def list_models(channel: Channel) -> tuple[list[ModelInfo], ProbeResult]:
    started = time.perf_counter()
    headers = _headers_for(channel)
    url = _join_url(channel.base_url, "/v1/models")
    try:
        async with httpx.AsyncClient(timeout=channel.timeout_seconds) as client:
            response = await client.get(url, headers=headers)
        latency = int((time.perf_counter() - started) * 1000)
        data = response.json()
        if response.status_code >= 400:
            return [], ProbeResult(False, response.status_code, latency, _error_message(data, response.text), data)
        models = _parse_models(data)
        return models, ProbeResult(True, response.status_code, latency, f"获取到 {len(models)} 个模型", data)
    except Exception as exc:  # network and invalid response errors are both probe failures
        latency = int((time.perf_counter() - started) * 1000)
        return [], ProbeResult(False, None, latency, str(exc))


async def test_model(channel: Channel, model: str, prompt: str = "Reply with OK.") -> ProbeResult:
    if channel.provider_type == "claude":
        return await _test_claude(channel, model, prompt)
    return await _test_openai(channel, model, prompt)


def _headers_for(channel: Channel) -> dict[str, str]:
    if channel.provider_type == "claude":
        return {
            "x-api-key": channel.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {"Authorization": f"Bearer {channel.api_key}", "content-type": "application/json"}


def _parse_models(data: Any) -> list[ModelInfo]:
    raw_models = data.get("data", data) if isinstance(data, dict) else data
    models: list[ModelInfo] = []
    if isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, str):
                models.append(ModelInfo(item))
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    models.append(ModelInfo(str(model_id), item.get("owned_by") or item.get("display_name")))
    return models


async def _test_openai(channel: Channel, model: str, prompt: str) -> ProbeResult:
    started = time.perf_counter()
    headers = _headers_for(channel)
    if channel.openai_test_mode == "responses":
        url = _join_url(channel.base_url, "/v1/responses")
        payload = {"model": model, "input": prompt, "max_output_tokens": 8}
    else:
        url = _join_url(channel.base_url, "/v1/chat/completions")
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
    try:
        async with httpx.AsyncClient(timeout=channel.timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
        latency = int((time.perf_counter() - started) * 1000)
        data = response.json()
        if response.status_code >= 400:
            return ProbeResult(False, response.status_code, latency, _error_message(data, response.text), data)
        return ProbeResult(True, response.status_code, latency, "模型测试成功", data)
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency, str(exc))


async def _test_claude(channel: Channel, model: str, prompt: str) -> ProbeResult:
    started = time.perf_counter()
    headers = _headers_for(channel)
    payload = {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": prompt}]}
    url = _join_url(channel.base_url, "/v1/messages")
    try:
        async with httpx.AsyncClient(timeout=channel.timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
        latency = int((time.perf_counter() - started) * 1000)
        data = response.json()
        if response.status_code >= 400:
            return ProbeResult(False, response.status_code, latency, _error_message(data, response.text), data)
        return ProbeResult(True, response.status_code, latency, "模型测试成功", data)
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency, str(exc))


def _error_message(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        if data.get("message"):
            return str(data["message"])
    return fallback[:500] if fallback else "请求失败"
