import json

import pytest

from app.models import Channel, ChannelModel, ChannelSyncLink, SyncEvent, SyncTarget
from app.services.channel_sync import create_channel_sync_link, record_sync_event, sync_channel_links, sync_existing_link
from app.services.sync_clients import SyncClientError, SyncClientResult


class FakeSub2APIClient:
    def __init__(self, *, existing=None, fail_update=False):
        self.existing = existing
        self.fail_update = fail_update
        self.created_payload = None
        self.updated_payload = None

    async def find_account_by_name(self, name):
        return self.existing

    async def create_account(self, payload):
        self.created_payload = payload
        return SyncClientResult(200, {"id": 42, **payload}, {"code": 0, "data": {"id": 42, **payload}})

    async def get_account(self, remote_id):
        return {"id": int(remote_id), "credentials": {"keep": "value"}}

    async def update_account(self, remote_id, payload):
        if self.fail_update:
            raise SyncClientError("update failed", status_code=500, response={"message": "bad"})
        self.updated_payload = payload
        return SyncClientResult(200, {"id": int(remote_id), **payload}, {"code": 0, "data": {"id": int(remote_id)}})


class FakeNewAPIClient:
    def __init__(self, *, existing=None):
        self.existing = existing
        self.created_payload = None

    async def find_channel_by_name(self, name):
        if self.created_payload and self.created_payload["name"] == name:
            return {"id": 9, "name": name}
        return self.existing

    async def create_channel(self, payload):
        self.created_payload = payload
        return SyncClientResult(200, {"success": True}, {"success": True})

    async def get_channel(self, remote_id):
        return {"id": int(remote_id), "name": "union_main", "group": "default"}

    async def update_channel(self, payload):
        return SyncClientResult(200, {"success": True}, {"success": True})


class FakeSensitiveErrorClient:
    async def get_account(self, remote_id):
        raise SyncClientError(
            "remote failed",
            status_code=500,
            response='api_key=sk-live\nAuthorization: Bearer secret\n{"key":"sk-live"}\npassword: hunter2',
        )


class FakeLeakyUpdateErrorClient:
    async def get_account(self, remote_id):
        return {"id": int(remote_id), "credentials": {"keep": "value"}}

    async def update_account(self, remote_id, payload):
        raise SyncClientError(
            "HTTP 500 Authorization: Bearer secret password=hunter2",
            status_code=500,
            response={"message": "bad"},
        )


class RoutingClient:
    def __init__(self):
        self.updated = []

    async def get_account(self, remote_id):
        return {"id": int(remote_id), "credentials": {"keep": "value"}}

    async def update_account(self, remote_id, payload):
        if str(remote_id) == "42":
            raise SyncClientError("boom", status_code=500, response={"message": "bad"})
        self.updated.append(str(remote_id))
        return SyncClientResult(200, {"id": int(remote_id), **payload}, {"ok": True})


def _add_channel_with_models(db_session):
    channel = Channel(name="main", provider_type="openai", base_url="https://upstream.test", api_key="sk-live")
    db_session.add(channel)
    db_session.commit()
    db_session.add_all(
        [
            ChannelModel(channel_id=channel.id, model_id="gpt-4"),
            ChannelModel(channel_id=channel.id, model_id="gpt-4o"),
        ]
    )
    db_session.commit()
    return channel


@pytest.mark.asyncio
async def test_create_link_blocks_same_remote_name(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    client = FakeSub2APIClient(existing={"id": 1, "name": "union_main"})

    with pytest.raises(ValueError, match="已存在同名对象"):
        await create_channel_sync_link(db_session, channel, target, "1,2", 50, 3, client=client)

    assert db_session.query(ChannelSyncLink).count() == 0
    assert db_session.query(SyncEvent).count() == 1


@pytest.mark.asyncio
async def test_create_sub2api_link_persists_remote_id(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    client = FakeSub2APIClient()
    link = await create_channel_sync_link(db_session, channel, target, "1,2", 60, 5, client=client)

    assert link.remote_type == "account"
    assert link.remote_id == "42"
    assert link.remote_name == "union_main"
    assert link.last_sync_status == "success"
    assert json.loads(link.sub2api_group_ids_json) == [1, 2]
    assert client.created_payload["credentials"]["model_mapping"] == {"gpt-4": "gpt-4", "gpt-4o": "gpt-4o"}


@pytest.mark.asyncio
async def test_create_newapi_link_searches_created_channel_id(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    client = FakeNewAPIClient()
    link = await create_channel_sync_link(db_session, channel, target, "", 50, 3, client=client)

    assert link.remote_type == "channel"
    assert link.remote_id == "9"
    assert client.created_payload["models"] == "gpt-4,gpt-4o"


@pytest.mark.asyncio
async def test_sync_failure_updates_link_error_without_raising(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42", remote_name="union_main")
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeSub2APIClient(fail_update=True), action="manual_update")

    assert not result
    assert link.last_sync_status == "failed"
    assert link.last_sync_error == "目标站点同步失败：HTTP 500"
    assert db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).count() == 1


@pytest.mark.asyncio
async def test_manual_retry_success_clears_error(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_main",
        last_sync_status="failed",
        last_sync_error="old",
    )
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeSub2APIClient(), action="manual_update")

    assert result
    assert link.last_sync_status == "success"
    assert link.last_sync_error is None
    assert link.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_payload_validation_error_marks_failed_without_raising(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_main",
        sub2api_group_ids_json="bad",
    )
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeSub2APIClient(), action="manual_update")

    assert not result
    assert link.last_sync_status == "failed"
    assert "分组 ID" in link.last_sync_error
    event = db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).one()
    assert "分组 ID" in event.message


