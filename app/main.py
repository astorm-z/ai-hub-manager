from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import timedelta
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, init_db
from app.models import AlertEvent, AlertRule, BalanceSnapshot, Channel, ChannelModel, ChannelSyncLink, ExtractorTemplate, HealthCheck, NotificationChannel, SyncTarget, User, now_utc
from app.services.auth import authenticate, create_admin, has_admin, make_session_token, read_session_token
from app.services.extractors import query_channel_balance, seed_builtin_extractors
from app.services.monitoring import ensure_default_alert_rule, latest_balance, probe_channel_model, recent_status, refresh_channel_balance, refresh_channel_models
from app.services.notifications import send_notification
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.channel_sync import create_channel_sync_link, sync_channel_links, sync_existing_link
from app.services.sync_clients import SyncClientError, client_for_target
from app.time_utils import format_dt


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["format_dt"] = format_dt
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    db = next(get_db())
    try:
        seed_builtin_extractors(db)
        cleanup_removed_model_rows(db)
    finally:
        db.close()
    start_scheduler()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_scheduler()


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def flash_redirect(path: str, message: str, level: str = "success") -> RedirectResponse:
    response = redirect(path)
    payload = json.dumps({"message": message, "level": level}, ensure_ascii=False).encode("utf-8")
    response.set_cookie("flash", urlsafe_b64encode(payload).decode("ascii"), httponly=True, samesite="lax")
    return response


def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    flash = None
    raw_flash = request.cookies.get("flash")
    if raw_flash:
        try:
            flash = json.loads(urlsafe_b64decode(raw_flash.encode("ascii")).decode("utf-8"))
        except Exception:
            flash = None
    context.update({"request": request, "app_name": settings.app_name, "flash": flash})
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    if raw_flash:
        response.delete_cookie("flash")
    return response


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User | None:
    uid = read_session_token(request.cookies.get("session"))
    if uid is None:
        return None
    return db.get(User, uid)


def require_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    return user


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: HTTPException) -> RedirectResponse:
    return redirect("/login")


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    if has_admin(db):
        return redirect("/login")
    return render(request, "setup.html", {})


