"""Opt-in tracing routes, credential isolation and unchanged native accounting."""

import json
import os
import shlex
import subprocess
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
    langfuse_credentials_available,
    router_otel_resource_attributes,
    strip_langfuse_environment,
)
from router.snapshot import apply_delta, capture_final, prepare

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
                if executor == "opencode":
                    self.control.environment.update({"LANGFUSE_PUBLIC_KEY": "pk-test",
                                                     "LANGFUSE_SECRET_KEY": "sk-test",
                                                     "LANGFUSE_BASE_URL": "https://langfuse.invalid"})
                traced = self.wait(self.start("traced", f"{executor}-traced", executor=executor),
                                   {"awaiting_review", "failed", "interrupted"})
                self.assertEqual(traced["status"], "awaiting_review", traced)
                if executor == "opencode":
                    expected = {"requested": True, "available": True,
                                "reason": "OpenCode plugin export and Router correlation verified via OpenTelemetry resource attributes",
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

    def test_requested_opencode_without_environment_credentials_is_unavailable(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", "LANGFUSE_BASEURL"):
            self.control.environment.pop(key, None)
        validated = self.runtime.validate({"task_id": "task-missing-creds", "invocation": {
            "executor": "opencode", "model": "test", "parent_repo": str(self.repo),
            "parent_worktree": str(self.repo),
        }})
        self.assertFalse(validated["tracing"]["available"])
        self.assertIn("credentials are unavailable", validated["tracing"]["reason"])

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
                "permission_profile": {"*": "allow"}, "tracing": {"requested": True, "available": True,
                                                                         "correlation": {"session_id": "ses-child",
                                                                                         "router_task_id": task_id,
                                                                                         "host_id": self.control.host.host_id}}}
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

    def test_direct_git_paths_never_receive_tracing_credentials(self):
        self.runtime.stop()
        credential_file = self.repo / ".env"
        credential_file.write_text("# synthetic fixture\n")
        self.control.environment["AGENT_ROUTER_ENV_FILE"] = str(credential_file)
        hook_records = self.root / "git-hook.jsonl"
        hook = self.repo / ".git/hooks/reference-transaction"
        code = ("import json, os\nfrom pathlib import Path\n"
                f"with Path({str(hook_records)!r}).open('a') as stream:\n"
                "    stream.write(json.dumps({'keys': sorted(key for key in os.environ if key.startswith('LANGFUSE_')), "
                "'option': os.environ.get('EXAMPLE_GIT_OPTION')}) + '\\n')\n")
        hook.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)}\n")
        hook.chmod(0o700)
        run = subprocess.run
        environments = []

        def record_run(command, *args, **kwargs):
            if command[0] == "git":
                environment = os.environ if kwargs.get("env") is None else kwargs["env"]
                environments.append((command[3], dict(environment)))
            return run(command, *args, **kwargs)

        environment = {"LANGFUSE_PUBLIC_KEY": "synthetic-public", "LANGFUSE_SECRET_KEY": "synthetic-secret",
                       "LANGFUSE_UNDOCUMENTED": "synthetic-extra", "EXAMPLE_GIT_OPTION": "preserved"}
        with patch.dict(os.environ, environment), patch("router.snapshot.subprocess.run", side_effect=record_run):
            task_id = "direct-git"
            invocation = {"executor": "opencode", "model": "test", "parent_repo": str(self.repo),
                          "parent_worktree": str(self.repo)}
            self.runtime.validate({"task_id": task_id, "invocation": invocation})
            info = prepare(self.repo, task_id, (self.root / task_id).resolve())
            child = Path(info["worktree"])
            tracked = child / "tracked.txt"
            tracked.write_text(tracked.read_text() + "example update\n")
            final = capture_final(self.repo, info, 1, self.root / task_id)
            apply_delta(self.repo, Path(final["patch"]))
            self.assertEqual((self.repo / "tracked.txt").read_text(), tracked.read_text())
            with self.control.store.transaction() as db:
                self.control.store.put(db, {**info, "session_id": "S", "invocation": invocation,
                                           "status": "closed", "cleanup_pending": True})
            before = len(hook_records.read_text().splitlines())
            self.runtime.clean(task_id)
            self.assertIsNone(self.control.store.get(task_id).get("cleanup_error"))
            self.assertFalse(child.exists())
            self.assertGreater(len(hook_records.read_text().splitlines()), before)
            self.assertIn("check-ignore", [command for command, _ in environments])
            for command, actual in environments:
                self.assertFalse(any(key.startswith("LANGFUSE_") for key in actual), command)
                self.assertEqual(actual["EXAMPLE_GIT_OPTION"], "preserved", command)
        records = [json.loads(line) for line in hook_records.read_text().splitlines()]
        self.assertTrue(records)
        self.assertTrue(all(record == {"keys": [], "option": "preserved"} for record in records))


