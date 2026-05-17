import json

import pytest

from app.models import AlertEvent, Channel, ChannelModel, ExtractorTemplate, HealthCheck, now_utc
from app.schemas import ModelInfo
from app.services.monitoring import ensure_default_alert_rule, maybe_create_error_alert, run_channel_monitor, sync_models


def test_sync_models_stores_only_current_snapshot(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://example.test", api_key="key")
    db_session.add(channel)
    db_session.commit()

    change = sync_models(db_session, channel, [ModelInfo("gpt-a"), ModelInfo("gpt-b")])
    db_session.commit()
    assert change == {
        "old_models": [],
        "new_models": ["gpt-a", "gpt-b"],
        "added_models": ["gpt-a", "gpt-b"],
        "removed_models": [],
    }
    assert sorted(item.model_id for item in db_session.query(ChannelModel).all()) == ["gpt-a", "gpt-b"]

    change = sync_models(db_session, channel, [ModelInfo("gpt-b")])
    db_session.commit()
    assert change == {
        "old_models": ["gpt-a", "gpt-b"],
        "new_models": ["gpt-b"],
        "added_models": [],
        "removed_models": ["gpt-a"],
    }
    assert [item.model_id for item in db_session.query(ChannelModel).all()] == ["gpt-b"]

    change = sync_models(db_session, channel, [ModelInfo("gpt-a"), ModelInfo("gpt-b")])
    assert change == {
        "old_models": ["gpt-b"],
        "new_models": ["gpt-a", "gpt-b"],
        "added_models": ["gpt-a"],
        "removed_models": [],
    }


def test_error_threshold_alert(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://example.test", api_key="key")
    db_session.add(channel)
    db_session.commit()
    rule = ensure_default_alert_rule(db_session, channel)
    rule.error_threshold = 2
    rule.error_window_minutes = 10
    rule.cooldown_minutes = 30
    db_session.commit()

    db_session.add(HealthCheck(channel_id=channel.id, check_type="models", success=False, message="fail 1", created_at=now_utc()))
    db_session.commit()
    maybe_create_error_alert(db_session, channel)
    assert db_session.query(AlertEvent).count() == 0

    db_session.add(HealthCheck(channel_id=channel.id, check_type="models", success=False, message="fail 2", created_at=now_utc()))
    db_session.commit()
    maybe_create_error_alert(db_session, channel)
    event = db_session.query(AlertEvent).one()
    assert event.alert_type == "error_threshold"
    assert json.loads(event.payload_json)["error_count"] == 2


@pytest.mark.asyncio
async def test_run_channel_monitor_skips_model_checks_when_model_check_disabled(db_session, monkeypatch):
    calls = []
    channel = Channel(
        name="c1",
        provider_type="openai",
        base_url="https://example.test",
        api_key="key",
        probe_model="gpt-live",
        model_check_enabled=False,
        model_check_interval_minutes=10,
    )
    db_session.add(channel)
    db_session.commit()

    async def fake_refresh_channel_models(db, item):
        calls.append(("models", item.id))

    async def fake_probe_channel_model(db, item):
        calls.append(("probe", item.id))

    monkeypatch.setattr("app.services.monitoring.refresh_channel_models", fake_refresh_channel_models)
    monkeypatch.setattr("app.services.monitoring.probe_channel_model", fake_probe_channel_model)

    await run_channel_monitor(db_session, channel)

    assert calls == []


@pytest.mark.asyncio
async def test_run_channel_monitor_skips_balance_check_when_balance_check_disabled(db_session, monkeypatch):
    calls = []
    extractor = ExtractorTemplate(name="balance", template_json="{}")
    db_session.add(extractor)
    db_session.flush()
    channel = Channel(
        name="c1",
        provider_type="openai",
        base_url="https://example.test",
        api_key="key",
        extractor_template_id=extractor.id,
        balance_check_enabled=False,
        balance_check_interval_minutes=30,
    )
    db_session.add(channel)
    db_session.commit()

    async def fake_refresh_channel_balance(db, item):
        calls.append(("balance", item.id))

    monkeypatch.setattr("app.services.monitoring.refresh_channel_balance", fake_refresh_channel_balance)

    await run_channel_monitor(db_session, channel)

    assert calls == []
