import json

from app.models import AlertEvent, Channel, ChannelModel, HealthCheck, now_utc
from app.schemas import ModelInfo
from app.services.monitoring import ensure_default_alert_rule, maybe_create_error_alert, sync_models


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
