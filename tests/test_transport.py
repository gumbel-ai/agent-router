import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_runtime import RuntimeHarness

from router.client import request
from router.paths import PROJECT_MARKER, PROJECT_MARKER_CONTENT


class ClientDeadlineTests(unittest.TestCase):
    def test_total_deadline_is_opt_in_for_fragmented_responses(self):
        clock = [0.0]

        class FragmentedSocket:
            def __init__(self):
                self.fragments = iter([b'{"ok":', b'true,', b'"result":', b'{}}\n'])
                self.timeout = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def connect(self, endpoint):
                pass

            def sendall(self, data):
                pass

            def settimeout(self, seconds):
                self.timeout = seconds

            def recv(self, limit):
                clock[0] += min(.2, self.timeout)
                if self.timeout < .2:
                    raise TimeoutError("fragment exceeded remaining budget")
                return next(self.fragments, b'')[:limit]

            def makefile(self, mode):
                return self

            def readline(self, limit):
                received = b''
                while not received.endswith(b'\n'):
                    received += self.recv(limit - len(received))
                return received

        for total in (False, True):
            clock[0] = 0.0
            with self.subTest(total=total), patch("router.client.socket.socket", return_value=FragmentedSocket()), \
                    patch("router.client.time", SimpleNamespace(monotonic=lambda: clock[0])):
                if total:
                    with self.assertRaises(TimeoutError):
                        request("completion_next", {"consumer": "worker-a"}, Path("router.sock"), timeout=.5, deadline=True)
                else:
                    self.assertEqual(request("observe", {}, Path("router.sock"), timeout=.5), {})
            self.assertAlmostEqual(clock[0], .5 if total else .8)


class TransportHarness(RuntimeHarness):
    # Exercise the same lifecycle over a real separate daemon and Unix socket.
    def setUp(self):
        super().setUp()
        self.runtime.stop()
        # The separate daemon records only sessions seen inside an observed project.
        (self.repo / PROJECT_MARKER).write_text(json.dumps(PROJECT_MARKER_CONTENT))
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
    def completion_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "router.cli", *args], env=self.environment,
                              capture_output=True, text=True, timeout=15, check=False)

    def test_completion_wait_replays_offline_evidence_and_lost_ack_replies(self):
        # A consumer subscribes even when it times out before any command starts.
        initial = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "2")
        self.assertEqual(json.loads(initial.stdout)["status"], "deadline")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        completed = subprocess.run([sys.executable, "-m", "router.cli", "run", "--label", "batch-job", "--",
                                    sys.executable, "-c", "raise SystemExit(3)"], env=self.environment,
                                   cwd=self.repo, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 3)
        unavailable = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5")
        self.assertEqual((unavailable.returncode, json.loads(unavailable.stdout)["status"]), (1, "unavailable"))
        self.boot()
        first = json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5").stdout)
        self.assertEqual(first["status"], "failed")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        self.boot()
        self.assertEqual(json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5").stdout), first)
        event_id = first["event"]["event_id"]
        # Discard the first ack reply, then retry with a new consumer process.
        request("completion_ack", {"consumer": "worker-a", "event_id": event_id}, self.endpoint)
        self.assertTrue(json.loads(self.completion_cli("ack", "--consumer", "worker-a", event_id).stdout)["acknowledged"])
        self.assertEqual(json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "2").stdout)["status"], "deadline")

    def test_deadline_and_disconnected_waiter_do_not_change_running_command(self):
        release = self.root / "release"
        started = self.root / "started"
        script = ("import pathlib,time; "
                  f"p=pathlib.Path({str(started)!r}); f=p.open('a'); f.write('once'); f.close(); "
                  f"release=pathlib.Path({str(release)!r}); "
                  "exec('while not release.exists(): time.sleep(.02)')")
        command = subprocess.Popen([sys.executable, "-m", "router.cli", "run", "--", sys.executable, "-c", script],
                                   cwd=self.repo, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(command.communicate)
        self.addCleanup(lambda: command.wait(timeout=15))
        self.addCleanup(lambda: release.touch())
        deadline = time.monotonic() + 15
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(started.exists())
        timed_out = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "2")
        self.assertEqual(json.loads(timed_out.stdout)["status"], "deadline")
        waiter = subprocess.Popen([sys.executable, "-m", "router.cli", "wait", "--consumer", "worker-b", "--timeout", "30"],
                                  env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # Observe admission before disconnecting, without a wall-clock scheduling guess.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with self.control.store.connect() as db:
                    admitted = db.execute("SELECT 1 FROM completion_consumers WHERE name='worker-b'").fetchone()
                if admitted:
                    break
                time.sleep(.02)
            self.assertIsNotNone(admitted)
            # Another daemon request remains usable while the consumer is waiting.
            self.assertEqual(request("status", {}, self.endpoint), [])
            waiter.terminate()
            waiter.wait(timeout=15)
            self.assertIsNone(command.poll())
            release.touch()
            event = json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "10").stdout)
            self.assertEqual(event["status"], "completed")
            self.assertEqual(command.wait(timeout=15), 0)
            self.assertEqual(started.read_text(), "once")
        finally:
            if waiter.poll() is None:
                waiter.kill()
            waiter.communicate()

    def test_socket_permissions_and_no_auto_spawn(self):
        self.assertEqual(self.endpoint.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(request("status", {}, self.endpoint)), 0)
        observed = request("observe", {"source": "claude", "session_id": "native", "agent_id": "child",
                                       "kind": "start", "observed_at": int(time.time() * 1000),
                                       "cwd": str(self.repo)}, self.endpoint)
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
