from datetime import timedelta

from app.main import align_time_to_bucket, build_status_lanes
from app.models import Channel, HealthCheck, now_utc


def test_build_status_lanes_groups_recent_checks(db_session):
    channel = Channel(name="channel", provider_type="openai", base_url="https://example.test", api_key="key")
    db_session.add(channel)
    db_session.commit()
    end_time = align_time_to_bucket(now_utc(), 30)
    db_session.add_all(
        [
            HealthCheck(channel_id=channel.id, check_type="models", success=True, created_at=end_time - timedelta(minutes=80)),
            HealthCheck(channel_id=channel.id, check_type="models", success=False, status_code=500, message="fail", created_at=end_time - timedelta(minutes=20)),
        ]
    )
    db_session.commit()

    lanes = build_status_lanes(db_session, [channel], hours=2, bucket_minutes=30)

    assert len(lanes) == 1
    states = [bucket["state"] for bucket in lanes[0]["buckets"]]
    assert "up" in states
    assert "down" in states
    assert "unknown" in states
    assert lanes[0]["total"] == 2
    assert lanes[0]["failures"] == 1
    assert lanes[0]["uptime"] == 50


def test_build_status_lanes_marks_disabled_channel(db_session):
    channel = Channel(name="channel", provider_type="openai", base_url="https://example.test", api_key="key", enabled=False)
    db_session.add(channel)
    db_session.commit()

    lane = build_status_lanes(db_session, [channel], hours=1, bucket_minutes=30)[0]

    assert [bucket["state"] for bucket in lane["buckets"]] == ["disabled", "disabled"]


def test_build_status_lanes_defaults_to_12_hours_at_5_minutes(db_session):
    channel = Channel(name="channel", provider_type="openai", base_url="https://example.test", api_key="key")
    db_session.add(channel)
    db_session.commit()

    lane = build_status_lanes(db_session, [channel])[0]

    assert len(lane["buckets"]) == 144
    assert lane["window_label"] == "最近 12 小时"


def test_align_time_to_natural_5_minute_bucket():
    value = now_utc().replace(hour=16, minute=7, second=42, microsecond=123)

    aligned = align_time_to_bucket(value, 5)

    assert aligned.hour == 16
    assert aligned.minute == 5
    assert aligned.second == 0
    assert aligned.microsecond == 0


def test_align_time_keeps_existing_bucket_boundary():
    value = now_utc().replace(hour=16, minute=0, second=0, microsecond=0)

    aligned = align_time_to_bucket(value, 5)

    assert aligned.hour == 16
    assert aligned.minute == 0


def test_build_status_lanes_marks_mixed_bucket_degraded(db_session):
    channel = Channel(name="channel", provider_type="openai", base_url="https://example.test", api_key="key")
    db_session.add(channel)
    db_session.commit()
    bucket_time = align_time_to_bucket(now_utc(), 5) - timedelta(minutes=5)
    db_session.add_all(
        [
            HealthCheck(channel_id=channel.id, check_type="models", success=True, status_code=200, message="ok", created_at=bucket_time + timedelta(seconds=10)),
            HealthCheck(channel_id=channel.id, check_type="probe", success=False, status_code=500, message="Failed to validate API key", created_at=bucket_time + timedelta(seconds=20)),
        ]
    )
    db_session.commit()

    lane = build_status_lanes(db_session, [channel], hours=1, bucket_minutes=5)[0]
    degraded = [bucket for bucket in lane["buckets"] if bucket["state"] == "degraded"]

    assert len(degraded) == 1
    assert "失败 1 次" in degraded[0]["label"]
    assert "状态码 500" in degraded[0]["label"]
    assert "Failed to validate API key" in degraded[0]["label"]
