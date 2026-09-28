"""Opt-in Kimi account quota, using the same endpoint as Kimi CLI /usage."""
import json
import os
from pathlib import Path

from .billing import get_json
from .model import number, timestamp


def parse_kimi(data):
    rows = []
    items = [("weekly", data.get("usage") or {}, {})]
    for i, item in enumerate(data.get("limits") or []):
        items.append((str(item.get("name") or f"limit_{i}"), item.get("detail") or item,
                      item.get("window") or {}))
    for name, item, window in items:
        maximum, used = number(item.get("limit")), number(item.get("used"))
        if used is None and maximum is not None:
            remaining = number(item.get("remaining"))
            used = max(0, maximum - remaining) if remaining is not None else None
        if used is None or not maximum:
            continue
        reset = next((item[k] for k in ("reset_at", "resetAt", "reset_time", "resetTime") if k in item), None)
        unit = str(window.get("timeUnit", "")).upper()
        multiplier = next((n for key, n in (("MINUTE", 1), ("HOUR", 60), ("DAY", 1440),
                                          ("SECOND", 1 / 60)) if key in unit), None)
        duration = number(window.get("duration"))
        rows.append({"name": name, "used": used, "limit": maximum,
                     "used_percent": used / maximum * 100, "resets_at": timestamp(reset),
                     "window_minutes": duration * multiplier if duration and multiplier else None})
    return rows


def refresh_kimi(store, config, now):
    settings = config.get("kimi_quota")
    if not settings:
        return
    sessions = store.db.execute("SELECT key,telemetry FROM sessions WHERE agent='kimi' AND last_seen=?", (now,)).fetchall()
    if not sessions:
        return
    latest = max(json.loads(r[1]).get("metric_observed_at", {}).get("quota_attempted_at", 0) for r in sessions)
    if now - latest < 300:
        return
    token = os.environ.get(settings.get("key_env", ""))
    if not token and settings.get("credentials_file"):
        try:
            data = json.loads(Path(settings["credentials_file"]).expanduser().read_text())
            if number(data.get("expires_at")) and data["expires_at"] > now:
                token = data.get("access_token")
        except (OSError, ValueError):
            pass
    patch = {"observed_at": now, "quota_attempted_at": now, "quota_status": "credential_missing"}
    if token:
        try:
            data = get_json("https://api.kimi.com/coding/v1/usages", {"Authorization": "Bearer " + token})
            parsed = parse_kimi(data)
            if not parsed:
                raise ValueError("no quota windows")
            patch.update(limits=parsed, limits_scope="account", quota_status="ok")
        except (OSError, ValueError, TypeError, KeyError):
            patch["quota_status"] = "fetch_failed"
    for row in sessions:
        store.telemetry(row[0], patch)
