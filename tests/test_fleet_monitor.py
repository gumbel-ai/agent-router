import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import mock_open, patch

from router.capacity import (
    MAX_RESERVATION_BYTES,
    CapacitySampler,
    CapacityThresholds,
    ReservationReader,
    _linux_cpu_ticks,
    _linux_memory,
    classify,
)
from router.cli import fleet_table
from router.cli import main as cli_main
from router.client import request
from router.db import Store
from router.fleet import (
    MAX_COLLECTION_RECORDS,
    FleetConfig,
    FleetHost,
    _redact_error,
    _validate_snapshot,
    capacity_projection,
    fleet_exit_ok,
    load_fleet_config,
    query_fleet,
    ssh_snapshot,
)
from router.fleet_snapshot import MAX_SNAPSHOT_BYTES, AlertHistory, bound, compose
from router.host import current_boot_id, stable_host_id
from router.native import NativeObserver
from router.sessions import SessionObserver


def snapshot(host_id="host-1"):
    return {"schema_version": 1, "partial": False, "generated_at": 1,
            "host": {"host_id": host_id, "hostname": "test-host", "boot_id": "boot-1",
                     "platform": "linux", "architecture": "x86_64",
                     "router_version": "0.4.0", "claude_version": "2.1.300",
                     "observed_at": 1},
            "capacity": {"logical_cpus": 8, "load_1m": 1, "load_5m": 1, "cpu_busy_pct": 20,
                         "memory_available_bytes": 16 * 1024**3, "disk_available_bytes": 20 * 1024**3,
                         "sample_age_ms": 1},
            "sessions": [{"host_id": host_id, "source": "claude", "session_id": "same", "status": "running"}],
            "agents": [{"host_id": host_id, "source": "claude", "parent_session_id": "same",
                        "agent_id": "child", "status": "running"}],
            "tasks": [], "managed_processes": [], "inferred_processes": [], "alerts": []}


class HostAndMigrationTests(unittest.TestCase):
    def test_generated_host_identity_is_stable_private_and_boot_id_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = stable_host_id(directory, "darwin")
            self.assertEqual(stable_host_id(directory, "darwin"), first)
            self.assertEqual((directory / "host-id").stat().st_mode & 0o777, 0o600)
        completed = lambda value: subprocess.CompletedProcess([], 0, stdout=value, stderr=b"")
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

    def test_operator_visible_task_metadata_redacts_credentials(self):
        self.event("session_start")
        self.event("task_created", task_id="T", task_subject="rotate password=swordfish")
        task = self.observer.tasks()[0]
        self.assertNotIn("swordfish", task["subject"])
        self.assertIn("[REDACTED]", task["subject"])

        native = NativeObserver(self.store, host_id="host")
        record = native.observe({
            "source": "claude", "session_id": "S", "agent_id": "A",
            "kind": "launch", "observed_at": self.now,
            "task": "use api_key=sk-ant-abcdefghijklmnop",
        })
        self.assertNotIn("sk-ant-abcdefghijklmnop", record["task"])
        self.assertIn("[REDACTED]", record["task"])

        session_boundary_secret = "x" * 477 + " https://user:swordfish@example.com"
        self.event("task_created", task_id="boundary", task_subject=session_boundary_secret)
        bounded_task = next(task for task in self.observer.tasks() if task["task_id"] == "boundary")
        native_boundary_secret = "x" * 217 + " https://user:swordfish@example.com"
        bounded_native = native.observe({
            "source": "claude", "session_id": "S", "agent_id": "boundary",
            "kind": "launch", "observed_at": self.now, "task": native_boundary_secret,
        })
        self.assertNotIn("swordfish", bounded_task["subject"])
        self.assertNotIn("swordfish", bounded_native["task"])

        task_id = record["task_id"]
        legacy = native.get(task_id)
        legacy["task"] = "legacy password=swordfish"
        with self.store.transaction() as db:
            native.put(db, legacy)
        visible = native.snapshot()[0]
        self.assertNotIn("swordfish", visible["task"])
        self.assertIn("[REDACTED]", visible["task"])

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

    def test_first_cpu_sample_is_unknown_until_a_delta_exists(self):
        with patch("router.capacity._raw_cpu_ticks", side_effect=[(100, 50), (120, 55)]), \
                patch("router.capacity._raw_memory", return_value=(16, 8, 0)), \
                patch("router.capacity.shutil.disk_usage", return_value=type("Usage", (), {"free": 20})()), \
                patch("router.capacity.os.getloadavg", return_value=(0, 0, 0)), \
                patch("router.capacity.os.cpu_count", return_value=2):
            sampler = CapacitySampler(Path(self.temp.name))
            self.assertIsNone(sampler.view(CapacityThresholds())["cpu_busy_pct"])
            self.assertEqual(sampler.sample()["cpu_busy_pct"], 75.0)

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
            reservation.write_text("not-json")
            partial = sampler.view(CapacityThresholds())
            self.assertTrue(partial["partial"])
            self.assertEqual(partial["capacity_state"], "unknown")

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


