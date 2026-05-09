from __future__ import annotations

import json
from typing import Any

from app.models import Channel, ChannelModel, ChannelSyncLink, SyncEvent, SyncTarget, now_utc
from app.services.sync_clients import SyncClientError, SyncClientResult, client_for_target
from app.services.sync_payloads import (
    build_newapi_channel_payload,
    build_remote_name,
    build_sub2api_account_payload,
    dumps_group_ids,
    parse_group_ids,
    redact_sensitive,
)


SAME_NAME_ERROR = "目标站点已存在同名对象，请先手动改名或删除后再导入。"


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


def _dump_event_json(value: Any) -> str | None:
    if value is None:
        return None
    safe_value = redact_sensitive(_json_safe(value))
    return json.dumps(safe_value, ensure_ascii=False)


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


async def create_channel_sync_link(
    db: Any,
    channel: Channel,
    target: SyncTarget,
    group_ids: str | None,
    priority: int,
    concurrency: int,
    *,
    client: Any = None,
) -> ChannelSyncLink:
    remote_name = build_remote_name(target, channel)
    request_payload: Any = None

    try:
        if not target.enabled:
            raise ValueError("目标站点未启用。")
        if _link_exists(db, channel.id, target.id):
            raise ValueError("该渠道已绑定此同步目标。")

        group_id_values = parse_group_ids(group_ids)
        link = ChannelSyncLink(
            channel_id=channel.id,
            target_id=target.id,
            remote_type=_remote_type_for_target(target),
            remote_name=remote_name,
            sub2api_group_ids_json=dumps_group_ids(group_id_values),
            sub2api_priority=priority,
            sub2api_concurrency=concurrency,
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
            request_payload = build_newapi_channel_payload(channel, models, remote_name=remote_name)
            result = await target_client.create_channel(request_payload)
            created = await target_client.find_channel_by_name(remote_name)
            if not created:
                raise SyncClientError("new-api 创建成功但无法查询到新渠道 ID。", response=_result_response(result))
            link.remote_id = _extract_remote_id(created)
        else:
            raise SyncClientError(f"未知同步目标类型：{target.target_type}")

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
            response_payload=_result_response(result),
        )
        db.commit()
        db.refresh(link)
        return link
    except ValueError as exc:
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            action="import_create",
            success=False,
            message=str(exc),
            request_payload=request_payload,
        )
        db.commit()
        raise
    except SyncClientError as exc:
        record_sync_event(
            db,
            channel_id=channel.id,
            target_id=target.id,
            action="import_create",
            success=False,
            status_code=exc.status_code,
            message=str(exc),
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

    target_client = client or client_for_target(link.target)
    models = channel_model_ids(db, link.channel_id)
    request_payload: Any = None

    try:
        if link.remote_type == "account":
            existing = await target_client.get_account(link.remote_id)
            request_payload = build_sub2api_account_payload(
                link.channel,
                models,
                link,
                remote_name=link.remote_name or build_remote_name(link.target, link.channel),
                existing_payload=existing,
            )
            result = await target_client.update_account(link.remote_id, request_payload)
        elif link.remote_type == "channel":
            existing = await target_client.get_channel(link.remote_id)
            request_payload = build_newapi_channel_payload(
                link.channel,
                models,
                remote_name=link.remote_name or build_remote_name(link.target, link.channel),
                remote_id=link.remote_id,
                existing_payload=existing,
            )
            result = await target_client.update_channel(request_payload)
        else:
            raise SyncClientError(f"未知远端对象类型：{link.remote_type}")

        link.last_sync_status = "success"
        link.last_sync_error = None
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
    except SyncClientError as exc:
        link.last_sync_status = "failed"
        link.last_sync_error = str(exc)
        record_sync_event(
            db,
            channel_id=link.channel_id,
            target_id=link.target_id,
            link_id=link.id,
            action=action,
            success=False,
            status_code=exc.status_code,
            message=str(exc),
            request_payload=request_payload,
            response_payload=exc.response,
        )
        db.commit()
        return False


async def sync_channel_links(db: Any, channel: Channel, *, action: str = "auto_update") -> tuple[int, int]:
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
        if await sync_existing_link(db, link, action=action):
            success_count += 1
        else:
            failure_count += 1
    return success_count, failure_count
