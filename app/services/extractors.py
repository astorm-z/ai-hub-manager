from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.models import Channel, ExtractorTemplate
from app.schemas import BalanceResult


VAR_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


SUB2API_TEMPLATE = {
    "request": {
        "url": "{{baseUrl}}/v1/usage",
        "method": "GET",
        "headers": {"Authorization": "Bearer {{apiKey}}"},
    },
    "extract": {
        "isValid": {"first": [{"path": "is_active"}, {"path": "isValid"}, {"const": True}]},
        "remaining": {"first": [{"path": "remaining"}, {"path": "quota.remaining"}, {"path": "balance"}]},
        "unit": {"first": [{"path": "unit"}, {"path": "quota.unit"}, {"const": "USD"}]},
    },
}


NEWAPI_TEMPLATE = {
    "request": {
        "url": "{{baseUrl}}/api/user/self",
        "method": "GET",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer {{accessToken}}",
            "User-Agent": "ai-hub-manager/1.0",
            "New-Api-User": "{{userId}}",
        },
    },
    "validWhen": {"path": "success"},
    "invalidMessage": {"first": [{"path": "message"}, {"const": "查询失败"}]},
    "extract": {
        "planName": {"first": [{"path": "data.group"}, {"const": "默认套餐"}]},
        "remaining": {"divide": [{"path": "data.quota"}, {"const": 500000}]},
        "used": {"divide": [{"path": "data.used_quota"}, {"const": 500000}]},
        "total": {
            "divide": [
                {"add": [{"path": "data.quota"}, {"path": "data.used_quota"}]},
                {"const": 500000},
            ]
        },
        "unit": {"const": "USD"},
    },
}


def builtin_templates() -> list[dict[str, Any]]:
    return [
        {"name": "sub2api", "description": "sub2api /v1/usage 余额格式", "template_json": json.dumps(SUB2API_TEMPLATE, ensure_ascii=False), "builtin": True},
        {"name": "newapi", "description": "New API /api/user/self 余额格式", "template_json": json.dumps(NEWAPI_TEMPLATE, ensure_ascii=False), "builtin": True},
    ]


def seed_builtin_extractors(db) -> None:
    for item in builtin_templates():
        existing = db.query(ExtractorTemplate).filter(ExtractorTemplate.name == item["name"]).first()
        if existing:
            if existing.builtin:
                existing.description = item["description"]
                existing.template_json = item["template_json"]
            continue
        db.add(ExtractorTemplate(**item))
    db.commit()


def render_template(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(variables.get(key, ""))

        return VAR_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, variables) for key, item in value.items()}
    return value


def get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
        if current is None:
            return None
    return current


def eval_expr(expr: Any, data: Any) -> Any:
    if not isinstance(expr, dict):
        return expr
    if "path" in expr:
        return get_path(data, str(expr["path"]))
    if "const" in expr:
        return expr["const"]
    if "first" in expr:
        for item in expr["first"]:
            value = eval_expr(item, data)
            if value is not None:
                return value
        return None
    if "add" in expr:
        total = 0.0
        for item in expr["add"]:
            value = eval_expr(item, data)
            if value is None:
                return None
            total += float(value)
        return total
    if "divide" in expr:
        values = expr["divide"]
        if len(values) != 2:
            return None
        left = eval_expr(values[0], data)
        right = eval_expr(values[1], data)
        if left is None or right in (None, 0):
            return None
        return float(left) / float(right)
    if "default" in expr:
        values = expr["default"]
        if len(values) != 2:
            return None
        value = eval_expr(values[0], data)
        return value if value is not None else eval_expr(values[1], data)
    return None


def build_variables(channel: Channel, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    variables = {"baseUrl": channel.base_url.rstrip("/"), "apiKey": channel.api_key}
    try:
        stored = json.loads(channel.extractor_vars_json or "{}")
    except json.JSONDecodeError:
        stored = {}
    variables.update(stored)
    if extra:
        variables.update(extra)
    return variables


async def run_extractor(template: dict[str, Any], variables: dict[str, Any], timeout: float = 20) -> BalanceResult:
    request_config = render_template(template.get("request", {}), variables)
    method = str(request_config.get("method", "GET")).upper()
    url = request_config.get("url")
    headers = request_config.get("headers") or {}
    body = request_config.get("body")
    if not url:
        return BalanceResult(is_valid=False, invalid_message="提取器缺少请求 URL")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, headers=headers, json=body if isinstance(body, (dict, list)) else None, content=body if isinstance(body, str) else None)
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
    except httpx.RequestError as exc:
        return BalanceResult(is_valid=False, invalid_message=str(exc))

    if response.status_code >= 400:
        return BalanceResult(is_valid=False, invalid_message=f"HTTP {response.status_code}", raw=data)

    valid_expr = template.get("validWhen")
    if valid_expr is not None and not bool(eval_expr(valid_expr, data)):
        invalid_message = eval_expr(template.get("invalidMessage", {"const": "查询失败"}), data)
        return BalanceResult(is_valid=False, invalid_message=str(invalid_message), raw=data)

    extract = template.get("extract", {})
    values = {key: eval_expr(expr, data) for key, expr in extract.items()}
    is_valid = values.get("isValid")
    if is_valid is None:
        is_valid = True
    return BalanceResult(
        is_valid=bool(is_valid),
        invalid_message=values.get("invalidMessage"),
        plan_name=values.get("planName"),
        remaining=_to_float(values.get("remaining")),
        used=_to_float(values.get("used")),
        total=_to_float(values.get("total")),
        unit=str(values.get("unit")) if values.get("unit") is not None else None,
        raw=data,
    )


async def query_channel_balance(channel: Channel, template: ExtractorTemplate | None, extra_vars: dict[str, Any] | None = None) -> BalanceResult:
    if not template:
        return BalanceResult(is_valid=False, invalid_message="未配置余额提取器")
    try:
        template_data = json.loads(template.template_json)
    except json.JSONDecodeError:
        return BalanceResult(is_valid=False, invalid_message="提取器 JSON 无效")
    return await run_extractor(template_data, build_variables(channel, extra_vars), channel.timeout_seconds)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
