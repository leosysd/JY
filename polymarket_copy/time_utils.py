from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Union


BEIJING_TZ = timezone(timedelta(hours=8))
BEIJING_TIMEZONE_NAME = "Asia/Shanghai"
Timestamp = Union[int, float]


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_now_iso(timespec: str = "seconds") -> str:
    return beijing_now().isoformat(timespec=timespec)


def beijing_datetime_from_ts(timestamp: Timestamp) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=BEIJING_TZ)


def beijing_iso_from_ts(timestamp: Timestamp, timespec: str = "seconds") -> str:
    return beijing_datetime_from_ts(timestamp).isoformat(timespec=timespec)


def beijing_iso_or_empty(timestamp: object, timespec: str = "seconds") -> str:
    try:
        numeric = int(float(timestamp))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if numeric <= 0:
        return ""
    return beijing_iso_from_ts(numeric, timespec=timespec)


def beijing_text_from_ts(timestamp: Timestamp, fmt: str = "%m-%d %H:%M:%S") -> str:
    return beijing_datetime_from_ts(timestamp).strftime(fmt)