@pytest.mark.asyncio
async def test_sync_link_target_type_mismatch_records_failure(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42", remote_name="union_main")
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeNewAPIClient(), action="manual_update")

    assert not result
    assert link.last_sync_status == "failed"
    assert "远端对象类型" in link.last_sync_error
    assert db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).count() == 1


@pytest.mark.asyncio
async def test_sync_channel_links_counts_failures_and_continues(db_session):
    channel = _add_channel_with_models(db_session)
    first_target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    second_target = SyncTarget(name="sub2", target_type="sub2api", base_url="https://sub2.test", auth_config_json="{}")
    db_session.add_all([first_target, second_target])
    db_session.commit()
    db_session.add_all(
        [
            ChannelSyncLink(channel_id=channel.id, target_id=first_target.id, remote_type="account", remote_id="42", remote_name="union_main"),
            ChannelSyncLink(channel_id=channel.id, target_id=second_target.id, remote_type="account", remote_id="43", remote_name="union_second"),
        ]
    )
    db_session.commit()

    client = RoutingClient()
    success_count, failure_count = await sync_channel_links(db_session, channel, action="manual_update", client=client)

    assert (success_count, failure_count) == (1, 1)
    assert client.updated == ["43"]
    assert db_session.query(SyncEvent).filter(SyncEvent.success.is_(True)).count() == 1
    assert db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).count() == 1


@pytest.mark.asyncio
async def test_event_response_redacts_secret_strings_and_remains_json_loadable(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42", remote_name="union_main")
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeSensitiveErrorClient(), action="manual_update")

    assert not result
    event = db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).one()
    decoded = json.loads(event.response_json)
    encoded = json.dumps(decoded, ensure_ascii=False)
    assert "sk-live" not in encoded
    assert "Bearer secret" not in encoded
    assert "hunter2" not in encoded
    assert "password" in encoded


def test_record_sync_event_redacts_request_and_response_payloads(db_session):
    record_sync_event(
        db_session,
        channel_id=None,
        target_id=None,
        action="manual_update",
        success=False,
        request_payload={"credentials": {"api_key": "sk-live"}},
        response_payload='Authorization: Bearer secret\npassword: hunter2\n{"key":"sk-live"}',
    )
    db_session.commit()

    event = db_session.query(SyncEvent).one()
    json.loads(event.request_json)
    response = json.dumps(json.loads(event.response_json), ensure_ascii=False)
    assert "sk-live" not in event.request_json
    assert "sk-live" not in response
    assert "Bearer secret" not in response
    assert "hunter2" not in response


@pytest.mark.asyncio
async def test_sync_failure_message_is_sanitized_before_persisting(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()
    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account", remote_id="42", remote_name="union_main")
    db_session.add(link)
    db_session.commit()

    result = await sync_existing_link(db_session, link, client=FakeLeakyUpdateErrorClient(), action="manual_update")

    assert not result
    event = db_session.query(SyncEvent).filter(SyncEvent.success.is_(False)).one()
    persisted = f"{link.last_sync_error} {event.message}"
    assert "HTTP 500" in persisted
    assert "Authorization" not in persisted
    assert "secret" not in persisted
    assert "password" not in persisted
    assert "hunter2" not in persisted


@pytest.mark.asyncio
async def test_create_sub2api_link_rejects_invalid_priority_and_concurrency(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="sub", target_type="sub2api", base_url="https://sub.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    with pytest.raises(ValueError, match="优先级"):
        await create_channel_sync_link(db_session, channel, target, "", -1, 3, client=FakeSub2APIClient())
    with pytest.raises(ValueError, match="并发"):
        await create_channel_sync_link(db_session, channel, target, "", 50, 0, client=FakeSub2APIClient())

    assert db_session.query(ChannelSyncLink).count() == 0


@pytest.mark.asyncio
async def test_create_newapi_event_response_includes_create_and_lookup_sources(db_session):
    channel = _add_channel_with_models(db_session)
    target = SyncTarget(name="new", target_type="new_api", base_url="https://new.test", auth_config_json="{}")
    db_session.add(target)
    db_session.commit()

    link = await create_channel_sync_link(db_session, channel, target, "", 50, 3, client=FakeNewAPIClient())

    event = db_session.query(SyncEvent).filter(SyncEvent.success.is_(True)).one()
    response = json.loads(event.response_json)
    assert link.remote_id == "9"
    assert response["create"]["success"] is True
    assert response["lookup"]["id"] == 9
    assert response["remote_id"] == "9"
