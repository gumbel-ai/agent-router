import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from router.cli import fleet_table
from router.client import request
from router.fleet import FleetConfig, FleetHost, query_fleet


class ManagedCommandSmokeTest(unittest.TestCase):
    """router run through the real CLI and daemon, read back through snapshot and fleet views."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-smoke-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        state = root / "state"
        state.mkdir(mode=0o700)
        self.socket = root / "r.sock"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_ROUTER_")}
        home = root / "home"
        home.mkdir()
        # Isolated from the developer's own Claude and Codex sessions; runnable from any directory.
        self.environment.update(AGENT_ROUTER_STATE_DIR=str(state), AGENT_ROUTER_SOCKET=str(self.socket),
                                AGENT_ROUTER_ENV_FILE=str(root / "none.env"), HOME=str(home),
                                PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        self.daemon = subprocess.Popen([sys.executable, "-m", "router.cli", "daemon", "start"], env=self.environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_daemon)
        deadline = time.monotonic() + 30
        while not self.socket.exists() and self.daemon.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.socket.exists():
            self.fail(f"daemon did not start: {self.daemon.stderr.read() if self.daemon.poll() is not None else ''}")

    def stop_daemon(self):
        if self.daemon.poll() is None:
            try:
                request("daemon_stop", {}, self.socket)
                self.daemon.wait(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.daemon.kill()
                self.daemon.wait()
        self.daemon.stderr.close()

    def router_run(self, label, script):
        return subprocess.run([sys.executable, "-m", "router.cli", "run", "--label", label, "--session", "S",
                               "--agent", "A", "--", sys.executable, "-c", script],
                              env=self.environment, capture_output=True, text=True, timeout=60, check=False)

    def test_short_and_failing_managed_commands_reach_fleet_status(self):
        self.assertEqual(self.router_run("smoke-ok", "print('done')").returncode, 0)
        failed = self.router_run("smoke-fail", "raise SystemExit(5)")
        self.assertEqual(failed.returncode, 5, failed.stderr)
        snapshot = request("snapshot", {}, self.socket)
        managed = {record["label"]: record for record in snapshot["managed_processes"]}
        self.assertEqual({label: (record["status"], record["attribution"]) for label, record in managed.items()},
                         {"smoke-ok": ("exited", "exact"), "smoke-fail": ("failed", "exact")})
        self.assertEqual([alert["entity"] for alert in snapshot["alerts"] if alert["kind"] == "managed_process_failed"],
                         [{"managed_id": managed["smoke-fail"]["managed_id"]}])
        self.assertNotIn("SystemExit", json.dumps(snapshot))  # Argument text never leaves the wrapper.
        result = query_fleet(FleetConfig((FleetHost("local", "local"),)),
                             local_snapshot=lambda thresholds: request("snapshot", {}, self.socket))
        table = fleet_table(result)
        self.assertIn("smoke-fail", table)
        self.assertIn("exit 5", table)
        self.assertIn("managed_process_failed", table)


class ProcessViewTests(unittest.TestCase):
    def test_uncertain_outcomes_alert_and_session_processes_summarize(self):
        from router.capacity import CapacityThresholds
        from router.fleet_snapshot import alerts

        host = {"host_id": "h"}
        managed = [{"managed_id": "managed-a", "label": "gone", "status": "unknown", "started_at": 1,
                    "visibility_reason": "The router run wrapper ended without completion evidence"},
                   {"managed_id": "managed-b", "label": "leaky", "status": "exited", "exit_code": 0,
                    "group_outlived": True, "finished_at": 2},
                   {"managed_id": "managed-c", "label": "fine", "status": "exited", "exit_code": 0, "finished_at": 3}]
        found = alerts(host, {}, [], [], CapacityThresholds(), 100, 10, [*managed, {"status": "failed"}])
        self.assertEqual(sorted((alert["kind"], alert["entity"]["managed_id"]) for alert in found),
                         [("managed_process_shutdown_uncertain", "managed-a"),
                          ("managed_process_shutdown_uncertain", "managed-b")])
        # Many uncertain agents cannot crowd out a managed command's outcome.
        agents = [{"source": "claude", "parent_session_id": "s", "agent_id": f"a{index}", "status": "unknown"}
                  for index in range(200)]
        crowded = alerts(host, {}, [], agents, CapacityThresholds(), 101, 10, managed)
        self.assertEqual([alert["kind"] for alert in crowded[:2]], ["managed_process_shutdown_uncertain"] * 2)
        snapshot = {"capacity": {}, "sessions": [], "agents": [], "alerts": [], "managed_processes": managed,
                    "inferred_processes": [
                        {"pid": 1, "source": "claude", "session_id": "s", "cpu_pct": 12.5, "rss_bytes": 1024 ** 2},
                        {"pid": 2, "source": "claude", "session_id": "s", "cpu_pct": None, "rss_bytes": 1024 ** 2}]}
        table = fleet_table({"hosts": [{"name": "here", "ok": True, "snapshot": snapshot}]})
        self.assertIn("session processes: 2 in 1 session(s), CPU 12% + 1 unmeasured, RSS 2.0M", table)
        self.assertIn("The router run wrapper", table)
        # Only process evidence (another section failed): the summary still shows, CPU unknown until measured.
        only = {"capacity": {}, "sessions": [], "agents": [], "alerts": [], "partial": True,
                "errors": {"sessions": "failed"},
                "inferred_processes": [{"pid": 3, "source": "codex", "session_id": "t", "cpu_pct": None,
                                        "rss_bytes": 1024}]}
        table = fleet_table({"hosts": [{"name": "here", "ok": True, "snapshot": only}]})
        self.assertIn("session processes: 1 in 1 session(s), CPU unknown, RSS 1.0K", table)
        truncated = {**only, "errors": {"inferred_processes": "Collection truncated to history_limit=1"},
                     "inferred_processes": [{**only["inferred_processes"][0], "rss_bytes": None}]}
        self.assertIn("session processes (shown only): 1 in 1 session(s), CPU unknown, RSS unknown",
                      fleet_table({"hosts": [{"name": "h", "ok": True, "snapshot": truncated}]}))
        measured_zero = {**only, "inferred_processes": [{**only["inferred_processes"][0], "cpu_pct": 0.0}]}
        self.assertIn("CPU 0%, RSS", fleet_table({"hosts": [{"name": "h", "ok": True, "snapshot": measured_zero}]}))


if __name__ == "__main__":
    unittest.main()
