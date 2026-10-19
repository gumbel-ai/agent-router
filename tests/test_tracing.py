"""Opt-in child tracing: recorded explicitly, never changing child launch or Router counters."""

import json
import shlex
import sys
import unittest
from pathlib import Path

from test_control import settings
from test_runtime import RuntimeHarness

from router.child import TRACING_UNAVAILABLE, child_environment, executor_command

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

    def test_requested_tracing_is_recorded_unavailable_and_keeps_native_counters(self):
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


class TracingLaunchTests(unittest.TestCase):
    def test_unavailable_opt_in_adds_no_arguments_settings_or_credentials_to_children(self):
        config = settings(Path("/tmp/project"), command=["fixture"])
        config.codex_command = ["codex"]
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept"}
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
                self.assertEqual(child_environment(environment, traced), child_environment(environment, plain))


if __name__ == "__main__":
    unittest.main()
