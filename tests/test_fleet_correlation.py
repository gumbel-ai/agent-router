import copy
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
from router.client import request as router_request
from router.correlation import export
from router.db import Store
from router.dispatch import canonical
from router.fleet import FleetConfig, FleetHost


class FleetCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-fleet-correlation-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stores = {}
        for host in ("host-a", "host-b"):
            store = Store(self.root / host / "router.sqlite3", host)
            with store.transaction() as db:
                for number in range(3):
                    db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (host, "example-runtime", f"session-{number}",
                               canonical({"status": "idle", "last_event": "stop", "last_event_at": 1,
                                          "prompt": "fixture-private-content", "secret": "fixture-secret-value"})))
            self.stores[host] = store
        self.config = FleetConfig((FleetHost("worker-a", "local"), FleetHost("worker-b", "ssh", target="example-host")),
                                  ssh_timeout_seconds=15)

    def page(self, host="host-a", **options):
        return export(self.stores[host], host, options)

    def test_cli_reports_complete_partial_and_interrupted_exports(self):
        config = self.root / "fleet.toml"
        config.write_text('schema_version=1\n[[host]]\nname="worker-a"\ntransport="local"\n')
        for failure, expected in ((None, 0), (ValueError("fixture-secret-value"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(failure=type(failure).__name__):
                output, error = io.StringIO(), io.StringIO()
                with patch("sys.argv", ["router", "fleet", "correlation", "--json", "--config", str(config)]), \
                        patch("router.client.request", return_value=self.page(), side_effect=failure), \
                        redirect_stdout(output), redirect_stderr(error):
                    try:
                        main()
                        code = 0
                    except SystemExit as caught:
                        code = caught.code
                self.assertEqual(code, expected, error.getvalue())
                self.assertNotIn("fixture-secret-value", output.getvalue() + error.getvalue())
                if expected != 130:
                    result = json.loads(output.getvalue())
                    self.assertEqual(result["partial"], bool(expected))
                    self.assertEqual(result["hosts"][0]["ok"], not expected)
                    if expected:
                        self.assertEqual(result["hosts"][0]["error"]["code"], "request_failed")

    def test_remote_projection_and_schema_failures_do_not_forward_content(self):
        from router.fleet_correlation import query_correlation
        config = FleetConfig((FleetHost("worker-b", "ssh", target="example-host"),))
        with self.stores["host-b"].transaction() as db:
            db.execute("""INSERT INTO completion_events(managed_id,host_id,phase,created_at,data)
                          VALUES(?,?,?,?,?)""", ("managed-job", "host-b", "finished", 1, canonical({
                "status": "completed", "event": {"session_id": "session  a", "agent_id": "worker  a"}})))
        source = self.page("host-b")
        source["records"].append({"kind": "attempt", "host_id": "host-b", "runtime": "codex",
                                  "model_provider": "openai", "session_id": "child-session",
                                  "parent_session_id": "parent-session", "parent_agent_id": "parent-agent",
                                  "parent_runtime": "claude", "router_task_id": "task-a",
                                  "attempt_number": 2, "attempts": 2, "task": "fixture-private-content"})
        source["prompt"] = "fixture-private-content"
        source["records"][0]["tool_contents"] = {"secret": "fixture-secret-value"}
        for mutate, valid, partial in ((lambda page: None, True, False),
                              (lambda page: page.update(schema_version=True), False, True),
                              (lambda page: page.update(records={}), False, True),
                              (lambda page: page.pop("next_cursor"), False, True),
                              (lambda page: page["records"][0].update(host_id=None, session_id=None), True, False),
                              (lambda page: page["records"][0].update(kind="future-kind"), True, True),
                              (lambda page: page["records"][0].update(timestamp=True), True, True),
                              (lambda page: page["records"][0].update(timestamp=float("nan")), True, True),
                              (lambda page: page["records"][0].update(status={"raw": "fixture-private-content"}), True, True),
                              (lambda page: page["records"][0].update(host_id="foreign-host"), True, True),
                              (lambda page: page["records"][0].update(session_id="unsafe\nidentity"), True, True),
                              (lambda page: page["records"][0].update(traced=True), True, True)):
            page = copy.deepcopy(source)
            mutate(page)
            for transport in ("local", "ssh"):
                config = FleetConfig((FleetHost("worker-b", transport, target="example-host" if transport == "ssh" else None),))
                with self.subTest(page_valid=valid, transport=transport), \
                        patch("router.client.request", return_value=page), \
                        patch("router.fleet_correlation.ssh_stdout", return_value=json.dumps(page).encode()):
                    result = query_correlation(config)
                    self.assertEqual(result["hosts"][0]["ok"], valid)
                    self.assertEqual(result["partial"], partial)
                    encoded = canonical(result)
                    for text in ("fixture-private-content", "fixture-secret-value", "tool_contents", "prompt"):
                        self.assertNotIn(text, encoded)
                    if valid:
                        projected = result["hosts"][0]["page"]
                        self.assertEqual(projected["records"][0]["runtime"], "example-runtime")
                        self.assertEqual(projected["wire_error_count"], int(partial))
                        attempt = next(record for record in projected["records"] if record["kind"] == "attempt")
                        self.assertEqual((attempt["runtime"], attempt["model_provider"], attempt["session_id"],
                                          attempt["parent_session_id"], attempt["parent_agent_id"], attempt["parent_runtime"],
                                          attempt["router_task_id"], attempt["attempt_number"], attempt["attempts"]),
                                         ("codex", "openai", "child-session", "parent-session", "parent-agent", "claude",
                                          "task-a", 2, 2))
                        if page["records"][0].get("host_id") is None:
                            self.assertIsNone(projected["records"][0]["host_id"])
                            self.assertIsNone(projected["records"][0]["session_id"])
                        completion = next(record for record in projected["records"] if record["kind"] == "completion")
                        self.assertEqual((completion["host_id"], completion["event_id"], completion["managed_id"],
                                          completion["session_id"], completion["agent_id"]),
                                         ("host-b", "completion-1", "managed-job", "session  a", "worker  a"))
                        self.assertIsNone(completion["runtime"])
                        if partial:
                            self.assertTrue(projected["wire_errors"])
                    else:
                        self.assertEqual(result["hosts"][0]["error"]["code"], "invalid_response")

        for fields, wire_errors in ((["session_id"], 0), (["fixture-secret-value"], 1)):
            incoming = copy.deepcopy(source)
            incoming.update(partial=True, invalid_records=1, errors=[{
                "kind": "session", "row": 1, "fields": fields, "detail": "fixture-private-content"}])
            with self.subTest(diagnostic_fields=fields), \
                    patch("router.fleet_correlation.ssh_stdout", return_value=canonical(incoming).encode()):
                result = query_correlation(config)
            host = result["hosts"][0]
            self.assertTrue(host["ok"])
            self.assertTrue(result["partial"])
            self.assertEqual(host["page"]["wire_error_count"], wire_errors)
            self.assertNotIn("fixture-secret-value", canonical(result))
            self.assertNotIn("fixture-private-content", canonical(result))

    def test_pagination_preserves_host_ids_and_reports_identity_collisions(self):
        from router.fleet_correlation import query_correlation
        def remote(host, timeout, arguments):
            self.assertEqual(arguments[:2], ["correlation", "--json"])
            options = {"limit": int(arguments[3])}
            if "--cursor" in arguments:
                options["cursor"] = arguments[5]
            return canonical(self.page("host-b", **options)).encode()
        def local(operation, options, **kwargs):
            self.assertEqual(operation, "correlation")
            self.assertTrue(kwargs["deadline"])
            return self.page(**options)
        with patch("router.client.request", side_effect=local), patch("router.fleet_correlation.ssh_stdout", side_effect=remote):
            first = query_correlation(self.config, limit=2)
            self.assertFalse(first["partial"], first)
            self.assertEqual([host["host_id"] for host in first["hosts"]], ["host-a", "host-b"])
            cursor = first["hosts"][1]["page"]["next_cursor"]
            second = query_correlation(self.config, only="worker-b", limit=2, cursor=cursor)
            records = first["hosts"][1]["page"]["records"] + second["hosts"][0]["page"]["records"]
            self.assertEqual([record["session_id"] for record in records], [f"session-{n}" for n in range(3)])
            self.assertTrue(all(record["host_id"] == "host-b" for record in records))
            self.assertIsNone(second["hosts"][0]["page"]["next_cursor"])
        with patch("router.fleet_correlation.ssh_stdout", return_value=canonical(self.page("host-b")).encode()):
            foreign = query_correlation(self.config, only="worker-b", cursor=self.page(limit=1)["next_cursor"])
        self.assertFalse(foreign["hosts"][0]["ok"])
        self.assertEqual(foreign["hosts"][0]["error"]["code"], "invalid_response")
        with patch("router.client.request", return_value=self.page()), \
                patch("router.fleet_correlation.ssh_stdout", return_value=canonical(self.page()).encode()):
            result = query_correlation(self.config)
        self.assertTrue(result["partial"])
        for host in result["hosts"]:
            self.assertFalse(host["ok"])
            self.assertEqual(host["host_id"], "host-a")
            self.assertEqual(host["error"]["code"], "duplicate_host_id")
            self.assertNotIn("page", host)

    def test_ssh_errors_deadlines_bounds_and_argument_quoting_are_explicit(self):
        from router.fleet import SSHQueryError, ssh_stdout
        from router.fleet_correlation import query_correlation
        config = FleetConfig((FleetHost("worker-b", "ssh", target="odd; target"),), ssh_timeout_seconds=15)
        popen, processes, commands = subprocess.Popen, [], []
        def launch(source):
            def start(command, **options):
                commands.append(command)
                self.assertEqual(command[:3], ["ssh", "-o", "BatchMode=yes"])
                self.assertIn("-n", command)
                self.assertEqual(command[command.index("--") + 1], "odd; target")
                self.assertEqual(options["stdin"], subprocess.DEVNULL)
                self.assertTrue(options["start_new_session"])
                process = popen([sys.executable, "-c", source], **options)
                processes.append(process)
                return process
            return start
        source = "import os;os.write(2,b'fixture-secret-value'*10000);os.write(1," + repr(canonical(self.page("host-b")).encode()) + ")"
        with patch("router.fleet.subprocess.Popen", side_effect=launch(source)):
            result = query_correlation(config)
        self.assertFalse(result["partial"])
        self.assertIsNotNone(processes[-1].poll())
        for source, code, exit_code in (("import sys;sys.stderr.write('fixture-secret-value');sys.exit(127)", "exit_failed", 127),
                                        ("import os;os.write(1,b'x'*(1024*1024+1))", "output_limit", None),
                                        ("print('invalid JSON')", "invalid_response", None)):
            with self.subTest(code=code), patch("router.fleet.subprocess.Popen", side_effect=launch(source)):
                result = query_correlation(config)
            host = result["hosts"][0]
            self.assertFalse(host["ok"])
            self.assertEqual(host["error"]["code"], code)
            self.assertEqual(host["error"].get("exit_code"), exit_code)
            self.assertNotIn("fixture-secret-value", canonical(result))
            self.assertIsNotNone(processes[-1].poll())
        with patch("router.fleet.subprocess.Popen", side_effect=launch("import time;time.sleep(30)")), \
                patch("router.fleet.time.monotonic", side_effect=[0, 16]), \
                patch("router.fleet._kill_remaining_group"), self.assertRaises(SSHQueryError) as caught:
            ssh_stdout(config.hosts[0], 15, ["correlation", "--json"])
        self.assertEqual(caught.exception.code, "deadline")
        self.assertIsNotNone(processes[-1].poll())
        cursor = "opaque; $(false) ' quoted"
        with patch("router.fleet.subprocess.Popen", side_effect=launch("print('{}')")):
            ssh_stdout(config.hosts[0], 15, ["correlation", "--json", "--cursor", cursor])
        remote = commands[-1][commands[-1].index("--") + 2:]
        self.assertEqual(shlex.split(" ".join(remote)), ["router", "correlation", "--json", "--cursor", cursor])
        with patch("router.fleet.subprocess.Popen", side_effect=FileNotFoundError(2, "fixture-secret-value")):
            host = query_correlation(config)["hosts"][0]
        self.assertEqual(host["error"], {"code": "spawn_failed", "errno": 2})
        self.assertNotIn("fixture-secret-value", canonical(host))

    def test_invalid_selection_is_rejected_before_any_transport(self):
        from router.fleet_correlation import query_correlation
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(True)
            raise AssertionError("Unexpected transport request")
        for options in ({"limit": True}, {"limit": 0}, {"limit": 101}, {"only": "unknown"},
                        {"cursor": "token"}, {"only": "worker-b", "cursor": "x" * 513}):
            with self.subTest(options=options), patch("router.client.request", side_effect=forbidden), \
                    patch("router.fleet_correlation.ssh_stdout", side_effect=forbidden), self.assertRaises(ValueError):
                query_correlation(self.config, **options)
        self.assertEqual(calls, [])

    def test_two_processes_keep_state_identity_and_pages_through_restart_and_failure(self):
        from router.fleet_correlation import query_correlation
        popen = subprocess.Popen
        processes, endpoints, environments = [], [], []
        project = self.root / "example-project"
        project.mkdir()
        (project / ".agent-router.json").write_text('{"observe": true}\n')
        launcher = ("import sys,router.host; identity=sys.argv[1]; "
                    "router.host.stable_host_id=lambda directory: identity; "
                    "from router.cli import main; sys.argv=['router','daemon','start']; main()")
        def stop(process, endpoint):
            if process.poll() is None:
                try:
                    router_request("daemon_stop", {}, endpoint, timeout=15, deadline=True)
                    process.wait(timeout=15)
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=15)
            process.communicate()
        def start(number):
            endpoint = self.root / f"host-{number}.sock"
            state = self.root / f"daemon-{number}"
            home = self.root / f"home-{number}"
            home.mkdir(exist_ok=True)
            environment = {key: value for key, value in os.environ.items()
                           if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME"}}
            environment.update(HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                               AGENT_ROUTER_STATE_DIR=str(state), AGENT_ROUTER_SOCKET=str(endpoint),
                               AGENT_ROUTER_ENV_FILE=str(self.root / "missing.env"))
            process = popen([sys.executable, "-c", launcher, f"emulated-host-{number}"], env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            processes.append((process, endpoint))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    self.fail("Simulated host exited: " + error.decode(errors="replace")[-1000:])
                try:
                    router_request("health", {}, endpoint, timeout=.5, deadline=True)
                    return process, endpoint, environment
                except (ValueError, OSError):
                    time.sleep(.02)
            self.fail("Simulated host did not start within 15 seconds")
        ssh_processes = []
        try:
            for number in range(2):
                _, endpoint, environment = start(number)
                endpoints.append(endpoint)
                environments.append(environment)
                for identity in ("session-a", "session-b"):
                    router_request("session_observe", {"source": "codex", "session_id": identity, "kind": "session_start",
                                                       "cwd": str(project), "observed_at": int(time.time() * 1000) - 1000},
                                   endpoint, timeout=15, deadline=True)
            def local(operation, options, **kwargs):
                return router_request(operation, options, endpoints[0], **kwargs)
            def remote(command, **options):
                args = shlex.split(" ".join(command[command.index("--") + 2:]))
                self.assertEqual(args[:3], ["router", "correlation", "--json"])
                process = popen([sys.executable, "-m", "router.cli", *args[1:]], env=environments[1], **options)
                ssh_processes.append(process)
                return process
            with patch("router.client.request", side_effect=local), patch("router.fleet.subprocess.Popen", side_effect=remote):
                first = query_correlation(self.config, limit=1)
                self.assertFalse(first["partial"], first)
                self.assertEqual([host["host_id"] for host in first["hosts"]], ["emulated-host-0", "emulated-host-1"])
                self.assertEqual([host["page"]["records"][0]["session_id"] for host in first["hosts"]],
                                 ["session-a", "session-a"])
                cursor = first["hosts"][1]["page"]["next_cursor"]
                stop(*processes[1])
                _, _, environments[1] = start(1)
                resumed = query_correlation(self.config, only="worker-b", limit=1, cursor=cursor)
                self.assertFalse(resumed["partial"])
                record = resumed["hosts"][0]["page"]["records"][0]
                self.assertEqual((record["host_id"], record["session_id"]), ("emulated-host-1", "session-b"))
                stop(*processes[-1])
                partial = query_correlation(self.config)
                self.assertTrue(partial["partial"])
                self.assertTrue(partial["hosts"][0]["ok"])
                self.assertFalse(partial["hosts"][1]["ok"])
                self.assertEqual(partial["hosts"][1]["error"], {"code": "exit_failed", "exit_code": 1})
            self.assertTrue(all(process.poll() is not None for process in ssh_processes))
        finally:
            for process, endpoint in reversed(processes):
                stop(process, endpoint)
