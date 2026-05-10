import json
import re
from base64 import urlsafe_b64decode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm.attributes import set_committed_value

from app.database import get_db
from app.main import app, build_sync_target_config, channel_sync_context, create_channel, default_probe_model_for_provider, normalize_new_channel_probe_model, normalize_sync_target_base_url, sync_target_form_from_item
from app.models import AlertEvent, AlertRule, Channel, ChannelSyncLink, SyncEvent, SyncTarget, User
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


def test_normalize_sync_target_base_url_rejects_missing_host():
    with pytest.raises(ValueError, match="Base URL 无效"):
        normalize_sync_target_base_url("https:///s2a.astorm.cn")


def test_create_sync_target_rejects_base_url_without_host(db_session):
    user = User(username="admin", password_hash="hash")
    db_session.add(user)
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            "/sync-targets",
            data={
                "name": "bad-url",
                "target_type": "sub2api",
                "base_url": "https:///s2a.astorm.cn",
                "name_prefix": "union_",
                "enabled": "true",
                "sub2api_admin_api_key": "admin-secret",
            },
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert response.status_code == 303
    assert flash["message"] == "Base URL 无效，请填写类似 https://target.example.com 的完整地址。"
    assert db_session.query(SyncTarget).count() == 0


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


def test_sync_target_import_page_renders_unlinked_remote_channels(db_session, monkeypatch):
    class ListClient:
        async def list_channels(self):
            return [
                {"id": 9, "name": "linked", "type": 1, "key": "sk-linked", "base_url": "https://linked.test"},
                {"id": 10, "name": "fresh", "type": 1, "base_url": "https://fresh.test", "models": "gpt-4o"},
            ]

    user = User(username="admin", password_hash="hash")
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", enabled=True, auth_config_json="{}")
    channel = Channel(name="linked", provider_type="openai", base_url="https://linked.test", api_key="key")
    db_session.add_all([user, target, channel])
    db_session.flush()
    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel", remote_id="9"))
    db_session.commit()
    monkeypatch.setattr("app.services.channel_sync.client_for_target", lambda item: ListClient())

    client = _client_with_db(db_session)
    try:
        response = client.get(
            f"/sync-targets/{target.id}/import",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert "fresh" in response.text
    assert 'value="10"' in response.text
    assert 'name="api_key_10"' in response.text
    assert "linked" not in response.text


def test_sync_target_import_post_creates_selected_channel(db_session, monkeypatch):
    class ListClient:
        async def list_channels(self):
            return [
                {
                    "id": 10,
                    "name": "fresh",
                    "type": 1,
                    "key": "sk-fresh",
                    "base_url": "https://fresh.test",
                    "models": "gpt-4o",
                    "status": 1,
                }
            ]

    user = User(username="admin", password_hash="hash")
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, target])
    db_session.commit()
    monkeypatch.setattr("app.services.channel_sync.client_for_target", lambda item: ListClient())

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/sync-targets/{target.id}/import",
            data={"remote_ids": "10"},
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    channel = db_session.query(Channel).filter(Channel.name == "fresh").one()
    link = db_session.query(ChannelSyncLink).filter(ChannelSyncLink.channel_id == channel.id).one()

    assert response.status_code == 303
    assert response.headers["location"] == f"/sync-targets/{target.id}/import"
    assert flash["message"] == "导入完成：成功 1 个。"
    assert channel.api_key == "sk-fresh"
    assert link.remote_id == "10"


def test_sync_target_import_post_uses_manual_newapi_key(db_session, monkeypatch):
    class ListClient:
        async def list_channels(self):
            return [
                {
                    "id": 10,
                    "name": "fresh",
                    "type": 1,
                    "base_url": "https://fresh.test",
                    "models": "gpt-4o",
                    "status": 1,
                }
            ]

    user = User(username="admin", password_hash="hash")
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, target])
    db_session.commit()
    monkeypatch.setattr("app.services.channel_sync.client_for_target", lambda item: ListClient())

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/sync-targets/{target.id}/import",
            data={"remote_ids": "10", "api_key_10": "sk-manual"},
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    channel = db_session.query(Channel).filter(Channel.name == "fresh").one()

    assert response.status_code == 303
    assert flash["message"] == "导入完成：成功 1 个。"
    assert channel.api_key == "sk-manual"


