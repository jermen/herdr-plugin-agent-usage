"""Hourly rollups that outlive the raw records, for daily, weekly and monthly reports.

Raw usage, status and quota records are pruned after 35 days. Each collection folds
the hours they touched into small per-hour tables that are kept, so long-range
reports need neither the raw records nor the transcripts. Buckets are UTC hours;
reports group them into local days, so a zone with a half-hour offset shifts by 30
minutes at day boundaries."""
import json
import os
import re
from pathlib import Path

from .adapters import parse
from .model import TOKENS
from .pricing import estimate

HOUR = 3600
RETENTION = 35 * 86400
MAX_LINE = 16 * 1024 * 1024
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

SCHEMA = '''
    CREATE TABLE IF NOT EXISTS rollup_usage (
        hour INTEGER, session TEXT, agent TEXT, model TEXT, tokens TEXT, records INTEGER,
        cost TEXT, unpriced INTEGER, PRIMARY KEY(hour,session,model));
    CREATE TABLE IF NOT EXISTS rollup_status (
        hour INTEGER, session TEXT, agent TEXT, status TEXT, seconds REAL,
        PRIMARY KEY(hour,session,status));
    CREATE TABLE IF NOT EXISTS rollup_quota (
        hour INTEGER, agent TEXT, name TEXT, used_percent REAL, PRIMARY KEY(hour,agent,name));
    CREATE TABLE IF NOT EXISTS rollup_sessions (
        session TEXT PRIMARY KEY, agent TEXT, project TEXT);
    CREATE TABLE IF NOT EXISTS rollup_dirty (
        session TEXT, hour INTEGER, PRIMARY KEY(session,hour));
    CREATE TABLE IF NOT EXISTS rollup_state (name TEXT PRIMARY KEY, value REAL);
    -- INSERT OR REPLACE also fires this, so a streamed update refolds its hour.
    CREATE TRIGGER IF NOT EXISTS usage_rollup AFTER INSERT ON usage BEGIN
        INSERT OR IGNORE INTO rollup_dirty VALUES(NEW.session, CAST(NEW.at / 3600 AS INTEGER) * 3600);
    END;
'''


