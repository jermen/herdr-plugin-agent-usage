#!/usr/bin/env python3
"""CLI entry point; stdout is reserved for the JSON consumer interface."""
import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_usage.adapters import claude_statusline
from agent_usage.billing import import_spend, refresh
from agent_usage.collector import AGENTS, SESSION_ID, Herdr, claude_transcript_path, collect
from agent_usage.store import Store


def settings(args):
    config_dir = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR",
                                   Path.home() / ".config/herdr/plugins/config/jermen.agent-usage"))
    path = Path(args.config) if args.config else config_dir / "config.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    config.setdefault("poll_seconds", 30)
    if not isinstance(config["poll_seconds"], (int, float)) or not 5 <= config["poll_seconds"] <= 3600:
        raise ValueError("poll_seconds must be between 5 and 3600")
    from zoneinfo import ZoneInfo
    ZoneInfo(config.get("timezone", "UTC"))
    socket_path = args.socket or os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        raise ValueError("HERDR_SOCKET_PATH or --socket is required")
    state_base = Path(args.state_dir or config.get("state_dir") or os.environ.get("HERDR_PLUGIN_STATE_DIR",
                     Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "herdr-agent-usage"))
    # Independent Herdr endpoints cannot overwrite one another's pane bindings.
    scope = hashlib.sha256(os.path.abspath(socket_path).encode()).hexdigest()[:16]
    return config, socket_path, state_base / scope


def run_once(store, client, config):
    with (store.directory / "collect.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        snapshot = client.snapshot()  # Fail before changing state on an unavailable server.
        refresh(store, config, now)
        return collect(store, snapshot, config, now)


def ingest_claude(store, client, data, now):
    sid = data.get("session_id")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise ValueError("invalid Claude session ID")
    key = "claude:" + sid
    if not store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
        # The first statusLine update can beat the watcher's first collection.
        # Require an exact live Herdr binding before accepting that early update.
        for pane in client.snapshot()["panes"]:
            binding = pane.get("agent_session") or {}
            if (AGENTS.get(pane.get("agent")) == "claude" and binding.get("kind") == "id"
                    and binding.get("value") == sid
                    and (not binding.get("agent") or AGENTS.get(binding["agent"]) == "claude")):
                store.ensure_session("claude", sid, now)
                break
        else:
            raise ValueError("session has not been observed in Herdr")
    store.telemetry(key, claude_statusline(data, now))
    path = claude_transcript_path(sid, data.get("transcript_path"))
    if path is not None:
        store.bind_transcript(key, path)


def watch(directory, socket_path, config):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        alive = True

        def stop(_sig, _frame):
            nonlocal alive
            alive = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        initial = os.stat(socket_path)
        identity = (initial.st_dev, initial.st_ino)
        store, client = Store(directory), Herdr(socket_path)
        try:
            while alive:
                try:
                    current = os.stat(socket_path)
                    if identity != (current.st_dev, current.st_ino):
                        break  # A new Herdr instance has its own startup hook.
                    run_once(store, client, config)
                except FileNotFoundError:
                    break
                except (OSError, ValueError, TypeError):
                    # Exception messages can contain provider responses; log only a code.
                    print("agent-usage: collection_failed", file=sys.stderr, flush=True)
                end = time.monotonic() + config["poll_seconds"]
                while alive and time.monotonic() < end:
                    time.sleep(max(0, min(1, end - time.monotonic())))
        finally:
            store.db.close()


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "watch", "collect", "snapshot", "path", "ingest-claude", "import-spend"])
    parser.add_argument("--config")
    parser.add_argument("--socket")
    parser.add_argument("--state-dir", help="State root; an endpoint subdirectory is appended")
    args = parser.parse_args(argv)
    config, socket_path, directory = settings(args)
    if args.command == "path":
        print(directory / "snapshot.json")
        return
    if args.command == "snapshot":
        print((directory / "snapshot.json").read_text(), end="")
        return
    if args.command == "start":
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Avoid spawning on every event when a watcher already holds the lock.
        with (directory / "watch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        command = [sys.executable, str(Path(__file__).resolve()), "watch",
                   "--socket", socket_path]
        for name in ("state_dir", "config"):
            if getattr(args, name):
                command += ["--" + name.replace("_", "-"), str(Path(getattr(args, name)).absolute())]
        with (directory / "watch.log").open("a") as output:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                             start_new_session=True, close_fds=True)
        return
    if args.command == "watch":
        watch(directory, socket_path, config)
        return
    store = Store(directory)
    try:
        if args.command == "collect":
            print(json.dumps(run_once(store, Herdr(socket_path), config)))
        else:
            data = json.loads(sys.stdin.read(1024 * 1024 + 1))
            with (store.directory / "collect.lock").open("a") as lock, store.db:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if args.command == "import-spend":
                    import_spend(store, data, time.time())
                else:
                    ingest_claude(store, Herdr(socket_path), data, time.time())
            # The watcher publishes on its next pass; hook output stays empty.
    finally:
        store.db.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("agent-usage: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
