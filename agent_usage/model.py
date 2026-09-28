"""Normalized metrics. Input excludes cache reads and cache writes."""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TOKENS = ("input", "cache_read", "cache_write", "cache_write_1h", "output", "reasoning")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    if isinstance(value, (int, float)):
        return number(value)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else None
    except (AttributeError, ValueError, OverflowError):
        return None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def windows(now, zone):
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    return {
        "today": local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
        "last_7_days": now - timedelta(days=7).total_seconds(),
        "last_31_days": now - timedelta(days=31).total_seconds(),
    }


def counts(**values):
    return {key: int(number(values.get(key)) or 0) for key in TOKENS}


def context(used=None, capacity=None, percent=None):
    used, capacity, percent = number(used), number(capacity), number(percent)
    if percent is None and used is not None and capacity:
        percent = used / capacity * 100
    return {"used_tokens": used, "capacity_tokens": capacity, "used_percent": percent}


def limits(raw):
    """Whitelist metrics; never persist raw provider responses or credentials."""
    result = []
    for name, item in (raw or {}).items():
        if not isinstance(item, dict):
            continue
        used = next((number(item[k]) for k in
                     ("used_percent", "used_percentage", "usedPercent", "utilization")
                     if k in item), None)
        if used is None:
            continue
        duration = item.get("window_minutes", item.get("windowDurationMins"))
        reset = item.get("resets_at", item.get("resetsAt"))
        result.append({"name": str(name), "used_percent": used,
                       "window_minutes": number(duration), "resets_at": timestamp(reset)})
    return result
