import io
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router.cli import main
from router.client import UnknownTask
from router.client import request as router_request
from router.daemon import Controller, Handler, Server
from router.wait_tasks import wait_tasks

UNREACHABLE = object()


class FakeRouter:
    """Serves scripted task states, one script step per poll round; records every request."""

    def __init__(self, rounds: list[dict], latency: float = 0):
        self.rounds = rounds
        self.round = 0
        self.now = 0.0
        self.latency = latency
        self.requests = []
        self.timeouts = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.round = min(self.round + 1, len(self.rounds) - 1)

    def request(self, operation, payload, *, timeout, deadline):
        self.requests.append((operation, payload))
        self.timeouts.append(timeout)
        if self.latency > timeout:
            self.now += timeout
            raise TimeoutError("Router request deadline expired")
        self.now += self.latency
        state = self.rounds[self.round]
        if callable(state.get(payload["task_id"]) if isinstance(state, dict) else None):
            return state[payload["task_id"]]()
        if state is UNREACHABLE or state.get(payload["task_id"]) is UNREACHABLE:
            raise ValueError("Agent Router is not reachable.") from ConnectionRefusedError()
        task = state.get(payload["task_id"])
        if task is None:
            raise UnknownTask("Unknown task_id")
        return {"task_id": payload["task_id"], "attempts": 1, "cleanup_error": None, **task}

    def wait(self, task_ids, mode, timeout=10):
        return wait_tasks(task_ids, mode, timeout, request=self.request, sleep=self.sleep, clock=self.clock)


def statuses(result):
    return {task["task_id"]: (task["status"], task["ready"]) for task in result["tasks"]}


