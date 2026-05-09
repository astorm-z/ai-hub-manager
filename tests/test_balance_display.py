from datetime import timedelta

from app.main import balance_snapshot_text, find_balance_for_check
from app.models import BalanceSnapshot, Channel, HealthCheck, now_utc
from app.schemas import BalanceResult
from app.services.monitoring import balance_check_message


def test_balance_check_message_includes_amounts_and_plan():
    message = balance_check_message(BalanceResult(is_valid=True, remaining=12.5, used=1.25, total=13.75, unit="USD", plan_name="default"))

    assert "余额查询成功" in message
    assert "余额 12.5 USD" in message
    assert "已用 1.25 USD" in message
    assert "总额 13.75 USD" in message
    assert "套餐 default" in message


def test_find_balance_for_check_matches_nearby_snapshot(db_session):
    channel = Channel(name="c1", provider_type="openai", base_url="https://example.test", api_key="key")
    db_session.add(channel)
    db_session.commit()
    created_at = now_utc()
    check = HealthCheck(channel_id=channel.id, check_type="balance", success=True, created_at=created_at, message="余额查询成功")
    snapshot = BalanceSnapshot(channel_id=channel.id, is_valid=True, remaining=8.5, unit="USD", created_at=created_at - timedelta(seconds=1))
    db_session.add_all([check, snapshot])
    db_session.commit()

    assert find_balance_for_check(db_session, check).id == snapshot.id


def test_balance_snapshot_text_formats_invalid_snapshot():
    snapshot = BalanceSnapshot(channel_id=1, is_valid=False, invalid_message="token invalid")

    assert balance_snapshot_text(snapshot) == "token invalid"
