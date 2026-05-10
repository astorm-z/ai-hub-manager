from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from typing import Any

from app.models import AlertRule, Channel, ChannelModel, ChannelSyncLink, SyncEvent, SyncTarget, now_utc
from app.services.sync_clients import SyncClientError, SyncClientResult, client_for_target
from app.services.sync_payloads import (
    build_newapi_channel_payload,
    build_remote_name,
    build_sub2api_account_payload,
    dumps_group_ids,
    normalize_newapi_groups,
    parse_group_ids,
    redact_sensitive,
)


SAME_NAME_ERROR = "目标站点已存在同名对象，请先手动改名或删除后再导入。"
SAFE_ERROR_MESSAGES = {
    SAME_NAME_ERROR,
    "缺少 API Key",
    "缺少 Base URL",
    "目标站点未启用。",
    "该渠道已绑定此同步目标。",
    "分组 ID 必须是字符串或整数列表",
    "分组 ID 必须是整数列表",
    "分组 ID 必须是正整数",
    "sub2api 优先级必须大于或等于 0。",
    "sub2api 并发必须大于 0。",
}
STRING_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,&;\"'}]+)"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,&;\"'}]+)"),
    re.compile(r"(?i)(password\s*[=:]\s*)([^\s,&;\"'}]+)"),
    re.compile(r'(?i)("(?:api[_-]?key|key|password|authorization)"\s*:\s*")([^"]*)(")'),
)


@dataclass(frozen=True)
class RemoteChannelCandidate:
    remote_id: str
    remote_name: str
    remote_type: str
    provider_type: str
    base_url: str
    api_key: str
    enabled: bool
    models: list[str] = field(default_factory=list)
    probe_model: str | None = None
    sub2api_group_ids: list[int] = field(default_factory=list)
    sub2api_group_labels: list[str] = field(default_factory=list)
    sub2api_priority: int = 1
    sub2api_concurrency: int = 10
    newapi_groups: str = "default"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteImportResult:
    imported_count: int
    failed_count: int
    imported_channel_ids: list[int]
    failures: list[str]


def channel_model_ids(db: Any, channel_id: int) -> list[str]:
    return [
        model_id
        for (model_id,) in db.query(ChannelModel.model_id)
        .filter(ChannelModel.channel_id == channel_id)
        .order_by(ChannelModel.model_id)
        .all()
    ]


