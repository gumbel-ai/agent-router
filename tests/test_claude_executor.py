"""Claude child contracts using local structured-output fixtures only."""

import copy
import json
import os
import shlex
import signal
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_runtime import RuntimeHarness

from router import service
from router.child import child_environment, executor_command
from router.claude_executor import collect
from router.daemon import PhaseStopped, Runtime
from router.process import creation_identity, owned_state

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
        task_id = self.start("silent hang", executor="claude")
        running = self.wait(task_id, {"running"})
        result_path = Path(running["worktree"]) / "result.txt"
        deadline = time.monotonic() + 15
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(.04)
        self.assertTrue(result_path.exists(), "Child did not publish its ready work")
        # Expire an already-running child; interpreter/Git startup is not the deadline under test.
        with patch("router.daemon.now_ms", return_value=running["deadline_at"] + 1):
            task = self.wait(task_id, {"failed", "interrupted"})
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["failure_reason"], "router-timeout")
        self.assertTrue(task["execution_stopped"])
        self.assertEqual(task["attempts"], 1)
        self.assertEqual((Path(task["worktree"]) / "result.txt").read_text(), "first\n")

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
        for index, outcome in enumerate(("invalid", "success", "deadline", "cancel", "restart")):
            with self.subTest(outcome=outcome):
                directory = self.root / f"version-{index}"
                directory.mkdir()
                self.control.environment.update(ROUTER_FIXTURE_VERSION_DIRECTORY=str(directory),
                                                ROUTER_FIXTURE_VERSION_MODE="wait",
                                                ROUTER_FIXTURE_CLAUDE_VERSION="invalid" if outcome == "invalid" else "2.1.303")
                if outcome == "restart":
                    with patch.object(self.runtime, "observe", side_effect=SystemExit("synthetic daemon loss")):
                        task_id = self.start("must not launch", f"wrapper-{index}", executor="claude")
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            task = self.control.store.get(task_id)
                            if task.get("phase") == "version" and task_id not in self.runtime.workers:
                                break
                            time.sleep(.04)
                else:
                    task_id = self.start("must not launch", f"wrapper-{index}", executor="claude")
                deadline = time.monotonic() + 15
                while not (directory / "helper.pid").exists() and time.monotonic() < deadline:
                    time.sleep(.04)
                self.assertTrue((directory / "helper.pid").exists())
                pid = int((directory / "helper.pid").read_text())
                identity = creation_identity(pid)
                try:
                    task = self.control.store.get(task_id)
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
                finally:
                    try:
                        if creation_identity(pid) == identity:
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.control.environment.pop("ROUTER_FIXTURE_VERSION_DIRECTORY")

    def test_unconfirmed_foreground_shutdown_reserves_capacity_across_restart(self):
        self.enable()
        self.config.concurrency_cap = 1
        for index, prompt in enumerate(("crash hang", "ignore interrupt hang")):
            with self.subTest(prompt=prompt):
                task_id = self.start(prompt, f"foreground-{index}", executor="claude")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    task = self.control.store.get(task_id)
                    pid_file = Path(task["worktree"]) / "sleeper.pid" if task.get("worktree") else None
                    if pid_file and pid_file.exists():
                        break
                    time.sleep(.04)
                self.assertTrue(pid_file and pid_file.exists(), task)
                pid = int(pid_file.read_text())
                identity = creation_identity(pid)
                self.assertEqual(os.getpgid(pid), pid)
                try:
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
                finally:
                    if creation_identity(pid) == identity:
                        os.killpg(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.04)
                    else:
                        self.fail("Foreground fixture did not stop after explicit inspection cleanup")
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
                with self.assertRaisesRegex(ValueError, "one executable"):
                    executor_command(self.config, {"executor": "claude", "invocation": {"task": "continue"},
                                                   "child_session_id": "12345678-1234-4321-8765-123456789abc", "attempts": 0,
                                                   "permission_profile": PROFILE, "resolved_model": MODEL})
                with self.assertRaisesRegex(ValueError, "one executable"):
                    self.runtime.validate({"invocation": {"executor": "claude", "model": "test",
                                                          "parent_worktree": str(self.repo), "parent_repo": str(self.repo)}})
        with self.assertRaises(ValueError):
            executor_command(self.config, {"executor": "claude", "invocation": {"task": "continue"}, "attempts": 1})


class ClaudeAccountingTests(unittest.TestCase):
    def test_malformed_identity_and_model_evidence_fails_closed(self):
        session = "12345678-1234-4321-8765-123456789abc"
        for event in ({"type": []}, {"type": {}}, {"type": None},
                      {"type": "assistant", "session_id": session, "message": []},
                      {"type": "assistant", "session_id": session, "message": {"model": "other"}},
                      {"type": "system", "subtype": "init", "session_id": session, "model": "other"},
                      {"type": "result", "session_id": []}, {"type": "result", "session_id": "invalid"}):
            with self.subTest(event=event):
                task = {"executor": "claude", "child_session_id": session, "resolved_model": MODEL,
                        "thread_observed": True, "executor_completed": True}
                collect(task, event)
                self.assertTrue(Runtime.child_failed(task))


    def test_result_replacement_baselines_and_invalid_numeric_fields(self):
        session = "12345678-1234-4321-8765-123456789abc"
        event = {"type": "result", "session_id": session, "subtype": "success", "is_error": False,
                 "total_cost_usd": .5, "modelUsage": {"claude-sonnet-4-6": {
                     "inputTokens": 200, "outputTokens": 40, "cacheReadInputTokens": 80,
                     "cacheCreationInputTokens": 20}}}
        base = {"child_session_id": session, "resolved_model": MODEL,
                "claude_baseline": {"usage": {"input_tokens": 100, "output_tokens": 20,
                                              "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10},
                                    "cost": .25, "complete": True, "cost_complete": True}}
        task = copy.deepcopy(base)
        for _ in range(2):
            collect(task, event)
        self.assertEqual(task["usage"], base["claude_baseline"]["usage"])
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
                    if field == "total_cost_usd":
                        self.assertNotIn("cost", task)
                    else:
                        self.assertFalse(task["usage_complete"])
                        self.assertEqual(task["usage"], {})
        for baseline in ({"complete": False, "cost_complete": False},
                         {"usage": {"input_tokens": 201}, "cost": .51}):
            task = {**copy.deepcopy(base), "claude_baseline": baseline}
            collect(task, event)
            self.assertFalse(task["usage_complete"])
            self.assertNotIn("cost", task)