@app.post("/setup")
def setup_admin(
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> RedirectResponse:
    if has_admin(db):
        return redirect("/login")
    if len(username.strip()) < 2 or len(password) < 8:
        return flash_redirect("/setup", "用户名至少 2 位，密码至少 8 位。", "error")
    create_admin(db, username, password)
    return flash_redirect("/login", "管理员已创建，请登录。")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    if not has_admin(db):
        return redirect("/setup")
    if current_user(request, db):
        return redirect("/")
    return render(request, "login.html", {})


@app.post("/login")
def login(
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> RedirectResponse:
    user = authenticate(db, username, password)
    if not user:
        return flash_redirect("/login", "用户名或密码错误。", "error")
    response = redirect("/")
    response.set_cookie("session", make_session_token(user.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/logout")
def logout() -> RedirectResponse:
    response = redirect("/login")
    response.delete_cookie("session")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    channels = db.query(Channel).order_by(Channel.created_at.desc()).all()
    rows = [_channel_row(db, channel) for channel in channels]
    status_lanes = build_status_lanes(db, channels)
    stats = {
        "channels": len(channels),
        "enabled": sum(1 for item in channels if item.enabled),
        "alerts": db.query(AlertEvent).filter(AlertEvent.acknowledged.is_(False)).count(),
        "notifications": db.query(NotificationChannel).filter(NotificationChannel.enabled.is_(True)).count(),
    }
    events = db.query(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(8).all()
    return render(request, "dashboard.html", {"user": user, "rows": rows, "stats": stats, "events": events, "status_lanes": status_lanes})


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    channels = db.query(Channel).order_by(Channel.created_at.desc()).all()
    return render(request, "channels.html", {"rows": [_channel_row(db, item) for item in channels], "user": user})


@app.get("/channels/new", response_class=HTMLResponse)
def new_channel_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    extractors = db.query(ExtractorTemplate).order_by(ExtractorTemplate.builtin.desc(), ExtractorTemplate.name).all()
    return render(request, "channel_form.html", {"channel": None, "extractors": extractors, "user": user})


@app.post("/channels")
def create_channel(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    provider_type: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    api_key: Annotated[str, Form()],
    enabled: Annotated[bool | None, Form()] = None,
    timeout_seconds: Annotated[float, Form()] = 20,
    model_check_interval_minutes: Annotated[int, Form()] = 10,
    balance_check_interval_minutes: Annotated[int, Form()] = 30,
    probe_model: Annotated[str, Form()] = "",
    openai_test_mode: Annotated[str, Form()] = "chat_completions",
    extractor_template_id: Annotated[str, Form()] = "",
    extractor_vars_json: Annotated[str, Form()] = "{}",
) -> RedirectResponse:
    error = _validate_json(extractor_vars_json)
    if error:
        return flash_redirect("/channels/new", error, "error")
    channel = Channel(
        name=name.strip(),
        provider_type=provider_type,
        base_url=base_url.strip().rstrip("/"),
        api_key=api_key.strip(),
        enabled=bool(enabled),
        timeout_seconds=timeout_seconds,
        model_check_interval_minutes=model_check_interval_minutes,
        balance_check_interval_minutes=balance_check_interval_minutes,
        probe_model=probe_model.strip() or None,
        openai_test_mode=openai_test_mode,
        extractor_template_id=_optional_int(extractor_template_id),
        extractor_vars_json=extractor_vars_json or "{}",
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    ensure_default_alert_rule(db, channel)
    return flash_redirect(f"/channels/{channel.id}", "渠道已创建。")


@app.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel_detail(request: Request, channel_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    channel = _get_channel(db, channel_id)
    extractors = db.query(ExtractorTemplate).order_by(ExtractorTemplate.builtin.desc(), ExtractorTemplate.name).all()
    rule = ensure_default_alert_rule(db, channel)
    models = db.query(ChannelModel).filter(ChannelModel.channel_id == channel.id).order_by(ChannelModel.model_id).all()
    checks = db.query(HealthCheck).filter(HealthCheck.channel_id == channel.id).order_by(HealthCheck.created_at.desc()).limit(15).all()
    balances = db.query(BalanceSnapshot).filter(BalanceSnapshot.channel_id == channel.id).order_by(BalanceSnapshot.created_at.desc()).limit(10).all()
    check_rows = build_check_rows(db, checks)
    context = {"channel": channel, "extractors": extractors, "rule": rule, "models": models, "checks": check_rows, "balances": balances, "user": user}
    context.update(channel_sync_context(db, channel))
    return render(request, "channel_detail.html", context)


@app.post("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    provider_type: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    api_key: Annotated[str, Form()],
    enabled: Annotated[bool | None, Form()] = None,
    timeout_seconds: Annotated[float, Form()] = 20,
    model_check_interval_minutes: Annotated[int, Form()] = 10,
    balance_check_interval_minutes: Annotated[int, Form()] = 30,
    probe_model: Annotated[str, Form()] = "",
    openai_test_mode: Annotated[str, Form()] = "chat_completions",
    extractor_template_id: Annotated[str, Form()] = "",
    extractor_vars_json: Annotated[str, Form()] = "{}",
) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    error = _validate_json(extractor_vars_json)
    if error:
        return flash_redirect(f"/channels/{channel.id}", error, "error")
    channel.name = name.strip()
    channel.provider_type = provider_type
    channel.base_url = base_url.strip().rstrip("/")
    channel.api_key = api_key.strip()
    channel.enabled = bool(enabled)
    channel.timeout_seconds = timeout_seconds
    channel.model_check_interval_minutes = model_check_interval_minutes
    channel.balance_check_interval_minutes = balance_check_interval_minutes
    channel.probe_model = probe_model.strip() or None
    channel.openai_test_mode = openai_test_mode
    channel.extractor_template_id = _optional_int(extractor_template_id)
    channel.extractor_vars_json = extractor_vars_json or "{}"
    redirect_path = f"/channels/{channel.id}"
    sync_channel = SimpleNamespace(id=channel.id)
    db.commit()
    success, failed = await sync_channel_links(db, sync_channel, action="auto_update")
    if failed > 0:
        return flash_redirect(redirect_path, f"渠道已保存；同步成功 {success} 个，失败 {failed} 个。", "error")
    if success > 0:
        return flash_redirect(redirect_path, f"渠道已保存；已同步 {success} 个目标。")
    return flash_redirect(redirect_path, "渠道已保存。")


@app.post("/channels/{channel_id}/delete")
def delete_channel(channel_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    db.delete(channel)
    db.commit()
    return flash_redirect("/channels", "渠道已删除。")


@app.post("/channels/{channel_id}/refresh-models")
async def refresh_models(channel_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    redirect_path = f"/channels/{channel.id}"
    sync_channel = SimpleNamespace(id=channel.id)
    result = await refresh_channel_models(db, channel)
    if result.success:
        await sync_channel_links(db, sync_channel, action="auto_update")
    level = "success" if result.success else "error"
    return flash_redirect(redirect_path, result.message, level)


@app.post("/channels/{channel_id}/sync-links")
async def create_sync_link(
    channel_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    target_id: Annotated[int, Form()],
    sub2api_group_ids: Annotated[str, Form()] = "",
    sub2api_priority: Annotated[int, Form()] = 50,
    sub2api_concurrency: Annotated[int, Form()] = 3,
) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    target = _get_sync_target(db, target_id)
    try:
        await create_channel_sync_link(db, channel, target, sub2api_group_ids, sub2api_priority, sub2api_concurrency)
    except ValueError as exc:
        return flash_redirect(f"/channels/{channel.id}", str(exc), "error")
    return flash_redirect(f"/channels/{channel.id}", "目标站点导入成功。")


@app.post("/channels/{channel_id}/sync-links/{link_id}/sync")
async def sync_channel_link(channel_id: int, link_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    link = _get_channel_sync_link(db, channel_id, link_id)
    if await sync_existing_link(db, link, action="manual_update"):
        return flash_redirect(f"/channels/{channel_id}", "同步成功。")
    return flash_redirect(f"/channels/{channel_id}", str(link.__dict__.get("last_sync_error") or "同步失败。"), "error")


@app.post("/channels/{channel_id}/sync-links/{link_id}/toggle")
def toggle_channel_sync_link(channel_id: int, link_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    link = _get_channel_sync_link(db, channel_id, link_id)
    link.sync_enabled = not link.sync_enabled
    message = "同步关联已恢复。" if link.sync_enabled else "同步关联已暂停。"
    db.commit()
    return flash_redirect(f"/channels/{channel_id}", message)


@app.post("/channels/{channel_id}/sync-links/{link_id}/delete")
def delete_channel_sync_link(channel_id: int, link_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    link = _get_channel_sync_link(db, channel_id, link_id)
    db.delete(link)
    db.commit()
    return flash_redirect(f"/channels/{channel_id}", "同步关联已删除；远端对象未删除。")


@app.post("/channels/{channel_id}/test-model")
async def test_channel_model(
    channel_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    model: Annotated[str, Form()],
    prompt: Annotated[str, Form()] = "Reply with OK.",
) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    result = await probe_channel_model(db, channel, model, prompt)
    level = "success" if result.success else "error"
    return flash_redirect(f"/channels/{channel.id}", f"{model}: {result.message}", level)


@app.post("/channels/{channel_id}/refresh-balance")
async def refresh_balance(channel_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    result = await refresh_channel_balance(db, channel)
    if result.is_valid:
        message = f"余额：{result.remaining if result.remaining is not None else '-'} {result.unit or ''}"
        return flash_redirect(f"/channels/{channel.id}", message)
    return flash_redirect(f"/channels/{channel.id}", result.invalid_message or "余额查询失败", "error")


@app.post("/channels/{channel_id}/alert-rule")
def save_alert_rule(
    channel_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    error_window_minutes: Annotated[int, Form()],
    error_threshold: Annotated[int, Form()],
    model_change_enabled: Annotated[bool | None, Form()] = None,
    balance_threshold_enabled: Annotated[bool | None, Form()] = None,
    balance_threshold: Annotated[str, Form()] = "",
    cooldown_minutes: Annotated[int, Form()] = 30,
) -> RedirectResponse:
    channel = _get_channel(db, channel_id)
    rule = ensure_default_alert_rule(db, channel)
    rule.error_window_minutes = error_window_minutes
    rule.error_threshold = error_threshold
    rule.model_change_enabled = bool(model_change_enabled)
    rule.balance_threshold_enabled = bool(balance_threshold_enabled)
    rule.balance_threshold = _optional_float(balance_threshold)
    rule.cooldown_minutes = cooldown_minutes
    db.commit()
    return flash_redirect(f"/channels/{channel.id}", "告警策略已保存。")


@app.get("/extractors", response_class=HTMLResponse)
def extractors_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    extractors = db.query(ExtractorTemplate).order_by(ExtractorTemplate.builtin.desc(), ExtractorTemplate.name).all()
    return render(request, "extractors.html", {"extractors": extractors, "user": user})


@app.post("/extractors")
def create_extractor(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    template_json: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        json.loads(template_json)
    except json.JSONDecodeError as exc:
        return flash_redirect("/extractors", f"JSON 无效：{exc}", "error")
    db.add(ExtractorTemplate(name=name.strip(), description=description.strip() or None, template_json=template_json, builtin=False))
    db.commit()
    return flash_redirect("/extractors", "提取器已创建。")


@app.post("/extractors/{extractor_id}/delete")
def delete_extractor(extractor_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    extractor = db.get(ExtractorTemplate, extractor_id)
    if not extractor:
        raise HTTPException(status_code=404)
    if extractor.builtin:
        return flash_redirect("/extractors", "内置提取器不能删除。", "error")
    db.delete(extractor)
    db.commit()
    return flash_redirect("/extractors", "提取器已删除。")


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    channels = db.query(NotificationChannel).order_by(NotificationChannel.created_at.desc()).all()
    return render(request, "notifications.html", {"channels": channels, "user": user, "mode": "create", "form": default_notification_form()})


@app.post("/notifications")
def create_notification(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    channel_type: Annotated[str, Form()],
    enabled: Annotated[bool | None, Form()] = None,
    email_host: Annotated[str, Form()] = "",
    email_port: Annotated[int, Form()] = 587,
    email_use_tls: Annotated[bool | None, Form()] = None,
    email_username: Annotated[str, Form()] = "",
    email_password: Annotated[str, Form()] = "",
    email_sender: Annotated[str, Form()] = "",
    email_recipients: Annotated[str, Form()] = "",
    email_subject: Annotated[str, Form()] = "[AI渠道告警] $channel_name $alert_type",
    email_body: Annotated[str, Form()] = "$message",
    http_method: Annotated[str, Form()] = "POST",
    http_url: Annotated[str, Form()] = "",
    http_headers_json: Annotated[str, Form()] = "{}",
    http_body: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        config = build_notification_config_from_form(locals())
    except ValueError as exc:
        return flash_redirect("/notifications", str(exc), "error")
    config_json = json.dumps(config, ensure_ascii=False)
    db.add(NotificationChannel(name=name.strip(), channel_type=channel_type, enabled=bool(enabled), config_json=config_json))
    db.commit()
    return flash_redirect("/notifications", "通知渠道已创建。")


@app.get("/notifications/{notification_id}/edit", response_class=HTMLResponse)
def edit_notification_page(notification_id: int, request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    item = _get_notification(db, notification_id)
    channels = db.query(NotificationChannel).order_by(NotificationChannel.created_at.desc()).all()
    return render(request, "notifications.html", {"channels": channels, "user": user, "mode": "edit", "editing": item, "form": notification_form_from_item(item)})


@app.post("/notifications/{notification_id}")
def update_notification(
    notification_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    channel_type: Annotated[str, Form()],
    enabled: Annotated[bool | None, Form()] = None,
    email_host: Annotated[str, Form()] = "",
    email_port: Annotated[int, Form()] = 587,
    email_use_tls: Annotated[bool | None, Form()] = None,
    email_username: Annotated[str, Form()] = "",
    email_password: Annotated[str, Form()] = "",
    email_sender: Annotated[str, Form()] = "",
    email_recipients: Annotated[str, Form()] = "",
    email_subject: Annotated[str, Form()] = "[AI渠道告警] $channel_name $alert_type",
    email_body: Annotated[str, Form()] = "$message",
    http_method: Annotated[str, Form()] = "POST",
    http_url: Annotated[str, Form()] = "",
    http_headers_json: Annotated[str, Form()] = "{}",
    http_body: Annotated[str, Form()] = "",
) -> RedirectResponse:
    item = _get_notification(db, notification_id)
    try:
        config = build_notification_config_from_form(locals(), existing=notification_config(item))
    except ValueError as exc:
        return flash_redirect(f"/notifications/{item.id}/edit", str(exc), "error")
    item.name = name.strip()
    item.channel_type = channel_type
    item.enabled = bool(enabled)
    item.config_json = json.dumps(config, ensure_ascii=False)
    db.commit()
    return flash_redirect("/notifications", "通知渠道已保存。")


@app.post("/notifications/{notification_id}/delete")
def delete_notification(notification_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    item = _get_notification(db, notification_id)
    db.delete(item)
    db.commit()
    return flash_redirect("/notifications", "通知渠道已删除。")


@app.post("/notifications/{notification_id}/test")
async def test_notification(notification_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    item = _get_notification(db, notification_id)
    values = {
        "alert_type": "test",
        "channel_name": "测试渠道",
        "severity": "info",
        "message": "这是一条来自 AI 中转渠道管理的测试通知。",
        "created_at": format_dt(now_utc()),
    }
    try:
        await send_notification(item, values)
    except Exception as exc:
        return flash_redirect("/notifications", f"测试失败：{exc}", "error")
    return flash_redirect("/notifications", "测试通知已发送。")


@app.get("/sync-targets", response_class=HTMLResponse)
def sync_targets_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    targets = db.query(SyncTarget).order_by(SyncTarget.created_at.desc()).all()
    return render(
        request,
        "sync_targets.html",
        {"targets": build_sync_target_rows(db, targets), "user": user, "mode": "create", "form": sync_target_form_from_item()},
    )


@app.post("/sync-targets")
def create_sync_target(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    target_type: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    name_prefix: Annotated[str, Form()] = "union_",
    enabled: Annotated[bool | None, Form()] = None,
    sub2api_admin_api_key: Annotated[str, Form()] = "",
    newapi_authorization: Annotated[str, Form()] = "",
    newapi_user: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        config = build_sync_target_config(target_type, sub2api_admin_api_key, newapi_authorization, newapi_user)
    except ValueError as exc:
        return flash_redirect("/sync-targets", str(exc), "error")

    target = SyncTarget(
        name=name.strip(),
        target_type=target_type,
        base_url=base_url.strip().rstrip("/"),
        enabled=bool(enabled),
        name_prefix=name_prefix.strip() or "union_",
        auth_config_json=json.dumps(config, ensure_ascii=False),
        default_config_json="{}",
    )
    try:
        db.add(target)
        db.commit()
    except IntegrityError:
        db.rollback()
        return flash_redirect("/sync-targets", "同步目标名称已存在。", "error")
    except Exception:
        db.rollback()
        return flash_redirect("/sync-targets", "目标站点创建失败。", "error")
    return flash_redirect("/sync-targets", "目标站点已创建。")


@app.get("/sync-targets/{target_id}/edit", response_class=HTMLResponse)
def edit_sync_target_page(target_id: int, request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    target = _get_sync_target(db, target_id)
    targets = db.query(SyncTarget).order_by(SyncTarget.created_at.desc()).all()
    return render(
        request,
        "sync_targets.html",
        {
            "targets": build_sync_target_rows(db, targets),
            "user": user,
            "mode": "edit",
            "editing": target,
            "form": sync_target_form_from_item(target),
        },
    )


@app.post("/sync-targets/{target_id}")
def update_sync_target(
    target_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    name: Annotated[str, Form()],
    target_type: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    name_prefix: Annotated[str, Form()] = "union_",
    enabled: Annotated[bool | None, Form()] = None,
    sub2api_admin_api_key: Annotated[str, Form()] = "",
    newapi_authorization: Annotated[str, Form()] = "",
    newapi_user: Annotated[str, Form()] = "",
) -> RedirectResponse:
    target = _get_sync_target(db, target_id)
    try:
        config = build_sync_target_config(
            target_type,
            sub2api_admin_api_key,
            newapi_authorization,
            newapi_user,
            existing=sync_target_auth_config(target),
        )
    except ValueError as exc:
        return flash_redirect(f"/sync-targets/{target.id}/edit", str(exc), "error")

    target.name = name.strip()
    target.target_type = target_type
    target.base_url = base_url.strip().rstrip("/")
    target.enabled = bool(enabled)
    target.name_prefix = name_prefix.strip() or "union_"
    target.auth_config_json = json.dumps(config, ensure_ascii=False)
    edit_path = f"/sync-targets/{target_id}/edit"
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return flash_redirect(edit_path, "同步目标名称已存在。", "error")
    except Exception:
        db.rollback()
        return flash_redirect(edit_path, "目标站点保存失败。", "error")
    return flash_redirect("/sync-targets", "目标站点已保存。")


@app.post("/sync-targets/{target_id}/delete")
def delete_sync_target(target_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    target = _get_sync_target(db, target_id)
    linked = db.query(ChannelSyncLink).filter(ChannelSyncLink.target_id == target.id).first()
    if linked:
        return flash_redirect("/sync-targets", "目标站点已有渠道关联，请先删除关联。", "error")
    try:
        db.delete(target)
        db.commit()
    except Exception:
        db.rollback()
        return flash_redirect("/sync-targets", "目标站点删除失败。", "error")
    return flash_redirect("/sync-targets", "目标站点已删除。")


@app.post("/sync-targets/{target_id}/test")
async def test_sync_target(target_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    target = _get_sync_target(db, target_id)
    try:
        await client_for_target(target).test_connection()
    except SyncClientError as exc:
        return flash_redirect("/sync-targets", sync_target_test_error_message(exc), "error")
    return flash_redirect("/sync-targets", "目标站点连接测试成功。")


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    events = db.query(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(200).all()
    channel_names = {item.id: item.name for item in db.query(Channel).all()}
    unread_count = db.query(AlertEvent).filter(AlertEvent.acknowledged.is_(False)).count()
    return render(request, "alerts.html", {"events": events, "channel_names": channel_names, "unread_count": unread_count, "user": user})


@app.post("/alerts/ack-all")
def ack_all_alerts(db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    count = mark_all_alerts_acknowledged(db)
    return flash_redirect("/alerts", f"已标记 {count} 条告警为已读。")


@app.post("/alerts/{event_id}/ack")
def ack_alert(event_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]) -> RedirectResponse:
    event = db.get(AlertEvent, event_id)
    if not event:
        raise HTTPException(status_code=404)
    event.acknowledged = True
    db.commit()
    return flash_redirect("/alerts", "告警已标记已读。")


def _channel_row(db: Session, channel: Channel) -> dict[str, Any]:
    status = recent_status(db, channel.id)
    balance = latest_balance(db, channel.id)
    model_count = db.query(ChannelModel).filter(ChannelModel.channel_id == channel.id, ChannelModel.available.is_(True)).count()
    return {"channel": channel, "status": status, "balance": balance, "model_count": model_count}


def build_check_rows(db: Session, checks: list[HealthCheck]) -> list[dict[str, Any]]:
    return [{"check": item, "balance": find_balance_for_check(db, item) if item.check_type == "balance" else None} for item in checks]


def channel_sync_context(db: Session, channel: Channel) -> dict[str, Any]:
    sync_targets = db.query(SyncTarget).filter(SyncTarget.enabled.is_(True)).order_by(SyncTarget.name).all()
    sync_links = (
        db.query(ChannelSyncLink)
        .filter(ChannelSyncLink.channel_id == channel.id)
        .order_by(ChannelSyncLink.created_at.desc())
        .all()
    )
    return {
        "sync_targets": sync_targets,
        "sync_links": sync_links,
        "linked_target_ids": {link.target_id for link in sync_links},
    }


def find_balance_for_check(db: Session, check: HealthCheck) -> BalanceSnapshot | None:
    window_start = check.created_at - timedelta(seconds=5)
    window_end = check.created_at + timedelta(seconds=5)
    return (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.channel_id == check.channel_id, BalanceSnapshot.created_at >= window_start, BalanceSnapshot.created_at <= window_end)
        .order_by(BalanceSnapshot.created_at.desc())
        .first()
    )


def balance_snapshot_text(snapshot: BalanceSnapshot | None) -> str:
    if snapshot is None:
        return ""
    if not snapshot.is_valid:
        return snapshot.invalid_message or "余额查询失败"
    parts = []
    if snapshot.remaining is not None:
        parts.append(f"余额 {snapshot.remaining:g} {snapshot.unit or ''}".strip())
    if snapshot.used is not None:
        parts.append(f"已用 {snapshot.used:g} {snapshot.unit or ''}".strip())
    if snapshot.total is not None:
        parts.append(f"总额 {snapshot.total:g} {snapshot.unit or ''}".strip())
    if snapshot.plan_name:
        parts.append(f"套餐 {snapshot.plan_name}")
    return "；".join(parts)


def build_status_lanes(db: Session, channels: list[Channel], hours: int = 12, bucket_minutes: int = 5) -> list[dict[str, Any]]:
    bucket_count = max(1, int(hours * 60 / bucket_minutes))
    end_time = align_time_to_bucket(now_utc(), bucket_minutes)
    start_time = end_time - timedelta(minutes=bucket_count * bucket_minutes)
    lanes: list[dict[str, Any]] = []

    for channel in channels:
        checks = (
            db.query(HealthCheck)
            .filter(HealthCheck.channel_id == channel.id, HealthCheck.created_at >= start_time, HealthCheck.created_at <= end_time)
            .order_by(HealthCheck.created_at.asc())
            .all()
        )
        buckets = []
        for index in range(bucket_count):
            bucket_start = start_time + timedelta(minutes=index * bucket_minutes)
            bucket_end = bucket_start + timedelta(minutes=bucket_minutes)
            bucket_checks = [item for item in checks if bucket_start <= item.created_at < bucket_end]
            failures = [item for item in bucket_checks if not item.success]
            successes = [item for item in bucket_checks if item.success]
            if not channel.enabled:
                state = "disabled"
                label = "渠道停用"
            elif not bucket_checks:
                state = "unknown"
                label = "无检测数据"
            elif failures and successes:
                state = "degraded"
                label = build_bucket_label(len(bucket_checks), len(failures), failures)
            elif failures:
                state = "down"
                label = build_bucket_label(len(bucket_checks), len(failures), failures)
            else:
                state = "up"
                label = f"共 {len(bucket_checks)} 次检测；成功 {len(successes)} 次；失败 0 次"
            buckets.append(
                {
                    "state": state,
                    "label": label,
                    "start": format_dt(bucket_start, "%m-%d %H:%M"),
                    "end": format_dt(bucket_end, "%H:%M"),
                }
            )
        total = len(checks)
        failures = sum(1 for item in checks if not item.success)
        uptime = None if total == 0 else round((total - failures) / total * 100, 2)
        latest = checks[-1] if checks else recent_status(db, channel.id)
        lanes.append(
            {
                "channel": channel,
                "buckets": buckets,
                "total": total,
                "failures": failures,
                "uptime": uptime,
                "latest": latest,
                "window_label": f"最近 {hours} 小时",
            }
        )
    return lanes


def align_time_to_bucket(value, bucket_minutes: int):
    bucket_minutes = max(bucket_minutes, 1)
    aligned_minute = value.minute - (value.minute % bucket_minutes)
    return value.replace(minute=aligned_minute, second=0, microsecond=0)


def build_bucket_label(total: int, failure_count: int, failures: list[HealthCheck]) -> str:
    status_codes = sorted({str(item.status_code) if item.status_code is not None else "无状态码" for item in failures})
    reasons = []
    for item in failures:
        reason = item.message or "无失败原因"
        if reason not in reasons:
            reasons.append(reason)
        if len(reasons) >= 3:
            break
    reason_text = "；".join(reasons)
    if len({item.message for item in failures}) > len(reasons):
        reason_text += "；..."
    return f"共 {total} 次检测；成功 {total - failure_count} 次；失败 {failure_count} 次；状态码 {', '.join(status_codes)}；原因 {reason_text}"


def _get_channel(db: Session, channel_id: int) -> Channel:
    channel = db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404)
    return channel


def _get_channel_sync_link(db: Session, channel_id: int, link_id: int) -> ChannelSyncLink:
    link = db.get(ChannelSyncLink, link_id)
    if not link or link.channel_id != channel_id:
        raise HTTPException(status_code=404)
    return link


def mark_all_alerts_acknowledged(db: Session) -> int:
    unread_events = db.query(AlertEvent).filter(AlertEvent.acknowledged.is_(False)).all()
    for event in unread_events:
        event.acknowledged = True
    db.commit()
    return len(unread_events)


def cleanup_removed_model_rows(db: Session) -> int:
    removed = db.query(ChannelModel).filter(ChannelModel.available.is_(False)).all()
    for item in removed:
        db.delete(item)
    db.commit()
    return len(removed)


def _get_notification(db: Session, notification_id: int) -> NotificationChannel:
    item = db.get(NotificationChannel, notification_id)
    if not item:
        raise HTTPException(status_code=404)
    return item


def _get_sync_target(db: Session, target_id: int) -> SyncTarget:
    target = db.get(SyncTarget, target_id)
    if not target:
        raise HTTPException(status_code=404)
    return target


def _validate_json(value: str) -> str | None:
    if not value:
        return None
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        return f"JSON 无效：{exc}"
    return None


def _optional_int(value: str | int | None) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_float(value: str | float | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def build_notification_config(
    *,
    channel_type: str,
    email_host: str = "",
    email_port: int = 587,
    email_use_tls: bool = True,
    email_username: str = "",
    email_password: str = "",
    email_sender: str = "",
    email_recipients: str = "",
    email_subject: str = "[AI渠道告警] $channel_name $alert_type",
    email_body: str = "$message",
    http_method: str = "POST",
    http_url: str = "",
    http_headers_json: str = "{}",
    http_body: str = "",
) -> dict[str, Any]:
    if channel_type == "email":
        if not email_host.strip():
            raise ValueError("SMTP 服务器必填。")
        if not email_sender.strip() and not email_username.strip():
            raise ValueError("发件人或用户名至少填写一个。")
        if not email_recipients.strip():
            raise ValueError("收件人必填。")
        return {
            "host": email_host.strip(),
            "port": int(email_port or 587),
            "use_tls": bool(email_use_tls),
            "username": email_username.strip(),
            "password": email_password,
            "sender": email_sender.strip() or email_username.strip(),
            "recipients": email_recipients.strip(),
            "subject": email_subject.strip() or "[AI渠道告警] $channel_name $alert_type",
            "body": email_body or "$message",
        }
    if channel_type == "http":
        if not http_url.strip():
            raise ValueError("HTTP 通知 URL 必填。")
        try:
            headers = json.loads(http_headers_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Headers JSON 无效：{exc}") from exc
        if not isinstance(headers, dict):
            raise ValueError("Headers JSON 必须是对象。")
        method = http_method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError("HTTP 方法只支持 GET 或 POST。")
        return {
            "method": method,
            "url": http_url.strip(),
            "headers": headers,
            "body": http_body,
        }
    raise ValueError("通知类型无效。")


def build_notification_config_from_form(form: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    email_password = str(form.get("email_password") or "")
    if form.get("channel_type") == "email" and not email_password and existing.get("password"):
        email_password = str(existing["password"])
    return build_notification_config(
        channel_type=str(form.get("channel_type") or ""),
        email_host=str(form.get("email_host") or ""),
        email_port=int(form.get("email_port") or 587),
        email_use_tls=bool(form.get("email_use_tls")),
        email_username=str(form.get("email_username") or ""),
        email_password=email_password,
        email_sender=str(form.get("email_sender") or ""),
        email_recipients=str(form.get("email_recipients") or ""),
        email_subject=str(form.get("email_subject") or "[AI渠道告警] $channel_name $alert_type"),
        email_body=str(form.get("email_body") or "$message"),
        http_method=str(form.get("http_method") or "POST"),
        http_url=str(form.get("http_url") or ""),
        http_headers_json=str(form.get("http_headers_json") or "{}"),
        http_body=str(form.get("http_body") or ""),
    )


def notification_config(item: NotificationChannel) -> dict[str, Any]:
    try:
        config = json.loads(item.config_json or "{}")
    except json.JSONDecodeError:
        return {}
    return config if isinstance(config, dict) else {}


def sync_target_auth_config(item: SyncTarget) -> dict[str, Any]:
    try:
        config = json.loads(item.auth_config_json or "{}")
    except json.JSONDecodeError:
        return {}
    return config if isinstance(config, dict) else {}


def default_notification_form() -> dict[str, Any]:
    return {
        "name": "",
        "channel_type": "email",
        "enabled": True,
        "email_host": "",
        "email_port": 587,
        "email_use_tls": True,
        "email_username": "",
        "email_password": "",
        "email_sender": "",
        "email_recipients": "",
        "email_subject": "[AI渠道告警] $channel_name $alert_type",
        "email_body": "渠道：$channel_name\n级别：$severity\n类型：$alert_type\n时间：$created_at\n消息：$message\n新增模型：$added_models\n移除模型：$removed_models\n旧模型列表：$old_models\n新模型列表：$new_models",
        "http_method": "POST",
        "http_url": "",
        "http_headers_json": json.dumps({"Content-Type": "application/json"}, ensure_ascii=False, indent=2),
        "http_body": json.dumps({"title": "AI渠道告警", "channel": "$channel_name", "type": "$alert_type", "severity": "$severity", "message": "$message", "time": "$created_at", "added_models": "$added_models", "removed_models": "$removed_models", "old_models": "$old_models", "new_models": "$new_models"}, ensure_ascii=False, indent=2),
    }


def notification_form_from_item(item: NotificationChannel) -> dict[str, Any]:
    config = notification_config(item)
    form = default_notification_form()
    form.update({"name": item.name, "channel_type": item.channel_type, "enabled": item.enabled})
    if item.channel_type == "email":
        form.update(
            {
                "email_host": config.get("host", ""),
                "email_port": config.get("port", 587),
                "email_use_tls": bool(config.get("use_tls", True)),
                "email_username": config.get("username", ""),
                "email_password": "",
                "email_sender": config.get("sender", ""),
                "email_recipients": config.get("recipients", ""),
                "email_subject": config.get("subject", form["email_subject"]),
                "email_body": config.get("body", form["email_body"]),
            }
        )
    if item.channel_type == "http":
        form.update(
            {
                "http_method": config.get("method", "POST"),
                "http_url": config.get("url", ""),
                "http_headers_json": json.dumps(config.get("headers", {}), ensure_ascii=False, indent=2),
                "http_body": _format_body_for_form(config.get("body", "")),
            }
        )
    return form


def build_sync_target_config(
    target_type: str,
    sub2api_admin_api_key: str = "",
    newapi_authorization: str = "",
    newapi_user: str = "",
    existing: dict[str, Any] | None = None,
) -> dict[str, str]:
    existing = existing or {}
    if target_type == "sub2api":
        admin_api_key = sub2api_admin_api_key.strip() or str(existing.get("admin_api_key") or "").strip()
        if not admin_api_key:
            raise ValueError("sub2api Admin API Key 必填。")
        return {"admin_api_key": admin_api_key}
    if target_type == "new_api":
        authorization = newapi_authorization.strip() or str(existing.get("authorization") or "").strip()
        user = newapi_user.strip()
        if not authorization:
            raise ValueError("new-api Authorization 必填。")
        if not user:
            raise ValueError("new-api New-Api-User 必填。")
        return {"authorization": authorization, "new_api_user": user}
    raise ValueError("目标站点类型无效。")


def sync_target_form_from_item(item: SyncTarget | None = None) -> dict[str, Any]:
    form = {
        "name": "",
        "target_type": "sub2api",
        "base_url": "",
        "enabled": True,
        "name_prefix": "union_",
        "sub2api_admin_api_key": "",
        "newapi_authorization": "",
        "newapi_user": "",
        "secret_configured": False,
    }
    if item is None:
        return form

    config = sync_target_auth_config(item)
    secret_configured = False
    if item.target_type == "sub2api":
        secret_configured = bool(str(config.get("admin_api_key") or "").strip())
    elif item.target_type == "new_api":
        secret_configured = bool(str(config.get("authorization") or "").strip())

    form.update(
        {
            "name": item.name,
            "target_type": item.target_type,
            "base_url": item.base_url,
            "enabled": item.enabled,
            "name_prefix": item.name_prefix or "union_",
            "newapi_user": str(config.get("new_api_user") or ""),
            "secret_configured": secret_configured,
        }
    )
    return form


def build_sync_target_rows(db: Session, targets: list[SyncTarget]) -> list[dict[str, Any]]:
    return [
        {
            "target": target,
            "link_count": db.query(ChannelSyncLink).filter(ChannelSyncLink.target_id == target.id).count(),
            "secret_configured": sync_target_form_from_item(target)["secret_configured"],
        }
        for target in targets
    ]


def sync_target_test_error_message(exc: SyncClientError) -> str:
    if exc.status_code is not None:
        return f"连接测试失败：HTTP {exc.status_code}"
    return "连接测试失败。"


def _format_body_for_form(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if not isinstance(value, str):
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return json.dumps(parsed, ensure_ascii=False, indent=2)