def _json_safe(value: Any) -> Any:
    if isinstance(value, SyncClientResult):
        return {
            "status_code": value.status_code,
            "data": _json_safe(value.data),
            "raw": _json_safe(value.raw),
        }
    if hasattr(value, "json") and hasattr(value, "text") and hasattr(value, "status_code"):
        try:
            body = value.json()
        except ValueError:
            body = value.text
        return {"status_code": value.status_code, "body": _json_safe(body)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redact_secret_strings(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_secret_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_strings(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_strings(item) for item in value]
    if not isinstance(value, str):
        return value

    redacted = value
    for pattern in STRING_SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]\3" if pattern.groups >= 3 else r"\1[REDACTED]", redacted)
    return redacted


def _dump_event_json(value: Any) -> str | None:
    if value is None:
        return None
    safe_value = _redact_secret_strings(redact_sensitive(_json_safe(value)))
    return json.dumps(safe_value, ensure_ascii=False)


def _safe_error_message(message: str | None, status_code: int | None = None) -> str | None:
    if not message:
        return None
    if message in SAFE_ERROR_MESSAGES:
        return message
    if message.startswith("远端对象类型与目标站点类型不匹配："):
        return message
    http_match = re.search(r"\bHTTP\s+(\d{3})\b", message, flags=re.IGNORECASE)
    if http_match:
        return f"目标站点同步失败：HTTP {http_match.group(1)}"
    if status_code is not None:
        return f"目标站点同步失败：HTTP {status_code}"
    return "目标站点同步失败。"


def record_sync_event(
    db: Any,
    *,
    channel_id: int | None,
    target_id: int | None,
    link_id: int | None = None,
    action: str,
    success: bool,
    status_code: int | None = None,
    message: str | None = None,
    request_payload: Any = None,
    response_payload: Any = None,
) -> SyncEvent:
    event = SyncEvent(
        channel_id=channel_id,
        target_id=target_id,
        link_id=link_id,
        action=action,
        success=success,
        status_code=status_code,
        message=message,
        request_json=_dump_event_json(request_payload),
        response_json=_dump_event_json(response_payload),
    )
    db.add(event)
    return event


def _link_exists(db: Any, channel_id: int, target_id: int) -> bool:
    return (
        db.query(ChannelSyncLink)
        .filter(ChannelSyncLink.channel_id == channel_id, ChannelSyncLink.target_id == target_id)
        .first()
        is not None
    )


def _extract_remote_id(data: Any) -> str:
    if isinstance(data, dict):
        remote_id = data.get("id")
        if remote_id is None and isinstance(data.get("data"), dict):
            remote_id = data["data"].get("id")
        if remote_id is not None:
            return str(remote_id)
    raise SyncClientError("目标站点响应中缺少远端 ID。", response=data)


def _result_status_code(result: Any) -> int | None:
    return result.status_code if isinstance(result, SyncClientResult) else None


def _result_response(result: Any) -> Any:
    if isinstance(result, SyncClientResult):
        return result.data
    return result


def _remote_type_for_target(target: SyncTarget) -> str:
    if target.target_type == "sub2api":
        return "account"
    if target.target_type == "new_api":
        return "channel"
    raise SyncClientError(f"未知同步目标类型：{target.target_type}")


def _validate_link_target_type(link: ChannelSyncLink) -> None:
    expected_remote_type = _remote_type_for_target(link.target)
    if link.remote_type != expected_remote_type:
        raise SyncClientError(
            f"远端对象类型与目标站点类型不匹配：{link.remote_type} 不适用于 {link.target.target_type}。"
        )


def _validate_sub2api_link_settings(priority: int, concurrency: int) -> None:
    if priority < 0:
        raise ValueError("sub2api 优先级必须大于或等于 0。")
    if concurrency <= 0:
        raise ValueError("sub2api 并发必须大于 0。")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return ""


def _normalize_remote_models(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_models = value.split(",")
    elif isinstance(value, dict):
        raw_models = value.keys()
    elif isinstance(value, (list, tuple, set)):
        raw_models = value
    else:
        raw_models = []

    models: list[str] = []
    seen: set[str] = set()
    for raw_model in raw_models:
        model = _clean_text(raw_model)
        if not model or model in seen:
            continue
        models.append(model)
        seen.add(model)
    return sorted(models)


def _int_or_default(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive_int_or_default(value: Any, default: int) -> int:
    parsed = _int_or_default(value, default)
    return parsed if parsed > 0 else default


def _non_negative_int_or_default(value: Any, default: int) -> int:
    parsed = _int_or_default(value, default)
    return parsed if parsed >= 0 else default


def _provider_from_sub2api_platform(platform: Any) -> str:
    normalized = _clean_text(platform).lower()
    if normalized in {"anthropic", "claude"}:
        return "claude"
    return "openai"


def _provider_from_newapi_type(channel_type: Any) -> str:
    if _int_or_default(channel_type, 0) == 14:
        return "claude"
    return "openai"


def _enabled_from_newapi_status(status: Any) -> bool:
    if isinstance(status, bool):
        return status
    if isinstance(status, int):
        return status == 1
    normalized = _clean_text(status).lower()
    if normalized in {"1", "enabled", "enable", "active", "true"}:
        return True
    if normalized in {"2", "disabled", "disable", "false"}:
        return False
    return True


def _enabled_from_sub2api_status(status: Any) -> bool:
    normalized = _clean_text(status).lower()
    if normalized in {"disabled", "disable", "inactive", "false", "0"}:
        return False
    return True


def _group_ids_from_remote(value: Any) -> list[int]:
    if isinstance(value, str):
        try:
            return parse_group_ids(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []

    group_ids: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, dict):
            raw_id = item.get("id")
        else:
            raw_id = item
        group_id = _int_or_default(raw_id, 0)
        if group_id <= 0 or group_id in seen:
            continue
        group_ids.append(group_id)
        seen.add(group_id)
    return group_ids


def _group_labels_from_remote(raw: dict[str, Any], group_ids: list[int]) -> list[str]:
    labels_by_id: dict[int, str] = {}

    groups = raw.get("groups")
    if isinstance(groups, list):
        for item in groups:
            if not isinstance(item, dict):
                continue
            group_id = _int_or_default(item.get("id"), 0)
            group_name = _clean_text(item.get("name"))
            if group_id > 0 and group_name:
                labels_by_id[group_id] = group_name

    account_groups = raw.get("account_groups")
    if isinstance(account_groups, list):
        for item in account_groups:
            if not isinstance(item, dict):
                continue
            group = item.get("group")
            group_id = _int_or_default(item.get("group_id"), 0)
            if isinstance(group, dict):
                group_id = _int_or_default(group.get("id"), group_id)
                group_name = _clean_text(group.get("name"))
            else:
                group_name = ""
            if group_id > 0 and group_name:
                labels_by_id[group_id] = group_name

    return [labels_by_id.get(group_id, str(group_id)) for group_id in group_ids]


def _candidate_display_name(raw: dict[str, Any], remote_id: str) -> str:
    return _first_text(raw.get("name"), raw.get("label"), raw.get("display_name")) or f"remote-{remote_id}"


def _newapi_candidate_from_remote(raw: dict[str, Any]) -> RemoteChannelCandidate:
    remote_id = _extract_remote_id(raw)
    name = _candidate_display_name(raw, remote_id)
    api_key = _first_text(raw.get("key"), raw.get("api_key"), raw.get("apiKey"))
    base_url = _first_text(raw.get("base_url"), raw.get("baseUrl"))
    if not base_url:
        raise ValueError("缺少 Base URL")

    return RemoteChannelCandidate(
        remote_id=remote_id,
        remote_name=name,
        remote_type="channel",
        provider_type=_provider_from_newapi_type(raw.get("type")),
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        enabled=_enabled_from_newapi_status(raw.get("status")),
        models=_normalize_remote_models(raw.get("models")),
        probe_model=_first_text(raw.get("test_model"), raw.get("probe_model")) or None,
        newapi_groups=normalize_newapi_groups(raw.get("group")),
        raw=raw,
    )


def _sub2api_candidate_from_remote(raw: dict[str, Any]) -> RemoteChannelCandidate:
    remote_id = _extract_remote_id(raw)
    name = _candidate_display_name(raw, remote_id)
    credentials = raw.get("credentials")
    if not isinstance(credentials, dict):
        credentials = {}
    api_key = _first_text(credentials.get("api_key"), credentials.get("apiKey"), raw.get("api_key"), raw.get("key"))
    base_url = _first_text(credentials.get("base_url"), credentials.get("baseUrl"), raw.get("base_url"))
    if not api_key:
        raise ValueError("缺少 API Key")
    if not base_url:
        raise ValueError("缺少 Base URL")
    group_ids = _group_ids_from_remote(raw.get("group_ids"))

    return RemoteChannelCandidate(
        remote_id=remote_id,
        remote_name=name,
        remote_type="account",
        provider_type=_provider_from_sub2api_platform(raw.get("platform")),
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        enabled=_enabled_from_sub2api_status(raw.get("status")),
        models=_normalize_remote_models(credentials.get("model_mapping") or raw.get("models")),
        sub2api_group_ids=group_ids,
        sub2api_group_labels=_group_labels_from_remote(raw, group_ids),
        sub2api_priority=_non_negative_int_or_default(raw.get("priority"), 1),
        sub2api_concurrency=_positive_int_or_default(raw.get("concurrency"), 10),
        raw=raw,
    )


def _remote_candidate_from_target(target: SyncTarget, raw: dict[str, Any]) -> RemoteChannelCandidate:
    if target.target_type == "sub2api":
        return _sub2api_candidate_from_remote(raw)
    if target.target_type == "new_api":
        return _newapi_candidate_from_remote(raw)
    raise SyncClientError(f"未知同步目标类型：{target.target_type}")


def _existing_remote_ids(db: Any, target: SyncTarget) -> set[str]:
    rows = (
        db.query(ChannelSyncLink.remote_id)
        .filter(
            ChannelSyncLink.target_id == target.id,
            ChannelSyncLink.remote_type == _remote_type_for_target(target),
            ChannelSyncLink.remote_id.isnot(None),
        )
        .all()
    )
    return {str(remote_id) for (remote_id,) in rows if remote_id is not None}


async def list_remote_channel_import_candidates(
    db: Any,
    target: SyncTarget,
    *,
    client: Any = None,
) -> list[RemoteChannelCandidate]:
    target_client = client or client_for_target(target)
    if target.target_type == "sub2api":
        remote_items = await target_client.list_accounts()
    elif target.target_type == "new_api":
        remote_items = await target_client.list_channels()
    else:
        raise SyncClientError(f"未知同步目标类型：{target.target_type}")

    existing_ids = _existing_remote_ids(db, target)
    candidates: list[RemoteChannelCandidate] = []
    for raw in remote_items:
        try:
            candidate = _remote_candidate_from_target(target, raw)
        except (SyncClientError, ValueError):
            continue
        if candidate.remote_id in existing_ids:
            continue
        candidates.append(candidate)
    return candidates


def _unique_channel_name(db: Any, preferred_name: str) -> str:
    base_name = _clean_text(preferred_name) or "远端渠道"
    existing_names = {
        name
        for (name,) in db.query(Channel.name).all()
    }
    if base_name not in existing_names:
        return base_name
    suffix = 2
    while True:
        candidate = f"{base_name}-{suffix}"
        if candidate not in existing_names:
            return candidate
        suffix += 1


def _add_channel_models(db: Any, channel: Channel, models: list[str]) -> None:
    for model_id in models:
        db.add(ChannelModel(channel_id=channel.id, model_id=model_id, available=True))


def _candidate_summary(candidate: RemoteChannelCandidate) -> dict[str, Any]:
    return {
        "remote_id": candidate.remote_id,
        "remote_name": candidate.remote_name,
        "remote_type": candidate.remote_type,
        "provider_type": candidate.provider_type,
        "base_url": candidate.base_url,
        "enabled": candidate.enabled,
        "models": candidate.models,
        "newapi_groups": candidate.newapi_groups,
        "sub2api_group_ids": candidate.sub2api_group_ids,
        "sub2api_group_labels": candidate.sub2api_group_labels,
        "sub2api_priority": candidate.sub2api_priority,
        "sub2api_concurrency": candidate.sub2api_concurrency,
    }


def apply_candidate_api_key(candidate: RemoteChannelCandidate, api_key: str | None) -> RemoteChannelCandidate:
    cleaned_api_key = _clean_text(api_key)
    if not cleaned_api_key:
        return candidate
    return replace(candidate, api_key=cleaned_api_key)


def _create_channel_from_candidate(db: Any, target_id: int, candidate: RemoteChannelCandidate) -> Channel:
    if not candidate.api_key:
        raise ValueError("缺少 API Key")
    channel = Channel(
        name=_unique_channel_name(db, candidate.remote_name),
        provider_type=candidate.provider_type,
        base_url=candidate.base_url,
        api_key=candidate.api_key,
        enabled=candidate.enabled,
        timeout_seconds=20,
        model_check_interval_minutes=10,
        balance_check_interval_minutes=30,
        probe_model=candidate.probe_model,
        openai_test_mode="chat_completions",
        extractor_vars_json="{}",
    )
    db.add(channel)
    db.flush()
    _add_channel_models(db, channel, candidate.models)

    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target_id,
        remote_type=candidate.remote_type,
        remote_id=candidate.remote_id,
        remote_name=candidate.remote_name,
        sync_enabled=True,
        sub2api_group_ids_json=dumps_group_ids(candidate.sub2api_group_ids),
        sub2api_priority=candidate.sub2api_priority,
        sub2api_concurrency=candidate.sub2api_concurrency,
        newapi_groups=candidate.newapi_groups,
        last_sync_status="success",
        last_sync_error=None,
        last_synced_at=now_utc(),
    )
    db.add(link)
    db.add(AlertRule(channel_id=channel.id))
    db.flush()
    record_sync_event(
        db,
        channel_id=channel.id,
        target_id=target_id,
        link_id=link.id,
        action="remote_import",
        success=True,
        message="success",
        request_payload={"remote_id": candidate.remote_id},
        response_payload=_candidate_summary(candidate),
    )
    return channel


async def import_remote_channels(
    db: Any,
    target: SyncTarget,
    remote_ids: list[str],
    *,
    api_keys_by_remote_id: dict[str, str] | None = None,
    client: Any = None,
) -> RemoteImportResult:
    selected_ids = {_clean_text(remote_id) for remote_id in remote_ids if _clean_text(remote_id)}
    if not selected_ids:
        return RemoteImportResult(0, 0, [], [])

    target_client = client or client_for_target(target)
    candidates = await list_remote_channel_import_candidates(db, target, client=target_client)
    candidates_by_id = {candidate.remote_id: candidate for candidate in candidates}
    api_keys_by_remote_id = api_keys_by_remote_id or {}
    target_id_value = target.id
    imported_channel_ids: list[int] = []
    failures: list[str] = []

    for remote_id in sorted(selected_ids):
        candidate = candidates_by_id.get(remote_id)
        if candidate is None:
            failures.append(f"{remote_id}: 未找到可导入的未关联渠道")
            record_sync_event(
                db,
                channel_id=None,
                target_id=target_id_value,
                action="remote_import",
                success=False,
                message="未找到可导入的未关联渠道",
                request_payload={"remote_id": remote_id},
            )
            db.commit()
            continue

        try:
            candidate = apply_candidate_api_key(candidate, api_keys_by_remote_id.get(candidate.remote_id))
            channel = _create_channel_from_candidate(db, target_id_value, candidate)
            channel_id_value = channel.id
            db.commit()
            imported_channel_ids.append(channel_id_value)
        except Exception as exc:
            db.rollback()
            safe_message = _safe_error_message(str(exc)) or "目标站点同步失败。"
            failures.append(f"{candidate.remote_name}: {safe_message}")
            record_sync_event(
                db,
                channel_id=None,
                target_id=target_id_value,
                action="remote_import",
                success=False,
                message=safe_message,
                request_payload={"remote_id": candidate.remote_id},
                response_payload=candidate.raw,
            )
            db.commit()

    return RemoteImportResult(
        imported_count=len(imported_channel_ids),
        failed_count=len(failures),
        imported_channel_ids=imported_channel_ids,
        failures=failures,
    )


async def create_channel_sync_link(
    db: Any,
    channel: Channel,
    target: SyncTarget,
    group_ids: str | None,
    priority: int,
    concurrency: int,
    *,
    newapi_groups: str | None = None,
    client: Any = None,
) -> ChannelSyncLink:
    remote_name = build_remote_name(target, channel)
    request_payload: Any = None

    try:
        if not target.enabled:
            raise ValueError("目标站点未启用。")
        if _link_exists(db, channel.id, target.id):
            raise ValueError("该渠道已绑定此同步目标。")

        group_id_values = parse_group_ids(group_ids) if target.target_type == "sub2api" else []
        if target.target_type == "sub2api":
            _validate_sub2api_link_settings(priority, concurrency)
        link = ChannelSyncLink(
            channel_id=channel.id,
            target_id=target.id,
            remote_type=_remote_type_for_target(target),
            remote_name=remote_name,
            sub2api_group_ids_json=dumps_group_ids(group_id_values),
            sub2api_priority=priority,
            sub2api_concurrency=concurrency,
            newapi_groups=normalize_newapi_groups(newapi_groups) if target.target_type == "new_api" else "default",
        )
        target_client = client or client_for_target(target)
        models = channel_model_ids(db, channel.id)

        if target.target_type == "sub2api":
            if await target_client.find_account_by_name(remote_name):
                raise ValueError(SAME_NAME_ERROR)
            request_payload = build_sub2api_account_payload(channel, models, link, remote_name=remote_name)
            result = await target_client.create_account(request_payload)
            link.remote_id = _extract_remote_id(_result_response(result))
        elif target.target_type == "new_api":
            if await target_client.find_channel_by_name(remote_name):
                raise ValueError(SAME_NAME_ERROR)
            request_payload = build_newapi_channel_payload(
                channel,
                models,
                remote_name=remote_name,
                newapi_groups=link.newapi_groups,
            )
            result = await target_client.create_channel(request_payload)
            created = await target_client.find_channel_by_name(remote_name)
            if not created:
                raise SyncClientError("new-api 创建成功但无法查询到新渠道 ID。", response=_result_response(result))
            link.remote_id = _extract_remote_id(created)
            response_payload = {
                "create": _result_response(result),
                "lookup": created,
                "remote_id": link.remote_id,
            }
        else:
            raise SyncClientError(f"未知同步目标类型：{target.target_type}")

        if target.target_type != "new_api":
            response_payload = _result_response(result)

        link.last_sync_status = "success"
        link.last_sync_error = None
        link.last_synced_at = now_utc()
        db.add(link)
        db.flush()
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            link_id=link.id,
            action="import_create",
            success=True,
            status_code=_result_status_code(result),
            message="success",
            request_payload=request_payload,
            response_payload=response_payload,
        )
        db.commit()
        db.refresh(link)
        return link
    except ValueError as exc:
        safe_message = _safe_error_message(str(exc))
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            action="import_create",
            success=False,
            message=safe_message,
            request_payload=request_payload,
        )
        db.commit()
        raise
    except SyncClientError as exc:
        safe_message = _safe_error_message(str(exc), exc.status_code)
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            action="import_create",
            success=False,
            status_code=exc.status_code,
            message=safe_message,
            request_payload=request_payload,
            response_payload=exc.response,
        )
        db.commit()
        raise ValueError(str(exc)) from exc


