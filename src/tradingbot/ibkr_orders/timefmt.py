"""Broker time strings (SDK-free)."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_broker_time(text: str) -> datetime | None:
    """'YYYYMMDD HH:MM:SS <tz>' (TWS), 'YYYYMMDD-HH:MM:SS' (UTC) or 'YYYYMMDD HH:MM:SS'."""
    if not text:
        return None
    parts = text.strip().split(" ")
    try:
        if len(parts) >= 3:
            return datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S").replace(
                tzinfo=ZoneInfo(parts[2])).astimezone(UTC)
        if "-" in parts[0] and len(parts) == 1:
            return datetime.strptime(parts[0], "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC)
        if len(parts) == 2:
            return datetime.strptime(text.strip(), "%Y%m%d %H:%M:%S").replace(tzinfo=UTC)
    except (ValueError, KeyError, OSError, ZoneInfoNotFoundError):
        return None
    return None
