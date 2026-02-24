"""
Time utilities – all times in UTC.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta


def utc_now() -> datetime:
    """Current time in UTC with timezone info."""
    return datetime.now(timezone.utc)


def utc_timestamp() -> float:
    """Current UTC epoch seconds."""
    return time.time()


def utc_iso() -> str:
    """ISO-8601 string for current UTC time."""
    return utc_now().isoformat()


def hours_since(dt: datetime) -> float:
    """Hours elapsed since *dt* (must be tz-aware UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = utc_now() - dt
    return delta.total_seconds() / 3600.0


def is_past_hours(dt: datetime, hours: float) -> bool:
    """True when *hours* have elapsed since *dt*."""
    return hours_since(dt) >= hours


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string into tz-aware UTC datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def today_midnight_utc() -> datetime:
    """Return midnight of today (UTC), tz-aware."""
    now = utc_now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def ms_to_sec(ms: int) -> float:
    return ms / 1000.0


def sec_to_ms(sec: float) -> int:
    return int(sec * 1000)
