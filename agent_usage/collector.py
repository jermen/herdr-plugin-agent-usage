"""Read Herdr bindings, then only the transcripts of those exact sessions."""
import json
import os
import re
import socket
import stat
from pathlib import Path

from .adapters import parse
from .quota import refresh_kimi

AGENTS = {"claude": "claude", "claude-code": "claude", "codex": "codex", "kimi": "kimi"}
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
MAX_LINE = 16 * 1024 * 1024


class Herdr:
    def __init__(self, path):
        self.path = path

    def snapshot(self):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(self.path)
            sock.sendall(b'{"id":"agent-usage","method":"session.snapshot","params":{}}\n')
            with sock.makefile("rb") as stream:
                line = stream.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE:
            raise ValueError("Herdr snapshot too large")
        reply = json.loads(line)
        snap = reply.get("result", {}).get("snapshot")
        if not isinstance(snap, dict) or not isinstance(snap.get("panes"), list):
            raise ValueError("Herdr snapshot unavailable")
        return snap


class IdentityError(ValueError):
    """A transcript header contradicts its requested session."""


def claude_transcript_path(sid, value):
    """Accept an exact native session path, never a relative path or another ID."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.name != f"{sid}.jsonl":
        return None
    return path


def paths(agent, sid, config, reported_path=None):
    roots = config.get("roots", {})
    home = Path.home()
    if agent == "codex":
        root = Path(roots.get(agent, os.environ.get("CODEX_HOME", home / ".codex"))).expanduser()
        return sorted(root.glob(f"sessions/*/*/*/*-{sid}.jsonl")) + sorted(root.glob(f"archived_sessions/*-{sid}.jsonl"))
    if agent == "claude":
        exact = claude_transcript_path(sid, reported_path)
        if exact is not None:
            return [exact]
        root = Path(roots.get(agent, os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))).expanduser()
        return sorted(root.glob(f"projects/*/{sid}.jsonl"))
    root = Path(roots.get(agent, os.environ.get("KIMI_SHARE_DIR", home / ".kimi"))).expanduser()
    return sorted(root.glob(f"sessions/*/{sid}/wire.jsonl"))


def read_log(store, key, agent, sid, path, now):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("transcript is not a regular file")
    inode = f"{info.st_dev}:{info.st_ino}"
    saved = store.db.execute("SELECT * FROM cursors WHERE path=?", (str(path),)).fetchone()
    state, offset = {}, 0
    if saved and saved["inode"] == inode and saved["offset"] <= info.st_size and json.loads(saved["state"]).get("parser_version") == 2:
        state, offset = json.loads(saved["state"]), saved["offset"]
    state["parser_version"] = 2
    initial_offset = offset
    malformed = 0
    with path.open("rb") as stream:
        stream.seek(offset)
        # Bound each pass; a large new transcript catches up over several polls.
        ceiling = offset + 64 * 1024 * 1024
        while stream.tell() < ceiling:
            line = stream.readline(MAX_LINE + 1)
            if not line or not line.endswith(b"\n"):
                # Leave a partially written record for the next pass.
                if len(line) > MAX_LINE:
                    raise ValueError("oversized transcript record")
                break
            offset = stream.tell()
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("record is not an object")
                if agent == "codex" and row.get("type") == "session_meta":
                    if (row.get("payload") or {}).get("id") != sid:
                        raise IdentityError("session identity mismatch")
                if agent == "claude" and row.get("sessionId") not in (None, sid):
                    continue
                event, patch = parse(agent, row, state)
                if event and now - 35 * 86400 <= event["at"] <= now:
                    store.event(key, event)
                if patch and patch["observed_at"] <= now:
                    store.telemetry(key, patch)
            except IdentityError:
                raise
            except (TypeError, ValueError, AttributeError):
                malformed += 1
    store.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?,?)",
                     (str(path), inode, offset, json.dumps(state)))
    return {"bytes_read": offset - initial_offset,
            "malformed_records": malformed, "caught_up": offset >= info.st_size}


def collect(store, snapshot, config, now):
    panes, groups = [], {}
    for pane in snapshot["panes"]:
        agent = AGENTS.get(pane.get("agent"))
        if not agent:
            continue
        binding = pane.get("agent_session") or {}
        sid = binding.get("value") if binding.get("kind") == "id" else None
        if binding.get("agent") and AGENTS.get(binding["agent"]) != agent:
            sid = None
        view = {"pane_id": pane["pane_id"], "agent": agent,
                "status": pane.get("agent_status", "unknown"), "session_key": None}
        panes.append(view)
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            view["collection_status"] = "session_unreported"
            continue
        key = store.ensure_session(agent, sid, now)
        view["session_key"] = key
        groups.setdefault(key, []).append(view)
    store.db.commit()
    for key, group in groups.items():
        agent, sid = key.split(":", 1)
        found = paths(agent, sid, config, store.transcript_path(key))
        status = "transcript_missing"
        if len(found) > 1:
            # A duplicated transcript is ambiguous; importing both doubles requests.
            status = "transcript_ambiguous"
        elif found:
            try:
                with store.db:
                    result = read_log(store, key, agent, sid, found[0], now)
                status = "ok" if result["caught_up"] else "catching_up"
                if result["malformed_records"]:
                    status = "partial"
            except FileNotFoundError:
                status = "transcript_missing"
            except (OSError, ValueError):
                status = "transcript_error"
        for view in group:
            view["collection_status"] = status
        priority = {"working": 5, "blocked": 4, "done": 3, "idle": 2, "unknown": 1}
        observed = max((p["status"] for p in group), key=lambda s: priority.get(s, 0))
        store.observe(key, "status", now, observed)
    refresh_kimi(store, config, now)
    store.prune(now)
    store.db.commit()
    report = store.report(now, config.get("timezone", "UTC"), panes,
                          max_status_gap=config.get("poll_seconds", 30) * 2, pricing=config.get("pricing"))
    store.publish(report)
    return report
