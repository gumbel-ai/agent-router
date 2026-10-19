import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from router.cli import main
from router.client import request
from router.fleet import FleetHost, SSHQueryError
from router.request_status import MAX_WIRE_BYTES, fetch, options


class RequestStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ar-request-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.selection = {"session_id": "session-a", "request_id": "request-a", "host_id": "host-a"}
        self.receipt = {"schema_version": 1, **self.selection, "status": "found",
                        "operation": "start", "task_id": "task-a"}

    def cli(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["router", *arguments]), redirect_stdout(output), redirect_stderr(error):
            try:
                main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code
        return code, output.getvalue(), error.getvalue()

    def test_cli_local_and_fleet_exit_contract_and_input_validation(self):
        config = self.root / "fleet.toml"
        config.write_text('schema_version=1\n[[host]]\nname="worker-a"\ntransport="local"\n')
        local = ["request-status", "--session", "session-a", "--request-id", "request-a", "--json"]
        fleet = ["fleet", *local, "--host", "worker-a", "--host-id", "host-a", "--config", str(config)]
        missing = {key: value for key, value in {**self.receipt, "status": "not_found"}.items()
                   if key not in {"task_id", "operation"}}
        for arguments in (local, fleet):
            for receipt in (self.receipt, missing):
                with self.subTest(command=arguments[0], status=receipt["status"]), \
                        patch("router.client.request", return_value=receipt):
                    code, output, error = self.cli(arguments)
                    self.assertEqual((code, json.loads(output)), (0, receipt), error)
            for failure, expected in ((ValueError("sensitive diagnostic"), 1), (TimeoutError(), 1),
                                      (ConnectionResetError(), 1), (KeyboardInterrupt(), 130)):
                with self.subTest(failure=type(failure).__name__), patch("router.client.request", side_effect=failure):
                    code, output, error = self.cli(arguments)
                    self.assertEqual(code, expected, error)
                    self.assertNotIn("sensitive diagnostic", output + error)
                    if expected == 1:
                        self.assertEqual(json.loads(output)["status"], "unknown")
                    else:
                        self.assertEqual(output, "")
            for invalid in ("", " leading", "line\nbreak", "x" * 241):
                bad = list(arguments)
                bad[bad.index("--request-id") + 1] = invalid
                with self.subTest(invalid=invalid[:20]), patch("router.client.request") as transport:
                    code, _, _ = self.cli(bad)
                    self.assertEqual(code, 2)
                    transport.assert_not_called()
        bad_host = ["missing" if value == "worker-a" else value for value in fleet]
        with patch("router.client.request") as transport:
            self.assertEqual(self.cli(bad_host)[0], 2)
            transport.assert_not_called()

    def test_remote_schema_and_request_identity_never_degrade_to_not_found(self):
        host = FleetHost("worker-a", "ssh", target="example-host")
        raw = json.dumps(self.receipt).encode()
        changes = ({"schema_version": True}, {"schema_version": 2}, {"host_id": "host-b"},
                   {"host_id": None}, {"session_id": "session-b"}, {"request_id": "request-b"},
                   {"status": []}, {"task_id": "bad\nidentity"}, {"operation": []},
                   {"operation": "unknown"}, {"prompt": "sensitive diagnostic"})
        malformed = [json.dumps({**self.receipt, **change}).encode() for change in changes]
        malformed.extend(json.dumps({key: value for key, value in self.receipt.items() if key != missing}).encode()
                         for missing in self.receipt)
        malformed.extend((b"", b"null", b"[]", raw + b"{}", raw[:-1],
                          raw.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
                          raw.replace(b'"schema_version": 1', b'"schema_version": 1e309'),
                          b"[" * 2000 + b"0" + b"]" * 2000, b" " * (MAX_WIRE_BYTES + 1)))
        for content in malformed:
            with self.subTest(content=content[:70]), patch("router.fleet.ssh_stdout", return_value=content):
                result = fetch(self.selection, host=host)
                self.assertEqual((result["status"], result["error"]["code"]), ("unknown", "invalid_response"))
                self.assertNotIn("sensitive diagnostic", str(result))
        with patch("router.fleet.ssh_stdout", return_value=raw) as transport:
            self.assertEqual(fetch(self.selection, host=host), self.receipt)
            self.assertEqual(transport.call_args.args[2], ["request-status", "--session=session-a", "--request-id=request-a",
                                                         "--host-id=host-a", "--json"])
        for payload in ({"session_id": []}, {"request_id": None}, {"host_id": "unsafe\nidentity"}, {"task": "contents"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                options({**self.selection, **payload})
        with self.assertRaises(ValueError):
            fetch({key: value for key, value in self.selection.items() if key != "host_id"}, host=host)
        for failure, code in ((SSHQueryError("deadline", "sensitive diagnostic"), "ssh_deadline"),
                              (SSHQueryError("exit_failed", "sensitive diagnostic", exit_code=255), "ssh_exit_failed"),
                              (SSHQueryError("exit_failed", "", exit_code=1, stdout=raw), "invalid_response")):
            with self.subTest(transport=code), patch("router.fleet.ssh_stdout", side_effect=failure):
                result = fetch(self.selection, host=host)
                self.assertEqual((result["status"], result["error"]["code"]), ("unknown", code))
        refusal = {"schema_version": 1, **self.selection, "status": "unknown", "error": {"code": "binding_invalid"}}
        with patch("router.fleet.ssh_stdout", side_effect=SSHQueryError("exit_failed", "", exit_code=1,
                                                                       stdout=json.dumps(refusal).encode())):
            self.assertEqual(fetch(self.selection, host=host), refusal)

    def test_local_raw_framing_is_bounded_and_requires_eof_within_deadline(self):
        envelope = json.dumps({"ok": True, "result": self.receipt}).encode() + b"\n"

        class ReplySocket:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, timeout):
                pass

            def connect(self, endpoint):
                pass

            def sendall(self, content):
                if json.loads(content)["operation"] != "request_status":
                    raise AssertionError("Request lookup attempted a different operation")

            def recv(self, count):
                if not self.chunks:
                    return b""
                chunk = self.chunks.pop(0)
                if isinstance(chunk, BaseException):
                    raise chunk
                if len(chunk) > count:
                    self.chunks.insert(0, chunk[count:])
                return chunk[:count]

        cases = (([envelope], "found", None), ([envelope[:10], envelope[10:]], "found", None),
                 ([b" " * (MAX_WIRE_BYTES - len(envelope)) + envelope], "found", None),
                 ([envelope, b"{}"], "unknown", "invalid_response"),
                 ([envelope, b"\n"], "unknown", "invalid_response"),
                 ([envelope[:-1]], "unknown", "invalid_response"),
                 ([b" " * (MAX_WIRE_BYTES + 1)], "unknown", "invalid_response"),
                 ([b'{"ok":1,"result":{}}\n'], "unknown", "invalid_response"),
                 ([b'{"ok":true,"ok":true,"result":{}}\n'], "unknown", "invalid_response"),
                 ([b'{"ok":false,"error":"sensitive diagnostic"}\n'], "unknown", "request_failed"),
                 ([envelope, TimeoutError()], "unknown", "transport_deadline"))
        for chunks, status, code in cases:
            with self.subTest(status=status, code=code), patch("router.client.socket.socket", return_value=ReplySocket(chunks)):
                result = fetch(self.selection)
                self.assertEqual(result["status"], status)
                if code is not None:
                    self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("sensitive diagnostic", str(result))
        for failure, code in ((TimeoutError(), "transport_deadline"), (ConnectionRefusedError(), "transport_unavailable")):
            with self.subTest(connect=type(failure).__name__), patch.object(ReplySocket, "connect", side_effect=failure), \
                    patch("router.client.socket.socket", return_value=ReplySocket([])):
                self.assertEqual(fetch(self.selection)["error"]["code"], code)

    def test_separate_host_processes_recover_same_request_after_restart_without_mutations(self):
        from router.config import Settings
        from router.daemon import Controller
        popen = subprocess.Popen
        environments, processes, endpoints = [], [], []
        launcher = (
            "import sys; from pathlib import Path; from types import SimpleNamespace; "
            "from router.daemon import Controller,Server,Handler; from router.db import Store; "
            "controller=object.__new__(Controller); controller.host=SimpleNamespace(host_id=sys.argv[3]); "
            "controller.store=Store(Path(sys.argv[1])); server=Server(sys.argv[2],Handler); "
            "server.control=controller; server.serve_forever()"
        )

        def stop(process):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()

        def start(index):
            process = popen([sys.executable, "-c", launcher, str(controllers[index].store.path),
                             str(endpoints[index]), f"host-{index}"], env=environments[index],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(process)
            self.addCleanup(stop, process)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("Simulated host exited before serving: " + process.stderr.read().decode())
                if endpoints[index].exists():
                    try:
                        request("request_status", {"session_id": "session-a", "request_id": "request-a"},
                                endpoints[index], timeout=1, deadline=True)
                        return process
                    except (ValueError, OSError):
                        pass
                time.sleep(.01)
            self.fail("Simulated host did not become reachable within 15 seconds")

        controllers, task_ids = [], []
        for index in range(2):
            state = self.root / f"state-{index}"
            controller = Controller(Settings(), {}, state)
            # Register via production authorization/start; no scheduler or native child is started.
            controller.settings = Settings(AGENT_ROUTER_MODELS={"test": "provider/exact"},
                                           AGENT_ROUTER_REPOSITORIES={str(self.root): {"setup_command": "true"}},
                                           AGENT_ROUTER_OPENCODE_COMMAND=["opencode"], AGENT_ROUTER_CONCURRENCY_CAP=1,
                                           AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS=30, AGENT_ROUTER_MAX_ATTEMPTS=1)
            instruction = controller.handle("submit", {"session_id": "session-a", "prompt_id": "prompt-a", "parent_cli": "claude"})
            invocation = {"request_version": "1", "request_id": "request-a", **instruction, "executor": "opencode",
                          "model": "test", "task": "Modify the example file", "parent_repo": str(self.root),
                          "parent_worktree": str(self.root), "mode": "write"}
            controller.handle("authorize", {"session_id": "session-a", "prompt_id": "prompt-a",
                                            "operation": "start", "invocation": invocation})
            task_ids.append(controller.handle("start", invocation)["task_id"])
            controllers.append(controller)
            endpoints.append(self.root / f"host-{index}.sock")
            environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
            environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                               AGENT_ROUTER_SOCKET=str(endpoints[index]), PYTHONPYCACHEPREFIX=str(self.root / "pycache"))
            environments.append(environment)
            start(index)
        before = [controller.store.all() for controller in controllers]

        def remote(command, **kwargs):
            self.assertEqual(command[command.index("--") + 1], "example-host")
            arguments = shlex.split(" ".join(command[command.index("--") + 2:]))
            self.assertEqual(arguments[:2], ["router", "request-status"])
            return popen([sys.executable, "-m", "router.cli", *arguments[1:]], env=environments[1], **kwargs)

        host = FleetHost("worker-a", "ssh", target="example-host")
        selection = {**self.selection, "host_id": "host-1"}
        with patch("router.fleet.subprocess.Popen", side_effect=remote):
            result = fetch(selection, host=host)
            self.assertEqual((result["status"], result["task_id"]), ("found", task_ids[1]))
            self.assertNotEqual(result["task_id"], task_ids[0])
            for field in ("session_id", "request_id", "host_id"):
                with self.subTest(option_shaped_identity=field):
                    shaped = fetch({**selection, field: "--json"}, host=host)
                    self.assertEqual(shaped["status"], "unknown" if field == "host_id" else "not_found")
                    if field == "host_id":
                        self.assertEqual(shaped["error"]["code"], "identity_mismatch")
            stop(processes[1])
            endpoints[1].unlink()
            disconnected = fetch(selection, host=host)
            self.assertEqual(disconnected["status"], "unknown")
        start(1)
        with patch("router.fleet.subprocess.Popen", side_effect=remote):
            self.assertEqual(fetch(selection, host=host), result)
            self.assertEqual(fetch({**selection, "host_id": "host-0"}, host=host)["error"]["code"], "identity_mismatch")
        self.assertEqual([controller.store.all() for controller in controllers], before)


if __name__ == "__main__":
    unittest.main()