class WaitTasksTests(unittest.TestCase):
    def test_any_returns_on_first_ready_task_with_every_current_state(self):
        router = FakeRouter([
            {"task-a": {"status": "running"}, "task-b": {"status": "running"}},
            {"task-a": {"status": "running"}, "task-b": {"status": "awaiting_review"}},
        ])
        result = router.wait(["task-a", "task-b"], "any")
        self.assertEqual((result["condition_met"], result["timed_out"]), (True, False))
        self.assertEqual(statuses(result), {"task-a": ("running", False), "task-b": ("awaiting_review", True)})

    def test_all_waits_for_every_task_across_mixed_terminal_states(self):
        router = FakeRouter([
            {"task-a": {"status": "failed", "failure_reason": "tests failed"}, "task-b": {"status": "running"},
             "task-c": {"status": "cancelling"}},
            {"task-a": {"status": "failed", "failure_reason": "tests failed"}, "task-b": {"status": "interrupted"},
             "task-c": {"status": "cancelled"}},
        ])
        result = router.wait(["task-a", "task-b", "task-c"], "all")
        self.assertTrue(result["condition_met"])
        self.assertEqual(statuses(result), {"task-a": ("failed", True), "task-b": ("interrupted", True),
                                            "task-c": ("cancelled", True)})
        self.assertEqual(result["tasks"][0]["failure_reason"], "tests failed")
        self.assertEqual(router.now, 1)

    def test_retrying_task_is_not_ready_until_the_next_review(self):
        router = FakeRouter([
            {"task-a": {"status": "retrying", "attempts": 1}},
            {"task-a": {"status": "running", "attempts": 2}},
            {"task-a": {"status": "awaiting_review", "attempts": 2}},
        ])
        result = router.wait(["task-a"], "any")
        self.assertEqual(result["tasks"], [{"task_id": "task-a", "status": "awaiting_review", "attempts": 2,
                                            "ready": True}])
        self.assertEqual(router.now, 2)

    def test_missing_task_is_reported_and_ready(self):
        router = FakeRouter([{"task-a": {"status": "running"}}])
        result = router.wait(["task-a", "task-gone"], "any")
        self.assertTrue(result["condition_met"])
        self.assertEqual(result["tasks"][1], {"task_id": "task-gone", "status": "missing", "ready": True})

    def test_cleanup_error_makes_the_task_ready(self):
        router = FakeRouter([{"task-a": {"status": "running", "cleanup_error": "worktree busy"}}])
        result = router.wait(["task-a"], "all")
        self.assertTrue(result["condition_met"])
        self.assertEqual(result["tasks"][0]["cleanup_error"], "worktree busy")

    def test_timeout_returns_latest_state_of_every_task(self):
        router = FakeRouter([{"task-a": {"status": "running"}, "task-b": {"status": "awaiting_review"}}])
        result = router.wait(["task-a", "task-b"], "all", timeout=3)
        self.assertEqual((result["condition_met"], result["timed_out"], result["daemon_unavailable"]),
                         (False, True, False))
        self.assertEqual(statuses(result), {"task-a": ("running", False), "task-b": ("awaiting_review", True)})

    def test_daemon_restart_keeps_waiting_then_reports_the_new_state(self):
        router = FakeRouter([
            {"task-a": {"status": "running"}},
            UNREACHABLE,
            UNREACHABLE,
            {"task-a": {"status": "interrupted"}},
        ])
        result = router.wait(["task-a"], "any")
        self.assertTrue(result["condition_met"])
        self.assertEqual(statuses(result), {"task-a": ("interrupted", True)})

    def test_daemon_unavailable_at_deadline_is_distinguished_and_keeps_last_known_state(self):
        router = FakeRouter([{"task-a": {"status": "running"}}, UNREACHABLE])
        result = router.wait(["task-a"], "any", timeout=3)
        self.assertEqual((result["timed_out"], result["daemon_unavailable"]), (True, True))
        self.assertEqual(statuses(result), {"task-a": ("running", False)})

    def test_never_reached_daemon_reports_unknown_states(self):
        router = FakeRouter([UNREACHABLE])
        result = router.wait(["task-a"], "any", timeout=2)
        self.assertEqual(result["tasks"], [{"task_id": "task-a", "status": "unknown", "ready": False}])
        self.assertTrue(result["daemon_unavailable"])

    def test_only_reads_status(self):
        router = FakeRouter([{"task-a": {"status": "running"}}, {"task-a": {"status": "accepted"}}])
        router.wait(["task-a", "task-a"], "all")
        self.assertEqual({operation for operation, _ in router.requests}, {"status"})
        self.assertEqual(len(router.requests), 2)  # Duplicate IDs are read once per round.

    def test_fractional_deadline_waits_until_it_expires(self):
        router = FakeRouter([{"task-a": {"status": "running"}}])
        result = router.wait(["task-a"], "any", timeout=.5)
        self.assertTrue(result["timed_out"])
        self.assertEqual(router.now, .5)
        router = FakeRouter([{"task-a": {"status": "running"}}, {"task-a": {"status": "awaiting_review"}}])
        result = router.wait(["task-a"], "any", timeout=1.5)
        self.assertTrue(result["condition_met"])
        self.assertEqual(router.now, 1)

    def test_request_latency_never_extends_the_deadline(self):
        router = FakeRouter([{f"task-{index}": {"status": "running"} for index in range(5)}], latency=.25)
        result = router.wait([f"task-{index}" for index in range(5)], "any", timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertLessEqual(router.now, 1)
        self.assertTrue(all(0 < timeout <= 2 for timeout in router.timeouts))

    def test_failed_read_keeps_states_already_read_in_the_same_round(self):
        router = FakeRouter([{"task-a": {"status": "running"}, "task-b": {"status": "running"}},
                             {"task-a": {"status": "awaiting_review"}, "task-b": UNREACHABLE}])
        result = router.wait(["task-a", "task-b"], "any")
        self.assertTrue(result["condition_met"])
        self.assertEqual(statuses(result), {"task-a": ("awaiting_review", True), "task-b": ("running", False)})

    def test_a_failing_read_does_not_hide_a_ready_task_after_it(self):
        router = FakeRouter([{"task-a": UNREACHABLE, "task-b": {"status": "awaiting_review"}}])
        result = router.wait(["task-a", "task-b"], "any")
        self.assertTrue(result["condition_met"])
        self.assertEqual(statuses(result), {"task-a": ("unknown", False), "task-b": ("awaiting_review", True)})
        self.assertEqual(router.now, 0)

    def test_recovered_partial_round_at_deadline_is_not_reported_unavailable(self):
        router = FakeRouter([UNREACHABLE, {"task-a": {"status": "running"}, "task-b": {"status": "running"}}],
                            latency=.25)
        # Round 1 fails both reads by 0.5 s; round 2 reads task-a by 1.75 s, leaving no time for task-b.
        result = router.wait(["task-a", "task-b"], "all", timeout=1.75)
        self.assertEqual((result["timed_out"], result["daemon_unavailable"]), (True, False))
        self.assertEqual(statuses(result)["task-a"], ("running", False))

    def test_malformed_status_responses_are_failed_reads_not_readiness(self):
        for response in (None, {}, {"task_id": "task-a", "status": "bogus"}, {"task_id": "task-other",
                                                                             "status": "failed"}, ["task-a"]):
            with self.subTest(response=response):
                router = FakeRouter([{"task-a": lambda response=response: response}])
                result = router.wait(["task-a"], "any", timeout=2)
                self.assertEqual((result["condition_met"], result["daemon_unavailable"]), (False, True))
                self.assertEqual(statuses(result), {"task-a": ("unknown", False)})

    def test_read_cut_short_by_the_wait_deadline_is_not_router_unavailability(self):
        router = FakeRouter([{"task-a": {"status": "running"}}], latency=.25)
        # The first read ends at 0.25 s; after sleeping to 1.25 s the next read has only 0.125 s left.
        result = router.wait(["task-a"], "any", timeout=1.375)
        self.assertEqual((result["timed_out"], result["daemon_unavailable"]), (True, False))
        self.assertEqual(statuses(result), {"task-a": ("running", False)})

    def test_rejects_invalid_arguments(self):
        router = FakeRouter([{}])
        for task_ids, mode, timeout in ((["task-a"], "any", 0), (["task-a"], "any", 3601),
                                        (["native-x"], "any", 1), (["task-a"], "some", 1), ([], "any", 1),
                                        ([" task-a "], "any", 1), (["task-\x00"], "any", 1),
                                        (["t" * 241], "any", 1)):
            with self.subTest(task_ids=task_ids, mode=mode, timeout=timeout), self.assertRaises(ValueError):
                router.wait(task_ids, mode, timeout)
        self.assertEqual(router.requests, [])

    def test_cli_usage_errors_exit_2_without_reading(self):
        with patch("router.wait_tasks.router_request") as request, patch("sys.argv", [
                "router", "wait-tasks", " task-a ", "--any", "--timeout", "5"]), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_:
            main()
        self.assertEqual(exit_.exception.code, 2)
        request.assert_not_called()


class WaitTasksWireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-wait-tasks-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "example-project"
        self.project.mkdir()
        self.router = Controller(settings(self.project), {}, self.root / "state")

    @contextmanager
    def wire(self):
        endpoint = self.root / "router.sock"
        with Server(str(endpoint), Handler) as server:
            server.control = self.router
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                yield endpoint
            finally:
                server.shutdown()
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive(), "Fixture server did not stop")

    def test_cli_reads_real_daemon_and_reports_unknown_task_by_code(self):
        task = {"task_id": "task-review", "session_id": "parent", "status": "awaiting_review", "attempts": 1,
                "invocation": {"task": "fixture", "parent_worktree": str(self.project)}}
        with self.router.store.transaction() as db:
            self.router.store.put(db, task)
        with self.wire() as endpoint:
            with self.assertRaises(UnknownTask):
                router_request("status", {"task_id": "task-absent"}, endpoint)
            output = io.StringIO()
            argv = ["router", "wait-tasks", "task-review", "task-absent", "--all", "--timeout", "5"]
            with patch("router.wait_tasks.router_request",
                       lambda *args, **kwargs: router_request(*args, endpoint, **kwargs)), \
                    patch("sys.argv", argv), redirect_stdout(output):
                main()
        result = json.loads(output.getvalue())
        self.assertEqual((result["mode"], result["condition_met"], result["timed_out"]), ("all", True, False))
        self.assertEqual(statuses(result), {"task-review": ("awaiting_review", True), "task-absent": ("missing", True)})
        with self.router.store.transaction() as db:
            self.assertEqual(self.router.store.get("task-review", db)["status"], "awaiting_review")


if __name__ == "__main__":
    unittest.main()