class SnapshotAndFleetTests(unittest.TestCase):
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
            encoded(lambda value: value["host"].pop("boot_id")),
            encoded(lambda value: value.update(sessions=[{}] * (MAX_COLLECTION_RECORDS + 1))),
            encoded(lambda value: value["capacity"].update(reserved_until=True)),
            encoded(lambda value: value.update(alerts=[{"kind": "memory_low"}])),
        ])
        for payload in cases:
            with self.subTest(payload=payload[:80]), self.assertRaises(ValueError):
                _validate_snapshot(payload)

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
                         "available")

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
                def view(self, thresholds, **counts): return counts

            controller = type("Controller", (), {
                "host": Host(), "sessions": sessions, "native": native, "capacity": Capacity(),
            })()
            result = compose(controller, history_limit=1)

        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(len(result["agents"]), 1)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["capacity"]["active_claude_sessions"], 2)
        self.assertEqual(result["capacity"]["active_agents"], 2)
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
        self.assertEqual(session["fleet_id"], "host-1:claude:same")
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
        self.assertIn("memory_low", {alert["kind"] for alert in good["snapshot"]["alerts"]})
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
        self.assertEqual(peer_capacity["sample_age_ms"], 1)
        self.assertEqual(peer_capacity["capacity_state"], "available")
        self.assertNotIn("host_snapshot_stale", {
            alert["kind"] for alert in result["hosts"][1]["alerts"]
        })

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
        with patch("router.fleet.subprocess.Popen", side_effect=launch):
            self.assertEqual(ssh_snapshot(host, 2)["host"]["host_id"], "host-1")
        command = commands[0]
        self.assertEqual(command[-4:], ["odd; target", "router", "snapshot", "--json"])
        self.assertIn("--", command)
        self.assertIn("-n", command)
        self.assertEqual(options[0]["stdin"], subprocess.DEVNULL)

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
        first = [{"alert_id": "same", "first_seen_at": 10}]
        second = [{"alert_id": "same", "first_seen_at": 20}]
        self.assertEqual(history.track(first)[0]["first_seen_at"], 10)
        self.assertEqual(history.track(second)[0]["first_seen_at"], 10)
        history.track([])
        third = [{"alert_id": "same", "first_seen_at": 30}]
        self.assertEqual(history.track(third)[0]["first_seen_at"], 30)

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

    def test_ssh_output_is_bounded_drained_reaped_and_redacted(self):
        host = FleetHost("remote", "ssh", target="remote")
        actual_popen = subprocess.Popen
        launched = []
        script = "import os,sys;os.write(2,b'x'*200000);os.write(1," + repr(json.dumps(snapshot()).encode()) + ")"
        def launch(command, **kwargs):
            process = actual_popen([sys.executable, "-c", script], **kwargs)
            launched.append(process)
            return process
        with patch("router.fleet.subprocess.Popen", side_effect=launch):
            self.assertEqual(ssh_snapshot(host, 2)["host"]["host_id"], "host-1")
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
        error = run_script("import os,sys;os.write(2,b'password: swordfish');sys.exit(2)")
        self.assertNotIn("swordfish", error)
        self.assertIn("[REDACTED]", error)

    def test_snapshot_augmentation_stays_bounded_and_common_credentials_are_redacted(self):
        expanded = snapshot("h" * 128)
        expanded["agents"] = []
        expanded["sessions"] = [
            {"source": "claude", "session_id": str(index), "status": "running", "activity": "x" * 850}
            for index in range(1000)
        ]
        self.assertLess(len(json.dumps(expanded, separators=(",", ":")).encode()), MAX_SNAPSHOT_BYTES)
        result = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                             local_snapshot=lambda thresholds: expanded)
        self.assertFalse(result["hosts"][0]["ok"])
        for secret, leaked in (
                ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
                ('password: "orchid meadow"', "orchid meadow"),
                ('"password": "orchid meadow"', "orchid meadow"),
                ('"password": "orchid \\"meadow\\""', "meadow"),
                ('"AWS_SECRET_ACCESS_KEY": "abcd 1234"', "abcd 1234"),
                ('password: "orchid meadow', "meadow"),
                ('password: "orchid\nmeadow"', "meadow"),
                ('password: "orchid\nmeadow', "meadow"),
                ("AWS_SECRET_ACCESS_KEY='abcd 1234", "1234"),
                ("passphrase=hunter2", "hunter2"),
                ("pwd=hunter2", "hunter2"),
                ("https://user:pass@example.com/path", "user:pass"),
                ("AWS_SECRET_ACCESS_KEY=abcd1234secret", "abcd1234secret"),
                ("ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop", "sk-ant-abcdefghijklmnop"),
                ("credential ghp_abcdefghijklmnop", "ghp_abcdefghijklmnop"),
                ("credential github_pat_abcdefghijklmnop", "github_pat_abcdefghijklmnop"),
                ("credential sk-abcdefghijklmnop", "sk-abcdefghijklmnop"),
                ("-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-body\n-----END OPENSSH PRIVATE KEY-----",
                 "private-body"),
        ):
            redacted = _redact_error(secret)
            self.assertNotIn(leaked, redacted)
            self.assertIn("[REDACTED", redacted)

    def test_tables_strip_terminal_controls_and_agent_only_host_keeps_metrics(self):
        value = snapshot("host\x1b[31m")
        value["sessions"] = []
        value["agents"][0]["activity"] = "build\n\x1b[2J"
        result = {"hosts": [{"name": "bad\x1bname", "ok": True, "snapshot": value}]}
        rendered = fleet_table(result)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\n\x1b", rendered)
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
                self.assertFalse(result["partial"])
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


if __name__ == "__main__":
    unittest.main()
