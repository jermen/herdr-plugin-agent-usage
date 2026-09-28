import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agent_usage.collector import Herdr, IdentityError, read_log
from agent_usage.model import counts
from agent_usage.pricing import estimate
from agent_usage.quota import parse_kimi, refresh_kimi
from agent_usage.store import Store
from install import configure_claude, main as install_main

ROOT = Path(__file__).resolve().parents[1]


class PricingTests(unittest.TestCase):
    def event(self, model, tokens, **details):
        return {"model": model, "tokens": json.dumps(tokens), "details": json.dumps(details)}

    def test_cache_write_duration_and_reasoning(self):
        e = self.event("claude-fable-5-1", counts(input=1000, cache_read=1000,
                       cache_write=1000, cache_write_1h=600, output=1000, reasoning=500))
        v = estimate([e], "claude", {})
        self.assertEqual(Decimal(v["amount"]), Decimal("0.07725"))
        self.assertEqual(v["coverage"], "complete")

    def test_astra_long_context_and_priority(self):
        e = self.event("gpt-6-astra", counts(input=200000, cache_read=100000, output=1000),
                       request_input=300000, service_tier="priority")
        # Long-context priority: 40 input, 4 cached, 150 output / million.
        self.assertEqual(Decimal(estimate([e], "codex", {})["amount"]), Decimal("8.55"))

    def test_unknown_model_is_not_free(self):
        e = self.event("future-model", counts(input=1000))
        v = estimate([e], "codex", {})
        self.assertIsNone(v["amount"])
        self.assertEqual(v["unpriced_models"], ["future-model"])

    def test_partial_cost_has_coverage(self):
        events = [self.event("gpt-6-astra", counts(input=100)), self.event("unknown", counts(input=999))]
        v = estimate(events, "codex", {})
        self.assertEqual(v["coverage"], "partial")
        self.assertEqual(v["unpriced_events"], 1)

    def test_kimi_model_override_is_labelled(self):
        event = self.event(None, counts(input=1000000, cache_read=1000000, output=1000000))
        v = estimate([event], "kimi", {"default_models": {"kimi": "kimi-k2.6"}})
        self.assertEqual(Decimal(v["amount"]), Decimal("5.11"))
        self.assertIn("configured_model_for_unidentified_events", v["assumptions"])

    def test_custom_rates_and_multiplier(self):
        event = self.event("gateway-model", counts(input=1000000))
        config = {"models": {"gateway-model": {"tiers": {"standard": {"input": "2.5"}}}},
                  "multipliers": {"codex": "1.1"}}
        self.assertEqual(Decimal(estimate([event], "codex", config)["amount"]), Decimal("2.75"))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = self.root / ".claude"
        self.profile.mkdir()
        self.settings = self.profile / "settings.json"
        self.original = {"statusLine": {"type": "command", "command": "cat", "padding": 2},
                         "unrelated": {"keep": True}}
        self.settings.write_text(json.dumps(self.original))
        self.wrapper = self.root / ".config/herdr/plugins/config/jermen.agent-usage/claude-statusline.json"

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, *args, profile=None):
        previous_umask = os.umask(0o077)
        try:
            with patch("install.Path.home", return_value=self.root), patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(self.root / "state")}), patch("sys.argv", ["install.py", *args]), \
                    patch("install.subprocess.run"), patch("sys.stdout", new_callable=io.StringIO):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                if profile is not None:
                    os.environ["CLAUDE_CONFIG_DIR"] = str(profile)
                install_main()
        finally:
            os.umask(previous_umask)

    def test_default_install_wraps_existing_statusline_and_is_repeatable(self):
        self.install()
        settings = json.loads(self.settings.read_text())
        self.assertIn("claude_statusline.py", settings["statusLine"]["command"])
        self.assertEqual(settings["statusLine"]["padding"], 2)
        self.assertEqual(settings["unrelated"], self.original["unrelated"])
        wrapper = self.wrapper.read_text()
        self.assertEqual(json.loads(wrapper)["original"], self.original["statusLine"])
        backups = list(self.profile.glob("settings.json.agent-usage-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.original)
        self.install()
        self.assertEqual(json.loads(self.settings.read_text()), settings)
        self.assertEqual(self.wrapper.read_text(), wrapper)
        self.assertEqual(list(self.profile.glob("settings.json.agent-usage-*.bak")), backups)

    def test_opt_out_leaves_claude_settings_and_existing_wrapper_unchanged(self):
        original = self.settings.read_bytes()
        self.install("--no-claude-statusline")
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(self.wrapper.exists())
        self.assertEqual(list(self.profile.glob("*.bak")), [])
        self.install()
        configured, wrapper = self.settings.read_bytes(), self.wrapper.read_bytes()
        self.install("--no-claude-statusline")
        self.assertEqual(self.settings.read_bytes(), configured)
        self.assertEqual(self.wrapper.read_bytes(), wrapper)

    def test_legacy_flag_configures_selected_claude_profile(self):
        profile = self.root / "custom-profile"
        self.install("--claude-statusline", profile=profile)
        configured = json.loads((profile / "settings.json").read_text())
        self.assertIn("claude_statusline.py", configured["statusLine"]["command"])
        self.assertEqual(json.loads(self.settings.read_text()), self.original)
        self.assertIsNone(json.loads(self.wrapper.read_text())["original"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_herdr_wire_protocol(self):
        path = str(self.root / "herdr.sock")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            listener.listen()
            errors = []
            def serve():
                try:
                    with listener.accept()[0] as conn:
                        data = json.loads(conn.makefile("rb").readline())
                        self.assertEqual(data["method"], "session.snapshot")
                        self.assertIsInstance(data["id"], str)
                        conn.sendall(json.dumps({"id": data["id"], "result": {
                            "type": "session_snapshot", "snapshot": {"panes": []}}}).encode() + b"\n")
                except Exception as e:
                    errors.append(e)
            thread = threading.Thread(target=serve)
            thread.start()
            self.assertEqual(Herdr(path).snapshot(), {"panes": []})
            thread.join(2)
            self.assertFalse(errors)

    def test_identity_mismatch_is_rejected(self):
        store = Store(self.root / "state")
        try:
            key = store.ensure_session("codex", "expected", time.time())
            path = self.root / "wrong.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "different"}}) + "\n")
            with self.assertRaises(IdentityError):
                read_log(store, key, "codex", "expected", path, time.time())
        finally:
            store.db.close()

    def test_claude_wrapper_preserves_settings_and_output(self):
        settings = self.root / "settings.json"
        original = {"statusLine": {"type": "command", "command": "cat", "padding": 2},
                    "unrelated": {"keep": True}}
        settings.write_text(json.dumps(original))
        config_dir = self.root / "config"
        state_dir = self.root / "state"
        configure_claude(settings, config_dir, state_dir, config_dir / "config.json")
        current = json.loads(settings.read_text())
        self.assertEqual(current["unrelated"], original["unrelated"])
        self.assertEqual(current["statusLine"]["padding"], 2)
        self.assertEqual(len(list(self.root.glob("*.bak"))), 1)
        self.assertEqual(configure_claude(settings, config_dir, state_dir, config_dir / "config.json"), "already configured")
        env = {k: v for k, v in os.environ.items() if k != "HERDR_SOCKET_PATH"}
        payload = b'{"session_id":"s1","model":{"id":"test"}}'
        result = subprocess.run([sys.executable, str(ROOT / "claude_statusline.py"),
                                 str(config_dir / "claude-statusline.json")],
                                input=payload, capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, payload)

    def test_claude_wrapper_moves_with_the_checkout(self):
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "cat", "padding": 2}}))
        config_dir = self.root / "config"
        state_dir = self.root / "state"
        with patch("install.ROOT", Path("/old checkout")):
            configure_claude(settings, config_dir, state_dir, config_dir / "config.json")
        wrapper_state = (config_dir / "claude-statusline.json").read_text()
        self.assertEqual(configure_claude(settings, config_dir, state_dir, config_dir / "config.json"), "moved to this checkout")
        status_line = json.loads(settings.read_text())["statusLine"]
        self.assertEqual(shlex.split(status_line["command"])[1], str(ROOT / "claude_statusline.py"))
        self.assertEqual(status_line["padding"], 2)
        self.assertEqual((config_dir / "claude-statusline.json").read_text(), wrapper_state)
        self.assertEqual(len(list(self.root.glob("*.bak"))), 1)
        self.assertEqual(configure_claude(settings, config_dir, state_dir, config_dir / "config.json"), "already configured")
        with patch("install.ROOT", Path("/elsewhere")), patch("install.sys.executable", "/other/python"):
            (config_dir / "claude-statusline.json").rename(config_dir / "moved.json")
            with self.assertRaises(ValueError):
                configure_claude(settings, config_dir, state_dir, config_dir / "config.json")

    def test_no_link_skips_linking_but_starts_the_collector(self):
        with patch("install.Path.home", return_value=self.root), patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "state"), "HERDR_BIN_PATH": "/bin/herdr"}), \
                patch("sys.argv", ["install.py", "--no-link", "--no-claude-statusline"]), \
                patch("install.subprocess.run") as run, patch("sys.stdout", new_callable=io.StringIO):
            install_main()
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["/bin/herdr", "plugin", "action", "invoke", "start", "--plugin", "jermen.agent-usage"]])

    def test_kimi_quota_units_and_credentials_not_persisted(self):
        payload = {"usage": {"limit": "100", "remaining": "25"}, "limits": [
            {"name": "short", "window": {"duration": 5, "timeUnit": "HOUR"},
             "detail": {"limit": 20, "used": 4, "resetAt": "2026-09-21T15:00:00Z"}}],
            "secret": "do not save"}
        rows = parse_kimi(payload)
        self.assertEqual(rows[0]["used_percent"], 75)
        self.assertEqual(rows[1]["window_minutes"], 300)
        store = Store(self.root / "state")
        try:
            now = time.time()
            store.ensure_session("kimi", "s1", now)
            with patch.dict(os.environ, {"TEST_KIMI_KEY": "secret"}), patch(
                    "agent_usage.quota.get_json", return_value=payload):
                refresh_kimi(store, {"kimi_quota": {"key_env": "TEST_KIMI_KEY"}}, now)
            report = store.report(now, "UTC", [])
            self.assertNotIn("secret", json.dumps(report))
            self.assertEqual(report["sessions"][0]["current"]["quota_status"], "ok")
        finally:
            store.db.close()

    def test_watcher_singleton_and_socket_lifecycle(self):
        path = str(self.root / "herdr.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen()
        listener.settimeout(.1)
        stop = threading.Event()
        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.makefile("rb").readline()
                    conn.sendall(b'{"id":"agent-usage","result":{"snapshot":{"panes":[]}}}\n')
        server = threading.Thread(target=serve)
        server.start()
        config = self.root / "config.json"
        config.write_text('{"poll_seconds":5}')
        command = [sys.executable, str(ROOT / "usage.py"), "watch", "--socket", path,
                   "--state-dir", str(self.root / "state"), "--config", str(config)]
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not list((self.root / "state").glob('*/snapshot.json')) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(list((self.root / "state").glob('*/snapshot.json')))
            duplicate = subprocess.run(command, capture_output=True, timeout=3)
            self.assertEqual(duplicate.returncode, 0)
            self.assertIsNone(proc.poll())
            Path(path).unlink()
            self.assertEqual(proc.wait(timeout=7), 0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
            proc.stderr.close()
            stop.set()
            server.join(2)
            listener.close()


if __name__ == "__main__":
    unittest.main()
