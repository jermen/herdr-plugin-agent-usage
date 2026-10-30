#!/usr/bin/env python3
"""Configure a reversible Claude metrics wrapper and the report key, then start the collector.

Without --no-link it also links this checkout as a local plugin (development)."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from agent_usage.store import atomic_json

ROOT = Path(__file__).resolve().parent
KEY_BLOCK = ("# >>> agent-usage keybinding block", "# <<< agent-usage keybinding block")


def configure_claude(settings_path, config_dir, state_dir, config_file):
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    original = settings.get("statusLine")
    wrapper = config_dir / "claude-statusline.json"
    command = shlex.join([sys.executable, str(ROOT / "claude_statusline.py"), str(wrapper)])
    if isinstance(original, dict) and "claude_statusline.py" in original.get("command", ""):
        if original.get("command") == command and wrapper.exists():
            return "already configured"
        if wrapper.exists() and shlex.split(original["command"])[-1:] == [str(wrapper)]:
            # Same wrapper state from another checkout, e.g. after switching between
            # a linked development checkout and `herdr plugin install`.
            settings["statusLine"] = {**original, "command": command}
            atomic_json(settings_path, settings)
            return "moved to this checkout"
        raise ValueError("a different usage wrapper is already configured")
    if original is not None and (not isinstance(original, dict) or original.get("type") != "command"):
        raise ValueError("unsupported existing statusLine")
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if wrapper.exists():
        raise ValueError("existing wrapper state requires review")
    backup = settings_path.with_name(settings_path.name + ".agent-usage-" + str(time.time_ns()) + ".bak")
    settings_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if settings_path.exists():
        with backup.open("x") as stream:
            stream.write(settings_path.read_text())
        os.chmod(backup, 0o600)
    atomic_json(wrapper, {"original": original, "state_dir": str(state_dir), "config_file": str(config_file)})
    settings["statusLine"] = {**(original or {}), "type": "command", "command": command}
    atomic_json(settings_path, settings)
    return "configured; original settings backed up alongside settings.json"


def herdr_config_path():
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "config.toml"


def remove_block(text):
    start, end = KEY_BLOCK
    kept, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == start:
            inside = True
        elif inside and line.strip() == end:
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise ValueError("unterminated agent-usage block in the Herdr config; fix it by hand")
    return "".join(kept)


def bound_keys(parsed):
    keys = parsed.get("keys", {})
    used = {v for k, v in keys.items() if k != "command" and isinstance(v, str)}
    return used | {c.get("key") for c in keys.get("command", []) if isinstance(c, dict)}


def configure_key(path, key):
    """A managed [[keys.command]] block that opens the report pane; replaced on rerun."""
    before = path.read_text() if path.exists() else ""
    rest = remove_block(before)
    if key in bound_keys(tomllib.loads(rest)):
        raise ValueError(f"{key} is already bound in {path}; pass --key or --no-key")
    start, end = KEY_BLOCK
    block = (f"{start}\n[[keys.command]]\nkey = {json.dumps(key)}\ntype = \"plugin_action\"\n"
             f'command = "jermen.agent-usage.open"\ndescription = "Agent usage report"\n{end}\n')
    after = rest.rstrip("\n") + ("\n\n" if rest.strip() else "") + block
    tomllib.loads(after)
    if after == before:
        return "report key unchanged"
    target = path.resolve()  # A symlinked config (dotfiles) keeps its link.
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".agent-usage-" + str(time.time_ns()) + ".bak")
        backup.write_text(before)
    temporary = target.with_name("." + target.name + ".agent-usage.tmp")
    temporary.write_text(after)
    os.replace(temporary, target)
    return f"{key} opens the usage report; previous config backed up alongside"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-statusline", action=argparse.BooleanOptionalAction, default=True,
                        help="configure the Claude metrics wrapper (default: enabled); "
                             "--no-claude-statusline leaves Claude settings unchanged")
    parser.add_argument("--timezone", default="Europe/Prague")
    parser.add_argument("--key", default="prefix+u", help="Herdr key that opens the report (default: prefix+u)")
    parser.add_argument("--no-key", action="store_true", help="leave the Herdr keybindings unchanged")
    parser.add_argument("--no-link", action="store_true",
                        help="do not link this checkout; for plugins installed with `herdr plugin install`")
    args = parser.parse_args()
    from zoneinfo import ZoneInfo
    ZoneInfo(args.timezone)
    config_dir = Path.home() / ".config/herdr/plugins/config/jermen.agent-usage"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_file = config_dir / "config.json"
    state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "herdr-agent-usage"
    # Explicit state root keeps the plugin and statusLine at the same location.
    if not config_file.exists():
        atomic_json(config_file, {"timezone": args.timezone, "poll_seconds": 30, "state_dir": str(state_dir)})
    else:
        configured = json.loads(config_file.read_text())
        state_dir = Path(configured.get("state_dir", state_dir)).expanduser()
    if args.claude_statusline:
        settings_path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
        print(configure_claude(settings_path, config_dir, state_dir, config_file))
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    if not args.no_key:
        print(configure_key(herdr_config_path(), args.key))
        subprocess.run([herdr, "server", "reload-config"], check=False)
    if not args.no_link:
        subprocess.run([herdr, "plugin", "link", str(ROOT), "--enabled"], check=True)
    subprocess.run([herdr, "plugin", "action", "invoke", "start", "--plugin", "jermen.agent-usage"], check=True)


if __name__ == "__main__":
    main()
