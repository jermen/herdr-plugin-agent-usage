#!/usr/bin/env python3
"""Capture metrics and pass stdin/stdout through the user's existing statusLine."""
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    payload = sys.stdin.buffer.read(1024 * 1024)
    wrapper = Path(sys.argv[1])
    config = json.loads(wrapper.read_text())
    command = [sys.executable, str(Path(__file__).with_name("usage.py")), "ingest-claude",
               "--state-dir", config["state_dir"], "--config", config["config_file"]]
    # Herdr's pane environment identifies the endpoint; never attach by cwd.
    if os.environ.get("HERDR_SOCKET_PATH"):
        try:
            subprocess.run(command, input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
    original = config.get("original") or {}
    if original.get("command"):
        return subprocess.run(original["command"], shell=True, input=payload).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
