"""Daily, weekly and monthly views over the hourly rollups, for the report pane and CLI.

Costs are the collector's API-equivalent estimates, never billed amounts."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .model import TOKENS

PERIODS = ("day", "week", "month")
HOUR_FLOOR = 3600  # A month projection needs at least an hour of data.


def tokens_total(tokens):
    # cache_write_1h and reasoning are subsets of cache_write and output.
    return sum(tokens.get(k, 0) for k in ("input", "cache_read", "cache_write", "output"))


def bucket(day, period):
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return day


def following(start, period):
    if period == "day":
        return start + timedelta(days=1)
    if period == "week":
        return start + timedelta(days=7)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def label(start, period):
    if period == "day":
        return start.strftime("%a %b %d")
    if period == "week":
        end = start + timedelta(days=6)
        return f"W{start.isocalendar().week:02d} {start:%b %d}–{end:%b %d}"
    return start.strftime("%B %Y")


def midnight(day, zone):
    return datetime(day.year, day.month, day.day, tzinfo=zone).timestamp()


def empty(start, period, zone):
    return {"start": start, "label": label(start, period), "begin": midnight(start, zone),
            "end": midnight(following(start, period), zone),
            "cost": Decimal(0), "unpriced": 0, "records": 0, "tokens": dict.fromkeys(TOKENS, 0),
            "agents": {}, "models": {}, "projects": {}, "sessions": {}, "status": {}, "quota": {},
            "days": set()}


def add(group, name, row, tokens):
    entry = group.setdefault(name, {"cost": Decimal(0), "tokens": 0, "records": 0, "unpriced": 0,
                                    "priced": False, "agent": row["agent"]})
    if row["cost"] is not None:
        entry["cost"] += row["cost"]
        entry["priced"] = True
    entry["tokens"] += tokens
    entry["records"] += row["records"]
    entry["unpriced"] += row["unpriced"]
    return entry


class History:
    """Rollups loaded once; each view is computed in memory from them."""

    def __init__(self, db, zone, now, config=None):
        self.zone_name, self.zone, self.now = zone, ZoneInfo(zone), now
        self.config = (config or {}).get("report", {})
        self.usage = [dict(r) for r in db.execute("SELECT * FROM rollup_usage ORDER BY hour")]
        for row in self.usage:
            row["tokens"] = json.loads(row["tokens"])
            row["cost"] = Decimal(row["cost"]) if row["cost"] is not None else None
        self.status = [dict(r) for r in db.execute("SELECT * FROM rollup_status")]
        self.quota = [dict(r) for r in db.execute("SELECT * FROM rollup_quota")]
        self.projects = {r[0]: r[1] for r in db.execute("SELECT session,project FROM rollup_sessions")}
        self._local = {}

    def local(self, hour):
        if hour not in self._local:
            self._local[hour] = datetime.fromtimestamp(hour, self.zone)
        return self._local[hour]

    def today(self):
        return datetime.fromtimestamp(self.now, self.zone).date()

    def buckets(self, period):
        """Every period with any usage or activity, newest first."""
        found = {}

        def get(hour):
            start = bucket(self.local(hour).date(), period)
            if start not in found:
                found[start] = empty(start, period, self.zone)
            return found[start]

        for row in self.usage:
            b = get(row["hour"])
            count = tokens_total(row["tokens"])
            if row["cost"] is not None:
                b["cost"] += row["cost"]
            b["unpriced"] += row["unpriced"]
            b["records"] += row["records"]
            for k in TOKENS:
                b["tokens"][k] += row["tokens"].get(k, 0)
            b["days"].add(self.local(row["hour"]).date())
            add(b["agents"], row["agent"], row, count)
            add(b["models"], row["model"] or "unknown", row, count)
            project = self.projects.get(row["session"]) or "unknown"
            add(b["projects"], project, row, count)
            add(b["sessions"], row["session"], row, count)["project"] = project
            s = b["sessions"][row["session"]]
            s["first"] = min(s.get("first", row["hour"]), row["hour"])
            s["last"] = max(s.get("last", row["hour"]), row["hour"])
        for row in self.status:
            b = get(row["hour"])
            b["status"][row["status"]] = b["status"].get(row["status"], 0) + row["seconds"]
        for row in self.quota:
            b = get(row["hour"])
            key = (row["agent"], row["name"])
            b["quota"][key] = max(b["quota"].get(key, 0), row["used_percent"])
        return [found[k] for k in sorted(found, reverse=True)]

    def cost_between(self, begin, end):
        return sum((r["cost"] for r in self.usage if r["cost"] is not None and begin <= r["hour"] < end),
                   Decimal(0))

    def headline(self):
        today = self.today()
        result = {}
        for period in PERIODS:
            start = bucket(today, period)
            begin = midnight(start, self.zone)
            previous = bucket(start - timedelta(days=1), period)
            previous_begin = midnight(previous, self.zone)
            elapsed = self.now - begin
            result[period] = {
                "label": label(start, period),
                "cost": self.cost_between(begin, self.now + 1),
                # The previous period up to the same elapsed point, for a fair comparison.
                "previous_same_point": self.cost_between(previous_begin, previous_begin + elapsed),
                "previous_total": self.cost_between(previous_begin, begin),
            }
        month = result["month"]
        start = bucket(today, "month")
        length = midnight(following(start, "month"), self.zone) - midnight(start, self.zone)
        elapsed = self.now - midnight(start, self.zone)
        projected = month["cost"] * Decimal(length) / Decimal(max(elapsed, HOUR_FLOOR))
        month["projected"] = projected.quantize(Decimal("0.01"))
        active = {self.local(r["hour"]).date() for r in self.usage if r["records"]}
        streak, day = 0, today if today in active else today - timedelta(days=1)
        while day in active:
            streak, day = streak + 1, day - timedelta(days=1)
        result["streak_days"] = streak
        result["active_days"] = len(active)
        result["first_day"] = min(active) if active else None
        daily = {}
        for r in self.usage:
            if r["cost"] is not None:
                d = self.local(r["hour"]).date()
                daily[d] = daily.get(d, Decimal(0)) + r["cost"]
        result["daily"] = [(today - timedelta(days=i), daily.get(today - timedelta(days=i), Decimal(0)))
                           for i in range(29, -1, -1)]
        subscriptions = self.config.get("subscriptions", {})
        result["subscriptions"] = {}
        for agent, monthly in subscriptions.items():
            spent = sum((r["cost"] for r in self.usage if r["agent"] == agent and r["cost"] is not None
                         and r["hour"] >= midnight(start, self.zone)), Decimal(0))
            result["subscriptions"][agent] = {"monthly": Decimal(str(monthly)), "value": spent,
                                              "ratio": spent / Decimal(str(monthly)) if monthly else None}
        return result

    def heatmap(self, begin=None, end=None, days=28):
        """Weekday x local hour grids of agent working time (seconds) and estimated cost
        in [begin, end); the last `days` days by default."""
        if begin is None:
            begin, end = midnight(self.today() - timedelta(days=days - 1), self.zone), self.now + 1
        else:
            days = round((end - begin) / 86400)
        work = [[0.0] * 24 for _ in range(7)]
        cost = [[0.0] * 24 for _ in range(7)]
        active = {"working_seconds": set(), "cost": set()}
        for r in self.status:
            if begin <= r["hour"] < end and r["status"] == "working":
                t = self.local(r["hour"])
                work[t.weekday()][t.hour] += r["seconds"]
                active["working_seconds"].add(t.date())
        for r in self.usage:
            if begin <= r["hour"] < end and r["cost"] is not None:
                t = self.local(r["hour"])
                cost[t.weekday()][t.hour] += float(r["cost"])
                active["cost"].add(t.date())
        # Active days per grid: working time starts later than the imported spend.
        return {"begin": begin, "end": end, "days": days,
                "active_days": {k: len(v) for k, v in active.items()},
                "working_seconds": work, "cost": cost}

    def status_since(self):
        """Start of the working-time history, which only the live collector records."""
        return min((r["hour"] for r in self.status), default=None)



def missing_prices(b):
    """Models without a catalog price, for agents that are otherwise priced. Agents with no
    priced usage at all (Lumo Code is a subscription) are expected to be unpriced."""
    priced = {a for a, e in b["agents"].items() if e["priced"]}
    return sorted(m for m, e in b["models"].items() if e["unpriced"] and e["agent"] in priced)


def cache_hit(tokens):
    prompt = tokens.get("input", 0) + tokens.get("cache_read", 0) + tokens.get("cache_write", 0)
    return tokens.get("cache_read", 0) / prompt if prompt else None


def plain(value):
    """JSON-safe copy: Decimal as string, dates as ISO, sets as sorted lists."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {("/".join(k) if isinstance(k, tuple) else str(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = sorted(value) if isinstance(value, set) else value
        return [plain(v) for v in items]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def report(db, zone, now, config, period="day"):
    history = History(db, zone, now, config)
    buckets = history.buckets(period)
    for b in buckets:
        b["cache_hit"] = cache_hit(b["tokens"])
        b["tokens"]["total"] = tokens_total(b["tokens"])
        b["sessions"] = dict(sorted(b["sessions"].items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["tokens"]))[:20])
    return plain({"schema": "agent-usage-report/1", "timezone": zone, "period": period,
                  "basis": "API-equivalent token estimates from hourly rollups; not billed amounts",
                  "headline": history.headline(), "periods": buckets, "heatmap": history.heatmap()})