def floor_hour(at):
    return int(at // HOUR) * HOUR


def setup(db):
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM rollup_state WHERE name='seeded'").fetchone():
        # Usage stored before the rollups existed is folded on the next pass.
        db.execute("""INSERT OR IGNORE INTO rollup_dirty
                      SELECT DISTINCT session, CAST(at / 3600 AS INTEGER) * 3600 FROM usage""")
        db.execute("INSERT INTO rollup_state VALUES('seeded', 1)")
        db.commit()


def fold_usage(db, session, hour, pricing):
    agent = session.split(":", 1)[0]
    groups = {}
    for row in db.execute("SELECT * FROM usage WHERE session=? AND at>=? AND at<?",
                          (session, hour, hour + HOUR)):
        groups.setdefault(row["model"] or "", []).append(row)
    db.execute("DELETE FROM rollup_usage WHERE hour=? AND session=?", (hour, session))
    for model, rows in groups.items():
        tokens = dict.fromkeys(TOKENS, 0)
        for row in rows:
            for k, v in json.loads(row["tokens"]).items():
                if k in tokens:
                    tokens[k] += v
        cost = estimate(rows, agent, pricing)
        db.execute("INSERT INTO rollup_usage VALUES(?,?,?,?,?,?,?,?)",
                   (hour, session, agent, model, json.dumps(tokens), len(rows),
                    cost["amount"], cost["unpriced_events"]))


def fold_status(db, start, now, gap):
    """Status seconds per hour, with the same span semantics as the snapshot report."""
    db.execute("DELETE FROM rollup_status WHERE hour>=?", (start,))
    spans = db.execute("SELECT * FROM status_spans WHERE last>=? ORDER BY session,start",
                       (start - gap,)).fetchall()
    totals = {}
    for i, span in enumerate(spans):
        following = spans[i + 1] if i + 1 < len(spans) and spans[i + 1]["session"] == span["session"] else None
        end = min(now, span["last"] + gap, following["start"] if following else now)
        begin = max(start, span["start"])
        status = json.loads(span["value"])
        while begin < end:
            hour = floor_hour(begin)
            piece = min(end, hour + HOUR) - begin
            key = (hour, span["session"], status)
            totals[key] = totals.get(key, 0) + piece
            begin += piece
    db.executemany("INSERT INTO rollup_status VALUES(?,?,?,?,?)",
                   [(h, s, s.split(":", 1)[0], st, sec) for (h, s, st), sec in totals.items()])


def fold_quota(db, start):
    """Peak account quota per hour and window; percentages are never summed."""
    db.execute("DELETE FROM rollup_quota WHERE hour>=?", (start,))
    peaks = {}
    for row in db.execute("SELECT session,at,value FROM observations WHERE kind='limits' AND at>=?", (start,)):
        for window in json.loads(row["value"]):
            key = (floor_hour(row["at"]), row["session"].split(":", 1)[0], window["name"])
            peaks[key] = max(peaks.get(key, 0), window["used_percent"])
    db.executemany("INSERT INTO rollup_quota VALUES(?,?,?,?)",
                   [(*key, value) for key, value in peaks.items()])


def workdir(path):
    """The session's working directory from its transcript header, home shortened to ~."""
    try:
        with Path(path).open("rb") as stream:
            for _ in range(200):
                line = stream.readline(MAX_LINE)
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                cwd = (row.get("cwd") or (row.get("payload") or {}).get("cwd")
                       or (row.get("body") or {}).get("cwd"))
                if isinstance(cwd, str) and cwd:
                    home = str(Path.home())
                    return "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd
    except (OSError, AttributeError):
        pass
    return None


def roll(store, now, gap, pricing=None, locate=None, complete=False):
    """Fold changed hours into the rollups. An hour that starts before the raw retention
    may be partly pruned, so it is only refolded from a complete import."""
    db = store.db
    cutoff = now - RETENTION
    for session, hour in db.execute("SELECT session,hour FROM rollup_dirty").fetchall():
        if complete or hour >= cutoff:
            fold_usage(db, session, hour, pricing or {})
    db.execute("DELETE FROM rollup_dirty")
    if not complete:
        # Spans keep growing, so the last folded hours are refolded with the new samples.
        previous = db.execute("SELECT value FROM rollup_state WHERE name='timeline'").fetchone()
        start = max(floor_hour(cutoff) + HOUR, floor_hour((previous[0] if previous else 0) - HOUR))
        fold_status(db, start, now, gap)
        fold_quota(db, start)
        db.execute("INSERT OR REPLACE INTO rollup_state VALUES('timeline', ?)", (now,))
    if locate:
        for (session,) in db.execute("""SELECT DISTINCT session FROM rollup_usage
                                        WHERE session NOT IN (SELECT session FROM rollup_sessions)""").fetchall():
            path = locate(session)
            if path is not None:
                db.execute("INSERT OR REPLACE INTO rollup_sessions VALUES(?,?,?)",
                           (session, session.split(":", 1)[0], workdir(path)))


def sources(config):
    """Every native session file on this machine, as (agent, session ID, path)."""
    from .collector import SESSION_ID, lumo_session_id
    roots, home = config.get("roots", {}), Path.home()
    claude = Path(roots.get("claude", os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))).expanduser()
    for path in sorted(claude.glob("projects/*/*.jsonl")):
        if SESSION_ID.fullmatch(path.stem):
            yield "claude", path.stem, path
    codex = Path(roots.get("codex", os.environ.get("CODEX_HOME", home / ".codex"))).expanduser()
    for path in sorted(codex.glob("sessions/*/*/*/*.jsonl")) + sorted(codex.glob("archived_sessions/*.jsonl")):
        sid = path.stem[-36:]
        if UUID.fullmatch(sid):
            yield "codex", sid, path
    lumo = Path(roots.get("lumo", os.environ.get("LUMO_HOME", home / ".lumo"))).expanduser()
    for path in sorted(lumo.glob("sessions/*/*.jsonl")):
        sid = lumo_session_id(path)
        if isinstance(sid, str) and SESSION_ID.fullmatch(sid):
            yield "lumo", sid, path


def import_file(store, key, agent, sid, path, now):
    """Usage records of a whole session file, with the collector's identity checks."""
    state, records = {}, 0
    with path.open("rb") as stream:
        for line in stream:
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                if agent == "claude" and row.get("sessionId") not in (None, sid):
                    continue
                if agent == "codex" and row.get("type") == "session_meta" and (row.get("payload") or {}).get("id") != sid:
                    return 0
                event, _ = parse(agent, row, state)
            except (TypeError, ValueError, AttributeError):
                continue
            if event and event["at"] <= now:
                store.event(key, event)
                records += 1
    return records


def backfill(store, config, now, since=None, progress=None):
    """Fold existing session files into the rollups, including sessions Herdr never
    observed. Record IDs match the collector's, so a repeat or overlap is idempotent.
    Sessions are not added to the snapshot; their raw records age out with the rest."""
    files = records = 0
    for agent, sid, path in sources(config):
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
            key = f"{agent}:{sid}"
            with store.db:
                records += import_file(store, key, agent, sid, path, now)
                store.db.execute("INSERT OR REPLACE INTO rollup_sessions VALUES(?,?,?)",
                                 (key, agent, workdir(path)))
                roll(store, now, 0, config.get("pricing"), complete=True)
        except OSError:
            continue
        files += 1
        if progress:
            progress(files, path)
    with store.db:
        store.prune(now)
    return {"files": files, "records": records}
