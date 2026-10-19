import fcntl
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_control import observe_every_session, settings

from router import service
from router.child import launch
from router.cli import table
from router.daemon import Controller, Runtime, serve
from router.process import UncertainProcess, creation_identity, owned_state, stop_owned
from router.snapshot import apply_delta, cleanup, git

PREPARE_STDERR_LIMIT = 64 * 1024


class RuntimeHarness:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / ".gitignore").write_text(".venv/\n.env\n")
        (self.repo / "tracked.txt").write_text("baseline\n")
        (self.repo / "opencode.json").write_text(json.dumps({"permission": {"*": "allow", "external_directory": "deny"}}))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")
        self.config = settings(self.repo, command=[sys.executable, str(Path(__file__).with_name("fixture_executor.py"))])
        observe_every_session(self)
        self.control = Controller(self.config, os.environ.copy(), self.root / "state")
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.instruction = self.control.handle("submit", {"session_id": "S", "prompt_id": "P"})["instruction_id"]

    def mutate(self, operation, payload):
        self.control.handle("authorize", {"session_id": "S", "prompt_id": "P", "operation": operation, "invocation": payload})
        return self.control.handle(operation, payload)

    def start(self, prompt="flood", request="R", executor="opencode"):
        return self.mutate("start", {"request_version": "1", "request_id": request, "instruction_id": self.instruction,
                                     "executor": executor, "model": "test", "task": prompt,
                                     "parent_repo": str(self.repo), "parent_worktree": str(self.repo), "mode": "write"})["task_id"]

    def wait(self, task_id, states, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            task = self.control.handle("status", {"task_id": task_id})
            if task["status"] in states:
                return task
            time.sleep(0.04)
        diagnostics = []
        directory = task.get("artifact_dir")
        paths = sorted(Path(directory).glob("attempt-*/prepare/*.stderr")) if directory else []
        for path in paths:
            try:
                with path.open("rb") as stream:
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, size - PREPARE_STDERR_LIMIT))
                    content = stream.read(PREPARE_STDERR_LIMIT).decode(errors="replace") or "[empty stderr]"
                if size > PREPARE_STDERR_LIMIT:
                    content = "[truncated; showing stderr tail]\n" + content
            except OSError as error:
                content = f"[stderr unavailable: {error}]"
            diagnostics.append(f"{path.relative_to(directory)}:\n{content}")
        evidence = "\n".join(diagnostics) if diagnostics else "[no prepare stderr artifacts available]"
        self.fail(f"Task did not reach {states}: {task}\nGit prepare stderr:\n{evidence}")

class ExitingLeader:
    """Models Darwin under load: an exiting group leader stays unsignalable before it becomes reapable."""

    def __init__(self, process, record, seconds):
        self.process, self.completion, self.seconds = process, Path(record["completion"]), seconds
        self.reapable_at = None

    def reapable(self):
        # The gate publishes completion before exiting; the modeled exit starts there.
        if self.reapable_at is None and self.completion.exists():
            self.reapable_at = time.monotonic() + self.seconds
        return self.reapable_at is not None and time.monotonic() >= self.reapable_at

    def exiting(self):
        return not self.reapable() and self.reapable_at is not None

    def poll(self):
        # Never reap before the modeled window ends, so completion cannot bypass it.
        return self.process.poll() if self.reapable() else None

    def wait(self, timeout=None):
        if self.exiting():
            remaining = self.reapable_at - time.monotonic()
            if timeout is not None and timeout < remaining:
                time.sleep(timeout)
                raise subprocess.TimeoutExpired("leader", timeout)
            time.sleep(max(0, remaining))
        return self.process.wait(timeout)


def exiting_leaders(test):
    """Patch the group signal probe: Darwin reports EPERM for a group whose leader is exiting."""
    leaders, killpg = {}, os.killpg
    def probe(pgid, signal_number):
        leader = leaders.get(pgid)
        if signal_number == 0 and leader is not None and leader.exiting():
            raise PermissionError(1, "Operation not permitted")
        return killpg(pgid, signal_number)
    patcher = patch("router.process.os.killpg", side_effect=probe)
    patcher.start()
    test.addCleanup(patcher.stop)
    return leaders


