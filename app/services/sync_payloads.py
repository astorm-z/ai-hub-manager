from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from app.models import Channel, ChannelSyncLink, SyncTarget


NEW_API_OPENAI_TYPE = 1
NEW_API_ANTHROPIC_TYPE = 14
SENSITIVE_KEYS = {
    "accesstoken",
    "adminapikey",
    "apikey",
    "authorization",
    "key",
    "password",
    "refreshtoken",
    "xapikey",
}


def _normalize_models(models: Iterable[str]) -> list[str]:
    return sorted({model for item in models if (model := str(item).strip())})


def _normalize_sensitive_key(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "")


def build_remote_name(target: SyncTarget, channel: Channel) -> str:
    return f"{target.name_prefix or 'union_'}{channel.name}"


def build_model_mapping(models: Iterable[str]) -> dict[str, str]:
    return {model: model for model in _normalize_models(models)}


def parse_group_ids(raw: str | None) -> list[int]:
    if not raw:
        return []

    text = raw.strip()
    if not text:
        return []

    from_json = text.startswith("[")
    if from_json:
        try:
            values = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("分组 ID 必须是整数列表") from exc
    else:
        values = [part.strip() for part in text.split(",") if part.strip()]

    if not isinstance(values, list):
        raise ValueError("分组 ID 必须是整数列表")

    group_ids: list[int] = []
    for value in values:
        if from_json:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("分组 ID 必须是正整数")
            group_id = value
        else:
            if not isinstance(value, str) or not value.isdecimal():
                raise ValueError("分组 ID 必须是正整数")
            group_id = int(value)
        if group_id <= 0:
            raise ValueError("分组 ID 必须是正整数")
        group_ids.append(group_id)
    return group_ids


def dumps_group_ids(group_ids: Iterable[int]) -> str:
    return json.dumps(list(group_ids), ensure_ascii=False)


def provider_to_newapi_type(provider_type: str) -> int:
    if provider_type == "claude":
        return NEW_API_ANTHROPIC_TYPE
    return NEW_API_OPENAI_TYPE


def provider_to_sub2api_platform(provider_type: str) -> str:
    if provider_type == "claude":
        return "anthropic"
    return "openai"


def build_newapi_channel_payload(
    channel: Channel,
    models: Iterable[str],
    *,
    remote_name: str,
    remote_id: str | int | None = None,
    existing_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = deepcopy(existing_payload) if existing_payload else {}
    if remote_id is not None:
        payload["id"] = int(remote_id)
    payload["name"] = remote_name
    payload["type"] = provider_to_newapi_type(channel.provider_type)
    payload["key"] = channel.api_key
    payload["base_url"] = channel.base_url
    payload["models"] = ",".join(_normalize_models(models))
    payload["test_model"] = channel.probe_model or ""
    payload["status"] = 1 if channel.enabled else 2
    payload.setdefault("group", "default")
    return payload


def build_sub2api_account_payload(
    channel: Channel,
    models: Iterable[str],
    link: ChannelSyncLink,
    *,
    remote_name: str,
    existing_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = deepcopy(existing_payload) if existing_payload else {}
    credentials = payload.get("credentials")
    if not isinstance(credentials, dict):
        credentials = {}
    else:
        credentials = deepcopy(credentials)

    credentials["api_key"] = channel.api_key
    credentials["base_url"] = channel.base_url
    credentials["model_mapping"] = build_model_mapping(models)

    payload["name"] = remote_name
    payload["platform"] = provider_to_sub2api_platform(channel.provider_type)
    payload["type"] = "apikey"
    payload["credentials"] = credentials
    payload["group_ids"] = parse_group_ids(link.sub2api_group_ids_json)
    payload["priority"] = link.sub2api_priority
    payload["concurrency"] = link.sub2api_concurrency
    payload["status"] = "active" if channel.enabled else "disabled"
    return payload


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _normalize_sensitive_key(key) in SENSITIVE_KEYS else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value