def test_sync_target_import_page_renders_sub2api_group_names(db_session, monkeypatch):
    class ListClient:
        async def list_accounts(self):
            return [
                {
                    "id": 42,
                    "name": "remote-main",
                    "platform": "openai",
                    "status": "active",
                    "credentials": {
                        "api_key": "sk-sub",
                        "base_url": "https://sub-upstream.test",
                    },
                    "group_ids": [1, 2],
                    "groups": [{"id": 1, "name": "plus"}, {"id": 2, "name": "free"}],
                }
            ]

    user = User(username="admin", password_hash="hash")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, target])
    db_session.commit()
    monkeypatch.setattr("app.services.channel_sync.client_for_target", lambda item: ListClient())

    client = _client_with_db(db_session)
    try:
        response = client.get(
            f"/sync-targets/{target.id}/import",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert "分组：plus，free" in response.text
    assert "分组：[1, 2]" not in response.text


def test_channel_sync_context_lists_enabled_targets_and_existing_links(db_session):
    class NoGroupsClient:
        async def list_groups(self, **kwargs):
            return []

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

    context = channel_sync_context(db_session, channel, client_factory=lambda target: NoGroupsClient())

    assert [target.name for target in context["sync_targets"]] == ["enabled"]
    assert context["sync_links"] == [link]
    assert context["linked_target_ids"] == {enabled_target.id}
    assert context["sync_target_group_options"][enabled_target.id] == []


def test_channel_sync_context_loads_remote_group_options(db_session):
    class GroupClient:
        def __init__(self, target):
            self.target = target

        async def list_groups(self, **kwargs):
            calls.append((self.target.name, kwargs))
            return [{"value": "1", "label": "default"}]

    calls = []
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="sk-live")
    sub_target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    new_target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", enabled=True, auth_config_json="{}")
    db_session.add_all([channel, sub_target, new_target])
    db_session.commit()

    context = channel_sync_context(db_session, channel, client_factory=lambda target: GroupClient(target))

    assert context["sync_target_group_options"][sub_target.id] == [{"value": "1", "label": "default"}]
    assert context["sync_target_group_options"][new_target.id] == [{"value": "1", "label": "default"}]
    assert sorted(calls) == [("new", {}), ("sub", {})]


def test_channel_sync_context_records_group_list_error(db_session):
    class FailingClient:
        async def list_groups(self, **kwargs):
            raise SyncClientError("bad token", status_code=401)

    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="sk-live")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    context = channel_sync_context(db_session, channel, client_factory=lambda item: FailingClient())

    assert context["sync_target_group_options"][target.id] == []
    assert context["sync_target_group_errors"][target.id] == "分组列表获取失败：HTTP 401"


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


