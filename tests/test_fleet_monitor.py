import fcntl
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import mock_open, patch

from test_control import settings

from router.capacity import (
    MAX_RESERVATION_BYTES,
    CapacitySampler,
    CapacityThresholds,
    ReservationReader,
    _darwin_memory,
    _linux_cpu_ticks,
    _linux_memory,
    classify,
)
from router.cli import fleet_table
from router.cli import main as cli_main
from router.client import request
from router.config import Settings
from router.daemon import Controller
from router.db import Store
from router.fleet import (
    MAX_COLLECTION_RECORDS,
    FleetConfig,
    FleetHost,
    _error_text,
    _prefix_identities,
    _validate_snapshot,
    capacity_projection,
    fleet_exit_ok,
    load_fleet_config,
    query_fleet,
    ssh_snapshot,
)
from router.fleet_snapshot import MAX_SNAPSHOT_BYTES, AlertHistory, bound, compose
from router.host import HostIdentity, current_boot_id, stable_host_id
from router.native import NativeObserver
from router.sanitize import bound_text
from router.sessions import MAX_EXACT_INFLIGHT_TOOLS, SessionObserver
from router.validation import HOST_STRING_LIMITS


def snapshot(host_id="host-1"):
    return {"schema_version": 1, "partial": False, "generated_at": 1,
            "host": {"host_id": host_id, "hostname": "test-host", "boot_id": "boot-1",
                     "platform": "linux", "architecture": "x86_64",
                     "router_version": "0.4.0", "claude_version": "2.1.300",
                     "observed_at": 1},
            "capacity": {"logical_cpus": 8, "load_1m": 1, "load_5m": 1, "cpu_busy_pct": 20,
                         "memory_total_bytes": 32 * 1024**3,
                         "memory_available_bytes": 16 * 1024**3, "disk_available_bytes": 20 * 1024**3,
                         "swap_used_bytes": 0, "observed_at": 1, "sample_age_ms": 1,
                         "active_claude_sessions": 1,
                         "active_agents": 1, "active_managed_processes": 0},
            "sessions": [{"host_id": host_id, "source": "claude", "session_id": "same",
                          "status": "running", "started_at": 0, "last_event_at": 1}],
            "agents": [{"host_id": host_id, "source": "claude", "parent_session_id": "same",
                        "agent_id": "child", "status": "running", "started_at": 0,
                        "last_event_at": 1}],
            "tasks": [], "managed_processes": [], "inferred_processes": [], "alerts": []}


