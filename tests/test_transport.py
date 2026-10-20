import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_control import settings
from test_runtime import RuntimeHarness

from router.client import request
from router.daemon import Controller
from router.managed import run
from router.paths import PROJECT_MARKER, PROJECT_MARKER_CONTENT
from router.processes import parent_and_identity


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
        for operation, error in (("completion_next", ConnectionResetError), ("observe", ValueError)):
            connection = FragmentedSocket()
            connection.fragments = iter([])
            with self.subTest(operation=operation), patch("router.client.socket.socket", return_value=connection), \
                    self.assertRaises(error):
                request(operation, {}, Path("router.sock"))
        from router.managed import wait
        pending = {"status": "completed", "host_id": "host-a", "consumer": "worker-a",
                   "event": {"host_id": "host-a", "event_id": "completion-1"}}
        reply = (json.dumps({"ok": True, "result": pending}) + "\n").encode()

        def advance(seconds):
            clock[0] += seconds

        timer = SimpleNamespace(monotonic=lambda: clock[0], sleep=advance)
        binding = {"consumer": "worker-a", "subscription_id": "subscription-a", "host_id": "host-a"}
        # Every cut before the newline is a lost reply, including a complete JSON value at EOF.
        for cut in range(len(reply)):
            clock[0] = 0.0
            lost, recovered = FragmentedSocket(), FragmentedSocket()
            subscribed = FragmentedSocket()
            subscribed.fragments = iter([(json.dumps({"ok": True, "result": binding}) + "\n").encode()])
            lost.fragments, recovered.fragments = iter([reply[:cut]]), iter([reply])
            with self.subTest(cut=cut), patch("router.client.socket.socket", side_effect=[subscribed, lost, recovered]), \
                    patch("router.client.time", timer), patch("router.managed.time", timer), redirect_stderr(io.StringIO()):
                self.assertEqual(wait("worker-a", 5), pending)
        malformed = FragmentedSocket()
        subscribed = FragmentedSocket()
        subscribed.fragments = iter([(json.dumps({"ok": True, "result": binding}) + "\n").encode()])
        malformed.fragments = iter([b'not-json\n'])
        with patch("router.client.socket.socket", side_effect=[subscribed, malformed]), redirect_stderr(io.StringIO()):
            self.assertEqual(wait("worker-a", 5)["status"], "unavailable")


class MaintenanceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-maintenance-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        repo = root / "repo"
        repo.mkdir()
        self.instance = {
            "schema_version": 1,
            "pid": os.getpid(),
            "creation_identity": parent_and_identity(os.getpid())[1],
            "source": {
                "status": "verified",
                "scheme": "python-record-v1",
                "distribution": "agent-router",
                "version": "1.2.3",
                "commit": "a" * 40,
                "content_sha256": "b" * 64,
            },
        }
        self.control = Controller(settings(repo), {}, root / "state", self.instance)

    def test_native_pre_exec_claim_blocks_atomic_stop_until_exact_abort(self):
        claim = {
            "reservation_id": "reservation-a",
            "source": "claude",
            "pid": os.getpid(),
            "creation_identity": parent_and_identity(os.getpid())[1],
        }
        health = self.control.handle("health", {})
        self.assertEqual(health["instance"], self.instance)
        self.assertEqual(
            health["maintenance"],
            {
                "schema_version": 1,
                "atomic_stop": True,
                "native_launch_reservation": True,
            },
        )
        reserved = self.control.handle("native_launch_reserve", claim)
        self.assertEqual(reserved["instance"], self.instance)
        with self.assertRaisesRegex(ValueError, "Active or uncertain work"):
            self.control.stop_for_maintenance(
                {
                    "only_if_idle": True,
                    "expected_instance": self.instance,
                }
            )
        self.assertFalse(self.control.draining)
        with self.assertRaises(ValueError):
            self.control.handle(
                "native_launch_abort",
                {
                    "reservation_id": claim["reservation_id"],
                    "pid": claim["pid"],
                    "creation_identity": "wrong",
                },
            )
        self.control.handle(
            "native_launch_abort",
            {key: claim[key] for key in ("reservation_id", "pid", "creation_identity")},
        )
        malformed = self.control.native_launches.directory / "malformed.json"
        malformed.write_text("[]")
        with self.assertRaisesRegex(ValueError, "evidence is invalid"):
            self.control.stop_for_maintenance(
                {"only_if_idle": True, "expected_instance": self.instance}
            )
        self.assertFalse(self.control.draining)
        malformed.unlink()
        result = self.control.stop_for_maintenance(
            {
                "only_if_idle": True,
                "expected_instance": self.instance,
            }
        )
        self.assertTrue(result["admissions_closed"])
        self.assertEqual(result["instance"], self.instance)
        self.assertFalse(any(result["workload"].values()))

    def test_direct_observation_in_flight_refuses_atomic_stop(self):
        entered = threading.Event()
        release = threading.Event()

        def in_scope(_events):
            entered.set()
            self.assertTrue(release.wait(5))
            return False

        with (
            patch.object(self.control, "in_scope", side_effect=in_scope),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            observation = executor.submit(
                self.control.handle,
                "observe",
                {
                    "source": "claude",
                    "session_id": "session-a",
                    "kind": "activity",
                    "observed_at": int(time.time() * 1000),
                },
            )
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, "Lifecycle observation liveness"):
                self.control.stop_for_maintenance(
                    {
                        "only_if_idle": True,
                        "expected_instance": self.instance,
                    }
                )
            self.assertFalse(self.control.draining)
            release.set()
            self.assertFalse(observation.result(timeout=5)["observed"])

    def test_drain_rejects_legacy_managed_start_admission(self):
        self.control.draining = True
        start = {
            "managed_id": "managed-legacy",
            "attribution": "host",
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "wrapper_pid": os.getpid(),
            "wrapper_identity": parent_and_identity(os.getpid())[1],
            "argument_count": 0,
            "started_at": int(time.time() * 1000),
        }
        with self.assertRaisesRegex(ValueError, "managed start was not admitted"):
            self.control.handle("managed_start", start)
        self.assertEqual(self.control.managed.records(), ([], 0))

    def test_completed_history_at_display_limit_does_not_block_idle(self):
        completed = [{"status": "stopped", "session_id": f"old-{index}"}
                     for index in range(1000)]
        with patch.object(self.control.sessions, "fleet_records", return_value=(completed, 0)), \
                patch.object(self.control.native, "fleet_records", return_value=([], 0)), \
                patch.object(self.control.managed, "records", return_value=([], 0)), \
                patch.object(self.control, "inferred_process_records", return_value=[]):
            workload = self.control.maintenance_workload(None)
        self.assertFalse(any(workload.values()))

    def test_current_root_exclusion_does_not_exclude_inferred_children(self):
        current = {"source": "codex", "session_id": "session-a"}
        root = {"source": "codex", "session_id": "session-a", "status": "running"}
        child = {"source": "codex", "session_id": "session-a", "status": "running"}
        with (
            patch.object(
                self.control.sessions, "fleet_records", return_value=([root], 1)
            ),
            patch.object(self.control.native, "fleet_records", return_value=([], 0)),
            patch.object(self.control.managed, "records", return_value=([], 0)),
            patch.object(
                self.control, "inferred_process_records", return_value=[child]
            ),
        ):
            workload = self.control.maintenance_workload(current)
        self.assertEqual(workload["sessions"], 0)
        self.assertEqual(workload["inferred_processes"], 1)


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

    def test_consumer_cli_removal_stops_wait_and_new_wait_resubscribes(self):
        initial = self.completion_cli("consumer", "list")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertEqual(json.loads(initial.stdout)["consumers"], [])
        waiter = subprocess.Popen([sys.executable, "-m", "router.cli", "wait", "--consumer", "worker-a", "--timeout", "30"],
                                  env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            end = time.monotonic() + 15
            while time.monotonic() < end:
                listing = json.loads(self.completion_cli("consumer", "list").stdout)
                if listing["consumers"]:
                    break
                time.sleep(.02)
            self.assertEqual([row["name"] for row in listing["consumers"]], ["worker-a"])
            removed = self.completion_cli("consumer", "remove", "worker-a")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertTrue(json.loads(removed.stdout)["removed"])
            self.assertEqual(json.loads(self.completion_cli("consumer", "remove", "worker-a").stdout),
                             json.loads(removed.stdout))
            output, errors = waiter.communicate(timeout=15)
            self.assertEqual(waiter.returncode, 1)
            self.assertEqual(json.loads(output), {"status": "unavailable", "consumer": "worker-a"})
            self.assertIn("removed or replaced", errors)
            self.assertEqual(json.loads(self.completion_cli("consumer", "list").stdout)["consumers"], [])
            completed = subprocess.run([sys.executable, "-m", "router.cli", "run", "--", sys.executable, "-c", "pass"],
                                       cwd=self.repo, env=self.environment, capture_output=True, timeout=15, check=False)
            self.assertEqual(completed.returncode, 0)
            event = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5")
            self.assertEqual(event.returncode, 0, event.stderr)
            self.assertEqual(json.loads(event.stdout)["status"], "completed")
        finally:
            if waiter.poll() is None:
                waiter.kill()
            waiter.communicate()

    def test_locked_completion_delivery_reports_contention_and_wait_recovers(self):
        completed = subprocess.run([sys.executable, "-m", "router.cli", "run", "--", sys.executable, "-c", "pass"],
                                   cwd=self.repo, env=self.environment, capture_output=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0)
        event = request("completion_next", {"consumer": "worker-a"}, self.endpoint)
        request("completion_next", {"consumer": "worker-c"}, self.endpoint)
        request("completion_ack", {"consumer": "worker-c", "event_id": event["event"]["event_id"]}, self.endpoint)
        request("managed_start", {"managed_id": "managed-running", "pid": os.getpid(), "pgid": os.getpgid(0),
                "wrapper_pid": os.getpid(), "wrapper_identity": parent_and_identity(os.getpid())[1],
                "argument_count": 0, "started_at": int(time.time() * 1000), "attribution": "host"}, self.endpoint)
        diagnosed, diagnostics = threading.Event(), []
        waiter = None
        reader = None

        def read_diagnostic():
            diagnostics.append(waiter.stderr.readline())
            diagnosed.set()

        try:
            with self.control.store.transaction():  # A real BEGIN IMMEDIATE held in a different process.
                self.assertEqual(request("completion_next", {"consumer": "worker-a"}, self.endpoint), event)
                for operation, payload in (
                    ("completion_next", {"consumer": "worker-b"}),
                    ("completion_next", {"consumer": "worker-c"}),
                    ("completion_ack", {"consumer": "worker-a", "event_id": event["event"]["event_id"]}),
                ):
                    with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "database is busy"):
                        request(operation, payload, self.endpoint, timeout=5, deadline=True)
                waiter = subprocess.Popen([sys.executable, "-m", "router.cli", "wait", "--consumer", "worker-b",
                                           "--timeout", "30"], env=self.environment,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                reader = threading.Thread(target=read_diagnostic)
                reader.start()
                self.assertTrue(diagnosed.wait(15))  # Release only after a failed poll, without a timing guess.
                self.assertIn("database is busy", diagnostics[0])
                self.assertIsNone(waiter.poll())
            reader.join(15)
            output, errors = waiter.communicate(timeout=15)
            self.assertEqual(waiter.returncode, 0, errors)
            self.assertEqual(json.loads(output)["event"], event["event"])
            ack = self.completion_cli("ack", "--consumer", "worker-a", event["event"]["event_id"])
            self.assertEqual(ack.returncode, 0, ack.stderr)
            self.assertTrue(json.loads(ack.stdout)["acknowledged"])
            self.assertEqual(request("completion_next", {"consumer": "worker-b"}, self.endpoint)["event"], event["event"])
        finally:
            if waiter is not None:
                if waiter.poll() is None:
                    waiter.kill()
                if reader is not None:
                    reader.join(15)
                waiter.communicate()

    def test_completion_wait_replays_offline_evidence_and_lost_ack_replies(self):
        self.environment.update(ROUTER_MANAGED_ID="managed-parent", EXAMPLE_OPTION="preserved")
        identity = ("import json, os; print(json.dumps({key: os.environ.get(key) "
                    "for key in ('ROUTER_MANAGED_ID', 'EXAMPLE_OPTION')})); ")
        outside = subprocess.run([sys.executable, "-m", "router.cli", "run", "--", sys.executable, "-c",
                                  identity + "raise SystemExit(4)"], env=self.environment, cwd=self.root,
                                 capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(outside.returncode, 4)
        self.assertIn("not recording", outside.stderr)
        self.assertIn("no completion event will follow", outside.stderr)
        outside_identity = json.loads(outside.stdout)["ROUTER_MANAGED_ID"]
        self.assertTrue(outside_identity)
        self.assertNotEqual(outside_identity, "managed-parent")
        # A consumer subscribes even when it times out before any command starts.
        initial = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "2")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertEqual(json.loads(initial.stdout)["status"], "deadline")
        identity_file = self.root / "managed-child-environment.json"
        offline_identity = (
            "import json, os, pathlib; "
            f"pathlib.Path({str(identity_file)!r}).write_text(json.dumps({{key: os.environ.get(key) "
            "for key in ('ROUTER_MANAGED_ID', 'EXAMPLE_OPTION')})); "
        )

        def reserve_then_disconnect(operation, payload, timeout):
            result = request(operation, payload, self.endpoint, timeout=timeout)
            if operation == "managed_reserve":
                request("daemon_stop", {}, self.endpoint)
                self.daemons[-1].wait(timeout=15)
            return result

        previous_cwd = Path.cwd()
        try:
            os.chdir(self.repo)
            with patch.dict(os.environ, self.environment, clear=True), patch(
                "router.managed.request", side_effect=reserve_then_disconnect
            ):
                completed_code = run(
                    [sys.executable, "-c", offline_identity + "raise SystemExit(3)"],
                    "batch-job",
                    None,
                    None,
                    self.root / "state",
                )
        finally:
            os.chdir(previous_cwd)
        self.assertEqual(completed_code, 3)
        unavailable = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5")
        self.assertEqual((unavailable.returncode, json.loads(unavailable.stdout)["status"]), (1, "unavailable"))
        self.boot()
        delivered = self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        first = json.loads(delivered.stdout)
        self.assertEqual(first["status"], "failed")
        child_environment = json.loads(identity_file.read_text())
        self.assertEqual(child_environment["ROUTER_MANAGED_ID"], first["event"]["managed_id"])
        self.assertNotIn(child_environment["ROUTER_MANAGED_ID"], (outside_identity, "managed-parent"))
        self.assertEqual(child_environment["EXAMPLE_OPTION"], "preserved")
        self.assertEqual(self.environment["ROUTER_MANAGED_ID"], "managed-parent")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        self.boot()
        self.assertEqual(json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "5").stdout), first)
        event_id = first["event"]["event_id"]
        # Discard the first ack reply, then retry with a new consumer process.
        request("completion_ack", {"consumer": "worker-a", "event_id": event_id}, self.endpoint)
        acknowledged = self.completion_cli("ack", "--consumer", "worker-a", event_id)
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)
        self.assertTrue(json.loads(acknowledged.stdout)["acknowledged"])
        self.assertEqual(json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "2").stdout)["status"], "deadline")

    def test_invalid_completion_arguments_exit_two_even_when_daemon_is_unavailable(self):
        refused = self.completion_cli("ack", "--consumer", "worker-a", "completion-1")
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("Unknown completion consumer", refused.stderr)
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        invalid = []
        for consumer in ("", " worker-a", "worker-\na", "x" * 241):
            invalid.extend((("wait", "--consumer", consumer, "--timeout", "1"),
                            ("ack", "--consumer", consumer, "completion-1")))
        invalid.extend(("wait", "--consumer", "worker-a", "--timeout", value)
                       for value in ("0", "-1", "3601", "nan", "inf", "not-a-number"))
        invalid.extend(("ack", "--consumer", "worker-a", value)
                       for value in ("completion-0", "completion-01", "completion-x"))
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                result = self.completion_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
        for arguments in (("wait", "--consumer", "worker-a", "--timeout", "1"),
                          ("ack", "--consumer", "worker-a", "completion-1"), ("status",)):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.completion_cli(*arguments).returncode, 1)

    def test_interrupted_ack_before_or_after_commit_can_retry_without_skipping(self):
        for _ in range(2):
            completed = subprocess.run([sys.executable, "-m", "router.cli", "run", "--", sys.executable, "-c", "pass"],
                                       cwd=self.repo, env=self.environment, capture_output=True, timeout=15, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)

        def hold_reply(listener, committed, admitted, release):
            connection, _ = listener.accept()
            with connection, connection.makefile("rb") as incoming:
                connection.settimeout(15)
                message = json.loads(incoming.readline(1024 * 1024))
                self.assertEqual(message["operation"], "completion_ack")
                if committed:
                    request("completion_ack", message["payload"], self.endpoint)
                admitted.set()
                self.assertTrue(release.wait(60))
                # Drop an undelivered request or a committed reply at the chosen boundary.

        for committed in (False, True):
            with self.subTest(committed=committed):
                consumer = "worker-after" if committed else "worker-before"
                first = json.loads(self.completion_cli("wait", "--consumer", consumer, "--timeout", "3600").stdout)
                event_id = first["event"]["event_id"]
                admitted, release = threading.Event(), threading.Event()
                endpoint = self.root / (consumer + ".sock")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, ThreadPoolExecutor(max_workers=1) as executor:
                    listener.bind(str(endpoint))
                    listener.listen(1)
                    listener.settimeout(15)

                    future = executor.submit(hold_reply, listener, committed, admitted, release)
                    child = subprocess.Popen([sys.executable, "-m", "router.cli", "ack", "--consumer", consumer, event_id],
                        env={**self.environment, "AGENT_ROUTER_SOCKET": str(endpoint)},
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        self.assertTrue(admitted.wait(15))
                        child.send_signal(signal.SIGINT)
                        output, errors = child.communicate(timeout=15)
                        self.assertEqual(child.returncode, 130)
                        self.assertEqual(output, "")
                        self.assertIn("commit state is unknown", errors)
                        pending = request("completion_next", {"consumer": consumer}, self.endpoint)
                        self.assertEqual(pending["event"]["event_id"] == event_id, not committed)
                    finally:
                        release.set()
                        if child.poll() is None:
                            child.kill()
                        child.communicate()
                        future.result(timeout=15)
                for _ in range(2):
                    retried = self.completion_cli("ack", "--consumer", consumer, event_id)
                    self.assertEqual(retried.returncode, 0, retried.stderr)
                next_event = request("completion_next", {"consumer": consumer}, self.endpoint)
                self.assertNotEqual(next_event["event"]["event_id"], event_id)
                self.assertEqual(request("completion_next", {"consumer": consumer}, self.endpoint), next_event)

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
            waiter.send_signal(signal.SIGINT)
            waiter.wait(timeout=15)
            output, errors = waiter.communicate(timeout=15)
            self.assertEqual(waiter.returncode, 130)
            self.assertEqual(json.loads(output)["status"], "unavailable")
            self.assertIn("interrupted", errors.decode())
            self.assertIsNone(command.poll())
            release.touch()
            event = json.loads(self.completion_cli("wait", "--consumer", "worker-a", "--timeout", "10").stdout)
            self.assertEqual(event["status"], "completed")
            self.assertEqual(command.wait(timeout=15), 0)
            self.assertEqual(started.read_text(), "once")
            self.assertEqual(json.loads(self.completion_cli("wait", "--consumer", "worker-b", "--timeout", "5").stdout)["event"],
                             event["event"])
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
