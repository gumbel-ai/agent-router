"""Codex Langfuse child selection and isolation contracts."""

import hashlib
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from router import codex_tracing
from router.child import child_environment, executor_command
from router.config import Settings


class CodexTracingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plugin = Path(self.temp.name).parent / (Path(self.temp.name).name + "-plugin")
        (self.plugin / ".codex-plugin").mkdir(parents=True)
        (self.plugin / "hooks").mkdir()
        (self.plugin / "dist").mkdir()
        (self.plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
            "name": "tracing", "version": "0.4.2", "hooks": "./hooks/hooks.json"}))
        (self.plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": codex_tracing.HOOK_COMMAND, "timeout": 30,
            "statusMessage": codex_tracing.HOOK_STATUS}]}]}}))
        (self.plugin / "dist/index.mjs").write_text("// reviewed synthetic hook\n")
        (self.plugin / "package.json").write_text(json.dumps({"name": "@langfuse/codex-observability-plugin", "version": "0.4.2"}))
        digest = hashlib.sha256()
        for relative in codex_tracing.PLUGIN_FILES:
            digest.update(relative.encode() + b"\0" + (self.plugin / relative).read_bytes() + b"\0")
        self.pin = patch.object(codex_tracing, "AUDITED_DIGEST", digest.hexdigest())
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.node = patch.object(codex_tracing.shutil, "which", return_value="/usr/bin/node")
        self.node.start()
        self.addCleanup(self.node.stop)
        self.task = {"task_id": "task-1", "child_session_id": "session-1", "resolved_model": "openai/gpt-5.3-codex",
                     "worktree": str(self.root / "worktree"), "artifact_dir": str(self.root / "artifacts"), "executor": "codex", "attempts": 0,
                     "invocation": {"task": "do work", "parent_repo": str(self.root), "parent_worktree": str(self.root)},
                     "permission_profile": {"sandbox": "workspace-write"}}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.plugin, ignore_errors=True)
        self.temp.cleanup()

    def selected_task(self):
        self.task.update(codex_tracing.configure(json.dumps({"plugin_dir": str(self.plugin)}), self.task,
                                                  "host-a", self.root, True))
        return self.task

    def test_selection_adds_trusted_stop_hook_and_router_metadata(self):
        task = self.selected_task()
        settings = Settings()
        settings.codex_command = ["codex"]
        command = executor_command(settings, task)
        self.assertIn(codex_tracing.hook_configuration(), command)
        environment = child_environment({"PATH": "/usr/bin", "LANGFUSE_SECRET_KEY": "secret",
                                         "LANGFUSE_PUBLIC_KEY": "public", "OPENAI_API_KEY": "provider"}, task)
        self.assertEqual(environment["PLUGIN_ROOT"], str(self.plugin.resolve()))
        self.assertEqual(json.loads(environment["LANGFUSE_CODEX_METADATA"])["router_task_id"], "task-1")
        self.assertEqual(environment["LANGFUSE_CODEX_USER_ID"], "agent-router")
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_hook_configuration_is_valid_toml_with_expected_structure(self):
        parsed = tomllib.loads(codex_tracing.hook_configuration())
        self.assertEqual(parsed, {
            "hooks": {
                "Stop": [{"hooks": [{
                    "type": "command",
                    "command": codex_tracing.HOOK_COMMAND,
                    "timeout": 30,
                    "statusMessage": codex_tracing.HOOK_STATUS,
                }]}],
                "state": {
                    codex_tracing.HOOK_TRUST_KEY: {
                        "enabled": True,
                        "trusted_hash": codex_tracing.HOOK_TRUST_HASH,
                    }
                },
            }
        })

    def test_setup_does_not_receive_codex_tracing_credentials(self):
        task = self.selected_task()
        environment = child_environment({"PATH": "/usr/bin", "LANGFUSE_SECRET_KEY": "secret",
                                         "LANGFUSE_PUBLIC_KEY": "public"}, task,
                                         include_tracing_credentials=False)
        self.assertNotIn("LANGFUSE_SECRET_KEY", environment)
        self.assertNotIn("LANGFUSE_CODEX_METADATA", environment)

    def test_unselected_child_drops_ambient_codex_hook_enablement(self):
        task = {**self.task}
        environment = child_environment({"PATH": "/usr/bin", "TRACE_TO_LANGFUSE": "1",
                                         "PLUGIN_ROOT": "/unselected", "LANGFUSE_SECRET_KEY": "secret"}, task)
        self.assertNotIn("TRACE_TO_LANGFUSE", environment)
        self.assertNotIn("PLUGIN_ROOT", environment)

    def test_native_thread_reconciles_router_correlation(self):
        del self.task["child_session_id"]
        task = self.selected_task()
        from router.codex import collect
        thread = "12345678-1234-4321-8765-123456789abc"
        collect(task, {"type": "thread.started", "thread_id": thread})
        self.assertEqual(task["child_session_id"], thread)
        self.assertEqual(task["tracing"]["correlation"]["session_id"], thread)
        self.assertTrue(task["thread_observed"])
        self.assertNotIn("execution_error", task)

    def test_hook_path_excludes_delegated_repositories(self):
        task = self.selected_task()
        environment = child_environment({"PATH": "/usr/bin", "NODE_OPTIONS": "--require x",
                                         "LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": "secret"}, task)
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertNotIn("NODE_OPTIONS", environment)
        codex_tracing.shutil.which.return_value = None
        with self.assertRaisesRegex(ValueError, "node"):
            child_environment({"PATH": "/usr/bin"}, task)
        codex_tracing.shutil.which.return_value = str(self.root / "worktree" / "node")
        with self.assertRaisesRegex(ValueError, "node"):
            child_environment({"PATH": "/usr/bin"}, task)

    def test_missing_credentials_disable_export_and_pin_endpoint(self):
        task = self.selected_task()
        environment = child_environment({"PATH": "/usr/bin", "LANGFUSE_BASEURL": "https://langfuse.example",
                                         "LANGFUSE_ENVIRONMENT": "ci"}, task)
        self.assertEqual(environment["TRACE_TO_LANGFUSE"], "0")
        self.assertEqual(environment["LANGFUSE_BASE_URL"], "https://langfuse.example")
        self.assertEqual(environment["LANGFUSE_TRACING_ENVIRONMENT"], "ci")

    def test_selection_allows_native_session_to_be_assigned_after_launch(self):
        task = {key: value for key, value in self.task.items() if key != "child_session_id"}
        selected = codex_tracing.configure(json.dumps({"plugin_dir": str(self.plugin)}), task,
                                           "host-a", self.root, True)
        self.assertNotIn("session_id", selected["tracing"]["correlation"])

    def test_changed_plugin_refuses_launch(self):
        task = self.selected_task()
        (self.plugin / "dist/index.mjs").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Codex tracing"):
            settings = Settings()
            settings.codex_command = ["codex"]
            executor_command(settings, task)


if __name__ == "__main__":
    unittest.main()