class HostAndMigrationTests(unittest.TestCase):
    def test_host_view_keeps_display_text_strips_controls_and_rejects_unsafe_identity(self):
        host = HostIdentity.__new__(HostIdentity)
        host.host_id = "host"
        host._static = {
            "host_id": "host", "hostname": "build \x1b[31mbox\x1b[0m", "boot_id": "boot-1",
            "platform": "darwin", "architecture": "arm64", "router_version": "0.4.0",
            "claude_version": "2.1.300",
        }
        view = host.view(1)
        self.assertEqual(view["hostname"], "build box")
        self.assertEqual(view["platform"], "darwin")
        host.host_id = "host\x1b[2J"
        with self.assertRaisesRegex(ValueError, "host identity"):
            host.view(1)

    def test_host_display_bounds_match_the_snapshot_wire_schema(self):
        base = {
            "host_id": "host", "hostname": "host", "boot_id": "boot",
            "platform": "linux", "architecture": "x86_64",
            "router_version": "0.4.0", "claude_version": "2.1.300",
        }
        for field, limit in HOST_STRING_LIMITS.items():
            if field == "host_id":
                continue
            with self.subTest(field=field, boundary="at"):
                host = HostIdentity.__new__(HostIdentity)
                host.host_id = "host"
                host._static = {**base, field: "x" * limit}
                value = snapshot("host")
                value["host"] = host.view(1)
                self.assertEqual(len(value["host"][field]), limit)
                self.assertEqual(_validate_snapshot(json.dumps(value).encode())["host"][field], "x" * limit)
            with self.subTest(field=field, boundary="beyond"):
                host._static = {**base, field: "x" * (limit + 1)}
                with self.assertRaisesRegex(ValueError, field):
                    host.view(1)

    def test_generated_host_identity_is_stable_private_and_boot_id_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = stable_host_id(directory, "darwin")
            self.assertEqual(stable_host_id(directory, "darwin"), first)
            self.assertEqual((directory / "host-id").stat().st_mode & 0o777, 0o600)
        def completed(value):
            return subprocess.CompletedProcess([], 0, stdout=value, stderr=b"")
        with patch("router.host.subprocess.run", side_effect=[completed(b"boot one"), completed(b"boot two")]):
            self.assertNotEqual(current_boot_id("darwin"), current_boot_id("darwin"))

    def test_generated_host_identity_recovers_empty_file_and_concurrent_creators_agree(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "host-id").touch()
            with ThreadPoolExecutor(max_workers=8) as pool:
                identities = list(pool.map(lambda _: stable_host_id(directory, "darwin"), range(8)))
            self.assertEqual(len(set(identities)), 1)
            uuid.UUID(identities[0])
            self.assertEqual((directory / "host-id").stat().st_mode & 0o777, 0o600)

    def test_empty_host_identity_recovery_is_serialized_across_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "host-id").touch()
            lock_descriptor = os.open(directory / ".host-id.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            script = (
                "import sys\n"
                "from pathlib import Path\n"
                "import router.host as host\n"
                "real_flock = host.fcntl.flock\n"
                "def traced_flock(descriptor, operation):\n"
                "    print('locking', flush=True)\n"
                "    return real_flock(descriptor, operation)\n"
                "host.fcntl.flock = traced_flock\n"
                "print(host.stable_host_id(Path(sys.argv[1]), 'darwin'), flush=True)\n"
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(directory)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            try:
                locking = [process.stdout.readline().strip() for process in processes]
                blocked = [process.poll() is None for process in processes]
            finally:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)

            completed = [process.communicate(timeout=5) for process in processes]
            self.assertEqual(locking, ["locking", "locking"])
            self.assertEqual(blocked, [True, True])
            self.assertEqual([process.returncode for process in processes], [0, 0])
            self.assertEqual([stderr for _, stderr in completed], ["", ""])
            identities = [stdout.strip() for stdout, _ in completed]
            self.assertEqual(len(set(identities)), 1)
            self.assertEqual((directory / "host-id").read_text().strip(), identities[0])

    def test_additive_observation_migration_preserves_history_and_adds_host_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "router.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE observations(task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL)")
                db.execute("INSERT INTO observations VALUES(?,?,?)", ("native-old", "S", json.dumps({
                    "task_id": "native-old", "parent_session_id": "S", "agent_id": "A", "executor": "claude"})))
            store = Store(path, "local-host")
            with store.connect() as db:
                self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(observations)")],
                                 ["task_id", "session_id", "data"])
                row = db.execute("SELECT data FROM observations").fetchone()
                record = json.loads(row[0])
                self.assertEqual(record["task_id"], "native-old")
                self.assertEqual(record["host_id"], "local-host")
                self.assertEqual(record["source"], "claude")

    def test_unknown_observation_schema_is_rejected_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "router.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE observations(
                    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    data TEXT NOT NULL, legacy_note TEXT NOT NULL)""")
                db.execute("INSERT INTO observations VALUES(?,?,?,?)",
                           ("native-old", "S", "{}", "retain me"))
            with self.assertRaisesRegex(ValueError, "Unsupported observations schema"):
                Store(path, "local-host")
            with sqlite3.connect(path) as db:
                self.assertEqual(
                    [row[1] for row in db.execute("PRAGMA table_info(observations)")],
                    ["task_id", "session_id", "data", "legacy_note"],
                )
                self.assertEqual(
                    db.execute("SELECT legacy_note FROM observations").fetchone()[0],
                    "retain me",
                )

    def test_migration_is_atomic_and_preserves_legacy_positional_inserts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "router.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE observations(task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL)")
                db.execute("INSERT INTO observations VALUES(?,?,?)", ("bad", "S", "not-json"))
            with self.assertRaises(json.JSONDecodeError):
                Store(path, "host")
            with sqlite3.connect(path) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables, {"observations"})
                db.execute("UPDATE observations SET data=?", (json.dumps({
                    "task_id": "bad", "parent_session_id": "S", "agent_id": "A", "executor": "claude"}),))
            Store(path, "host")
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO observations VALUES(?,?,?)", ("legacy-write", "S2", json.dumps({
                    "task_id": "legacy-write", "parent_session_id": "S2", "agent_id": "B"})))
                self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)

    def test_reopening_store_does_not_rebuild_observation_index(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "router.sqlite3"
            Store(path, "host")
            with sqlite3.connect(path) as db:
                schema_version = db.execute("PRAGMA schema_version").fetchone()[0]
            Store(path, "host")
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA schema_version").fetchone()[0], schema_version)

    def test_active_tool_deadline_index_persists_and_serves_the_count_query(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "router.sqlite3"
            Store(path, "host")
            Store(path, "host")

            with sqlite3.connect(path) as db:
                index = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                    ("sessions_active_tool_deadline",),
                ).fetchone()
                plan = db.execute("""EXPLAIN QUERY PLAN SELECT COUNT(*) FROM sessions
                    WHERE host_id=? AND json_extract(data,'$.status')='running'
                      AND json_extract(data,'$.last_event_at')<?
                      AND json_extract(data,'$.tool_active_until')>=?""",
                                  ("host", 1, 1)).fetchall()

        self.assertIsNotNone(index)
        self.assertIn("sessions_active_tool_deadline", " ".join(str(item) for row in plan for item in row))


class SessionAndCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "router.sqlite3", "host")
        self.observer = SessionObserver(self.store, "host")
        self.now = int(time.time() * 1000)

    def event(self, kind, **values):
        return self.observer.observe({"source": "claude", "session_id": "S", "kind": kind,
                                      "observed_at": values.pop("observed_at", self.now), **values})

    def test_waiting_notification_does_not_hide_uncompleted_tool_after_freshness_window(self):
        start = self.now - 61_000
        self.event("tool_start", tool_use_id="tool-1", activity="Bash", observed_at=start)
        waiting = self.event("notification", notification_type="permission_prompt",
                             agent_id="other-agent", observed_at=start + 1)
        self.assertTrue(waiting["needs_input"])
        records, active = self.observer.fleet_records(10)
        self.assertEqual(records[0]["inflight_tool_count"], 1)
        self.assertNotEqual(records[0]["status"], "unknown")
        self.assertEqual(active, 1)

    def test_delayed_completion_cannot_clear_tool_evicted_from_event_history(self):
        start = self.now - 61_000
        self.event("tool_start", tool_use_id="long", activity="Bash", observed_at=start)
        for index in range(130):  # Evicts "long" from the 128-entry event history.
            self.event("tool_start", tool_use_id=f"short-{index}", observed_at=start + 1 + 2 * index)
            self.event("tool_end", tool_use_id=f"short-{index}", observed_at=start + 2 + 2 * index)
        self.event("tool_end", tool_use_id="long", observed_at=start - 1)  # Older than the start.
        records, active = self.observer.fleet_records(10)
        self.assertEqual(records[0]["inflight_tool_count"], 1)
        self.assertEqual(active, 1)

    def test_delayed_pre_restart_tool_start_stays_active_after_restart_window(self):
        self.event("prompt", observed_at=self.now - 3000)
        self.event("notification", notification_type="agent_completed", observed_at=self.now - 1000)
        self.observer.reconcile_restart()
        self.event("tool_start", tool_use_id="late", activity="Bash", observed_at=self.now - 2000)
        with patch("router.sessions.now_ms", return_value=self.now + 61_000):
            self.assertEqual(self.observer.fleet_records(10)[1], 1)

    def test_delayed_tool_start_after_agent_completed_counts_once_as_active(self):
        self.event("prompt", observed_at=self.now - 3000)
        completed = self.event("notification", notification_type="agent_completed", observed_at=self.now - 1000)
        self.assertEqual(completed["status"], "idle")
        self.event("tool_start", tool_use_id="late-tool", activity="Bash", observed_at=self.now - 2000)
        records, active = self.observer.fleet_records(10)
        self.assertEqual(records[0]["inflight_tool_count"], 1)
        self.assertEqual(active, 1)

    def test_same_time_task_or_subagent_event_does_not_reopen_stopped_turn(self):
        for kind in ("task_completed", "subagent_stop", "subagent_start", "task_created"):
            session = "tie-" + kind
            self.observer.observe({"source": "claude", "session_id": session, "kind": "prompt",
                                   "observed_at": self.now})
            self.observer.observe({"source": "claude", "session_id": session, "kind": "stop",
                                   "observed_at": self.now + 1})
            tied = self.observer.observe({"source": "claude", "session_id": session, "kind": kind,
                                          "observed_at": self.now + 1, "task_id": "T", "agent_id": "A"})
            self.assertEqual(tied["status"], "idle", kind)
            later = self.observer.observe({"source": "claude", "session_id": session, "kind": kind,
                                           "observed_at": self.now + 2, "task_id": "T", "agent_id": "A"})
            self.assertEqual(later["status"], "running", kind)

    def test_session_tasks_needs_input_stop_and_restart_uncertainty(self):
        self.assertEqual(self.event("session_start", cwd="/project")["status"], "running")
        self.event("task_created", task_id="T", task_subject="Safe subject", task_description="PRIVATE")
        waiting = self.event("notification", notification_type="agent_needs_input")
        self.assertTrue(waiting["needs_input"])
        self.assertEqual(waiting["status"], "idle")
        running = self.event("stop", background_tasks=[{"status": "running"}])
        self.assertEqual(running["status"], "running")
        self.observer.reconcile_restart()
        restarted = self.observer.sessions()[0]
        self.assertEqual(restarted["status"], "unknown")
        self.assertIn("restart", restarted["visibility_reason"])
        self.assertNotIn("PRIVATE", json.dumps(self.observer.tasks()))
        self.assertEqual(self.event("session_end")["status"], "stopped")

    def test_restart_unknown_records_remain_visible_until_terminal_evidence(self):
        old = self.now - 301_000
        self.event("session_start", observed_at=old)
        native = NativeObserver(self.store, host_id="host")
        native.observe({
            "source": "claude", "session_id": "S", "agent_id": "A",
            "kind": "start", "observed_at": old,
        })

        self.observer.reconcile_restart()
        native.reconcile_restart()
        self.assertEqual(self.observer.sessions()[0]["status"], "unknown")
        self.assertEqual(native.snapshot()[0]["status"], "unknown")
        self.assertEqual(self.observer.active_count(), 0)
        self.assertEqual(native.active_count(), 0)

        self.event("session_end", observed_at=old + 1)
        native.observe({
            "source": "claude", "session_id": "S", "agent_id": "A",
            "kind": "stop", "observed_at": old + 1,
        })
        self.assertEqual(self.observer.sessions(), [])
        self.assertEqual(native.snapshot(), [])

    def test_operator_visible_task_text_is_kept_bounded_and_control_free(self):
        self.event("session_start")
        self.event("task_created", task_id="T", task_subject="status=failed\napiKey=example")
        self.assertEqual(self.observer.tasks()[0]["subject"], "status=failed apiKey=example")
        self.event("task_created", task_id="long", task_subject="x" * 600)
        bounded = next(task for task in self.observer.tasks() if task["task_id"] == "long")
        self.assertEqual(bounded["subject"], "x" * 500)

        native = NativeObserver(self.store, host_id="host")
        record = native.observe({
            "source": "claude", "session_id": "S", "agent_id": "A", "kind": "launch",
            "observed_at": self.now, "task": "Fix https://user:pw@example.com/repo \x1b[31mnow",
        })
        self.assertEqual(record["task"], "Fix https://user:pw@example.com/repo now")
        legacy = native.get(record["task_id"])
        legacy["task"] = "legacy \x1b]0;title\x07text"
        with self.store.transaction() as db:
            native.put(db, legacy)
        self.assertEqual(native.snapshot()[0]["task"], "legacy text")

    def test_session_and_task_serialization_strips_controls_and_rejects_unsafe_identities(self):
        control = "build \x1b[31mred\x1b[0m"
        visible = self.event("session_start", cwd=f"/tmp/{control}", model="opus",
                             start_source="resume")
        self.assertEqual(visible["cwd"], "/tmp/build red")
        self.event("task_created", task_id="safe-task", task_subject=control, teammate_name=control)
        self.assertNotIn("\x1b", json.dumps(self.observer.tasks()))

        for field, value in (("session_id", "bad\x1b[2J"), ("parent_session_id", " padded ")):
            event = {"source": "claude", "session_id": "safe", "kind": "session_start",
                     "observed_at": self.now, field: value}
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                self.observer.observe(event)
        with self.assertRaisesRegex(ValueError, "invalid task_id"):
            self.event("task_created", task_id="task\x07")
        with self.assertRaisesRegex(ValueError, "invalid tool_use_id"):
            self.event("tool_start", tool_use_id="tool\x1b]0;x\x07")

        with self.store.transaction() as db:
            record = self.observer._session(db, "claude", "S", self.now)
            record["session_id"] = "bad\x1b[2J"
            db.execute("DELETE FROM sessions WHERE host_id=? AND source=? AND session_id=?",
                       ("host", "claude", "S"))
            self.observer._put_session(db, record)
        with self.assertRaisesRegex(ValueError, "invalid session_id"):
            self.observer.sessions()

    def test_capacity_classification_checks_all_dimensions_and_staleness(self):
        sample = {"logical_cpus": 8, "load_1m": 7, "cpu_busy_pct": 70,
                  "memory_available_bytes": 3 * 1024**3, "disk_available_bytes": 9 * 1024**3,
                  "sample_age_ms": 1}
        state, reasons = classify(sample, CapacityThresholds())
        self.assertEqual(state, "busy")
        self.assertEqual(len(reasons), 4)
        stale, stale_reasons = classify({**sample, "sample_age_ms": 20_000}, CapacityThresholds())
        self.assertEqual(stale, "unknown")
        self.assertIn("stale", stale_reasons[0])
        available, available_reasons = classify({**sample, "load_1m": 1, "cpu_busy_pct": 20,
                                                  "memory_available_bytes": 8 * 1024**3,
                                                  "disk_available_bytes": 20 * 1024**3}, CapacityThresholds())
        self.assertEqual((available, available_reasons), ("available", []))
        saturated, saturated_reasons = classify({**sample, "cpu_busy_pct": 95}, CapacityThresholds())
        self.assertEqual(saturated, "saturated")
        self.assertTrue(any("cpu_busy_pct" in reason for reason in saturated_reasons))
        missing, missing_reasons = classify({**sample, "cpu_busy_pct": None}, CapacityThresholds())
        self.assertEqual(missing, "unknown")
        self.assertIn("cpu_busy_pct", missing_reasons[0])
        partial, partial_reasons = classify(
            {**sample, "partial": True, "capacity_state": "unknown",
             "capacity_reasons": ["reservation data unavailable"]},
            CapacityThresholds(),
        )
        self.assertEqual((partial, partial_reasons),
                         ("unknown", ["reservation data unavailable"]))
        missing_age, missing_age_reasons = classify(
            {key: value for key, value in sample.items() if key != "sample_age_ms"},
            CapacityThresholds(),
        )
        self.assertEqual(missing_age, "unknown")
        self.assertIn("age", missing_age_reasons[0])
        incomplete_reservation, reservation_reasons = classify(
            {**sample, "reserved_by": "coordinator", "reserved_until": self.now + 60_000,
             "reserved_cpu_slots": 1, "reserved_memory_bytes": 0,
             "reservation_source": "scheduler"},
            CapacityThresholds(),
        )
        self.assertEqual(incomplete_reservation, "unknown")
        self.assertIn("incomplete", reservation_reasons[0])

    def test_linux_capacity_parsers_use_structured_proc_fields(self):
        with patch("router.capacity.Path.read_text", return_value=(
                "MemTotal: 100 kB\nMemAvailable: 40 kB\nSwapTotal: 20 kB\nSwapFree: 5 kB\n")):
            self.assertEqual(_linux_memory(), (102_400, 40_960, 15_360))
        with patch("router.capacity.Path.read_text", return_value="cpu  10 2 3 20 5 0 0 0\n"):
            self.assertEqual(_linux_cpu_ticks(), (40, 25))

    def test_linux_cpu_ticks_do_not_double_count_guest_time(self):
        with patch("router.capacity.Path.read_text",
                   return_value="cpu  10 2 3 20 5 0 0 0 7 11\n"):
            self.assertEqual(_linux_cpu_ticks(), (40, 25))

    def test_stale_nonterminal_session_and_native_agent_remain_visible_as_unknown(self):
        old = self.now - 61_000
        self.event("session_start", observed_at=old)
        self.event("notification", observed_at=old + 1, notification_type="agent_needs_input")
        waiting = self.observer.sessions()[0]
        self.assertEqual(waiting["status"], "unknown")
        self.assertFalse(waiting["needs_input"])
        native = NativeObserver(self.store, host_id="host")
        native.observe({"source": "claude", "session_id": "S", "agent_id": "A",
                        "kind": "idle", "observed_at": old})
        self.assertEqual(native.snapshot()[0]["status"], "unknown")
        with patch("router.sessions.now_ms", return_value=self.now + 301_000), \
                patch("router.native.now_ms", return_value=self.now + 301_000):
            self.assertEqual(self.observer.sessions()[0]["status"], "unknown")
            self.assertEqual(native.snapshot()[0]["status"], "unknown")

    def test_waiting_state_is_ordered_per_agent_not_per_session(self):
        base = self.now - 10_000  # Whole timeline in the past (process rule 5b).

        def notice(agent, offset, kind="agent_needs_input"):
            return self.event("notification", observed_at=base + offset, notification_type=kind, agent_id=agent)

        cases = {
            # The deferred PR #4 finding: a delayed completion for A arrives after B's newer notice.
            "delayed completion for another agent": (
                [("A", 1, "agent_needs_input"), ("B", 3, "agent_needs_input"),
                 ("A", 2, "agent_completed"), ("B", 4, "agent_completed")], False),
            "delayed completion older than the same agent's newer notice": (
                [("A", 5, "agent_needs_input"), ("A", 2, "agent_completed")], True),
            "delayed notice older than the agent's completion": (
                [("A", 4, "agent_completed"), ("A", 2, "agent_needs_input")], False),
            # A completion that clears nothing is still evidence and must be kept.
            "completion recorded before its notice arrives": (
                [("B", 4, "agent_completed"), ("A", 3, "agent_completed"), ("A", 2, "agent_needs_input")], False),
            # A delayed notice must set the waiting reason as an in-order one would.
            "delayed session-level notice": ([("A", 10, "agent_completed"), (None, 5, "permission_prompt")], True),
        }
        for name, (events, waiting) in cases.items():
            with self.subTest(name):
                self.store = Store(Path(tempfile.mkdtemp(dir=self.temp.name)) / "router.sqlite3", "host")
                self.observer = SessionObserver(self.store, "host")
                self.event("session_start", observed_at=base)
                for agent, offset, kind in events:
                    result = notice(agent, offset, kind)
                self.assertEqual(result["needs_input"], waiting)
                session = self.observer.sessions()[0]
                self.assertEqual(session["needs_input"], waiting)
                self.assertEqual(session["visibility_reason"] == "Waiting for input", waiting)
                self.assertEqual(self.observer.fleet_records(10)[1], 1 if waiting else 0)

    def test_waiter_history_overflow_reports_uncertainty_instead_of_guessing(self):
        from router.sessions import MAX_WAITER_HISTORY
        base = self.now - 10_000
        # The subagent events keep the session running, so the exact total is 1.
        for others, waiting, exact in ((MAX_WAITER_HISTORY - 1, False, 1), (MAX_WAITER_HISTORY, True, None)):
            with self.subTest(others=others):
                self.store = Store(Path(tempfile.mkdtemp(dir=self.temp.name)) / "router.sqlite3", "host")
                self.observer = SessionObserver(self.store, "host")
                self.event("session_start", observed_at=base)
                self.event("notification", observed_at=base + 10, notification_type="agent_needs_input", agent_id="X")
                self.event("notification", observed_at=base + 20, notification_type="agent_completed", agent_id="X")
                for index in range(others):
                    self.event("subagent_start", observed_at=base + 30 + index, agent_id=f"Y{index}")
                # X's delayed notice is rejected while its completion is retained; once the
                # history overflows it is accepted and the active total is reported unknown.
                late = self.event("notification", observed_at=base + 15,
                                  notification_type="agent_needs_input", agent_id="X")
                # Accepted after overflow, but shown as uncertain rather than a confirmed wait.
                self.assertFalse(late["needs_input"])
                self.assertEqual(late["status"] == "unknown", waiting)
                with patch("router.sessions.now_ms", return_value=base + 30 + others):
                    self.assertEqual(self.observer.fleet_records(10)[1], exact)
                if waiting:
                    # The session itself shows the uncertainty, not a confirmed wait.
                    with patch("router.sessions.now_ms", return_value=base + 30 + others):
                        session = self.observer.sessions()[0]
                    self.assertEqual((session["status"], session["needs_input"]), ("unknown", False))
                    self.assertIn("tracking bound", session["visibility_reason"])
                    self.event("prompt", observed_at=base + 40 + others)  # A reset resolves it.
                    with patch("router.sessions.now_ms", return_value=base + 41 + others):
                        self.assertEqual(self.observer.fleet_records(10)[1], 1)
                        self.assertEqual(self.observer.sessions()[0]["status"], "running")

    def test_superseded_completions_do_not_fill_waiter_history(self):
        from router.sessions import MAX_WAITER_HISTORY
        base = self.now - 10_000
        self.event("session_start", observed_at=base)
        self.event("prompt", observed_at=base + 2000)
        for index in range(MAX_WAITER_HISTORY + 1):  # All older than the prompt's reset.
            self.event("subagent_stop", observed_at=base + 1 + index, agent_id=f"Y{index}")
        with patch("router.sessions.now_ms", return_value=base + 2001):
            self.assertEqual(self.observer.fleet_records(10)[1], 1)

    def test_older_reset_does_not_clear_newer_eviction_uncertainty(self):
        from router.sessions import MAX_WAITER_HISTORY
        base = self.now - 10_000
        self.event("session_start", observed_at=base)
        for index in range(MAX_WAITER_HISTORY + 1):  # Unmatched completions at 20 overflow the history.
            self.event("tool_end", observed_at=base + 20, tool_use_id=f"T{index}", agent_id=f"A{index}")
        self.event("prompt", observed_at=base + 10)  # Delayed reset, older than the evicted evidence.
        self.event("notification", observed_at=base + 15, notification_type="agent_needs_input", agent_id="A0")
        with patch("router.sessions.now_ms", return_value=base + 21):
            self.assertIsNone(self.observer.fleet_records(10)[1])
            self.assertEqual(self.observer.sessions()[0]["status"], "unknown")

    def test_accepted_early_tool_completion_orders_the_agent_waiter(self):
        for completion in ("tool_end", "tool_failure"):
            with self.subTest(completion):
                self.store = Store(Path(tempfile.mkdtemp(dir=self.temp.name)) / "router.sqlite3", "host")
                self.observer = SessionObserver(self.store, "host")
                base = self.now - 10_000
                self.event("session_start", observed_at=base)
                self.event(completion, observed_at=base + 4, tool_use_id="T", agent_id="A")  # Before its start.
                self.event("notification", observed_at=base + 6, notification_type="agent_completed", agent_id="B")
                self.event("notification", observed_at=base + 2, notification_type="agent_needs_input", agent_id="A")
                self.event("tool_start", observed_at=base + 1, tool_use_id="T", agent_id="A", activity="Bash")
                self.assertFalse(self.observer.sessions()[0]["needs_input"])
                with patch("router.sessions.now_ms", return_value=base + 7):
                    self.assertEqual(self.observer.fleet_records(10)[1], 0)

    def test_pushed_eviction_floor_format_upgrades_to_uncertainty(self):
        base = self.now - 10_000
        self.event("session_start", observed_at=base)
        with self.store.transaction() as db:
            record = self.observer._session(db, "claude", "S", base)
            record.pop("waiter_history_partial", None)
            record.update(waiter_cleared_at={}, waiter_clear_floor=base + 20)  # X's completion was evicted.
            self.observer._put_session(db, record)
        late = self.event("notification", observed_at=base + 15, notification_type="agent_needs_input", agent_id="X")
        self.assertTrue(late["needs_input"] is False and late["status"] == "unknown", late)
        self.assertNotIn("waiter_clear_floor", late)
        with patch("router.sessions.now_ms", return_value=base + 16):
            self.assertIsNone(self.observer.fleet_records(10)[1])

    def test_rejected_tool_replay_does_not_clear_a_waiter(self):
        base = self.now - 10_000
        self.event("session_start", observed_at=base)
        self.event("tool_start", observed_at=base + 5, tool_use_id="T", agent_id="A", activity="Bash")
        self.event("tool_end", observed_at=base + 10, tool_use_id="T", agent_id="A")
        self.event("notification", observed_at=base + 10, notification_type="agent_needs_input", agent_id="A")
        replay = self.event("tool_start", observed_at=base + 10, tool_use_id="T", agent_id="A", activity="Bash")
        self.assertTrue(replay["needs_input"])

    def test_legacy_waiting_list_is_still_cleared_per_agent(self):
        self.event("session_start")
        with self.store.transaction() as db:
            record = self.observer._session(db, "claude", "S", self.now)
            record.pop("waiters", None)
            record.update(waiting_agents=["A", "B"], needs_input=True)
            self.observer._put_session(db, record)
        one_left = self.event("notification", observed_at=self.now + 1, notification_type="agent_completed", agent_id="A")
        self.assertTrue(one_left["needs_input"])  # B, migrated from the legacy list, still waits.
        none_left = self.event("notification", observed_at=self.now + 2, notification_type="agent_completed", agent_id="B")
        self.assertFalse(none_left["needs_input"])

    def test_agent_completion_clears_only_the_matching_waiting_state(self):
        self.event("session_start")
        self.event("notification", observed_at=self.now + 1, notification_type="agent_needs_input", agent_id="A")
        self.event("notification", observed_at=self.now + 2, notification_type="agent_needs_input", agent_id="B")
        one_waiter = self.event("notification", observed_at=self.now + 3,
                                notification_type="agent_completed", agent_id="A")
        self.assertEqual((one_waiter["status"], one_waiter["needs_input"]), ("idle", True))
        notice = self.event("notification", observed_at=self.now + 4,
                            notification_type="agent_completed", agent_id="B")
        self.assertEqual((notice["status"], notice["needs_input"]), ("idle", False))
        self.assertNotIn("waiting_agents", notice)
        self.event("task_completed", observed_at=self.now + 4, task_id="T")
        self.event("task_created", observed_at=self.now + 5, task_id="T")
        task = self.observer.tasks()[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["completed_at"], self.now + 4)

    def test_terminal_states_are_monotonic_and_session_end_preserves_failed_agents(self):
        self.event("session_start")
        self.event("session_end", observed_at=self.now + 1)
        self.assertEqual(self.event("tool_end", observed_at=self.now + 2)["status"], "stopped")
        native = NativeObserver(self.store, host_id="host")
        native.observe({"source": "claude", "session_id": "N", "agent_id": "A",
                        "kind": "start", "observed_at": self.now})
        native.observe({"source": "claude", "session_id": "N", "agent_id": "A",
                        "kind": "failed", "observed_at": self.now + 1})
        native.observe({"source": "claude", "session_id": "N", "kind": "session_end",
                        "observed_at": self.now + 2})
        identity = "native-" + uuid.uuid5(uuid.NAMESPACE_URL, '["claude","N","A"]').hex
        self.assertEqual(native.get(identity)["status"], "failed")
        self.assertEqual(native.observe({"source": "claude", "session_id": "N", "agent_id": "A",
                                        "kind": "activity", "observed_at": self.now + 3})["status"], "failed")

    def test_inflight_tool_keeps_session_active_until_matching_completion(self):
        old = self.now - 600_000
        self.event("session_start", observed_at=old)
        active = self.event("tool_start", observed_at=old + 1, tool_use_id="tool-1", activity="Bash")
        self.assertEqual(active["status"], "running")
        with patch("router.sessions.now_ms", return_value=self.now):
            self.assertEqual(self.observer.sessions()[0]["status"], "running")
            self.assertEqual(self.observer.active_count(), 1)
        completed = self.event("tool_end", observed_at=self.now, tool_use_id="tool-1", activity="Bash")
        self.assertEqual(completed["inflight_tool_count"], 0)
        with patch("router.sessions.now_ms", return_value=self.now + 61_000):
            self.assertEqual(self.observer.active_count(), 0)

    def test_abandoned_inflight_tool_eventually_becomes_unknown_without_leaking_bookkeeping(self):
        old = self.now - 86_400_002
        self.event("session_start", observed_at=old)
        public = self.event("tool_start", observed_at=old + 1, tool_use_id="tool-1", activity="Bash")
        self.assertEqual(public["status"], "unknown")
        self.assertEqual(public["inflight_tool_count"], 1)
        with patch("router.sessions.now_ms", return_value=self.now):
            self.assertEqual(self.observer.active_count(), 0)
        for internal in ("inflight_tools", "tool_event_at", "tool_finished_at", "waiting_agents"):
            self.assertNotIn(internal, public)

    def test_out_of_order_tool_completion_cannot_strand_inflight_work(self):
        self.event("session_start", observed_at=self.now)
        self.event("tool_start", observed_at=self.now + 1, tool_use_id="A", activity="Read")
        self.event("tool_start", observed_at=self.now + 3, tool_use_id="B", activity="Bash")
        self.event("tool_end", observed_at=self.now + 2, tool_use_id="A", activity="Read")
        completed = self.event("tool_end", observed_at=self.now + 4, tool_use_id="B", activity="Bash")
        self.assertEqual(completed["inflight_tool_count"], 0)
        tied_start = self.event("tool_start", observed_at=self.now + 4, tool_use_id="B", activity="Bash")
        self.assertEqual(tied_start["inflight_tool_count"], 0)

    def test_evicted_tool_completion_retains_conservative_terminal_floor(self):
        self.event("session_start", observed_at=self.now)
        for number in range(129):
            completed = self.event(
                "tool_end", observed_at=self.now + 100 + number,
                tool_use_id=f"tool-{number}", activity="Bash",
            )
        self.assertTrue(completed["tool_visibility_partial"])

        delayed = self.event(
            "tool_start", observed_at=self.now + 50,
            tool_use_id="tool-0", activity="Bash",
        )
        self.assertTrue(delayed["tool_visibility_partial"])
        self.assertEqual(delayed["inflight_tool_count"], 0)
        self.assertEqual(delayed["status"], "unknown")
        self.assertIn("bound", delayed["visibility_reason"])

        reset = self.event("prompt", observed_at=self.now + 300)
        self.assertFalse(reset["tool_visibility_partial"])
        self.assertEqual(reset["inflight_tool_count"], 0)

    def test_tool_reset_watermark_and_agent_completion_preserve_live_work(self):
        self.event("session_start", observed_at=self.now)
        self.event("prompt", observed_at=self.now + 10)
        delayed = self.event("tool_start", observed_at=self.now + 10,
                             tool_use_id="old", activity="stale")
        self.assertEqual(delayed["inflight_tool_count"], 0)
        self.event("tool_start", observed_at=self.now + 11,
                   tool_use_id="live", activity="Bash")
        self.event("notification", observed_at=self.now + 12,
                   notification_type="agent_needs_input", agent_id="A")
        completed = self.event("notification", observed_at=self.now + 13,
                               notification_type="agent_completed", agent_id="B")
        self.assertEqual((completed["status"], completed["needs_input"]), ("running", True))
        self.event("session_end", observed_at=self.now + 20)
        tied = self.event("session_start", observed_at=self.now + 20)
        self.assertEqual(tied["status"], "stopped")

    def test_completion_after_stop_updates_only_the_tool_watermark(self):
        self.event("session_start", observed_at=self.now)
        self.event("tool_start", observed_at=self.now + 1,
                   tool_use_id="old", activity="Bash")
        stopped = self.event("stop", observed_at=self.now + 2)
        delayed = self.event("tool_end", observed_at=self.now + 3,
                             tool_use_id="old", activity="Bash")

        self.assertEqual((stopped["status"], delayed["status"]), ("idle", "idle"))
        self.assertEqual(delayed["last_event_at"], stopped["last_event_at"])
        self.assertEqual(delayed["inflight_tool_count"], 0)
        replay = self.event("tool_start", observed_at=self.now + 3,
                            tool_use_id="old", activity="Bash")
        self.assertEqual((replay["status"], replay["inflight_tool_count"]), ("idle", 0))

    def test_uncorrelated_tool_start_cannot_reopen_idle_after_stop(self):
        self.event("session_start", observed_at=self.now)
        stopped = self.event("stop", observed_at=self.now + 1)
        replay = self.event("tool_start", observed_at=self.now + 2, activity="Bash")
        self.assertEqual((stopped["status"], replay["status"]), ("idle", "idle"))
        self.assertEqual(replay["last_event_at"], stopped["last_event_at"])

    def test_display_tool_bound_does_not_destroy_exact_correlation(self):
        self.event("session_start", observed_at=self.now)
        for number in range(65):
            overflow = self.event("tool_start", observed_at=self.now + number + 1,
                                  tool_use_id=f"tool-{number}")
        self.assertEqual(overflow["inflight_tool_count"], 65)
        self.assertTrue(overflow["tool_visibility_partial"])
        for number in range(65):
            overflow = self.event("tool_end", observed_at=self.now + 100 + number,
                                  tool_use_id=f"tool-{number}")
        self.assertEqual(overflow["status"], "running")
        self.assertEqual(overflow["inflight_tool_count"], 0)
        self.assertFalse(overflow["tool_visibility_partial"])
        with patch("router.sessions.now_ms", return_value=self.now + 100 + 65 + 300_001):
            rows, active = self.observer.fleet_records(100)
        self.assertEqual((rows[0]["status"], active), ("unknown", 0))

    def test_exact_tool_bound_degrades_to_unknown_instead_of_synthetic_activity(self):
        self.event("session_start", observed_at=self.now)
        for number in range(MAX_EXACT_INFLIGHT_TOOLS + 1):
            result = self.event(
                "tool_start", observed_at=self.now + number + 1,
                tool_use_id=f"tool-{number}",
            )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["tool_visibility_partial"])
        self.assertEqual(result["inflight_tool_count"], MAX_EXACT_INFLIGHT_TOOLS)
        with patch("router.sessions.now_ms", return_value=self.now + MAX_EXACT_INFLIGHT_TOOLS + 1):
            self.assertIsNone(self.observer.active_count())

    def test_active_tool_total_uses_indexed_deadline_without_decoding_candidates(self):
        old = self.now - 600_000
        self.event("session_start", observed_at=old)
        self.event("tool_start", observed_at=old + 1,
                   tool_use_id="long-running", activity="Bash")

        expired = []
        for number in range(2000):
            session_id = f"expired-{number}"
            expired.append(("host", "claude", session_id, json.dumps({
                "host_id": "host", "source": "claude", "session_id": session_id,
                "status": "running", "last_event_at": old,
                "tool_active_until": self.now - 1,
            }, separators=(",", ":"))))
        with self.store.transaction() as db:
            db.executemany("INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)", expired)

        with patch("router.sessions.now_ms", return_value=self.now), \
                patch.object(self.observer, "view", side_effect=AssertionError("decoded candidate")):
            self.assertEqual(self.observer.active_count(), 1)

    def test_fresh_activity_is_busy_but_unavailable_totals_are_unknown(self):
        base = {"logical_cpus": 8, "load_1m": 1, "cpu_busy_pct": 20,
                "memory_available_bytes": 8 * 1024**3, "disk_available_bytes": 20 * 1024**3,
                "sample_age_ms": 1, "active_claude_sessions": 1,
                "active_agents": 0, "active_managed_processes": 0}
        state, reasons = classify(base, CapacityThresholds())
        self.assertEqual(state, "busy")
        self.assertIn("active Claude sessions", reasons[0])
        unknown, unknown_reasons = classify({**base, "active_agents": None}, CapacityThresholds())
        self.assertEqual(unknown, "unknown")
        self.assertIn("active_agents", unknown_reasons[0])

    def test_first_cpu_sample_is_unknown_until_a_delta_exists(self):
        with patch("router.capacity._raw_cpu_ticks", side_effect=[(100, 50), (120, 55)]), \
                patch("router.capacity._raw_memory", return_value=(16, 8, 0)), \
                patch("router.capacity.shutil.disk_usage", return_value=type("Usage", (), {"free": 20})()), \
                patch("router.capacity.os.getloadavg", return_value=(0, 0, 0)), \
                patch("router.capacity.os.cpu_count", return_value=2):
            sampler = CapacitySampler(Path(self.temp.name))
            self.assertIsNone(sampler.view(CapacityThresholds())["cpu_busy_pct"])
            self.assertEqual(sampler.sample()["cpu_busy_pct"], 75.0)

    def test_each_raw_capacity_failure_is_partial_and_the_sampler_recovers(self):
        with patch("router.capacity._raw_cpu_ticks", return_value=(100, 50)), \
                patch("router.capacity._raw_memory", return_value=(16, 8, 0)), \
                patch("router.capacity.shutil.disk_usage", return_value=type("Usage", (), {"free": 20})()), \
                patch("router.capacity.os.getloadavg", return_value=(0, 0, 0)), \
                patch("router.capacity.os.cpu_count", return_value=2):
            sampler = CapacitySampler(Path(self.temp.name))
            for target in (
                "router.capacity._raw_cpu_ticks",
                "router.capacity._raw_memory",
                "router.capacity.shutil.disk_usage",
                "router.capacity.os.getloadavg",
                "router.capacity.os.cpu_count",
            ):
                with self.subTest(target=target), patch(target, side_effect=OSError("raw sample failed")):
                    failed = sampler.sample()
                    self.assertTrue(failed["partial"])
                    self.assertIn("raw sample failed", failed["error"])
                recovered = sampler.sample()
                self.assertFalse(recovered["partial"])

    def test_capacity_freshness_uses_monotonic_time_and_wall_rollback_is_unknown(self):
        with patch("router.capacity._raw_cpu_ticks", return_value=(100, 50)), \
                patch("router.capacity._raw_memory", return_value=(16, 8, 0)), \
                patch("router.capacity.shutil.disk_usage", return_value=type("Usage", (), {"free": 20})()), \
                patch("router.capacity.os.getloadavg", return_value=(0, 0, 0)), \
                patch("router.capacity.os.cpu_count", return_value=2), \
                patch("router.capacity.time.time", return_value=1000), \
                patch("router.capacity.time.monotonic", side_effect=[10, 12]):
            sampler = CapacitySampler(Path(self.temp.name))
            monotonic = sampler.view(CapacityThresholds())
        self.assertEqual(monotonic["sample_age_ms"], 2000)

        sampler._latest = {
            "logical_cpus": 2, "load_1m": 0, "load_5m": 0, "cpu_busy_pct": 1,
            "memory_total_bytes": 16, "memory_available_bytes": 8,
            "swap_used_bytes": 0, "disk_available_bytes": 20,
            "observed_at": 1_000_000, "partial": False,
        }
        sampler._latest_observed_at = None
        with patch("router.capacity.time.time", return_value=500):
            rolled_back = sampler.view(CapacityThresholds())
        self.assertTrue(rolled_back["partial"])
        self.assertEqual(rolled_back["capacity_state"], "unknown")
        self.assertIn("clock moved backwards", rolled_back["capacity_reasons"][0])

    def test_session_wall_clock_rollback_makes_exact_active_total_unavailable(self):
        self.event("session_start", observed_at=self.now)
        with self.store.connect() as db:
            record = json.loads(db.execute("SELECT data FROM sessions").fetchone()[0])
        rolled_back = self.observer.view(record, self.now - 1)
        self.assertEqual(rolled_back["status"], "unknown")
        self.assertIn("clock moved backwards", rolled_back["visibility_reason"])
        with patch("router.sessions.now_ms", return_value=self.now - 1):
            self.assertIsNone(self.observer.active_count())

    def test_active_reservation_prevents_available_capacity_and_malformed_record_is_partial(self):
        reservation = Path(self.temp.name) / "reservation.json"
        reservation.write_text(json.dumps({
            "schema_version": 1, "reserved_by": "coordinator", "reserved_until": self.now + 60_000,
            "reserved_cpu_slots": 2, "reserved_memory_bytes": 1024,
            "reservation_source": "scheduler",
        }))
        sample = {"logical_cpus": 8, "load_1m": 1, "cpu_busy_pct": 20,
                  "memory_available_bytes": 8 * 1024**3, "disk_available_bytes": 20 * 1024**3,
                  "sample_age_ms": 1, "reservation_active": True, "reserved_by": "coordinator",
                  "reserved_until": self.now + 60_000, "reserved_cpu_slots": 2,
                  "reserved_memory_bytes": 1024, "reservation_source": "scheduler"}
        state, reasons = classify(sample, CapacityThresholds())
        self.assertEqual(state, "busy")
        self.assertTrue(any("reserved by coordinator" in reason for reason in reasons))
        with patch("router.capacity._raw_cpu_ticks", side_effect=[(100, 50), (120, 55)]), \
                patch("router.capacity._raw_memory", return_value=(16, 8, 0)), \
                patch("router.capacity.shutil.disk_usage", return_value=type("Usage", (), {"free": 20})()), \
                patch("router.capacity.os.getloadavg", return_value=(0, 0, 0)), \
                patch("router.capacity.os.cpu_count", return_value=2), \
                patch("router.capacity.time.time", return_value=self.now / 1000):
            sampler = CapacitySampler(Path(self.temp.name), reservation_path=reservation)
            sampler.sample()
            view = sampler.view(CapacityThresholds())
            self.assertTrue(view["reservation_active"])
            self.assertEqual(view["capacity_state"], "saturated")
            with patch("router.capacity.time.time", return_value=(self.now + 120_000) / 1000):
                as_of_snapshot = sampler.view(CapacityThresholds(), as_of_ms=self.now)
            self.assertTrue(as_of_snapshot["reservation_active"])
            reservation.write_text("not-json")
            partial = sampler.view(CapacityThresholds())
            self.assertTrue(partial["partial"])
            self.assertEqual(partial["capacity_state"], "unknown")
            with patch.object(
                    sampler.reservations, "read",
                    side_effect=ValueError("reservation \x1b[31mbad\x1b[0m field")):
                reservation_error = sampler.view(CapacityThresholds())
            self.assertEqual(reservation_error["error"], "reservation bad field")
            reservation.unlink()
            sampler._latest = {"observed_at": self.now, "partial": True, "error": "sampler " + "e" * 600}
            sample_error = sampler.view(CapacityThresholds())
            self.assertEqual(sample_error["error"], ("sampler " + "e" * 600)[:500])

    def test_snapshot_reservation_state_uses_one_generated_at_boundary(self):
        observed = {}

        class Host:
            def view(self, at): return {"host_id": "host", "observed_at": at}
        class Sessions:
            def fleet_records(self, limit): return [], 0
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit): return [], 0
        class Capacity:
            def view(self, thresholds, as_of_ms=None, **counts):
                observed["as_of_ms"] = as_of_ms
                return {"reserved_by": "scheduler", "reserved_until": 1001,
                        "reserved_cpu_slots": 1, "reserved_memory_bytes": 0,
                        "reservation_source": "test", "reservation_active": False,
                        **counts}

        controller = type("Controller", (), {
            "host": Host(), "sessions": Sessions(), "native": Native(), "capacity": Capacity(),
        })()
        with patch("router.fleet_snapshot.now_ms", return_value=1000):
            result = compose(controller)
        self.assertIsNone(observed["as_of_ms"])
        self.assertEqual(result["generated_at"], 1000)
        self.assertTrue(result["capacity"]["reservation_active"])

    def test_memory_reservation_threshold_expiry_and_read_bound(self):
        raw_sample = {"logical_cpus": 8, "load_1m": 1, "cpu_busy_pct": 20,
                      "memory_available_bytes": 8 * 1024**3,
                      "disk_available_bytes": 20 * 1024**3, "sample_age_ms": 1}
        sample = {**raw_sample, "reservation_active": True, "reserved_by": "coordinator",
                  "reserved_until": self.now + 60_000, "reserved_cpu_slots": 0,
                  "reserved_memory_bytes": 5 * 1024**3, "reservation_source": "scheduler"}
        state, reasons = classify(sample, CapacityThresholds())
        self.assertEqual(state, "busy")
        self.assertTrue(any("available memory 3.0 GiB" in reason for reason in reasons))

        reservation = Path(self.temp.name) / "reservation.json"
        reservation.write_text(json.dumps({
            "schema_version": 1, "reserved_by": "coordinator",
            "reserved_until": self.now - 1, "reserved_cpu_slots": 0,
            "reserved_memory_bytes": 5 * 1024**3, "reservation_source": "scheduler",
        }))
        expired_reservation = ReservationReader(reservation).read(self.now)
        self.assertFalse(expired_reservation["reservation_active"])
        expired, expired_reasons = classify(
            {**raw_sample, **expired_reservation}, CapacityThresholds()
        )
        self.assertEqual((expired, expired_reasons), ("available", []))

        opened = mock_open(read_data=b"x" * (MAX_RESERVATION_BYTES + 1))
        reader = ReservationReader(reservation)
        with patch("pathlib.Path.open", opened), self.assertRaisesRegex(ValueError, "exceeds 16 KiB"):
            reader.read(self.now)
        opened().read.assert_called_once_with(MAX_RESERVATION_BYTES + 1)

    def test_persisted_monitoring_integers_match_the_wire_bound(self):
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.event("session_start", observed_at=2**63)
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.event("session_start", observed_at=self.now + 120_000)

        with self.assertRaisesRegex(ValueError, "invalid session_id"):
            self.event("session_start", session_id="abc\u202edef")
        with self.assertRaisesRegex(ValueError, "invalid task_id"):
            self.event("task_created", task_id="task\u202eid")
        with self.assertRaisesRegex(ValueError, "invalid tool_use_id"):
            self.event("tool_start", tool_use_id="tool\u202eid", activity="Bash")

        reservation = Path(self.temp.name) / "reservation.json"
        reservation.write_text(json.dumps({
            "schema_version": 1, "reserved_by": "coordinator",
            "reserved_until": 2**63, "reserved_cpu_slots": 0,
            "reserved_memory_bytes": 0, "reservation_source": "scheduler",
        }))
        with self.assertRaisesRegex(ValueError, "64-bit integer"):
            ReservationReader(reservation).read(self.now)
        sampler = CapacitySampler(Path(self.temp.name), reservation_path=reservation)
        projected = sampler.view(CapacityThresholds())
        self.assertTrue(projected["partial"])
        self.assertEqual(projected["capacity_state"], "unknown")

    def test_darwin_available_memory_counts_speculative_pages_once(self):
        gib = 1024**3

        class Library:
            def mach_host_self(self):
                return 1
            def host_page_size(self, host, page_size):
                page_size._obj.value = 16384
                return 0
            def host_statistics64(self, host, flavor, statistics, count):
                pages = statistics._obj
                # Kernel free_count already includes the speculative pages.
                pages.free_count, pages.speculative_count = 3 * gib // 16384, gib * 3 // 2 // 16384
                pages.inactive_count = gib // 2 // 16384
                return 0
            def sysctlbyname(self, name, swap, size, new, new_size):
                return 0

        with patch("router.capacity._darwin_library", return_value=Library()), \
                patch("router.capacity.os.sysconf", return_value=16 * gib // 16384):
            total, available, _ = _darwin_memory()
        self.assertEqual(total, 16 * gib)
        self.assertEqual(available, 3 * gib + gib // 2)

    def test_deeply_nested_reservation_reports_unknown_capacity(self):
        reservation = Path(self.temp.name) / "reservation.json"
        reservation.write_text("[[]]")
        # CPython 3.14 parses 16 KiB of nesting, but a smaller stack may not.
        with patch("router.capacity.json.loads", side_effect=RecursionError):
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                ReservationReader(reservation).read(self.now)
            projected = CapacitySampler(Path(self.temp.name), reservation_path=reservation).view(CapacityThresholds())
        self.assertTrue(projected["partial"])
        self.assertEqual(projected["capacity_state"], "unknown")

    def test_reservation_schema_version_requires_the_integer_one(self):
        reservation = Path(self.temp.name) / "reservation.json"
        base = {
            "reserved_by": "coordinator", "reserved_until": self.now + 1,
            "reserved_cpu_slots": 0, "reserved_memory_bytes": 0,
            "reservation_source": "scheduler",
        }
        for schema_version in (True, 1.0, "1", 2, None):
            reservation.write_text(json.dumps({"schema_version": schema_version, **base}))
            with self.subTest(schema_version=schema_version), self.assertRaisesRegex(
                    ValueError, "schema_version 1"):
                ReservationReader(reservation).read(self.now)


class SnapshotAndFleetTests(unittest.TestCase):
    def test_capacity_semantics_reject_every_incomplete_or_contradictory_variant(self):
        for field in ("load_5m", "memory_total_bytes", "swap_used_bytes", "observed_at"):
            value = snapshot()
            value["capacity"].pop(field)
            with self.subTest(missing=field):
                projected = _validate_snapshot(json.dumps(value).encode())
                self.assertTrue(projected["partial"])
                self.assertEqual(projected["capacity"]["capacity_state"], "unknown")

        contradictory_age = snapshot()
        contradictory_age["generated_at"] = 100_000
        contradictory_age["host"]["observed_at"] = 100_000
        contradictory_age["capacity"].update(observed_at=1, sample_age_ms=1)
        projected = _validate_snapshot(json.dumps(contradictory_age).encode())
        self.assertEqual(projected["capacity"]["capacity_state"], "unknown")
        self.assertIn("contradicts", " ".join(projected["capacity"]["capacity_reasons"]))

        contradictory_memory = snapshot()
        contradictory_memory["capacity"]["memory_total_bytes"] = 1
        projected = _validate_snapshot(json.dumps(contradictory_memory).encode())
        self.assertEqual(projected["capacity"]["capacity_state"], "unknown")
        self.assertIn("exceeds total", " ".join(projected["capacity"]["capacity_reasons"]))

        source_unknown = snapshot()
        source_unknown["sessions"] = []
        source_unknown["agents"] = []
        source_unknown["capacity"].update(
            active_claude_sessions=0, active_agents=0,
            capacity_state="unknown", capacity_reasons=["sensor uncertainty"],
        )
        result = query_fleet(
            FleetConfig((FleetHost("local", "local"),)),
            local_snapshot=lambda thresholds: source_unknown,
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["hosts"][0]["snapshot"]["capacity"]["capacity_state"], "unknown")
        self.assertEqual(
            result["hosts"][0]["snapshot"]["capacity"]["capacity_reasons"],
            ["sensor uncertainty"],
        )

        for section in ("sessions", "agents", "managed_processes"):
            value = snapshot()
            value.update(partial=True, errors={section: "collection failed"})
            with self.subTest(failed_section=section):
                result = query_fleet(
                    FleetConfig((FleetHost("local", "local"),)),
                    local_snapshot=lambda thresholds, value=value: value,
                )
                capacity = result["hosts"][0]["snapshot"]["capacity"]
                self.assertEqual(capacity["capacity_state"], "busy")
                self.assertNotIn("partial", capacity)

        for section in (
            "session_active_count", "agent_active_count", "managed_process_active_count",
            "lifecycle", "lifecycle_consistency",
        ):
            value = snapshot()
            value.update(partial=True, errors={section: "active count unavailable"})
            with self.subTest(failed_exact_count=section):
                projected = _validate_snapshot(json.dumps(value).encode())
                self.assertEqual(projected["capacity"]["capacity_state"], "unknown")
                self.assertTrue(projected["capacity"]["partial"])

        for field, mutate in (
            ("active_claude_sessions", lambda value: value["capacity"].update(active_claude_sessions=0)),
            ("active_agents", lambda value: value["capacity"].update(active_agents=0)),
            ("active_managed_processes", lambda value: (
                value["managed_processes"].append({
                    "host_id": "host-1", "managed_id": "managed", "status": "running",
                }),
                value["capacity"].update(active_managed_processes=0),
            )),
        ):
            value = snapshot()
            mutate(value)
            with self.subTest(contradicted_total=field):
                projected = _validate_snapshot(json.dumps(value).encode())
                self.assertEqual(projected["capacity"]["capacity_state"], "unknown")
                self.assertIn(field, " ".join(projected["capacity"]["capacity_reasons"]))

    def test_fleet_latency_preserves_integer_capacity_schema(self):
        result = query_fleet(
            FleetConfig((FleetHost("local", "local"),)),
            local_snapshot=lambda thresholds: snapshot(),
        )
        nested = result["hosts"][0]["snapshot"]
        self.assertIsInstance(nested["capacity"]["sample_age_ms"], int)
        self.assertIsInstance(
            _validate_snapshot(json.dumps(nested).encode())["capacity"]["sample_age_ms"], int,
        )

    def test_remote_snapshot_validator_rejects_malformed_wire_shapes(self):
        cases = []

        def encoded(mutator):
            value = snapshot()
            mutator(value)
            return json.dumps(value, separators=(",", ":")).encode()

        cases.extend([
            b"{",
            encoded(lambda value: value.update(schema_version=2)),
            encoded(lambda value: value.update(generated_at="now")),
            encoded(lambda value: value.update(sessions={})),
            encoded(lambda value: value["sessions"][0].pop("source")),
            encoded(lambda value: value["sessions"][0].pop("status")),
            encoded(lambda value: value["sessions"][0].update(status="banana")),
            encoded(lambda value: value["agents"][0].pop("last_event_at")),
            encoded(lambda value: value["agents"][0].update(status="banana")),
            encoded(lambda value: value.update(tasks=[{
                "host_id": "host-1", "source": "claude", "session_id": "same",
                "task_id": "task", "status": "banana", "created_at": 0,
                "last_event_at": 0,
            }])),
            encoded(lambda value: value["host"].pop("boot_id")),
            encoded(lambda value: value.update(sessions=[{}] * (MAX_COLLECTION_RECORDS + 1))),
            encoded(lambda value: value["capacity"].update(reserved_until=True)),
            encoded(lambda value: value.update(alerts=[{"kind": "memory_low"}])),
        ])
        for payload in cases:
            with self.subTest(payload=payload[:80]), self.assertRaises(ValueError):
                _validate_snapshot(payload)

    def test_remote_projection_rejects_nonfinite_json_and_drops_hostile_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "invalid snapshot JSON"):
            _validate_snapshot(json.dumps({**snapshot(), "generated_at": float("nan")}).encode())
        hostile = snapshot()
        hostile.update(prompt="PRIVATE", transcript="PRIVATE", environment={"TOKEN": "PRIVATE"},
                       messages=["PRIVATE"], future_schema_field="drop-me",
                       errors={"capacity": "status=failed apiKey=example next=ok",
                               "unknown": "PRIVATE"})
        hostile["sessions"][0].update(prompt="PRIVATE", messages=["PRIVATE"], unknown="drop-me")
        hostile["agents"][0].update(transcript="PRIVATE", environment={"TOKEN": "PRIVATE"})
        hostile["capacity"]["future"] = "drop-me"
        projected = _validate_snapshot(json.dumps(hostile).encode())
        serialized = json.dumps(projected)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("drop-me", serialized)
        # Known fields are projected; their text is kept as written.
        self.assertIn("status=failed apiKey=example next=ok", projected["errors"]["capacity"])
        self.assertTrue(projected["partial"])
        self.assertIn("unsupported field", projected["errors"]["projection"])
        self.assertIn("unsupported field", projected["errors"]["sessions"])
        self.assertIn("unsupported field", projected["errors"]["agents"])
        self.assertEqual(projected["capacity"]["capacity_state"], "unknown")

        display_only = snapshot()
        display_only["sessions"][0]["unknown_display_field"] = "omitted"
        display_projection = _validate_snapshot(json.dumps(display_only).encode())
        self.assertTrue(display_projection["partial"])
        self.assertFalse(display_projection["capacity"].get("partial", False))
        fleet_projection = query_fleet(
            FleetConfig((FleetHost("local", "local"),)),
            local_snapshot=lambda thresholds: display_only,
        )
        self.assertEqual(
            fleet_projection["hosts"][0]["snapshot"]["capacity"]["capacity_state"],
            "busy",
        )
        unsafe_identity = snapshot()
        unsafe_identity["sessions"][0]["session_id"] = "id\x1b[2J"
        with self.assertRaisesRegex(ValueError, "unsafe sessions session_id"):
            _validate_snapshot(json.dumps(unsafe_identity).encode())

    def test_remote_projection_strips_display_controls_rejects_unsafe_identities_and_bounds_usage(self):
        secret = "build \x1b[31mred\x1b]0;title\x07 done"
        value = snapshot()
        for key in ("hostname", "boot_id", "platform", "architecture", "router_version", "claude_version"):
            value["host"][key] = secret
        value["sessions"][0].update(cwd=secret, model=secret, activity=secret,
                                    visibility_reason=secret)
        value["agents"][0].update(task=secret, agent_type=secret, requested_model=secret,
                                  resolved_model=secret, models=[secret], activity=secret,
                                  cwd=secret, worktree=secret, cost_note=secret,
                                  visibility_reason=secret)
        value["tasks"] = [{"host_id": "host-1", "source": "claude", "session_id": "same",
                           "task_id": "safe", "status": "created", "subject": secret,
                           "teammate_name": secret, "created_at": 0, "last_event_at": 0}]
        value["capacity"].update(capacity_reasons=[secret], error=secret)
        projected = _validate_snapshot(json.dumps(value).encode())
        self.assertNotIn("\\u001b", json.dumps(projected))
        self.assertEqual(projected["agents"][0]["task"], "build red done")

        identity_cases = {
            "host host_id": lambda item: (
                item["host"].update(host_id="host\x1b[2J"),
                [record.update(host_id="host\x1b[2J")
                 for collection in ("sessions", "agents", "tasks", "managed_processes", "inferred_processes")
                 for record in item[collection]],
            ),
            "sessions parent_session_id": lambda item: item["sessions"][0].update(
                parent_session_id="id\x07"),
            "sessions line-separated session_id": lambda item: item["sessions"][0].update(
                session_id="safe\u2028spoofed"),
            "sessions surrogate session_id": lambda item: item["sessions"][0].update(
                session_id="safe\ud800spoofed"),
            "agents parent_agent_id": lambda item: item["agents"][0].update(
                parent_agent_id="id\x07"),
            "tasks task_id": lambda item: item.update(tasks=[{
                "host_id": "host-1", "source": "claude", "session_id": "same",
                "task_id": "task\x1b]0;x\x07", "status": "created",
                "created_at": 0, "last_event_at": 0,
            }]),
        }
        for name, mutate in identity_cases.items():
            unsafe = snapshot()
            mutate(unsafe)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe"):
                _validate_snapshot(json.dumps(unsafe).encode())

        # Native usage and cost moved to Langfuse: an older peer still sending them is
        # projected without them and is not marked partial for that reason.
        older_peer = snapshot()
        older_peer["agents"][0].update(usage={"input_tokens": 1}, cost=.5, cost_source="transcript",
                                       models=["claude-opus"], usage_partial=False, worktree="/w",
                                       resolved_model="from-transcript", model_source="transcript")
        projected = _validate_snapshot(json.dumps(older_peer).encode())
        agent = projected["agents"][0]
        self.assertFalse({"usage", "cost", "cost_source", "models", "usage_partial", "worktree"} & agent.keys())
        self.assertIsNone(agent["resolved_model"])
        self.assertFalse(projected["partial"])
        host_reported = snapshot()
        host_reported["agents"][0].update(resolved_model="claude-opus", model_source="host_result")
        self.assertEqual(_validate_snapshot(json.dumps(host_reported).encode())["agents"][0]["resolved_model"],
                         "claude-opus")
        unknown = snapshot()
        unknown["agents"][0]["future_field"] = 1
        self.assertTrue(_validate_snapshot(json.dumps(unknown).encode())["partial"])


    def test_projection_preserves_producer_fields_and_reports_safe_transformations(self):
        value = snapshot()
        value["tasks"] = [{
            "host_id": "host-1", "source": "claude", "session_id": "same",
            "task_id": "task", "status": "created", "subject": "x" * 501,
            "created_at": 0, "last_event_at": 0,
        }]
        value["agents"][0].update(activity="Edit \x1b[31mnow")
        projected = _validate_snapshot(json.dumps(value).encode())
        self.assertEqual(len(projected["tasks"][0]["subject"]), 500)
        self.assertEqual(projected["agents"][0]["activity"], "Edit now")
        self.assertTrue(projected["partial"])
        self.assertIn("changed for output safety", projected["errors"]["tasks"])
        self.assertIn("changed for output safety", projected["errors"]["agents"])

    def test_wire_numeric_and_identity_domains_are_field_specific(self):
        cases = {
            "negative timestamp": lambda value: value["sessions"][0].update(started_at=-1),
            "boolean duration": lambda value: value["agents"][0].update(duration_ms=True),
            "zero pid": lambda value: value.update(managed_processes=[{
                "host_id": "host-1", "managed_id": "managed", "pid": 0,
            }]),
            "negative pgid": lambda value: value.update(inferred_processes=[{
                "host_id": "host-1", "pid": 1, "pgid": -1,
            }]),
            "negative count": lambda value: value["sessions"][0].update(inflight_tool_count=-1),
        }
        for name, mutate in cases.items():
            value = snapshot()
            mutate(value)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "invalid"):
                _validate_snapshot(json.dumps(value).encode())

        signed_exit = snapshot()
        signed_exit["managed_processes"] = [{
            "host_id": "host-1", "managed_id": "managed", "pid": 1, "exit_code": -9,
        }]
        self.assertEqual(
            _validate_snapshot(json.dumps(signed_exit).encode())["managed_processes"][0]["exit_code"],
            -9,
        )

        for identity in ("unsafe\nidentity", "unsafe\x85identity", "unsafe\u202eidentity", "unsafe\x1b[2J"):
            value = snapshot()
            value["sessions"][0]["session_id"] = identity
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "unsafe"):
                _validate_snapshot(json.dumps(value).encode())

    def test_capacity_and_process_errors_remain_partial_and_unknown(self):
        value = snapshot()
        value["capacity"].update(future_metric=1, capacity_state="available")
        value["errors"] = {
            "capacity": "sampler failed",
            "managed_processes": "manager unavailable",
            "inferred_processes": "process scan unavailable",
        }
        projected = _validate_snapshot(json.dumps(value).encode())
        self.assertTrue(projected["partial"])
        self.assertTrue(projected["capacity"]["partial"])
        self.assertEqual(projected["capacity"]["capacity_state"], "unknown")
        self.assertIn("unsupported field", projected["errors"]["capacity"])
        self.assertIn("managed_processes", projected["errors"])
        self.assertIn("inferred_processes", projected["errors"])

        result = query_fleet(
            FleetConfig((FleetHost("local", "local"),)),
            local_snapshot=lambda thresholds: value,
        )
        self.assertEqual(
            result["hosts"][0]["snapshot"]["capacity"]["capacity_state"],
            "unknown",
        )

    def test_unknown_error_sections_are_visible_and_oversized_lists_are_rejected(self):
        value = snapshot()
        value["errors"] = {"future-secret-section": "password=swordfish"}
        projected = _validate_snapshot(json.dumps(value).encode())
        self.assertTrue(projected["partial"])
        self.assertEqual(projected["errors"]["other"],
                         "1 unrecognized error section(s) omitted")
        self.assertNotIn("swordfish", json.dumps(projected))

        too_many_reasons = snapshot()
        too_many_reasons["capacity"]["capacity_reasons"] = ["reason"] * 33
        with self.assertRaisesRegex(ValueError, "capacity_reasons"):
            _validate_snapshot(json.dumps(too_many_reasons).encode())

    def test_fleet_identity_encoding_is_unambiguous_for_delimiters_and_unicode(self):
        first = snapshot("host:a")
        first["sessions"][0].update(source="b", session_id="c:雪")
        second = snapshot("host")
        second["sessions"][0].update(source="a:b", session_id="c:雪")
        first_id = _prefix_identities(first)["sessions"][0]["fleet_id"]
        second_id = _prefix_identities(second)["sessions"][0]["fleet_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(json.loads(first_id), ["host:a", "b", "c:雪"])

    def test_local_composed_snapshot_round_trips_through_wire_projection(self):
        value = snapshot()

        class Host:
            def view(self, at): return {**value["host"], "observed_at": at}
        class Sessions:
            def fleet_records(self, limit): return value["sessions"], 1
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit): return value["agents"], 1
        class Capacity:
            def view(self, thresholds, **counts):
                return {**value["capacity"], **counts, "partial": False,
                        "capacity_state": "busy", "capacity_reasons": ["1 active Claude sessions"]}

        local = compose(type("Controller", (), {"host": Host(), "sessions": Sessions(),
                                                  "native": Native(), "capacity": Capacity()})())
        projected = _validate_snapshot(json.dumps(local).encode())
        self.assertEqual(projected["host"]["host_id"], "host-1")
        self.assertEqual(projected["capacity"]["active_claude_sessions"], 1)

    def test_duplicate_reported_host_identity_is_partial_without_fleet_ids(self):
        config = FleetConfig((FleetHost("one", "local"), FleetHost("two", "ssh", target="two")))
        with patch("router.fleet.ssh_snapshot", return_value=snapshot("duplicate")):
            result = query_fleet(config, local_snapshot=lambda thresholds: snapshot("duplicate"))
        self.assertTrue(result["partial"])
        for record in result["hosts"]:
            self.assertIn("host_identity", record["snapshot"]["errors"])
            self.assertNotIn("fleet_id", record["snapshot"]["sessions"][0])
            self.assertIn("duplicate_host_identity", {alert["kind"] for alert in record["alerts"]})

    def test_invalid_peer_snapshot_does_not_hide_healthy_peer(self):
        config = FleetConfig((FleetHost("invalid", "local"),
                              FleetHost("healthy", "ssh", target="healthy")))
        invalid = snapshot("invalid")
        invalid["capacity"]["reserved_memory_bytes"] = "lots"
        with patch("router.fleet.ssh_snapshot", return_value=snapshot("healthy")):
            result = query_fleet(config, local_snapshot=lambda thresholds: invalid)
        self.assertFalse(result["hosts"][0]["ok"])
        self.assertTrue(result["hosts"][1]["ok"])
        self.assertEqual(result["hosts"][1]["snapshot"]["host"]["host_id"], "healthy")

    def test_non_boolean_capacity_partial_is_rejected_and_peer_isolated(self):
        invalid = snapshot("invalid")
        invalid["capacity"]["partial"] = "true"
        payload = json.dumps(invalid, separators=(",", ":")).encode()
        with self.assertRaisesRegex(ValueError, "invalid capacity partial flag"):
            _validate_snapshot(payload)

        config = FleetConfig((FleetHost("invalid", "local"),
                              FleetHost("healthy", "ssh", target="healthy")))
        with patch("router.fleet.ssh_snapshot", return_value=snapshot("healthy")):
            result = query_fleet(config, local_snapshot=lambda thresholds: invalid)
        self.assertFalse(result["hosts"][0]["ok"])
        self.assertTrue(result["hosts"][1]["ok"])
        self.assertEqual(result["hosts"][1]["snapshot"]["capacity"]["capacity_state"],
                         "busy")

    def test_oversized_logical_cpu_count_is_rejected_and_peer_isolated(self):
        invalid = snapshot("invalid")
        invalid["capacity"].update(logical_cpus=10**400, load_1m=1.0)
        payload = json.dumps(invalid, separators=(",", ":")).encode()
        with self.assertRaisesRegex(ValueError, "positive finite integer"):
            _validate_snapshot(payload)

        config = FleetConfig((FleetHost("invalid", "local"),
                              FleetHost("healthy", "ssh", target="healthy")))
        with patch("router.fleet.ssh_snapshot", return_value=snapshot("healthy")):
            result = query_fleet(config, local_snapshot=lambda thresholds: invalid)
        self.assertFalse(result["hosts"][0]["ok"])
        self.assertTrue(result["hosts"][1]["ok"])
        self.assertEqual(result["hosts"][1]["snapshot"]["capacity"]["capacity_state"],
                         "busy")

    def test_malformed_remote_alert_isolated_from_healthy_peer(self):
        config = FleetConfig((FleetHost("local", "local"),
                              FleetHost("invalid", "ssh", target="invalid")))
        invalid = snapshot("invalid")
        invalid["alerts"] = [{"kind": "memory_low"}]
        history = AlertHistory()
        with patch("router.fleet.ssh_snapshot", side_effect=lambda host, timeout: _validate_snapshot(
                json.dumps(invalid, separators=(",", ":")).encode())):
            result = query_fleet(
                config,
                local_snapshot=lambda thresholds: snapshot("healthy"),
                alert_history=history,
            )
        self.assertTrue(result["hosts"][0]["ok"])
        self.assertFalse(result["hosts"][1]["ok"])
        self.assertTrue(result["partial"])

    def test_snapshot_section_failure_is_partial_and_output_is_bounded(self):
        class Host:
            host_id = "host"
            def view(self, at): return {"host_id": "host", "observed_at": at}
        class Sessions:
            def sessions(self, limit): return []
            def tasks(self, limit): return []
            def active_count(self): return 0
            def fleet_records(self, limit): return self.sessions(limit), self.active_count()
        class Native:
            def snapshot(self, limit): raise OSError("process data unavailable")
            def active_count(self): return 0
            def fleet_records(self, limit): return self.snapshot(limit), self.active_count()
        class Capacity:
            def view(self, thresholds, **counts):
                return {"capacity_state": "unknown", "capacity_reasons": ["missing"], **counts}
        controller = type("Controller", (), {"host": Host(), "sessions": Sessions(),
                                               "native": Native(), "capacity": Capacity()})()
        result = compose(controller)
        self.assertTrue(result["partial"])
        self.assertEqual(result["agents"], [])
        self.assertIn("process data unavailable", result["errors"]["agents"])
        oversized = snapshot()
        oversized["agents"] = [{"agent_id": str(number), "activity": "x" * 20_000} for number in range(100)]
        limited = bound(oversized)
        self.assertLessEqual(len(json.dumps(limited, separators=(",", ":")).encode()), MAX_SNAPSHOT_BYTES)
        self.assertTrue(limited["partial"])

    def test_nested_capacity_failure_propagates_partial_error(self):
        class Host:
            def view(self, at): return {"host_id": "host", "router_version": "0.4.0", "observed_at": at}
        class Sessions:
            def sessions(self, limit): return []
            def tasks(self, limit): return []
            def active_count(self): return 0
            def fleet_records(self, limit): return self.sessions(limit), self.active_count()
        class Native:
            def snapshot(self, limit): return []
            def active_count(self): return 0
            def fleet_records(self, limit): return self.snapshot(limit), self.active_count()
        class Capacity:
            def view(self, thresholds, **counts):
                return {"partial": True, "error": "sqlite read failed", **counts}
        controller = type("Controller", (), {"host": Host(), "sessions": Sessions(),
                                               "native": Native(), "capacity": Capacity()})()
        result = compose(controller)
        self.assertTrue(result["partial"])
        self.assertEqual(result["errors"]["capacity"], "sqlite read failed")

    def test_collection_failure_cannot_be_reclassified_as_available(self):
        class Host:
            def view(self, at):
                return {"host_id": "host", "observed_at": at}

        class Sessions:
            def fleet_records(self, limit):
                return [], 0

            def tasks(self, limit):
                return []

        class Native:
            def fleet_records(self, limit):
                raise OSError("native lifecycle unavailable")

        with tempfile.TemporaryDirectory() as temp, \
                patch("router.capacity._raw_cpu_ticks", side_effect=[(100, 50), (120, 55)]), \
                patch("router.capacity._raw_memory", return_value=(16 * 1024**3, 8 * 1024**3, 0)), \
                patch("router.capacity.shutil.disk_usage",
                      return_value=type("Usage", (), {"free": 20 * 1024**3})()), \
                patch("router.capacity.os.getloadavg", return_value=(1, 1, 1)), \
                patch("router.capacity.os.cpu_count", return_value=8):
            capacity = CapacitySampler(Path(temp))
            capacity.sample()
            result = compose(type("Controller", (), {
                "host": Host(), "sessions": Sessions(), "native": Native(),
                "capacity": capacity,
            })())

        self.assertTrue(result["partial"])
        self.assertIn("native lifecycle unavailable", result["errors"]["agents"])
        self.assertIsNone(result["capacity"]["active_agents"])
        self.assertEqual(result["capacity"]["capacity_state"], "unknown")
        self.assertIn("active_agents", result["capacity"]["capacity_reasons"][0])

    def test_lifecycle_uncertainty_invalidates_local_capacity_before_transport(self):
        value = snapshot()

        class Host:
            def view(self, at): return {**value["host"], "observed_at": at}
        class Sessions:
            def fleet_records(self, limit): return [], 0
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit): return [], 0
        class Capacity:
            def view(self, thresholds, **counts):
                return {**value["capacity"], **counts, "capacity_state": "available",
                        "capacity_reasons": []}
        class Controller:
            host = Host()
            sessions = Sessions()
            native = Native()
            capacity = Capacity()

            @staticmethod
            def lifecycle_health():
                return "accepted writes pending"

        result = compose(Controller())
        self.assertTrue(result["partial"])
        self.assertEqual(result["capacity"]["capacity_state"], "unknown")
        self.assertIn("lifecycle", " ".join(result["capacity"]["capacity_reasons"]))

    def test_independent_active_counts_and_needs_input_alerts(self):
        class Host:
            def view(self, at): return {"host_id": "host", "router_version": "0.4.0", "observed_at": at}
        class Sessions:
            def sessions(self, limit):
                return [{"source": "claude", "session_id": "S", "status": "idle", "needs_input": True,
                         "last_event_at": 1, "visibility_reason": "waiting"}]
            def tasks(self, limit): return []
            def active_count(self): return 3
            def fleet_records(self, limit): return self.sessions(limit), self.active_count()
        class Native:
            def snapshot(self, limit):
                return [{"source": "claude", "parent_session_id": "S", "agent_id": "A",
                         "status": "needs_input", "last_event_at": 1}]
            def active_count(self): return 4
            def fleet_records(self, limit): return self.snapshot(limit), self.active_count()
        class Capacity:
            def view(self, thresholds, **counts):
                return {"partial": False, "capacity_state": "unknown", "capacity_reasons": [], **counts}
        result = compose(type("Controller", (), {"host": Host(), "sessions": Sessions(),
                                                   "native": Native(), "capacity": Capacity()})())
        self.assertEqual(result["capacity"]["active_claude_sessions"], 3)
        self.assertEqual(result["capacity"]["active_agents"], 4)
        self.assertEqual(sum(alert["kind"] == "agent_needs_input" for alert in result["alerts"]), 2)

    def test_alert_truncation_is_explicit_and_output_remains_bounded(self):
        class Host:
            def view(self, at): return {"host_id": "host", "observed_at": at}
        class Sessions:
            def fleet_records(self, limit):
                records = [
                    {"source": "claude", "session_id": f"S-{number}", "status": "idle",
                     "needs_input": True, "last_event_at": number}
                    for number in range(60)
                ]
                return records[:limit], len(records)
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit):
                records = [
                    {"source": "claude", "parent_session_id": f"S-{number}",
                     "agent_id": f"A-{number}", "status": "needs_input",
                     "last_event_at": number}
                    for number in range(60)
                ]
                return records[:limit], len(records)
        class Capacity:
            def view(self, thresholds, **counts):
                return {"partial": False, "capacity_state": "available",
                        "capacity_reasons": [], **counts}

        result = compose(type("Controller", (), {
            "host": Host(), "sessions": Sessions(), "native": Native(),
            "capacity": Capacity(),
        })())

        self.assertEqual(len(result["alerts"]), 100)
        self.assertEqual(len({alert["alert_id"] for alert in result["alerts"]}), 100)
        self.assertTrue(result["partial"])
        self.assertEqual(result["errors"]["alerts"],
                         "Collection truncated to alert_limit=100")
        self.assertLessEqual(
            len(json.dumps(result, separators=(",", ":")).encode()),
            MAX_SNAPSHOT_BYTES,
        )

    def test_active_counts_ignore_display_limits_and_truncation_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "router.sqlite3", "host")
            sessions = SessionObserver(store, "host")
            native = NativeObserver(store, host_id="host")
            observed_at = int(time.time() * 1000)
            for number in range(2):
                session_id = f"S-{number}"
                sessions.observe({"source": "claude", "session_id": session_id,
                                  "kind": "session_start", "observed_at": observed_at})
                sessions.observe({"source": "claude", "session_id": session_id,
                                  "kind": "task_created", "task_id": f"T-{number}",
                                  "task_subject": f"Task {number}", "observed_at": observed_at})
                native.observe({"source": "claude", "session_id": session_id,
                                "agent_id": f"A-{number}", "kind": "start",
                                "observed_at": observed_at})

            class Host:
                def view(self, at): return {"host_id": "host", "observed_at": at}

            class Capacity:
                def view(self, thresholds, **counts):
                    return {
                        "logical_cpus": 8, "load_1m": 1, "load_5m": 1,
                        "cpu_busy_pct": 20, "memory_total_bytes": 32 * 1024**3,
                        "memory_available_bytes": 16 * 1024**3,
                        "swap_used_bytes": 0, "disk_available_bytes": 20 * 1024**3,
                        "observed_at": 1000, "sample_age_ms": 0, "partial": False,
                        **counts,
                    }

            controller = type("Controller", (), {
                "host": Host(), "sessions": sessions, "native": native, "capacity": Capacity(),
            })()
            with patch("router.fleet_snapshot.now_ms", return_value=1000):
                result = compose(controller, history_limit=1)

        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(len(result["agents"]), 1)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["capacity"]["active_claude_sessions"], 2)
        self.assertEqual(result["capacity"]["active_agents"], 2)
        self.assertEqual(result["capacity"]["capacity_state"], "busy")
        self.assertFalse(result["capacity"]["partial"])
        self.assertTrue(result["partial"])
        self.assertIn("truncated", result["errors"]["sessions"])
        self.assertIn("truncated", result["errors"]["agents"])
        self.assertIn("truncated", result["errors"]["tasks"])

    def test_observed_rows_and_active_totals_share_one_database_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "router.sqlite3", "host")
            sessions = SessionObserver(store, "host")
            native = NativeObserver(store, host_id="host")
            observed_at = int(time.time() * 1000)
            sessions.observe({"source": "claude", "session_id": "S",
                              "kind": "session_start", "observed_at": observed_at})
            native_record = native.observe({"source": "claude", "session_id": "S",
                                            "agent_id": "A", "kind": "start",
                                            "observed_at": observed_at})
            original_connect = store.connect

            def collect_with_interleaved_stop(observer, table, where, parameters):
                changed = False

                def stop_record():
                    with original_connect() as writer:
                        writer.execute("BEGIN IMMEDIATE")
                        row = writer.execute(
                            f"SELECT data FROM {table} WHERE {where}", parameters,
                        ).fetchone()
                        record = json.loads(row[0])
                        record.update(status="stopped", last_event_at=observed_at + 1)
                        writer.execute(
                            f"UPDATE {table} SET data=? WHERE {where}",
                            (json.dumps(record, separators=(",", ":")), *parameters),
                        )
                        writer.commit()

                @contextmanager
                def interleaved_connect(timeout=10):
                    nonlocal changed
                    with original_connect(timeout) as connection:
                        class InterleavedConnection:
                            def execute(self, statement, parameters=()):
                                nonlocal changed
                                cursor = connection.execute(statement, parameters)
                                if not changed and statement.lstrip().startswith(
                                        f"SELECT data FROM {table}"):
                                    rows = cursor.fetchall()
                                    changed = True
                                    stop_record()
                                    return rows
                                return cursor
                        yield InterleavedConnection()

                with patch.object(store, "connect", interleaved_connect), \
                        patch("router.sessions.now_ms", return_value=observed_at), \
                        patch("router.native.now_ms", return_value=observed_at):
                    return observer.fleet_records(100)

            session_rows, session_active = collect_with_interleaved_stop(
                sessions, "sessions", "host_id=? AND source=? AND session_id=?",
                ("host", "claude", "S"),
            )
            native_rows, native_active = collect_with_interleaved_stop(
                native, "observations", "task_id=?", (native_record["task_id"],),
            )

        self.assertEqual((session_rows[0]["status"], session_active), ("running", 1))
        self.assertEqual((native_rows[0]["status"], native_active), ("running", 1))

    def test_fleet_config_validation_and_partial_hosts_preserve_composite_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.toml"
            path.write_text("""schema_version = 1
