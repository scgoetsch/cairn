"""Timestamps. bd writes ISO-8601 UTC ("2026-09-30T10:01:12Z", sometimes with
nanoseconds); comparing such strings as text happens to work, but a space
separator, a numeric offset or a date-only value would silently compare wrong.
Everything that orders or compares times goes through `parse` instead."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_STAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d+))?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def parse(stamp: Optional[str]) -> Optional[datetime]:
    """Return an aware UTC datetime, or None when `stamp` is empty or malformed.
    A value without an offset is taken as UTC; a date alone is midnight UTC."""
    if not isinstance(stamp, str):
        return None
    match = _STAMP_RE.match(stamp.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        value = datetime(int(year), int(month), int(day), int(hour or 0),
                         int(minute or 0), int(second or 0), micro, tzinfo=timezone.utc)
    except ValueError:
        return None
    if offset and offset not in ("Z", "z"):
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        value -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return value


def epoch_seconds(stamp: Optional[str]) -> int:
    """Whole seconds since the epoch; 0 for a value that does not parse."""
    value = parse(stamp)
    return 0 if value is None else int(value.timestamp())


def in_future(stamp: Optional[str], now: str) -> bool:
    """True when `stamp` is later than `now`, to the second. An empty or
    malformed `stamp` is not in the future (the importer rejects malformed ones)."""
    value, reference = parse(stamp), parse(now)
    if value is None or reference is None:
        return False
    return int(value.timestamp()) > int(reference.timestamp())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
