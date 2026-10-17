import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from router.db import Store
from router.dispatch import canonical
from router.managed_completion import completion_payload
from router.processes import (
    ProcessMonitor,
    _descendant_pids,
    _display_text,
    _parse_linux_stat,
)


class FakeSampler:
    def __init__(self, processes=None):
        self.processes = processes or []
        self.error = None
        self.unreadable_pids = set()

    def sample(self):
        if self.error is not None:
            raise self.error
        return [dict(process) for process in self.processes]


def process(pid, ppid=1, pgid=None, start=None, executable="python", boot="boot-a", **values):
    start = start or f"start-{pid}"
    return {
        "pid": pid, "ppid": ppid, "pgid": pgid or pid, "boot_id": boot,
        "process_start_time": start, "creation_identity": f"linux:{boot}:{start}",
        "executable": executable, "cpu_pct": values.get("cpu_pct"),
        "rss_bytes": values.get("rss_bytes", 1024),
    }


class ProcessMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-processes-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "router.sqlite3")
        self.sampler = FakeSampler()
        self.monitor = ProcessMonitor(
            self.store, self.root, "host-a", "boot-a", sampler=self.sampler,
            registration_probe=lambda *evidence: None,
        )

    def registration(self, identity="one", **values):
        pid = values.pop("pid", 101)
        pgid = values.pop("pgid", 100)
        start = values.pop("process_start_time", f"start-{pid}")
        base = {
            "request_version": "1", "managed_process_id": f"managed-{identity}",
            "label": "freeze suite",
            "pid": pid, "pgid": pgid, "process_start_time": start,
            "creation_identity": f"linux:boot-a:{start}",
            "group_creation_identity": f"linux:boot-a:start-{pgid}",
            "executable": "python", "argument_count": 2, "argument_hash": "a" * 64,
            "cwd": "/work", "environment_allowlist": {}, "started_at": 1000,
        }
        return {**base, **values}

    def write_completion(self, identity="one", **values):
        exit_code = values.pop("exit_code", 0)
        payload = completion_payload(
            exit_code, finished_at=3000, peak_rss_bytes=4096, cpu_seconds=1.5,
        )
        payload.update(values)
        self.monitor.completion_path(f"managed-{identity}").write_text(json.dumps(payload))

    def test_linux_stat_parser_and_tree_construction_are_structural(self):
        fields = ["R", "7", "8", "8", "0", "-1", "0", "1", "2", "3", "4",
                  "11", "12", "0", "0", "20", "0", "1", "0", "999"]
        parsed = _parse_linux_stat("42 (worker ) name) " + " ".join(fields))
        self.assertEqual(parsed, {
            "pid": 42, "ppid": 7, "pgid": 8, "cpu_ticks": 23,
            "process_start_time": "999",
        })
        tree = [process(1, ppid=0), process(2, ppid=1), process(3, ppid=2),
                process(4, ppid=99)]
        self.assertEqual(_descendant_pids(tree, 1), {2, 3})
        with self.assertRaisesRegex(ValueError, "Malformed"):
            _parse_linux_stat("not a stat")

    def test_registration_is_idempotent_but_identity_and_attribution_conflicts_fail(self):
        payload = self.registration(session_id="session", agent_id="agent")
        first = self.monitor.register(payload)
        again = self.monitor.register(payload)
        self.assertEqual(first["managed_process_id"], again["managed_process_id"])
        self.assertEqual(first["creation_identity"], again["creation_identity"])
        self.assertEqual(first["attribution"], again["attribution"])
        self.assertEqual(first["attribution"], "exact")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.monitor.register({**payload, "pid": 999})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.monitor.register({**payload, "agent_id": "another"})
        with self.assertRaisesRegex(ValueError, "requires session"):
            self.monitor.register(self.registration("agent-only", agent_id="agent"))
        missing_version = self.registration("missing-version", pid=351, pgid=350)
        del missing_version["request_version"]
        with self.assertRaisesRegex(ValueError, "request_version"):
            self.monitor.register(missing_version)
        missing_start = self.registration("missing-start", pid=401, pgid=400)
        del missing_start["process_start_time"]
        with self.assertRaisesRegex(ValueError, "process_start_time"):
            self.monitor.register(missing_start)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.monitor.register(self.registration(
                "mismatched-start", pid=501, pgid=500, process_start_time="invented",
                creation_identity="linux:boot-a:actual",
            ))
        session = self.monitor.register(self.registration(
            "session", pid=201, pgid=200, session_id="session",
        ))
        host = self.monitor.register(self.registration("host", pid=301, pgid=300))
        self.assertEqual((session["attribution"], host["attribution"]), ("session", "host"))

    def test_registration_verifies_live_creation_identity_and_process_group(self):
        monitor = ProcessMonitor(self.store, self.root, "host-a", "boot-a", sampler=self.sampler)
        payload = self.registration("verified")
        with patch("router.processes.creation_identity", side_effect=lambda pid: {
                101: "linux:boot-a:start-101",
                100: "linux:boot-a:start-100",
        }[pid]), patch("router.processes.os.getpgid", return_value=100):
            self.assertEqual(monitor.register(payload)["status"], "running")
        # A retry remains idempotent even if the process exits before the
        # client learns that the durable registration succeeded.
        self.assertEqual(monitor.register(payload)["managed_process_id"], "managed-verified")
        mismatched = self.registration("mismatch", pid=201, pgid=200)
        with patch("router.processes.creation_identity", return_value="linux:boot-a:other"), \
                patch("router.processes.os.getpgid", return_value=200), \
                self.assertRaisesRegex(ValueError, "does not match"):
            monitor.register(mismatched)

    def test_configured_registration_is_host_attributed_and_rejects_claimed_ownership(self):
        host = self.monitor.register(
            self.registration("configured"), registration_kind="configured",
        )
        self.assertEqual(host["attribution"], "host")
        self.assertIsNone(host["session_id"])
        self.assertIsNone(host["agent_id"])
        with self.assertRaisesRegex(ValueError, "cannot claim"):
            self.monitor.register(
                self.registration("forged", pid=201, pgid=200,
                                  session_id="session", agent_id="agent"),
                registration_kind="configured",
            )

    def test_arguments_and_environment_values_are_never_persisted(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        payload = self.registration(
            "private", label=f"token={secret}", executable=f"https://user:{secret}@example.test/tool",
        )
        record = self.monitor.register(payload)
        database = self.store.path.read_bytes().decode(errors="ignore")
        self.assertNotIn(secret, database)
        self.assertIn("[REDACTED]", record["label"])
        self.assertIn("[REDACTED]", record["executable"])
        self.assertNotIn("arguments", record)
        self.assertEqual(record["argument_count"], 2)
        self.assertEqual(len(record["argument_hash"]), 64)
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.monitor.register({**self.registration("raw-arguments"),
                                   "arguments": ["--token", secret]})
        with self.assertRaisesRegex(ValueError, "environment_allowlist"):
            self.monitor.register(self.registration(
                "environment", environment_allowlist={"TOKEN": secret},
            ))
        with self.assertRaisesRegex(ValueError, "credential material"):
            self.monitor.register(self.registration(
                "credential-attribution", pid=301, pgid=300,
                session_id=secret, agent_id=secret,
            ))
        with self.assertRaisesRegex(ValueError, "managed_process_id"):
            self.monitor.register({
                **self.registration("credential-id", pid=401, pgid=400),
                "managed_process_id": secret,
            })
        self.assertNotIn(secret, self.store.path.read_bytes().decode(errors="ignore"))

    def test_pid_reuse_and_boot_mismatch_never_reopen_managed_execution(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [process(101, pgid=100, start="different")]
        self.assertTrue(self.monitor.sample_once())
        record = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "unknown")
        self.assertTrue(record["shutdown_uncertain"])
        restarted = ProcessMonitor(
            self.store, self.root, "host-a", "boot-b",
            sampler=FakeSampler([process(101, pgid=100, start="start-101", boot="boot-b")]),
        )
        restarted.reconcile_restart()
        record = restarted.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "unknown")
        self.assertTrue(record["shutdown_was_uncertain"])

    def test_known_descendant_identity_does_not_collide_across_boots(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(101, ppid=100, pgid=100),
            process(102, ppid=101, pgid=100),
        ]
        self.monitor.sample_once()
        self.write_completion()
        restarted = ProcessMonitor(
            self.store, self.root, "host-a", "boot-b",
            sampler=FakeSampler([process(102, ppid=1, pgid=999, boot="boot-b")]),
            registration_probe=lambda *evidence: None,
        )
        with patch("router.processes.now_ms", return_value=4000):
            restarted.reconcile_restart()
            record = restarted.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "stopped")
        self.assertFalse(record["shutdown_uncertain"])

    def test_completion_records_exit_status_signal_and_resource_evidence(self):
        self.monitor.register(self.registration())
        self.write_completion()
        completed = self.monitor.complete("managed-one")
        self.assertEqual(completed["status"], "stopped")
        self.assertEqual(completed["exit_status"], 0)
        self.assertIsNone(completed["signal"])
        self.assertEqual(completed["peak_rss_bytes"], 4096)
        self.assertEqual(completed["cpu_seconds"], 1.5)
        self.assertEqual(self.monitor.complete("managed-one")["status"], "stopped")

        self.monitor.register(self.registration("signal", pid=201, pgid=200))
        self.write_completion("signal", exit_code=-15, exit_status=None, signal=15)
        signalled = self.monitor.complete("managed-signal")
        self.assertEqual(signalled["status"], "failed")
        self.assertEqual(signalled["signal"], 15)
        self.assertIsNone(signalled["exit_status"])

    def test_sampler_completion_commit_removes_file_and_enforces_history_cap(self):
        with patch("router.processes.MAX_TERMINAL_RECORDS", 2):
            for number in range(4):
                self.monitor.register(self.registration(
                    str(number), pid=101 + number, pgid=201 + number,
                ))
                self.write_completion(str(number))
            self.assertTrue(self.monitor.sample_once())
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT status FROM managed_processes ORDER BY sort_at DESC"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "stopped" for row in rows))
        self.assertEqual(list((self.root / "managed").glob("*.completion.json")), [])

    def test_managed_resources_aggregate_observed_workers(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(101, ppid=100, pgid=100, cpu_pct=1, rss_bytes=100),
            process(102, ppid=101, pgid=100, cpu_pct=80, rss_bytes=8000),
        ]
        self.monitor.sample_once()
        record = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(record["cpu_pct"], 81)
        self.assertEqual(record["rss_bytes"], 8100)
        self.assertEqual(record["worker_count"], 1)
        self.assertEqual(self.monitor.inferred(10), [])

    def test_incomplete_worker_metrics_do_not_understate_aggregate_usage(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(101, ppid=100, pgid=100, cpu_pct=1, rss_bytes=100),
            process(102, ppid=101, pgid=100, cpu_pct=None, rss_bytes=None),
        ]
        self.assertTrue(self.monitor.sample_once())
        record = self.monitor.fleet_records(10)[0][0]
        self.assertIsNone(record["cpu_pct"])
        self.assertIsNone(record["rss_bytes"])
        self.assertEqual(record["worker_count"], 1)
        self.assertIn("incomplete", record["visibility_reason"])
        self.assertTrue(self.monitor.health()["partial"])

    def test_unreadable_managed_leader_remains_reconcilable_and_marks_health_partial(self):
        self.monitor.register(
            self.registration(pgid=101), registration_kind="configured",
        )
        self.sampler.unreadable_pids = {101}
        self.assertTrue(self.monitor.sample_once())
        record = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "unknown")
        self.assertIn("unreadable", record["visibility_reason"])
        self.assertTrue(self.monitor.health()["partial"])
        with self.store.connect() as db:
            active = db.execute(
                "SELECT monitor_active FROM managed_processes"
            ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_dormant_unknown_rows_are_not_decoded_on_every_sample(self):
        self.monitor.register(self.registration("dormant"))
        self.monitor.sample_once()
        self.monitor.register(self.registration("running", pid=201, pgid=200))
        self.sampler.processes = [process(201, pgid=200, cpu_pct=1, rss_bytes=100)]
        with patch.object(
                self.monitor, "_apply_managed_sample",
                wraps=self.monitor._apply_managed_sample,
        ) as apply_sample:
            self.monitor.sample_once()
        self.assertEqual(apply_sample.call_count, 1)
        self.assertEqual(
            apply_sample.call_args.args[0]["managed_process_id"], "managed-running",
        )

    def test_late_completion_reactivates_dormant_unknown_without_finish_request(self):
        self.monitor.register(self.registration())
        self.monitor.sample_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT monitor_active FROM managed_processes"
            ).fetchone()[0], 0)
        self.write_completion()
        with patch("router.processes.now_ms", return_value=4000):
            self.monitor.sample_once()
            record = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "stopped")
        self.assertFalse(self.monitor.completion_path("managed-one").exists())

    def test_completion_file_removal_race_returns_committed_terminal_record(self):
        self.monitor.register(self.registration())

        def reconcile(_record):
            with self.store.transaction() as db:
                current = self.monitor._get(db, "managed-one")
                current.update(
                    status="stopped", finished_at=3000, exit_status=0, signal=None,
                    shutdown_uncertain=False, _uncertain_process_live=False,
                    _needs_reconciliation=False,
                )
                self.monitor._put(db, current)

        with patch.object(self.monitor, "_read_completion", side_effect=reconcile):
            self.assertEqual(self.monitor.complete("managed-one")["status"], "stopped")

    def test_kernel_executable_is_redacted_before_display_bound(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        displayed = _display_text("x" * 1988 + secret, 2000)
        self.assertNotIn(secret, displayed)
        self.assertNotIn("ghp_abcdefgh", displayed)
        self.assertIn("[REDACTED]", displayed)

    def test_unreadable_known_descendant_prevents_false_completion(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(101, ppid=100, pgid=100),
            process(102, ppid=101, pgid=100),
        ]
        self.monitor.sample_once()
        self.write_completion()
        self.sampler.processes = []
        self.sampler.unreadable_pids = {102}
        uncertain = self.monitor.complete("managed-one")
        self.assertEqual(uncertain["status"], "unknown")
        self.assertTrue(uncertain["shutdown_uncertain"])
        self.assertIn("descendant", uncertain["visibility_reason"])

        self.sampler.unreadable_pids = set()
        with patch("router.processes.now_ms", return_value=4000):
            self.monitor.sample_once()
            self.assertEqual(self.monitor.fleet_records(10)[0][0]["status"], "stopped")

    def test_first_sample_group_survivor_keeps_completion_evidence_uncertain(self):
        self.monitor.register(self.registration())
        self.write_completion(exit_code=7, exit_status=7, signal=None)
        self.sampler.processes = [process(102, ppid=1, pgid=100)]
        uncertain = self.monitor.complete("managed-one")
        self.assertEqual(uncertain["status"], "unknown")
        self.assertTrue(uncertain["shutdown_uncertain"])
        self.assertEqual(uncertain["exit_status"], 7)
        self.assertEqual(uncertain["finished_at"], 3000)
        self.assertEqual(uncertain["peak_rss_bytes"], 4096)
        self.assertEqual(uncertain["cpu_seconds"], 1.5)

        self.sampler.processes = []
        with patch("router.processes.now_ms", return_value=4000):
            self.monitor.sample_once()
            settled = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(settled["status"], "failed")
        self.assertFalse(settled["shutdown_uncertain"])

    def test_leaderless_unverified_group_is_not_hidden_from_inference(self):
        session = {
            "host_id": "host-a", "source": "claude", "session_id": "session-one",
            "status": "running", "claude_pid": 300,
            "claude_process_start_time": "start-300",
            "claude_creation_identity": "linux:boot-a:start-300",
            "last_event_at": 2_000_000_000_000,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "session-one", canonical(session)),
            )
        self.monitor.register(self.registration())
        self.write_completion()
        self.sampler.processes = [
            process(300, ppid=1, pgid=300, executable="claude"),
            process(999, ppid=300, pgid=100, executable="worker"),
        ]
        self.monitor.sample_once()
        managed = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(managed["status"], "unknown")
        self.assertIn(999, [record["pid"] for record in self.monitor.inferred(10)])
        self.assertTrue(self.monitor.health()["partial"])
        self.assertIn("unverified live membership", self.monitor.health()["error"])
        self.monitor.sample_once()
        self.assertTrue(self.monitor.health()["partial"])

    def test_reused_process_group_identity_does_not_block_completion(self):
        self.monitor.register(self.registration())
        self.write_completion()
        self.sampler.processes = [process(100, pgid=100, start="reused-group")]
        completed = self.monitor.complete("managed-one")
        self.assertEqual(completed["status"], "stopped")
        self.assertFalse(completed["shutdown_uncertain"])

    def test_hostile_unrelated_executable_does_not_hide_managed_visibility(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(101, ppid=100, pgid=100, cpu_pct=1),
            process(999, executable="x\n" + "z" * 3000),
        ]
        self.assertTrue(self.monitor.sample_once())
        self.assertFalse(self.monitor.health()["partial"])
        record = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "running")

    def test_trusted_completion_is_bounded_and_cannot_be_redirected(self):
        payload = self.registration()
        payload["completion_path"] = str(self.root / "outside.json")
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.monitor.register(payload)
        self.monitor.register(self.registration())
        (self.root / "outside.json").write_text(json.dumps({"exit_code": 0}))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.monitor.complete("managed-one")
        destination = self.root / "outside-completion.json"
        destination.write_text(json.dumps({"exit_code": 0, "finished_at": 3000}))
        self.monitor.completion_path("managed-one").symlink_to(destination)
        with self.assertRaises((OSError, ValueError)):
            self.monitor.complete("managed-one")
        self.monitor.completion_path("managed-one").unlink()
        os.mkfifo(self.monitor.completion_path("managed-one"), 0o600)
        with self.assertRaisesRegex(ValueError, "Invalid managed process completion"):
            self.monitor.complete("managed-one")

    def test_detached_descendant_keeps_shutdown_uncertain_until_identity_is_gone(self):
        self.monitor.register(self.registration())
        leader = process(101, ppid=100, pgid=100)
        supervisor = process(100, ppid=1, pgid=100)
        descendant = process(102, ppid=101, pgid=100)
        self.sampler.processes = [supervisor, leader, descendant]
        self.monitor.sample_once()
        self.write_completion()
        self.sampler.processes = [process(102, ppid=1, pgid=999)]
        uncertain = self.monitor.complete("managed-one")
        self.assertEqual(uncertain["status"], "unknown")
        self.assertTrue(uncertain["shutdown_uncertain"])
        self.sampler.processes = []
        with patch("router.processes.now_ms", return_value=4000):
            self.monitor.sample_once()
            stopped = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(stopped["shutdown_uncertain"])
        self.assertTrue(stopped["shutdown_was_uncertain"])

    def test_truncated_descendant_tracking_never_claims_a_clean_stop(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(100, pgid=100), process(101, ppid=100, pgid=100),
            *[process(pid, ppid=101, pgid=100) for pid in range(1000, 1600)],
        ]
        self.monitor.sample_once()
        self.write_completion()
        self.sampler.processes = []
        result = self.monitor.complete("managed-one")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["descendant_tracking_truncated"])
        self.assertTrue(result["shutdown_uncertain"])

    def test_truncated_but_unobserved_descendants_eventually_expire(self):
        with patch("router.processes.MAX_KNOWN_DESCENDANTS", 1), \
                patch("router.processes.now_ms", return_value=1000):
            self.monitor.register(self.registration())
            self.sampler.processes = [
                process(101, ppid=100, pgid=100),
                process(102, ppid=101, pgid=100),
                process(103, ppid=101, pgid=100),
            ]
            self.monitor.sample_once()
        self.write_completion()
        self.sampler.processes = []
        with patch("router.processes.now_ms", return_value=10_000):
            self.monitor.sample_once()
            uncertain = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(uncertain["status"], "unknown")
        self.assertTrue(uncertain["descendant_tracking_truncated"])

        expired_at = 10_000 + 7 * 24 * 60 * 60 * 1000 + 1
        with patch("router.processes.now_ms", return_value=expired_at):
            self.monitor.sample_once()
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0]
        self.assertEqual(count, 0)

    def test_complete_sample_resolves_prior_descendant_truncation(self):
        self.monitor.register(self.registration())
        self.sampler.processes = [
            process(100, pgid=100), process(101, ppid=100, pgid=100),
            *[process(pid, ppid=101, pgid=100) for pid in range(1000, 1600)],
        ]
        self.monitor.sample_once()
        self.sampler.processes = [process(100, pgid=100), process(101, ppid=100, pgid=100)]
        self.monitor.sample_once()
        visible = self.monitor.fleet_records(10)[0][0]
        self.assertFalse(visible["descendant_tracking_truncated"])
        self.write_completion()
        self.sampler.processes = []
        completed = self.monitor.complete("managed-one")
        self.assertEqual(completed["status"], "stopped")

    def test_inferred_processes_use_structured_session_roots_without_agent_upgrade(self):
        session = {
            "host_id": "host-a", "source": "claude", "session_id": "session-one",
            "status": "running", "claude_pid": 300,
            "claude_process_start_time": "start-300",
            "claude_creation_identity": "linux:boot-a:start-300",
            "last_event_at": 2_000_000_000_000,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "session-one", canonical(session)),
            )
        self.sampler.processes = [
            process(300, ppid=1, pgid=300, executable="claude"),
            {**process(301, ppid=300, pgid=300, executable="python"),
             "cmdline": "--token PRIVATE", "environment": {"TOKEN": "PRIVATE"}},
        ]
        self.monitor.sample_once()
        rows = self.monitor.inferred(10)
        self.assertEqual([row["pid"] for row in rows], [300, 301])
        self.assertTrue(all(row["attribution"] == "session" for row in rows))
        self.assertTrue(all(row["agent_id"] is None for row in rows))
        self.assertNotIn("PRIVATE", json.dumps(rows))
        self.assertNotIn("cmdline", rows[1])
        self.assertNotIn("environment", rows[1])

        session["claude_creation_identity"] = "linux:boot-a:different"
        with self.store.transaction() as db:
            db.execute(
                "UPDATE sessions SET data=? WHERE host_id=? AND source=? AND session_id=?",
                (canonical(session), "host-a", "claude", "session-one"),
            )
        self.monitor.sample_once()
        self.assertEqual(self.monitor.inferred(10), [])
        self.assertTrue(self.monitor.health()["partial"])
        self.assertIn("does not match", self.monitor.health()["error"])

    def test_unreadable_session_root_marks_process_visibility_partial(self):
        session = {
            "host_id": "host-a", "source": "claude", "session_id": "session-one",
            "status": "running", "claude_pid": 300,
            "claude_process_start_time": "start-300",
            "claude_creation_identity": "linux:boot-a:start-300",
            "last_event_at": 2_000_000_000_000,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "session-one", canonical(session)),
            )
        self.sampler.unreadable_pids = {300}
        self.assertTrue(self.monitor.sample_once())
        self.assertEqual(self.monitor.inferred(10), [])
        health = self.monitor.health()
        self.assertTrue(health["partial"])
        self.assertIn("session root", health["error"])

    def test_stale_sessions_without_process_evidence_do_not_poison_sampler_health(self):
        with self.store.transaction() as db:
            for number in range(1000):
                session = {
                    "host_id": "host-a", "source": "claude",
                    "session_id": f"stale-{number}", "status": "unknown",
                    "claude_pid": None, "claude_process_start_time": None,
                    "claude_creation_identity": None, "last_event_at": 1000,
                }
                db.execute(
                    "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                    ("host-a", "claude", session["session_id"], canonical(session)),
                )
        self.assertTrue(self.monitor.sample_once())
        self.assertFalse(self.monitor.health()["partial"])

    def test_configured_process_does_not_absorb_shared_group_siblings(self):
        session = {
            "host_id": "host-a", "source": "claude", "session_id": "session-one",
            "status": "running", "claude_pid": 300,
            "claude_process_start_time": "start-300",
            "claude_creation_identity": "linux:boot-a:start-300", "last_event_at": 1000,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "session-one", canonical(session)),
            )
        self.sampler.processes = [
            process(300, ppid=1, pgid=300, executable="claude", cpu_pct=1, rss_bytes=100),
            process(301, ppid=300, pgid=300, cpu_pct=2, rss_bytes=200),
            process(302, ppid=300, pgid=300, cpu_pct=40, rss_bytes=4000),
            process(303, ppid=301, pgid=300, cpu_pct=3, rss_bytes=300),
        ]
        self.monitor.sample_once()
        self.assertEqual(
            [record["pid"] for record in self.monitor.inferred(10)],
            [300, 301, 302, 303],
        )
        self.monitor.register(
            self.registration("configured", pid=301, pgid=300),
            registration_kind="configured",
        )
        self.assertEqual(
            [record["pid"] for record in self.monitor.inferred(10)], [300, 302],
        )
        self.monitor.sample_once()
        managed = self.monitor.fleet_records(10)[0][0]
        self.assertEqual(
            (managed["cpu_pct"], managed["rss_bytes"], managed["worker_count"]),
            (5, 500, 1),
        )
        self.assertIn(302, [record["pid"] for record in self.monitor.inferred(10)])

    def test_inference_ignores_terminal_session_history(self):
        with self.store.transaction() as db:
            for number in range(1000):
                session = {
                    "host_id": "host-a", "source": "claude", "session_id": f"old-{number}",
                    "status": "stopped", "claude_pid": 10_000 + number,
                    "claude_process_start_time": str(number),
                    "claude_creation_identity": f"linux:boot-a:{number}",
                }
                db.execute(
                    "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                    ("host-a", "claude", session["session_id"], canonical(session)),
                )
            active = {
                "host_id": "host-a", "source": "claude", "session_id": "active",
                "status": "running", "claude_pid": 300,
                "claude_process_start_time": "start-300",
                "claude_creation_identity": "linux:boot-a:start-300",
            }
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "active", canonical(active)),
            )
        self.sampler.processes = [process(300, executable="claude")]
        self.monitor.sample_once()
        self.assertEqual([record["session_id"] for record in self.monitor.inferred(10)], ["active"])

    def test_managed_group_is_not_duplicated_as_inferred_session_processes(self):
        session = {
            "host_id": "host-a", "source": "claude", "session_id": "session-one",
            "status": "running", "claude_pid": 300,
            "claude_process_start_time": "start-300",
            "claude_creation_identity": "linux:boot-a:start-300", "last_event_at": 1000,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "session-one", canonical(session)),
            )
        self.monitor.register(self.registration(
            session_id="session-one", agent_id="agent-one", pid=301, pgid=301,
        ))
        self.sampler.processes = [
            process(300, ppid=1, pgid=300, executable="claude"),
            process(301, ppid=300, pgid=301, executable="python"),
            process(302, ppid=301, pgid=301, executable="worker"),
        ]
        self.monitor.sample_once()
        self.assertEqual([record["pid"] for record in self.monitor.inferred(10)], [300])

        self.sampler.processes = [
            process(300, ppid=1, pgid=300, executable="claude"),
            process(301, ppid=300, pgid=301, start="reused-group", executable="python"),
        ]
        self.monitor.sample_once()
        self.assertEqual([record["pid"] for record in self.monitor.inferred(10)], [300, 301])

    def test_registration_immediately_removes_stale_inferred_group(self):
        session = {
            "host_id": "host-a", "source": "claude", "session_id": "session-one",
            "status": "running", "claude_pid": 300,
            "claude_process_start_time": "start-300",
            "claude_creation_identity": "linux:boot-a:start-300", "last_event_at": 1000,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)",
                ("host-a", "claude", "session-one", canonical(session)),
            )
        self.sampler.processes = [
            process(300, ppid=1, pgid=300, executable="claude"),
            process(301, ppid=300, pgid=301, executable="python"),
            process(302, ppid=301, pgid=301, executable="worker"),
        ]
        self.monitor.sample_once()
        self.assertEqual([record["pid"] for record in self.monitor.inferred(10)], [300, 301, 302])
        self.monitor.register(self.registration(
            session_id="session-one", agent_id="agent-one", pid=301, pgid=301,
        ))
        self.assertEqual([record["pid"] for record in self.monitor.inferred(10)], [300])

    def test_active_total_is_independent_of_display_limit(self):
        for number in range(3):
            self.monitor.register(self.registration(str(number), pid=101 + number, pgid=100 + number))
        rows, active = self.monitor.fleet_records(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(active, 3)
        self.assertEqual(self.monitor.fleet_records(0), ([], 3))

    def test_sampler_failure_is_explicit_and_preserves_managed_evidence(self):
        registered = self.monitor.register(self.registration())
        self.sampler.error = RuntimeError("kernel sampler unavailable")
        self.assertFalse(self.monitor.sample_once())
        rows, active = self.monitor.fleet_records(10)
        self.assertEqual(rows[0]["managed_process_id"], registered["managed_process_id"])
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(active, 1)
        self.assertTrue(self.monitor.health()["partial"])
        self.assertIn("kernel sampler unavailable", self.monitor.health()["error"])
        self.assertEqual(self.monitor.inferred(10), [])

    def test_completion_sampler_failure_does_not_claim_stopped(self):
        self.monitor.register(self.registration())
        self.write_completion()
        self.sampler.error = OSError("permission denied")
        result = self.monitor.complete("managed-one")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["shutdown_uncertain"])
        self.assertEqual(result["exit_status"], 0)
        self.assertEqual(result["finished_at"], 3000)
        self.assertEqual(result["peak_rss_bytes"], 4096)
        self.assertEqual(result["cpu_seconds"], 1.5)
        self.assertTrue(self.monitor.health()["partial"])

    def test_terminal_history_and_completion_files_are_bounded(self):
        with patch("router.processes.MAX_TERMINAL_RECORDS", 2):
            for number in range(4):
                identity = str(number)
                self.monitor.register(self.registration(
                    identity, pid=101 + number, pgid=100 + number,
                ))
                self.write_completion(identity)
                self.monitor.complete(f"managed-{identity}")
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(list((self.root / "managed").glob("*.completion.json")), [])

    def test_missing_process_uncertainty_expires_but_live_descendant_does_not(self):
        self.monitor.register(self.registration("missing"))
        self.monitor.register(self.registration("live", pid=201, pgid=200))
        self.sampler.processes = [
            process(201, ppid=200, pgid=200),
            process(202, ppid=201, pgid=200),
        ]
        with patch("router.processes.now_ms", return_value=10_000):
            self.monitor.sample_once()
        self.sampler.processes = [process(202, ppid=1, pgid=999)]
        expired_at = 10_000 + 7 * 24 * 60 * 60 * 1000 + 1
        with patch("router.processes.now_ms", return_value=expired_at):
            self.monitor.sample_once()
        rows, active = self.monitor.fleet_records(10)
        self.assertEqual([row["managed_process_id"] for row in rows], ["managed-live"])
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(active, 0)
        self.assertTrue(self.monitor.health()["partial"])
        self.assertIn("members are live", self.monitor.health()["error"])

    def test_missing_process_uncertainty_is_count_bounded(self):
        with patch("router.processes.MAX_UNCERTAIN_RECORDS", 2), \
                patch("router.processes.now_ms", return_value=10_000):
            for number in range(4):
                self.monitor.register(self.registration(
                    str(number), pid=101 + number, pgid=201 + number,
                ))
            self.monitor.sample_once()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT status FROM managed_processes ORDER BY sort_at DESC"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "unknown" for row in rows))

    def test_sampler_thread_uses_configured_interval_and_stops(self):
        monitor = ProcessMonitor(
            self.store, self.root, "host-a", "boot-a", interval_seconds=.01,
            sampler=self.sampler,
        )
        with patch.object(monitor, "sample_once", wraps=monitor.sample_once) as sampled:
            monitor.start()
            for _ in range(100):
                if sampled.call_count:
                    break
                threading.Event().wait(.002)
            monitor.stop()
        self.assertGreaterEqual(sampled.call_count, 1)

    def test_sampler_thread_survives_startup_failure_with_partial_health(self):
        self.sampler.error = OSError("startup permission denied")
        monitor = ProcessMonitor(
            self.store, self.root, "host-a", "boot-a", interval_seconds=.01,
            sampler=self.sampler,
        )
        monitor.start()
        try:
            for _ in range(100):
                if monitor.health()["observed_at"] is not None:
                    break
                threading.Event().wait(.002)
            health = monitor.health()
            self.assertTrue(health["partial"])
            self.assertIn("startup permission denied", health["error"])
            self.assertTrue(monitor._thread.is_alive())
        finally:
            monitor.stop()

    def test_sampler_shutdown_timeout_does_not_allow_duplicate_thread(self):
        class StuckThread:
            alive = True

            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return self.alive

        monitor = ProcessMonitor(
            self.store, self.root, "host-a", "boot-a", interval_seconds=.01,
            sampler=self.sampler,
        )
        stuck = StuckThread()
        monitor._thread = stuck
        monitor.stop()
        self.assertIs(monitor._thread, stuck)
        self.assertTrue(monitor.health()["partial"])
        with self.assertRaisesRegex(RuntimeError, "shutdown"):
            monitor.start()
        self.assertIs(monitor._thread, stuck)

        stuck.alive = False
        monitor.start()
        self.assertIsNot(monitor._thread, stuck)
        monitor.stop()


if __name__ == "__main__":
    unittest.main()
