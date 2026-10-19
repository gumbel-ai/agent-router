"""Opt-in child tracing: recorded explicitly, never changing child launch or Router counters."""

import json
import shlex
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_control import settings
from test_runtime import RuntimeHarness

from router.child import (
    LANGFUSE_ENVIRONMENT_KEYS,
    OPENCODE_LANGFUSE_PLUGIN,
    TRACING_UNAVAILABLE,
    child_environment,
    executor_command,
    strip_langfuse_environment,
)

CLAUDE_MODEL = "anthropic/claude-sonnet-4-6"
CLAUDE_PROFILE = {"tools": ["Read", "Edit", "Write"], "allow": ["Read", "Edit", "Write"]}


class TracingTests(RuntimeHarness, unittest.TestCase):
    def configure(self, executor):
        config = self.config.repositories[str(self.repo.resolve())]
        if executor == "opencode":
            self.config.models["test"] = "provider/exact"
        elif executor == "codex":
            self.config.codex_command = self.config.opencode_command
            self.config.models["test"] = "openai/gpt-5.3-codex"
            config["codex_sandbox"] = "workspace-write"
        else:
            wrapper = self.root / "claude-fixture"
            fixture = Path(__file__).with_name("fixture_executor.py")
            wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fixture))} "$@"\n')
            wrapper.chmod(0o700)
            self.config.claude_command = [str(wrapper)]
            self.config.models["test"] = CLAUDE_MODEL
            config["claude_permissions"] = json.dumps(CLAUDE_PROFILE)
        return config

    def test_requested_tracing_records_route_and_keeps_native_counters(self):
        for executor in ("opencode", "codex", "claude"):
            with self.subTest(executor=executor):
                config = self.configure(executor)
                config.pop("tracing", None)
                plain = self.wait(self.start("plain", f"{executor}-plain", executor=executor),
                                  {"awaiting_review", "failed", "interrupted"})
                self.assertEqual(plain["status"], "awaiting_review", plain)
                self.assertNotIn("tracing", plain)
                config["tracing"] = "requested"
                traced = self.wait(self.start("traced", f"{executor}-traced", executor=executor),
                                   {"awaiting_review", "failed", "interrupted"})
                self.assertEqual(traced["status"], "awaiting_review", traced)
                if executor == "opencode":
                    expected = {"requested": True, "available": False,
                                "reason": "OpenCode plugin route is configured; live export and Router correlation are unverified",
                                "plugin": OPENCODE_LANGFUSE_PLUGIN,
                                "correlation": {"session_id": traced["child_session_id"],
                                                "router_task_id": traced["task_id"],
                                                "host_id": self.control.host.host_id}}
                else:
                    expected = {"requested": True, "available": False, "reason": TRACING_UNAVAILABLE[executor]}
                self.assertEqual(traced["tracing"], expected)
                self.assertEqual(traced["attempt_history"][0]["tracing"], expected)
                for key in ("usage", "usage_complete", "cost", "cost_source"):
                    self.assertEqual(traced[key], plain[key], key)

    def test_invalid_tracing_value_refuses_before_launch(self):
        config = self.configure("codex")
        config["tracing"] = "true"
        task = self.wait(self.start("must not launch", executor="codex"), {"failed", "awaiting_review"})
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["attempts"], 0)
        self.assertNotIn("worktree", task)
        self.assertNotIn("tracing", task)

    def test_reloaded_repository_opt_out_disables_stale_tracing(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        task = {"invocation": {"parent_worktree": str(self.repo)}}
        self.assertTrue(self.runtime._tracing_requested(task))
        config.pop("tracing")
        self.assertFalse(self.runtime._tracing_requested(task))

    def test_runtime_phase_only_restores_credentials_for_the_executor_child(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        self.control.environment.update({"LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret"})
        task_id = "phase-tracing"
        task = {"task_id": task_id, "session_id": "S", "executor": "opencode", "status": "running",
                "attempt_number": 1, "artifact_dir": str(self.root / "artifacts"), "deadline_at": 9_999_999_999_999,
                "invocation": {"parent_worktree": str(self.repo)}, "worktree": str(self.root / "child"),
                "permission_profile": {"*": "allow"}, "tracing": {"requested": True, "available": False}}
        with self.control.store.transaction() as db:
            self.control.store.put(db, task)
        environments = []

        def fake_launch(command, cwd, environment, directory, register, *, disposable=False):
            environments.append(environment)
            return SimpleNamespace(poll=lambda: 0), {}

        with patch("router.daemon.launch", side_effect=fake_launch), \
                patch.object(self.runtime, "observe", return_value={"exit_code": 0}):
            self.runtime.phase(task_id, "setup", ["setup"], self.root)
            self.runtime.phase(task_id, "child", ["child"], self.root)
        self.assertNotIn("LANGFUSE_PUBLIC_KEY", environments[0])
        self.assertNotIn("LANGFUSE_SECRET_KEY", environments[0])
        self.assertEqual(environments[1]["LANGFUSE_PUBLIC_KEY"], "pk-secret")
        self.assertEqual(environments[1]["LANGFUSE_SECRET_KEY"], "sk-secret")


class TracingLaunchTests(unittest.TestCase):
    def test_router_phase_environment_stripping_does_not_mutate_source(self):
        environment = {"PATH": "/usr/bin", "LANGFUSE_SECRET_KEY": "sk-secret", "OTHER": "kept"}
        self.assertEqual(strip_langfuse_environment(environment), {"PATH": "/usr/bin", "OTHER": "kept"})
        self.assertEqual(environment["LANGFUSE_SECRET_KEY"], "sk-secret")

    def test_tracing_off_strips_credentials_and_does_not_add_plugin(self):
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept",
                       "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret",
                       "LANGFUSE_USER_ID": "operator", "LANGFUSE_UNDOCUMENTED": "must-strip"}
        task = {"executor": "opencode", "permission_profile": {"*": "allow"},
                "resolved_model": "provider/exact", "worktree": "/tmp/child"}
        child = child_environment(environment, task)
        self.assertEqual(json.loads(child["OPENCODE_CONFIG_CONTENT"]), {"permission": {"*": "allow"}})
        self.assertEqual(child["EXAMPLE_OPTION"], "kept")
        self.assertTrue(all(key not in child for key in (*LANGFUSE_ENVIRONMENT_KEYS, "LANGFUSE_UNDOCUMENTED")))

    def test_requested_opencode_adds_pinned_plugin_preserves_permission_and_passes_credentials(self):
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept",
                       "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret",
                       "LANGFUSE_BASEURL": "https://langfuse.invalid", "LANGFUSE_USER_ID": "operator",
                       "LANGFUSE_UNDOCUMENTED": "must-strip"}
        permission = {"*": "allow", "external_directory": "deny"}
        task = {"executor": "opencode", "permission_profile": permission,
                "resolved_model": "provider/exact", "worktree": "/tmp/child",
                "tracing": {"requested": True, "available": False,
                             "reason": "OpenCode plugin route is configured; live export and Router correlation are unverified"}}
        child = child_environment(environment, task)
        self.assertEqual(json.loads(child["OPENCODE_CONFIG_CONTENT"]),
                         {"permission": permission, "plugins": [OPENCODE_LANGFUSE_PLUGIN]})
        for key in LANGFUSE_ENVIRONMENT_KEYS:
            if key in environment:
                self.assertEqual(child[key], environment[key])
        self.assertNotIn("LANGFUSE_UNDOCUMENTED", child)
        self.assertEqual(child["EXAMPLE_OPTION"], "kept")

    def test_setup_environment_never_receives_tracing_plugin_or_credentials(self):
        environment = {"PATH": "/usr/bin", "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret"}
        task = {"executor": "opencode", "permission_profile": {"*": "allow"},
                "resolved_model": "provider/exact", "worktree": "/tmp/child",
                "tracing": {"requested": True, "available": False}}
        setup = child_environment(environment, task, include_tracing_credentials=False)
        self.assertEqual(json.loads(setup["OPENCODE_CONFIG_CONTENT"]), {"permission": {"*": "allow"}})
        self.assertNotIn("LANGFUSE_PUBLIC_KEY", setup)
        self.assertNotIn("LANGFUSE_SECRET_KEY", setup)

    def test_non_opencode_children_never_receive_langfuse_credentials(self):
        config = settings(Path("/tmp/project"), command=["fixture"])
        config.codex_command = ["codex"]
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept",
                       "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret"}
        tasks = {
            "codex": {"executor": "codex", "permission_profile": {"sandbox": "workspace-write"},
                      "resolved_model": "openai/gpt-5.3-codex"},
            "claude": {"executor": "claude", "permission_profile": CLAUDE_PROFILE, "resolved_model": CLAUDE_MODEL,
                       "child_session_id": "12345678-1234-4321-8765-123456789abc",
                       "claude_executable": "/usr/bin/claude", "executor_version": "2.1.303"},
        }
        for executor, fields in tasks.items():
            with self.subTest(executor=executor):
                plain = {**fields, "invocation": {"task": "work"}, "attempts": 0, "worktree": "/tmp/child"}
                traced = {**plain, "tracing": {"requested": True, "available": False,
                                               "reason": TRACING_UNAVAILABLE[executor]}}
                self.assertEqual(executor_command(config, traced), executor_command(config, plain))
                child = child_environment(environment, traced)
                self.assertTrue(all(key not in child for key in LANGFUSE_ENVIRONMENT_KEYS))

    def test_opencode_resume_reuses_the_correlated_native_session(self):
        config = settings(Path("/tmp/project"), command=["fixture"])
        session = "ses-router-stable"
        task = {"executor": "opencode", "resolved_model": "provider/exact",
                "child_session_id": session, "attempts": 0,
                "invocation": {"task": "work"}}
        first = executor_command(config, task)
        resumed = executor_command(config, {**task, "attempts": 1})
        self.assertEqual(first[first.index("--session") + 1], session)
        self.assertEqual(resumed[resumed.index("--session") + 1], session)


if __name__ == "__main__":
    unittest.main()
