import json
import tempfile
import time
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
                            requested_model="opus", resolved_model="claude-opus")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual((result["task"], result["resolved_model"]), ("Fix parser", "claude-opus"))
        self.assertEqual(stopped["control_mode"], "observe")
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
        self.assertIn("n/a", rendered[0])  # Native cost belongs to Langfuse.
        self.assertIn(record["task_id"], rendered[0])

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

    def test_terminal_result_blocks_older_delayed_start(self):
        for terminal in ("stop", "failed"):
            agent = "after-" + terminal
            self.event("start", agent=agent, at=1000)
            closed = self.event(terminal, agent=agent, at=1100)
            self.event("result", agent=agent, at=1300)
            for at in (1200, 1300):
                delayed = self.event("start", agent=agent, at=at)
                self.assertEqual(delayed["status"], closed["status"], (terminal, at))
                self.assertEqual(delayed["finished_at"], closed["finished_at"])
            reopened = self.event("start", agent=agent, at=1301)
            self.assertEqual(self.control.native.get(reopened["task_id"])["status"], "running")

    def test_delayed_failure_closes_like_delayed_stop(self):
        self.event("start", agent="after-result", at=1000)
        self.event("result", agent="after-result", at=2100)
        failed = self.event("failed", agent="after-result", at=2000)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["duration_ms"], 1000)
        self.event("start", agent="after-end", session="E", at=1000)
        self.event("session_end", agent=None, session="E", at=3000)
        failed = self.event("failed", agent="after-end", session="E", at=2500)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["duration_ms"], 1500)

    def test_legacy_row_with_invalid_identity_does_not_hide_other_agents(self):
        at = int(time.time() * 1000)
        self.event("start", agent="good", at=at)
        legacy = self.event("start", agent="legacy", at=at)
        with self.control.store.transaction() as db:
            db.execute("UPDATE observations SET data=json_set(data,'$.agent_id',' spaced') WHERE task_id=?",
                       (legacy["task_id"],))
        with self.assertLogs("router.native", "WARNING"):
            rows = self.control.native.status()
        self.assertEqual([row["agent_id"] for row in rows], ["good"])
        with self.assertLogs("router.native", "WARNING"):
            records, active = self.control.native.fleet_records(10)
        self.assertEqual([record["agent_id"] for record in records], ["good"])
        self.assertIsNone(active)

    def test_fleet_page_keeps_needs_input_agent_ahead_of_running_agents(self):
        at = int(time.time() * 1000)
        self.event("needs_input", agent="waiting", at=at - 5000)
        for index in range(3):
            self.event("start", agent=f"busy-{index}", at=at - index)
        records, _ = self.control.native.fleet_records(1)
        self.assertEqual(records[0]["agent_id"], "waiting")

    def test_native_clock_rollback_is_uncertain_not_fresh(self):
        at = int(time.time() * 1000)
        record = self.event("start", agent="ahead", at=at)
        with patch("router.native.now_ms", return_value=at - 5000):
            active = self.control.native.fleet_records(0)[1]
            view = self.control.native.view(self.control.native.get(record["task_id"]))
        self.assertIsNone(active)
        self.assertEqual(view["status"], "unknown")
        self.assertIn("clock", view["visibility_reason"])
        self.assertEqual(self.control.native.fleet_records(0)[1], 1)

    def test_failed_agent_reports_known_elapsed_time(self):
        self.event("start", agent="elapsed", at=1000)
        failed = self.event("failed", agent="elapsed", at=2000)
        self.assertEqual(failed["elapsed_ms"], 1000)

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
        )

        self.assertEqual(launched["status"], "stopped")
        self.assertEqual(launched["finished_at"], stopped["finished_at"])
        self.assertEqual(launched["duration_ms"], stopped["duration_ms"])
        self.assertEqual(launched["last_observed_status"], "stopped")
        self.assertEqual(
            (launched["parent_agent_id"], launched["task"], launched["agent_type"]),
            ("main", "Fix parser", "worker"),
        )
        self.assertEqual((launched["requested_model"], launched["resolved_model"]), ("opus", "claude-opus"))

        final = self.event(
            "result", at=2200, task="Final task", resolved_model="claude-final",
            agent_type="reviewer", cwd="/final",
            parent_agent_id="new-parent",
        )
        delayed = self.event(
            "start", at=500, task="Stale task", resolved_model="claude-old",
            agent_type="stale", cwd="/stale",
            parent_agent_id="old-parent",
        )
        self.assertEqual(delayed["status"], "stopped")
        self.assertEqual(
            (delayed["task"], delayed["resolved_model"], delayed["agent_type"], delayed["cwd"]),
            (final["task"], final["resolved_model"], final["agent_type"], final["cwd"]),
        )
        self.assertEqual(delayed["parent_agent_id"], "new-parent")

    def test_identifiers_ordering_restart_segments_and_legacy_summary_sanitizing(self):
        exact = "s" * 240
        self.assertEqual(self.event("start", session=exact, agent=exact)["agent_id"], exact)
        for key, value in (("session", "s" * 241), ("agent", "a" * 241),
                           ("session", "bad\x1b[2Jid"), ("agent", "bad\x00id")):
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
        legacy["task"] = "done \x1b[31mok"
        self.assertEqual(self.control.native.view(legacy)["task"], "done ok")

    def test_content_usage_and_cost_are_neither_stored_nor_shown_even_from_legacy_rows(self):
        # Langfuse owns content, per-call usage, and cost; Router keeps lifecycle only.
        record = self.event("result", agent="boundary", at=1000, task="Fix parser",
                            resolved_model="claude-opus", summary="assistant text", cost=.25,
                            transcript="/tmp/agent.jsonl", file_path="/tmp/notes.md")
        retired = {"summary", "cost", "cost_source", "cost_note", "usage", "usage_source", "usage_partial",
                   "models", "files_touched", "transcript", "transcript_error", "transcript_pending",
                   "messages", "price_sources", "price_overrides_used", "price_date", "price_source"}
        stored = self.control.native.get(record["task_id"])
        self.assertFalse(retired & stored.keys(), retired & stored.keys())
        self.assertEqual((stored["task"], stored["resolved_model"]), ("Fix parser", "claude-opus"))
        # A row written by an older version still holds these fields (and possibly a
        # transcript-derived model); no view may expose them.
        legacy = {**stored, **{key: "legacy" for key in retired},
                  "resolved_model": "from-transcript", "model_source": "transcript"}
        with self.control.store.transaction() as db:
            db.execute("UPDATE observations SET data=? WHERE task_id=?", (json.dumps(legacy), record["task_id"]))
        views = [self.control.handle("status", {"task_id": record["task_id"]}),
                 *self.control.native.status(), *self.control.native.fleet_records(10)[0],
                 *self.control.handle("snapshot", {})["agents"]]
        for view in views:
            self.assertFalse(retired & view.keys(), retired & view.keys())
            self.assertIsNone(view["resolved_model"])
        # Daemon start removes the retired content from disk.
        self.control.native.reconcile_restart()
        self.assertFalse(retired & self.control.native.get(record["task_id"]).keys())

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
        # Keep every synthetic event in the past; future stamps read as clock rollback.
        base = int(time.time() * 1000) - 10_000
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
        # Keep every synthetic event in the past; future stamps read as clock rollback.
        base = int(time.time() * 1000) - 10_000
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

    def test_native_output_strips_controls_from_displays_and_rejects_unsafe_identities(self):
        control = "build \x1b[31mred\x1b]0;title\x07 done"
        record = self.event(
            "launch", agent="display", task=control, activity=control, summary=control,
            agent_type=control, requested_model=control, resolved_model=control,
            cwd=f"/tmp/{control}", worktree=f"/tmp/{control}",
        )
        self.assertEqual(record["task"], "build red done")
        with self.control.store.connect() as db:
            persisted = db.execute(
                "SELECT data FROM observations WHERE task_id=?", (record["task_id"],),
            ).fetchone()[0]
        self.assertNotIn("\\u001b", persisted)
        with self.assertRaisesRegex(ValueError, "invalid parent_agent_id"):
            self.event("launch", agent="identity", parent_agent_id="parent\x1b[2J")
        legacy = self.control.native.get(record["task_id"])
        legacy.update(activity=control)
        self.assertNotIn("\\u001b", json.dumps(self.control.native.view(legacy)))
        legacy["agent_id"] = "agent\x1b[2J"
        with self.assertRaisesRegex(ValueError, "invalid agent_id"):
            self.control.native.view(legacy)


if __name__ == "__main__":
    unittest.main()
