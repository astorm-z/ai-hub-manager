import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.database import Base, ensure_schema_compatibility
from app.models import Channel, ChannelSyncLink, SyncEvent, SyncTarget


def test_sync_target_link_and_event_persist(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(
        name="sub2api-main",
        target_type="sub2api",
        base_url="https://sub2api.test",
        name_prefix="union_",
        auth_config_json=json.dumps({"admin_api_key": "admin-key"}),
    )
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type="account",
        remote_id="42",
        remote_name="union_c1",
        sub2api_group_ids_json="[1, 2]",
        sub2api_priority=50,
        sub2api_concurrency=3,
        last_sync_status="success",
    )
    db_session.add(link)
    db_session.commit()

    event = SyncEvent(
        channel_id=channel.id,
        target_id=target.id,
        link_id=link.id,
        action="import_create",
        success=True,
        status_code=200,
        message="created",
        request_json="{}",
        response_json="{}",
    )
    db_session.add(event)
    db_session.commit()

    stored = db_session.query(ChannelSyncLink).one()
    assert stored.channel.name == "c1"
    assert stored.target.name == "sub2api-main"
    assert stored.sub2api_priority == 50
    assert stored.newapi_groups == "default"
    assert db_session.query(SyncEvent).one().message == "created"


def test_newapi_groups_persist_on_sync_link(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="new-api", target_type="new_api", base_url="https://newapi.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(
        channel_id=channel.id,
        target_id=target.id,
        remote_type="channel",
        remote_id="10",
        newapi_groups="claude-code,claude-code-ot",
    )
    db_session.add(link)
    db_session.commit()

    stored = db_session.query(ChannelSyncLink).one()
    assert stored.newapi_groups == "claude-code,claude-code-ot"


def test_sub2api_link_defaults_priority_one_and_concurrency_ten(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="sub2api", target_type="sub2api", base_url="https://sub2api.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="account")
    db_session.add(link)
    db_session.commit()

    stored = db_session.query(ChannelSyncLink).one()
    assert stored.sub2api_priority == 1
    assert stored.sub2api_concurrency == 10


def test_schema_compatibility_adds_newapi_groups_to_existing_channel_sync_links_table():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE channel_sync_links RENAME TO channel_sync_links_new_schema"))
            conn.execute(
                text(
                    """
                    CREATE TABLE channel_sync_links (
                        id INTEGER NOT NULL PRIMARY KEY,
                        channel_id INTEGER NOT NULL,
                        target_id INTEGER NOT NULL,
                        remote_type VARCHAR(32) NOT NULL,
                        remote_id VARCHAR(120),
                        remote_name VARCHAR(200),
                        sync_enabled BOOLEAN NOT NULL,
                        sub2api_group_ids_json TEXT NOT NULL,
                        sub2api_priority INTEGER NOT NULL,
                        sub2api_concurrency INTEGER NOT NULL,
                        last_sync_status VARCHAR(32) NOT NULL,
                        last_sync_error TEXT,
                        last_synced_at DATETIME,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(text("DROP TABLE channel_sync_links_new_schema"))

        ensure_schema_compatibility(engine)
        ensure_schema_compatibility(engine)

        columns = {column["name"] for column in inspect(engine).get_columns("channel_sync_links")}
        assert "newapi_groups" in columns
    finally:
        engine.dispose()


def test_channel_target_pair_is_unique(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="new-api", target_type="new_api", base_url="https://newapi.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel"))
    db_session.commit()

    db_session.add(ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_deleting_channel_removes_local_links_not_events(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="new-api", target_type="new_api", base_url="https://newapi.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel", remote_id="10")
    db_session.add(link)
    db_session.commit()
    db_session.add(SyncEvent(channel_id=channel.id, target_id=target.id, link_id=link.id, action="manual_update", success=True))
    db_session.commit()

    db_session.delete(channel)
    db_session.commit()

    assert db_session.query(ChannelSyncLink).count() == 0
    assert db_session.query(SyncEvent).count() == 1
    stored_event = db_session.query(SyncEvent).one()
    assert stored_event.channel_id is None
    assert stored_event.link_id is None
    assert stored_event.target_id == target.id


def test_deleting_target_removes_local_links_not_events(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://local.test", api_key="local-key")
    target = SyncTarget(name="new-api", target_type="new_api", base_url="https://newapi.test", auth_config_json="{}")
    db_session.add_all([channel, target])
    db_session.commit()

    link = ChannelSyncLink(channel_id=channel.id, target_id=target.id, remote_type="channel", remote_id="10")
    db_session.add(link)
    db_session.commit()
    db_session.add(SyncEvent(channel_id=channel.id, target_id=target.id, link_id=link.id, action="manual_update", success=True))
    db_session.commit()

    db_session.delete(target)
    db_session.commit()

    assert db_session.query(ChannelSyncLink).count() == 0
    assert db_session.query(SyncEvent).count() == 1
    stored_event = db_session.query(SyncEvent).one()
    assert stored_event.channel_id == channel.id
    assert stored_event.target_id is None
    assert stored_event.link_id is None
