import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from router.client import request
from router.config import load_settings
from router.db import Store
from router.service import definition, start
from router.setup import init, initialize, setup
from router.snapshot import git


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-setup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_ROUTER_")}
        self.environment.update(AGENT_ROUTER_STATE_DIR=str(self.root / "state"),
                                AGENT_ROUTER_ENV_FILE=str(self.root / "missing.env"))
        self.env = patch.dict(os.environ, self.environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def repository(self, name):
        repo = self.root / name
        repo.mkdir()
        git(repo, "init")
        git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial")
        return repo

    def test_project_opt_in_preserves_other_projects_and_never_runs_setup(self):
        first, second = self.repository("first"), self.repository("second")
        permissions = {"*": "deny", "read": "allow", "edit": "allow"}
        with patch("router.setup.shutil.which", return_value="/usr/bin/opencode"), patch("router.setup.refresh_service"):
            initialize(first, "small=provider/exact", "touch DO_NOT_RUN", permissions, 3, 600, 3)
            initialize(second, "small=provider/exact", "true", permissions, 3, 600, 3)
            initialize(second, "small=provider/exact", "true", None, 3, 600, 3)
        config, _ = load_settings()
        self.assertEqual(set(config.repositories), {str(first.resolve()), str(second.resolve())})
        self.assertEqual(config.models, {"small": "provider/exact"})
        self.assertFalse((first / "DO_NOT_RUN").exists())
        self.assertEqual(json.loads((first / "opencode.json").read_text())["permission"], permissions)
        self.assertEqual((self.root / "state" / "config.json").stat().st_mode & 0o777, 0o600)

    def test_opt_in_rejects_permission_overwrite_and_model_substitution(self):
        repo = self.repository("project")
        original = '{"permission":{"*":"deny"},"other":"preserved"}'
        (repo / "opencode.json").write_text(original)
        with patch("router.setup.shutil.which", return_value="/usr/bin/opencode"), patch("router.setup.refresh_service"):
            with self.assertRaisesRegex(ValueError, "existing"):
                initialize(repo, "small=provider/exact", "true", {"*": "allow"}, 3, 600, 3)
            initialize(repo, "small=provider/exact", "true", None, 3, 600, 3)
            with self.assertRaisesRegex(ValueError, "already"):
                initialize(repo, "small=other/model", "true", None, 3, 600, 3)
        self.assertEqual((repo / "opencode.json").read_text(), original)

    def test_init_confirmation_identifies_existing_opencode_permissions(self):
        repo = self.repository("existing")
        (repo / "opencode.json").write_text('{"permission":{"*":"deny"}}')
        args = Namespace(yes=True, model="small=provider/exact", setup_command="true", permissions=None,
                         sandbox=None, executor="opencode", concurrency=3, timeout=600, attempts=3)
        with patch("router.setup.git", return_value=str(repo)), patch("router.setup.initialize"), \
                patch("router.setup.confirm") as confirmation:
            init(args)
        self.assertIn("existing opencode.json permissions", confirmation.call_args.args[0])
        self.assertNotIn("permissions None", confirmation.call_args.args[0])

    def test_codex_opt_in_needs_no_opencode_and_preserves_both_executors(self):
        repo = self.repository("codex")
        with patch("router.setup.shutil.which", side_effect=lambda name: "/usr/bin/" + name), patch("router.setup.refresh_service"):
            initialize(repo, "codex=openai/gpt-5.3-codex", "true", None, 3, 600, 3,
                       executor="codex", sandbox="workspace-write")
            config, _ = load_settings()
            self.assertTrue(config.delegation_enabled)
            self.assertIsNone(config.opencode_command)
            self.assertEqual(config.codex_command, ["/usr/bin/codex"])
            self.assertFalse((repo / "opencode.json").exists())
            self.assertEqual(config.repositories[str(repo.resolve())]["codex_sandbox"], "workspace-write")
            initialize(repo, "open=provider/exact", "true", {"*": "deny"}, 3, 600, 3)
            config, _ = load_settings()
            self.assertEqual(config.repositories[str(repo.resolve())]["codex_sandbox"], "workspace-write")
            self.assertEqual(set(config.models), {"codex", "open"})
            self.assertIsNotNone(config.opencode_command)


    def test_launch_agent_uses_explicit_runtime_and_shared_private_state(self):
        interpreter = self.root / "managed" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        state = self.root / "state"
        state.mkdir()
        (state / "python-path").write_text(str(interpreter) + "\n")
        with patch("router.service.sys.platform", "darwin"):
            label, path, content = definition()
        job = plistlib.loads(content)
        self.assertEqual(job["Label"], label)
        self.assertEqual(job["ProgramArguments"][-3:], ["router.cli", "daemon", "start"])
        self.assertTrue(Path(job["ProgramArguments"][0]).is_absolute())
        self.assertEqual(job["ProgramArguments"][0], str(interpreter))
        self.assertEqual(job["EnvironmentVariables"]["AGENT_ROUTER_STATE_DIR"], str((self.root / "state").resolve()))
        self.assertTrue(job["RunAtLoad"])
        self.assertNotIn("PROVIDER_TEST_KEY", job["EnvironmentVariables"])
        self.assertEqual(path.parent.name, "LaunchAgents")

    def test_init_does_not_capture_legacy_env_and_rejects_overridden_setup(self):
        old, new = self.repository("legacy"), self.repository("new")
        env_file = self.root / "missing.env"
        env_file.write_text("AGENT_ROUTER_CONCURRENCY_CAP=3\nAGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS=600\n"
            "AGENT_ROUTER_MAX_ATTEMPTS=3\nAGENT_ROUTER_OPENCODE_COMMAND='[\"opencode\"]'\n"
            "AGENT_ROUTER_MODELS='{\"old\":\"provider/old\"}'\n" +
            "AGENT_ROUTER_REPOSITORIES='" + json.dumps({str(old.resolve()): {"setup_command": "true"}}) + "'\n")
        with patch("router.setup.shutil.which", return_value="/usr/bin/opencode"), patch("router.setup.refresh_service"):
            initialize(new, "new=provider/new", "new-setup", {"*": "deny"}, 3, 600, 3)
            stored = json.loads((self.root / "state" / "config.json").read_text())
            self.assertEqual(set(stored["AGENT_ROUTER_REPOSITORIES"]), {str(new.resolve())})
            self.assertEqual(stored["AGENT_ROUTER_MODELS"], {"new": "provider/new"})
            with env_file.open("a") as stream:
                stream.write("AGENT_ROUTER_REPOSITORIES='" + json.dumps({str(new.resolve()): {"setup_command": "override"}}) + "'\n")
            before = (self.root / "state" / "config.json").read_text()
            with self.assertRaisesRegex(ValueError, "override"):
                initialize(new, "new=provider/new", "different-setup", None, 3, 600, 3)
            self.assertEqual((self.root / "state" / "config.json").read_text(), before)

    def test_managed_runtime_path_alias_does_not_reinstall_or_reenter_setup(self):
        tools = self.root / "tools"
        runtime = tools / "agent-router"
        runtime.mkdir(parents=True)
        calls = []
        def command(argv):
            calls.append(argv)
            return str(tools) if argv == ["uv", "tool", "dir"] else ""
        with patch("router.setup.sys.prefix", str(runtime.resolve())), \
                patch("router.setup.sys.executable", str(runtime / "bin" / "python")), \
                patch("router.service.sys.platform", "darwin"), \
                patch("router.setup.shutil.which", return_value="/usr/bin/tool"), \
                patch("router.setup.run", side_effect=command), \
                patch("router.setup.install_codex_plugin") as codex, \
                patch("router.setup.start", return_value={"version": "0.2.0"}):
            setup(yes=True)
        codex.assert_called_once_with("gumbel-ai/agent-router")
        self.assertEqual(calls, [["uv", "tool", "dir"], ["claude", "plugin", "install", "agent-router",
                              "--marketplace", "gumbel-ai/agent-router", "--scope", "user"]])
        pointer = self.root / "state" / "python-path"
        self.assertEqual(pointer.read_text().strip(), str(runtime / "bin" / "python"))
        self.assertEqual(pointer.stat().st_mode & 0o777, 0o600)

    def test_service_start_recovers_offline_tasks_without_killing_a_loaded_job(self):
        state = self.root / "state"
        store = Store(state / "router.sqlite3")
        with store.transaction() as db:
            store.put(db, {"task_id": "interrupted", "session_id": "S", "status": "interrupted", "execution_stopped": False})
        label, path = "test.agent-router", self.root / "service.plist"
        ready = {"version": "0.3.0", "active_tasks": 1}
        for present in (True, False):
            with self.subTest(loaded=present), patch("router.service.definition", return_value=(label, path, b"plist")), \
                    patch("router.service.domain", return_value="gui/test"), patch("router.service.loaded", return_value=present), \
                    patch("router.service.run") as command, \
                    patch("router.service.health", side_effect=lambda: ready if command.called else None):
                self.assertEqual(start(), ready)
                expected = ["launchctl", "kickstart", f"gui/test/{label}"] if present else ["launchctl", "bootstrap", "gui/test", str(path)]
                self.assertEqual(command.call_args.args[0], expected)
                self.assertEqual(command.call_count, 1)
        with patch("router.service.definition", return_value=(label, path, b"plist")), \
                patch("router.service.domain", return_value="gui/test"), patch("router.service.health", return_value=ready), \
                patch("router.service.run") as command, self.assertRaisesRegex(ValueError, "active Router"):
            start(restart=True)
        command.assert_not_called()

    def test_observation_only_daemon_and_published_hooks_span_two_projects(self):
        state = self.root / "state"
        state.mkdir()
        (state / "python-path").write_text(sys.executable + "\n")
        plugin = Path(__file__).resolve().parents[1]
        environment = {**self.environment, "CLAUDE_PLUGIN_ROOT": str(plugin)}
        daemon = subprocess.Popen([sys.executable, "-m", "router.cli", "daemon", "start"],
                                  env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while True:
                if daemon.poll() is not None:
                    out, err = daemon.communicate()
                    self.fail(f"Observation daemon failed: {out!r} {err!r}")
                try:
                    info = request("health", {})
                    break
                except ValueError:
                    if time.monotonic() > deadline:
                        self.fail("Observation daemon did not listen")
                    time.sleep(.03)
            self.assertFalse(info["delegation_enabled"])
            self.assertEqual(request("submit", {"session_id": "observe-only", "prompt_id": "turn", "cwd": str(self.root)}),
                             {"instruction_id": None})
            for number in range(2):
                event = {"hook_event_name": "PostToolUse", "session_id": f"session-{number}",
                         "cwd": str(self.root / f"project-{number}"), "tool_name": "Agent",
                         "tool_input": {"description": "Observed task", "prompt": "DO NOT STORE THIS PROMPT"},
                         "tool_response": {"agentId": f"child-{number}", "isAsync": True}}
                result = subprocess.run(["sh", str(plugin / "hook.sh"), "observe"], env=environment,
                                        input=json.dumps(event), capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {})
            rows = request("status", {})
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["parent_session_id"] for row in rows}, {"session-0", "session-1"})
            self.assertTrue(all(row["control_mode"] == "observe" for row in rows))
            self.assertNotIn("DO NOT STORE", json.dumps(rows))
            for prefix in (self.root / "temporary", self.root / "tools" / "agent-router"):
                (state / "python-path").unlink(missing_ok=True)
                with patch("router.setup.run", return_value=str(self.root / "tools")) as command, \
                        patch("router.setup.sys.prefix", str(prefix)), \
                        patch("router.setup.shutil.which", return_value="/usr/bin/tool"), \
                        patch("router.service.sys.platform", "darwin"), \
                        patch("router.service.domain", return_value="gui/test"), \
                        patch("router.service.loaded", return_value=False), \
                        self.assertRaisesRegex(ValueError, "manually started daemon"):
                    setup(yes=True)
                self.assertEqual([call.args[0] for call in command.call_args_list], [["uv", "tool", "dir"]])
                self.assertFalse((state / "python-path").exists())
            with self.assertRaisesRegex(ValueError, "not configured"):
                request("start", {})
            request("daemon_stop", {"only_if_idle": True})
            daemon.wait(timeout=15)
        finally:
            if daemon.poll() is None:
                daemon.terminate()
                daemon.wait(timeout=15)
            daemon.communicate()
