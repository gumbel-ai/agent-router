"""Claude tracing command/configuration contracts; no native runtime or collector."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_runtime import RuntimeHarness

from router import claude_tracing
from router.child import (
    LANGFUSE_ENVIRONMENT_KEYS,
    child_environment,
    executor_command,
    langfuse_credentials_available,
    launch,
)

MODEL = "anthropic/claude-sonnet-4-6"
PROFILE = {"tools": ["Read", "Edit", "Write"], "allow": ["Read", "Edit", "Write"]}


class ClaudeTracingTests(RuntimeHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.plugin = self.root / "selected-plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / "hooks").mkdir()
        (self.plugin / ".claude-plugin/plugin.json").write_text(json.dumps({
            "name": "langfuse-observability", "version": "1.2.1",
            "userConfig": {"LANGFUSE_SECRET_KEY": {"type": "string", "sensitive": True}}}))
        (self.plugin / "hooks/hooks.json").write_text('{"hooks":{"SessionEnd":[]}}')
        (self.plugin / "hooks/langfuse_hook.py").write_text("# operator-approved synthetic plugin\n")
        digest = hashlib.sha256()
        for relative in claude_tracing.PLUGIN_FILES:
            digest.update(relative.encode() + b"\0" + (self.plugin / relative).read_bytes() + b"\0")
        # Exercise source pinning with a synthetic fixture; never execute the real exporter.
        pinned = patch("router.claude_tracing.AUDITED_DIGEST", digest.hexdigest())
        pinned.start()
        self.addCleanup(pinned.stop)
        self.selection = {"plugin_dir": str(self.plugin)}
        # Synthetic startup credentials; never the developer's ambient values.
        for key in tuple(self.control.environment):
            if key.startswith("LANGFUSE_"):
                self.control.environment.pop(key)
        self.credentials = {"LANGFUSE_PUBLIC_KEY": "synthetic-public", "LANGFUSE_SECRET_KEY": "synthetic-secret",
                            "LANGFUSE_BASE_URL": "https://collector.example.invalid", "LANGFUSE_USER_ID": "synthetic-user"}
        self.control.environment.update(self.credentials)
        wrapper = self.root / "claude-fixture"
        fixture = Path(__file__).with_name("fixture_executor.py")
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fixture))} "$@"\n')
        wrapper.chmod(0o700)
        self.config.claude_command = [str(wrapper)]
        self.config.models["test"] = MODEL
        self.repository = self.config.repositories[str(self.repo.resolve())]
        self.repository["claude_permissions"] = json.dumps(PROFILE)
        self.repository["claude_tracing"] = json.dumps(self.selection)
        self.operator_bin = self.root / "operator-bin"
        self.operator_bin.mkdir()
        uv = self.operator_bin / "uv"
        uv.write_text("#!/bin/sh\nexit 0\n")
        uv.chmod(0o700)
        self.control.environment["PATH"] = str(self.operator_bin) + os.pathsep + self.control.environment.get("PATH", os.defpath)

    def test_opt_in_fresh_and_resume_keep_permissions_identity_and_private_selection(self):
        work_item = "w" * 240
        task_id = self.mutate("start", {"request_version": "1", "request_id": "R", "instruction_id": self.instruction,
            "executor": "claude", "model": "test", "task": "finished", "parent_repo": str(self.repo),
            "parent_worktree": str(self.repo), "mode": "write", "work_item_id": work_item})["task_id"]
        first = self.wait(task_id, {"awaiting_review", "failed"})
        self.assertEqual(first["status"], "awaiting_review", first)
        raw = self.control.store.get(first["task_id"])
        self.assertTrue(first.get("tracing", {}).get("requested"), first)
        self.assertNotIn("_claude_tracing", first)
        self.assertNotIn(str(self.plugin), json.dumps(first))
        self.assertIs(first["tracing"]["available"], True)
        self.assertEqual(first["tracing"]["correlation"]["session_id"], first["child_session_id"])
        self.assertEqual(first["tracing"]["correlation"]["work_item_id"], work_item)
        self.assertIn("work_item_id", first["tracing"]["omitted_tags"])
        self.mutate("review", {"task_id": first["task_id"], "request_id": "resume", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Second synthetic attempt"})
        second = self.wait(first["task_id"], {"awaiting_review", "failed"})
        self.assertEqual(second["status"], "awaiting_review", second)
        self.assertEqual(second["child_session_id"], first["child_session_id"])
        self.assertEqual(second["worktree"], first["worktree"])
        self.assertEqual(second["cost"], .5)
        self.assertEqual(second["usage"]["input_tokens"], 200)
        self.assertEqual(raw["_claude_tracing"], self.control.store.get(first["task_id"])["_claude_tracing"])
        for frame in second["attempt_history"]:
            self.assertNotIn("_claude_tracing", frame)
        self.assertNotIn("_claude_tracing", json.dumps(self.control.store.history(first["task_id"])))
        commands = json.loads((Path(second["worktree"]) / "session.json").read_text())["commands"]
        for command in commands:
            self.assertIn("--restricted", command)
            self.assertNotIn("--safe-mode", command)
            self.assertEqual(command[command.index("--plugin-dir") + 1], str(self.plugin.resolve()))
            self.assertEqual(command[command.index("--setting-sources") + 1], "")
            self.assertEqual(command[command.index("--tools") + 1], ",".join(PROFILE["tools"]))
            settings = json.loads(command[command.index("--settings") + 1])
            self.assertEqual(settings["permissions"]["allow"], PROFILE["allow"])
            self.assertEqual(settings["fallbackModel"], [])
            self.assertIs(settings["switchModelsOnFlag"], False)
            self.assertNotIn("pluginConfigs", settings)
        self.assertIn("--session-id", commands[0])
        self.assertIn("--resume", commands[1])
        exported = self.control.handle("correlation", {})
        self.assertNotIn("_claude_tracing", json.dumps(exported))
        child_records = [row for row in exported["records"] if row["kind"] in {"router_task", "attempt"}]
        self.assertEqual(len(child_records), 3)
        for row in child_records:
            self.assertEqual(row["work_item_id"], work_item)
            self.assertEqual(row["traced"], "unknown")
        self.assertNotIn(str(self.plugin), json.dumps(self.control.router_task_records()))

    def test_opt_in_environment_keeps_native_auth_but_excludes_ambient_customization(self):
        self.runtime.stop()
        task = {"task_id": "task-synthetic", "work_item_id": "work-synthetic", "invocation": {
            "executor": "claude", "model": "test", "parent_worktree": str(self.repo), "parent_repo": str(self.repo)}}
        task.update(self.runtime.validate(task), worktree=str(self.root / "worker"), artifact_dir=str(self.root / "artifacts"))
        environment = {"HOME": "/synthetic/operator", "CLAUDE_CONFIG_DIR": "/synthetic/native",
                       "ANTHROPIC_API_KEY": "synthetic-provider", "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-oauth",
                       "HTTPS_PROXY": "https://proxy.example.invalid", "PATH": os.pathsep.join((
                           str(Path(task["worktree"]) / ".venv/bin"), str(self.repo / "bin"), ".", str(self.operator_bin), "/usr/bin")),
                       "CLAUDE_CODE_PLUGIN_DIRS": "/unselected", "CLAUDE_CODE_PLUGIN_SEED_DIR": "/unselected",
                       "CLAUDE_CODE_PLUGIN_CACHE_DIR": "/unselected", "CLAUDE_CODE_SAFE_MODE": "1",
                       "CLAUDE_CODE_SIMPLE": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
                       "CLAUDE_PLUGIN_OPTION_LANGFUSE_SECRET_KEY": "synthetic-no-export",
                       "LANGFUSE_SECRET_KEY": "synthetic-no-export", "CC_LANGFUSE_SECRET_KEY": "synthetic-no-export",
                       "CC_LANGFUSE_TRACEPARENT": "ambient-parent", "CC_LANGFUSE_PARENT_TRACE_ID": "ambient-parent",
                       "TRACE_TO_LANGFUSE": "true", "BASH_ENV": "/unselected", "NODE_OPTIONS": "--require /unselected"}
        overrides = {key: str(Path(task["worktree"]) / "untrusted") for key in (
            "UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "UV_ENV_FILE", "UV_CONFIG_FILE", "UV_CACHE_DIR",
            "PYTHONUSERBASE", "PYTHONHOME", "_PYTHON_SYSCONFIGDATA_PATH", "PIP_FIND_LINKS", "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES", "__PYVENV_LAUNCHER__", "BASH_FUNC_hook%%")}
        environment.update(overrides)
        storage_overrides = {key: str(Path(task["worktree"]) / "untrusted") for key in (
            "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_BIN_HOME", "TMPDIR", "TMP", "TEMP")}
        environment.update(storage_overrides)
        actual = child_environment(environment, task)
        for key in ("HOME", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY",
                    "LANGFUSE_SECRET_KEY"):
            self.assertEqual(actual[key], environment[key])
        for key in ("CLAUDE_CODE_PLUGIN_DIRS", "CLAUDE_CODE_PLUGIN_SEED_DIR", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE",
                    "CLAUDE_PLUGIN_OPTION_LANGFUSE_SECRET_KEY", "CC_LANGFUSE_SECRET_KEY",
                    "CC_LANGFUSE_TRACEPARENT", "CC_LANGFUSE_PARENT_TRACE_ID", "TRACE_TO_LANGFUSE", "BASH_ENV", "NODE_OPTIONS"):
            self.assertNotIn(key, actual)
        self.assertEqual(actual["PATH"], str(self.operator_bin) + os.pathsep + "/usr/bin")
        self.assertEqual(actual["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(actual["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        for key in overrides.keys() - {"UV_PYTHON", "UV_CACHE_DIR"}:
            self.assertNotIn(key, actual)
        self.assertEqual(actual["UV_PYTHON"], str(Path(sys.executable).resolve()))
        self.assertEqual(actual["UV_PYTHON_DOWNLOADS"], "never")
        for key in ("UV_NO_CONFIG", "UV_NO_ENV_FILE", "PYTHONNOUSERSITE", "PYTHONSAFEPATH"):
            self.assertEqual(actual[key], "1")
        self.assertTrue(Path(actual["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).is_relative_to(task["artifact_dir"]))
        private = Path(task["artifact_dir"]) / "claude-tracing"
        self.assertEqual(actual["UV_CACHE_DIR"], str(private / "runtime-cache"))
        for key in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(actual[key], str(private / "runtime-tmp"))
        for key in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_BIN_HOME"):
            # Native authentication/config locations stay unchanged; explicit uv
            # cache/Python and no-config/no-downloads prevent runtime discovery here.
            self.assertEqual(actual[key], environment[key])
        real_uv = shutil.which("uv")
        if real_uv is not None:
            probe_env = {key: value for key, value in actual.items() if key in {
                "HOME", "PATH", "UV_CACHE_DIR", "UV_PYTHON", "UV_PYTHON_DOWNLOADS", "UV_NO_CONFIG", "UV_NO_ENV_FILE",
                "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR", "TMP", "TEMP"}}
            probe = subprocess.run([real_uv, "--no-config", "cache", "dir"], env=probe_env, cwd=self.repo,
                                   capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(Path(probe.stdout.strip()).resolve(), private / "runtime-cache")
        self.assertEqual(actual["CC_LANGFUSE_STATE_DIR"], str(private / "state"))
        self.assertEqual(actual["CC_LANGFUSE_TRACE_SEED"], task["child_session_id"])
        tags = json.loads(actual["CC_LANGFUSE_TRACE_TAGS"])
        self.assertEqual(tags, ["router_task_id:task-synthetic", "router_host_id:" + self.control.host.host_id,
                                "work_item_id:work-synthetic"])
        unchanged = {key: value for key, value in task.items() if key not in {"_claude_tracing", "tracing"}}
        default = child_environment(environment, unchanged)
        self.assertNotIn("LANGFUSE_SECRET_KEY", default)
        self.assertEqual(default["CLAUDE_CODE_PLUGIN_DIRS"], environment["CLAUDE_CODE_PLUGIN_DIRS"])

        for work_item in (None, "short", "x" * (200 - len("work_item_id:")),
                          "x" * (201 - len("work_item_id:")), "x" * 240):
            with self.subTest(work_item_length=None if work_item is None else len(work_item)):
                changed = {key: value for key, value in task.items() if key != "work_item_id"}
                if work_item is not None:
                    changed["work_item_id"] = work_item
                changed.update(self.runtime.validate(changed))
                selected_tags = json.loads(child_environment(environment, changed)["CC_LANGFUSE_TRACE_TAGS"])
                self.assertTrue(all(len(tag) <= 200 for tag in selected_tags))
                supported = work_item is not None and len("work_item_id:" + work_item) <= 200
                self.assertEqual(len(selected_tags), 3 if supported else 2)
                self.assertEqual(changed["tracing"]["correlation"].get("work_item_id"), work_item)
                self.assertEqual("omitted_tags" in changed["tracing"], work_item is not None and not supported)

    def _opted_task(self, task_id="task-synthetic"):
        task = {"task_id": task_id, "invocation": {
            "executor": "claude", "model": "test", "parent_worktree": str(self.repo), "parent_repo": str(self.repo)}}
        task.update(self.runtime.validate(task), worktree=str(self.root / "worker"), artifact_dir=str(self.root / "artifacts"))
        return task

    def test_availability_follows_startup_credentials(self):
        self.runtime.stop()
        self.assertIs(self._opted_task()["tracing"]["available"], True)
        for missing in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            with self.subTest(missing=missing):
                value = self.control.environment.pop(missing)
                tracing = self._opted_task()["tracing"]
                self.assertIs(tracing["available"], False)
                self.assertIn("credentials are unavailable", tracing["reason"])
                self.control.environment[missing] = value

    def test_only_opted_claude_child_phase_receives_startup_credentials(self):
        self.runtime.stop()
        environment = {**self.credentials, "PATH": str(self.operator_bin) + os.pathsep + "/usr/bin",
                       "LANGFUSE_UNDOCUMENTED": "synthetic-extra"}
        task = self._opted_task()
        child = child_environment(environment, task)
        for key in self.credentials:
            self.assertEqual(child[key], self.credentials[key])
        self.assertNotIn("LANGFUSE_UNDOCUMENTED", child)
        setup = child_environment(environment, task, include_tracing_credentials=False)
        unopted = child_environment(environment, {key: value for key, value in task.items()
                                                  if key not in {"_claude_tracing", "tracing"}})
        # Setup keeps its normal dependency environment; hook restrictions are child-only.
        self.assertEqual(setup["PATH"].split(os.pathsep)[0], str(Path(task["worktree"]) / ".venv" / "bin"))
        self.assertNotIn("CC_LANGFUSE_TRACE_TAGS", setup)
        for name, actual in (("setup", setup), ("unopted", unopted)):
            with self.subTest(phase=name):
                self.assertTrue(all(key not in actual for key in LANGFUSE_ENVIRONMENT_KEYS))
        codex = child_environment(environment, {"executor": "codex", "worktree": task["worktree"]})
        self.assertFalse(any(key.startswith("LANGFUSE_") for key in codex))

    def test_claude_alias_endpoint_maps_to_canonical_name(self):
        self.runtime.stop()
        task = self._opted_task()
        base = {**self.credentials, "PATH": str(self.operator_bin)}
        alias = {key: value for key, value in base.items() if key != "LANGFUSE_BASE_URL"}
        alias["LANGFUSE_BASEURL"] = "https://alias.example.invalid"
        self.assertEqual(child_environment(alias, task)["LANGFUSE_BASE_URL"], "https://alias.example.invalid")
        blank = {**alias, "LANGFUSE_BASE_URL": ""}
        self.assertEqual(child_environment(blank, task)["LANGFUSE_BASE_URL"], "https://alias.example.invalid")
        both = {**base, "LANGFUSE_BASEURL": "https://alias.example.invalid"}
        self.assertEqual(child_environment(both, task)["LANGFUSE_BASE_URL"], self.credentials["LANGFUSE_BASE_URL"])

    def test_revoked_or_superseded_selection_refuses_without_untraced_replacement(self):
        self.runtime.stop()
        task = self._opted_task()
        environment = {"PATH": str(self.operator_bin)}
        revoked = {key: value for key, value in task.items() if key != "tracing"}
        with self.assertRaisesRegex(ValueError, "revoked after selection"):
            child_environment(environment, revoked)
        legacy = {**task, "_claude_tracing": {**task["_claude_tracing"], "schema_version": 1,
                                              "public_key": "synthetic-public",
                                              "base_url": "https://collector.example.invalid"}}
        with self.assertRaisesRegex(ValueError, "superseded native-option format"):
            child_environment(environment, legacy)
        self.assertTrue(self.runtime._tracing_requested(task))
        self.repository.pop("claude_tracing")
        self.assertFalse(self.runtime._tracing_requested(task))
        # A Claude selection never authorizes another executor's tracing route.
        self.repository["claude_tracing"] = json.dumps(self.selection)
        self.assertFalse(self.runtime._tracing_requested({"invocation": task["invocation"]}))

    def test_resumed_attempt_refreshes_availability_and_keeps_earlier_attempt(self):
        def set_credentials(present):
            for key in LANGFUSE_ENVIRONMENT_KEYS:
                self.control.environment.pop(key, None)
            if present:
                self.control.environment.update(self.credentials)

        def recording_launch(command, cwd, environment, directory, register, *, disposable=False):
            if directory.name == "child":
                launched.append(langfuse_credentials_available(environment))
            return launch(command, cwd, environment, directory, register, disposable=disposable)

        for before, after in ((True, False), (False, True)):
            with self.subTest(before=before, after=after):
                launched = []
                set_credentials(before)
                with patch("router.daemon.launch", side_effect=recording_launch):
                    task_id = self.start("finished", f"resume-{before}-{after}", executor="claude")
                    first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
                    self.assertEqual(first["status"], "awaiting_review", first)
                    # Models a daemon restart with a changed credential environment.
                    set_credentials(after)
                    self.mutate("review", {"task_id": task_id, "request_id": f"reject-{before}-{after}",
                                           "expected_attempt": 1, "outcome": "rejected", "feedback": "Correct it"})
                    second = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
                self.assertEqual(second["status"], "awaiting_review", second)
                self.assertEqual(second["attempts"], 2)
                self.assertEqual(launched, [before, after])
                self.assertEqual(second["child_session_id"], first["child_session_id"])
                expected = [{**first["tracing"], **claude_tracing.availability(value)} for value in (before, after)]
                self.assertEqual(first["tracing"], expected[0])
                self.assertEqual(second["tracing"], expected[1])
                self.assertEqual([attempt["tracing"] for attempt in second["attempt_history"]], expected)

    def test_opted_claude_validation_git_subprocesses_strip_credentials(self):
        self.runtime.stop()
        run = subprocess.run
        environments = []

        def record_run(command, *args, **kwargs):
            if command[0] == "git":
                environments.append(dict(os.environ if kwargs.get("env") is None else kwargs["env"]))
            return run(command, *args, **kwargs)

        with (patch.dict(os.environ, self.credentials),
              patch("router.snapshot.subprocess.run", side_effect=record_run)):
            self.assertIs(self._opted_task()["tracing"]["available"], True)
        self.assertTrue(environments)
        for actual in environments:
            self.assertFalse(any(key.startswith("LANGFUSE_") for key in actual))

    def test_private_roots_and_hook_interpreter_refuse_delegated_links(self):
        self.runtime.stop()
        task = {"task_id": "task-synthetic", "invocation": {
            "executor": "claude", "model": "test", "parent_worktree": str(self.repo), "parent_repo": str(self.repo)}}
        task.update(self.runtime.validate(task), worktree=str(self.root / "worker"), artifact_dir=str(self.root / "artifacts"))
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        base = Path(task["artifact_dir"]) / "claude-tracing"
        base.mkdir(parents=True)
        for name in ("plugins", "state", "runtime-cache", "runtime-tmp"):
            with self.subTest(component=name):
                (base / name).symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "without symlinks"):
                    child_environment({"PATH": str(self.operator_bin)}, task)
                self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
                (base / name).unlink()
        runtime_bin = self.root / "runtime-bin"
        runtime_bin.mkdir()
        delegated = Path(task["worktree"])
        delegated.mkdir()
        untrusted = delegated / "python"
        untrusted.write_text("#!/bin/sh\nexit 99\n")
        untrusted.chmod(0o700)
        for executable in ("uv", "router-python"):
            with self.subTest(executable=executable):
                if executable == "uv":
                    (runtime_bin / executable).symlink_to(untrusted)
                    with self.assertRaisesRegex(ValueError, "interpreter must be outside"):
                        child_environment({"PATH": str(runtime_bin) + os.pathsep + str(self.operator_bin)}, task)
                    (runtime_bin / executable).unlink()
                else:
                    with (patch("router.claude_tracing.sys", SimpleNamespace(executable=str(untrusted))),
                          self.assertRaisesRegex(ValueError, "interpreter must be outside")):
                        child_environment({"PATH": str(self.operator_bin)}, task)
        with self.assertRaisesRegex(ValueError, "existing operator-provisioned uv"):
            child_environment({"PATH": str(runtime_bin)}, task)
        for root in (delegated, self.repo):
            with (self.subTest(private_directory_root=root),
                  self.assertRaisesRegex(ValueError, "runtime/cache/tmp must be outside")):
                child_environment({"PATH": str(self.operator_bin)}, {**task, "artifact_dir": str(root / "runtime")})

    def test_invalid_selection_refuses_before_any_executor_or_worktree(self):
        cases = ["{", "null", json.dumps({**self.selection, "secret_key": "synthetic"}),
                 json.dumps({**self.selection, "public_key": "synthetic-public"}),
                 json.dumps({"plugin_dir": "relative/plugin"}),
                 json.dumps({**self.selection, "plugin_dir": str(self.root / "absent")})]
        for number, selected in enumerate(cases):
            with self.subTest(selected=number):
                self.repository["claude_tracing"] = selected
                result = self.wait(self.start("must not launch", f"invalid-{number}", executor="claude"), {"failed", "awaiting_review"})
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["attempts"], 0)
                self.assertNotIn("worktree", result)
                self.assertNotIn("process", result)
                self.assertIn("Claude tracing", result["failure_reason"])
        self.repository["claude_tracing"] = json.dumps(self.selection)
        manifest_file = self.plugin / ".claude-plugin/plugin.json"
        original = manifest_file.read_text()
        for number, change in enumerate(({"version": "1.2.2"}, {"mcpServers": {"unselected": {}}},
                                        {"description": "same version, different source"})):
            with self.subTest(source=change):
                manifest_file.write_text(json.dumps({**json.loads(original), **change}))
                result = self.wait(self.start("must not launch", f"source-{number}", executor="claude"), {"failed", "awaiting_review"})
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["attempts"], 0)
                self.assertNotIn("process", result)
        manifest_file.write_text(original)

    def test_plugin_change_refuses_resume_and_command_after_setup_without_fallback(self):
        first = self.wait(self.start("finished", executor="claude"), {"awaiting_review", "failed"})
        self.assertEqual(first["status"], "awaiting_review", first)
        selected = self.control.store.get(first["task_id"])
        with self.assertRaisesRegex(ValueError, "refusing an untraced replacement"):
            executor_command(self.config, {key: value for key, value in selected.items() if key != "_claude_tracing"})
        (self.plugin / "hooks/langfuse_hook.py").write_text("# changed after operator selection\n")
        with self.assertRaisesRegex(ValueError, "Claude tracing"):
            executor_command(self.config, selected)
        self.mutate("review", {"task_id": first["task_id"], "request_id": "changed", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Keep the original source"})
        result = self.wait(first["task_id"], {"failed", "awaiting_review"})
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["child_session_id"], first["child_session_id"])
        commands = json.loads((Path(first["worktree"]) / "session.json").read_text())["commands"]
        self.assertEqual(len(commands), 1)