def test_channel_detail_renders_remote_delete_option_for_sync_link(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    db_session.add(AlertRule(channel_id=channel.id))
    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42"))
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
    assert "删除关联" in response.text
    assert "删除远端" in response.text
    assert 'type="hidden" name="delete_remote" value="true"' in response.text


def test_channel_sync_link_delete_with_remote_passes_option_to_service(db_session, monkeypatch):
    captured = {}

    async def fake_delete_channel_sync_link_service(db, link, *, delete_remote):
        captured["link_id"] = link.id
        captured["delete_remote"] = delete_remote
        db.delete(link)
        db.commit()
        return True, "同步关联已删除；远端对象已删除。"

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42")
    db_session.add(link)
    db_session.commit()
    monkeypatch.setattr("app.main.delete_channel_sync_link_service", fake_delete_channel_sync_link_service)

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/sync-links/{link.id}/delete",
            data={"delete_remote": "true"},
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert response.status_code == 303
    assert captured == {"link_id": link.id, "delete_remote": True}
    assert flash["message"] == "同步关联已删除；远端对象已删除。"
    assert db_session.get(ChannelSyncLink, link.id) is None


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


def test_default_probe_model_matches_provider_type():
    assert default_probe_model_for_provider("openai") == "gpt-5.4-mini"
    assert default_probe_model_for_provider("claude") == "claude-haiku-4-5"
    assert default_probe_model_for_provider("unknown") == "gpt-5.4-mini"


def test_new_channel_probe_model_defaults_only_when_blank():
    assert normalize_new_channel_probe_model("openai", "") == "gpt-5.4-mini"
    assert normalize_new_channel_probe_model("claude", "  ") == "claude-haiku-4-5"
    assert normalize_new_channel_probe_model("claude", " custom-model ") == "custom-model"


def test_create_channel_persists_default_probe_model(db_session):
    user = User(username="admin", password_hash="hash")

    response = create_channel(
        db=db_session,
        user=user,
        name="main",
        provider_type="claude",
        base_url="https://upstream.test",
        api_key="key",
        enabled=True,
        timeout_seconds=20,
        model_check_interval_minutes=10,
        balance_check_interval_minutes=30,
        probe_model="",
        openai_test_mode="chat_completions",
        extractor_template_id="",
        extractor_vars_json="{}",
    )

    channel = db_session.query(Channel).filter(Channel.name == "main").one()
    assert response.status_code == 303
    assert channel.probe_model == "claude-haiku-4-5"


def test_delete_channel_nulls_history_references_before_delete(db_session):
    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    db_session.add_all([user, channel])
    db_session.flush()
    db_session.add(SyncEvent(channel_id=channel.id, action="remote_import", success=True))
    db_session.add(AlertEvent(channel_id=channel.id, alert_type="model_change", message="changed"))
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/delete",
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    sync_event = db_session.query(SyncEvent).one()
    alert_event = db_session.query(AlertEvent).one()
    assert response.status_code == 303
    assert db_session.get(Channel, channel.id) is None
    assert sync_event.channel_id is None
    assert alert_event.channel_id is None


def test_new_channel_page_renders_probe_model_defaults(db_session):
    user = User(username="admin", password_hash="hash")
    db_session.add(user)
    db_session.commit()

    client = _client_with_db(db_session)
    try:
        response = client.get(
            "/channels/new",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert re.search(r'<input[^>]*id="probe_model"[^>]*name="probe_model"[^>]*value="gpt-5\.4-mini"', response.text, flags=re.DOTALL)
    assert 'data-default-openai="gpt-5.4-mini"' in response.text
    assert 'data-default-claude="claude-haiku-4-5"' in response.text


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


def test_channel_detail_renders_group_multiselects_for_each_target_type(db_session, monkeypatch):
    class GroupClient:
        def __init__(self, target):
            self.target = target

        async def list_groups(self, **kwargs):
            if self.target.target_type == "sub2api":
                return [{"value": "1", "label": "sub-default"}]
            return [{"value": "claude-code", "label": "claude-code"}]

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    sub_target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    new_target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, sub_target, new_target])
    db_session.flush()
    db_session.add(AlertRule(channel_id=channel.id))
    db_session.commit()
    monkeypatch.setattr("app.main.client_for_target", lambda target: GroupClient(target))

    client = _client_with_db(db_session)
    try:
        response = client.get(
            f"/channels/{channel.id}",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert 'name="sub2api_group_ids"' in response.text
    assert 'name="newapi_groups"' in response.text
    assert 'multiple' in response.text
    assert 'value="1"' in response.text
    assert "sub-default" in response.text
    assert 'value="claude-code"' in response.text
    assert "claude-code" in response.text
    assert 'name="sub2api_priority" type="number" min="0" value="1"' in response.text
    assert 'name="sub2api_concurrency" type="number" min="1" value="10"' in response.text


def test_channel_detail_renders_manual_group_fallback_when_group_list_fails(db_session, monkeypatch):
    class FailingClient:
        async def list_groups(self, **kwargs):
            raise SyncClientError("bad token", status_code=401)

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.flush()
    db_session.add(AlertRule(channel_id=channel.id))
    db_session.commit()
    monkeypatch.setattr("app.main.client_for_target", lambda item: FailingClient())

    client = _client_with_db(db_session)
    try:
        response = client.get(
            f"/channels/{channel.id}",
            cookies={"session": make_session_token(user.id)},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert "分组列表获取失败：HTTP 401" in response.text
    assert 'data-group-fallback="true"' in response.text


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


def test_create_sync_link_shows_local_sub2api_setting_errors(db_session, monkeypatch):
    async def fake_create_channel_sync_link(*args, **kwargs):
        raise ValueError("sub2api 并发必须大于 0。")

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
            data={
                "target_id": str(target.id),
                "sub2api_group_ids": "",
                "sub2api_priority": "50",
                "sub2api_concurrency": "0",
            },
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    flash = _flash_from_response(response)
    assert flash["message"] == "sub2api 并发必须大于 0。"


def test_create_sync_link_passes_newapi_groups_to_service(db_session, monkeypatch):
    captured = {}

    async def fake_create_channel_sync_link(db, channel, target, group_ids, priority, concurrency, *, newapi_groups="", **kwargs):
        captured.update(
            {
                "group_ids": group_ids,
                "priority": priority,
                "concurrency": concurrency,
                "newapi_groups": newapi_groups,
            }
        )

    user = User(username="admin", password_hash="hash")
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="key")
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", enabled=True, auth_config_json="{}")
    db_session.add_all([user, channel, target])
    db_session.commit()
    monkeypatch.setattr("app.main.create_channel_sync_link", fake_create_channel_sync_link)

    client = _client_with_db(db_session)
    try:
        response = client.post(
            f"/channels/{channel.id}/sync-links",
            data={"target_id": str(target.id), "newapi_groups": ["claude-code", "claude-code-ot"]},
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert captured["newapi_groups"] == "claude-code,claude-code-ot"


def test_create_sync_link_passes_multiple_sub2api_group_ids_to_service(db_session, monkeypatch):
    captured = {}

    async def fake_create_channel_sync_link(db, channel, target, group_ids, priority, concurrency, *, newapi_groups="", **kwargs):
        captured.update({"group_ids": group_ids, "newapi_groups": newapi_groups})

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
            data={"target_id": str(target.id), "sub2api_group_ids": ["1", "2"]},
            cookies={"session": make_session_token(user.id)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert captured["group_ids"] == "1,2"


def test_create_sync_link_uses_sub2api_default_priority_and_concurrency(db_session, monkeypatch):
    captured = {}

    async def fake_create_channel_sync_link(db, channel, target, group_ids, priority, concurrency, *, newapi_groups="", **kwargs):
        captured.update({"priority": priority, "concurrency": concurrency})

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

    assert response.status_code == 303
    assert captured == {"priority": 1, "concurrency": 10}


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
