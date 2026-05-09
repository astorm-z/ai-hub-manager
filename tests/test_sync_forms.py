import json
from base64 import urlsafe_b64decode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm.attributes import set_committed_value

from app.database import get_db
from app.main import app, build_sync_target_config, channel_sync_context, sync_target_form_from_item
from app.models import AlertRule, Channel, ChannelSyncLink, SyncTarget, User
from app.schemas import ProbeResult
from app.services.auth import make_session_token
from app.services.sync_clients import SyncClientError


def test_build_sub2api_target_config():
    config = build_sync_target_config(
        target_type="sub2api",
        sub2api_admin_api_key=" admin-secret ",
        newapi_authorization="",
        newapi_user="",
    )

    assert config == {"admin_api_key": "admin-secret"}


def test_build_newapi_target_config():
    config = build_sync_target_config(
        target_type="new_api",
        sub2api_admin_api_key="",
        newapi_authorization=" Bearer token ",
        newapi_user=" 1 ",
    )

    assert config == {"authorization": "Bearer token", "new_api_user": "1"}


def test_sync_target_config_rejects_missing_secret():
    with pytest.raises(ValueError, match="Admin API Key"):
        build_sync_target_config(target_type="sub2api", sub2api_admin_api_key="", newapi_authorization="", newapi_user="")


def test_sync_target_form_masks_secrets():
    target = SyncTarget(
        name="new",
        target_type="new_api",
        base_url="https://new.test",
        name_prefix="union_",
        auth_config_json=json.dumps({"authorization": "Bearer token", "new_api_user": "1"}),
    )

    form = sync_target_form_from_item(target)

    assert form["newapi_authorization"] == ""
    assert form["newapi_user"] == "1"
    assert form["secret_configured"]


def test_edit_sync_target_preserves_blank_secret(db_session):
    user = User(username="admin", password_hash="hash")
    target = SyncTarget(
        name="new",
        target_type="new_api",
        base_url="https://old.test",
        name_prefix="union_",
        auth_config_json=json.dumps({"authorization": "Bearer old", "new_api_user": "1"}),
    )
    db_session.add_all([user, target])
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/sync-targets/{target.id}",
            data={
                "name": "newer",
                "target_type": "new_api",
                "base_url": " https://new.test/ ",
                "name_prefix": " api_ ",
                "enabled": "true",
                "newapi_authorization": "",
                "newapi_user": "2",
            },
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    db_session.refresh(target)
    assert response.status_code == 303
    assert target.name == "newer"
    assert target.base_url == "https://new.test"
    assert target.name_prefix == "api_"
    assert json.loads(target.auth_config_json) == {"authorization": "Bearer old", "new_api_user": "2"}


def test_delete_sync_target_blocks_when_linked(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="source", provider_type="openai", base_url="https://source.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json='{"admin_api_key":"secret"}')
    db_session.add_all([user, channel, target])
    db_session.flush()
    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="sub2api_account"))
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/sync-targets/{target.id}/delete",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/sync-targets"
    assert db_session.get(SyncTarget, target.id) is not None


def test_create_sync_target_duplicate_name_uses_stable_flash(db_session):
    user = User(username="admin", password_hash="hash")
    target = SyncTarget(name="dup", target_type="sub2api", base_url="https://sub.test", auth_config_json='{"admin_api_key":"secret"}')
    db_session.add_all([user, target])
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            "/sync-targets",
            data={
                "name": "dup",
                "target_type": "sub2api",
                "base_url": "https://other.test",
                "name_prefix": "union_",
                "enabled": "true",
                "sub2api_admin_api_key": "new-secret",
            },
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert flash["message"] == "同步目标名称已存在。"
    assert _has_no_low_level_details(flash["message"])


def test_update_sync_target_duplicate_name_uses_stable_flash(db_session):
    user = User(username="admin", password_hash="hash")
    first = SyncTarget(name="first", target_type="sub2api", base_url="https://first.test", auth_config_json='{"admin_api_key":"secret"}')
    second = SyncTarget(name="second", target_type="sub2api", base_url="https://second.test", auth_config_json='{"admin_api_key":"secret"}')
    db_session.add_all([user, first, second])
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/sync-targets/{second.id}",
            data={
                "name": "first",
                "target_type": "sub2api",
                "base_url": "https://second.test",
                "name_prefix": "union_",
                "enabled": "true",
                "sub2api_admin_api_key": "",
            },
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert flash["message"] == "同步目标名称已存在。"
    assert _has_no_low_level_details(flash["message"])


def test_sync_target_connection_test_uses_sanitized_flash(db_session, monkeypatch):
    class LeakyClient:
        async def test_connection(self):
            raise SyncClientError("Authorization: Bearer secret", status_code=401, response={"token": "secret"})

    user = User(username="admin", password_hash="hash")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json='{"admin_api_key":"secret"}')
    db_session.add_all([user, target])
    db_session.commit()
    monkeypatch.setattr("app.main.client_for_target", lambda item: LeakyClient())

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/sync-targets/{target.id}/test",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert flash["message"] == "连接测试失败：HTTP 401"
    assert "secret" not in flash["message"]
    assert "Authorization" not in flash["message"]


def test_channel_sync_context_lists_enabled_targets_and_existing_links(db_session):
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="sk-live")
    enabled_target = SyncTarget(name="enabled", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    disabled_target = SyncTarget(name="disabled", target_type="new_api", base_url="https://new.test", enabled=False, auth_config_json="{}")
    db_session.add_all([channel, enabled_target, disabled_target])
    db_session.commit()
    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=enabled_target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_main",
    )
    db_session.add(link)
    db_session.commit()

    context = channel_sync_context(db_session, channel)

    assert [target.name for target in context["sync_targets"]] == ["enabled"]
    assert context["sync_links"] == [link]
    assert context["linked_target_ids"] == {enabled_target.id}


