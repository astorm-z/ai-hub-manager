from datetime import datetime, timezone

from app.time_utils import format_dt


def test_format_dt_uses_asia_shanghai_timezone():
    assert format_dt(datetime(2026, 5, 8, 0, 30, 0, tzinfo=timezone.utc)) == "2026-05-08 08:30:00"


def test_format_dt_treats_naive_datetime_as_utc():
    assert format_dt(datetime(2026, 5, 8, 0, 30, 0)) == "2026-05-08 08:30:00"
