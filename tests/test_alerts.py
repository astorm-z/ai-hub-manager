from app.main import mark_all_alerts_acknowledged
from app.models import AlertEvent


def test_mark_all_alerts_acknowledged_marks_only_unread_count(db_session):
    db_session.add_all(
        [
            AlertEvent(alert_type="error_threshold", severity="critical", message="unread 1", acknowledged=False),
            AlertEvent(alert_type="model_change", severity="warning", message="unread 2", acknowledged=False),
            AlertEvent(alert_type="balance_threshold", severity="critical", message="read", acknowledged=True),
        ]
    )
    db_session.commit()

    count = mark_all_alerts_acknowledged(db_session)

    assert count == 2
    assert db_session.query(AlertEvent).filter(AlertEvent.acknowledged.is_(False)).count() == 0
    assert db_session.query(AlertEvent).filter(AlertEvent.acknowledged.is_(True)).count() == 3
