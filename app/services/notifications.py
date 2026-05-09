from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from string import Template
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import AlertEvent, Channel, NotificationChannel
from app.time_utils import format_dt


def render_text(template: str, values: dict[str, Any]) -> str:
    safe_values = {key: "" if value is None else value for key, value in values.items()}
    return Template(template).safe_substitute(safe_values)


def event_values(db: Session, event: AlertEvent) -> dict[str, Any]:
    payload = {}
    try:
        payload = json.loads(event.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    channel_name = ""
    if event.channel_id:
        channel = db.get(Channel, event.channel_id)
        channel_name = channel.name if channel else ""
    values = {
        "alert_type": event.alert_type,
        "channel_name": channel_name,
        "severity": event.severity,
        "message": event.message,
        "created_at": format_dt(event.created_at),
    }
    values.update(payload)
    return values


async def dispatch_pending_notifications(db: Session) -> None:
    events = db.query(AlertEvent).filter(AlertEvent.notification_status.in_(["pending", "failed"])).order_by(AlertEvent.created_at.asc()).limit(20).all()
    if not events:
        return
    channels = db.query(NotificationChannel).filter(NotificationChannel.enabled.is_(True)).all()
    if not channels:
        for event in events:
            event.notification_status = "skipped"
            event.notification_error = "未配置启用的通知渠道"
        db.commit()
        return

    for event in events:
        errors = []
        values = event_values(db, event)
        for channel in channels:
            try:
                await send_notification(channel, values)
            except Exception as exc:
                errors.append(f"{channel.name}: {exc}")
        if errors:
            event.notification_status = "failed"
            event.notification_error = "\n".join(errors)
        else:
            event.notification_status = "sent"
            event.notification_error = None
    db.commit()


async def send_notification(channel: NotificationChannel, values: dict[str, Any]) -> None:
    config = json.loads(channel.config_json or "{}")
    if channel.channel_type == "email":
        send_email(config, values)
    elif channel.channel_type == "http":
        await send_http(config, values)
    else:
        raise ValueError(f"未知通知渠道类型：{channel.channel_type}")


def send_email(config: dict[str, Any], values: dict[str, Any]) -> None:
    host = config.get("host")
    port = int(config.get("port") or 587)
    username = config.get("username")
    password = config.get("password")
    sender = config.get("sender") or username
    recipients = [item.strip() for item in str(config.get("recipients", "")).replace(";", ",").split(",") if item.strip()]
    subject = render_text(config.get("subject", "[AI渠道告警] $channel_name $alert_type"), values)
    body = render_text(config.get("body", "$message"), values)
    if not host or not sender or not recipients:
        raise ValueError("SMTP host、sender、recipients 必填")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    use_tls = bool(config.get("use_tls", True))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password or "")
        smtp.send_message(message)


async def send_http(config: dict[str, Any], values: dict[str, Any]) -> None:
    method = str(config.get("method", "POST")).upper()
    url = render_text(config.get("url", ""), values)
    if not url:
        raise ValueError("HTTP 通知 URL 必填")
    headers = {key: render_text(str(value), values) for key, value in (config.get("headers") or {}).items()}
    body_template = config.get("body", "")
    body = render_text(body_template, values) if isinstance(body_template, str) else body_template
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(method, url, headers=headers, content=body if isinstance(body, str) else None, json=body if isinstance(body, (dict, list)) else None)
    if response.status_code >= 400:
        raise ValueError(f"HTTP {response.status_code}: {response.text[:200]}")
