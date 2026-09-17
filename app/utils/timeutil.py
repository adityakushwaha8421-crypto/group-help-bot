"""Time helpers. All DB timestamps are timezone-aware UTC. Includes a tolerant parser for the
many date formats seen in payment apps and admin panels (no external dependency)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MONTHS = {
    m: i
    for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)
}

_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %I:%M %p",
    "%d-%m-%Y %I:%M %p",
    "%d/%m/%Y, %I:%M %p",
    "%d %b %Y %I:%M %p",
    "%d %b %Y, %I:%M %p",
    "%d %b %Y %H:%M",
    "%d %b %Y, %H:%M",
    "%d %B %Y %I:%M %p",
    "%d %B %Y, %I:%M %p",
    "%d %b %Y · %H:%M",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%d %b, %Y %I:%M %p",
    "%d %b %Y %I:%M:%S %p",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d %b %Y",
    "%d %b, %Y",
    "%d %B %Y",
    "%d %B, %Y",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None, default_tz: str = "Asia/Kolkata") -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime | None, tz: str = "Asia/Kolkata") -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz))


def fmt_local(dt: datetime | None, tz: str = "Asia/Kolkata", fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    loc = to_local(dt, tz)
    return loc.strftime(fmt) if loc else "-"


def parse_datetime_loose(text: str | None, default_tz: str = "Asia/Kolkata") -> datetime | None:
    """Parse a human/admin-panel timestamp. Returns aware UTC datetime or None."""
    if not text:
        return None
    s = str(text).strip()
    # ISO with offset
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return ensure_utc(dt, default_tz)
    except ValueError:
        pass
    # explicit offset like "2026-07-29 14:34:00 +05:30"
    m = re.match(r"^(.*?)\s*([+-]\d{2}):?(\d{2})$", s)
    offset = None
    if m and re.search(r"\d", m.group(1)):
        s, sign_h, mins = m.group(1).strip(), m.group(2), m.group(3)
        offset = timezone(timedelta(hours=int(sign_h), minutes=int(mins) * (1 if int(sign_h) >= 0 else -1)))
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    cleaned = re.sub(r"\s+", " ", cleaned.replace(" ", " ").replace(",", ", ")).replace(" ,", ",")
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    cleaned = cleaned.replace("a.m.", "AM").replace("p.m.", "PM").replace("am", "AM").replace("pm", "PM")
    candidates = [cleaned, cleaned.replace(", ", " "), cleaned.replace(" · ", " ")]
    for cand in candidates:
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(cand, fmt)
            except ValueError:
                continue
            if offset:
                dt = dt.replace(tzinfo=offset)
            return ensure_utc(dt, default_tz)
    return None
