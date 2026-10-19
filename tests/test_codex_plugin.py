import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import unix_serve

from router.cli import main as cli_main
from router.codex_plugin import (
    HOOKS_FILE,
    PLUGIN_ID,
    active_plugin_root,
    availability,
    expected_trust,
    hook_hash,
)
from router.setup import register_codex_marketplace

ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((ROOT / HOOKS_FILE).read_text())


class CodexPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-codex-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "example-project"
        self.project.mkdir()
        (self.project / ".agent-router.json").write_text('{"observe": true}\n')
        self.servers = []
        self.daemon_identity = {}
        self.addCleanup(self.stop_servers)
        self.start_server(self.home)
        which = patch("router.codex_plugin.shutil.which", return_value=str(self.home / "codex-not-invoked"))
        which.start()
        self.addCleanup(which.stop)
        cwd = patch("router.codex_plugin.Path.cwd", return_value=self.project)
        cwd.start()
        self.addCleanup(cwd.stop)

    def start_server(self, home, endpoint=None):
        endpoint = endpoint or home / "app-server-control" / "app-server-control.sock"
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        identity = self.daemon_identity[home] = {"automatic_login": False, "user_agent": "existing-client"}

        def handle(connection):
            fixture = home / "rpc.json"
            options = json.loads(fixture.read_text()) if fixture.exists() else {}
            try:
                config = tomllib.loads((home / "config.toml").read_text())
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                connection.close(1011, "fixture configuration invalid")
                return
            cwd = str(self.project)
            try:
                for raw in connection:
                    request = json.loads(raw)
                    with (home / "rpc.log").open("a") as log:
                        log.write(json.dumps({"request": request, "home": str(home), "cwd": cwd}) + "\n")
                    if "id" not in request:
                        continue
                    if request["method"] == "initialize":
                        # Native 0.163.0 initialize_processor: only originating clients
                        # alter daemon-wide identity or permit automatic gateway login.
                        params = request["params"]
                        name = params["clientInfo"]["name"]
                        explicit_login = params.get("capabilities", {}).get("explicitGatewayOauth", False)
                        originating = name not in {"codex_app_server_daemon", "codex-backend"}
                        if explicit_login or originating:
                            identity["automatic_login"] = not explicit_login
                        if originating:
                            identity["user_agent"] = name
                        response = {"result": options.get("initialize", {"codexHome": str(home)})}
                    elif request["method"] == "hooks/list":
                        hooks = config.get("hooks")
                        state = options.get("state", hooks.get("state", {}) if isinstance(hooks, dict) else {})
                        root = active_plugin_root(home)
                        declarations = json.loads((root / HOOKS_FILE).read_text())
                        entries = []
                        # Native inventory includes untrusted hooks, with their current
                        # declaration hash independent of the saved trust preference.
                        for key, current_hash in expected_trust(declarations).items():
                            entry = state.get(key, {})
                            status = ("untrusted" if "trusted_hash" not in entry else
                                      "trusted" if entry["trusted_hash"] == current_hash else "modified")
                            entries.append({"key": key, "pluginId": PLUGIN_ID, "enabled": entry.get("enabled", True),
                                            "trustStatus": status, "currentHash": current_hash})
                        response = {"result": options.get("inventory", {
                            "data": [{"cwd": requested, "hooks": entries, "errors": [], "warnings": []}
                                     for requested in request["params"]["cwds"]]})}
                    else:
                        cwd = request["params"]["cwd"]
                        mode = options.get("mode")
                        if mode == "exit":
                            connection.close(1011, "fixture transport unavailable")
                            return
                        if mode == "timeout":
                            connection.recv(timeout=15)
                            return
                        if mode == "oversize":
                            connection.send("x" * (2 * 1024 * 1024))
                            continue
                        if mode == "invalid_json":
                            connection.send("not JSON")
                            continue
                        if mode == "error":
                            response = {"error": {"code": -32601, "message": "fixture method unavailable"}}
                        else:
                            connection.send(json.dumps({"method": "fixture/notification"}))
                            response = {"result": {"config": options.get("config", config)}}
                    connection.send(json.dumps({"id": request["id"], **response}))
            except ConnectionClosed:
                pass

        server = unix_serve(handle, path=endpoint, compression=None)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.servers.append((server, thread))

    def stop_servers(self):
        for server, thread in self.servers:
            server.shutdown()
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("Fixture daemon did not stop")

    def install(self, version, declarations=HOOKS, home=None):
        hooks = (home or self.home).joinpath("plugins", "cache", "agent-router", "agent-router", version, HOOKS_FILE)
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text(json.dumps(declarations))

    def configure(self, plugin="enabled = true\n", entries=None, extra=""):
        lines = [extra]
        if plugin is not None:
            lines.append(f'[plugins."{PLUGIN_ID}"]\n{plugin}')
        for key, entry in (entries or {}).items():
            lines.append(f'[hooks.state."{key}"]\n' + "".join(f"{name} = {json.dumps(value)}\n"
                                                               for name, value in entry.items()))
        (self.home / "config.toml").write_text("\n".join(lines))
        return availability(self.home)["state"]

    def test_hash_matches_a_hook_trusted_by_codex(self):
        vector = json.loads((ROOT / "tests" / "codex_trust_vector.json").read_text())
        self.assertEqual(hook_hash(vector["event"], vector["group"]), vector["trusted_hash"])

    def test_plugin_manifest_points_at_hooks_whose_command_exists(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["hooks"], "./" + HOOKS_FILE)
        commands = {handler["command"] for groups in HOOKS["hooks"].values()
                    for group in groups for handler in group["hooks"]}
        self.assertEqual(commands, {'sh "${PLUGIN_ROOT}/router/plugin/codex/hook.sh"'})
        # Router stamps events on receipt; background hooks could arrive out of order.
        self.assertFalse(any(handler.get("async") for groups in HOOKS["hooks"].values()
                             for group in groups for handler in group["hooks"]))
        self.assertTrue((ROOT / "router" / "plugin" / "codex" / "hook.sh").is_file())
        self.assertEqual(len(expected_trust(HOOKS)), len(HOOKS["hooks"]))
        # Codex caches the plugin under its manifest version; keep it tied to the package.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(manifest["version"], project["project"]["version"])

    def test_availability_names_the_missing_prerequisite(self):
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        first = next(iter(trusted))
        self.assertEqual(self.configure(entries=trusted), "plugin_not_installed")
        self.install("0.9.0")
        self.assertEqual(self.configure(plugin=None), "plugin_not_installed")
        self.assertEqual(self.configure("enabled = false\n", trusted), "plugin_disabled")
        with self.subTest(plugin="malformed boolean"):
            self.assertEqual(self.configure('enabled = "false"\n', trusted), "unknown")
        self.assertEqual(self.configure(entries={}), "hooks_untrusted")
        self.assertEqual(self.configure(entries={**trusted, first: {"trusted_hash": "sha256:old"}}), "hooks_untrusted")
        self.assertEqual(self.configure(entries={**trusted, first: {**trusted[first], "enabled": False}}),
                         "hooks_disabled")
        self.assertEqual(self.configure(entries=trusted), "ready")
        for extra, state in (("[features]\nhooks = false\n", "hooks_disabled"),
                             ("[features]\nplugins = false\n", "plugin_disabled"),
                             ("[features]\ncodex_hooks = false\n", "hooks_disabled"),
                             ("[features]\ncodex_hooks = false\nhooks = true\n", "ready"),
                             ('[features]\nhooks = "false"\n', "unknown"),
                             ('[features]\nplugins = "false"\n', "unknown"),
                             ("plugins = 1\n", "unknown"), ("hooks = true\n", "unknown")):
            with self.subTest(extra=extra):
                self.assertEqual(self.configure(entries=trusted, extra=extra), state)
        # Codex trusts the copy it loads: the highest cached version, not Router's package.
        changed = json.loads(json.dumps(HOOKS))
        next(iter(changed["hooks"].values()))[0]["hooks"][0]["timeout"] = 3
        self.install("0.10.0", changed)
        self.assertEqual(self.configure(entries=trusted), "hooks_untrusted")
        retrusted = {key: {"trusted_hash": value} for key, value in expected_trust(changed).items()}
        self.assertEqual(self.configure(entries=retrusted), "ready")
        # SemVer order, as Codex sorts it: 0.10.0 still wins over 0.10.0-beta.1 and 0.9.0.
        self.install("0.10.0-beta.1")
        self.assertEqual(self.configure(entries=retrusted), "ready")
        self.install("0.11.0-beta.1")
        self.assertEqual(self.configure(entries=retrusted), "hooks_untrusted")
        # Every lifecycle event must keep a handler, or observation is incomplete.
        without_end = {"hooks": {event: groups for event, groups in HOOKS["hooks"].items() if event != "SessionEnd"}}
        for declarations in (without_end, {"hooks": {}}):
            self.install("0.11.0-beta.1", declarations)
            trusted_rest = {key: {"trusted_hash": value} for key, value in expected_trust(declarations).items()}
            self.assertEqual(self.configure(entries=trusted_rest), "unknown")
        (self.home / "config.toml").write_bytes(b"\xff")
        self.assertEqual(availability(self.home)["state"], "unknown")
        (self.home / "config.toml").write_text("[plugins\n")
        self.assertEqual(availability(self.home)["state"], "unknown")
        with patch("router.codex_plugin.shutil.which", return_value=None):
            self.assertEqual(availability(self.home)["state"], "codex_not_installed")

    def test_hook_answers_codex_even_without_router(self):
        state = self.home / "state"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "AGENT_ROUTER_STATE_DIR": str(state)}
        for pointer in (None, str(self.home / "removed" / "python")):
            if pointer:
                state.mkdir()
                (state / "python-path").write_text(pointer + "\n")
            with self.subTest(pointer=pointer):
                result = subprocess.run(["sh", str(ROOT / "router" / "plugin" / "codex" / "hook.sh")], env=environment,
                                        input='{"hook_event_name":"Stop"}', capture_output=True, text=True,
                                        timeout=30, check=False)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "{}"), result.stderr)

    def test_readiness_uses_codex_effective_project_configuration(self):
        self.install("0.9.0")
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        self.configure("enabled = false\n", trusted)
        original = (self.home / "config.toml").read_bytes()
        effective = tomllib.loads(original.decode())
        enabled = {**effective, "plugins": {PLUGIN_ID: {"enabled": True}}}
        cases = [
            (enabled, trusted, "ready"),
            (effective, trusted, "plugin_disabled"),
            ({**effective, "plugins": None}, trusted, "plugin_not_installed"),
            ({**enabled, "hooks": None}, {}, "hooks_untrusted"),
            ({**enabled, "features": {"hooks": False}}, trusted, "hooks_disabled"),
            # A project layer cannot supply user hook trust.
            (enabled, {}, "hooks_untrusted"),
            ({**enabled, "hooks": None}, trusted, "ready"),
            (enabled, {**trusted, next(iter(trusted)): {"enabled": False,
                       **trusted[next(iter(trusted))]}}, "hooks_disabled"),
        ]
        for config, state, expected in cases:
            with self.subTest(expected=expected):
                (self.home / "rpc.json").write_text(json.dumps({"config": config, "state": state}))
                self.assertEqual(availability(self.home)["state"], expected)
        self.assertEqual((self.home / "config.toml").read_bytes(), original)
        self.assertTrue((self.home / "rpc.log").is_file(), "readiness did not consult Codex")
        calls = [json.loads(line) for line in (self.home / "rpc.log").read_text().splitlines()]
        self.assertEqual({call["request"]["method"] for call in calls},
                         {"initialize", "initialized", "config/read", "hooks/list"})
        self.assertTrue(all(call["home"] == str(self.home) and call["cwd"] == str(self.project) for call in calls))
        self.assertTrue(all(call["request"]["params"]["cwd"] == str(self.project)
                            for call in calls if call["request"]["method"] == "config/read"))

    def test_status_codex_json_reports_readiness_without_the_daemon(self):
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}

        def status(*flags):
            output = io.StringIO()
            with patch.object(sys, "argv", ["router", "status", *flags]), \
                    patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), \
                    patch("router.cli.request", side_effect=AssertionError("daemon contacted")), \
                    redirect_stdout(output), redirect_stderr(io.StringIO()):
                cli_main()
            return json.loads(output.getvalue())

        self.configure(entries=trusted)
        self.assertEqual(status("--codex", "--json")["codex_observation"]["state"], "plugin_not_installed")
        self.install("0.9.0")
        effective = tomllib.loads((self.home / "config.toml").read_text())
        cases = [({}, trusted, "ready"), ({}, {}, "hooks_untrusted"),
                 # A project-level override reaches JSON through Codex's effective configuration.
                 ({"config": {**effective, "plugins": {PLUGIN_ID: {"enabled": False}}}}, trusted, "plugin_disabled")]
        for options, entries, expected in cases:
            with self.subTest(expected=expected):
                self.configure(entries=entries)
                (self.home / "rpc.json").write_text(json.dumps(options))
                result = status("--codex", "--json")
                self.assertEqual(result, {"codex_observation": availability(self.home)})
                self.assertEqual(result["codex_observation"]["state"], expected)
        with patch("router.codex_plugin.shutil.which", return_value=None):
            self.assertEqual(status("--codex", "--json")["codex_observation"]["state"], "codex_not_installed")
        for flags in (["--codex"], ["task-1", "--codex", "--json"], ["--codex", "--json", "--filter", "needs_decision"],
                      ["--codex", "--json", "--watch"], ["--codex", "--json", "--native-offset", "100"],
                      ["--codex", "--json", "--native-offset", "0"], ["", "--codex", "--json"]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as raised:
                status(*flags)
            self.assertEqual(raised.exception.code, 2)

    def test_readiness_bounds_protocol_failures_and_preserves_daemon(self):
        self.install("0.9.0")
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        self.configure(entries=trusted)
        complete = [{"key": key, "pluginId": PLUGIN_ID, "enabled": True, "trustStatus": "trusted",
                     "currentHash": value["trusted_hash"]} for key, value in trusted.items()]
        entry = complete[0]
        cases = [({"mode": mode}, detail) for mode, detail in
                 (("exit", "fixture transport unavailable"), ("timeout", "deadline"),
                  ("oversize", "1009"), ("invalid_json", "JSON"), ("error", "fixture method unavailable"))]
        cases += [
            ({"initialize": {}}, "daemon home"),
            ({"initialize": {"codexHome": "relative-home"}}, "daemon home"),
            ({"initialize": {"codexHome": str(self.home / "other-home")}}, "different Codex home"),
            ({"inventory": {"data": []}}, "inventory"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": [],
                                      "errors": ["fixture hook load failure"], "warnings": []}]}}, "fixture hook load failure"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": [entry, entry],
                                      "errors": [], "warnings": []}]}}, "malformed"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": [], "errors": [],
                                      "warnings": ["fixture warning-only load failure"]}]}}, "fixture warning-only load failure"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": [],
                                      "errors": [], "warnings": []}]}}, "missing Router hooks"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": complete,
                                      "errors": [], "warnings": "not-a-list"}]}}, "malformed"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": complete,
                                      "errors": [], "warnings": [{}]}]}}, "malformed"),
            ({"inventory": {"data": [{"cwd": str(self.project), "hooks": complete,
                                      "errors": []}]}}, "malformed"),
        ]
        for options, detail in cases:
            with self.subTest(options=options):
                (self.home / "rpc.json").write_text(json.dumps(options))
                result = availability(self.home)
                self.assertEqual(result["state"], "unknown")
                self.assertIn(detail, result["detail"])
                (self.home / "rpc.json").unlink()
                self.assertEqual(availability(self.home)["state"], "ready")
        inventory = {"data": [{"cwd": str(self.project), "hooks": complete, "errors": [],
                                "warnings": ["fixture unrelated warning"]}]}
        (self.home / "rpc.json").write_text(json.dumps({"inventory": inventory}))
        self.assertEqual(availability(self.home)["state"], "ready")
        for hook in ({**entry, "trustStatus": "modified"}, {**entry, "currentHash": "sha256:other"}):
            with self.subTest(hook=hook):
                inventory = {"data": [{"cwd": str(self.project), "hooks": [hook, *complete[1:]],
                                        "errors": [], "warnings": []}]}
                (self.home / "rpc.json").write_text(json.dumps({"inventory": inventory}))
                self.assertEqual(availability(self.home)["state"], "hooks_untrusted")
        (self.home / "rpc.json").unlink()
        endpoint = self.home / "app-server-control" / "app-server-control.sock"
        endpoint.unlink()
        result = availability(self.home)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("start Codex normally", result["detail"])

    def test_readiness_deadline_keeps_only_completed_evidence(self):
        self.install("0.9.0")
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        self.configure(entries=trusted)
        timeout = asyncio.timeout
        cases = [("connect", {}, "unknown"), ("config/read", {}, "unknown"),
                 ("hooks/list", {}, "unknown"), ("close", {}, "ready"),
                 ("close", {"config": {"plugins": {PLUGIN_ID: {"enabled": False}}}}, "plugin_disabled"),
                 ("close", {"state": {}}, "hooks_untrusted"),
                 ("close", {"inventory": {"data": []}}, "unknown"), ("close_error", {}, "unknown")]
        for boundary, options, expected in cases:
            with self.subTest(boundary=boundary, expected=expected):
                (self.home / "rpc.json").write_text(json.dumps(options))
                (self.home / "rpc.log").unlink(missing_ok=True)
                deadlines = []

                def capture_timeout(seconds, deadlines=deadlines):
                    deadline = timeout(seconds)
                    deadlines.append(deadline)
                    return deadline

                async def expire(deadlines=deadlines):
                    # Exercise the real deadline cancellation at an exact boundary,
                    # without depending on interpreter speed or a sleep race.
                    deadlines[0].reschedule(asyncio.get_running_loop().time())
                    await asyncio.Future()

                @asynccontextmanager
                async def connect(*args, boundary=boundary, expire=expire, **kwargs):
                    if boundary == "connect":
                        await expire()
                    async with unix_connect(*args, **kwargs) as connection:
                        receive = connection.recv

                        async def recv():
                            raw = await receive()
                            identity = {"config/read": 1, "hooks/list": 2}.get(boundary)
                            if identity is not None and json.loads(raw).get("id") == identity:
                                await expire()
                            return raw

                        with patch.object(connection, "recv", side_effect=recv):
                            try:
                                yield connection
                            finally:
                                if boundary == "close":
                                    await expire()
                                if boundary == "close_error":
                                    raise TimeoutError("fixture close failure")

                with patch("router.codex_plugin.unix_connect", side_effect=connect), \
                        patch("router.codex_plugin.asyncio.timeout", side_effect=capture_timeout):
                    result = availability(self.home)
                self.assertEqual(result["state"], expected, result)
                self.assertEqual(deadlines[0].expired(), boundary != "close_error")
                if boundary == "connect":
                    self.assertFalse((self.home / "rpc.log").exists())
                else:
                    calls = [json.loads(line)["request"]["method"]
                             for line in (self.home / "rpc.log").read_text().splitlines()]
                    self.assertEqual(calls[-1], "config/read" if boundary == "config/read" else "hooks/list")
                self.assertEqual(availability(self.home)["state"], expected if boundary == "close" else "ready")

    def test_readiness_honors_the_selected_home_and_project_marker(self):
        self.install("0.9.0")
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        self.configure(entries=trusted)
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), \
                patch("router.codex_plugin.Path.home", side_effect=RuntimeError("default home unavailable")):
            self.assertEqual(availability()["state"], "ready")
        default = self.home / "user-home" / ".codex"
        self.install("0.9.0", home=default)
        (default / "config.toml").write_bytes((self.home / "config.toml").read_bytes())
        self.start_server(default)
        with patch.dict(os.environ), patch("router.codex_plugin.Path.home", return_value=default.parent):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(availability()["state"], "ready")
        with self.subTest(home="empty"), patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(availability()["state"], "unknown")
        long_home = self.home / ("selected-home-" + "x" * 120)
        self.install("0.9.0", home=long_home)
        (long_home / "config.toml").write_bytes((self.home / "config.toml").read_bytes())
        rendezvous = self.home / "rendezvous" / "daemon.sock"
        self.start_server(long_home, endpoint=rendezvous)
        # Native Codex advertises a short rendezvous socket through a long alias.
        control = long_home / "app-server-control"
        control.symlink_to(rendezvous.parent, target_is_directory=True)
        (rendezvous.parent / "app-server-control.sock").symlink_to(rendezvous)
        with self.subTest(home="long socket alias"):
            self.assertEqual(availability(long_home)["state"], "ready")
        control.unlink()
        control.mkdir()
        (control / "app-server-control.sock").symlink_to(rendezvous)
        with self.subTest(home="long socket leaf alias"):
            self.assertEqual(availability(long_home)["state"], "ready")
        copied_home = self.home / "copied-home"
        self.install("0.9.0", home=copied_home)
        (copied_home / "app-server-control").symlink_to(self.home / "app-server-control", target_is_directory=True)
        with self.subTest(home="copied alias serves another home"):
            result = availability(copied_home)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("different Codex home", result["detail"])
        (control / "app-server-control.sock").unlink()
        (control / "app-server-control.sock").symlink_to(self.home / "missing.sock")
        with self.subTest(home="dangling socket alias"):
            self.assertEqual(availability(long_home)["state"], "unknown")
        marker = self.project / ".agent-router.json"
        marker.unlink()
        with self.subTest(marker="missing"):
            self.assertEqual(availability(self.home)["state"], "project_not_enabled")
        marker.write_text('{"observe": false}')
        with self.subTest(marker="invalid"):
            result = availability(self.home)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("Project admission unavailable", result["detail"])
            self.assertNotIn("start Codex", result["detail"])
        marker.write_text('{"observe": true}')
        cache = self.home / "plugins/cache/agent-router/agent-router"
        cache.rename(cache.with_name("saved-cache"))
        cache.write_text("not a directory")
        with self.subTest(home="unreadable cache"):
            result = availability(self.home)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("Codex plugin cache unreadable", result["detail"])
            self.assertNotIn("start Codex", result["detail"])
        with self.subTest(home="unavailable directory"), \
                patch("router.codex_plugin.Path.cwd", side_effect=OSError("fixture directory unavailable")):
            result = availability(self.home)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("Readiness paths unavailable", result["detail"])

    def test_readiness_never_starts_a_codex_process(self):
        self.install("0.9.0")
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        with patch("subprocess.Popen", side_effect=AssertionError("Readiness must not start a process")) as processes:
            self.assertEqual(self.configure(entries=trusted), "ready")
        self.assertEqual(processes.call_count, 0)

    def test_readiness_preserves_daemon_auth_policy_and_client_identity(self):
        self.install("0.9.0")
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        self.configure(entries=trusted)
        identity = self.daemon_identity[self.home]
        for options, expected in (({}, "ready"), ({"mode": "error"}, "unknown")):
            with self.subTest(options=options):
                identity.update(automatic_login=False, user_agent="existing-client")
                original = dict(identity)
                (self.home / "rpc.json").write_text(json.dumps(options))
                self.assertEqual(availability(self.home)["state"], expected)
                self.assertEqual(identity, original)

    def test_setup_registration_is_best_effort_and_never_enables_plugins(self):
        for codes, installed in (([0], True), ([1], False)):
            results = [subprocess.CompletedProcess([], code) for code in codes]
            with self.subTest(codes=codes), patch("router.setup.shutil.which", return_value="/usr/bin/codex"), \
                    patch("router.setup.subprocess.run", side_effect=results) as command, \
                    patch("builtins.print") as output:
                register_codex_marketplace("owner/repo")
            self.assertEqual([call.args[0] for call in command.call_args_list],
                             [["codex", "plugin", "marketplace", "add", "owner/repo"]])
            self.assertEqual("registered" in output.call_args.args[0], installed)
        for failure in (subprocess.TimeoutExpired("codex", 120), OSError("bad interpreter")):
            with self.subTest(failure=failure), patch("router.setup.shutil.which", return_value="/usr/bin/codex"), \
                    patch("router.setup.subprocess.run", side_effect=failure), \
                    patch("builtins.print") as output:
                register_codex_marketplace("owner/repo")
            self.assertIn("Register it later", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
