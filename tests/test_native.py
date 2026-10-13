import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router.cli import table
from router.daemon import Controller


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = Controller(settings(self.root / "repo"), {}, self.root / "state")

    def event(self, kind, agent="child", session="S", at=1000, **values):
        return self.control.handle("observe", {"source": "claude", "session_id": session,
            "agent_id": agent, "kind": kind, "observed_at": at, **values})

    def test_concurrent_lifecycle_is_idempotent_and_host_owned(self):
        event = {"source": "claude", "session_id": "S", "agent_id": "child", "kind": "start", "observed_at": 1000}
        with ThreadPoolExecutor(max_workers=5) as pool:
            copies = list(pool.map(lambda _: self.control.handle("observe", event), range(5)))
        agent_id = copies[0]["task_id"]
        self.assertEqual(len({copy["task_id"] for copy in copies}), 1)
        launched = self.event("launch", at=900, parent_agent_id="main", task="Fix parser", resolved_model="claude-haiku-5-5")
        self.assertEqual(launched["started_at"], 1000)
        self.assertEqual(launched["parent_agent_id"], "main")
        self.assertEqual(launched["task"], "Fix parser")
        stopped = self.event("stop", at=2000)
        self.event("start", at=1000)
        self.assertEqual(self.event("stop", at=2000)["status"], "stopped")
        self.assertEqual(stopped["duration_ms"], 1000)
        # A synchronous Agent result is delivered after SubagentStop.
        self.assertEqual(self.event("launch", at=2100, parent_agent_id="main")["status"], "stopped")
        self.assertEqual(stopped["control_mode"], "observe")
        self.assertIsNone(stopped["cost"])
        other = self.event("start", session="different")
        self.assertNotEqual(other["task_id"], agent_id)
        self.assertEqual(len(self.control.store.all()), 0)
        for operation in ("review", "cancel", "close"):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "host-managed"):
                self.control.handle(operation, {"task_id": agent_id, "request_id": operation})
        with patch("router.native.now_ms", return_value=2100):
            rows = self.control.handle("status", {})
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.control.handle("status", {"filter": "needs_decision"}), [])

    def test_transcript_usage_is_incremental_without_prompts_or_double_counting(self):
        transcript = self.root / "agent.jsonl"
        message = {"type": "assistant", "message": {"id": "message-1", "model": "claude-haiku-5-5", "usage": {
            "input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
            "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 0}},
            "content": [{"type": "text", "text": "PRIVATE RESPONSE"}]}}
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "PRIVATE PROMPT"}}) + "\n" +
                              json.dumps(message) + "\n" + json.dumps(message) + "\n" + '{"type":')
        record = self.event("start", at=1000, transcript=str(transcript))
        with patch("router.native.now_ms", return_value=1100):
            first = self.control.handle("status", {"task_id": record["task_id"]})
            again = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(first["usage"], {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3})
        self.assertEqual(first["usage"], again["usage"])
        self.assertEqual(first["resolved_model"], "claude-haiku-5-5")
        self.assertIsNone(first["cost"])
        self.assertTrue(first["transcript_pending"])
        with transcript.open("a") as stream:
            stream.write('"assistant","message":{"id":"message-2","model":"claude-haiku-5-5","usage":{"input_tokens":1,"output_tokens":4}}}\n')
        self.event("stop", at=1200)
        result = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(result["usage"]["output_tokens"], 6)
        self.assertAlmostEqual(result["cost"], .000004045)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("PRIVATE", self.control.store.path.read_bytes().decode(errors="ignore"))

    def test_missing_lifecycle_evidence_stays_unknown_across_restart(self):
        record = self.event("start", at=1000)
        with patch("router.native.now_ms", return_value=70000):
            stale = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(stale["status"], "unknown")
        self.assertEqual(stale["last_observed_status"], "running")
        self.assertIsNone(stale["duration_ms"])
        restarted = Controller(settings(self.root / "repo"), {}, self.root / "state")
        restarted.native.reconcile_restart()
        result = restarted.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(result["status"], "unknown")
        self.assertIn("restart", result["visibility_reason"])
        self.event("activity", at=71000, activity="Read")
        self.event("session_end", agent=None, at=72000)
        self.assertEqual(self.control.handle("status", {"task_id": record["task_id"]})["status"], "unknown")

    def test_unified_view_labels_ownership_and_unknown_cost(self):
        record = self.event("launch", at=1000, task="Fix parser", parent_agent_id="main", resolved_model="exact/model")
        with patch("router.native.now_ms", return_value=1500):
            rows = self.control.handle("status", {})
        # Five independent renderings over the same frozen observation.
        with ThreadPoolExecutor(max_workers=5) as pool:
            rendered = list(pool.map(table, [rows] * 5))
        self.assertTrue(all(result == rendered[0] for result in rendered))
        self.assertIn("claude/observe", rendered[0])
        self.assertIn("Fix parser", rendered[0])
        self.assertIn("main", rendered[0])
        self.assertIn("unknown", rendered[0])
        self.assertIn(record["task_id"], rendered[0])

    def test_oversized_transcript_advances_and_marks_partial_usage(self):
        transcript = self.root / "large.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": "x" * 1_100_000}) + "\n" +
                              json.dumps({"type": "assistant", "message": {"id": "real", "model": "claude-haiku-5-5",
                                  "usage": {"input_tokens": 1, "output_tokens": 3}}}) + "\n")
        record = self.event("stop", at=1000, transcript=str(transcript))
        first = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertTrue(first["transcript_pending"])
        second = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(second["usage"]["output_tokens"], 3)
        self.assertTrue(second["usage_partial"])
        self.assertFalse(second["transcript_pending"])
        self.assertIsNone(second["cost"])

    def test_synthetic_messages_do_not_replace_real_usage_or_model(self):
        transcript = self.root / "synthetic.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"id": "real", "model": "claude-haiku-5-5",
            "usage": {"input_tokens": 1, "output_tokens": 2}}}) + "\n" +
            json.dumps({"type": "assistant", "message": {"id": "synthetic", "model": "<synthetic>",
                "usage": {"input_tokens": 0, "output_tokens": 0}}}) + "\n")
        record = self.event("stop", transcript=str(transcript))
        result = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(result["resolved_model"], "claude-haiku-5-5")
        self.assertEqual(result["models"], ["claude-haiku-5-5"])
        self.assertAlmostEqual(result["cost"], .0000011)

    def test_native_list_is_paged_but_history_remains_addressable(self):
        identities = []
        for number in range(105):
            identities.append(self.event("stop", agent=str(number), at=number)["task_id"])
        rows = self.control.handle("status", {})
        self.assertEqual(len(rows), 100)
        self.assertNotIn("transcript", rows[0])
        self.assertEqual(len(self.control.handle("status", {"native_offset": 100})), 5)
        self.assertEqual(self.control.handle("status", {"task_id": identities[0]})["status"], "stopped")

    def test_result_metadata_does_not_discard_delayed_stop(self):
        self.event("start", at=1000)
        self.assertEqual(self.event("result", at=2000)["status"], "unknown")
        stopped = self.event("stop", at=1500)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["duration_ms"], 500)
        self.event("start", at=2000)
        self.event("result", at=2100)
        self.event("start", at=3000)
        resumed = self.event("stop", at=4000)
        self.assertTrue(resumed["runtime_partial"])
        self.assertEqual(resumed["duration_ms"], 1500)


if __name__ == "__main__":
    unittest.main()
