"""Transactional, idempotent storage and read-only consumer snapshots."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .model import TOKENS, iso, windows


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".snapshot-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "usage.sqlite3", timeout=15)
        os.chmod(self.directory / "usage.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY, agent TEXT, session_id TEXT, first_seen REAL,
                last_seen REAL, telemetry TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS usage (
                session TEXT, event_id TEXT, at REAL, model TEXT, tokens TEXT,
                PRIMARY KEY(session,event_id));
            CREATE INDEX IF NOT EXISTS usage_time ON usage(session,at);
            CREATE TABLE IF NOT EXISTS observations (
                session TEXT, kind TEXT, at REAL, value TEXT,
                PRIMARY KEY(session,kind,at));
            CREATE INDEX IF NOT EXISTS observation_time ON observations(session,kind,at);
            CREATE TABLE IF NOT EXISTS cursors (
                path TEXT PRIMARY KEY, inode TEXT, offset INTEGER, state TEXT);
            CREATE TABLE IF NOT EXISTS transcripts (
                session TEXT PRIMARY KEY, path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS spend (
                session TEXT, event_id TEXT, at REAL, amount TEXT, currency TEXT,
                source TEXT, PRIMARY KEY(session,event_id));
            CREATE TABLE IF NOT EXISTS billing (
                provider TEXT PRIMARY KEY, value TEXT);
        ''')
        if "details" not in {r[1] for r in self.db.execute("PRAGMA table_info(usage)")}:
            self.db.execute("ALTER TABLE usage ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")

    def ensure_session(self, agent, sid, now):
        key = agent + ":" + sid
        self.db.execute('''INSERT INTO sessions(key,agent,session_id,first_seen,last_seen)
                           VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen''',
                        (key, agent, sid, now, now))
        return key

    def bind_transcript(self, key, path):
        # Discovery state stays private; paths are not part of the consumer snapshot.
        self.db.execute("INSERT OR REPLACE INTO transcripts VALUES(?,?)", (key, str(path)))

    def transcript_path(self, key):
        row = self.db.execute("SELECT path FROM transcripts WHERE session=?", (key,)).fetchone()
        return row[0] if row else None

    def telemetry(self, key, patch):
        row = self.db.execute("SELECT telemetry FROM sessions WHERE key=?", (key,)).fetchone()
        old = json.loads(row[0])
        at = patch.get("observed_at", 0)
        # Track timestamps per metric; a newer quota report must not erase context.
        times = old.setdefault("metric_observed_at", {})
        for field, value in patch.items():
            if field == "observed_at":
                continue
            if at >= times.get(field, 0):
                old[field], times[field] = value, at
        old["observed_at"] = max(at, old.get("observed_at", 0))
        self.db.execute("UPDATE sessions SET telemetry=? WHERE key=?", (json.dumps(old), key))
        if "limits" in patch:
            self.observe(key, "limits", at, patch["limits"])

    def observe(self, key, kind, at, value):
        self.db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?)",
                        (key, kind, at, json.dumps(value)))

    def event(self, key, event):
        previous = self.db.execute("SELECT tokens,at FROM usage WHERE session=? AND event_id=?",
                                   (key, event["id"])).fetchone()
        tok = event["tokens"]
        at = event["at"]
        if previous:
            # Streaming chunks update one request, rather than charging it again.
            old = json.loads(previous[0])
            tok = {k: max(tok.get(k, 0), old.get(k, 0)) for k in TOKENS}
            at = min(at, previous[1])
        self.db.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?,?,?,?)",
                        (key, event["id"], at, event.get("model"), json.dumps(tok), json.dumps(event.get("details", {}))))

    def report(self, now, zone, panes, max_status_gap=120, pricing=None):
        from decimal import Decimal
        from .pricing import estimate
        starts = windows(now, zone)
        active = {p.get("session_key") for p in panes}
        sessions = []
        for row in self.db.execute("SELECT * FROM sessions ORDER BY key"):
            key = row["key"]
            telemetry = json.loads(row["telemetry"])
            periods = {}
            events = list(self.db.execute("SELECT * FROM usage WHERE session=? AND at>=? AND at<=?",
                                         (key, starts["last_31_days"], now)))
            observations = list(self.db.execute(
                "SELECT * FROM observations WHERE session=? AND at>=? AND at<=? ORDER BY at",
                (key, starts["last_31_days"] - max_status_gap, now)))
            spending = list(self.db.execute("SELECT * FROM spend WHERE session=? AND at>=? AND at<=?",
                                           (key, starts["last_31_days"], now)))
            for name, start in starts.items():
                chosen = [x for x in events if x["at"] >= start]
                tok = {k: 0 for k in TOKENS}
                for event in chosen:
                    for k, v in json.loads(event["tokens"]).items():
                        tok[k] += v
                # Reasoning is already included in output.
                tok["total"] = sum(tok[k] for k in ("input", "cache_read", "cache_write", "output"))
                samples = [{"at": x["at"], "windows": json.loads(x["value"])}
                           for x in observations if x["kind"] == "limits" and x["at"] >= start]
                states = [x for x in observations if x["kind"] == "status"]
                durations = {}
                for i, sample in enumerate(states):
                    end = min(now, sample["at"] + max_status_gap,
                              states[i + 1]["at"] if i + 1 < len(states) else now)
                    seconds = max(0, end - max(start, sample["at"]))
                    status = json.loads(sample["value"])
                    if seconds:
                        durations[status] = durations.get(status, 0) + seconds
                amounts = {}
                for item in spending:
                    if item["at"] >= start:
                        currency = item["currency"]
                        amounts[currency] = amounts.get(currency, Decimal(0)) + Decimal(item["amount"])
                periods[name] = {
                    "start": iso(start), "end": iso(now), "tokens": tok,
                    "usage_records_observed": len(chosen),
                    "estimated_spend": estimate(chosen, row["agent"], pricing or {}),
                    "billed_spend": [{"currency": k, "amount": str(v)} for k, v in amounts.items()] or None,
                    "spend_coverage": "reported_records_only" if amounts else "unavailable",
                    "status_seconds_observed": durations, "quota_samples": samples,
                }
            sessions.append({"key": key, "agent": row["agent"], "session_id": row["session_id"],
                             "active": key in active, "first_seen": iso(row["first_seen"]),
                             "last_seen": iso(row["last_seen"]), "current": telemetry, "periods": periods})
        return {"schema_version": 1, "generated_at": iso(now), "timezone": zone,
                "status_max_gap_seconds": max_status_gap, "panes": panes, "sessions": sessions,
                "account_billing": {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT * FROM billing")}}

    def prune(self, now):
        # A 35-day retention margin preserves the sample before any 31-day boundary.
        before = now - 35 * 86400
        for table in ("usage", "observations", "spend"):
            self.db.execute(f"DELETE FROM {table} WHERE at<?", (before,))

    def publish(self, report):
        atomic_json(self.directory / "snapshot.json", report)