class RuntimeTests(RuntimeHarness, unittest.TestCase):
    def test_wait_failure_includes_git_preparation_stderr(self):
        # A real Git namespace conflict makes worktree branch creation fail deterministically.
        git(self.repo, "branch", "agent")
        task_id = self.start("batch-job")
        task = self.wait(task_id, {"failed"})
        self.assertEqual(task["phase"], "prepare")
        stderr_path = Path(task["process"]["stderr"])
        stderr = stderr_path.read_text()
        self.assertIn("refs/heads/agent", stderr)
        clock = [0.0]

        def advance(_):
            clock[0] = 2.0

        timer = SimpleNamespace(monotonic=lambda: clock[0], sleep=advance)
        for variant in ("available", "oversized", "missing", "unreadable"):
            with self.subTest(variant=variant):
                if variant == "oversized":
                    stderr_path.write_text("prefix-marker\n" + "x" * 1_000_000 + "\ntail-marker")
                elif variant == "missing":
                    stderr_path.unlink()
                elif variant == "unreadable":
                    stderr_path.mkdir()
                clock[0] = 0.0
                with patch("test_runtime.time", timer), self.assertRaises(AssertionError) as failed:
                    self.wait(task_id, {"running"}, seconds=1)
                message = str(failed.exception)
                self.assertIn("Git prepare failed", message)
                if variant == "available":
                    self.assertIn(stderr, message)
                elif variant == "oversized":
                    self.assertIn("tail-marker", message)
                    self.assertNotIn("prefix-marker", message)
                    self.assertIn("truncated", message)
                    self.assertLess(len(message), 100_000)
                else:
                    self.assertIn("no prepare stderr artifacts" if variant == "missing" else "stderr unavailable", message)

    def seed_attempt_task(self, task_id: str, *, second_attempt: bool) -> dict:
        self.runtime.stop()
        started = int(time.time() * 1000) - 100
        first = {
            "task_id": task_id,
            "session_id": "S",
            "status": "awaiting_review",
            "attempts": 1,
            "attempt_number": 1,
            "attempt_started_at": started,
            "duration_ms": 10,
            "cost": 2.0,
            "usage": {"input_tokens": 2},
            "usage_complete": True,
            "execution_stopped": True,
            "execution_outcome": "completed",
            "created_at": started,
            "invocation": {"task": "transaction test", "parent_worktree": str(self.repo)},
        }
        current = dict(first)
        if second_attempt:
            current.update(
                status="running",
                attempts=2,
                attempt_number=2,
                attempt_started_at=started + 20,
                cost=20.0,
                usage={"input_tokens": 20},
                execution_stopped=False,
            )
        with self.control.store.transaction() as db:
            self.control.store.put(db, current)
        self.control.store.save_attempt(first)
        return current

    # Durable outcomes are under test, not the 40 ms acknowledgement latency.
    @patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", 5)
    def test_lifecycle_failure_reports_not_persisted_and_writer_continues(self):
        at = int(time.time() * 1000)
        failed = self.control.handle("lifecycle_observe", {
            "session": {"source": "claude", "session_id": "partial-write",
                        "kind": "session_start", "observed_at": at},
            "native": {"source": "claude", "session_id": "S",
                       "agent_id": "unsafe\x1b[2Jidentity", "kind": "result",
                       "observed_at": at},
        })
        self.assertEqual(failed["accepted"], False)
        self.assertEqual(failed["queued"], False)
        self.assertEqual(failed["persisted"], False)
        self.assertTrue(failed["partial"])
        self.assertEqual(failed["persistence"], {"session": "persisted", "native": "failed"})
        self.assertIn("partially persisted", failed["reason"])
        failed_snapshot = self.control.handle("snapshot", {})
        self.assertIn("partial-write", {session["session_id"] for session in failed_snapshot["sessions"]})
        self.assertTrue(failed_snapshot["partial"])
        self.assertIn("1 write failures", failed_snapshot["errors"]["lifecycle"])
        following = self.control.handle("lifecycle_observe", {"session": {
            "source": "claude", "session_id": "following", "kind": "session_start",
            "observed_at": at + 1,
        }})
        self.assertEqual(following, {
            "accepted": True,
            "queued": False,
            "persisted": True,
            "partial": False,
            "persistence": {"session": "persisted"},
        })
        self.assertIn("following", {
            session["session_id"] for session in self.control.handle("snapshot", {})["sessions"]
        })

    @patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", 5)
    def test_one_lost_lifecycle_write_does_not_keep_snapshots_partial_forever(self):
        at = int(time.time() * 1000)
        rejected = self.control.handle("lifecycle_observe", {"native": {
            "source": "claude", "session_id": "S", "agent_id": "child", "kind": "bogus", "observed_at": at}})
        self.assertFalse(rejected["accepted"])
        self.assertIn("1 write failures", self.control.lifecycle_health())
        with patch("router.daemon.now_ms", return_value=at + 60_000):
            self.assertIsNotNone(self.control.lifecycle_health())
        with patch("router.daemon.now_ms", return_value=at + 61_000):
            self.assertIsNone(self.control.lifecycle_health())

    # Durable outcomes are under test, not the 40 ms acknowledgement latency.
    @patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", 5)
    def test_lifecycle_failure_identifies_component_when_nothing_persists(self):
        failed = self.control.handle("lifecycle_observe", {"native": {
            "source": "claude", "session_id": "S", "agent_id": "unsafe\x1b[2Jidentity",
            "kind": "result", "observed_at": int(time.time() * 1000),
        }})
        self.assertEqual(failed, {
            "accepted": False,
            "queued": False,
            "persisted": False,
            "partial": False,
            "persistence": {"native": "failed"},
            "reason": "Lifecycle observation could not be persisted; failed components: native",
        })

    # Durable outcomes are under test, not the 40 ms acknowledgement latency.
    @patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", 5)
    def test_unexpected_lifecycle_observer_failure_isolated_from_paired_write(self):
        at = int(time.time() * 1000)
        payload = {
            "session": {"source": "claude", "session_id": "unexpected",
                        "kind": "session_start", "observed_at": at},
            "native": {"source": "claude", "session_id": "unexpected",
                       "agent_id": "child", "kind": "start", "observed_at": at},
        }
        with patch.object(self.control.sessions, "observe", side_effect=RuntimeError("observer bug")):
            failed = self.control.handle("lifecycle_observe", payload)

        self.assertFalse(failed["accepted"])
        self.assertFalse(failed["queued"])
        self.assertFalse(failed["persisted"])
        self.assertTrue(failed["partial"])
        self.assertEqual(failed["persistence"], {"session": "failed", "native": "persisted"})
        self.assertIn("partially persisted", failed["reason"])
        self.assertEqual(
            self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "writer-survived",
                "kind": "session_start", "observed_at": at + 1,
            }})["accepted"],
            True,
        )

    def test_lifecycle_queue_overflow_is_reported_in_snapshot_health(self):
        with patch.object(self.control.lifecycle_queue, "put_nowait", side_effect=queue.Full):
            result = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "full", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
        self.assertFalse(result["accepted"])
        self.assertFalse(result["queued"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["persistence"], {"session": "rejected"})
        snapshot = self.control.handle("snapshot", {})
        self.assertTrue(snapshot["partial"])
        self.assertIn("1 queue overflows", snapshot["errors"]["lifecycle"])

    def test_queued_lifecycle_work_is_nondurable_before_writer_begins_processing(self):
        self.control.stop_lifecycle_writer()
        dequeued = threading.Event()
        release = threading.Event()
        original_get = self.control.lifecycle_queue.get
        response = {}

        def paused_get():
            work = original_get()
            if work is not None:
                dequeued.set()
                release.wait(1)
            return work

        with patch.object(self.control.lifecycle_queue, "get", side_effect=paused_get):
            self.control.start_lifecycle_writer()
            submitter = threading.Thread(target=lambda: response.update(self.control.handle(
                "lifecycle_observe", {"session": {
                    "source": "claude", "session_id": "queued-before-write", "kind": "session_start",
                    "observed_at": int(time.time() * 1000),
                }})))
            submitter.start()
            try:
                self.assertTrue(dequeued.wait(1))
                submitter.join(timeout=1)
                self.assertEqual(response["accepted"], False)
                self.assertEqual(response["queued"], True)
                self.assertEqual(response["persisted"], False)
                self.assertEqual(response["partial"], False)
                self.assertEqual(response["persistence"], {"session": "pending"})
                snapshot = self.control.handle("snapshot", {})
                self.assertTrue(snapshot["partial"])
                self.assertIn("1 queued writes pending", snapshot["errors"]["lifecycle"])
            finally:
                release.set()
                submitter.join(timeout=1)

        deadline = time.monotonic() + 1
        while self.control.lifecycle_pending and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.control.lifecycle_pending, 0)

    def test_lifecycle_shutdown_closes_admission_before_writer_sentinel(self):
        entered = threading.Event()
        release = threading.Event()
        write_timeouts = []
        original = self.control.sessions.observe

        def blocked(event, timeout=10):
            write_timeouts.append(timeout)
            entered.set()
            release.wait(1)
            return original(event, timeout)

        with patch.object(self.control.sessions, "observe", side_effect=blocked):
            first = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "accepted-first", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
            self.assertFalse(first["accepted"])
            self.assertTrue(first["queued"])
            self.assertIn("not durably persisted", first["reason"])
            self.assertTrue(entered.wait(1))
            second = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "accepted-second", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
            self.assertFalse(second["accepted"])
            self.assertTrue(second["queued"])
            self.assertIn("not durably persisted", second["reason"])
            stopper = threading.Thread(target=self.control.stop_lifecycle_writer)
            stopper.start()
            deadline = time.monotonic() + 1
            while not self.control.lifecycle_stopping and time.monotonic() < deadline:
                time.sleep(.01)
            rejected = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "rejected", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
            self.assertEqual(rejected["accepted"], False)
            self.assertEqual(rejected["queued"], False)
            self.assertIn("stopping", rejected["reason"])
            release.set()
            stopper.join(timeout=3)
        self.assertFalse(stopper.is_alive())
        self.assertIsNone(self.control.lifecycle_thread)
        session_ids = {
            session["session_id"] for session in self.control.handle("snapshot", {})["sessions"]
        }
        self.assertIn("accepted-first", session_ids)
        self.assertIn("accepted-second", session_ids)
        self.assertEqual(len(write_timeouts), 2)
        self.assertGreater(write_timeouts[1], 0)

    def test_lifecycle_shutdown_is_hard_bounded_and_retains_blocked_writer(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.control.sessions.observe

        def blocked(event, timeout=10):
            entered.set()
            release.wait()
            return original(event, timeout)

        with (patch.object(self.control.sessions, "observe", side_effect=blocked),
              patch("router.daemon.LIFECYCLE_STOP_TIMEOUT_SECONDS", .05)):
            result = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "slow-accepted", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
            self.assertFalse(result["accepted"])
            self.assertTrue(result["queued"])
            self.assertIn("not durably persisted", result["reason"])
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            try:
                self.control.stop_lifecycle_writer()
                self.assertLess(time.monotonic() - started, .2)
                self.assertIsNotNone(self.control.lifecycle_thread)
                self.assertIn("1 queued writes pending", self.control.lifecycle_health())
            finally:
                release.set()
                self.control.lifecycle_thread.join(timeout=1)
                self.control.stop_lifecycle_writer()

        self.assertIsNone(self.control.lifecycle_thread)
        session_ids = {
            session["session_id"] for session in self.control.handle("snapshot", {})["sessions"]
        }
        self.assertIn("slow-accepted", session_ids)

    def test_lifecycle_shutdown_drains_when_sentinel_enqueue_reports_full(self):
        entered = threading.Event()
        release = threading.Event()
        original_observe = self.control.sessions.observe
        original_put = self.control.lifecycle_queue.put_nowait

        def blocked(event, timeout=10):
            entered.set()
            release.wait(1)
            return original_observe(event, timeout)

        def refuse_sentinel(item):
            if item is None:
                raise queue.Full
            return original_put(item)

        with (patch.object(self.control.sessions, "observe", side_effect=blocked),
              patch.object(self.control.lifecycle_queue, "put_nowait", side_effect=refuse_sentinel)):
            response = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "sentinel-full", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
            self.assertTrue(entered.wait(1))
            self.assertFalse(response["accepted"])
            self.assertTrue(response["queued"])
            stopper = threading.Thread(target=self.control.stop_lifecycle_writer)
            stopper.start()
            deadline = time.monotonic() + 1
            while not self.control.lifecycle_stopping and time.monotonic() < deadline:
                time.sleep(.01)
            release.set()
            stopper.join(timeout=3)

        self.assertFalse(stopper.is_alive())
        self.assertIsNone(self.control.lifecycle_thread)
        sessions, _active_total = self.control.sessions.fleet_records(100)
        session_ids = {session["session_id"] for session in sessions}
        self.assertIn("sentinel-full", session_ids)

    def test_lifecycle_shutdown_is_bounded_during_database_contention(self):
        import sqlite3

        with closing(sqlite3.connect(self.control.store.path, timeout=0)) as locked, locked:
            locked.execute("BEGIN IMMEDIATE")
            result = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "contended", "kind": "session_start",
                "observed_at": int(time.time() * 1000),
            }})
            started = time.monotonic()
            self.control.stop_lifecycle_writer()
            elapsed = time.monotonic() - started
        self.assertFalse(result["accepted"])
        self.assertTrue(result["queued"])
        self.assertFalse(result["persisted"])
        self.assertLess(elapsed, 3)
        self.assertIsNone(self.control.lifecycle_thread)
        self.assertIn("write failures", self.control.lifecycle_health())

    # Ack must fire while native is held (up to 1 s) but after the session write.
    @patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", .5)
    def test_snapshot_marks_paired_lifecycle_midpoint_partial(self):
        native_entered = threading.Event()
        release_native = threading.Event()
        original_native = self.control.native.observe
        response = {}

        def pause_native(event, timeout=10):
            native_entered.set()
            release_native.wait(1)
            return original_native(event, timeout)

        payload = {
            "session": {"source": "claude", "session_id": "paired", "kind": "session_start",
                        "observed_at": int(time.time() * 1000)},
            "native": {"source": "claude", "session_id": "paired", "agent_id": "child",
                       "kind": "start", "observed_at": int(time.time() * 1000)},
        }
        with patch.object(self.control.native, "observe", side_effect=pause_native):
            submitter = threading.Thread(
                target=lambda: response.update(self.control.handle("lifecycle_observe", payload)),
            )
            submitter.start()
            self.assertTrue(native_entered.wait(1))
            submitter.join(timeout=1)
            self.assertFalse(response["accepted"])
            self.assertTrue(response["queued"])
            self.assertTrue(response["partial"])
            self.assertEqual(response["persistence"], {"session": "persisted", "native": "pending"})
            midpoint = self.control.handle("snapshot", {})
            self.assertTrue(midpoint["partial"])
            self.assertIn("changed during snapshot", midpoint["errors"]["lifecycle_consistency"])
            release_native.set()
            submitter.join(timeout=1)

    def test_direct_lifecycle_writes_invalidate_snapshot_consistency(self):
        original_sessions = self.control.sessions.fleet_records
        observed_at = int(time.time() * 1000)

        for operation, payload in (
            ("observe", {"source": "claude", "session_id": "direct-native",
                         "agent_id": "child", "kind": "start", "observed_at": observed_at}),
            ("session_observe", {"source": "claude", "session_id": "direct-session",
                                 "kind": "session_start", "observed_at": observed_at}),
        ):
            with self.subTest(operation=operation):
                injected = False

                def interleaved(limit, operation=operation, payload=payload):
                    nonlocal injected
                    result = original_sessions(limit)
                    if not injected:
                        injected = True
                        self.control.handle(operation, payload)
                    return result

                with patch.object(self.control.sessions, "fleet_records", side_effect=interleaved):
                    snapshot = self.control.handle("snapshot", {})

                self.assertTrue(snapshot["partial"])
                self.assertIn("changed during snapshot", snapshot["errors"]["lifecycle_consistency"])

    def test_task_finalization_and_attempt_history_commit_atomically(self):
        task_id = "task-atomic-finalize"
        self.seed_attempt_task(task_id, second_attempt=True)
        entered = threading.Event()
        release = threading.Event()
        original_save = self.control.store.save_attempt

        def paused_save(task, db=None):
            if task.get("attempt_number") == 2:
                entered.set()
                release.wait(1)
            return original_save(task, db)

        with patch.object(self.control.store, "save_attempt", side_effect=paused_save):
            writer = threading.Thread(target=self.runtime.finish, args=(task_id, "completed", None))
            writer.start()
            try:
                self.assertTrue(entered.wait(1))
                during = self.control.handle("status", {"task_id": task_id})
                self.assertEqual(during["status"], "running")
                self.assertEqual(
                    [attempt["attempt_number"] for attempt in during["attempt_history"]],
                    [1],
                )
            finally:
                release.set()
                writer.join(timeout=2)

        self.assertFalse(writer.is_alive())
        after = self.control.handle("status", {"task_id": task_id})
        self.assertEqual(after["status"], "awaiting_review")
        self.assertEqual(
            [attempt["attempt_number"] for attempt in after["attempt_history"]],
            [1, 2],
        )
        self.assertEqual(
            after["elapsed_ms"],
            sum(attempt["duration_ms"] for attempt in after["attempt_history"]),
        )

    def test_list_status_holds_one_snapshot_for_tasks_and_attempts(self):
        task_id = "task-list-snapshot"
        self.seed_attempt_task(task_id, second_attempt=False)
        task_read = threading.Event()
        release_reader = threading.Event()
        original_all = self.control.store.all
        rows = {}

        def paused_all(db=None):
            tasks = original_all(db)
            task_read.set()
            release_reader.wait(1)
            return tasks

        with patch.object(self.control.store, "all", side_effect=paused_all):
            reader = threading.Thread(
                target=lambda: rows.update(result=self.control.handle("status", {})),
            )
            reader.start()
            self.assertTrue(task_read.wait(1))

            def finalize_second(current):
                current.update(
                    status="failed",
                    attempts=2,
                    attempt_number=2,
                    duration_ms=20,
                    cost=20.0,
                    usage={"input_tokens": 20},
                    execution_stopped=True,
                    execution_outcome="failed",
                )

            writer = threading.Thread(
                target=self.control.store.update_with_attempt,
                args=(task_id, finalize_second),
            )
            writer.start()
            writer.join(timeout=1)
            self.assertFalse(writer.is_alive())
            release_reader.set()
            reader.join(timeout=2)

        self.assertFalse(reader.is_alive())
        during = next(row for row in rows["result"] if row["task_id"] == task_id)
        self.assertEqual(during["status"], "awaiting_review")
        self.assertEqual(during["attempts"], 1)
        self.assertEqual(during["cost"], 2.0)
        after = next(
            row for row in self.control.handle("status", {}) if row["task_id"] == task_id
        )
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["attempts"], 2)
        self.assertEqual(after["cost"], 22.0)

    def test_interrupted_capture_persists_attempt_with_task_state(self):
        task_id = "task-interrupted-attempt"
        task = self.seed_attempt_task(task_id, second_attempt=True)
        Path(task["invocation"]["parent_worktree"]).mkdir(exist_ok=True)
        self.control.store.update(task_id, lambda current: current.update(
            snapshot_commit="snapshot", worktree=str(self.repo),
        ))

        with patch.object(self.runtime, "snapshot_phase", side_effect=UncertainProcess("capture uncertain")):
            self.runtime.finish(task_id, "completed", None)

        result = self.control.handle("status", {"task_id": task_id})
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(result["execution_stopped"])
        self.assertEqual(result["attempt_history"][-1]["status"], "interrupted")
        self.assertEqual(result["attempt_history"][-1]["failure_reason"], "capture uncertain")

    def test_second_daemon_checks_lock_before_database_startup_work(self):
        directory = self.root / "blocked-state"
        directory.mkdir()
        database = directory / "router.sqlite3"
        import sqlite3
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE observations(task_id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")
            connection.execute("INSERT INTO observations VALUES('bad','S','not-json')")
        with (directory / "daemon.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "already running"):
                serve(self.config, os.environ.copy(), directory, self.root / "blocked.sock")

    def test_unsafe_state_directory_is_rejected_before_any_change(self):
        directory = self.repo / "router-state"
        directory.mkdir()
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "outside every configured"):
            serve(self.config, os.environ.copy(), directory, self.root / "unsafe.sock")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.assertFalse((directory / "daemon.lock").exists())

    def enable_codex(self, model="openai/gpt-5.3-codex"):
        self.config.codex_command = self.config.opencode_command
        self.config.models["test"] = model
        self.config.repositories[str(self.repo.resolve())]["codex_sandbox"] = "workspace-write"
        (self.repo / "opencode.json").unlink()

    def test_codex_resume_telemetry_and_integration_without_opencode_permissions(self):
        self.enable_codex()
        task_id = self.start("codex", executor="codex")
        first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(first["status"], "awaiting_review", first)
        self.assertEqual(first["child_session_id"], "12345678-1234-4321-8765-123456789abc")
        self.assertEqual(first["usage"]["input_tokens"], 100)
        self.assertEqual(first["usage"]["cached_input_tokens"], 40)
        self.assertTrue(first["usage_complete"])
        self.assertAlmostEqual(first["cost"], (60 * 1.75 + 40 * .175 + 20 * 14) / 1_000_000)
        self.assertEqual(first["cost_source"], "token_estimate")
        self.assertIn("excludes", first["cost_note"])
        self.assertEqual(first["model_source"], "configured")
        self.assertFalse((self.repo / "result.txt").exists())
        self.mutate("review", {"task_id": task_id, "request_id": "codex-reject", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Correct the result"})
        second = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(second["status"], "awaiting_review", second)
        self.assertEqual(second["usage"]["input_tokens"], 250)
        self.assertEqual(second["attempt_history"][1]["usage"]["input_tokens"], 150)
        self.assertAlmostEqual(second["cost"], first["cost"] * 2.5)
        commands = json.loads((Path(second["worktree"]) / "session.json").read_text())["commands"]
        self.assertNotIn("resume", commands[0])
        self.assertIn("resume", commands[1])
        self.assertIn(first["child_session_id"], commands[1])
        for command in commands:
            self.assertIn('approval_policy="never"', command)
            self.assertIn('sandbox_mode="workspace-write"', command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertIn("--ignore-rules", command)
        self.mutate("review", {"task_id": task_id, "request_id": "codex-accept", "expected_attempt": 2, "outcome": "accepted"})
        apply_delta(self.repo, Path(second["patch"]))
        self.assertEqual((self.repo / "result.txt").read_text(), "corrected\n")
        self.mutate("close", {"task_id": task_id, "request_id": "codex-close", "disposition": "integrated"})
        end = time.monotonic() + 5
        while time.monotonic() < end and self.control.handle("status", {"task_id": task_id})["cleanup_pending"]:
            time.sleep(.03)
        self.assertFalse(Path(second["worktree"]).exists())

    def test_codex_correction_requires_fresh_matching_thread_evidence(self):
        self.enable_codex()
        task_id = self.start("codex", executor="codex")
        first = self.wait(task_id, {"awaiting_review", "failed"})
        self.assertEqual(first["status"], "awaiting_review", first)
        self.mutate("review", {"task_id": task_id, "request_id": "no-thread", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "no thread"})
        second = self.wait(task_id, {"awaiting_review", "failed"})
        self.assertEqual(second["status"], "failed", second)

    def test_codex_missing_pricing_or_usage_stays_unknown(self):
        self.enable_codex("openai/unknown-model")
        task_id = self.start("full usage", executor="codex")
        task = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(task["status"], "awaiting_review", task)
        self.assertIsNone(task["cost"])
        self.assertTrue(task["usage_complete"])
        self.assertIn("price unavailable", task["cost_note"])
        self.config.models["test"] = "openai/gpt-5.3-codex"
        partial_id = self.start("partial usage", "partial", executor="codex")
        task = self.wait(partial_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(task["status"], "awaiting_review", task)
        self.assertIsNone(task["cost"])
        self.assertEqual(task["usage"]["input_tokens"], 100)
        self.assertNotIn("cached_input_tokens", task["usage"])
        self.assertFalse(task["usage_complete"])
        self.assertEqual(task["cost_source"], "unavailable")
        self.assertIn("tokens (partial):", table([task]))

    def test_codex_unapproved_sandbox_and_provider_never_launch(self):
        self.enable_codex()
        self.config.repositories[str(self.repo.resolve())].pop("codex_sandbox")
        task_id = self.start("must not launch", executor="codex")
        failed = self.wait(task_id, {"failed", "awaiting_review"})
        self.assertEqual(failed["status"], "failed", failed)
        self.assertNotIn("worktree", failed)
        self.assertEqual(failed["attempts"], 0)
        self.config.repositories[str(self.repo.resolve())]["codex_sandbox"] = "workspace-write"
        self.config.models["test"] = "other/model"
        other = self.start("must not launch", "bad-provider", executor="codex")
        failed = self.wait(other, {"failed", "awaiting_review"})
        self.assertEqual(failed["status"], "failed", failed)
        self.assertNotIn("worktree", failed)

    def test_codex_terminal_evidence_required_and_cancel_stops_descendants(self):
        self.enable_codex()
        task_id = self.start("no completion", executor="codex")
        failed = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(failed["status"], "failed", failed)
        self.assertIsNone(failed["cost"])
        error_id = self.start("final error", "codex-error", executor="codex")
        self.assertEqual(self.wait(error_id, {"failed", "awaiting_review"})["status"], "failed")
        hanging = self.start("hang", "codex-hang", executor="codex")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready = self.control.handle("status", {"task_id": hanging})
            pid_file = Path(ready["worktree"]) / "sleeper.pid" if ready.get("worktree") else None
            if ready.get("child_session_id") and pid_file and pid_file.exists():
                break
            time.sleep(.03)
        self.assertTrue(ready.get("child_session_id"))
        sleeper = int(pid_file.read_text())
        sleeper_record = {"pid": sleeper, "pgid": sleeper, "creation_identity": creation_identity(sleeper)}
        self.addCleanup(lambda: stop_owned(sleeper_record))
        self.assertEqual(os.getpgid(sleeper), sleeper)
        self.mutate("cancel", {"task_id": hanging, "request_id": "codex-cancel"})
        cancelled = self.wait(hanging, {"cancelled", "interrupted"})
        self.assertEqual(cancelled["status"], "cancelled", cancelled)
        self.assertTrue(cancelled["execution_stopped"])
        with self.assertRaises(ProcessLookupError):
            os.kill(sleeper, 0)

    def test_codex_forced_stop_cannot_claim_command_shutdown_or_be_closed(self):
        self.enable_codex()
        task_id = self.start("ignore interrupt hang", executor="codex")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = self.control.handle("status", {"task_id": task_id})
            if task.get("worktree") and (Path(task["worktree"]) / "interrupt-ready").exists():
                break
            time.sleep(.03)
        self.assertTrue((Path(task["worktree"]) / "interrupt-ready").exists())
        self.mutate("cancel", {"task_id": task_id, "request_id": "forced-cancel"})
        stopped = self.wait(task_id, {"cancelled", "interrupted"})
        self.assertEqual(stopped["status"], "interrupted", stopped)
        self.assertFalse(stopped["execution_stopped"])
        self.assertTrue(stopped["shutdown_unconfirmed"])
        self.mutate("cancel", {"task_id": task_id, "request_id": "cannot-confirm"})
        self.assertEqual(self.wait(task_id, {"cancelled", "interrupted"})["status"], "interrupted")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.mutate("close", {"task_id": task_id, "request_id": "unsafe-close", "disposition": "discarded"})
        self.runtime.stop()
        self.config.concurrency_cap = 1
        queued = self.start("queued", "after-recovery", executor="codex")
        before = self.control.store.history(task_id)
        directory = self.root / "state"
        with patch("router.service.state_dir", return_value=directory), \
                patch("router.service.sys.stdin.isatty", return_value=False), \
                self.assertRaisesRegex(ValueError, "interactive"):
            service.recover(task_id, "Inspected foreground commands")
        with patch("router.service.state_dir", return_value=directory), \
                patch("router.service.sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="wrong"):
            with self.assertRaisesRegex(ValueError, "No changes"):
                service.recover(task_id, "Inspected foreground commands")
            with (directory / "daemon.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, "Stop the daemon"):
                    service.recover(task_id, "Inspected foreground commands")
            with patch("router.service.owned_state", return_value="live"), self.assertRaisesRegex(ValueError, "stopped"):
                service.recover(task_id, "Inspected foreground commands")
            with patch("builtins.input", return_value=task_id):
                service.recover(task_id, "Inspected foreground commands")
        resolved = self.control.store.get(task_id)
        self.assertEqual(resolved["status"], "cancelled")
        self.assertTrue(resolved["execution_stopped"])
        self.assertTrue(resolved["shutdown_unconfirmed"])
        self.assertEqual(resolved["operator_recovery"]["reason"], "Inspected foreground commands")
        self.assertEqual(self.control.store.history(task_id), before)
        self.assertTrue(Path(resolved["worktree"]).exists())
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.assertEqual(self.wait(queued, {"awaiting_review", "failed"})["status"], "awaiting_review")

    def test_codex_restart_without_shutdown_evidence_keeps_reservation(self):
        self.enable_codex()
        task_id = self.start("finished", executor="codex")
        self.wait(task_id, {"awaiting_review"})
        self.runtime.stop()
        task = self.control.store.get(task_id)
        record = {**task["process"], "interrupt_first": True, "completion": str(self.root / "missing-completion")}
        self.assertEqual(owned_state(record), "stopped")
        self.control.store.update(task_id, lambda t: t.update(status="cancelling", execution_stopped=False,
            cancel_requested=True, process=record))
        self.config.concurrency_cap = 1
        queued = self.start("queued", "restart-queued", executor="codex")
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        interrupted = self.wait(task_id, {"interrupted"})
        self.assertTrue(interrupted["shutdown_unconfirmed"])
        self.assertFalse(interrupted["execution_stopped"])
        time.sleep(.15)
        self.assertEqual(self.control.store.get(queued)["status"], "created")

    def test_full_cycle_same_session_and_cleanup(self):
        (self.repo / "tracked.txt").write_text("dirty parent\n")
        task_id = self.start()
        first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(first["status"], "awaiting_review", first)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["cost"], 0.25)
        self.assertEqual(first["usage"]["cache"]["read"], 1)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "dirty parent\n")
        self.assertFalse((self.repo / "result.txt").exists())
        self.config.models["test"] = "provider/changed-registry-entry"
        self.mutate("review", {"task_id": task_id, "request_id": "reject", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Correct the result"})
        second = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(second["status"], "awaiting_review", second)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["cost"], .5)
        self.assertEqual(second["usage"]["cache"]["read"], 2)
        self.assertEqual(second["elapsed_ms"], sum(attempt["duration_ms"] for attempt in second["attempt_history"]))
        sessions = json.loads((Path(second["worktree"]) / "session.json").read_text())
        self.assertEqual(sessions["sessions"], [first["child_session_id"]] * 2)
        self.assertEqual(sessions["models"], ["provider/exact"] * 2)
        self.assertEqual(second["worktree"], first["worktree"])
        with self.assertRaisesRegex(ValueError, "ceiling"):
            self.mutate("review", {"task_id": task_id, "request_id": "over-ceiling", "expected_attempt": 2,
                                   "outcome": "rejected", "feedback": "One more attempt"})
        self.assertEqual(self.control.handle("status", {"task_id": task_id})["attempts"], 2)
        (self.repo / "later.txt").write_text("later edit\n")
        self.mutate("review", {"task_id": task_id, "request_id": "accept", "expected_attempt": 2, "outcome": "accepted"})
        apply_delta(self.repo, Path(second["patch"]))
        self.assertEqual((self.repo / "result.txt").read_text(), "corrected\n")
        self.assertEqual((self.repo / "later.txt").read_text(), "later edit\n")
        self.mutate("close", {"task_id": task_id, "request_id": "close", "disposition": "integrated"})
        end = time.monotonic() + 5
        while time.monotonic() < end:
            closed = self.control.handle("status", {"task_id": task_id})
            if not closed["cleanup_pending"]:
                break
            time.sleep(0.04)
        self.assertFalse(closed["cleanup_pending"], closed)
        self.assertFalse(Path(second["worktree"]).exists())
        self.assertEqual(closed["execution_outcome"], "completed")

    def test_timeout_preserves_work_and_salvage(self):
        self.config.attempt_timeout_seconds = 1.5
        task_id = self.start("silent hang")
        failed = self.wait(task_id, {"failed", "interrupted"})
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["failure_reason"], "router-timeout")
        self.assertEqual((Path(failed["worktree"]) / "result.txt").read_text(), "first\n")
        self.assertIn("final_tree", failed)
        self.assertNotIn("last_event_at", failed)
        accepted = self.mutate("review", {"task_id": task_id, "request_id": "salvage", "expected_attempt": 1, "outcome": "accepted"})
        self.assertEqual(accepted["execution_outcome"], "failed")
        self.assertEqual(accepted["attempts"], 1)
        self.enable_codex()
        codex_task = self.start("silent hang", "codex-timeout", executor="codex")
        stopped = self.wait(codex_task, {"failed", "interrupted"})
        self.assertEqual(stopped["status"], "failed", stopped)
        self.assertTrue(stopped["execution_stopped"])
        self.assertEqual(stopped["failure_reason"], "router-timeout")

    def test_gate_registration_failure_releases_no_command(self):
        marker = self.root / "must-not-exist"
        def refuse(record):
            self.assertEqual(owned_state(record), "live")
            self.assertFalse(marker.exists())
            raise ValueError("registration failed")
        with self.assertRaisesRegex(ValueError, "registration failed"):
            launch([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                   self.root, os.environ.copy(), self.root / "gate", refuse)
        self.assertFalse(marker.exists())

    def test_identity_mismatch_never_signals(self):
        records = []
        process, record = launch([sys.executable, "-c", "import time; time.sleep(60)"],
                                 self.root, os.environ.copy(), self.root / "group", records.append)
        self.addCleanup(lambda: stop_owned(record, process))
        with self.assertRaises(UncertainProcess):
            stop_owned({**record, "creation_identity": "another-process"}, process)
        self.assertIsNone(process.poll())

    def test_admission_before_preparation_and_twelve_task_monitoring(self):
        self.config.concurrency_cap = 3
        tasks = [self.start("hang", f"fanout-{i}") for i in range(5)]
        for task_id in tasks[:3]:
            self.wait(task_id, {"running"})
        for task_id in tasks[3:]:
            queued = self.control.handle("status", {"task_id": task_id})
            self.assertEqual(queued["status"], "created")
            self.assertNotIn("worktree", queued)
            self.mutate("cancel", {"task_id": task_id, "request_id": "cancel-" + task_id})
            self.mutate("close", {"task_id": task_id, "request_id": "close-" + task_id, "disposition": "discarded"})
        self.config.concurrency_cap = 12
        tasks = tasks[:3] + [self.start("hang", f"extra-{i}") for i in range(9)]
        for task_id in tasks:
            self.wait(task_id, {"running"})
        rows = self.control.handle("status", {})
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["status"] for row in rows}, {"running"})
        self.assertEqual(self.control.handle("status", {"filter": "needs_decision"}), [])
        self.assertEqual(len({self.control.store.get(task)["worktree"] for task in tasks}), 12)
        for task_id in tasks:
            self.mutate("cancel", {"task_id": task_id, "request_id": "stop-" + task_id})
        for task_id in tasks:
            stopped = self.wait(task_id, {"cancelled", "interrupted"})
            self.assertEqual(stopped["status"], "cancelled", stopped)

    def test_setup_creates_own_environment_and_runs_repository_tests(self):
        project_tests = self.repo / "tests"
        project_tests.mkdir()
        (project_tests / "test_project.py").write_text(
            "import sys, unittest\nfrom pathlib import Path\n"
            "class ProjectTest(unittest.TestCase):\n"
            " def test_result(self):\n"
            "  self.assertEqual(Path('result.txt').read_text(), 'first\\n')\n"
            "  Path('environment.txt').write_text(sys.prefix)\n")
        self.config.repositories[str(self.repo.resolve())]["setup_command"] = f"{sys.executable} -m venv .venv"
        task_id = self.start("real setup")
        result = self.wait(task_id, {"awaiting_review", "failed", "interrupted"}, seconds=40)
        self.assertEqual(result["status"], "awaiting_review", result)
        child = Path(result["worktree"])
        self.assertTrue((child / ".venv" / "bin" / "python").exists())
        self.assertFalse((self.repo / ".venv").exists())
        self.assertEqual(Path((child / "environment.txt").read_text()), child / ".venv")
        self.assertNotIn(".venv/", Path(result["patch"]).read_text())

    def test_cancel_during_setup_prevents_child_launch(self):
        self.config.repositories[str(self.repo.resolve())]["setup_command"] = "touch setup-generated.txt; sleep 60"
        task_id = self.start("must not launch")
        end = time.monotonic() + 10
        while time.monotonic() < end:
            task = self.control.handle("status", {"task_id": task_id})
            if task.get("phase") == "setup" and (Path(task["worktree"]) / "setup-generated.txt").exists():
                break
            time.sleep(0.03)
        self.assertEqual(task.get("phase"), "setup")
        self.mutate("cancel", {"task_id": task_id, "request_id": "cancel-setup"})
        stopped = self.wait(task_id, {"cancelled", "interrupted"})
        self.assertEqual(stopped["status"], "cancelled", stopped)
        self.assertEqual(stopped["attempts"], 0)
        self.assertFalse((Path(stopped["worktree"]) / "session.json").exists())
        self.assertIn("final_tree", stopped)

    def test_final_error_event_is_collected_after_process_exit(self):
        leaders = exiting_leaders(self)
        def slow_child_exit(command, cwd, environment, directory, register, **options):
            process, record = launch(command, cwd, environment, directory, register, **options)
            if directory.name != "child":
                return process, record
            leaders[record["pid"]] = ExitingLeader(process, record, seconds=1)
            return leaders[record["pid"]], record
        for slow_exit in (False, True):
            with self.subTest(slow_exit=slow_exit), (patch("router.daemon.launch", side_effect=slow_child_exit) if slow_exit else nullcontext()):
                task_id = self.start("final error", f"final-error-{slow_exit}")
                failed = self.wait(task_id, {"failed", "interrupted", "awaiting_review"})
                self.assertEqual(failed["status"], "failed", failed)
                self.assertEqual(failed["exit_code"], 0)
                self.assertTrue(failed["execution_error"])
                self.assertIn("final_tree", failed)
        self.assertEqual([leader.reapable_at is not None for leader in leaders.values()], [True])

    def test_stopping_an_exiting_leader_awaits_reaping(self):
        leaders = exiting_leaders(self)
        ready = "from pathlib import Path; Path('ready').touch(); "
        sleep = "import time; time.sleep(60)"
        on_interrupt = "import signal, sys; signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
        # Name, script, interrupt first, already exited before the stop, modeled exit seconds.
        rows = (("already exiting", ready, False, True, 1),
                ("exits on terminate", ready + sleep, False, False, 1),
                ("exits on interrupt", on_interrupt + ready + sleep, True, False, 3.5))
        for index, (name, script, interrupt_first, exited, seconds) in enumerate(rows):
            with self.subTest(name):
                cwd = self.root / f"exiting-{index}"
                cwd.mkdir()
                process, record = launch([sys.executable, "-c", script], cwd, os.environ.copy(),
                                         self.root / f"exiting-group-{index}", lambda record: None)
                self.addCleanup(lambda record=record, process=process: process.poll() is None and stop_owned(record, process))
                leader = leaders[record["pid"]] = ExitingLeader(process, record, seconds)
                record.update(interrupt_first=interrupt_first, executor="codex")
                marker = Path(record["completion"]) if exited else cwd / "ready"
                end = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < end:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                if exited:
                    self.assertTrue(leader.exiting())
                stop_owned(record, leader)
                self.assertIsNotNone(process.returncode)
                self.assertEqual(owned_state(record), "stopped")

    def test_uncertain_execution_reserves_admission_until_cancel(self):
        self.runtime.stop()
        self.config.concurrency_cap = 1
        uncertain_id = self.start("never dispatched", "uncertain")
        self.control.store.update(uncertain_id, lambda t: t.update(status="interrupted", execution_stopped=False))
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        queued_id = self.start("queued", "queued")
        time.sleep(0.2)
        queued = self.control.handle("status", {"task_id": queued_id})
        self.assertEqual(queued["status"], "created")
        self.assertNotIn("worktree", queued)
        self.assertEqual([row["task_id"] for row in self.control.handle("status", {"filter": "needs_decision"})], [uncertain_id])
        self.mutate("cancel", {"task_id": uncertain_id, "request_id": "resolve"})
        self.wait(uncertain_id, {"cancelled"})
        finished = self.wait(queued_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(finished["status"], "awaiting_review", finished)

    def test_close_replay_finishes_partial_cleanup_without_touching_other_worktree(self):
        task_id = self.start("cleanup")
        result = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(result["status"], "awaiting_review", result)
        other = self.root / "other-worktree"
        git(self.repo, "worktree", "add", "--force", str(other), result["branch"])
        close = {"task_id": task_id, "request_id": "discard", "disposition": "discarded"}
        self.mutate("close", close)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            partial = self.control.handle("status", {"task_id": task_id})
            if partial.get("cleanup_error"):
                break
            time.sleep(0.03)
        self.assertTrue(partial["cleanup_pending"])
        self.assertTrue(partial["cleanup_error"])
        self.assertTrue(other.exists())
        self.assertEqual(self.control.busy(), 0)
        rows = self.control.handle("status", {"filter": "needs_decision"})
        self.assertEqual([row["task_id"] for row in rows], [task_id])
        self.assertEqual(rows[0]["cleanup_error"], partial["cleanup_error"])
        self.assertIn("cleanup failed", table(rows))
        with patch("router.service.state_dir", return_value=self.root / "state"), patch("router.service.health", return_value=None):
            self.assertIsNone(service.require_idle())
        git(self.repo, "worktree", "remove", "--force", str(other))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def retry_cleanup(root, task):
            entered.set()
            release.wait(timeout=10)
            cleanup(root, task)
        with patch("router.daemon.cleanup", side_effect=retry_cleanup):
            self.control.handle("close", close)
            self.assertTrue(entered.wait(timeout=5))
            self.assertGreater(self.control.busy(), 0)
            with patch("router.service.state_dir", return_value=self.root / "state"), \
                    patch("router.service.health", return_value=None), self.assertRaisesRegex(ValueError, "active"):
                service.require_idle()
            release.set()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            closed = self.control.handle("status", {"task_id": task_id})
            if not closed["cleanup_pending"]:
                break
            time.sleep(0.03)
        self.assertFalse(closed["cleanup_pending"], closed)
        self.assertFalse(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/router/").strip())

    def test_restart_resumes_interrupted_cleanup_but_not_failed_cleanup(self):
        interrupted = self.start("cleanup", "restart-cleanup")
        failed = self.start("cleanup", "failed-cleanup")
        for task_id in (interrupted, failed):
            self.wait(task_id, {"awaiting_review"})
        self.runtime.stop()
        for task_id in (interrupted, failed):
            self.mutate("close", {"task_id": task_id, "request_id": "close-" + task_id, "disposition": "discarded"})
            self.control.store.update(task_id, lambda t: t.update(cleanup_requested=False))
        self.control.store.update(failed, lambda t: t.update(cleanup_error="previous failure"))
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.control.store.get(interrupted)["cleanup_pending"]:
            time.sleep(.03)
        self.assertFalse(self.control.store.get(interrupted)["cleanup_pending"])
        self.assertTrue(self.control.store.get(failed)["cleanup_pending"])
        self.assertEqual(self.control.store.get(failed)["cleanup_error"], "previous failure")
        self.assertTrue(Path(self.control.store.get(failed)["worktree"]).exists())
        self.assertEqual(self.control.busy(), 0)


if __name__ == '__main__':
    unittest.main()