async def sync_existing_link(
    db: Any,
    link: ChannelSyncLink,
    *,
    client: Any = None,
    action: str = "auto_update",
) -> bool:
    if not link.sync_enabled or not link.target.enabled or not link.remote_id:
        return True

    request_payload: Any = None

    try:
        _validate_link_target_type(link)
        target_client = client or client_for_target(link.target)
        models = channel_model_ids(db, link.channel_id)
        remote_name = build_remote_name(link.target, link.channel)

        if link.remote_type == "account":
            existing = await target_client.get_account(link.remote_id)
            request_payload = build_sub2api_account_payload(
                link.channel,
                models,
                link,
                remote_name=remote_name,
                existing_payload=existing,
            )
            result = await target_client.update_account(link.remote_id, request_payload)
        elif link.remote_type == "channel":
            existing = await target_client.get_channel(link.remote_id)
            request_payload = build_newapi_channel_payload(
                link.channel,
                models,
                remote_name=remote_name,
                remote_id=link.remote_id,
                newapi_groups=link.newapi_groups,
                existing_payload=existing,
            )
            result = await target_client.update_channel(request_payload)
        else:
            raise SyncClientError(f"未知远端对象类型：{link.remote_type}")

        link.last_sync_status = "success"
        link.last_sync_error = None
        link.remote_name = remote_name
        link.last_synced_at = now_utc()
        record_sync_event(
            db,
            channel_id=link.channel_id,
            target_id=link.target_id,
            link_id=link.id,
            action=action,
            success=True,
            status_code=_result_status_code(result),
            message="success",
            request_payload=request_payload,
            response_payload=_result_response(result),
        )
        db.commit()
        return True
    except (SyncClientError, ValueError) as exc:
        status_code = exc.status_code if isinstance(exc, SyncClientError) else None
        safe_message = _safe_error_message(str(exc), status_code)
        link.last_sync_status = "failed"
        link.last_sync_error = safe_message
        record_sync_event(
            db,
            channel_id=link.channel_id,
            target_id=link.target_id,
            link_id=link.id,
            action=action,
            success=False,
            status_code=status_code,
            message=safe_message,
            request_payload=request_payload,
            response_payload=exc.response if isinstance(exc, SyncClientError) else None,
        )
        db.commit()
        return False