ssh_timeout_seconds = 1
[capacity_defaults]
minimum_available_memory_gib = 8
[[host]]
name = "local"
transport = "local"
[[host]]
name = "remote"
transport = "ssh"
target = "remote-alias"
required = false
""")
            config = load_fleet_config(path)
        with patch("router.fleet.ssh_snapshot", side_effect=ValueError("connection failed")):
            result = query_fleet(config, local_snapshot=lambda thresholds: snapshot())
        self.assertTrue(result["partial"])
        self.assertTrue(result["hosts"][0]["ok"])
        self.assertFalse(result["hosts"][1]["ok"])
        session = result["hosts"][0]["snapshot"]["sessions"][0]
        self.assertEqual(json.loads(session["fleet_id"]), ["host-1", "claude", "same"])
        self.assertEqual([alert["kind"] for alert in result["hosts"][1]["alerts"]], ["host_offline"])
        self.assertNotIn("alert", result["hosts"][1])

    def test_fleet_config_rejects_unknown_and_invalid_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.toml"
            for contents in (
                'schema_version = 1\nunknown = true\n[[host]]\nname = "local"\ntransport = "local"\n',
                'schema_version = 1\n[[host]]\nname = "local"\ntransport = "local"\nminimum_available_memory_gib = -1\n',
                'schema_version = 1\n[[host]]\nname = "remote"\ntransport = "ssh"\n',
            ):
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    load_fleet_config(path)

    def test_fleet_config_requires_strict_schema_and_bounded_intervals(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.toml"
            host = '\n[[host]]\nname = "local"\ntransport = "local"\n'
            for schema_version in ("true", "1.0", '"1"', "2"):
                path.write_text(f"schema_version = {schema_version}{host}")
                with self.subTest(schema_version=schema_version), self.assertRaisesRegex(
                        ValueError, "schema_version"):
                    load_fleet_config(path)
            for field, value in (
                ("ssh_timeout_seconds", 0.099), ("ssh_timeout_seconds", 60.001),
                ("refresh_seconds", 0.099), ("refresh_seconds", 3600.001),
            ):
                path.write_text(f"schema_version = 1\n{field} = {value}{host}")
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                        ValueError, "between"):
                    load_fleet_config(path)

    def test_fleet_config_enforces_capacity_threshold_domains_and_strict_ordering(self):
        thresholds = {
            "minimum_available_memory_gib": 4,
            "maximum_cpu_busy_for_available_pct": 99,
            "maximum_load_per_logical_cpu_for_available": .75,
            "minimum_disk_available_gib": 10,
            "maximum_cpu_busy_for_saturated_pct": 100,
            "maximum_load_per_logical_cpu_for_saturated": 1.25,
            "minimum_available_memory_gib_for_saturated": 1,
            "minimum_disk_available_gib_for_saturated": 2,
        }

        def config(values):
            lines = ["schema_version = 1", "[capacity_defaults]"]
            lines.extend(f"{key} = {value}" for key, value in values.items())
            lines.extend(("[[host]]", 'name = "local"', 'transport = "local"'))
            return "\n".join(lines) + "\n"

        invalid = []
        invalid.append({**thresholds, "maximum_cpu_busy_for_saturated_pct": 101})
        for saturated, available in (
            ("maximum_cpu_busy_for_saturated_pct", "maximum_cpu_busy_for_available_pct"),
            ("maximum_load_per_logical_cpu_for_saturated", "maximum_load_per_logical_cpu_for_available"),
            ("minimum_available_memory_gib_for_saturated", "minimum_available_memory_gib"),
            ("minimum_disk_available_gib_for_saturated", "minimum_disk_available_gib"),
        ):
            invalid.append({**thresholds, saturated: thresholds[available]})
        invalid.extend((
            {**thresholds, "maximum_cpu_busy_for_saturated_pct": 98},
            {**thresholds, "maximum_load_per_logical_cpu_for_saturated": .5},
            {**thresholds, "minimum_available_memory_gib_for_saturated": 5},
            {**thresholds, "minimum_disk_available_gib_for_saturated": 11},
        ))

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.toml"
            path.write_text(config(thresholds))
            loaded = load_fleet_config(path)
            self.assertEqual(loaded.hosts[0].thresholds.maximum_cpu_busy_for_saturated_pct, 100)
            for values in invalid:
                path.write_text(config(values))
                with self.subTest(values=values), self.assertRaises(ValueError):
                    load_fleet_config(path)

    def test_fleet_config_and_aggregate_output_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.toml"
            path.write_text(
                'schema_version = 1\n[[host]]\nname = "local"\ntransport = "local"\n'
                f'tags = ["{"x" * (64 * 1024)}"]\n'
            )
            with self.assertRaisesRegex(ValueError, "exceeds 64 KiB"):
                load_fleet_config(path)

            invalid_values = (
                f'name = "{"n" * 121}"\ntransport = "local"\n',
                f'name = "remote"\ntransport = "ssh"\ntarget = "{"t" * 513}"\n',
                'name = "local"\ntransport = "local"\ntags = ['
                + ",".join(f'"tag-{index}"' for index in range(33)) + "]\n",
                f'name = "local"\ntransport = "local"\ntags = ["{"t" * 121}"]\n',
            )
            for host_config in invalid_values:
                path.write_text("schema_version = 1\n[[host]]\n" + host_config)
                with self.subTest(host_config=host_config[:40]), self.assertRaises(ValueError):
                    load_fleet_config(path)

        with patch("router.fleet.MAX_FLEET_OUTPUT_BYTES", 1_000):
            result = query_fleet(
                FleetConfig((FleetHost("local", "local"),)),
                local_snapshot=lambda thresholds: snapshot(),
            )
        self.assertTrue(result["partial"])
        self.assertFalse(result["hosts"][0]["ok"])
        self.assertLessEqual(len(json.dumps(result, indent=2).encode()), 1_000)
        self.assertIn("aggregate fleet output bound", result["hosts"][0]["error"])

    def test_malformed_host_isolated_capacity_override_and_capacity_projection(self):
        config = FleetConfig((
            FleetHost("bad", "local"),
            FleetHost("good", "ssh", target="good", thresholds=CapacityThresholds(
                minimum_available_memory_gib=32)),
        ))
        malformed = snapshot("bad")
        malformed["capacity"]["cpu_busy_pct"] = float("nan")
        with patch("router.fleet.ssh_snapshot", return_value=snapshot("good")):
            result = query_fleet(config, local_snapshot=lambda thresholds: malformed)
        self.assertFalse(result["hosts"][0]["ok"])
        self.assertTrue(result["hosts"][1]["ok"])
        good = result["hosts"][1]
        self.assertEqual(good["snapshot"]["capacity"]["capacity_state"], "busy")
        self.assertIn("memory_low", {alert["kind"] for alert in good["alerts"]})
        projected = capacity_projection(result)
        self.assertNotIn("sessions", projected["hosts"][1]["snapshot"])
        self.assertEqual(projected["hosts"][1]["alerts"], good["alerts"])

    def test_stale_and_version_alerts_live_on_host_wrappers(self):
        config = FleetConfig((FleetHost("one", "local"), FleetHost("two", "ssh", target="two")))
        first = snapshot("one")
        first["capacity"]["sample_age_ms"] = 20_000
        second = snapshot("two")
        second["capacity"]["sample_age_ms"] = 20_000
        second["host"]["router_version"] = "0.5.0"
        second["host"]["claude_version"] = "2.1.100"
        with patch("router.fleet.ssh_snapshot", return_value=second):
            result = query_fleet(config, local_snapshot=lambda thresholds: first)
        for record in result["hosts"]:
            wrapper_kinds = {alert["kind"] for alert in record["alerts"]}
            self.assertIn("router_version_mismatch", wrapper_kinds)
            self.assertIn("host_snapshot_stale", wrapper_kinds)
            self.assertFalse({"router_version_mismatch", "host_snapshot_stale"}
                             & {alert["kind"] for alert in record["snapshot"]["alerts"]})
        self.assertNotIn("unsupported_claude_version",
                         {alert["kind"] for alert in result["hosts"][0]["alerts"]})
        self.assertIn("unsupported_claude_version",
                      {alert["kind"] for alert in result["hosts"][1]["alerts"]})

    def test_fleet_collection_elapsed_time_ages_and_reclassifies_capacity(self):
        main_thread = threading.current_thread()
        completed_at = {}

        def monotonic():
            if threading.current_thread() is main_thread:
                return 116
            return completed_at.get(threading.get_ident(), 100)

        def local_snapshot(thresholds):
            completed_at[threading.get_ident()] = 100
            return snapshot("local")

        def remote_snapshot(host, timeout):
            completed_at[threading.get_ident()] = 116
            return snapshot("peer")

        config = FleetConfig((FleetHost("local", "local"),
                              FleetHost("peer", "ssh", target="peer")))
        with patch("router.fleet.time.monotonic", side_effect=monotonic), \
                patch("router.fleet.ssh_snapshot", side_effect=remote_snapshot):
            result = query_fleet(
                config,
                local_snapshot=local_snapshot,
            )
        local_capacity = result["hosts"][0]["snapshot"]["capacity"]
        peer_capacity = result["hosts"][1]["snapshot"]["capacity"]
        self.assertEqual(local_capacity["sample_age_ms"], 16_001)
        self.assertEqual(local_capacity["capacity_state"], "unknown")
        self.assertIn("stale", local_capacity["capacity_reasons"][0])
        self.assertIn("host_snapshot_stale", {
            alert["kind"] for alert in result["hosts"][0]["alerts"]
        })
        self.assertEqual(peer_capacity["sample_age_ms"], 16_001)
        self.assertEqual(peer_capacity["capacity_state"], "unknown")
        self.assertIn("host_snapshot_stale", {
            alert["kind"] for alert in result["hosts"][1]["alerts"]
        })

    def test_remote_reservation_expiry_uses_source_clock_not_viewer_clock(self):
        # The viewer's wall clock (time.time) is decades past the remote's
        # generated_at=1. SSH setup takes 5 s before the remote generates its
        # snapshot; merging happens 1 s after receipt.
        main_thread = threading.current_thread()
        worker_calls = []

        def monotonic():
            if threading.current_thread() is main_thread:
                return 106
            worker_calls.append(None)
            return 100 if len(worker_calls) == 1 else 105

        for reserved_until, active in ((3_001, True), (501, False)):
            worker_calls.clear()
            remote = snapshot("peer")
            remote["capacity"].update(reserved_by="coordinator", reserved_until=reserved_until,
                                      reserved_cpu_slots=8, reserved_memory_bytes=0,
                                      reservation_source="test", reservation_active=True)
            config = FleetConfig((FleetHost("peer", "ssh", target="peer"),), refresh_seconds=10)
            with self.subTest(reserved_until=reserved_until), \
                    patch("router.fleet.time.monotonic", side_effect=monotonic), \
                    patch("router.fleet.ssh_snapshot", side_effect=lambda host, timeout, value=remote: value):
                result = query_fleet(config)
                self.assertIs(result["hosts"][0]["snapshot"]["capacity"]["reservation_active"], active)

    def test_viewer_capacity_alerts_keep_nested_snapshot_chronology_valid(self):
        peer = snapshot("peer")
        peer["capacity"].update(memory_available_bytes=256 * 1024**2, load_1m=40, cpu_busy_pct=99)
        config = FleetConfig((FleetHost("peer", "ssh", target="peer"),))
        with patch("router.fleet.ssh_snapshot", side_effect=lambda host, timeout: peer):
            record = query_fleet(config)["hosts"][0]
        kinds = {alert["kind"] for alert in record["alerts"]}
        self.assertTrue({"memory_low", "host_saturated"} <= kinds, kinds)
        self.assertFalse({"memory_low", "host_saturated"} & {alert["kind"] for alert in record["snapshot"]["alerts"]})
        _validate_snapshot(json.dumps(record["snapshot"]).encode())  # Still a valid producer snapshot.

    def test_fleet_stale_classification_and_alert_share_one_threshold(self):
        # With a 10 s refresh, a 16 s old sample is fresh for alerting, so it
        # must not be classified unknown-for-staleness either.
        main_thread = threading.current_thread()

        def monotonic():
            return 116 if threading.current_thread() is main_thread else 100

        config = FleetConfig((FleetHost("local", "local"),), refresh_seconds=10)
        with patch("router.fleet.time.monotonic", side_effect=monotonic):
            result = query_fleet(config, local_snapshot=lambda thresholds: snapshot("local"))
        capacity = result["hosts"][0]["snapshot"]["capacity"]
        self.assertEqual(capacity["sample_age_ms"], 16_001)
        self.assertFalse(any("stale" in reason for reason in capacity["capacity_reasons"]))
        self.assertNotIn("host_snapshot_stale", {alert["kind"] for alert in result["hosts"][0]["alerts"]})

    def test_snapshot_rejects_record_with_missing_or_mismatched_host_identity(self):
        for host_id in (None, "other-host"):
            with self.subTest(host_id=host_id):
                value = snapshot()
                if host_id is None:
                    value["sessions"][0].pop("host_id")
                else:
                    value["sessions"][0]["host_id"] = host_id
                with self.assertRaisesRegex(ValueError, "sessions.*identity"):
                    _validate_snapshot(json.dumps(value).encode())

    def test_exit_semantics_order_and_timeout_isolation(self):
        all_offline = {"hosts": [{"ok": False, "required": False}]}
        partial = {"hosts": [{"ok": True, "required": True}, {"ok": False, "required": False}]}
        required_offline = {"hosts": [{"ok": True, "required": True}, {"ok": False, "required": True}]}
        self.assertFalse(fleet_exit_ok(all_offline, False))
        self.assertTrue(fleet_exit_ok(partial, False))
        self.assertTrue(fleet_exit_ok(partial, True))
        self.assertFalse(fleet_exit_ok(required_offline, True))
        config = FleetConfig((FleetHost("slow", "ssh", target="slow"), FleetHost("local", "local")),
                             ssh_timeout_seconds=.05)
        def remote(host, timeout):
            time.sleep(.05)
            raise ValueError("timed out")
        started = time.monotonic()
        with patch("router.fleet.ssh_snapshot", side_effect=remote):
            result = query_fleet(config, local_snapshot=lambda thresholds: snapshot("local"))
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual([host["name"] for host in result["hosts"]], ["slow", "local"])
        self.assertTrue(result["hosts"][1]["ok"])

    def test_ssh_command_is_argument_safe_and_validates_json(self):
        host = FleetHost("remote", "ssh", target="odd; target")
        popen = subprocess.Popen
        commands = []
        options = []
        def launch(command, **kwargs):
            commands.append(command)
            options.append(kwargs)
            return popen([sys.executable, "-c", "import sys;sys.stdout.write(" + repr(json.dumps(snapshot())) + ")"],
                         **kwargs)
        # A generous timeout: the success path returns as soon as output arrives.
        with patch("router.fleet.subprocess.Popen", side_effect=launch):
            self.assertEqual(ssh_snapshot(host, 15)["host"]["host_id"], "host-1")
        command = commands[0]
        self.assertEqual(command[-4:], ["odd; target", "router", "snapshot", "--json"])
        self.assertIn("--", command)
        self.assertIn("-n", command)
        self.assertEqual(options[0]["stdin"], subprocess.DEVNULL)
        self.assertTrue(options[0]["start_new_session"])

    def test_nullable_capacity_is_unknown_without_marking_host_offline(self):
        value = snapshot()
        value["capacity"].update(cpu_busy_pct=None, memory_available_bytes=None,
                                 disk_available_bytes=None)
        result = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                             local_snapshot=lambda thresholds: value)
        self.assertTrue(result["hosts"][0]["ok"])
        self.assertEqual(result["hosts"][0]["snapshot"]["capacity"]["capacity_state"], "unknown")

    def test_malformed_reservation_remains_unknown_after_fleet_reclassification(self):
        value = snapshot()
        value["partial"] = True
        value["capacity"].update(
            partial=True,
            error="reservation.json is not valid JSON",
            reservation_active=False,
            capacity_state="unknown",
            capacity_reasons=["reservation data unavailable"],
        )
        result = query_fleet(
            FleetConfig((FleetHost("local", "local"),)),
            local_snapshot=lambda thresholds: value,
        )
        host = result["hosts"][0]
        self.assertTrue(host["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(host["snapshot"]["capacity"]["capacity_state"], "unknown")
        self.assertEqual(host["snapshot"]["capacity"]["capacity_reasons"],
                         ["reservation data unavailable"])

    def test_reservation_expiry_is_recomputed_and_labels_are_kept(self):
        now = int(time.time() * 1000)
        for active, until in ((False, now + 60_000), (True, now - 1)):
            value = snapshot()
            value["generated_at"] = now
            value["capacity"].update(
                reserved_by="ci-coordinator", reserved_until=until,
                reserved_cpu_slots=1, reserved_memory_bytes=0,
                reservation_source="scheduler", reservation_active=active,
            )
            result = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                                 local_snapshot=lambda thresholds, value=value: value)
            capacity = result["hosts"][0]["snapshot"]["capacity"]
            self.assertEqual(capacity["capacity_state"], "unknown")
            self.assertTrue(result["partial"])
            self.assertEqual(capacity["reserved_by"], "ci-coordinator")

    def test_reservation_can_legitimately_expire_after_source_snapshot(self):
        value = snapshot()
        value["generated_at"] = 1_000
        value["host"]["observed_at"] = 1_000
        value["sessions"] = []
        value["agents"] = []
        value["capacity"].update(
            observed_at=1_000, sample_age_ms=0,
            active_claude_sessions=0, active_agents=0,
            reserved_by="coordinator", reserved_until=1_500,
            reserved_cpu_slots=1, reserved_memory_bytes=0,
            reservation_source="scheduler", reservation_active=True,
        )
        with patch("router.fleet.time.time", return_value=2.0):
            result = query_fleet(
                FleetConfig((FleetHost("local", "local"),)),
                local_snapshot=lambda thresholds: value,
            )
        capacity = result["hosts"][0]["snapshot"]["capacity"]
        self.assertFalse(capacity["reservation_active"])
        self.assertEqual(capacity["capacity_state"], "available")
        self.assertFalse(result["partial"])

    def test_incomplete_capacity_metadata_is_unknown_in_fleet_view(self):
        for field in ("sample_age_ms", "reservation_active"):
            with self.subTest(field=field):
                value = snapshot()
                if field == "reservation_active":
                    value["capacity"].update(
                        reserved_by="coordinator", reserved_until=int(time.time() * 1000) + 60_000,
                        reserved_cpu_slots=1, reserved_memory_bytes=0,
                        reservation_source="scheduler",
                    )
                else:
                    value["capacity"].pop(field)
                result = query_fleet(
                    FleetConfig((FleetHost("local", "local"),)),
                    local_snapshot=lambda thresholds, value=value: value,
                )
                capacity = result["hosts"][0]["snapshot"]["capacity"]
                self.assertEqual(capacity["capacity_state"], "unknown")

    def test_alert_history_preserves_first_seen_until_alert_clears(self):
        history = AlertHistory()
        first = [{"alert_id": "same", "first_seen_at": 10, "last_seen_at": 10}]
        second = [{"alert_id": "same", "first_seen_at": 20, "last_seen_at": 20}]
        self.assertEqual(history.track(first)[0]["first_seen_at"], 10)
        self.assertEqual(history.track(second)[0]["first_seen_at"], 10)
        rollback = [{"alert_id": "same", "first_seen_at": 5, "last_seen_at": 5}]
        self.assertEqual(history.track(rollback)[0]["first_seen_at"], 5)
        history.track([])
        third = [{"alert_id": "same", "first_seen_at": 30, "last_seen_at": 30}]
        self.assertEqual(history.track(third)[0]["first_seen_at"], 30)

    def test_alert_history_is_scoped_per_configured_host(self):
        history = AlertHistory()
        tracked = history.track([{"alert_id": "shared", "first_seen_at": 1, "last_seen_at": 1},
                                 {"alert_id": "shared", "first_seen_at": 9000, "last_seen_at": 9000}],
                                ["host-a", "host-b"])
        self.assertEqual([alert["first_seen_at"] for alert in tracked], [1, 9000])

    def test_needs_input_alert_survives_history_truncation(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / "router.sqlite3", "host")
        sessions = SessionObserver(store, "host")
        now = int(time.time() * 1000)
        sessions.observe({"source": "claude", "session_id": "waiting", "kind": "notification",
                          "notification_type": "permission_prompt", "observed_at": now - 5000})
        for index in range(3):
            sessions.observe({"source": "claude", "session_id": f"idle-{index}", "kind": "stop",
                              "observed_at": now - index})
        records, _ = sessions.fleet_records(3)
        self.assertEqual(records[0]["session_id"], "waiting")

        base = snapshot()
        class Host:
            def view(self, at):
                return {**base["host"], "observed_at": at}
        class Sessions:
            def fleet_records(self, limit):
                return [{"host_id": "host-1", "source": "claude", "session_id": f"s{index}",
                         "status": "idle", "started_at": 1, "last_event_at": 1} for index in range(limit)], 0
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit): return [], 0
        class Capacity:
            def view(self, thresholds, **counts):
                return {**base["capacity"], "observed_at": 100, "sample_age_ms": 0, "partial": False, **counts}
        controller = type("Controller", (), {"host": Host(), "sessions": Sessions(), "native": Native(),
                                             "capacity": Capacity()})()
        with patch("router.fleet_snapshot.now_ms", return_value=100):
            result = compose(controller, history_limit=1)
        self.assertTrue(result["partial"])
        self.assertIn("incomplete", result["errors"]["alerts"])

    def test_restart_uncertainty_keeps_capacity_unknown_until_it_expires(self):
        base = snapshot()
        for observer_kind in ("session", "agent"):
            with self.subTest(observer=observer_kind), tempfile.TemporaryDirectory() as temp:
                store = Store(Path(temp) / "router.sqlite3", "host-1")
                sessions, native = SessionObserver(store, "host-1"), NativeObserver(store, host_id="host-1")
                now = int(time.time() * 1000)
                if observer_kind == "session":
                    sessions.observe({"source": "claude", "session_id": "busy", "kind": "prompt",
                                      "observed_at": now - 1000})
                else:
                    native.observe({"source": "claude", "session_id": "S", "agent_id": "busy",
                                    "kind": "start", "observed_at": now - 1000})
                sessions.reconcile_restart()
                native.reconcile_restart()
                # Created after the restart, so only the "busy" record is uncertain; these
                # newer idle sessions push it off the one-row display page.
                for index in range(3):
                    sessions.observe({"source": "claude", "session_id": f"quiet-{index}", "kind": "stop",
                                      "observed_at": now - index})

                class Host:
                    def view(self, at):
                        return {**base["host"], "host_id": "host-1", "observed_at": at}
                class Capacity:
                    def view(self, thresholds, now=now, **counts):
                        return {**base["capacity"], "observed_at": now, "sample_age_ms": 0,
                                "partial": False, "load_1m": 0, "cpu_busy_pct": 1, **counts}
                controller = type("Controller", (), {"host": Host(), "sessions": sessions,
                                                     "native": native, "capacity": Capacity()})()
                with patch("router.fleet_snapshot.now_ms", return_value=now):
                    uncertain = compose(controller, history_limit=1)
                self.assertTrue(uncertain["partial"])
                self.assertEqual(uncertain["capacity"]["capacity_state"], "unknown")
                fleet = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                                    local_snapshot=lambda thresholds, value=uncertain: value)
                self.assertNotEqual(fleet["hosts"][0]["snapshot"]["capacity"]["capacity_state"], "available")
                later = now + 61_000
                with patch("router.sessions.now_ms", return_value=later), \
                        patch("router.native.now_ms", return_value=later):
                    self.assertIsNotNone(sessions.fleet_records(1)[1])
                    self.assertIsNotNone(native.fleet_records(1)[1])

    def test_fleet_text_sharing_is_on_by_default_and_can_be_withheld_by_the_producer(self):
        base = snapshot()

        class Host:
            def view(self, at):
                return {**base["host"], "observed_at": at}
        class Sessions:
            def fleet_records(self, limit): return [], 0
            def tasks(self, limit):
                return [{"host_id": "host-1", "source": "claude", "session_id": "S", "task_id": "T",
                         "status": "created", "subject": "Rotate staging keys", "created_at": 1,
                         "last_event_at": 1}]
        class Native:
            def fleet_records(self, limit):
                return [{"host_id": "host-1", "source": "claude", "parent_session_id": "S",
                         "agent_id": "A", "status": "stopped", "task": "Fix parser",
                         "started_at": 1, "last_event_at": 1}], 0
        class Capacity:
            def view(self, thresholds, **counts):
                return {**base["capacity"], "observed_at": 100, "sample_age_ms": 0, "partial": False, **counts}

        for share, task, subject in ((True, "Fix parser", "Rotate staging keys"), (False, None, None)):
            controller = type("Controller", (), {
                "host": Host(), "sessions": Sessions(), "native": Native(), "capacity": Capacity(),
                "settings": type("Settings", (), {"fleet_share_text": share})(),
            })()
            with self.subTest(share=share), patch("router.fleet_snapshot.now_ms", return_value=100):
                result = compose(controller)
                self.assertEqual(result["agents"][0]["task"], task)
                self.assertEqual(result["tasks"][0]["subject"], subject)
        self.assertTrue(Settings().fleet_share_text)
        self.assertFalse(Settings(AGENT_ROUTER_FLEET_SHARE_TEXT="false").fleet_share_text)

    def test_zero_and_future_alert_times_are_normalized_and_round_trip(self):
        base = snapshot()

        class Host:
            def view(self, at):
                return {**base["host"], "observed_at": at}
        class Sessions:
            def fleet_records(self, limit):
                return [
                    {"host_id": "host-1", "source": "claude", "session_id": "zero",
                     "status": "unknown", "started_at": 0, "last_event_at": 0},
                    {"host_id": "host-1", "source": "claude", "session_id": "future",
                     "status": "unknown", "started_at": 0, "last_event_at": 200},
                ], 0
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit): return [], 0
        class Capacity:
            def view(self, thresholds, **counts):
                return {**base["capacity"], "observed_at": 100, "sample_age_ms": 0,
                        "partial": False, **counts}

        controller = type("Controller", (), {
            "host": Host(), "sessions": Sessions(), "native": Native(),
            "capacity": Capacity(),
        })()
        with patch("router.fleet_snapshot.now_ms", return_value=100):
            result = compose(controller)
        alerts_by_session = {alert["entity"]["session_id"]: alert for alert in result["alerts"]}
        self.assertEqual(alerts_by_session["zero"]["first_seen_at"], 0)
        self.assertEqual(alerts_by_session["zero"]["last_seen_at"], 100)
        self.assertEqual(alerts_by_session["future"]["first_seen_at"], 100)
        self.assertEqual(alerts_by_session["future"]["last_seen_at"], 100)
        self.assertEqual(_validate_snapshot(json.dumps(result).encode())["alerts"], result["alerts"])

        hostile = snapshot()
        hostile["alerts"] = [{
            "alert_id": "alert", "host_id": "host-1", "severity": "warning",
            "kind": "future", "first_seen_at": 1, "last_seen_at": 2,
            "entity": {}, "evidence": "future",
        }]
        with self.assertRaisesRegex(ValueError, "later than snapshot generation"):
            _validate_snapshot(json.dumps(hostile).encode())

    def test_snapshot_generated_at_is_finalized_after_capacity_observation(self):
        base = snapshot()
        class Host:
            def view(self, at): return {**base["host"], "observed_at": at}
        class Sessions:
            def fleet_records(self, limit): return [], 0
            def tasks(self, limit): return []
        class Native:
            def fleet_records(self, limit): return [], 0
        class Capacity:
            def view(self, thresholds, **counts):
                return {**base["capacity"], "observed_at": 101, "sample_age_ms": 0,
                        "partial": False, **counts}

        with patch("router.fleet_snapshot.now_ms", return_value=102):
            result = compose(type("Controller", (), {
                "host": Host(), "sessions": Sessions(), "native": Native(),
                "capacity": Capacity(),
            })())
        self.assertEqual(result["generated_at"], 102)
        self.assertEqual(result["capacity"]["sample_age_ms"], 1)
        self.assertEqual(result["capacity"]["capacity_state"], "available")
        projected = _validate_snapshot(json.dumps(result).encode())
        self.assertEqual(projected["capacity"]["capacity_state"], "available")

    def test_capacity_projection_preserves_operator_visible_snapshot_errors(self):
        value = snapshot()
        value.update(partial=True, errors={"capacity": "sampler failed: disk unavailable"})
        result = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                             local_snapshot=lambda thresholds: value)
        projected = capacity_projection(result)
        errors = projected["hosts"][0]["snapshot"]["errors"]
        self.assertIn("sampler failed: disk unavailable", errors["capacity"])

    def test_watch_mode_keeps_refreshing_when_all_hosts_are_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "fleet.toml"
            config_path.write_text('schema_version = 1\n[[host]]\nname = "local"\ntransport = "local"\n')
            offline = {"hosts": [{"name": "local", "ok": False, "required": True,
                                   "error": "offline", "alerts": []}]}
            with patch.object(sys, "argv", ["router", "fleet", "status", "--watch", "--config", str(config_path)]), \
                    patch("router.fleet.query_fleet", side_effect=[offline, KeyboardInterrupt]), \
                    patch("router.cli.time.sleep"), redirect_stdout(io.StringIO()):
                cli_main()

    def test_watch_json_is_one_document_per_line(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "fleet.toml"
            config_path.write_text('schema_version = 1\n[[host]]\nname = "local"\ntransport = "local"\n')
            result = {"schema_version": 1, "generated_at": 1, "partial": False,
                      "version_mismatch": False, "hosts": []}
            output = io.StringIO()
            with patch.object(sys, "argv", ["router", "fleet", "status", "--watch", "--json",
                                             "--config", str(config_path)]), \
                    patch("router.fleet.query_fleet", side_effect=[result, result, KeyboardInterrupt]), \
                    patch("router.cli.time.sleep"), redirect_stdout(output):
                cli_main()
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line) for line in lines], [result, result])

    def test_ssh_output_is_bounded_drained_reaped_and_error_text_kept(self):
        host = FleetHost("remote", "ssh", target="remote")
        actual_popen = subprocess.Popen
        launched = []
        script = "import os,sys;os.write(2,b'x'*200000);os.write(1," + repr(json.dumps(snapshot()).encode()) + ")"
        def launch(command, **kwargs):
            process = actual_popen([sys.executable, "-c", script], **kwargs)
            launched.append(process)
            return process
        with patch("router.fleet.subprocess.Popen", side_effect=launch):
            self.assertEqual(ssh_snapshot(host, 15)["host"]["host_id"], "host-1")
        self.assertIsNotNone(launched[0].poll())

        def run_script(source, timeout=.2):
            processes = []
            def replacement(command, **kwargs):
                process = actual_popen([sys.executable, "-c", source], **kwargs)
                processes.append(process)
                return process
            with patch("router.fleet.subprocess.Popen", side_effect=replacement), \
                    self.assertRaises(ValueError) as raised:
                ssh_snapshot(host, timeout)
            self.assertIsNotNone(processes[0].poll())
            return str(raised.exception)

        self.assertIn("one MiB", run_script("import os;os.write(1,b'x'*(1024*1024+1))"))
        self.assertIn("timed out", run_script("import time;time.sleep(2)", .05))
        error = run_script("import os,sys;os.write(2,b'Permission denied\\x1b[31m (publickey)');sys.exit(2)")
        self.assertEqual(error, "Permission denied (publickey)")

    def test_ssh_setup_failure_and_pipe_holding_descendant_are_group_cleaned(self):
        host = FleetHost("remote", "ssh", target="remote")
        actual_popen = subprocess.Popen
        launched = []

        def launch(command, **kwargs):
            process = actual_popen([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
            launched.append(process)
            return process

        with patch("router.fleet.subprocess.Popen", side_effect=launch), \
                patch("router.fleet.os.set_blocking", side_effect=OSError("setup failed")), \
                self.assertRaisesRegex(ValueError, "setup failed"):
            ssh_snapshot(host, .05)
        self.assertIsNotNone(launched[0].poll())

        with tempfile.TemporaryDirectory() as temp:
            descendant_pid_file = Path(temp) / "descendant.pid"
            descendant = (
                "import os,time\n"
                f"path={str(descendant_pid_file)!r}\n"
                "pid=os.fork()\n"
                "if pid:\n"
                "    deadline=time.monotonic()+1\n"
                "    while not os.path.exists(path) and time.monotonic()<deadline: time.sleep(.001)\n"
                "    raise SystemExit(0)\n"
                "with open(path,'w') as stream: stream.write(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            descendant_pid = None
            started = time.monotonic()

            def fork_launch(command, **kwargs):
                return actual_popen([sys.executable, "-c", descendant], **kwargs)

            try:
                with patch("router.fleet.subprocess.Popen", side_effect=fork_launch), \
                        self.assertRaisesRegex(ValueError, "timed out"):
                    # Long enough for the descendant to start and report its PID.
                    ssh_snapshot(host, 3)
                self.assertLess(time.monotonic() - started, 4.5)  # Timeout plus bounded group cleanup.
                self.assertTrue(descendant_pid_file.exists(), "forked descendant never reported its PID")
                descendant_pid = int(descendant_pid_file.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(descendant_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.01)
                else:
                    self.fail("ssh process-group cleanup left the pipe-holding descendant alive")
            finally:
                if descendant_pid is not None:
                    try:
                        os.kill(descendant_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_ssh_failure_kills_sigterm_ignoring_member_after_leader_exits(self):
        host = FleetHost("remote", "ssh", target="remote")
        actual_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "member.pid"
            script = (
                "import os,signal,time\n"
                f"path={str(pid_file)!r}\n"
                "if os.fork() == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    os.close(1); os.close(2)\n"
                "    with open(path, 'w') as stream: stream.write(str(os.getpid()))\n"
                "    time.sleep(30)\n"
                "    os._exit(0)\n"
                "time.sleep(30)\n"  # The leader dies on SIGTERM.
            )
            member = None
            try:
                with patch("router.fleet.subprocess.Popen",
                           side_effect=lambda command, **kwargs: actual_popen([sys.executable, "-c", script], **kwargs)), \
                        self.assertRaisesRegex(ValueError, "timed out"):
                    ssh_snapshot(host, 3)  # Long enough for the member to fork and report.
                self.assertTrue(pid_file.exists(), "group member never reported its PID")
                member = int(pid_file.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(member, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.01)
                else:
                    self.fail("SIGTERM-ignoring group member survived failed collection")
            finally:
                if member is not None:
                    try:
                        os.kill(member, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_ssh_unreapable_process_cleanup_remains_bounded(self):
        host = FleetHost("remote", "ssh", target="remote")
        read_stdout, write_stdout = os.pipe()
        read_stderr, write_stderr = os.pipe()
        os.close(write_stdout)
        os.close(write_stderr)

        class Unreapable:
            pid = 999_999_999
            returncode = None
            stdout = os.fdopen(read_stdout, "rb", buffering=0)
            stderr = os.fdopen(read_stderr, "rb", buffering=0)
            def poll(self): return None
            def wait(self, timeout=None): raise subprocess.TimeoutExpired("ssh", timeout)
            def kill(self): pass

        started = time.monotonic()
        with patch("router.fleet.subprocess.Popen", return_value=Unreapable()), \
                self.assertRaisesRegex(ValueError, "did not exit"):
            ssh_snapshot(host, .05)
        self.assertLess(time.monotonic() - started, 1.2)

    def test_snapshot_augmentation_stays_bounded_and_error_text_is_kept_but_made_safe(self):
        expanded = snapshot("h" * 128)
        expanded["agents"] = []
        expanded["sessions"] = [
            {"host_id": "h" * 128, "source": "claude", "session_id": str(index),
             "status": "running", "started_at": 0, "last_event_at": 1,
             "cwd": "x" * 650}
            for index in range(1000)
        ]
        self.assertLess(len(json.dumps(expanded, separators=(",", ":")).encode()), MAX_SNAPSHOT_BYTES)
        result = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                             local_snapshot=lambda thresholds: expanded)
        self.assertFalse(result["hosts"][0]["ok"])
        self.assertEqual(
            result["hosts"][0]["error"],
            "Host snapshot exceeds one MiB after fleet identity augmentation",
        )
        # Text is kept as written; only terminal controls, line breaks, and length change.
        for raw, shown in (
                ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic dXNlcjpwYXNz"),
                ('{"password": "orchid meadow"}', '{"password": "orchid meadow"}'),
                ("C:\\Users\\x\\notes.txt", "C:\\Users\\x\\notes.txt"),
                ("line one\nline two\r\nthree", "line one line two  three"),
                ("\x1b[31mred\x1b[0m text", "red text"),
                ("API_\x1b[0mKEY=value", "API_KEY=value"),
                ("link \x1b]8;;https://x\x07label\x1b]8;;\x07 end", "link label end"),
                ("title \x1b]0;owned\x1b\\ ok", "title  ok"),
                ("eight\x9b31mbit", "eight31mbit"),  # 8-bit C1: only the control is dropped.
                ("caf\u0090e rest of text", "cafe rest of text"),
                ("a\x1b(B\x1b[mb", "ab"),  # tput sgr0
                ("a\x1b7b\x1b8c", "abc"),
                ("a\x1b=b", "ab"),
                ("a\x1b[3\n1mb", "ab"),
                ("a\x1b]0;title xx\x1b[31mred", "ared"),
                ("dcs \x1bPpayload\x1b\\ done", "dcs  done"),
                ("unterminated \x1b]0;rest of text", "unterminated "),
                ("zero\u200bwidth \u202eflip", "zerowidth flip"),
        ):
            with self.subTest(raw=raw[:30]):
                self.assertEqual(_error_text(raw), shown)
        self.assertEqual(bound_text("x" * 5000, 1000), "x" * 1000)

    def test_local_snapshot_final_boundary_strips_controls_from_errors_and_displays(self):
        secret = "build \x1b[31mred\x1b]0;title\x07 \u202eflip"

        class Host:
            def view(self, at):
                return {"host_id": "host", "hostname": secret, "observed_at": at}
        class Sessions:
            def fleet_records(self, limit):
                return [{"host_id": "host", "source": "claude", "session_id": "safe",
                         "status": "running", "activity": secret}], 1
            def tasks(self, limit):
                raise OSError(f"tasks failed {secret}")
        class Native:
            def fleet_records(self, limit):
                return [], 0
        class Capacity:
            def view(self, thresholds, **counts):
                return {"partial": True, "error": f"sampler failed {secret}",
                        "capacity_state": "unknown", "capacity_reasons": [secret], **counts}

        result = compose(type("Controller", (), {
            "host": Host(), "sessions": Sessions(), "native": Native(), "capacity": Capacity(),
        })())
        serialized = json.dumps(result)
        for control in ("\\u001b", "\\u0007", "\\u202e"):
            self.assertNotIn(control, serialized)
        self.assertTrue(result["partial"])
        self.assertIn("tasks failed build red flip", serialized)

    def test_cli_unicode_controls_are_sanitized_through_fleet_output(self):
        value = snapshot()
        value["sessions"][0]["activity"] = "safe\u202eevil\u2066\u2028fake-row\u2029tail\ud800\udcff"
        value.update(
            partial=True,
            errors={
                "sessions": "remote failed: --pass\u200bword example\u2060 value\x1b[2J done",
            },
        )
        result = query_fleet(
            FleetConfig((FleetHost("local", "local"),)),
            local_snapshot=lambda thresholds: value,
        )
        serialized = json.dumps(result, ensure_ascii=False)
        rendered = fleet_table(result)
        for leaked in ("\u202e", "\u2066", "\u2028", "\u2029", "\ud800", "\udcff", "\u200b", "\x1b"):
            self.assertNotIn(leaked, serialized)
            self.assertNotIn(leaked, rendered)
        serialized.encode("utf-8")
        rendered.encode("utf-8")
        self.assertIn("remote failed: --password example value done", serialized)
        host = result["hosts"][0]["snapshot"]
        self.assertTrue(host["partial"])
        self.assertIn("changed for output safety", host["errors"]["sessions"])
        self.assertEqual(host["capacity"]["capacity_state"], "busy")

    def test_tables_strip_terminal_controls_and_agent_only_host_keeps_metrics(self):
        value = snapshot("host\x1b[31m")
        value["sessions"] = []
        value["agents"][0]["activity"] = "build\n\x1b[2J\u202eevil\u2028fake-row\ud800"
        result = {"hosts": [{"name": "bad\x1b\u2066name\u2029fake-host\udcff",
                              "ok": True, "snapshot": value}]}
        rendered = fleet_table(result)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\u2066", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertNotIn("\u2029", rendered)
        self.assertNotIn("\ud800", rendered)
        self.assertNotIn("\udcff", rendered)
        self.assertNotIn("\n\x1b", rendered)
        rendered.encode("utf-8")
        agent_row = rendered.splitlines()[1]
        self.assertIn("badname", agent_row)
        self.assertIn("20%", agent_row)

    def test_table_marks_partial_empty_snapshot_and_bounds_section_errors(self):
        value = snapshot()
        value.update(
            partial=True,
            sessions=[],
            agents=[],
            errors={
                "lifecycle": "1 write failure\n\x1b[2J" + "x" * 300,
                **{f"section-{number}": "failed" for number in range(6)},
            },
        )
        rendered = fleet_table({"hosts": [{"name": "local", "ok": True, "snapshot": value}]})
        self.assertNotIn("no active sessions", rendered)
        capacity_rendered = fleet_table(
            {"hosts": [{"name": "local", "ok": True, "snapshot": value}]},
            capacity_only=True,
        )
        populated = {**value, "sessions": [{"session_id": "S", "status": "running"}]}
        populated_rendered = fleet_table(
            {"hosts": [{"name": "local", "ok": True, "snapshot": populated}]},
        )
        for output in (rendered, capacity_rendered, populated_rendered):
            self.assertIn("partial snapshot", output)
            self.assertIn("lifecycle error: 1 write failure", output)
            self.assertNotIn("\x1b", output)
            self.assertIn("... 2 more section errors omitted", output)
            self.assertNotIn("section-5 error", output)

    def test_table_renders_bounded_wrapper_and_snapshot_alerts(self):
        value = snapshot()
        value["sessions"] = []
        value["agents"] = []
        value["alerts"] = [{
            "kind": f"snapshot-{number}", "severity": "warning",
            "evidence": "uncertain\n\x1b[2J" + "x" * 300,
        } for number in range(8)]
        result = {"hosts": [{
            "name": "local", "ok": True, "snapshot": value,
            "alerts": [{"kind": "router_version_mismatch", "severity": "warning",
                        "evidence": "versions differ"}],
        }]}

        rendered = fleet_table(result)
        self.assertIn("alert [warning] router_version_mismatch: versions differ", rendered)
        self.assertIn("snapshot-6", rendered)
        self.assertNotIn("snapshot-7", rendered)
        self.assertIn("... 1 more alerts omitted", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_two_isolated_daemons_with_colliding_session_ids_merge_as_distinct_hosts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processes = []
            endpoints = []
            try:
                for number in range(2):
                    state = root / f"state-{number}"
                    endpoint = root / f"router-{number}.sock"
                    environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_ROUTER_")}
                    environment.update(AGENT_ROUTER_STATE_DIR=str(state), AGENT_ROUTER_SOCKET=str(endpoint),
                                       AGENT_ROUTER_ENV_FILE=str(root / "missing.env"))
                    launcher = ("import sys,router.host; identity=sys.argv[1]; "
                                "router.host.stable_host_id=lambda directory: identity; "
                                "from router.cli import main; sys.argv=['router','daemon','start']; main()")
                    process = subprocess.Popen([sys.executable, "-c", launcher, f"emulated-host-{number}"],
                                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    processes.append(process)
                    endpoints.append(endpoint)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            request("health", {}, endpoint, timeout=.2)
                            break
                        except ValueError:
                            time.sleep(.03)
                    else:
                        self.fail("isolated daemon did not start")
                    request("session_observe", {"source": "claude", "session_id": "same", "kind": "session_start",
                                                "observed_at": int(time.time() * 1000)}, endpoint)
                config = FleetConfig((FleetHost("one", "local"), FleetHost("two", "ssh", target="emulated")),
                                     ssh_timeout_seconds=1)
                with patch("router.fleet.ssh_snapshot", side_effect=lambda host, timeout: request("snapshot", {}, endpoints[1])):
                    result = query_fleet(config, local_snapshot=lambda thresholds: request("snapshot", {}, endpoints[0]))
                identities = [host["snapshot"]["sessions"][0]["fleet_id"] for host in result["hosts"]]
                self.assertEqual(len(set(identities)), 2)
                self.assertTrue(result["partial"])
                self.assertTrue(all(host["snapshot"]["capacity"]["capacity_state"] == "unknown"
                                    for host in result["hosts"]))
            finally:
                for process, endpoint in zip(processes, endpoints, strict=False):
                    if process.poll() is None:
                        try:
                            request("daemon_stop", {}, endpoint)
                            process.wait(timeout=5)
                        except (ValueError, subprocess.TimeoutExpired):
                            process.terminate()
                            process.wait(timeout=5)
                    process.communicate()


class RouterTaskFleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-fleet-tasks-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.control = Controller(settings(root / "repo"), {}, root / "state")

    def task(self, status, at=None, **fields):
        task = {"task_id": "task-" + uuid.uuid4().hex, "session_id": "parent-session", "status": status,
                "executor": "codex", "requested_model": "small", "attempts": 1, "created_at": 1,
                "invocation": {"task": "PRIVATE TASK TEXT", "parent_worktree": "/work"},
                "cost": 1.5, "usage": {"input": 9}, "worktree": "/secret", **fields}
        with self.control.store.transaction() as db, \
                patch("router.db.now_ms", return_value=at if at is not None else int(time.time() * 1000)):
            self.control.store.put(db, task)
        return task["task_id"]

    def test_router_tasks_reach_fleet_status_as_lifecycle_only(self):
        running = self.task("running", attempt_started_at=int(time.time() * 1000) - 1000)
        self.task("closed", at=1)  # Long finished: outside the fleet window.
        local = compose(self.control)
        self.assertEqual([task["task_id"] for task in local["router_tasks"]], [running])
        projected = _validate_snapshot(json.dumps(local).encode())
        [record] = projected["router_tasks"]
        self.assertEqual((record["status"], record["executor"], record["parent_session_id"]),
                         ("running", "codex", "parent-session"))
        self.assertEqual(record["task"], "PRIVATE TASK TEXT")
        self.assertFalse({"cost", "usage", "worktree", "cwd"} & record.keys())
        self.assertNotIn("router_tasks", projected.get("errors", {}))
        # A peer on an older version sends no Router tasks; that is not a partial snapshot.
        older = _validate_snapshot(json.dumps(snapshot("older-peer")).encode())
        self.assertEqual((older["router_tasks"], older["partial"]), ([], False))
        config = FleetConfig((FleetHost("here", "local"), FleetHost("there", "ssh", target="there")))
        with patch("router.fleet.ssh_snapshot", return_value=older):
            result = query_fleet(config, local_snapshot=lambda thresholds: local)
        self.assertTrue(all(host["ok"] for host in result["hosts"]))
        table = fleet_table(result)
        self.assertIn("codex:" + running[:14], table)
        self.assertIn("router:parent-sess", table)

    def test_router_task_text_follows_fleet_text_sharing(self):
        self.task("running")
        self.control.settings = Settings(AGENT_ROUTER_FLEET_SHARE_TEXT="false")
        self.assertIsNone(compose(self.control)["router_tasks"][0]["task"])


if __name__ == "__main__":
    unittest.main()