def test_channel_sync_link_toggle_requires_link_to_belong_to_channel(db_session):
    user = User(username="admin", password_hash="hash")
    first_channel = Channel(name="first", provider_type="openai", base_url="https://first.test", api_key="key")
    second_channel = Channel(name="second", provider_type="openai", base_url="https://second.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add_all([user, first_channel, second_channel, target])
    db_session.flush()
    link = ChannelSyncLink(channel_id=second_channel.id, target_id=target.id, remote_type="account", remote_id="42")
    db_session.add(link)
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{first_channel.id}/sync-links/{link.id}/toggle",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 404


def test_channel_sync_link_toggle_flips_sync_enabled(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42", sync_enabled=True)
    db_session.add(link)
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/sync-links/{link.id}/toggle",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    db_session.refresh(link)
    assert response.status_code == 303
    assert response.headers["location"] == f"/channels/{channel.id}"
    assert link.sync_enabled is False


def test_channel_sync_link_delete_removes_only_local_link(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42")
    db_session.add(link)
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/sync-links/{link.id}/delete",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert db_session.get(ChannelSyncLink, link.id) is None
    assert db_session.get(SyncTarget, target.id) is not None


def test_update_channel_runs_auto_sync_after_save(db_session, monkeypatch):
    calls = []

    async def fake_sync_channel_links(db, channel, *, action):
        calls.append((channel.id, action))
        return (1, 0)

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    db_session.add_all([user, channel])
    db_session.commit()
    monkeypatch.setattr("app.main.sync_channel_links", fake_sync_channel_links)

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}",
            data=_channel_form_data(name="renamed"),
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert response.status_code == 303
    assert calls == [(channel.id, "auto_update")]
    assert flash["message"] == "渠道已保存；已同步 1 个目标。"


def test_refresh_models_runs_auto_sync_when_refresh_succeeds(db_session, monkeypatch):
    calls = []

    async def fake_refresh_channel_models(db, channel):
        return ProbeResult(success=True, status_code=200, latency_ms=12, message="ok")

    async def fake_sync_channel_links(db, channel, *, action):
        calls.append((channel.id, action))
        return (1, 0)

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    db_session.add_all([user, channel])
    db_session.commit()
    monkeypatch.setattr("app.main.refresh_channel_models", fake_refresh_channel_models)
    monkeypatch.setattr("app.main.sync_channel_links", fake_sync_channel_links)

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/refresh-models",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert response.status_code == 303
    assert calls == [(channel.id, "auto_update")]
    assert flash["message"] == "ok"


def test_channel_detail_renders_sync_panel(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    db_session.add(AlertRule(channel_id=channel.id))
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.get(
            f"/channels/{channel.id}",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert "渠道同步" in response.text
    assert "导入目标站点" in response.text


def test_manual_sync_failure_flash_sanitizes_remote_error(db_session, monkeypatch):
    async def fake_sync_existing_link(db, link, *, action):
        link.last_sync_error = "HTTP 500 Authorization: Bearer secret password=hunter2"
        db.commit()
        set_committed_value(link, "last_sync_error", "HTTP 500 Authorization: Bearer secret password=hunter2")
        return False

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42")
    db_session.add(link)
    db_session.commit()
    monkeypatch.setattr("app.main.sync_existing_link", fake_sync_existing_link)

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/sync-links/{link.id}/sync",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert flash["message"] == "目标站点同步失败：HTTP 500"
    assert "secret" not in flash["message"]
    assert "Authorization" not in flash["message"]
    assert "password" not in flash["message"]
    assert "hunter2" not in flash["message"]


def test_create_sync_link_rejects_leaky_group_id_like_remote_error(db_session, monkeypatch):
    async def fake_create_channel_sync_link(*args, **kwargs):
        raise ValueError("分组 ID Authorization: Bearer secret password=hunter2")

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.commit()
    monkeypatch.setattr("app.main.create_channel_sync_link", fake_create_channel_sync_link)

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/sync-links",
            data={"target_id": str(target.id), "sub2api_group_ids": "1"},
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert flash["message"] == "目标站点同步失败。"
    assert "secret" not in flash["message"]
    assert "Authorization" not in flash["message"]
    assert "password" not in flash["message"]
    assert "hunter2" not in flash["message"]


def test_channel_detail_panel_sanitizes_sync_error(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    db_session.add(AlertRule(channel_id=channel.id))
    db_session.add(
        ChannelSyncLink(
            channel_id=channel.id,
            target_id=target.id,
            remote_type="account",
            remote_id="42",
            last_sync_status="failed",
            last_sync_error="HTTP 503 Authorization: Bearer secret password=hunter2",
        )
    )
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.get(
            f"/channels/{channel.id}",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert "目标站点同步失败：HTTP 503" in response.text
    assert "secret" not in response.text
    assert "Authorization" not in response.text
    assert "password" not in response.text
    assert "hunter2" not in response.text


def _client_with_db(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _flash_from_response(response):
    raw = response.cookies["flash"]
    return json.loads(urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))


def _has_no_low_level_details(message: str) -> bool:
    lowered = message.lower()
    return not any(token in lowered for token in ("unique", "sqlite", "insert", "update"))


def _channel_form_data(**overrides):
    data = {
        "name": "main",
        "provider_type": "openai",
        "base_url": "https://upstream.test",
        "api_key": "key",
        "enabled": "true",
        "timeout_seconds": "20",
        "model_check_interval_minutes": "10",
        "balance_check_interval_minutes": "30",
        "probe_model": "",
        "openai_test_mode": "chat_completions",
        "extractor_template_id": "",
        "extractor_vars_json": "{}",
    }
    data.update(overrides)
    return data