async def sync_channel_links(db: Any, channel: Channel, *, action: str = "auto_update", client: Any = None) -> tuple[int, int]:
    links = (
        db.query(ChannelSyncLink)
        .filter(
            ChannelSyncLink.channel_id == channel.id,
            ChannelSyncLink.sync_enabled.is_(True),
            ChannelSyncLink.remote_id.isnot(None),
        )
        .all()
    )

    success_count = 0
    failure_count = 0
    for link in links:
        if await sync_existing_link(db, link, action=action, client=client):
            success_count += 1
        else:
            failure_count += 1
    return success_count, failure_count


async def delete_channel_sync_link(
    db: Any,
    link: ChannelSyncLink,
    *,
    delete_remote: bool = False,
    client: Any = None,
) -> tuple[bool, str]:
    if not delete_remote:
        db.delete(link)
        db.commit()
        return True, "同步关联已删除；远端对象未删除。"

    if not link.remote_id:
        db.delete(link)
        db.commit()
        return True, "同步关联已删除；关联中没有远端对象 ID。"

    request_payload = {"remote_type": link.remote_type, "remote_id": link.remote_id}
    response_payload: Any = None
    try:
        _validate_link_target_type(link)
        target_client = client or client_for_target(link.target)
        if link.remote_type == "account":
            result = await target_client.delete_account(link.remote_id)
        elif link.remote_type == "channel":
            result = await target_client.delete_channel(link.remote_id)
        else:
            raise SyncClientError(f"未知远端对象类型：{link.remote_type}")
        response_payload = _result_response(result)
        record_sync_event(
            db,
            channel_id=link.channel_id,
            target_id=link.target_id,
            link_id=link.id,
            action="delete_remote",
            success=True,
            status_code=_result_status_code(result),
            message="success",
            request_payload=request_payload,
            response_payload=response_payload,
        )
        db.delete(link)
        db.commit()
        return True, "同步关联已删除；远端对象已删除。"
    except (SyncClientError, ValueError) as exc:
        status_code = exc.status_code if isinstance(exc, SyncClientError) else None
        safe_message = _safe_error_message(str(exc), status_code)
        link.last_sync_status = "failed"
        link.last_sync_error = safe_message
        record_sync_event(
            db,
            channel_id=link.channel_id,
            target_id=link.target_id,
            link_id=link.id,
            action="delete_remote",
            success=False,
            status_code=status_code,
            message=safe_message,
            request_payload=request_payload,
            response_payload=exc.response if isinstance(exc, SyncClientError) else response_payload,
        )
        db.commit()
        return False, safe_message or "目标站点同步失败。"
