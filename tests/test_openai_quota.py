import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_usage.adapters import claude_statusline, parse
from agent_usage.collector import collect, read_log
from agent_usage.model import iso
from agent_usage.openai_quota import SOURCE, URL, credentials, parse_openai, refresh_openai
from agent_usage.providers import model_provider
from agent_usage.store import Store

NOW = 1_800_000_000


def response(used=33, account="test-account"):
    return {"account_id": account, "email": "PRIVATE EMAIL", "plan_type": "business",
            "rate_limit": {"primary_window": {
                "used_percent": used, "limit_window_seconds": 604800,
                "reset_at": NOW + 3600}, "secondary_window": None},
            "code_review_rate_limit": {"primary_window": {
                "used_percent": 99, "limit_window_seconds": 300}}}


class ProviderTests(unittest.TestCase):
    def test_model_family_not_harness_or_protocol(self):
        for model in ("gpt-6-astra", "openai/gpt-6.1-sol", "chatgpt-4o-latest", "o3", "o4-mini"):
            self.assertEqual(model_provider(model), "openai")
        self.assertEqual(model_provider("claude-opus-5-5"), "anthropic")
        self.assertEqual(model_provider("kimi-k3.1-pro"), "moonshot")
        for model in (None, "my-gateway-alias", "", 12):
            self.assertIsNone(model_provider(model))

    def test_claude_transcript_records_actual_provider(self):
        for model, provider in (("gpt-6-astra", "openai"), ("claude-opus-5-5", "anthropic"),
                                ("kimi-k3", "moonshot"), ("custom-alias", None)):
            row = {"type": "assistant", "timestamp": iso(NOW), "message": {
                "id": "message", "model": model, "usage": {"input_tokens": 10, "output_tokens": 2}}}
            event, telemetry = parse("claude", row, {})
            self.assertEqual(event["details"]["provider"], provider)
            self.assertEqual(telemetry["provider"], provider)
            self.assertEqual(event["model"], model)

    def test_statusline_missing_foreign_limits_is_not_an_empty_report(self):
        for rate_limits in (None, {}, {"five_hour": None}):
            telemetry = claude_statusline({"model": {"id": "gpt-6-astra"},
                                          "rate_limits": rate_limits, "cost": {"total_cost_usd": 100}}, NOW)
            self.assertEqual(telemetry["provider"], "openai")
            self.assertNotIn("limits", telemetry)
        telemetry = claude_statusline({"model": {"id": "claude-opus-5-5"}}, NOW)
        self.assertEqual(telemetry["limits"], [])

    def test_statusline_real_forwarded_limits_are_kept(self):
        telemetry = claude_statusline({"model": {"id": "gpt-6-astra"}, "rate_limits": {
            "five_hour": {"used_percentage": 25, "resets_at": NOW + 50}}}, NOW)
        self.assertEqual(telemetry["limits"][0]["used_percent"], 25)
        self.assertEqual(telemetry["limits_provider"], "openai")

    def test_weekly_primary_and_missing_secondary(self):
        self.assertEqual(parse_openai(response(), NOW), [{"name": "primary", "used_percent": 33,
            "window_minutes": 10080, "resets_at": NOW + 3600}])

    def test_zero_usage_relative_reset_and_two_windows(self):
        data = response(0)
        data["rate_limit"]["secondary_window"] = {
            "used_percent": 9, "limit_window_seconds": 18000, "reset_after_seconds": 10}
        windows = parse_openai(data, NOW)
        self.assertEqual(windows[0]["used_percent"], 0)
        self.assertEqual(windows[1]["window_minutes"], 300)
        self.assertEqual(windows[1]["resets_at"], NOW + 10)

    def test_malformed_quota_values_are_not_zero(self):
        for used in (None, True, -1, 101, float("nan"), float("inf"), "invalid"):
            self.assertEqual(parse_openai(response(used), NOW), [])
        for raw in (None, [], "invalid"):
            self.assertEqual(parse_openai({"rate_limit": raw}, NOW), [])


class OpenAIQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.path = self.root / "credential.json"
        self.credential()
        self.config = {"openai_quota": {"credentials_file": str(self.path)}}
        self.key = self.session("one")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def credential(self, account="test-account", **extra):
        self.path.write_text(json.dumps({"type": "codex", "access_token": "PRIVATE TOKEN",
            "refresh_token": "PRIVATE REFRESH", "account_id": account, "email": "PRIVATE EMAIL",
            "expired": iso(NOW + 86400), **extra}))

    def session(self, sid, agent="claude", provider="openai", now=NOW):
        key = self.store.ensure_session(agent, sid, now)
        self.store.telemetry(key, {"observed_at": now - 1, "model": "gpt-6-astra",
                                  "provider": provider})
        return key

    def current(self, key=None):
        row = self.store.db.execute("SELECT telemetry FROM sessions WHERE key=?", (key or self.key,)).fetchone()
        return json.loads(row[0])

    def touch(self, now):
        self.store.ensure_session("claude", "one", now)

    def refresh(self, now=NOW, data=None):
        with patch("agent_usage.openai_quota.get_json", return_value=data or response()) as request:
            refresh_openai(self.store, self.config, now)
        return request

    def test_opt_in_and_active_matching_provider_only(self):
        self.session("anthropic", provider="anthropic")
        self.session("unknown", provider="custom")
        self.session("native", agent="codex")
        inactive = self.session("inactive", now=NOW - 100)
        with patch("agent_usage.openai_quota.get_json") as request:
            refresh_openai(self.store, {}, NOW)
            request.assert_not_called()
        self.refresh().assert_called_once()
        for key in ("claude:anthropic", "claude:unknown", "codex:native", inactive):
            self.assertNotIn("limits", self.current(key))

    def test_native_codex_can_be_selected_explicitly(self):
        key = self.session("native", agent="codex")
        self.config["openai_quota"]["agents"] = ["codex"]
        self.refresh().assert_called_once()
        self.assertEqual(self.current(key)["limits"][0]["used_percent"], 33)
        self.assertNotIn("limits", self.current())

    def test_one_request_for_multiple_sessions_and_cached_new_session(self):
        second = self.session("two")
        request = self.refresh()
        request.assert_called_once_with(URL, {"Authorization": "Bearer PRIVATE TOKEN",
                                             "ChatGPT-Account-Id": "test-account", "Accept": "application/json"})
        self.assertEqual(self.current(second)["limits"], self.current()["limits"])
        third = self.session("three", now=NOW + 30)
        self.refresh(NOW + 30).assert_not_called()
        self.assertEqual(self.current(third)["metric_observed_at"]["limits"], NOW)
        self.assertEqual(self.current(third)["limits"][0]["used_percent"], 33)

    def test_cache_survives_collector_restart(self):
        self.refresh()
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.touch(NOW + 30)
        self.refresh(NOW + 30).assert_not_called()
        self.assertEqual(self.current()["metric_observed_at"]["limits"], NOW)

    def test_statusline_does_not_erase_quota_or_advance_its_timestamp(self):
        self.refresh()
        self.store.telemetry(self.key, claude_statusline({"model": {"id": "gpt-6-astra"},
            "context_window": {"used_percentage": 18}, "cost": {"total_cost_usd": 120}}, NOW + 10))
        current = self.current()
        self.assertEqual(current["limits"][0]["used_percent"], 33)
        self.assertEqual(current["metric_observed_at"]["limits"], NOW)
        self.assertEqual(current["context"]["used_percent"], 18)
        self.assertEqual(current["billing_mode"], "subscription")

    def test_failure_preserves_last_good_quota_without_refreshing_it(self):
        self.refresh()
        self.touch(NOW + 300)
        with patch("agent_usage.openai_quota.get_json", side_effect=OSError("PRIVATE RESPONSE")) as request:
            refresh_openai(self.store, self.config, NOW + 300)
            self.touch(NOW + 330)
            refresh_openai(self.store, self.config, NOW + 330)
            request.assert_called_once()
        current = self.current()
        self.assertEqual(current["quota_status"], "fetch_failed")
        self.assertEqual(current["limits"][0]["used_percent"], 33)
        self.assertEqual(current["metric_observed_at"]["limits"], NOW)
        self.assertNotIn("PRIVATE", json.dumps(current))

    def test_unavailable_limits_do_not_imply_zero(self):
        self.refresh(data={"rate_limit": {"primary_window": None}})
        self.assertEqual(self.current()["quota_status"], "fetch_failed")
        self.assertNotIn("limits", self.current())
        self.assertEqual(self.current()["billing_mode"], "subscription")

    def test_no_credentials_or_secrets_persisted(self):
        self.refresh()
        report = json.dumps(self.store.report(NOW, "UTC", []))
        cache = self.store.db.execute("SELECT value FROM quota_cache").fetchone()[0]
        for content in (report, cache):
            for value in ("PRIVATE", "test-account", str(self.path), "access_token", "refresh_token"):
                self.assertNotIn(value, content)

    def test_native_auth_json_and_refuse_api_keys(self):
        self.path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "PRIVATE TOKEN", "account_id": "test-account"}}))
        self.refresh().assert_called_once()
        self.path.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "PRIVATE API KEY"}))
        self.touch(NOW + 10)
        self.refresh(NOW + 10).assert_not_called()
        self.assertEqual(self.current()["quota_status"], "credential_missing")
        self.assertNotIn("limits", self.current())

    def test_expired_or_disabled_credentials_never_sent(self):
        for fields, status in (({"expired": iso(NOW)}, "credential_expired"),
                               ({"disabled": True}, "credential_missing")):
            self.credential(**fields)
            self.refresh().assert_not_called()
            self.assertEqual(self.current()["quota_status"], status)
            self.assertEqual(self.current()["billing_mode"], "subscription")

    def test_missing_file_clears_unverifiable_old_account(self):
        self.refresh()
        self.path.unlink()
        self.touch(NOW + 30)
        self.refresh(NOW + 30).assert_not_called()
        self.assertNotIn("limits", self.current())
        self.assertEqual(self.current()["quota_status"], "credential_missing")

    def test_account_switch_invalidates_cached_and_current_quota(self):
        self.refresh()
        self.credential(account="different-account")
        self.touch(NOW + 30)
        self.refresh(NOW + 30, response(7, account="different-account")).assert_called_once()
        self.assertEqual(self.current()["limits"][0]["used_percent"], 7)
        self.assertEqual(self.current()["metric_observed_at"]["limits"], NOW + 30)

    def test_account_switch_failure_does_not_show_previous_account(self):
        self.refresh()
        self.credential(account="different-account")
        self.touch(NOW + 30)
        self.refresh(NOW + 30, response(account="wrong-account"))
        self.assertNotIn("limits", self.current())
        self.assertEqual(self.current()["quota_status"], "fetch_failed")

    def test_provider_switch_clears_foreign_quota_and_subscription(self):
        self.refresh()
        self.store.telemetry(self.key, claude_statusline({"model": {"id": "claude-opus-5-5"}}, NOW + 30))
        current = self.current()
        self.assertEqual(current["provider"], "anthropic")
        self.assertEqual(current["limits"], [])
        self.assertNotIn("billing_mode", current)
        self.assertNotIn("quota_account", current)
        self.assertNotIn("plan_type", current)
        self.store.telemetry(self.key, {"observed_at": NOW + 40, "provider": "moonshot"})
        self.assertNotIn("limits", self.current())

    def test_older_provider_report_cannot_replace_new_provider_quota(self):
        self.refresh()
        self.store.telemetry(self.key, {"observed_at": NOW + 30, "provider": "anthropic",
            "limits": [{"name": "five_hour", "used_percent": 8}], "limits_provider": "anthropic"})
        self.store.telemetry(self.key, {"observed_at": NOW + 10, "provider": "openai",
            "limits": [{"name": "primary", "used_percent": 99}], "limits_provider": "openai"})
        self.assertEqual(self.current()["provider"], "anthropic")
        self.assertEqual(self.current()["limits"][0]["used_percent"], 8)

    def test_provider_switch_back_can_reuse_original_observed_quota(self):
        self.refresh()
        self.store.telemetry(self.key, {"observed_at": NOW + 10, "provider": "anthropic"})
        self.store.telemetry(self.key, {"observed_at": NOW + 20, "provider": "openai"})
        self.touch(NOW + 30)
        self.refresh(NOW + 30).assert_not_called()
        self.assertEqual(self.current()["limits"][0]["used_percent"], 33)
        self.assertEqual(self.current()["metric_observed_at"]["limits"], NOW)

    def test_collection_with_native_claude_transcript_and_openai_account(self):
        path = self.root / "one.jsonl"
        row = {"type": "assistant", "sessionId": "one", "timestamp": iso(NOW - 1),
               "message": {"id": "req", "model": "gpt-6-astra", "usage": {
                   "input_tokens": 10, "output_tokens": 2}}}
        path.write_text(json.dumps(row) + "\n")
        self.store.bind_transcript(self.key, path)
        snapshot = {"panes": [{"pane_id": "p1", "agent": "claude", "agent_status": "working",
                              "agent_session": {"kind": "id", "value": "one"}}]}
        with patch("agent_usage.openai_quota.get_json", return_value=response()) as request:
            report = collect(self.store, snapshot, self.config, NOW)
        request.assert_called_once()
        self.assertEqual(report["panes"][0]["collection_status"], "ok")
        session = report["sessions"][0]
        self.assertEqual(session["agent"], "claude")
        self.assertEqual(session["current"]["provider"], "openai")
        self.assertEqual(session["current"]["limits"][0]["used_percent"], 33)
        self.assertEqual(session["periods"]["today"]["tokens"]["total"], 12)

    def test_parser_upgrade_corrects_provider_without_double_counting(self):
        path = self.root / "one.jsonl"
        row = {"type": "assistant", "sessionId": "one", "timestamp": iso(NOW - 1),
               "message": {"id": "req", "model": "gpt-6-astra", "usage": {"input_tokens": 10}}}
        path.write_text(json.dumps(row) + "\n")
        read_log(self.store, self.key, "claude", "one", path, NOW)
        self.store.db.execute("UPDATE usage SET details=?", (json.dumps({"provider": "anthropic"}),))
        self.store.db.execute("UPDATE cursors SET state=?", (json.dumps({"parser_version": 2}),))
        read_log(self.store, self.key, "claude", "one", path, NOW)
        rows = self.store.db.execute("SELECT details,tokens FROM usage").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["details"])["provider"], "openai")
        self.assertEqual(json.loads(rows[0]["tokens"])["input"], 10)

    def test_statusline_without_model_cannot_erase_external_quota(self):
        self.refresh()
        self.store.telemetry(self.key, claude_statusline({"rate_limits": {},
            "context_window": {"used_percentage": 18}}, NOW + 10))
        self.assertEqual(self.current()["limits"][0]["used_percent"], 33)
        self.assertEqual(self.current()["provider"], "openai")

    def test_malformed_credentials_are_refused_without_token_output(self):
        for data in ([], {"type": "codex", "access_token": "bad\nheader", "account_id": "account"}):
            self.path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                credentials(self.path)
