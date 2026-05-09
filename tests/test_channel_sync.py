import json

import pytest

from app.models import Channel, ChannelModel, ChannelSyncLink, SyncEvent, SyncTarget
from app.services.channel_sync import create_channel_sync_link, sync_existing_link
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
    assert "update failed" in link.last_sync_error
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
