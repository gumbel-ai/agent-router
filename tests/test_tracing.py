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
    launch,
    router_otel_resource_attributes,
    strip_langfuse_environment,
)
from router.daemon import Runtime
from router.snapshot import apply_delta, capture_final, prepare

CLAUDE_MODEL = "anthropic/claude-sonnet-4-6"
CLAUDE_PROFILE = {"tools": ["Read", "Edit", "Write"], "allow": ["Read", "Edit", "Write"]}
TEST_CREDENTIALS = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test",
                    "LANGFUSE_BASE_URL": "https://langfuse.invalid"}
AVAILABLE_REASON = "OpenCode plugin export and Router correlation verified via OpenTelemetry resource attributes"
UNAVAILABLE_REASON = "OpenCode route is verified, but required Langfuse credentials are unavailable"


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

    def set_credentials(self, present):
        for key in LANGFUSE_ENVIRONMENT_KEYS:
            self.control.environment.pop(key, None)
        if present:
            self.control.environment.update(TEST_CREDENTIALS)

    def expected_tracing(self, task, available):
        return {"requested": True, "available": available,
                "reason": AVAILABLE_REASON if available else UNAVAILABLE_REASON,
                "plugin": OPENCODE_LANGFUSE_PLUGIN,
                "correlation": {"session_id": task["child_session_id"], "router_task_id": task["task_id"],
                                "host_id": self.control.host.host_id}}

    def record_child_credentials(self, launched):
        def recording_launch(command, cwd, environment, directory, register, *, disposable=False):
            if directory.name == "child":
                launched.append(langfuse_credentials_available(environment))
            return launch(command, cwd, environment, directory, register, disposable=disposable)
        return patch("router.daemon.launch", side_effect=recording_launch)

    def reject(self, task_id, request_id):
        self.mutate("review", {"task_id": task_id, "request_id": request_id, "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Correct the result"})
        return self.wait(task_id, {"awaiting_review", "failed", "interrupted"})

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

    def test_remote_start_uses_serving_repository_tracing_selection(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        self.set_credentials(True)
        task = {"task_id": "task-remote-traced", "parent_tracing_requested": True, "invocation": {
            "executor": "opencode", "model": "test", "parent_repo": str(self.repo), "parent_worktree": str(self.repo)}}
        validated = self.runtime.validate(task)
        self.assertEqual(validated["tracing"], self.expected_tracing({**task, **validated}, True))

    def test_remote_start_without_serving_selection_is_explicitly_unavailable(self):
        for executor in ("opencode", "codex", "claude"):
            with self.subTest(executor=executor):
                config = self.configure(executor)
                config.pop("tracing", None)
                self.set_credentials(True)
                task = {"task_id": f"task-remote-{executor}", "parent_tracing_requested": True, "invocation": {
                    "executor": executor, "model": "test", "parent_repo": str(self.repo),
                    "parent_worktree": str(self.repo)}}
                validated = self.runtime.validate(task)
                tracing = validated["tracing"]
                self.assertEqual(tracing["requested"], True)
                self.assertIs(tracing["available"], False)
                self.assertIn("no tracing selection", tracing["reason"])
                if executor == "opencode":
                    environment = child_environment({**TEST_CREDENTIALS, "PATH": os.defpath},
                                                    {**task, **validated, "worktree": str(self.repo)})
                    self.assertFalse(langfuse_credentials_available(environment))
                    self.assertNotIn("plugins", json.loads(environment["OPENCODE_CONFIG_CONTENT"]))

    def test_remote_unselected_record_stays_unavailable_after_serving_repo_enables_tracing(self):
        config = self.configure("opencode")
        config.pop("tracing", None)
        self.set_credentials(True)
        task_id = "remote-unselected"
        task = {"task_id": task_id, "parent_tracing_requested": True, "invocation": {
            "executor": "opencode", "model": "test", "parent_repo": str(self.repo), "parent_worktree": str(self.repo)}}
        validated = self.runtime.validate(task)
        task.update(validated, session_id="S", executor="opencode", status="running", attempts=1, attempt_number=2,
                    artifact_dir=str(self.root / "artifacts"), deadline_at=9_999_999_999_999,
                    worktree=str(self.root / "child"))
        with self.control.store.transaction() as db:
            self.control.store.put(db, task)
        config["tracing"] = "requested"
        environments = []

        def fake_launch(command, cwd, environment, directory, register, *, disposable=False):
            environments.append(environment)
            return SimpleNamespace(poll=lambda: 0), {}

        with patch("router.daemon.launch", side_effect=fake_launch), \
                patch.object(self.runtime, "observe", return_value={"exit_code": 0}):
            self.runtime.phase(task_id, "child", ["child"], self.root)
        tracing = self.control.store.get(task_id)["tracing"]
        self.assertIs(tracing["available"], False)
        self.assertIn("no tracing selection", tracing["reason"])
        self.assertNotIn("LANGFUSE_SECRET_KEY", environments[0])
        self.assertNotIn("plugins", json.loads(environments[0]["OPENCODE_CONFIG_CONTENT"]))

    def test_reloaded_repository_opt_out_disables_stale_tracing(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        task = {"invocation": {"parent_worktree": str(self.repo)}}
        self.assertTrue(self.runtime._tracing_requested(task))
        config.pop("tracing")
        self.assertFalse(self.runtime._tracing_requested(task))

    def test_resumed_attempt_refreshes_availability_and_keeps_earlier_attempt(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        for before, after in ((True, False), (False, True)):
            with self.subTest(before=before, after=after):
                launched = []
                self.set_credentials(before)
                with self.record_child_credentials(launched):
                    task_id = self.start("traced", f"resume-{before}-{after}")
                    first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
                    self.assertEqual(first["status"], "awaiting_review", first)
                    # Models a daemon restart with a changed credential environment.
                    self.set_credentials(after)
                    second = self.reject(task_id, f"reject-{before}-{after}")
                self.assertEqual(second["status"], "awaiting_review", second)
                self.assertEqual(second["attempts"], 2)
                self.assertEqual(launched, [before, after])
                self.assertEqual(second["child_session_id"], first["child_session_id"])
                self.assertEqual(first["tracing"], self.expected_tracing(first, before))
                self.assertEqual(second["tracing"], self.expected_tracing(first, after))
                self.assertEqual([attempt["tracing"] for attempt in second["attempt_history"]],
                                 [self.expected_tracing(first, before), self.expected_tracing(first, after)])

    def test_opt_out_during_capture_keeps_launch_evidence(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        self.set_credentials(True)
        snapshot_phase = self.runtime.snapshot_phase

        def opt_out_before_capture(task_id, action):
            if action == "capture":
                config.pop("tracing", None)
            return snapshot_phase(task_id, action)

        launched = []
        with patch.object(self.runtime, "snapshot_phase", side_effect=opt_out_before_capture), \
                self.record_child_credentials(launched):
            task_id = self.start("traced", "capture-opt-out")
            first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
            self.assertEqual(first["status"], "awaiting_review", first)
            self.assertNotIn("tracing", config)
            second = self.reject(task_id, "reject-after-opt-out")
        expected = self.expected_tracing(first, True)
        self.assertEqual(first["tracing"], expected)
        self.assertEqual(first["attempt_history"][0]["tracing"], expected)
        # The opted-out resumed launch fails closed; the completed launch keeps its evidence.
        self.assertEqual(second["status"], "awaiting_review", second)
        self.assertEqual(launched, [True, False])
        self.assertNotIn("tracing", second)
        self.assertEqual(second["attempt_history"][0]["tracing"], expected)
        self.assertNotIn("tracing", second["attempt_history"][1])

    def test_recovered_child_capture_keeps_original_launch_evidence(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        self.set_credentials(True)
        snapshot_phase = self.runtime.snapshot_phase
        before_capture = {}

        def record_before_capture(task_id, action):
            if action == "capture":
                before_capture.update(self.control.store.get(task_id))
            return snapshot_phase(task_id, action)

        with patch.object(self.runtime, "snapshot_phase", side_effect=record_before_capture):
            task_id = self.start("traced", "recovered-capture")
            first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(first["status"], "awaiting_review", first)
        self.assertEqual(before_capture["phase"], "child")
        expected = self.expected_tracing(first, True)
        self.runtime.stop()
        # Persisted state of a daemon that stopped after the child exited and before capture.
        with self.control.store.transaction() as db:
            self.control.store.put(db, {**before_capture, "execution_stopped": False})
        config.pop("tracing")
        self.set_credentials(False)
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        recovered = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(recovered["status"], "awaiting_review", recovered)
        self.assertEqual(recovered["tracing"], expected)
        self.assertEqual(recovered["attempt_history"][0]["tracing"], expected)

    def test_runtime_phase_only_restores_credentials_for_the_executor_child(self):
        config = self.configure("opencode")
        config["tracing"] = "requested"
        self.control.environment.update({"LANGFUSE_PUBLIC_KEY": "pk-secret", "LANGFUSE_SECRET_KEY": "sk-secret"})
        task_id = "phase-tracing"
        task = {"task_id": task_id, "session_id": "S", "executor": "opencode", "status": "running",
                "attempts": 0, "attempt_number": 1, "artifact_dir": str(self.root / "artifacts"), "deadline_at": 9_999_999_999_999,
                "invocation": {"parent_worktree": str(self.repo)}, "worktree": str(self.root / "child"),
                "permission_profile": {"*": "allow"}, "tracing": {"requested": True, "available": True,
                                                                         "plugin": OPENCODE_LANGFUSE_PLUGIN,
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
                "tracing": {"requested": True, "available": True, "plugin": OPENCODE_LANGFUSE_PLUGIN,
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
                "tracing": {"requested": True, "plugin": OPENCODE_LANGFUSE_PLUGIN,
                            "correlation": {"router_task_id": "bad,task",
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
