import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_runtime import RuntimeHarness

from router.client import request
from router.process import creation_identity


class TransportHarness(RuntimeHarness):
    # Exercise the same lifecycle over a real separate daemon and Unix socket.
    def setUp(self):
        super().setUp()
        self.runtime.stop()
        self.endpoint = self.root / "router.sock"
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_ROUTER_")}
        self.environment.update(AGENT_ROUTER_STATE_DIR=str(self.root / "state"), AGENT_ROUTER_SOCKET=str(self.endpoint),
                                AGENT_ROUTER_ENV_FILE=str(self.root / "missing.env"),
                                AGENT_ROUTER_CONCURRENCY_CAP="3", AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS="30",
                                AGENT_ROUTER_MAX_ATTEMPTS="2", AGENT_ROUTER_OPENCODE_COMMAND=json.dumps(self.config.opencode_command),
                                AGENT_ROUTER_MODELS=json.dumps(self.config.models),
                                AGENT_ROUTER_REPOSITORIES=json.dumps(self.config.repositories))
        self.daemons = []
        self.addCleanup(self.stop_daemons)
        self.boot()

    def boot(self):
        daemon = subprocess.Popen([sys.executable, "-m", "router.cli", "daemon", "start"], env=self.environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.daemons.append(daemon)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if daemon.poll() is not None:
                out, err = daemon.communicate()
                self.fail(f"Daemon failed to start: {out!r} {err!r}")
            try:
                request("status", {}, self.endpoint)
                return daemon
            except ValueError:
                time.sleep(0.03)
        self.fail("Daemon did not listen")

    def stop_daemons(self):
        for daemon in self.daemons:
            if daemon.poll() is None:
                try:
                    request("daemon_stop", {}, self.endpoint)
                    daemon.wait(timeout=15)
                except (ValueError, subprocess.TimeoutExpired):
                    daemon.terminate()
                    daemon.wait(timeout=5)
            daemon.communicate()

    def mutate(self, operation, payload):
        request("authorize", {"session_id": "S", "prompt_id": "P", "operation": operation, "invocation": payload}, self.endpoint)
        return request(operation, payload, self.endpoint)

    def wait(self, task_id, states, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            task = request("status", {"task_id": task_id}, self.endpoint)
            if task["status"] in states:
                return task
            time.sleep(0.04)
        self.fail(f"Task did not reach {states}: {task}")

class TransportTests(TransportHarness, unittest.TestCase):
    def test_configured_process_registration_uses_real_cli_socket_and_host_attribution(self):
        pid = os.getpid()
        pgid = os.getpgid(pid)
        identity = creation_identity(pid)
        start = identity.rsplit(":", 1)[-1] if identity.startswith("linux:") \
            else identity.removeprefix("darwin:")
        payload = {
            "request_version": "1", "managed_process_id": "configured-live",
            "label": "configured build", "pid": pid, "pgid": pgid,
            "process_start_time": start, "creation_identity": identity,
            "group_creation_identity": creation_identity(pgid),
            "executable": "pytest", "argument_count": 0, "argument_hash": "a" * 64,
            "cwd": str(self.root), "environment_allowlist": {},
            "started_at": int(time.time() * 1000),
        }
        completed = subprocess.run(
            [sys.executable, "-m", "router.cli", "process", "register", "--json",
             json.dumps(payload)],
            env=self.environment, capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        registered = json.loads(completed.stdout)
        self.assertEqual(registered["attribution"], "host")
        snapshot = request("snapshot", {"history_limit": 10}, self.endpoint)
        visible = [record for record in snapshot["managed_processes"]
                   if record["managed_process_id"] == "configured-live"]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["attribution"], "host")

        forged = {**payload, "managed_process_id": "configured-forged",
                  "session_id": "session", "agent_id": "agent"}
        with self.assertRaisesRegex(ValueError, "cannot claim"):
            request("configured_process_register", forged, self.endpoint)

        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        self.boot()
        restarted = request("snapshot", {"history_limit": 10}, self.endpoint)
        retained = [record for record in restarted["managed_processes"]
                    if record["managed_process_id"] == "configured-live"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["attribution"], "host")

    def test_managed_command_completion_survives_daemon_restart_without_argument_leak(self):
        secret = "transport-secret-argument"
        completed = subprocess.run(
            [sys.executable, "-m", "router.cli", "run", "--label", "transport-suite",
             "--session", "session-one", "--agent", "agent-one", "--",
             sys.executable, "-c", "import sys;assert sys.argv[1];raise SystemExit(7)", secret],
            env=self.environment, capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, 7, completed.stderr)
        first = request("snapshot", {"history_limit": 10}, self.endpoint)
        self.assertEqual(len(first["managed_processes"]), 1)
        record = first["managed_processes"][0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_status"], 7)
        self.assertEqual(record["attribution"], "exact")
        self.assertEqual(record["session_id"], "session-one")
        self.assertEqual(record["agent_id"], "agent-one")
        self.assertNotIn(secret, json.dumps(first))

        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        self.boot()
        restarted = request("snapshot", {"history_limit": 10}, self.endpoint)
        retained = restarted["managed_processes"][0]
        self.assertEqual(retained["managed_process_id"], record["managed_process_id"])
        self.assertEqual(retained["status"], "failed")
        self.assertEqual(retained["exit_status"], 7)

    def test_live_managed_command_and_offline_completion_survive_restarts(self):
        marker = self.root / "managed-live"
        command = (
            "from pathlib import Path;import time;"
            f"Path({str(marker)!r}).touch();time.sleep(30)"
        )
        wrapper = subprocess.Popen(
            [sys.executable, "-m", "router.cli", "run", "--label", "restart-live", "--",
             sys.executable, "-c", command],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        pgid = None
        try:
            end = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < end:
                if wrapper.poll() is not None:
                    self.fail(f"managed wrapper exited early: {wrapper.communicate()!r}")
                time.sleep(.03)
            self.assertTrue(marker.exists())

            def managed_status(expected):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    records = request("snapshot", {"history_limit": 10}, self.endpoint)["managed_processes"]
                    matches = [record for record in records if record["label"] == "restart-live"]
                    if matches and matches[0]["status"] == expected:
                        return matches[0]
                    time.sleep(.05)
                self.fail(f"managed command did not reach {expected}: {matches if 'matches' in locals() else []}")

            running = managed_status("running")
            pgid = running["pgid"]
            request("daemon_stop", {}, self.endpoint)
            self.daemons[-1].wait(timeout=15)
            self.boot()
            recovered = managed_status("running")
            self.assertEqual(recovered["managed_process_id"], running["managed_process_id"])

            request("daemon_stop", {}, self.endpoint)
            self.daemons[-1].wait(timeout=15)
            wrapper.send_signal(signal.SIGTERM)
            _, stderr = wrapper.communicate(timeout=10)
            self.assertEqual(wrapper.returncode, 128 + signal.SIGTERM, stderr)
            self.assertIn("completion evidence exists locally", stderr)

            self.boot()
            completed = managed_status("failed")
            self.assertEqual(completed["managed_process_id"], running["managed_process_id"])
            self.assertEqual(completed["signal"], signal.SIGTERM)
            self.assertTrue(completed["shutdown_was_uncertain"])
        finally:
            if wrapper.poll() is None:
                wrapper.send_signal(signal.SIGTERM)
                try:
                    wrapper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if isinstance(pgid, int) and pgid > 0:
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    wrapper.kill()
                    wrapper.wait(timeout=5)

    def test_socket_permissions_and_no_auto_spawn(self):
        self.assertEqual(self.endpoint.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(request("status", {}, self.endpoint)), 0)
        observed = request("observe", {"source": "claude", "session_id": "native", "agent_id": "child",
                                       "kind": "start", "observed_at": int(time.time() * 1000)}, self.endpoint)
        second = subprocess.run([sys.executable, "-m", "router.cli", "daemon", "start"], env=self.environment,
                                capture_output=True, text=True, timeout=15, check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(request("status", {"task_id": observed["task_id"]}, self.endpoint)["status"], "running")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        result = subprocess.run([sys.executable, "-m", "router.cli", "status"], env=self.environment, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("router daemon start", result.stderr)
        self.assertFalse(self.endpoint.exists())
        self.boot()
        self.assertEqual(request("status", {"task_id": observed["task_id"]}, self.endpoint)["status"], "unknown")

    def test_restart_does_not_relaunch_live_child(self):
        task_id = self.start("hang")
        running = self.wait(task_id, {"running"})
        with self.assertRaisesRegex(ValueError, "active Router tasks"):
            request("daemon_stop", {"only_if_idle": True}, self.endpoint)
        self.assertEqual(request("status", {"task_id": task_id}, self.endpoint)["status"], "running")
        while not (Path(running["worktree"]) / "session.json").exists():
            time.sleep(0.04)
        self.daemons[-1].kill()
        self.daemons[-1].wait(timeout=5)
        self.boot()
        recovered = request("status", {"task_id": task_id}, self.endpoint)
        self.assertEqual(recovered["attempts"], 1)
        self.assertEqual(recovered["process"], running["process"])
        self.mutate("cancel", {"task_id": task_id, "request_id": "cancel"})
        stopped = self.wait(task_id, {"cancelled", "interrupted"})
        self.assertEqual(json.loads((Path(stopped["worktree"]) / "session.json").read_text())["sessions"], [running["child_session_id"]])
        if stopped["status"] == "interrupted":
            # An unreaped reparented leader is explicitly uncertain in the spec.
            self.assertFalse(stopped["execution_stopped"])
        else:
            self.assertTrue(stopped["execution_stopped"])

if __name__ == '__main__':
    unittest.main()
