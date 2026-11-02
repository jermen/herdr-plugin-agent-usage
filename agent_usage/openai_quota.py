"""Opt-in OpenAI subscription limits for any supported terminal harness.

Read the explicitly selected Codex/CLIProxyAPI credential, never discover an
account by guessing, and never refresh or persist its tokens. The proxy/native
client remains responsible for OAuth refresh. No management API is needed.
"""
import json
import re
from pathlib import Path

from .billing import get_json
from .model import digest, number, timestamp
from .providers import model_provider

URL = "https://chatgpt.com/backend-api/wham/usage"
SOURCE = "openai_codex"


def credentials(path):
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("credential file too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("disabled"):
        raise ValueError("credential unavailable")
    if data.get("type") == "codex":  # CLIProxyAPI's flat OAuth record
        tokens, expires = data, timestamp(data.get("expired"))
        if data.get("expired") is not None and expires is None:
            raise ValueError("invalid credential expiry")
    elif isinstance(data.get("tokens"), dict) and data.get("auth_mode") in (None, "chatgpt"):
        tokens, expires = data["tokens"], None  # native Codex auth.json
    else:
        raise ValueError("not a Codex OAuth credential")
    token, account = tokens.get("access_token"), tokens.get("account_id")
    if not all(isinstance(v, str) and v and "\n" not in v and "\r" not in v for v in (token, account)):
        raise ValueError("missing Codex OAuth fields")
    return token, account, expires


def parse_openai(data, now):
    """Normalize only account windows, not code-review or unrelated model quotas."""
    windows = []
    raw = data.get("rate_limit")
    if not isinstance(raw, dict):
        return windows
    for name in ("primary", "secondary"):
        item = raw.get(name + "_window")
        if not isinstance(item, dict):
            continue
        used = number(item.get("used_percent"))
        duration = number(item.get("limit_window_seconds"))
        if used is None or used > 100 or not duration:
            continue
        reset = timestamp(item.get("reset_at"))
        after = number(item.get("reset_after_seconds"))
        if reset is None and after is not None:
            reset = now + after
        windows.append({"name": name, "used_percent": used,
                        "window_minutes": duration / 60, "resets_at": reset})
    return windows


def refresh_openai(store, config, now):
    settings = config.get("openai_quota")
    if not settings:
        return
    if not isinstance(settings, dict) or not isinstance(settings.get("credentials_file"), str):
        raise ValueError("openai_quota requires credentials_file")
    agents = settings.get("agents", ["claude"])
    if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
        raise ValueError("openai_quota agents must be a list")
    interval = max(300, number(settings.get("poll_seconds")) or 300)
    sessions = []
    for row in store.db.execute("SELECT key,agent,telemetry FROM sessions WHERE last_seen=?", (now,)):
        current = json.loads(row["telemetry"])
        provider = current.get("provider") or model_provider(current.get("model"))
        if row["agent"] in agents and provider == "openai":
            sessions.append(row["key"])
    if not sessions:
        return

    path = Path(settings["credentials_file"]).expanduser().resolve()
    cache_key = digest([SOURCE, str(path)])
    row = store.db.execute("SELECT value FROM quota_cache WHERE source=?", (cache_key,)).fetchone()
    cached = json.loads(row[0]) if row else {}
    account_fingerprint = None
    try:
        token, account, expires = credentials(path)
        account_fingerprint = digest([SOURCE, account])
        # Replacing a credential file with another account must not reuse its quota.
        if cached.get("account") != account_fingerprint:
            cached = {}
        if expires is not None and expires <= now:
            status = "credential_expired"
        elif now - cached.get("attempted_at", 0) < interval:
            status = cached["status"]
        else:
            try:
                data = get_json(URL, {"Authorization": "Bearer " + token,
                                     "ChatGPT-Account-Id": account, "Accept": "application/json"})
                if not isinstance(data, dict) or data.get("account_id", account) != account:
                    raise ValueError("quota account mismatch")
                windows = parse_openai(data, now)
                if not windows:
                    raise ValueError("no account quota windows")
                plan = data.get("plan_type")
                plan = plan if isinstance(plan, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", plan) else None
                cached.update(limits=windows, observed_at=now, plan_type=plan)
                status = "ok"
            except (OSError, ValueError, TypeError):
                # Keep last good metrics and their original freshness timestamp.
                status = "fetch_failed"
            cached.update(attempted_at=now, account=account_fingerprint, status=status)
            store.db.execute("INSERT OR REPLACE INTO quota_cache VALUES(?,?)",
                             (cache_key, json.dumps(cached)))
    except (OSError, ValueError, TypeError):
        status = "credential_missing"
        # A missing/invalid credential cannot establish which account is in use.
        cached = {}

    for key in sessions:
        patch = {"observed_at": now, "billing_mode": "subscription",
                 "quota_status": status, "quota_attempted_at": now,
                 "quota_source": SOURCE, "quota_account": account_fingerprint}
        patch["quota_config"] = cache_key
        store.telemetry(key, patch)
        if cached.get("limits"):
            store.telemetry(key, {"observed_at": cached["observed_at"],
                                 "limits": cached["limits"], "limits_provider": "openai",
                                 "limits_source": SOURCE, "limits_scope": "account",
                                 "plan_type": cached.get("plan_type")})
