import json
from base64 import urlsafe_b64decode

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app, build_sync_target_config, sync_target_form_from_item
from app.models import Channel, ChannelSyncLink, SyncTarget, User
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
