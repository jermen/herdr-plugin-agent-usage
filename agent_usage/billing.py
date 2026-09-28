"""Optional billed account costs. Never attribute account totals to a session."""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import iso, timestamp


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, headers):
    # Provider URLs are fixed; do not forward credentials on HTTP redirects.
    with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=15) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("provider response too large")
    return json.loads(raw)


def fetch_costs(provider, token, now, request=get_json):
    today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start = today - 30 * 86400
    if provider == "openai":
        url = "https://api.openai.com/v1/organization/costs"
        params = {"start_time": int(start), "end_time": int(now), "bucket_width": "1d", "limit": 31}
        headers = {"Authorization": "Bearer " + token}
    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/organizations/cost_report"
        params = {"starting_at": iso(start), "ending_at": iso(now), "bucket_width": "1d", "limit": 31}
        headers = {"x-api-key": token, "anthropic-version": "2023-06-01"}
    else:
        raise ValueError("unsupported billing provider")
    buckets = []
    pages = set()
    for _ in range(100):
        data = request(url + "?" + urlencode(params), headers)
        for bucket in data["data"]:
            at = bucket.get("start_time") if provider == "openai" else timestamp(bucket.get("starting_at"))
            end = bucket.get("end_time") if provider == "openai" else timestamp(bucket.get("ending_at"))
            if at is None or end is None:
                raise ValueError("missing billing bucket boundary")
            amount = Decimal(0)
            for row in bucket["results"]:
                value = row.get("amount")
                currency = value.get("currency") if provider == "openai" else row.get("currency")
                if str(currency).upper() != "USD":
                    raise ValueError("unexpected billing currency")
                item = Decimal(str(value.get("value"))) if provider == "openai" else Decimal(str(value)) / 100
                if not item.is_finite():
                    raise ValueError("invalid billing amount")
                amount += item
            buckets.append({"start": at, "end": end, "amount": str(amount), "currency": "USD"})
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page or page in pages:
            raise ValueError("invalid billing pagination")
        pages.add(page)
        params["page"] = page
    else:
        raise ValueError("billing pagination limit")
    periods = {}
    for label, days in (("today", 1), ("last_7_days", 7), ("last_31_days", 31)):
        since = today - (days - 1) * 86400
        selected = [b for b in buckets if since <= b["start"] <= now]
        periods[label] = {"start": iso(since), "end": iso(now), "currency": "USD",
                          "amount": str(sum((Decimal(b["amount"]) for b in selected), Decimal(0))),
                          "bucket_count": len(selected)}
    return {"scope": "organization", "kind": "billed", "status": "ok", "observed_at": iso(now),
            "period_basis": "UTC calendar days including today; provider reporting may lag",
            "periods": periods, "buckets": buckets}


def refresh(store, config, now):
    for provider, settings in config.get("billing", {}).items():
        old = store.db.execute("SELECT value FROM billing WHERE provider=?", (provider,)).fetchone()
        previous = json.loads(old[0]) if old else {}
        last = timestamp(previous.get("attempted_at")) or 0
        if now - last < max(300, config.get("billing_poll_seconds", 900)):
            continue
        token = os.environ.get(settings.get("key_env", ""))
        if not token:
            value = {"scope": "organization", "kind": "billed", "status": "credential_missing"}
        else:
            try:
                value = fetch_costs(provider, token, now)
            except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
                # Preserve last good values and their timestamp, clearly marked stale.
                value = {**previous, "status": "fetch_failed", "stale": True}
        value["attempted_at"] = iso(now)
        store.db.execute("INSERT OR REPLACE INTO billing VALUES(?,?)", (provider, json.dumps(value)))


def import_spend(store, record, now):
    """Accept an actual session-attributed billing record from an external meter."""
    key = record["session_key"]
    if not store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
        raise ValueError("unknown session")
    amount = Decimal(str(record["amount"]))
    at = timestamp(record["timestamp"])
    if not amount.is_finite() or at is None or at > now:
        raise ValueError("invalid billed amount or timestamp")
    currency = str(record["currency"]).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("invalid currency")
    source, event_id = str(record["source"]), str(record["event_id"])
    if not source or not event_id or len(source) > 200 or len(event_id) > 200:
        raise ValueError("billing source and stable event ID are required")
    store.db.execute("INSERT OR REPLACE INTO spend VALUES(?,?,?,?,?,?)",
                     (key, source + ":" + event_id, at, str(amount), currency, source))
