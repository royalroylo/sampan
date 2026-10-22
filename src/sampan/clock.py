"""Timestamps. New values are written in UTC with an explicit offset (+00:00); display is machine-local time.

Older sampan wrote the author's local time (Asia/Taipei, +08:00). Strings carrying an offset keep meaning the same
instant; strings with no offset at all are read as the machine's local time.
"""
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def parse_iso(s):
    """An ISO string as an aware datetime (no offset = machine-local time); None when it is not one."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s).astimezone()
    except ValueError:
        return None


def to_local(s):
    """Like parse_iso but raises ValueError on bad input."""
    return datetime.fromisoformat(s).astimezone()


def fmt_local(dt):
    """'%m/%d %H:%M' in machine-local time ('-' for None)."""
    return dt.astimezone().strftime("%m/%d %H:%M") if dt else "-"
