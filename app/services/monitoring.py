from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import AlertEvent, AlertRule, BalanceSnapshot, Channel, ChannelModel, HealthCheck, now_utc
from app.schemas import BalanceResult, ModelInfo, ProbeResult
from app.services.ai_clients import list_models, test_model
from app.services.extractors import query_channel_balance
from app.services.notifications import dispatch_pending_notifications


def ensure_default_alert_rule(db: Session, channel: Channel) -> AlertRule:
    rule = db.query(AlertRule).filter(AlertRule.channel_id == channel.id).first()
    if rule:
        return rule
    rule = AlertRule(channel_id=channel.id)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


async def refresh_channel_models(db: Session, channel: Channel) -> ProbeResult:
    models, result = await list_models(channel)
    db.add(HealthCheck(channel_id=channel.id, check_type="models", success=result.success, status_code=result.status_code, latency_ms=result.latency_ms, message=result.message))
    if result.success:
        change = sync_models(db, channel, models)
        db.commit()
        if change["added_models"] or change["removed_models"]:
            maybe_create_model_change_alert(db, channel, change)
    else:
        db.commit()
        maybe_create_error_alert(db, channel)
    await dispatch_pending_notifications(db)
    return result


async def probe_channel_model(db: Session, channel: Channel, model: str | None = None, prompt: str = "Reply with OK.") -> ProbeResult:
    target_model = model or channel.probe_model
    if not target_model:
        return ProbeResult(False, None, None, "未指定测试模型")
    result = await test_model(channel, target_model, prompt)
    db.add(HealthCheck(channel_id=channel.id, check_type="probe", success=result.success, status_code=result.status_code, latency_ms=result.latency_ms, message=f"{target_model}: {result.message}"))
    db.commit()
    if not result.success:
        maybe_create_error_alert(db, channel)
        await dispatch_pending_notifications(db)
    return result


async def refresh_channel_balance(db: Session, channel: Channel) -> BalanceResult:
    result = await query_channel_balance(channel, channel.extractor_template)
    snapshot = BalanceSnapshot(
        channel_id=channel.id,
        is_valid=result.is_valid,
        invalid_message=result.invalid_message,
        plan_name=result.plan_name,
        remaining=result.remaining,
        used=result.used,
        total=result.total,
        unit=result.unit,
        raw_json=json.dumps(result.raw, ensure_ascii=False) if result.raw is not None else None,
    )
    db.add(snapshot)
    db.add(HealthCheck(channel_id=channel.id, check_type="balance", success=result.is_valid, status_code=None, latency_ms=None, message=balance_check_message(result)))
    db.commit()
    if not result.is_valid:
        maybe_create_error_alert(db, channel, message=result.invalid_message or "余额查询失败")
    else:
        maybe_create_balance_alert(db, channel, result)
    await dispatch_pending_notifications(db)
    return result


def balance_check_message(result: BalanceResult) -> str:
    if not result.is_valid:
        return result.invalid_message or "余额查询失败"
    parts = ["余额查询成功"]
    if result.remaining is not None:
        parts.append(f"余额 {result.remaining:g} {result.unit or ''}".strip())
    if result.used is not None:
        parts.append(f"已用 {result.used:g} {result.unit or ''}".strip())
    if result.total is not None:
        parts.append(f"总额 {result.total:g} {result.unit or ''}".strip())
    if result.plan_name:
        parts.append(f"套餐 {result.plan_name}")
    return "；".join(parts)


async def run_channel_monitor(db: Session, channel: Channel) -> None:
    if not channel.enabled:
        return
    if channel.model_check_enabled:
        if should_run_check(db, channel, "models", channel.model_check_interval_minutes):
            await refresh_channel_models(db, channel)
        if channel.probe_model and should_run_check(db, channel, "probe", channel.model_check_interval_minutes):
            await probe_channel_model(db, channel)
    if channel.balance_check_enabled and channel.extractor_template_id and should_run_balance(db, channel):
        await refresh_channel_balance(db, channel)


def sync_models(db: Session, channel: Channel, models: list[ModelInfo]) -> dict[str, list[str]]:
    seen_ids = {item.model_id for item in models}
    existing = {item.model_id: item for item in db.query(ChannelModel).filter(ChannelModel.channel_id == channel.id).all()}
    old_models = sorted(existing)
    new_models = sorted(seen_ids)
    added = sorted(seen_ids - set(existing))
    removed = sorted(set(existing) - seen_ids)

    for item in models:
        current = existing.get(item.model_id)
        if current is None:
            db.add(ChannelModel(channel_id=channel.id, model_id=item.model_id, owned_by=item.owned_by, available=True))
        else:
            current.available = True
            current.owned_by = item.owned_by
            current.last_seen_at = now_utc()

    for model_id, current in existing.items():
        if model_id not in seen_ids:
            db.delete(current)
    return {"old_models": old_models, "new_models": new_models, "added_models": added, "removed_models": removed}


