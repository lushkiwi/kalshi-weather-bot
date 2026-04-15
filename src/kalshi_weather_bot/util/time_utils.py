from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def utcnow_ts() -> int:
    return int(utcnow().timestamp())


def local_day(dt: datetime, tz_name: str) -> date:
    return dt.astimezone(ZoneInfo(tz_name)).date()
