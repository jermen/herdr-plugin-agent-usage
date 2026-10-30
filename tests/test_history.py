import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agent_usage.history import backfill, roll
from agent_usage.model import counts, iso, timestamp
from agent_usage.store import Store
from agent_usage.summary import History, missing_prices, report
from agent_usage.tui import View, plain

NOW = timestamp("2026-10-30T12:00:00Z")  # A Friday.
DAY = 86400
PRICES = {"models": {"test-model": {"tiers": {"standard": {
    "input": "1", "output": "10", "cache_read": "0.1", "cache_write": "1.25", "cache_write_1h": "2"}}}}}


def event(eid, at, model="test-model", **tokens):
    return {"id": eid, "at": at, "model": model, "tokens": counts(**tokens), "details": {}}


def claude_row(at, request, sid="s1", output=0, cwd="/work/proj"):
    return {"timestamp": iso(at), "type": "assistant", "sessionId": sid, "requestId": request, "cwd": cwd,
            "message": {"id": "m-" + request, "model": "test-model",
                        "usage": {"input_tokens": 1_000_000, "output_tokens": output}}}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def roll(self, now=NOW, **kwargs):
        with self.store.db:
            roll(self.store, now, 60, PRICES, **kwargs)

    def rows(self):
        return [dict(r) for r in self.store.db.execute("SELECT * FROM rollup_usage ORDER BY hour,model")]

    def test_rollup_outlives_raw_retention(self):
        self.store.event("claude:s1", event("a", NOW - 3600, input=1_000_000))
        self.store.event("claude:s1", event("b", NOW - 3500, output=100_000))
        self.roll()
        later = NOW + 40 * DAY
        self.store.prune(later)
        self.roll(later)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM usage").fetchone()[0], 0)
        [row] = self.rows()
        self.assertEqual(Decimal(row["cost"]), Decimal(2))
        self.assertEqual((row["records"], row["agent"], row["hour"] % 3600), (2, "claude", 0))

    def test_streamed_update_refolds_its_hour(self):
        self.store.event("claude:s1", event("a", NOW - 60, output=10))
        self.roll()
        self.store.event("claude:s1", event("a", NOW - 50, output=50))
        self.roll()
        [row] = self.rows()
        self.assertEqual((json.loads(row["tokens"])["output"], row["records"]), (50, 1))

    def test_unpriced_models_are_kept_separately(self):
        self.store.event("claude:s1", event("a", NOW - 60, input=10))
        self.store.event("claude:s1", event("b", NOW - 60, model="future-model", input=10))
        self.store.event("claude:s1", event("c", NOW - 60, model=None, input=10))
        self.roll()
        rows = {r["model"]: r for r in self.rows()}
        self.assertEqual(sorted(rows), ["", "future-model", "test-model"])
        self.assertIsNone(rows["future-model"]["cost"])
        self.assertEqual(rows["future-model"]["unpriced"], 1)

    def test_status_hours_match_the_snapshot(self):
        key = self.store.ensure_session("claude", "s1", NOW)
        for at in range(int(NOW) - 5400, int(NOW) - 1800, 30):
            self.store.status(key, at, "working", 60)
        for at in range(int(NOW) - 1800, int(NOW) - 600, 30):
            self.store.status(key, at, "blocked", 60)
        self.roll()
        folded = {}
        for status, seconds in self.store.db.execute("SELECT status, seconds FROM rollup_status"):
            folded[status] = folded.get(status, 0) + seconds
        snapshot = self.store.report(NOW, "UTC", [], max_status_gap=60)
        expected = snapshot["sessions"][0]["periods"]["today"]["status_seconds_observed"]
        self.assertEqual(folded.keys(), expected.keys())
        for status in expected:
            self.assertAlmostEqual(folded[status], expected[status])
        hours = {h for (h,) in self.store.db.execute("SELECT DISTINCT hour FROM rollup_status")}
        self.assertEqual(len(hours), 2)

    def test_backfill_imports_unobserved_sessions_idempotently(self):
        project = self.root / ".claude/projects/-work-proj"
        project.mkdir(parents=True)
        old = NOW - 60 * DAY
        rows = [claude_row(old, "r1"), claude_row(old + 10, "r1", output=100_000),
                claude_row(old + 20, "r2"), claude_row(old + 30, "r9", sid="other")]
        (project / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        config = {"roots": {k: str(self.root / ("." + k)) for k in ("claude", "codex", "lumo")}, "pricing": PRICES}
        result = backfill(self.store, config, NOW)
        self.assertEqual(result["files"], 1)
        [row] = self.rows()
        # r1 streamed twice counts once with its final usage; the other session's copy is skipped.
        self.assertEqual((row["records"], Decimal(row["cost"])), (2, Decimal(3)))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM usage").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT project FROM rollup_sessions").fetchone()[0], "/work/proj")
        backfill(self.store, config, NOW)
        self.assertEqual(self.rows(), [row])

    def test_local_days_weeks_months_and_headline(self):
        zone = "Europe/Prague"
        # 23:30 UTC on Sunday Oct 25 is already Monday 00:30 in Prague (CET after the DST change).
        for eid, at in (("mon", "2026-10-25T23:30:00Z"), ("sun", "2026-10-25T09:00:00Z"),
                        ("oct", "2026-10-01T09:00:00Z"), ("sep", "2026-09-30T09:00:00Z"),
                        ("today", "2026-10-30T08:00:00Z")):
            self.store.event("claude:s1", event(eid, timestamp(at), input=1_000_000))
        self.roll()
        history = History(self.store.db, zone, NOW)
        weeks = history.buckets("week")
        self.assertEqual([(str(b["start"]), b["cost"]) for b in weeks],
                         [("2026-10-26", 2), ("2026-10-19", 1), ("2026-09-28", 2)])
        self.assertEqual(weeks[0]["label"], "W44 Oct 26–Nov 01")
        self.assertEqual([(str(b["start"]), b["cost"]) for b in history.buckets("month")],
                         [("2026-10-01", 4), ("2026-09-01", 1)])
        days = history.buckets("day")
        self.assertEqual(str(days[0]["start"]), "2026-10-30")
        headline = history.headline()
        self.assertEqual((headline["day"]["cost"], headline["week"]["cost"], headline["month"]["cost"]), (1, 2, 4))
        self.assertEqual(headline["week"]["previous_total"], 1)
        self.assertEqual(headline["streak_days"], 1)
        self.assertGreater(headline["month"]["projected"], 4)

    def test_missing_prices_ignore_unpriced_agents(self):
        self.store.event("claude:s1", event("a", NOW - 60, input=10))
        self.store.event("claude:s1", event("b", NOW - 60, model="future-model", input=10))
        self.store.event("lumo:l1", event("c", NOW - 60, model="lumo-max", input=10))
        self.roll()
        [today] = History(self.store.db, "UTC", NOW).buckets("day")
        self.assertEqual(missing_prices(today), ["future-model"])

    def test_views_render_at_any_width_and_as_json(self):
        key = self.store.ensure_session("claude", "s1", NOW)
        for at in range(int(NOW) - 7200, int(NOW), 30):
            self.store.status(key, at, "working", 60)
        for i in range(48):
            self.store.event("claude:s1", event(f"e{i}", NOW - i * 3 * 3600, input=100_000, output=1000))
        self.store.event("lumo:l1", event("l", NOW - 60, model="lumo-max", input=10))
        self.roll()
        view = View(lambda: History(self.store.db, "UTC", NOW, {"report": {"subscriptions": {"claude": 100}}}))
        for period in ("day", "week", "month"):
            for heatmap in (False, True):
                for width in (60, 100, 160):
                    view.period, view.heatmap, view.selected = period, heatmap, 3
                    text = plain(view.render(width, 50))
                    self.assertIn("Agent usage", text)
                    self.assertIn("$", text)
        self.assertIn("Claude this month", text)
        data = report(self.store.db, "UTC", NOW, {}, "week")
        self.assertEqual(json.loads(json.dumps(data))["periods"][0]["agents"]["lumo"]["priced"], False)

    def test_heatmap_follows_the_selected_period(self):
        key = self.store.ensure_session("claude", "s1", NOW)
        for at in range(int(NOW) - 3600, int(NOW), 30):
            self.store.status(key, at, "working", 60)
        self.store.event("claude:s1", event("now", NOW - 60, input=1_000_000))
        self.store.event("claude:s1", event("old", timestamp("2026-10-21T09:30:00Z"), input=2_000_000))
        self.roll()
        history = History(self.store.db, "UTC", NOW)
        last_week = history.buckets("week")[1]
        grid = history.heatmap(last_week["begin"], last_week["end"])
        self.assertEqual((grid["days"], grid["cost"][2][9], sum(map(sum, grid["working_seconds"]))), (7, 2.0, 0))
        self.assertEqual(grid["active_days"], {"working_seconds": 0, "cost": 1})
        view = View(lambda: history)
        view.period, view.heatmap, view.selected = "week", True, 1
        text = plain(view.render(100, 50))
        self.assertIn("◀ W43 Oct 19–Oct 25 ▶", text)
        self.assertIn("working time is recorded by the live collector only", text)
        view.period, view.selected = "day", 0
        text = plain(view.render(100, 50))
        self.assertIn("on Fri Oct 30 by local hour", text)
        self.assertIn("1 with spend", text)
        self.assertIn("busiest hour 11:00", text)

    def test_empty_history_explains_itself(self):
        text = plain(View(lambda: History(self.store.db, "UTC", NOW)).render(100, 30))
        self.assertIn("No usage history yet", text)


if __name__ == "__main__":
    unittest.main()
