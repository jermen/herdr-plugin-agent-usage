import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_usage.adapters import parse, claude_statusline
from agent_usage.billing import fetch_costs, import_spend, refresh
from agent_usage.collector import collect, read_log
from agent_usage.model import counts, iso, timestamp, windows
from agent_usage.store import Store
from usage import ingest_claude

NOW = timestamp("2026-09-21T12:00:00Z")


def codex(at, input_tokens=100, output_tokens=20, cached=30):
    u = {"input_tokens": input_tokens, "output_tokens": output_tokens,
         "cached_input_tokens": cached, "total_tokens": input_tokens + output_tokens,
         "reasoning_output_tokens": 5}
    return {"timestamp": iso(at), "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": u, "last_token_usage": u,
                                        "model_context_window": 1000},
        "rate_limits": {"primary": {"used_percent": 20, "window_minutes": 300}}}}


def claude(at, output=20):
    return {"timestamp": iso(at), "type": "assistant", "sessionId": "s1", "requestId": "req1",
            "message": {"id": "msg1", "model": "claude-test", "usage": {
                "input_tokens": 10, "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 5, "output_tokens": output},
                "content": [{"text": "PRIVATE PROMPT MUST NOT BE SAVED"}]}}


class AdapterTests(unittest.TestCase):
    def test_codex_delta_and_repeated_notification(self):
        state = {}
        a, p = parse("codex", codex(NOW), state)
        b, _ = parse("codex", codex(NOW + 1), state)
        c, _ = parse("codex", codex(NOW + 2, 180, 35, 50), state)
        self.assertEqual(a["tokens"]["input"], 70)
        self.assertIsNone(b)
        self.assertEqual(c["tokens"]["input"], 60)
        self.assertEqual(c["tokens"]["output"], 15)
        self.assertEqual(p["context"]["used_percent"], 12)
        self.assertEqual(p["limits"][0]["window_minutes"], 300)

    def test_codex_reset_and_null_info(self):
        state = {}
        parse("codex", codex(NOW, 1000), state)
        event, _ = parse("codex", codex(NOW + 1), state)
        self.assertEqual(event["tokens"]["input"], 70)
        self.assertIsNone(parse("codex", {"timestamp": iso(NOW), "type": "event_msg", "payload": {
            "type": "token_count", "info": None}}, state)[0])

    def test_kimi_optional_fields_do_not_clear_context(self):
        state = {}
        row = {"timestamp": NOW, "message": {"type": "StatusUpdate", "payload": {
            "context_usage": .5, "context_tokens": 100, "max_context_tokens": 200,
            "message_id": "m1", "token_usage": {"input_other": 10, "input_cache_read": 30,
                                                    "input_cache_creation": 5, "output": 2}}}}
        event, p = parse("kimi", row, state)
        self.assertEqual(event["tokens"], counts(input=10, cache_read=30, cache_write=5, output=2))
        self.assertEqual(p["context"]["used_percent"], 50)
        row["message"]["payload"] = {"context_tokens": 120, "context_usage": .6}
        _, p = parse("kimi", row, state)
        self.assertEqual(p["context"]["capacity_tokens"], 200)
        self.assertEqual(p["context"]["used_percent"], 60)

    def test_claude_statusline_cost_is_estimated(self):
        p = claude_statusline({"cost": {"total_cost_usd": .125}, "api_key": "secret",
                              "rate_limits": {"five_hour": {"used_percentage": 60}},
                              "context_window": {"context_window_size": 1000, "current_usage": {
                                  "input_tokens": 100, "cache_read_input_tokens": 200}}}, NOW)
        self.assertEqual(p["context"]["used_percent"], 30)
        self.assertEqual(p["session_estimated_cost_usd"], .125)
        self.assertNotIn("secret", json.dumps(p))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.key = self.store.ensure_session("claude", "s1", NOW)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def report(self, now=NOW):
        return self.store.report(now, "Europe/Prague", [])

    def test_streaming_dedup_and_restart(self):
        path = self.root / "s1.jsonl"
        rows = [claude(NOW - 10, 5), claude(NOW - 9, 20), claude(NOW - 8, 0)]
        path.write_text("".join(json.dumps(x) + "\n" for x in rows))
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.root / "state")
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        report = self.report()
        period = report["sessions"][0]["periods"]["today"]
        self.assertEqual(period["tokens"]["total"], 65)
        self.assertEqual(period["usage_records_observed"], 1)
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertIsNone(period["billed_spend"])

    def test_partial_log_then_completed_record(self):
        path = self.root / "s1.jsonl"
        line = json.dumps(claude(NOW - 10))
        path.write_text(line[:20])
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        self.assertEqual(self.store.db.execute("SELECT offset FROM cursors").fetchone()[0], 0)
        with path.open("a") as f:
            f.write(line[20:] + "\n")
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        self.assertEqual(self.report()["sessions"][0]["periods"]["today"]["tokens"]["total"], 65)

    def test_truncated_log_replay_does_not_duplicate(self):
        path = self.root / "s1.jsonl"
        path.write_text(json.dumps(claude(NOW - 10)) + "\n" + "{}\n" * 100)
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        path.write_text(json.dumps(claude(NOW - 10)) + "\n")
        read_log(self.store, self.key, "claude", "s1", path, NOW)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)

    def test_timezone_periods_and_reasoning_not_double_counted(self):
        starts = windows(NOW, "Europe/Prague")
        self.assertEqual(iso(starts["today"]), "2026-09-20T22:00:00+00:00")
        for i, at in enumerate([starts["today"] - 1, starts["today"], NOW - 7 * 86400,
                                NOW - 31 * 86400, NOW - 31 * 86400 - 1, NOW + 1]):
            self.store.event(self.key, {"id": str(i), "at": at,
                                       "tokens": counts(input=10, output=10, reasoning=5)})
        p = self.report()["sessions"][0]["periods"]
        self.assertEqual(p["today"]["tokens"]["total"], 20)
        self.assertEqual(p["last_7_days"]["tokens"]["total"], 60)
        self.assertEqual(p["last_31_days"]["tokens"]["total"], 80)
        # Europe/Prague DST transition: midnight is still resolved in the named zone.
        at = timestamp("2026-10-25T12:00:00Z")
        self.assertEqual(iso(windows(at, "Europe/Prague")["today"]), "2026-10-24T22:00:00+00:00")

    def test_status_does_not_bridge_collector_downtime(self):
        self.store.observe(self.key, "status", NOW - 3600, "working")
        self.store.observe(self.key, "status", NOW - 20, "idle")
        p = self.report()["sessions"][0]["periods"]["today"]
        self.assertEqual(p["status_seconds_observed"], {"working": 120, "idle": 20})

    def test_metric_freshness_independent(self):
        self.store.telemetry(self.key, {"observed_at": NOW, "limits": []})
        self.store.telemetry(self.key, {"observed_at": NOW - 60, "context": {"used_tokens": 4}})
        self.store.telemetry(self.key, {"observed_at": NOW - 120, "context": {"used_tokens": 3}})
        current = self.report()["sessions"][0]["current"]
        self.assertEqual(current["context"]["used_tokens"], 4)
        self.assertEqual(current["metric_observed_at"]["context"], NOW - 60)

    def test_session_scoping_and_missing_binding(self):
        root = self.root / "claude" / "projects" / "project"
        root.mkdir(parents=True)
        (root / "s1.jsonl").write_text(json.dumps(claude(NOW - 1)) + "\n")
        (root / "unrelated.jsonl").write_text(json.dumps(claude(NOW - 1)) + "\n")
        panes = [{"pane_id": "p1", "agent": "claude", "agent_status": "working",
                  "agent_session": {"kind": "id", "value": "s1"}},
                 {"pane_id": "p2", "agent": "claude", "agent_status": "idle"},
                 {"pane_id": "p3", "agent": "claude", "agent_status": "working",
                  "agent_session": {"kind": "id", "value": "s1"}}]
        report = collect(self.store, {"panes": panes}, {"roots": {"claude": str(root.parent.parent)}}, NOW)
        self.assertEqual(len(report["sessions"]), 1)
        self.assertEqual(report["panes"][1]["collection_status"], "session_unreported")
        self.assertEqual(report["sessions"][0]["periods"]["today"]["usage_records_observed"], 1)
        saved = self.root / "state" / "snapshot.json"
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(saved.read_text())["schema_version"], 1)

    def test_api_claude_uses_reported_path_outside_daemon_root(self):
        path = self.root / "api-profile/projects/project/s1.jsonl"
        path.parent.mkdir(parents=True)
        row = claude(NOW - 1)
        row["message"]["model"] = "claude-fable-5-1"
        path.write_text(json.dumps(row) + "\n")
        pane = {"pane_id": "p1", "agent": "claude", "agent_status": "working",
                "agent_session": {"kind": "id", "value": "s1"}}
        config = {"roots": {"claude": str(self.root / "daemon-profile")}}
        before = collect(self.store, {"panes": [pane]}, config, NOW)
        self.assertEqual(before["panes"][0]["collection_status"], "transcript_missing")
        ingest_claude(self.store, None, {"session_id": "s1", "transcript_path": str(path),
            "context_window": {"context_window_size": 1000, "used_percentage": 14},
            "cost": {"total_cost_usd": .01}, "api_key": "SECRET"}, NOW)
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.root / "state")
        for _ in range(2):
            after = collect(self.store, {"panes": [pane]}, config, NOW)
            self.assertEqual(after["panes"][0]["collection_status"], "ok")
            session = after["sessions"][0]
            self.assertEqual(session["current"]["limits"], [])
            self.assertEqual(session["current"]["context"]["used_percent"], 14)
            for period in session["periods"].values():
                self.assertEqual(period["usage_records_observed"], 1)
                self.assertEqual(period["tokens"]["total"], 65)
                self.assertGreater(float(period["estimated_spend"]["amount"]), 0)
            for private in (str(path), "SECRET", "PRIVATE PROMPT"):
                self.assertNotIn(private, json.dumps(after))
        path.unlink()
        self.assertEqual(collect(self.store, {"panes": [pane]}, config, NOW)["panes"][0][
            "collection_status"], "transcript_missing")

    def test_reported_path_overrides_ambiguous_copies(self):
        root = self.root / "claude/projects"
        for folder in ("old-project", "current-project"):
            path = root / folder / "s1.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(claude(NOW - 1)) + "\n")
        pane = {"pane_id": "p1", "agent": "claude", "agent_session": {"kind": "id", "value": "s1"}}
        config = {"roots": {"claude": str(root.parent)}}
        self.assertEqual(collect(self.store, {"panes": [pane]}, config, NOW)["panes"][0][
            "collection_status"], "transcript_ambiguous")
        ingest_claude(self.store, None, {"session_id": "s1", "transcript_path": str(path)}, NOW)
        report = collect(self.store, {"panes": [pane]}, config, NOW)
        self.assertEqual(report["panes"][0]["collection_status"], "ok")
        self.assertEqual(report["sessions"][0]["periods"]["today"]["usage_records_observed"], 1)

    def test_invalid_transcript_hint_does_not_replace_valid_binding(self):
        valid = str(self.root / "s1.jsonl")
        ingest_claude(self.store, None, {"session_id": "s1", "transcript_path": valid}, NOW)
        for value in (None, {}, "s1.jsonl", "/tmp/other.jsonl", "/tmp/s1.jsonl\x00"):
            with self.subTest(path=value):
                ingest_claude(self.store, None, {"session_id": "s1", "transcript_path": value}, NOW)
                self.assertEqual(self.store.transcript_path(self.key), valid)

    def test_statusline_before_first_poll_requires_live_matching_binding(self):
        from unittest.mock import Mock
        client = Mock()
        payload = {"session_id": "early", "transcript_path": str(self.root / "early.jsonl")}
        for binding in ({"kind": "id", "value": "other"},
                        {"kind": "path", "value": "early"},
                        {"kind": "id", "value": "early", "agent": "codex"}):
            client.snapshot.return_value = {"panes": [{"agent": "claude", "agent_session": binding}]}
            with self.assertRaises(ValueError):
                ingest_claude(self.store, client, payload, NOW)
        self.assertIsNone(self.store.transcript_path("claude:early"))
        client.snapshot.return_value = {"panes": [{"agent": "claude-code", "agent_session": {
            "kind": "id", "value": "early", "agent": "claude"}}]}
        ingest_claude(self.store, client, payload, NOW)
        self.assertEqual(self.store.transcript_path("claude:early"), payload["transcript_path"])

    def test_non_file_transcript_is_rejected_without_blocking(self):
        path = self.root / "s1.jsonl"
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            read_log(self.store, self.key, "claude", "s1", path, NOW)

    def test_actual_spend_import_deduplicates_and_keeps_currency(self):
        record = {"session_key": self.key, "event_id": "bill1", "source": "gateway",
                  "timestamp": iso(NOW - 10), "amount": "0.123456", "currency": "USD"}
        import_spend(self.store, record, NOW)
        import_spend(self.store, record, NOW)
        p = self.report()["sessions"][0]["periods"]["today"]
        self.assertEqual(p["billed_spend"], [{"currency": "USD", "amount": "0.123456"}])
        with self.assertRaises(ValueError):
            import_spend(self.store, {**record, "amount": "NaN"}, NOW)

    def test_billing_missing_credential_and_failed_refresh_are_explicit(self):
        conf = {"billing": {"openai": {"key_env": "TEST_BILLING_KEY"}}}
        with patch.dict(os.environ, {}, clear=True):
            refresh(self.store, conf, NOW)
        v = self.report()["account_billing"]["openai"]
        self.assertEqual(v["status"], "credential_missing")
        with patch.dict(os.environ, {"TEST_BILLING_KEY": "test"}), patch(
                "agent_usage.billing.fetch_costs", side_effect=ValueError("secret")):
            refresh(self.store, conf, NOW + 1000)
        v = self.report()["account_billing"]["openai"]
        self.assertEqual(v["status"], "fetch_failed")
        self.assertNotIn("secret", json.dumps(v))


class BillingTests(unittest.TestCase):
    def test_openai_pagination_and_currency(self):
        calls = []
        def request(url, headers):
            calls.append(url)
            return {"data": [{"start_time": NOW - 100, "end_time": NOW,
                               "results": [{"amount": {"currency": "usd", "value": .125}}]}],
                    "has_more": len(calls) == 1, "next_page": "next"}
        v = fetch_costs("openai", "test", NOW, request)
        self.assertEqual(v["periods"]["today"]["amount"], "0.250")
        self.assertIn("page=next", calls[1])
        self.assertEqual(v["scope"], "organization")

    def test_anthropic_cents_are_converted_to_dollars(self):
        def request(url, headers):
            return {"data": [{"starting_at": iso(NOW - 100), "ending_at": iso(NOW),
                               "results": [{"amount": "123.78912", "currency": "USD"}]}],
                    "has_more": False}
        v = fetch_costs("anthropic", "test", NOW, request)
        self.assertEqual(v["periods"]["today"]["amount"], "1.2378912")

    def test_pagination_loop_fails(self):
        with self.assertRaises(ValueError):
            fetch_costs("openai", "test", NOW, lambda *_: {"data": [], "has_more": True, "next_page": "same"})


if __name__ == "__main__":
    unittest.main()