class TracingLaunchTests(unittest.TestCase):
    def test_router_phase_environment_stripping_does_not_mutate_source(self):
        environment = {"PATH": "/usr/bin", "LANGFUSE_SECRET_KEY": "sk-secret", "OTHER": "kept"}
        self.assertEqual(strip_langfuse_environment(environment), {"PATH": "/usr/bin", "OTHER": "kept"})
        self.assertEqual(environment["LANGFUSE_SECRET_KEY"], "sk-secret")

    def test_tracing_off_strips_credentials_and_does_not_add_plugin(self):
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept",
                       "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret",
                       "LANGFUSE_USER_ID": "operator", "LANGFUSE_UNDOCUMENTED": "must-strip",
                       "OTEL_RESOURCE_ATTRIBUTES": "service.name=parent,router.task_id=stale"}
        task = {"executor": "opencode", "permission_profile": {"*": "allow"},
                "resolved_model": "provider/exact", "worktree": "/tmp/child"}
        child = child_environment(environment, task)
        self.assertEqual(json.loads(child["OPENCODE_CONFIG_CONTENT"]), {"permission": {"*": "allow"}})
        self.assertEqual(child["EXAMPLE_OPTION"], "kept")
        self.assertTrue(all(key not in child for key in (*LANGFUSE_ENVIRONMENT_KEYS, "LANGFUSE_UNDOCUMENTED")))
        self.assertNotIn("OTEL_RESOURCE_ATTRIBUTES", child)

    def test_requested_opencode_adds_pinned_plugin_preserves_permission_and_passes_credentials(self):
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept",
                       "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret",
                       "LANGFUSE_BASEURL": "https://langfuse.invalid", "LANGFUSE_USER_ID": "operator",
                       "LANGFUSE_UNDOCUMENTED": "must-strip"}
        permission = {"*": "allow", "external_directory": "deny"}
        task = {"executor": "opencode", "permission_profile": permission,
                "resolved_model": "provider/exact", "worktree": "/tmp/child",
                "tracing": {"requested": True, "available": True,
                             "reason": "OpenCode plugin export and Router correlation verified via OpenTelemetry resource attributes",
                             "correlation": {"session_id": "ses-child", "router_task_id": "task-123",
                                             "host_id": "host-456"}}}
        child = child_environment(environment, task)
        self.assertEqual(json.loads(child["OPENCODE_CONFIG_CONTENT"]),
                         {"permission": permission, "plugins": [OPENCODE_LANGFUSE_PLUGIN]})
        for key in LANGFUSE_ENVIRONMENT_KEYS:
            if key in environment:
                self.assertEqual(child[key], environment[key])
        self.assertNotIn("LANGFUSE_UNDOCUMENTED", child)
        self.assertEqual(child["EXAMPLE_OPTION"], "kept")
        self.assertEqual(child["OTEL_RESOURCE_ATTRIBUTES"],
                         "router.task_id=task-123,router.host_id=host-456,router.child_session_id=ses-child")

    def test_router_otel_resource_attributes_fail_closed_for_invalid_correlation(self):
        self.assertEqual(router_otel_resource_attributes({}), None)
        self.assertEqual(router_otel_resource_attributes({"tracing": {"requested": True}}), None)
        self.assertEqual(router_otel_resource_attributes({
            "tracing": {"requested": True, "correlation": {
                "router_task_id": "task,not-safe", "host_id": "host-456", "session_id": "ses-child"
            }}
        }), None)

        environment = {"PATH": "/usr/bin", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}
        task = {"executor": "opencode", "permission_profile": {"*": "allow"},
                "resolved_model": "provider/exact", "worktree": "/tmp/child",
                "tracing": {"requested": True, "correlation": {"router_task_id": "bad,task",
                                                                     "host_id": "host-456",
                                                                     "session_id": "ses-child"}}}
        with self.assertRaisesRegex(ValueError, "correlation metadata"):
            child_environment(environment, task)

    def test_langfuse_credentials_require_public_and_secret_keys(self):
        self.assertFalse(langfuse_credentials_available({}))
        self.assertTrue(langfuse_credentials_available({"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}))

    def test_setup_environment_never_receives_tracing_plugin_or_credentials(self):
        environment = {"PATH": "/usr/bin", "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret",
                       "OTEL_RESOURCE_ATTRIBUTES": "router.task_id=stale"}
        task = {"executor": "opencode", "permission_profile": {"*": "allow"},
                "resolved_model": "provider/exact", "worktree": "/tmp/child",
                "tracing": {"requested": True, "available": False}}
        setup = child_environment(environment, task, include_tracing_credentials=False)
        self.assertEqual(json.loads(setup["OPENCODE_CONFIG_CONTENT"]), {"permission": {"*": "allow"}})
        self.assertNotIn("LANGFUSE_PUBLIC_KEY", setup)
        self.assertNotIn("LANGFUSE_SECRET_KEY", setup)
        self.assertNotIn("OTEL_RESOURCE_ATTRIBUTES", setup)

    def test_non_opencode_children_never_receive_langfuse_credentials(self):
        config = settings(Path("/tmp/project"), command=["fixture"])
        config.codex_command = ["codex"]
        environment = {"PATH": "/usr/bin", "EXAMPLE_OPTION": "kept",
                       "LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret",
                       "OTEL_RESOURCE_ATTRIBUTES": "router.task_id=parent"}
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
                self.assertNotIn("OTEL_RESOURCE_ATTRIBUTES", child)

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
