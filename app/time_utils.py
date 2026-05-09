from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


APP_TZ = ZoneInfo("Asia/Shanghai")


def to_app_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(APP_TZ)


def format_dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    local_value = to_app_timezone(value)
    if local_value is None:
        return "-"
    return local_value.strftime(fmt)