def maybe_create_error_alert(db: Session, channel: Channel, message: str | None = None) -> None:
    rule = ensure_default_alert_rule(db, channel)
    since = now_utc() - timedelta(minutes=rule.error_window_minutes)
    count = (
        db.query(HealthCheck)
        .filter(HealthCheck.channel_id == channel.id, HealthCheck.success.is_(False), HealthCheck.created_at >= since)
        .count()
    )
    if count < rule.error_threshold:
        return
    if _in_cooldown(db, channel.id, "error_threshold", rule.cooldown_minutes):
        return
    payload = {"error_count": count, "window_minutes": rule.error_window_minutes}
    db.add(AlertEvent(channel_id=channel.id, alert_type="error_threshold", severity="critical", message=message or f"{rule.error_window_minutes} 分钟内错误 {count} 次", payload_json=json.dumps(payload, ensure_ascii=False)))
    db.commit()


def maybe_create_model_change_alert(db: Session, channel: Channel, change: dict[str, list[str]]) -> None:
    rule = ensure_default_alert_rule(db, channel)
    if not rule.model_change_enabled or _in_cooldown(db, channel.id, "model_change", rule.cooldown_minutes):
        return
    pieces = []
    added = change["added_models"]
    removed = change["removed_models"]
    if added:
        pieces.append(f"新增 {len(added)} 个")
    if removed:
        pieces.append(f"移除 {len(removed)} 个")
    payload = {
        "added_models": added,
        "removed_models": removed,
        "old_models": change["old_models"],
        "new_models": change["new_models"],
    }
    db.add(AlertEvent(channel_id=channel.id, alert_type="model_change", severity="warning", message="模型列表变动：" + "，".join(pieces), payload_json=json.dumps(payload, ensure_ascii=False)))
    db.commit()


def maybe_create_balance_alert(db: Session, channel: Channel, result: BalanceResult) -> None:
    rule = ensure_default_alert_rule(db, channel)
    if not rule.balance_threshold_enabled or rule.balance_threshold is None or result.remaining is None:
        return
    if result.remaining > rule.balance_threshold:
        return
    if _in_cooldown(db, channel.id, "balance_threshold", rule.cooldown_minutes):
        return
    payload = {"remaining": result.remaining, "unit": result.unit, "threshold": rule.balance_threshold}
    db.add(AlertEvent(channel_id=channel.id, alert_type="balance_threshold", severity="critical", message=f"余额 {result.remaining:g} {result.unit or ''} 已低于阈值 {rule.balance_threshold:g}", payload_json=json.dumps(payload, ensure_ascii=False)))
    db.commit()


def _in_cooldown(db: Session, channel_id: int, alert_type: str, cooldown_minutes: int) -> bool:
    since = now_utc() - timedelta(minutes=cooldown_minutes)
    return (
        db.query(AlertEvent)
        .filter(AlertEvent.channel_id == channel_id, AlertEvent.alert_type == alert_type, AlertEvent.created_at >= since)
        .first()
        is not None
    )


def recent_status(db: Session, channel_id: int) -> HealthCheck | None:
    return db.query(HealthCheck).filter(HealthCheck.channel_id == channel_id).order_by(HealthCheck.created_at.desc()).first()


def latest_balance(db: Session, channel_id: int) -> BalanceSnapshot | None:
    return db.query(BalanceSnapshot).filter(BalanceSnapshot.channel_id == channel_id).order_by(BalanceSnapshot.created_at.desc()).first()


def should_run_check(db: Session, channel: Channel, check_type: str, interval_minutes: int) -> bool:
    latest = (
        db.query(HealthCheck)
        .filter(HealthCheck.channel_id == channel.id, HealthCheck.check_type == check_type)
        .order_by(HealthCheck.created_at.desc())
        .first()
    )
    if latest is None:
        return True
    return latest.created_at <= now_utc() - timedelta(minutes=max(interval_minutes, 1))


def should_run_balance(db: Session, channel: Channel) -> bool:
    latest = latest_balance(db, channel.id)
    if latest is None:
        return True
    return latest.created_at <= now_utc() - timedelta(minutes=max(channel.balance_check_interval_minutes, 1))


def run_async(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)
