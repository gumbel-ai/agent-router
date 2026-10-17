import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router.daemon import Controller
from router.fleet import _validate_snapshot
from router.fleet_snapshot import compose
from router.managed import completion_directory, run
from router.processes import parent_and_identity


class ManagedRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-managed-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "state"
        self.control = Controller(settings(root / "repo"), {}, self.state)
        self.daemon_up = True

    def request(self, operation, payload, timeout):
        if not self.daemon_up:
            raise OSError("Router daemon is not running")
        return self.control.handle(operation, payload)

    def run_command(self, script, **options):
        with patch("router.managed.request", side_effect=self.request):
            return run([sys.executable, "-c", script], options.get("label"), options.get("session"),
                       options.get("agent"), self.state)

    def managed(self):
        records, _ = self.control.managed.records()
        return {record["label"]: record for record in records}

    def test_completion_evidence_for_exit_codes_signals_and_survivors(self):
        self.assertEqual(self.run_command("pass", label="ok", session="S", agent="A"), 0)
        self.assertEqual(self.run_command("raise SystemExit(3)", label="failed", session="S"), 3)
        self.assertEqual(self.run_command("import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                                          label="signaled"), 128 + signal.SIGTERM)
        # The descendant blocks on a FIFO until the test releases it: survival is not a timing guess.
        fifo = self.state.parent / "hold"
        os.mkfifo(fifo)
        self.addCleanup(lambda: open(fifo, "w").close())  # Release the descendant.
        survivor = ("import subprocess, sys; "
                    f"subprocess.Popen([sys.executable, '-c', {f'open({str(fifo)!r}).read()'!r}])")
        self.assertEqual(self.run_command(survivor, label="survivor"), 0)
        records = self.managed()
        self.assertEqual({label: (record["status"], record["attribution"]) for label, record in records.items()},
                         {"ok": ("exited", "exact"), "failed": ("failed", "session"),
                          "signaled": ("signaled", "host"), "survivor": ("exited", "host")})
        self.assertEqual(records["signaled"]["signal"], signal.SIGTERM)
        self.assertTrue(records["survivor"]["group_outlived"])
        self.assertFalse(records["ok"]["group_outlived"])
        self.assertGreater(records["ok"]["max_process_rss_bytes"], 1_000_000)
        self.assertEqual(records["ok"]["executable"], Path(sys.executable).name)
        # Argument text is never kept, only its count and hash.
        self.assertNotIn("SIGTERM", json.dumps(records))

    def test_evidence_survives_a_daemon_that_is_down_at_completion(self):
        def down_after_start(operation, payload, timeout):
            result = self.request(operation, payload, timeout)
            if operation == "managed_start":
                self.daemon_up = False
            return result

        with patch("router.managed.request", side_effect=down_after_start):
            self.assertEqual(run([sys.executable, "-c", "raise SystemExit(4)"], "offline", None, None,
                                 self.state), 4)
        self.assertEqual(len(list(completion_directory(self.state).glob("*.json"))), 1)
        self.assertEqual(self.managed()["offline"]["exit_code"], 4)  # Read back from the completion file.
        self.assertEqual(list(completion_directory(self.state).glob("*.json")), [])

    def test_a_vanished_wrapper_without_evidence_is_uncertain_not_finished(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        live = {"managed_id": "managed-live", "label": "live", "attribution": "host", "pid": os.getpid(),
                "pgid": os.getpgid(0), "wrapper_pid": os.getpid(), "argument_count": 0,
                "wrapper_identity": parent_and_identity(os.getpid())[1], "started_at": int(time.time() * 1000)}
        self.control.handle("managed_start", live)
        self.control.handle("managed_start", {**live, "managed_id": "managed-gone", "label": "gone",
                                              "wrapper_pid": dead.pid, "wrapper_identity": "gone"})
        records = self.managed()
        self.assertEqual((records["live"]["status"], records["gone"]["status"]), ("running", "unknown"))
        snapshot = _validate_snapshot(json.dumps(compose(self.control)).encode())
        # A command whose outcome is unknown may still be running: capacity cannot claim availability.
        self.assertEqual(snapshot["capacity"]["capacity_state"], "unknown")
        self.assertTrue(snapshot["partial"])
        self.assertEqual({record["managed_id"] for record in snapshot["managed_processes"]},
                         {"managed-live", "managed-gone"})
        self.assertFalse({"wrapper_pid", "wrapper_identity"} & snapshot["managed_processes"][0].keys())
        self.assertIsNone(self.control.managed.records()[1])  # Unknown outcome: no exact active count.
        # Late evidence for the vanished wrapper still settles it.
        self.control.handle("managed_finish", {"managed_id": "managed-gone", "finished_at": int(time.time() * 1000),
                                               "elapsed_ms": 5,
                                               "exit_code": 0, "max_process_rss_bytes": 1,
                                               "group_outlived": False})
        self.assertEqual(self.managed()["gone"]["status"], "exited")
        self.assertIsNone(self.managed()["gone"]["visibility_reason"])
        self.assertEqual(self.control.managed.records()[1], 1)  # Settled: only the live command counts.

    def test_malformed_evidence_is_refused_and_completion_is_recorded_once(self):
        for bad in ({"managed_id": "managed-x"}, {**self.start_payload("managed-x"), "pid": True},
                    {**self.start_payload("managed-x"), "pid": 0}, {**self.start_payload("managed-x"), "pid": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.control.handle("managed_start", bad)
        finish = {"managed_id": "managed-y", "finished_at": int(time.time() * 1000), "elapsed_ms": 5, "exit_code": 2,
                  "max_process_rss_bytes": 1, "group_outlived": 1}
        for outcome in ({"exit_code": None}, {"signal": 9}):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                self.control.handle("managed_finish", {**finish, "group_outlived": False, **outcome,
                                                       "start": self.start_payload("managed-y")})
        with self.assertRaises(ValueError):
            self.control.handle("managed_finish", {**finish, "start": self.start_payload("managed-y")})
        # A start that never reached the daemon is recovered from the finish evidence.
        self.control.handle("managed_finish", {**finish, "group_outlived": False,
                                               "start": self.start_payload("managed-y")})
        self.control.handle("managed_finish", {**finish, "exit_code": 0, "group_outlived": False})
        self.assertEqual(self.managed()["y"]["exit_code"], 2)

    def start_payload(self, managed_id):
        return {"managed_id": managed_id, "label": managed_id.removeprefix("managed-"), "attribution": "host",
                "pid": 4242, "pgid": 4242, "wrapper_pid": 4242, "argument_count": 0, "started_at": 1}

    def test_interrupt_during_launch_reaches_the_command_and_handlers_are_restored(self):
        before = signal.getsignal(signal.SIGTERM)
        original_popen = subprocess.Popen

        def interrupted_launch(argv, **options):
            os.kill(os.getpid(), signal.SIGTERM)  # Arrives before the command exists.
            return original_popen(argv, **options)

        with patch("router.managed.subprocess.Popen", side_effect=interrupted_launch):
            code = self.run_command("import time; time.sleep(30)", label="interrupted")
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_one_bad_process_row_is_dropped_not_the_host(self):
        self.run_command("pass", label="good")
        local = compose(self.control)
        local["managed_processes"].append({**local["managed_processes"][0], "elapsed_ms": -2000})
        local["inferred_processes"] = [{"host_id": local["host"]["host_id"], "pid": 0}, None]
        projected = _validate_snapshot(json.dumps(local).encode())
        self.assertEqual(len(projected["managed_processes"]), 1)
        self.assertEqual(projected["inferred_processes"], [])
        self.assertIn("1 invalid record(s) omitted", projected["errors"]["managed_processes"])

    def test_an_interrupt_while_saving_evidence_does_not_lose_it(self):
        def interrupted(operation, payload, timeout):
            if operation == "managed_finish":
                os.kill(os.getpid(), signal.SIGTERM)  # Arrives after the command was reaped.
            return self.request(operation, payload, timeout)

        with patch("router.managed.request", side_effect=interrupted):
            code = run([sys.executable, "-c", "raise SystemExit(6)"], "saving", None, None, self.state)
        self.assertEqual((code, self.managed()["saving"]["exit_code"]), (6, 6))

    def test_completion_files_are_kept_until_imported_and_invalid_ones_set_aside(self):
        directory = completion_directory(self.state)
        directory.mkdir(parents=True)
        finish = {"managed_id": "managed-z", "finished_at": int(time.time() * 1000), "elapsed_ms": 1,
                  "exit_code": 0, "max_process_rss_bytes": 1, "group_outlived": False,
                  "start": self.start_payload("managed-z")}
        good, broken, outcome_less = (directory / f"managed-{name}.json" for name in ("z", "broken", "none"))
        good.write_text(json.dumps(finish))
        broken.write_text("{not json")
        outcome_less.write_text(json.dumps({**finish, "managed_id": "managed-none", "exit_code": None}))
        with patch("pathlib.Path.read_text", side_effect=OSError("busy")):
            self.control.managed.reconcile()
        self.assertTrue(good.exists())  # Unreadable for now: retried, not deleted.
        self.assertEqual(self.managed()["z"]["status"], "exited")
        self.assertFalse(good.exists())
        self.assertEqual(sorted(path.name for path in directory.iterdir()),
                         ["managed-broken.invalid", "managed-none.invalid"])

    def test_finished_records_are_pruned_even_when_nothing_is_running(self):
        old = int(time.time() * 1000) - 8 * 86_400_000
        self.control.handle("managed_finish", {"managed_id": "managed-old", "finished_at": old, "elapsed_ms": 1,
                                               "exit_code": 0, "max_process_rss_bytes": 1, "group_outlived": False,
                                               "start": self.start_payload("managed-old")})
        self.control.managed.records()
        with self.control.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0], 0)

    def test_a_start_the_daemon_missed_is_recovered_while_the_command_runs(self):
        fifo = self.state.parent / "running"
        os.mkfifo(fifo)
        seen = []

        def daemon_returns_then_release():
            deadline = time.monotonic() + 10
            while not list(completion_directory(self.state).glob("start-*.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.daemon_up = True  # The daemon is back while the command still runs.
            records, active = self.control.managed.records()
            seen.append(([record["label"] for record in records], active))
            with open(fifo, "w"):
                pass  # Let the command finish.

        self.daemon_up = False
        helper = threading.Thread(target=daemon_returns_then_release)
        helper.start()
        with patch("router.managed.request", side_effect=self.request):
            run([sys.executable, "-c", f"open({str(fifo)!r}).read()"], "missed", None, None, self.state)
        helper.join(10)
        self.assertEqual(seen, [(["missed"], 1)])
        self.assertEqual(self.managed()["missed"]["status"], "exited")

    def test_identities_are_refused_not_rewritten(self):
        for session, agent in (("a\nb", None), ("s" * 241, None), (" padded", None), (None, "agent-only")):
            with self.subTest(session=session, agent=agent), \
                    patch("router.managed.subprocess.Popen") as launch, self.assertRaises(ValueError):
                run(["true"], None, session, agent, self.state)
            launch.assert_not_called()  # Refused before anything was launched.
        with patch("router.managed.os.getcwd", side_effect=FileNotFoundError("removed")), \
                patch("router.managed.subprocess.Popen") as launch, self.assertRaises(FileNotFoundError):
            run(["true"], None, None, None, self.state)
        launch.assert_not_called()  # Metadata failures happen before launch, never after.
        for bad in ({"session_id": "s" * 241, "attribution": "session"},
                    {"attribution": "exact"}, {"session_id": "S", "attribution": "host"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.control.handle("managed_start", {**self.start_payload("managed-bad"), **bad})

    def test_labels_follow_fleet_text_sharing(self):
        self.run_command("pass", label="rotate key XYZ")
        self.control.settings = type(self.control.settings)(AGENT_ROUTER_FLEET_SHARE_TEXT="false")
        [record] = compose(self.control)["managed_processes"]
        self.assertIsNone(record["label"])

    def test_cli_requires_a_command(self):
        with self.assertRaises(ValueError):
            run([], None, None, None, self.state)


if __name__ == "__main__":
    unittest.main()
