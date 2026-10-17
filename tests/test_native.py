import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router.cli import table
from router.daemon import Controller
from router.native import MAX_TRANSCRIPT_MESSAGES


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = Controller(settings(self.root / "repo"), {}, self.root / "state")

    def event(self, kind, agent="child", session="S", at=1000, **values):
        return self.control.handle("observe", {"source": "claude", "session_id": session,
            "agent_id": agent, "kind": kind, "observed_at": at, **values})

    def test_lifecycle_timestamp_is_bounded_to_the_wire_domain(self):
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.event("start", at=2**63)
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.event("start", at=int(time.time() * 1000) + 120_000)

    def test_lifecycle_identities_reject_normalization_collisions(self):
        with self.assertRaisesRegex(ValueError, "invalid agent_id"):
            self.event("start", agent="abc\u202edef")
        with self.assertRaisesRegex(ValueError, "invalid session_id"):
            self.event("start", session="abc\u202edef")

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
        result = self.event("result", at=2100, parent_agent_id="main", task="Fix parser",
                            requested_model="opus", resolved_model="claude-opus", cost=.25)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual((result["task"], result["resolved_model"], result["cost"]),
                         ("Fix parser", "claude-opus", .25))
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
        pending_snapshot = next(item for item in self.control.native.snapshot()
                                if item["task_id"] == record["task_id"])
        self.assertTrue(pending_snapshot["transcript_pending"])
        self.assertIsNone(pending_snapshot["cost"])
        with transcript.open("a") as stream:
            stream.write('"assistant","message":{"id":"message-2","model":"claude-haiku-5-5","usage":{"input_tokens":1,"output_tokens":4}}}\n')
        self.event("stop", at=1200)
        result = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(result["usage"]["output_tokens"], 6)
        self.assertAlmostEqual(result["cost"], .000004045)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("PRIVATE", self.control.store.path.read_bytes().decode(errors="ignore"))

    def test_transcript_display_strings_are_redacted_before_persistence(self):
        transcript = self.root / "redaction.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "message-1",
                "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{
                    "type": "tool_use",
                    "name": "password=swordfish",
                    "input": {"file_path": "/tmp/token=credential-value"},
                }],
            },
        }) + "\n")
        record = self.event("stop", agent="redacted-transcript", transcript=str(transcript))

        visible = self.control.handle("status", {"task_id": record["task_id"]})
        with self.control.store.connect() as db:
            persisted = db.execute(
                "SELECT data FROM observations WHERE task_id=?", (record["task_id"],),
            ).fetchone()[0]

        self.assertNotIn("swordfish", persisted)
        self.assertNotIn("credential-value", persisted)
        self.assertNotIn("swordfish", json.dumps(visible))
        self.assertIn("[REDACTED]", persisted)

    def test_stopped_transcript_eof_detects_late_append_and_replacement(self):
        transcript = self.root / "late.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "first", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }) + "\n")
        record = self.event("stop", agent="late-eof", transcript=str(transcript))
        first = self.control.handle("status", {"task_id": record["task_id"]})
        quiescent = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(first["usage"], quiescent["usage"])

        with transcript.open("a") as stream:
            stream.write(json.dumps({
                "type": "assistant",
                "message": {
                    "id": "late", "model": "claude-haiku-5-5",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            }) + "\n")
        appended = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(appended["usage"]["input_tokens"], 3)
        self.assertEqual(appended["usage"]["output_tokens"], 4)

        replacement = self.root / "replacement.jsonl"
        replacement.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "replacement", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 8, "output_tokens": 13},
            },
        }) + "\n")
        replacement.replace(transcript)
        replaced = self.control.handle("status", {"task_id": record["task_id"]})
        persisted = self.control.native.get(record["task_id"])
        self.assertTrue(replaced["usage_partial"])
        self.assertIsNone(replaced["usage"])
        self.assertIsNone(replaced["cost"])
        self.assertIn("replaced", replaced["transcript_error"].lower())
        self.assertEqual(set(persisted["messages"]), {"replacement"})

    def test_same_inode_transcript_rewrite_cannot_preserve_stale_usage(self):
        transcript = self.root / "copytruncate.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "old", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }) + "\n")
        record = self.event("stop", agent="copytruncate", transcript=str(transcript))
        first = self.control.handle("status", {"task_id": record["task_id"]})
        inode = transcript.stat().st_ino
        self.assertEqual(first["usage"]["output_tokens"], 1)

        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "new-and-longer", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 50, "output_tokens": 80},
                "content": [{"type": "text", "text": "longer replacement record"}],
            },
        }) + "\n")
        self.assertEqual(transcript.stat().st_ino, inode)
        rewritten = self.control.handle("status", {"task_id": record["task_id"]})
        persisted = self.control.native.get(record["task_id"])
        self.assertTrue(rewritten["usage_partial"])
        self.assertIsNone(rewritten["usage"])
        self.assertIsNone(rewritten["cost"])
        self.assertIn("rewritten", rewritten["transcript_error"].lower())
        self.assertEqual(set(persisted["messages"]), {"new-and-longer"})

    def test_transient_transcript_failure_recovers_after_complete_reread(self):
        transcript = self.root / "appears-later.jsonl"
        record = self.event("stop", agent="transient", transcript=str(transcript))
        missing = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertTrue(missing["usage_partial"])
        self.assertIn("unavailable", missing["transcript_error"].lower())

        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "recovered", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        }) + "\n")
        recovered = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertFalse(recovered["usage_partial"])
        self.assertIsNone(recovered.get("transcript_error"))
        self.assertEqual(recovered["usage"]["output_tokens"], 3)
        self.assertIsNotNone(recovered["cost"])

        original_stat = Path.stat
        failed = False

        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path == transcript and not failed:
                failed = True
                raise PermissionError("temporarily unreadable")
            return original_stat(path, *args, **kwargs)

        with patch("router.native.Path.stat", new=fail_once):
            unreadable = self.control.native.refresh(record["task_id"])
            reread = self.control.native.refresh(record["task_id"])
        self.assertTrue(unreadable["usage_partial"])
        self.assertFalse(self.control.native.view(reread)["usage_partial"])
        self.assertIsNone(reread.get("transcript_error"))

    def test_every_non_synthetic_assistant_requires_valid_identity_and_usage(self):
        malformed_messages = (
            ("missing-message", None),
            ("missing-id", {"model": "claude-haiku-5-5", "usage": {"input_tokens": 1}}),
            ("wrong-id", {"id": 7, "model": "claude-haiku-5-5", "usage": {"input_tokens": 1}}),
            ("missing-usage", {"id": "message", "model": "claude-haiku-5-5"}),
            ("wrong-usage", {"id": "message", "model": "claude-haiku-5-5", "usage": []}),
            ("incomplete-usage", {
                "id": "message", "model": "claude-haiku-5-5", "usage": {"input_tokens": 1},
            }),
        )
        valid = {
            "type": "assistant",
            "message": {
                "id": "valid", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        }
        for name, message in malformed_messages:
            with self.subTest(name=name):
                transcript = self.root / f"{name}.jsonl"
                malformed = {"type": "assistant"}
                if message is not None:
                    malformed["message"] = message
                transcript.write_text(json.dumps(malformed) + "\n" + json.dumps(valid) + "\n")
                record = self.event("stop", agent=name, transcript=str(transcript))
                result = self.control.handle("status", {"task_id": record["task_id"]})
                self.assertTrue(result["usage_partial"])
                self.assertIsNone(result["usage"])
                self.assertIsNone(result["cost"])
                self.assertIn("assistant transcript", result["transcript_error"].lower())

    def test_transcript_message_bound_updates_existing_but_rejects_new_identity(self):
        transcript = self.root / "message-bound.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "message-0", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        }) + "\n")
        record = self.event("start", agent="message-bound", transcript=str(transcript))
        stored = self.control.native.get(record["task_id"])
        stored["messages"] = {
            f"message-{number}": {
                "model": "claude-haiku-5-5", "input_tokens": 1, "output_tokens": 1,
            }
            for number in range(MAX_TRANSCRIPT_MESSAGES)
        }
        with self.control.store.transaction() as db:
            self.control.native.put(db, stored)

        at_boundary = self.control.native.refresh(record["task_id"])
        self.assertFalse(at_boundary["usage_partial"])
        self.assertEqual(len(at_boundary["messages"]), MAX_TRANSCRIPT_MESSAGES)
        self.assertEqual(at_boundary["usage"]["input_tokens"], MAX_TRANSCRIPT_MESSAGES + 1)

        with transcript.open("a") as stream:
            stream.write(json.dumps({
                "type": "assistant",
                "message": {
                    "id": "one-too-many", "model": "claude-haiku-5-5",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }) + "\n")
        over_boundary = self.control.native.refresh(record["task_id"])
        self.assertTrue(over_boundary["usage_partial"])
        self.assertIsNone(over_boundary["usage"])
        self.assertIsNone(over_boundary["cost"])
        self.assertEqual(len(over_boundary["messages"]), MAX_TRANSCRIPT_MESSAGES)

    def test_concurrent_transcript_refresh_commits_one_cursor_advance(self):
        transcript = self.root / "concurrent.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "only", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        }) + "\n")
        record = self.event("start", agent="concurrent-refresh", transcript=str(transcript))
        initial_revision = self.control.native.get(record["task_id"])["transcript_revision"]
        rendezvous = threading.Barrier(2)
        original_open = Path.open

        def simultaneous_open(path, *args, **kwargs):
            if path == transcript:
                rendezvous.wait(timeout=5)
            return original_open(path, *args, **kwargs)

        with (patch("router.native.Path.open", new=simultaneous_open),
              ThreadPoolExecutor(max_workers=2) as pool):
            results = list(pool.map(
                lambda _: self.control.native.refresh(record["task_id"]), range(2),
            ))

        persisted = self.control.native.get(record["task_id"])
        self.assertTrue(all(result["usage"]["output_tokens"] == 3 for result in results))
        self.assertEqual(persisted["usage"]["output_tokens"], 3)
        self.assertEqual(persisted["transcript_revision"], initial_revision + 1)
        self.assertEqual(persisted["transcript_offset"], transcript.stat().st_size)

    def test_path_switch_discards_an_in_flight_stale_transcript_read(self):
        old_path = self.root / "old.jsonl"
        new_path = self.root / "new.jsonl"
        old_path.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "old", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 100, "output_tokens": 100},
            },
        }) + "\n")
        new_path.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "id": "new", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        }) + "\n")
        record = self.event("start", agent="stale-path", at=1000, transcript=str(old_path))
        entered = threading.Event()
        release = threading.Event()
        original_open = Path.open

        def delayed_open(path, *args, **kwargs):
            if path == old_path:
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release transcript read")
            return original_open(path, *args, **kwargs)

        with (patch("router.native.Path.open", new=delayed_open),
              ThreadPoolExecutor(max_workers=1) as pool):
            stale_refresh = pool.submit(self.control.native.refresh, record["task_id"])
            self.assertTrue(entered.wait(timeout=5))
            self.event("activity", agent="stale-path", at=1100, transcript=str(new_path))
            release.set()
            stale_result = stale_refresh.result(timeout=5)

        switched = self.control.native.get(record["task_id"])
        self.assertEqual(stale_result["transcript"], str(new_path))
        self.assertEqual(switched["transcript_offset"], 0)
        self.assertEqual(switched["messages"], {})
        refreshed = self.control.native.refresh(record["task_id"])
        self.assertEqual(set(refreshed["messages"]), {"new"})
        self.assertEqual(refreshed["usage"]["output_tokens"], 3)

    def test_missing_lifecycle_evidence_stays_unknown_across_restart(self):
        record = self.event("start", at=1000)
        with patch("router.native.now_ms", return_value=70000):
            stale = self.control.handle("status", {"task_id": record["task_id"]})
            self.assertEqual(self.control.native.active_count(), 0)
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
        # Reopening after result uncertainty requires a strictly newer start.
        self.event("start", at=2001)
        self.event("result", at=2100)
        self.event("start", at=3000)
        resumed = self.event("stop", at=4000)
        self.assertTrue(resumed["runtime_partial"])
        self.assertEqual(resumed["duration_ms"], 1599)

    def test_late_launch_enriches_terminal_record_without_reopening_it(self):
        self.event("start", at=1000)
        stopped = self.event("stop", at=2000)

        launched = self.event(
            "launch",
            at=500,
            parent_agent_id="main",
            task="Fix parser",
            agent_type="worker",
            requested_model="opus",
            resolved_model="claude-opus",
            cost=.25,
        )

        self.assertEqual(launched["status"], "stopped")
        self.assertEqual(launched["finished_at"], stopped["finished_at"])
        self.assertEqual(launched["duration_ms"], stopped["duration_ms"])
        self.assertEqual(launched["last_observed_status"], "stopped")
        self.assertEqual(
            (launched["parent_agent_id"], launched["task"], launched["agent_type"]),
            ("main", "Fix parser", "worker"),
        )
        self.assertEqual(
            (launched["requested_model"], launched["resolved_model"], launched["cost"]),
            ("opus", "claude-opus", .25),
        )

        final = self.event(
            "result", at=2200, task="Final task", resolved_model="claude-final",
            cost=.5, summary="Final summary", agent_type="reviewer", cwd="/final",
            parent_agent_id="new-parent",
        )
        delayed = self.event(
            "start", at=500, task="Stale task", resolved_model="claude-old",
            cost=.01, summary="Stale summary", agent_type="stale", cwd="/stale",
            parent_agent_id="old-parent",
        )
        self.assertEqual(delayed["status"], "stopped")
        self.assertEqual(
            (delayed["task"], delayed["resolved_model"], delayed["cost"], delayed["summary"],
             delayed["agent_type"], delayed["cwd"]),
            (final["task"], final["resolved_model"], final["cost"], final["summary"],
             final["agent_type"], final["cwd"]),
        )
        self.assertEqual(delayed["parent_agent_id"], "new-parent")

    def test_identifiers_ordering_restart_segments_and_legacy_summary_redaction(self):
        exact = "s" * 240
        self.assertEqual(self.event("start", session=exact, agent=exact)["agent_id"], exact)
        for key, value in (("session", "s" * 241), ("agent", "a" * 241),
                           ("session", "token=secret-value"), ("agent", "bad\x00id")):
            values = {key: value}
            with self.subTest(key=key, value=value[:20]), self.assertRaisesRegex(ValueError, "invalid"):
                self.event("start", **values)

        record = self.event("start", agent="ordered", at=1000)
        self.event("result", agent="ordered", at=2000)
        delayed = self.event("activity", agent="ordered", at=1500, activity="stale")
        self.assertEqual(delayed["status"], "unknown")
        stopped = self.event("stop", agent="ordered", at=1500)
        self.assertEqual(stopped["status"], "stopped")
        equal_start = self.event("start", agent="ordered", at=1500)
        self.assertEqual(equal_start["status"], "stopped")

        running = self.event("start", agent="restart", at=1000)
        self.event("activity", agent="restart", at=1500, activity="Read")
        with patch("router.native.now_ms", return_value=2000):
            self.control.native.reconcile_restart()
        restarted = self.control.handle("status", {"task_id": running["task_id"]})
        self.assertEqual(restarted["duration_ms"], 500)
        self.assertTrue(restarted["runtime_partial"])
        self.event("start", agent="restart", at=3000)
        ended = self.event("stop", agent="restart", at=4000)
        self.assertEqual(ended["duration_ms"], 1500)

        legacy = self.control.native.get(record["task_id"])
        legacy["summary"] = "password=swordfish"
        self.assertNotIn("swordfish", self.control.native.view(legacy)["summary"])

    def test_restart_preserves_a_valid_zero_timestamp_without_inflating_duration(self):
        record = self.event("start", agent="epoch", at=0)

        with patch("router.native.now_ms", return_value=1_000_000):
            self.control.native.reconcile_restart()

        restarted = self.control.native.get(record["task_id"])
        self.assertEqual(restarted["status"], "unknown")
        self.assertEqual(restarted["duration_ms"], 0)

    def test_uncertain_boundaries_bank_segments_without_bridging_unobserved_time(self):
        self.event("start", agent="result-gap", at=1000)
        uncertain = self.event("result", agent="result-gap", at=2000)
        self.assertEqual(uncertain["duration_ms"], 1000)
        self.assertTrue(uncertain["runtime_partial"])
        self.event("start", agent="result-gap", at=3000)
        stopped = self.event("stop", agent="result-gap", at=4000)
        self.assertEqual(stopped["duration_ms"], 2000)

        session = self.event("start", agent="session-gap", at=1000)
        self.event("session_end", agent=None, at=2000)
        ended = self.control.native.get(session["task_id"])
        self.assertEqual(ended["duration_ms"], 1000)
        self.assertIsNone(ended["run_started_at"])
        self.assertTrue(ended["runtime_partial"])
        self.event("start", agent="session-gap", at=3000)
        stopped = self.event("stop", agent="session-gap", at=4000)
        self.assertEqual(stopped["duration_ms"], 2000)

    def test_session_end_watermark_survives_restart_and_allows_delayed_stop(self):
        base = int(time.time() * 1000)
        record = self.event("start", agent="ended", at=base)
        self.event("session_end", agent=None, at=base + 1000)

        for kind in ("start", "launch", "activity", "idle", "needs_input"):
            with self.subTest(kind=kind):
                blocked = self.event(kind, agent="ended", at=base + 1000, activity="late")
                self.assertEqual(blocked["status"], "unknown")
                self.assertIn("session ended", blocked["visibility_reason"].lower())

        restarted = Controller(settings(self.root / "repo"), {}, self.root / "state")
        restarted.native.reconcile_restart()
        tied = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "ended",
            "kind": "start", "observed_at": base + 1000,
        })
        self.assertEqual(tied["status"], "unknown")

        stopped = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "ended",
            "kind": "stop", "observed_at": base + 500,
        })
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["duration_ms"], 500)

        reopened = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "ended",
            "kind": "start", "observed_at": base + 1001,
        })
        self.assertEqual(reopened["status"], "running")
        self.assertEqual(reopened["task_id"], record["task_id"])

        legacy_record = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "legacy",
            "kind": "start", "observed_at": base + 2000,
        })
        legacy = restarted.native.get(legacy_record["task_id"])
        for key in (
            "session_ended_at", "session_end_uncertain",
            "session_end_segment_started_at", "session_end_segment_base_duration",
            "session_end_prior_state_at",
        ):
            legacy.pop(key)
        with restarted.store.transaction() as db:
            restarted.native.put(db, legacy)
        observed = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "legacy",
            "kind": "activity", "observed_at": base + 2001,
        })
        self.assertEqual(observed["status"], "running")

    def test_session_end_before_first_agent_blocks_delayed_start_but_allows_newer_segment(self):
        base = int(time.time() * 1000)
        self.event("session_end", agent=None, at=base + 1000)

        delayed = self.event("start", agent="late", at=base)
        self.assertEqual(delayed["status"], "unknown")
        self.assertIn("session ended", delayed["visibility_reason"].lower())

        restarted = Controller(settings(self.root / "repo"), {}, self.root / "state")
        tied = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "new-after-restart",
            "kind": "start", "observed_at": base + 1000,
        })
        self.assertEqual(tied["status"], "unknown")
        newer = restarted.handle("observe", {
            "source": "claude", "session_id": "S", "agent_id": "new-after-restart",
            "kind": "start", "observed_at": base + 1001,
        })
        self.assertEqual(newer["status"], "running")

    def test_native_producer_bounds_model_history_and_host_cost(self):
        transcript = self.root / "many-models.jsonl"
        transcript.write_text("".join(json.dumps({
            "type": "assistant",
            "message": {
                "id": f"message-{number}",
                "model": f"model-{number}",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }) + "\n" for number in range(65)))
        record = self.event(
            "stop", agent="models", at=int(time.time() * 1000), transcript=str(transcript),
        )
        bounded = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(len(bounded["models"]), 64)
        self.assertTrue(bounded["usage_partial"])
        self.assertIsNone(bounded["cost"])
        snapshot = self.control.handle("snapshot", {})
        fleet_record = next(agent for agent in snapshot["agents"] if agent["agent_id"] == "models")
        self.assertLessEqual(len(fleet_record["models"]), 64)

        legacy = self.control.native.get(record["task_id"])
        legacy.update(
            models=[f"legacy-model-{number}" for number in range(65)],
            cost=1e13,
            cost_source="host_reported_estimate",
        )
        with self.control.store.transaction() as db:
            self.control.native.put(db, legacy)
        bounded_legacy = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(len(bounded_legacy["models"]), 64)
        self.assertTrue(bounded_legacy["usage_partial"])
        self.assertIsNone(bounded_legacy["cost"])

        valid = self.event("result", agent="cost", at=1000, cost=1e12)
        self.assertEqual(valid["cost"], 1e12)
        invalid = self.event("result", agent="cost", at=1001, cost=10**10_000)
        self.assertIsNone(invalid["cost"])
        self.assertEqual(invalid["cost_source"], "unavailable")
        self.assertIn("supported bound", invalid["cost_note"])
        json.dumps(self.control.handle("snapshot", {}), allow_nan=False)

    def test_malformed_and_overflowing_transcript_usage_stays_unavailable(self):
        for name, messages in (
            ("oversized", [{"id": "one", "usage": {"input_tokens": 2**63}}]),
            ("aggregate", [
                {"id": "one", "usage": {"input_tokens": 2**62}},
                {"id": "two", "usage": {"output_tokens": 2**62}},
            ]),
        ):
            transcript = self.root / f"{name}.jsonl"
            transcript.write_text("".join(json.dumps({
                "type": "assistant",
                "message": {**message, "model": "claude-haiku-5-5"},
            }) + "\n" for message in messages))
            record = self.event("stop", agent=name, transcript=str(transcript))
            first = self.control.handle("status", {"task_id": record["task_id"]})
            second = self.control.handle("status", {"task_id": record["task_id"]})
            self.assertTrue(first["usage_partial"])
            self.assertIsNone(first["usage"])
            self.assertEqual(first["usage_source"], "unavailable")
            self.assertEqual(second["usage"], first["usage"])
            self.assertIsNone(first["cost"])

    def test_transcript_cursor_is_path_scoped_and_failures_are_durable(self):
        first_path = self.root / "first.jsonl"
        second_path = self.root / "second.jsonl"
        first_path.write_text(json.dumps({
            "type": "assistant", "message": {"id": "first", "model": "claude-haiku-5-5",
                                                "usage": {"input_tokens": 1, "output_tokens": 1}},
        }) + "\n")
        second_path.write_text(json.dumps({
            "type": "assistant", "message": {"id": "second", "model": "claude-haiku-5-5",
                                                "usage": {"input_tokens": 9, "output_tokens": 9}},
        }) + "\n")
        record = self.event("start", agent="path-switch", at=1000, transcript=str(first_path))
        first = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(first["usage"]["input_tokens"], 1)

        self.event("activity", agent="path-switch", at=1100, transcript=str(second_path))
        switched = self.control.handle("status", {"task_id": record["task_id"]})
        self.assertEqual(switched["usage"]["input_tokens"], 9)
        self.assertFalse(switched["usage_partial"])

        second_path.write_text("")
        truncated = self.control.handle("status", {"task_id": record["task_id"]})
        snapshotted = next(item for item in self.control.native.snapshot()
                           if item["task_id"] == record["task_id"])
        for visible in (truncated, snapshotted):
            self.assertTrue(visible["usage_partial"])
            self.assertIsNone(visible["usage"])
            self.assertIsNone(visible["cost"])
            self.assertIn("truncated", visible["transcript_error"].lower())

        malformed_path = self.root / "malformed.jsonl"
        malformed_path.write_text("{bad json}\n" + first_path.read_text())
        malformed = self.event("stop", agent="malformed-line", at=1200,
                               transcript=str(malformed_path))
        malformed = self.control.handle("status", {"task_id": malformed["task_id"]})
        self.assertTrue(malformed["usage_partial"])
        self.assertIsNone(malformed["usage"])
        self.assertIn("malformed", malformed["transcript_error"].lower())

        nested_path = self.root / "deeply-nested.jsonl"
        nested_path.write_text("[" * 2_000 + "0" + "]" * 2_000 + "\n")
        nested = self.event("stop", agent="deeply-nested", at=1300, transcript=str(nested_path))
        nested = self.control.handle("status", {"task_id": nested["task_id"]})
        self.assertTrue(nested["usage_partial"])
        self.assertIsNone(nested["usage"])
        self.assertIn("malformed", nested["transcript_error"].lower())

    def test_native_output_redacts_every_display_field_and_rejects_unsafe_identities(self):
        secret = "password is correct horse battery staple"
        record = self.event(
            "launch", agent="privacy", task=secret, activity=secret, summary=secret,
            agent_type=secret, requested_model=secret, resolved_model=secret,
            cwd=f"/tmp/{secret}", worktree=f"/tmp/{secret}",
        )
        self.assertNotIn("correct horse", json.dumps(record))
        with self.control.store.connect() as db:
            persisted = db.execute(
                "SELECT data FROM observations WHERE task_id=?", (record["task_id"],),
            ).fetchone()[0]
        self.assertNotIn("correct horse", persisted)
        with self.assertRaisesRegex(ValueError, "invalid parent_agent_id"):
            self.event("launch", agent="identity", parent_agent_id="token=secret-value")
        for transcript in ("x" * 2001, "/tmp/token=secret-value"):
            with self.subTest(transcript=transcript[:20]), self.assertRaisesRegex(ValueError, "invalid transcript"):
                self.event("start", agent="path", transcript=transcript)

        legacy = self.control.native.get(record["task_id"])
        legacy.update(summary=secret, activity=secret, files_touched=[secret],
                      price_sources={secret: secret})
        self.assertNotIn("correct horse", json.dumps(self.control.native.view(legacy)))
        legacy["agent_id"] = "token=secret-value"
        with self.assertRaisesRegex(ValueError, "invalid agent_id"):
            self.control.native.view(legacy)


if __name__ == "__main__":
    unittest.main()
