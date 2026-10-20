"""Claude child contracts using local structured-output fixtures only."""

import copy
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_runtime import RuntimeHarness

from router import service
from router.child import child_environment, executor_command
from router.claude_executor import collect
from router.daemon import Controller, PhaseStopped, Runtime, public, visibility
from router.db import Store
from router.process import creation_identity, owned_state, stop_owned
from router.validation import MAX_WIRE_INTEGER

MODEL = "anthropic/claude-sonnet-4-6"
PROFILE = {"tools": ["Read", "Edit", "Write", "Bash"], "allow": ["Read", "Edit", "Write", "Bash"]}


class ClaudeExecutorTests(RuntimeHarness, unittest.TestCase):
    def enable(self):
        wrapper = self.root / "claude-fixture"
        fixture = Path(__file__).with_name("fixture_executor.py")
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fixture))} "$@"\n')
        wrapper.chmod(0o700)
        self.config.claude_command = [str(wrapper)]
        self.config.models["test"] = MODEL
        self.config.repositories[str(self.repo.resolve())]["claude_permissions"] = json.dumps(PROFILE)

    def test_declared_final_response_requires_independent_acceptance_after_repair(self):
        self.enable()
        invocation = {"request_version": "1", "request_id": "claude-contract", "instruction_id": self.instruction,
                      "executor": "claude", "model": "test", "task": "empty final", "mode": "write",
                      "parent_repo": str(self.repo), "parent_worktree": str(self.repo),
                      "completion": {"files": ["result.txt"], "final_response": True}}
        task_id = self.mutate("start", invocation)["task_id"]
        first = self.wait(task_id, {"awaiting_review", "failed"})
        self.assertEqual(first["completion_check"]["status"], "violated", first)
        with self.assertRaisesRegex(ValueError, "completion requirements"):
            self.mutate("review", {"task_id": task_id, "request_id": "claude-no-accept", "expected_attempt": 1, "outcome": "accepted"})
        self.mutate("review", {"task_id": task_id, "request_id": "claude-repair", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Provide a final response"})
        second = self.wait(task_id, {"awaiting_review", "failed"})
        self.assertEqual(second["completion_check"]["status"], "satisfied", second)
        self.assertEqual(second["status"], "awaiting_review")

    def fixture_marker(self, path):
        deadline = time.monotonic() + 15
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.04)
        self.assertTrue(path.exists(), f"Fixture did not publish {path.name}")

    def fixture_pid(self, path, ownership):
        self.fixture_marker(path)
        pid = json.loads(path.read_text())
        self.assertIs(type(pid), int)
        self.assertGreater(pid, 0)
        ownership["pid"] = pid
        ownership["identity"] = creation_identity(pid)
        self.assertEqual(os.getpgid(pid), ownership.get("pgid", pid))
        return pid, ownership["identity"]

    @contextmanager
    def fixture_cleanup(self, directory=None):
        ownership = {}
        completed = cleaned = False
        gate = self.control.environment.get("ROUTER_FIXTURE_PID_GATE")
        initialization_gate = self.control.environment.get("ROUTER_FIXTURE_INITIALIZATION_GATE")

        def cleanup():
            nonlocal cleaned
            if cleaned:
                return
            task = self.control.store.get(ownership["task_id"]) if "task_id" in ownership else {}
            worktree = Path(task["worktree"]) if task.get("worktree") else None
            pid_file = directory / "helper.pid" if directory is not None else worktree / "sleeper.pid" if worktree else None
            record = task.get("process")
            with self.runtime.lock:
                process = self.runtime.processes.get(task.get("task_id"))
            try:
                if gate and pid_file and (Path(gate) / (pid_file.name + ".pending")).exists():
                    (Path(gate) / "release").touch()
                    self.fixture_marker(Path(gate) / (pid_file.name + ".published"))
                if pid_file and pid_file.exists() and "identity" not in ownership:
                    if directory is not None and record:
                        ownership["pgid"] = record["pgid"]
                    try:
                        self.fixture_pid(pid_file, ownership)
                    except (ProcessLookupError, FileNotFoundError):
                        pass
            finally:
                # Persistent release also covers failures before PID/identity acquisition.
                if initialization_gate:
                    (Path(initialization_gate) / "release").touch()
                if directory is not None:
                    (directory / "helper.stop").touch()
                if worktree is not None and directory is None:
                    (worktree / "foreground.stop").touch()
                if not completed:
                    self.runtime.stop()
                if record and owned_state(record) == "live":
                    stop_owned(record, process if process is not None and process.pid == record["pid"] else None)
                if process is not None:
                    process.wait(timeout=5)
            if "identity" in ownership:
                pid = ownership["pid"]
                try:
                    if creation_identity(pid) == ownership["identity"]:
                        if directory is None:
                            self.assertEqual(os.getpgid(pid), pid)
                            os.killpg(pid, signal.SIGTERM)
                        else:
                            os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, FileNotFoundError):
                    pass
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.04)
                else:
                    self.fail("Owned fixture did not stop after inspection cleanup")
            if record:
                deadline = time.monotonic() + 15
                while owned_state(record) != "stopped" and time.monotonic() < deadline:
                    time.sleep(.04)
                self.assertEqual(owned_state(record), "stopped", "Owned fixture group survived cleanup")
            cleaned = True

        self.addCleanup(cleanup)  # Rescue an isolated cleanup reversal after its failure assertion.
        try:
            yield ownership
            completed = True
        finally:
            cleanup()

    def test_fresh_and_resumed_child_preserve_model_worktree_and_accounting(self):
        self.enable()
        self.control.environment["CLAUDE_CODE_CHILD_SESSION"] = "1"
        (self.repo / "tracked.txt").write_text("parent dirty\n")
        task_id = self.start("finished", executor="claude")
        first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(first["status"], "awaiting_review", first)
        self.assertEqual(first["resolved_model"], MODEL)
        self.assertEqual(first["cost"], .25)
        self.assertEqual(first["usage"]["input_tokens"], 100)
        self.assertTrue(first["usage_complete"])
        self.assertFalse((self.repo / "result.txt").exists())
        self.assertEqual((self.repo / "tracked.txt").read_text(), "parent dirty\n")
        self.mutate("review", {"task_id": task_id, "request_id": "correct", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Correct the result"})
        second = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(second["status"], "awaiting_review", second)
        self.assertEqual(second["worktree"], first["worktree"])
        self.assertEqual(second["child_session_id"], first["child_session_id"])
        self.assertEqual(second["cost"], .5)
        self.assertEqual(second["usage"]["input_tokens"], 200)
        records = json.loads((Path(second["worktree"]) / "session.json").read_text())
        self.assertEqual(records["models"], [MODEL.removeprefix("anthropic/")] * 2)
        self.assertIn("--session-id", records["commands"][0])
        self.assertIn("--resume", records["commands"][1])
        self.assertIn("--safe-mode", records["commands"][0])
        for command in records["commands"]:
            cli_settings = json.loads(command[command.index("--settings") + 1])
            self.assertEqual(cli_settings["fallbackModel"], [])
            self.assertIs(cli_settings["switchModelsOnFlag"], False)
            self.assertEqual(cli_settings["permissions"]["allow"], PROFILE["allow"])
            self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(command[command.index("--tools") + 1], ",".join(PROFILE["tools"]))
        with self.assertRaises(ValueError):
            self.mutate("review", {"task_id": task_id, "request_id": "over-ceiling", "expected_attempt": 2,
                                   "outcome": "rejected", "feedback": "Again"})

    def test_invalid_configuration_refuses_before_launch(self):
        self.enable()
        for index, (model, profile) in enumerate((("anthropic/sonnet", PROFILE), ("openai/gpt-6-sol", PROFILE),
                                                (MODEL, None), (MODEL, {"tools": ["Agent"], "allow": ["Agent"]}))):
            with self.subTest(model=model, profile=profile):
                self.config.models["test"] = model
                config = self.config.repositories[str(self.repo.resolve())]
                if profile is None:
                    config.pop("claude_permissions", None)
                else:
                    config["claude_permissions"] = json.dumps(profile)
                task = self.wait(self.start("must not launch", f"invalid-{index}", executor="claude"), {"failed"})
                self.assertEqual(task["attempts"], 0)
                self.assertNotIn("worktree", task)

        self.config.models["test"] = MODEL
        config["claude_permissions"] = json.dumps(PROFILE)
        command = self.config.claude_command.copy()
        for index, policy in enumerate(("environment", "option", "separator", "required-option", "interpreter-prefix")):
            with self.subTest(policy=policy):
                if policy == "environment":
                    self.control.environment["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "0"
                else:
                    extra = {"option": "--no-session-persistence", "separator": "--",
                             "required-option": "--append-system-prompt", "interpreter-prefix": "fixture_executor.py"}[policy]
                    self.config.claude_command = [*command, extra]
                task = self.wait(self.start("never launched", f"policy-{index}", executor="claude"), {"failed"})
                self.assertEqual(task["attempts"], 0)
                self.assertNotIn("process", task)
                self.assertIn("persistence" if policy == "environment" else "one executable", task["failure_reason"])
                self.control.environment.pop("CLAUDE_CODE_SKIP_PROMPT_HISTORY", None)
                self.config.claude_command = command
        for index, version in enumerate(("2.1.276", "invalid", "x" * 4097, "nonascii-é")):
            with self.subTest(version=version):
                self.control.environment["ROUTER_FIXTURE_CLAUDE_VERSION"] = version
                task = self.wait(self.start("never launched", f"version-{index}", executor="claude"), {"failed"})
                self.assertEqual(task["attempts"], 0)
                self.assertNotIn("worktree", task)
                self.assertIn("Claude", task["failure_reason"])

    def test_missing_wrong_and_error_evidence_never_succeeds(self):
        self.enable()
        for prompt in ("no completion", "wrong model", "wrong usage model", "wrong session", "final error"):
            with self.subTest(prompt=prompt):
                task = self.wait(self.start(prompt, prompt, executor="claude"), {"failed", "awaiting_review", "interrupted"})
                self.assertEqual(task["status"], "failed", task)
                self.assertTrue(task["execution_stopped"])
                self.assertEqual(task["attempts"], 1)

    def test_timeout_keeps_the_owned_worktree_without_relaunch(self):
        self.enable()
        gate = self.root / "initialization"
        gate.mkdir()
        self.control.environment["ROUTER_FIXTURE_INITIALIZATION_GATE"] = str(gate)
        with self.fixture_cleanup() as ownership:
            task_id = ownership["task_id"] = self.start("silent hang", executor="claude")
            running = self.wait(task_id, {"running"})
            self.fixture_marker(gate / "pending")
            self.assertTrue((Path(running["worktree"]) / "result.txt").exists())
            ready_pid = self.fixture_pid
            def release_initialization(path, owner):
                (gate / "release").touch()
                return ready_pid(path, owner)
            with patch.object(self, "fixture_pid", side_effect=release_initialization):
                pid, _ = self.fixture_pid(Path(running["worktree"]) / "sleeper.pid", ownership)
            # PID publication follows handler installation and foreground group creation.
            with patch("router.daemon.now_ms", return_value=running["deadline_at"] + 1):
                task = self.wait(task_id, {"failed", "interrupted"})
            self.assertEqual(task["status"], "failed", task)
            self.assertEqual(task["failure_reason"], "router-timeout")
            self.assertTrue(task["execution_stopped"])
            self.assertEqual(task["attempts"], 1)
            self.assertEqual((Path(task["worktree"]) / "result.txt").read_text(), "first\n")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_phase_deadlines_are_enforced_before_gate_release(self):
        base = int(time.time() * 1000) - 20_000
        for phase in ("version", "setup"):
            for span in (3000, 10_000):
                deadline = base + (min(5000, span) if phase == "version" else span)
                for edge in (-1, 0, 1):
                    with self.subTest(phase=phase, task_span=span, edge=edge):
                        task_id = f"admission-{phase}-{span}-{edge}"
                        task = {"task_id": task_id, "session_id": "S", "status": "validating", "executor": "claude",
                                "attempt_number": 1, "attempts": 0, "deadline_at": base + span,
                                "artifact_dir": str(self.root / task_id), "execution_stopped": True}
                        with self.control.store.transaction() as db:
                            self.control.store.put(db, task)
                        clock = [base]
                        released = []
                        def delayed_registration(command, cwd, environment, directory, register, *, disposable=False,
                                                 clock=clock, released=released, at=deadline + edge):
                            record = {}
                            clock[0] = at  # Registration became writable after this elapsed delay.
                            register(record)
                            released.append(True)
                            return SimpleNamespace(poll=lambda: 0), record
                        with patch("router.daemon.now_ms", side_effect=lambda clock=clock: clock[0]), \
                                patch("router.daemon.launch", side_effect=delayed_registration), \
                                patch.object(self.runtime, "observe", return_value={"exit_code": 0}):
                            if edge < 0:
                                self.runtime.phase(task_id, phase, ["fixture-command"], self.repo)
                            else:
                                with self.assertRaises(PhaseStopped) as stopped:
                                    self.runtime.phase(task_id, phase, ["fixture-command"], self.repo)
                                self.assertEqual(stopped.exception.reason, "router-timeout")
                        persisted = self.control.store.get(task_id)
                        self.assertEqual(bool(released), edge < 0)
                        self.assertEqual(persisted["execution_stopped"], edge >= 0)
                        self.assertEqual("process" in persisted, edge < 0)

    def test_version_wrappers_keep_cleanup_and_recovery_within_owned_phases(self):
        self.enable()
        probes = {}
        def finishing_group(record):
            state = owned_state(record)
            if state == "stopped" and record.get("disposable"):
                # Model the kernel reporting another killed member while the leader is reaped.
                probes[record["pgid"]] = probes.get(record["pgid"], 0) + 1
                if probes[record["pgid"]] <= 2:
                    return "uncertain"
            return state
        probe = patch("router.process.owned_state", side_effect=finishing_group)
        probe.start()
        self.addCleanup(probe.stop)
        outcomes = ("invalid", "success", "deadline", "cancel", "restart", "read-failure", "identity-failure", "group-failure")
        for index, outcome in enumerate(outcomes):
            with self.subTest(outcome=outcome):
                directory = self.root / f"version-{index}"
                directory.mkdir()
                self.control.environment.update(ROUTER_FIXTURE_VERSION_DIRECTORY=str(directory),
                                                ROUTER_FIXTURE_VERSION_MODE="wait",
                                                ROUTER_FIXTURE_CLAUDE_VERSION="invalid" if outcome == "invalid" else "2.1.303")
                expected = {"read-failure": OSError("injected PID read failure"),
                            "identity-failure": PermissionError("injected identity failure"),
                            "group-failure": AssertionError("injected group failure")}
                fault = outcome in expected
                sentinel = expected[outcome] if fault else None
                if fault:
                    gate = directory / "publication"
                    gate.mkdir()
                    self.control.environment["ROUTER_FIXTURE_PID_GATE"] = str(gate)
                reached = False
                with (self.assertRaises(type(sentinel)) if fault else nullcontext()) as caught, self.fixture_cleanup(directory) as ownership:
                    if outcome in {"restart", "read-failure", "identity-failure", "group-failure"}:
                        with patch.object(self.runtime, "observe", side_effect=SystemExit("synthetic daemon loss")):
                            task_id = ownership["task_id"] = self.start("must not launch", f"wrapper-{index}", executor="claude")
                            deadline = time.monotonic() + 15
                            while time.monotonic() < deadline:
                                task = self.control.store.get(task_id)
                                if task.get("phase") == "version" and task_id not in self.runtime.workers:
                                    break
                                time.sleep(.04)
                    else:
                        task_id = ownership["task_id"] = self.start("must not launch", f"wrapper-{index}", executor="claude")
                    if fault:
                        self.fixture_marker(gate / "helper.pid.pending")
                        self.assertFalse((directory / "helper.pid").exists(), "PID became visible before publication completed")
                        (gate / "release").touch()
                        self.fixture_marker(gate / "helper.pid.published")
                    self.fixture_marker(directory / "helper.pid")
                    task = self.control.store.get(task_id)
                    ownership["pgid"] = task["process"]["pgid"]
                    failure = (patch.object(self, "fixture_pid", side_effect=sentinel)
                               if outcome == "read-failure" else
                               patch("test_claude_executor.creation_identity", side_effect=sentinel)
                               if outcome == "identity-failure" else
                               patch("test_claude_executor.os", SimpleNamespace(getpgid=Mock(side_effect=sentinel)))
                               if outcome == "group-failure" else nullcontext())
                    reached = True
                    with failure:
                        pid, _ = self.fixture_pid(directory / "helper.pid", ownership)
                    if fault:
                        self.fail("Injected fixture acquisition failure was not raised")
                    self.assertEqual(task["phase"], "version", task)
                    self.assertFalse(task["execution_stopped"])
                    self.assertEqual(task["attempts"], 0)
                    self.assertNotEqual(task["process"]["pgid"], os.getpgrp())
                    if outcome in {"invalid", "success"}:
                        (directory / "release").touch()
                    elif outcome == "deadline":
                        with patch("router.daemon.now_ms", return_value=task["process"]["deadline_at"] + 1):
                            task = self.wait(task_id, {"failed", "interrupted"})
                    elif outcome == "cancel":
                        self.mutate("cancel", {"task_id": task_id, "request_id": f"cancel-wrapper-{index}"})
                    else:
                        # The first worker vanished after durable registration; recovery owns the same group.
                        with self.runtime.lock:
                            process = self.runtime.processes[task_id]
                        self.runtime.stopping.set()
                        self.control.stop_lifecycle_writer()
                        self.control.capacity.stop()
                        self.control.changed.set()
                        self.runtime.scheduler.join(timeout=5)
                        self.assertFalse(self.runtime.scheduler.is_alive())
                        # A real dead daemon cannot leave an unreaped direct child in this test process.
                        reaper = threading.Thread(target=process.wait, daemon=True)
                        reaper.start()
                        self.runtime = Runtime(self.control)
                        self.runtime.start()
                        self.addCleanup(self.runtime.stop)
                        (directory / "release").touch()
                    task = self.wait(task_id, {"failed", "cancelled", "interrupted", "awaiting_review"})
                    if outcome in {"success", "restart"}:
                        self.assertEqual(task["status"], "awaiting_review", task)
                        self.assertEqual(task["attempts"], 1)
                    else:
                        self.assertEqual(task["attempts"], 0)
                        self.assertNotIn("worktree", task)
                    if outcome == "restart":
                        reaper.join(timeout=5)
                        self.assertFalse(reaper.is_alive())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                    self.assertTrue(task["execution_stopped"], task)
                    self.assertEqual((directory / "launches").read_text().splitlines(), ["version"])
                if fault:
                    self.assertIs(caught.exception, sentinel)
                    self.assertTrue(reached, "Startup failed before the injected acquisition boundary")
                    self.assertIn("identity", ownership, "Cleanup did not recover the fixture's creation identity")
                    self.assertEqual(owned_state(task["process"]), "stopped")
                    self.runtime = Runtime(self.control)
                    self.runtime.start()
                    self.addCleanup(self.runtime.stop)
                self.control.environment.pop("ROUTER_FIXTURE_VERSION_DIRECTORY")
                self.control.environment.pop("ROUTER_FIXTURE_PID_GATE", None)

    def test_unconfirmed_foreground_shutdown_reserves_capacity_across_restart(self):
        self.enable()
        self.config.concurrency_cap = 1
        cases = [("crash hang", None), ("ignore interrupt hang", None)]
        cases += [("crash hang", fault) for fault in ("read", "identity", "group")]
        for index, (prompt, fault) in enumerate(cases):
            with self.subTest(prompt=prompt, fault=fault):
                if fault:
                    gate = self.root / f"foreground-publication-{index}"
                    gate.mkdir()
                    self.control.environment["ROUTER_FIXTURE_PID_GATE"] = str(gate)
                expected = {"read": OSError("injected PID read failure"),
                            "identity": PermissionError("injected identity failure"),
                            "group": AssertionError("injected group failure")}
                sentinel = expected[fault] if fault else None
                reached = False
                with (self.assertRaises(type(sentinel)) if fault else nullcontext()) as caught, self.fixture_cleanup() as ownership:
                    task_id = ownership["task_id"] = self.start(prompt, f"foreground-{index}", executor="claude")
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        task = self.control.store.get(task_id)
                        pid_file = Path(task["worktree"]) / "sleeper.pid" if task.get("worktree") else None
                        if pid_file and ((gate / "sleeper.pid.pending").exists() if fault else pid_file.exists()):
                            break
                        time.sleep(.04)
                    self.assertTrue(pid_file and task.get("worktree"), task)
                    if fault:
                        self.fixture_marker(gate / "sleeper.pid.pending")
                        self.assertFalse(pid_file.exists(), "PID became visible before publication completed")
                        (gate / "release").touch()
                        self.fixture_marker(gate / "sleeper.pid.published")
                    if fault:
                        task = self.wait(task_id, {"interrupted"})
                    failure = (patch.object(self, "fixture_pid", side_effect=sentinel)
                               if fault == "read" else
                               patch("test_claude_executor.creation_identity", side_effect=sentinel)
                               if fault == "identity" else
                               patch("test_claude_executor.os", SimpleNamespace(getpgid=Mock(side_effect=sentinel)))
                               if fault == "group" else nullcontext())
                    reached = True
                    with failure:
                        pid, identity = self.fixture_pid(pid_file, ownership)
                    if fault:
                        self.fail("Injected fixture acquisition failure was not raised")
                    if "ignore interrupt" in prompt:
                        with patch("router.daemon.now_ms", return_value=task["deadline_at"] + 1):
                            task = self.wait(task_id, {"failed", "interrupted"})
                    else:
                        task = self.wait(task_id, {"failed", "interrupted"})
                    self.assertEqual(task["status"], "interrupted", task)
                    self.assertFalse(task["execution_stopped"])
                    self.assertTrue(task["shutdown_unconfirmed"])
                    self.assertIn("Claude", task["failure_reason"])
                    self.assertEqual(task["attempts"], 1)
                    self.assertEqual(creation_identity(pid), identity)
                    with self.assertRaisesRegex(ValueError, "uncertain"):
                        self.mutate("close", {"task_id": task_id, "request_id": f"unsafe-{index}",
                                              "disposition": "discarded"})
                    self.runtime.stop()
                    queued = self.start("queued", f"queued-{index}", executor="claude")
                    self.runtime = Runtime(self.control)
                    scheduled = threading.Event()
                    original_wait = self.control.changed.wait
                    def observed_wait(timeout=None, scheduled=scheduled, original_wait=original_wait):
                        scheduled.set()  # A complete admission pass has examined the queued task.
                        return original_wait(timeout)
                    with patch.object(self.control.changed, "wait", side_effect=observed_wait):
                        self.runtime.start()
                        self.addCleanup(self.runtime.stop)
                        self.assertTrue(scheduled.wait(15))
                        self.assertEqual(self.control.store.get(queued)["status"], "created")
                        self.assertEqual(self.control.store.get(queued)["attempts"], 0)
                        self.runtime.stop()
                    self.assertFalse(self.control.store.get(task_id)["execution_stopped"])
                if fault:
                    self.assertIs(caught.exception, sentinel)
                    self.assertTrue(reached, "Startup failed before the injected acquisition boundary")
                    self.assertIn("identity", ownership, "Cleanup did not recover the fixture's creation identity")
                    pid = ownership["pid"]
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                    self.control.environment.pop("ROUTER_FIXTURE_PID_GATE")
                self.runtime.stop()
                reason = "Inspected and stopped the separate foreground command"
                with patch("router.service.state_dir", return_value=self.root / "state"), \
                        patch("router.service.sys.stdin.isatty", return_value=True), \
                        patch("builtins.input", return_value=task_id):
                    recovered = service.recover(task_id, reason)
                self.assertTrue(recovered["execution_stopped"])
                self.assertTrue(recovered["shutdown_unconfirmed"])
                self.assertEqual(recovered["operator_recovery"]["reason"], reason)
                self.runtime = Runtime(self.control)
                self.runtime.start()
                self.addCleanup(self.runtime.stop)

    def test_environment_does_not_forward_parent_runtime_overrides(self):
        env = {"PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "ANTHROPIC_MODEL": "sonnet",
               "ANTHROPIC_BASE_URL": "https://example.invalid", "CLAUDE_CODE_USE_BEDROCK": "1",
               "ANTHROPIC_API_KEY": "fixture-key", "CLAUDE_CONFIG_DIR": "/tmp/selected-claude"}
        actual = child_environment(env, {"executor": "claude", "worktree": "/tmp/worker-a"})
        for key in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertNotIn(key, actual)
        self.assertEqual(actual["ANTHROPIC_API_KEY"], env["ANTHROPIC_API_KEY"])
        self.assertEqual(actual["CLAUDE_CONFIG_DIR"], env["CLAUDE_CONFIG_DIR"])
        for value in ("1", "0", "false"):
            with self.subTest(persistence=value), self.assertRaisesRegex(ValueError, "persistence"):
                child_environment({**env, "CLAUDE_CODE_SKIP_PROMPT_HISTORY": value},
                                  {"executor": "claude", "worktree": "/tmp/worker-a"})
        self.enable()
        for arguments in (("--no-session-persistence",), ("--",), ("--append-system-prompt",),
                          ("--model",), ("fixture_executor.py",)):
            with self.subTest(arguments=arguments):
                self.config.claude_command = ["claude", *arguments]
                resumed = executor_command(self.config, {"executor": "claude", "invocation": {"task": "continue"},
                    "child_session_id": "12345678-1234-4321-8765-123456789abc", "attempts": 1,
                    "permission_profile": PROFILE, "resolved_model": MODEL,
                    "claude_executable": str(self.root / "claude-fixture"), "executor_version": "2.1.303"})
                self.assertEqual(resumed[0], str(self.root / "claude-fixture"))
                with self.assertRaisesRegex(ValueError, "one executable"):
                    self.runtime.validate({"invocation": {"executor": "claude", "model": "test",
                                                          "parent_worktree": str(self.repo), "parent_repo": str(self.repo)}})
        with self.assertRaises(ValueError):
            executor_command(self.config, {"executor": "claude", "invocation": {"task": "continue"}, "attempts": 1})


class ClaudeAttemptBindingTests(unittest.TestCase):
    def test_new_attempts_inspect_the_selected_path_and_recovery_keeps_its_binding(self):
        with tempfile.TemporaryDirectory(prefix="router-claude-binding-") as directory:
            root = Path(directory).resolve()
            selected = root / "bin" / "claude"
            alternate = root / "other" / "claude"
            shadow = root / "worktree" / ".venv" / "bin" / "claude"
            for executable in (selected, alternate, shadow):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("fixture executable; never invoked")
                executable.chmod(0o700)
            link = root / "selected-link"
            link.symlink_to(selected)
            for command, search, reconfigure in ((["claude"], str(selected.parent), False),
                    (["claude"], "bin", True), ([str(selected)], str(selected.parent), True),
                    (["bin/claude"], str(selected.parent), False), ([str(link)], str(selected.parent), False),
                    (None, str(selected.parent), False)):
                for recovery in (None, "version", "setup", "legacy", "relative-binding", "missing-version"):
                    with self.subTest(command=command, recovery=recovery, search=search):
                        link.unlink()
                        link.symlink_to(selected)
                        version_output = root / "version.stdout"
                        version_output.write_text("2.1.303 (Claude Code)\n")
                        task = {"task_id": "task-fixture", "session_id": "parent", "status": "retrying",
                                "executor": "claude", "attempts": 1, "attempt_number": 1,
                                "executor_version": "2.1.303", "claude_executable": str(selected),
                                "child_session_id": "12345678-1234-4321-8765-123456789abc",
                                "resolved_model": MODEL, "permission_profile": PROFILE, "setup_done": True,
                                "snapshot_commit": "snapshot", "worktree": str(root / "worktree"),
                                "invocation": {"task": "batch-job", "parent_worktree": str(root)},
                                "phase": "version" if recovery == "version" else "setup",
                                "process": {"stdout": str(version_output)}, "execution_stopped": True}
                        if recovery == "version":
                            task.pop("executor_version")
                        if recovery == "legacy":
                            task.pop("claude_executable")
                        elif recovery == "relative-binding":
                            task["claude_executable"] = "claude"
                        elif recovery == "missing-version":
                            task.pop("executor_version")
                        current = SimpleNamespace(claude_command=command if recovery is None else [str(alternate)],
                                                  max_attempts=3, attempt_timeout_seconds=30)
                        def update(task_id, change, task=task):
                            change(task)
                            return task.copy()
                        runtime = Runtime.__new__(Runtime)
                        runtime.store = SimpleNamespace(get=lambda task_id, task=task: task.copy(), update=update,
                                                        update_with_attempt=update)
                        runtime.control = SimpleNamespace(settings=current, environment={"PATH": search}, directory=root)
                        phases = []
                        def phase(task_id, name, argv, cwd, runtime=runtime, task=task, phases=phases,
                                  version_output=version_output, reconfigure=reconfigure, current=current, **kwargs):
                            environment = runtime.control.environment
                            if name == "child":
                                environment = child_environment(environment, task)
                            executable = Path(argv[0]) if Path(argv[0]).is_absolute() else Path(
                                shutil.which(argv[0], path=environment["PATH"]) or cwd / argv[0])
                            phases.append((name, str(executable.resolve())))
                            task.update(phase=name, process={"stdout": str(version_output)})
                            if name == "version":
                                if reconfigure:
                                    current.claude_command = [str(alternate)]
                                link.unlink()
                                link.symlink_to(alternate)
                            else:
                                task.update(executor_completed=True, thread_observed=True)
                            return {"exit_code": 0}
                        runtime.phase = phase
                        runtime.observe = lambda *args, **kwargs: {"exit_code": 0}
                        result = {}
                        runtime.finish = lambda task_id, outcome, reason, result=result, **kwargs: result.update(outcome=outcome, reason=reason)
                        runtime.execute(task["task_id"], recovered=recovery is not None)
                        if recovery in {"legacy", "relative-binding", "missing-version"} or command is None and recovery is None:
                            self.assertEqual(result["outcome"], "failed")
                            self.assertNotIn("child", [name for name, _ in phases])
                            if command is None and recovery is None:
                                self.assertIn("one executable", result["reason"])
                                self.assertNotIn("executor_version", task)
                                self.assertNotIn("claude_executable", task)
                        else:
                            self.assertEqual(result["outcome"], "completed", result)
                            self.assertEqual([path for _, path in phases], [str(selected)] * len(phases))
                            self.assertEqual([name for name, _ in phases], ["version", "child"] if recovery is None else ["child"])
                            self.assertEqual(task["executor_version"], "2.1.303")


class ClaudeAccountingTests(unittest.TestCase):
    def test_persisted_accounting_projection_preserves_raw_history_and_cost(self):
        buckets = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        with tempfile.TemporaryDirectory(prefix="router-claude-accounting-") as directory:
            database = Path(directory) / "router.sqlite3"
            for bucket in buckets:
                cases = [("published-overflow", 2 * MAX_WIRE_INTEGER), ("null", None), ("boolean", True),
                         ("negative", -1), ("missing", 0), ("nested", {}), ("partial", 7)]
                cases += [("aggregate", MAX_WIRE_INTEGER + edge) for edge in (-1, 0, 1)]
                for index, (case, value) in enumerate(cases):
                    with self.subTest(bucket=bucket, case=case, value=value):
                        store = Store(database)
                        task = {"task_id": f"task-{bucket}-{index}", "session_id": "parent", "executor": "claude",
                                "status": "awaiting_review", "attempt_number": 1, "attempts": 1,
                                "review_retries": 0, "continuations": 0, "resolved_model": MODEL,
                                "invocation": {"task": "batch-job", "parent_worktree": "/tmp/example-project"},
                                "usage": dict.fromkeys(buckets, 0), "usage_complete": True, "cost": .25}
                        task["usage"][bucket] = value
                        history = []
                        expected = None
                        complete = False
                        if case == "missing":
                            task["usage"].pop(bucket)
                        elif case == "aggregate":
                            prior = copy.deepcopy(task)
                            prior["usage"][bucket] = MAX_WIRE_INTEGER - 1
                            history.append(prior)
                            task.update(attempt_number=2, attempts=2)
                            task["usage"][bucket] = value - (MAX_WIRE_INTEGER - 1)
                            if value <= MAX_WIRE_INTEGER:
                                expected = {**dict.fromkeys(buckets, 0), bucket: value}
                                complete = True
                        elif case == "partial":
                            prior = copy.deepcopy(task)
                            history.append(prior)
                            task.update(attempt_number=2, attempts=2, usage={}, usage_complete=False)
                            expected = prior["usage"]
                        with store.transaction() as db:
                            store.put(db, task)
                            for frame in history:
                                store.save_attempt(frame, db)
                            store.save_attempt(task, db)
                        # Reopen persisted published-format frames, without rewriting their evidence.
                        store = Store(database)
                        before = (store.get(task["task_id"]), store.history(task["task_id"]))
                        control = Controller.__new__(Controller)
                        control.store = store
                        control.settings = SimpleNamespace(max_attempts=1)
                        control.native = SimpleNamespace(status=lambda **kwargs: [])
                        views = (public(before[0], store=store), visibility(*before),
                                 control.status({"task_id": task["task_id"]}),
                                 next(row for row in control.status({}) if row["task_id"] == task["task_id"]),
                                 next(row for row in control.status({"filter": "needs_decision"})
                                      if row["task_id"] == task["task_id"]))
                        for view in views:
                            self.assertEqual(view["usage"], expected)
                            self.assertIs(view["usage_complete"], complete)
                            self.assertEqual(view["cost"], .5 if history else .25)
                        for frame in views[0]["attempt_history"]:
                            valid = case == "aggregate" or case == "partial" and frame["attempt_number"] == 1
                            self.assertIs(frame["usage_complete"], valid)
                            self.assertEqual(set(frame["usage"]), set(buckets) if valid else set())
                        self.assertEqual(before, (store.get(task["task_id"]), store.history(task["task_id"])))

    def test_malformed_identity_and_model_evidence_fails_closed(self):
        session = "12345678-1234-4321-8765-123456789abc"
        selected = MODEL.removeprefix("anthropic/")
        counts = {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 40, "cacheCreationInputTokens": 10}
        init = {"type": "system", "subtype": "init", "session_id": session, "model": selected}
        result = {"type": "result", "session_id": session, "subtype": "success", "is_error": False,
                  "total_cost_usd": .25, "modelUsage": {selected: counts}}
        events = [{"type": []}, {"type": {}}, {"type": None},
                  {"type": "assistant", "session_id": session, "message": []},
                  {"type": "assistant", "session_id": session, "message": {"model": "other"}},
                  {**init, "model": "other"}, {**result, "session_id": []}, {**result, "session_id": "invalid"},
                  {**result, "session_id": "87654321-1234-4321-8765-123456789abc"},
                  {**result, "subtype": "error_during_execution", "modelUsage": None},
                  {**result, "is_error": True, "modelUsage": None}]
        foreign = "claude-opus-4-6"
        usages = [{foreign: counts}, {foreign: None}, {foreign: []}, {"": counts}, {"x" * 241: counts},
                  {foreign: counts, "invalid model": counts},
                  {f"claude-opus-fixture-{index}": counts for index in range(33)}]
        for bucket in counts:
            missing = counts.copy()
            missing.pop(bucket)
            usages.append({foreign: missing})
            usages.extend({foreign: {**counts, bucket: value}} for value in (None, True, -1, MAX_WIRE_INTEGER + 1))
        events.extend({**result, "modelUsage": usage} for usage in usages)
        with tempfile.TemporaryDirectory(prefix="router-claude-model-evidence-") as directory:
            database = Path(directory) / "router.sqlite3"
            store = Store(database)
            runtime = Runtime.__new__(Runtime)
            runtime.store = store
            runtime.observe = lambda *args, **kwargs: {"exit_code": 0}
            for index, event in enumerate(events):
                with self.subTest(event=event):
                    task = {"task_id": f"task-{index}", "session_id": "parent", "executor": "claude",
                            "status": "running", "phase": "child", "process": {}, "execution_stopped": True,
                            "child_session_id": session, "resolved_model": MODEL, "created_at": 1,
                            "attempt_number": 1, "attempts": 1, "review_retries": 0, "continuations": 0,
                            "invocation": {"task": "batch-job", "parent_worktree": "/tmp/example-project"}}
                    with store.transaction() as db:
                        store.put(db, task)
                    output = Path(directory) / "events.jsonl"
                    output.write_text("".join(json.dumps(value) + "\n" for value in (init, event)))
                    offset = runtime.events(task, {"stdout": str(output)}, 0, final=True)
                    self.assertTrue(store.get(task["task_id"]).get("execution_error"))
                    # Later matching evidence and repeated results cannot erase an execution error.
                    with output.open("a") as stream:
                        stream.write("".join(json.dumps(value) + "\n" for value in (init, result, result)))
                    runtime.events(task, {"stdout": str(output)}, offset, final=True)
                    self.assertTrue(Runtime.child_failed(store.get(task["task_id"])))
                    runtime.execute(task["task_id"], recovered=True)
                    reopened = Store(database)
                    view = public(reopened.get(task["task_id"]), store=reopened)
                    self.assertEqual(view["status"], "failed")
                    self.assertEqual(view["attempt_history"][0]["status"], "failed")
                    self.assertEqual(view["cost"], .25)

    def test_result_replacement_baselines_and_invalid_numeric_fields(self):
        session = "12345678-1234-4321-8765-123456789abc"
        event = {"type": "result", "session_id": session, "subtype": "success", "is_error": False,
                 "total_cost_usd": .5, "modelUsage": {"claude-sonnet-4-6": {
                     "inputTokens": 200, "outputTokens": 40, "cacheReadInputTokens": 80,
                     "cacheCreationInputTokens": 20, "costUSD": 999}}}
        counts = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}
        base = {"executor": "claude", "child_session_id": session, "resolved_model": MODEL,
                "claude_baseline": {"schema_version": 1, "models": {"claude-sonnet-4-6": counts},
                                    "cost": .25, "complete": True, "cost_complete": True}}
        collect(base, {"type": "system", "subtype": "init", "session_id": session,
                       "model": MODEL.removeprefix("anthropic/")})
        task = copy.deepcopy(base)
        for _ in range(2):
            collect(task, event)
        self.assertEqual(task["usage"], counts)
        self.assertEqual(task["cost"], .25)
        self.assertTrue(task["usage_complete"])
        for value in (None, True, -1, float("nan"), float("inf"), "100"):
            for field in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "total_cost_usd"):
                with self.subTest(field=field, value=value):
                    malformed = copy.deepcopy(event)
                    if field == "total_cost_usd":
                        malformed[field] = value
                    else:
                        malformed["modelUsage"]["claude-sonnet-4-6"][field] = value
                    task = copy.deepcopy(base)
                    collect(task, malformed)
                    self.assertFalse(Runtime.child_failed(task))
                    if field == "total_cost_usd":
                        self.assertNotIn("cost", task)
                    else:
                        self.assertFalse(task["usage_complete"])
                        self.assertEqual(task["usage"], {})
                        self.assertEqual(task["cost"], .25)
        selected = "claude-sonnet-4-6"
        auxiliary = "claude-haiku-4-5"
        counters = {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 40, "cacheCreationInputTokens": 10}
        for usage in ({}, {"modelUsage": None}, {"modelUsage": {}}, {"modelUsage": []},
                      {"modelUsage": {selected: None}}, {"modelUsage": {selected: counters, "": counters}},
                      {"modelUsage": {selected: counters, **{f"model-{index}": counters for index in range(32)}}}):
            with self.subTest(unavailable_usage=usage):
                task = copy.deepcopy(base)
                collect(task, {**{key: value for key, value in event.items() if key != "modelUsage"}, **usage})
                self.assertFalse(Runtime.child_failed(task))
                self.assertFalse(task["usage_complete"])
                self.assertEqual(task["usage"], {})
                self.assertEqual(task["cost"], .25)
        prior = {**copy.deepcopy(base), "claude_baseline": {"schema_version": 1, "models": {}, "complete": True, "cost_complete": True}}
        collect(prior, {**event, "modelUsage": {selected: counters, auxiliary: counters}})
        for variant in ("missing-model", "decreased-model", "missing-bucket", "decreased-bucket"):
            with self.subTest(continuity=variant):
                current = {selected: {key: value * 3 for key, value in counters.items()}, auxiliary: counters.copy()}
                if variant == "missing-model":
                    current.pop(auxiliary)
                elif variant == "decreased-model":
                    current[auxiliary] = dict.fromkeys(counters, 0)
                elif variant == "missing-bucket":
                    current[auxiliary].pop("cacheReadInputTokens")
                else:
                    current[auxiliary]["cacheReadInputTokens"] -= 1
                task = {**copy.deepcopy(base), "claude_baseline": prior["claude_total"]}
                collect(task, {**event, "total_cost_usd": .75, "modelUsage": current})
                self.assertFalse(task["usage_complete"])
                self.assertEqual(task["usage"], {})
                self.assertEqual(task["cost"], .25)
        # A newly observed model is legitimate; its counters start from zero.
        current = {selected: {key: value * 3 for key, value in counters.items()}, auxiliary: counters.copy(),
                   "additional-model": counters.copy()}
        task = {**copy.deepcopy(base), "claude_baseline": prior["claude_total"]}
        collect(task, {**event, "total_cost_usd": .75, "modelUsage": current})
        self.assertTrue(task["usage_complete"])
        self.assertEqual(task["usage"]["input_tokens"], 300)
        self.assertEqual(task["cost"], .25)
        self.assertEqual(task["claude_total"]["schema_version"], 1)
        # Cumulative totals, not just their per-model contributors or attempt deltas, must fit the wire.
        for native_bucket, bucket in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                ("cacheReadInputTokens", "cache_read_input_tokens"), ("cacheCreationInputTokens", "cache_creation_input_tokens")):
            for edge in (-1, 0, 1):
                for resumed in (False, True):
                    with self.subTest(aggregate_bucket=bucket, edge=edge, resumed=resumed):
                        left = dict.fromkeys(counters, 0)
                        right = left.copy()
                        first = {**copy.deepcopy(base), "claude_baseline": {"schema_version": 1, "models": {},
                                 "complete": True, "cost_complete": True}}
                        history = []
                        if resumed:
                            left[native_bucket] = MAX_WIRE_INTEGER - 1
                            collect(first, {**event, "total_cost_usd": .25, "modelUsage": {selected: left, auxiliary: right}})
                            self.assertTrue(first["usage_complete"])
                            history = [{**first, "attempt_number": 1}]
                            right[native_bucket] = edge + 1
                            baseline = first["claude_total"]
                        else:
                            left[native_bucket] = MAX_WIRE_INTEGER // 2
                            right[native_bucket] = MAX_WIRE_INTEGER - left[native_bucket] + edge
                            baseline = first["claude_baseline"]
                        task = {**copy.deepcopy(base), "claude_baseline": baseline, "attempt_number": 2 if resumed else 1}
                        collect(task, {**event, "modelUsage": {selected: left, auxiliary: right}})
                        self.assertEqual(task["usage_complete"], edge <= 0)
                        self.assertEqual(task["cost"], .25 if resumed else .5)
                        if edge > 0:
                            self.assertEqual(task["usage"], {})
                        view = public({**task, "session_id": "parent", "status": "awaiting_review", "executor": "claude",
                            "invocation": {"task": "batch-job", "parent_worktree": "/tmp/example-project"}}, history=history)
                        self.assertEqual(view["usage_complete"], edge <= 0)
                        if edge <= 0:
                            self.assertEqual(view["usage"][bucket], MAX_WIRE_INTEGER + edge)
                        elif view["usage"] is not None:
                            self.assertLessEqual(view["usage"][bucket], MAX_WIRE_INTEGER)
                        self.assertEqual(view["cost"], .5)
        # Previously published aggregate-only baselines cannot establish per-model continuity.
        for baseline in ({"usage": counts, "complete": True, "cost": .25, "cost_complete": True},
                         {"schema_version": 2, "models": {}, "complete": True, "cost": .25, "cost_complete": True}):
            with self.subTest(upgrade=baseline):
                task = {**copy.deepcopy(base), "claude_baseline": baseline}
                collect(task, event)
                self.assertFalse(task["usage_complete"])
                self.assertEqual(task["usage"], {})
                self.assertEqual(task["cost"], .25)
                self.assertEqual(task["claude_total"]["schema_version"], 1)
        view_task = {**task, "session_id": "parent", "status": "awaiting_review", "executor": "claude",
                     "claude_executable": "/tmp/worker-a", "invocation": {"task": "batch-job", "parent_worktree": "/tmp/example-project"}}
        view = public(view_task, history=[view_task])
        for internal in ("claude_baseline", "claude_total", "claude_executable"):
            self.assertNotIn(internal, view)
            self.assertNotIn(internal, view["attempt_history"][0])
            self.assertIn(internal, view_task)
        for baseline in (None, [], {"complete": False, "cost_complete": False},
                         {"usage": {"input_tokens": 201}, "cost": .51}):
            task = {**copy.deepcopy(base), "claude_baseline": baseline}
            collect(task, event)
            self.assertFalse(task["usage_complete"])
            self.assertNotIn("cost", task)
