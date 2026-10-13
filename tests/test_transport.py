import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_runtime import RuntimeHarness

from router.client import request


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
    def test_socket_permissions_and_no_auto_spawn(self):
        self.assertEqual(self.endpoint.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(request("status", {}, self.endpoint)), 0)
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        result = subprocess.run([sys.executable, "-m", "router.cli", "status"], env=self.environment, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("router daemon start", result.stderr)
        self.assertFalse(self.endpoint.exists())

    def test_restart_does_not_relaunch_live_child(self):
        task_id = self.start("hang")
        running = self.wait(task_id, {"running"})
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
