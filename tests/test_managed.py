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
from router.managed import ManagedProcesses, completion_directory, run
from router.paths import PROJECT_MARKER, PROJECT_MARKER_CONTENT
from router.processes import parent_and_identity


class ManagedRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-managed-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "state"
        self.control = Controller(settings(root / "repo"), {}, self.state)
        # Router records only commands run inside an observed project.
        project = root / "project"
        project.mkdir()
        (project / PROJECT_MARKER).write_text(json.dumps(PROJECT_MARKER_CONTENT))
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(project)
        self.daemon_up = True

    def request(self, operation, payload, timeout, **options):
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
        outcomes = []
        for _ in range(4):
            event = self.next_completion()
            outcomes.append(event["status"])
            self.ack_completion(event)
        self.assertEqual(outcomes, ["completed", "failed", "failed", "uncertain"])

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

    def test_lost_start_and_finish_replies_replay_evidence_without_duplicate_events(self):
        def lose_reply(operation, payload, timeout):
            self.request(operation, payload, timeout)
            raise OSError("reply lost after commit")

        with patch("router.managed.request", side_effect=lose_reply):
            self.assertEqual(run([sys.executable, "-c", "pass"], "batch-job", None, None, self.state), 0)
        event = self.next_completion()
        self.assertEqual(event["status"], "completed")
        self.assertEqual(len(self.managed()), 1)
        self.ack_completion(event)
        self.assertEqual(self.next_completion()["status"], "pending")
        self.assertEqual(list(completion_directory(self.state).glob("*.json")), [])

    def test_detaching_launcher_completes_while_its_detached_job_still_runs(self):
        release, ready = self.state.parent / "release-job", self.state.parent / "job-ready"
        self.addCleanup(release.touch)
        job = ("import os,pathlib,time; "
               f"ready=pathlib.Path({str(ready)!r}); temporary=ready.with_suffix('.tmp'); "
               "temporary.write_text(str(os.getpid())); temporary.replace(ready); "
               f"release=pathlib.Path({str(release)!r}); "
               "exec('while not release.exists() and release.parent.exists(): time.sleep(.02)')")
        launcher = ("import pathlib,subprocess,sys,time; "
                    f"subprocess.Popen([sys.executable,'-c',{job!r}], start_new_session=True, "
                    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                    f"ready=pathlib.Path({str(ready)!r}); "
                    "end=time.monotonic()+15; "
                    "exec('while not ready.exists() and time.monotonic()<end: time.sleep(.02)'); "
                    "assert ready.exists()")
        self.assertEqual(self.run_command(launcher, label="batch-job"), 0)
        event = self.next_completion()
        self.assertEqual(event["status"], "completed")
        self.assertFalse(event["event"]["group_outlived"])
        self.assertFalse(release.exists())
        os.kill(int(ready.read_text()), 0)  # The detached job is still alive after the launcher completes.

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
        (directory / "start-managed-odd.json").write_text(json.dumps({**self.start_payload("managed-odd"),
                                                                       "attribution": []}))
        with patch("pathlib.Path.read_text", side_effect=OSError("busy")):
            self.control.managed.reconcile()
        self.assertTrue(good.exists())  # Unreadable for now: retried, not deleted.
        self.assertEqual(self.managed()["z"]["status"], "exited")
        self.assertFalse(good.exists())
        self.assertEqual(sorted(path.name for path in directory.iterdir()),
                         ["managed-broken.invalid", "managed-none.invalid", "start-managed-odd.invalid"])

    def test_finished_records_are_pruned_even_when_nothing_is_running(self):
        old = int(time.time() * 1000) - 8 * 86_400_000
        self.control.handle("managed_finish", {"managed_id": "managed-old", "finished_at": old, "elapsed_ms": 1,
                                               "exit_code": 0, "max_process_rss_bytes": 1, "group_outlived": False,
                                               "start": self.start_payload("managed-old")})
        self.control.managed.records()
        with self.control.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0], 0)

    def next_completion(self, consumer="worker-a"):
        return self.control.handle("completion_next", {"consumer": consumer})

    def ack_completion(self, event, consumer="worker-a"):
        return self.control.handle("completion_ack", {"consumer": consumer, "event_id": event["event"]["event_id"]})

    def test_completion_backlog_replays_until_acknowledged_across_restart(self):
        self.run_command("pass", label="same-label")
        self.run_command("raise SystemExit(2)", label="same-label")
        first = self.next_completion()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(self.next_completion(), first)  # A lost reply or interrupted consumer changes nothing.
        self.control = Controller(settings(self.state.parent / "repo"), {}, self.state)
        self.assertEqual(self.next_completion(), first)
        self.assertEqual(self.next_completion("worker-b")["event"], first["event"])
        self.ack_completion(first)
        self.ack_completion(first)  # Including after restarting the consumer.
        second = self.next_completion()
        self.assertEqual(second["status"], "failed")
        self.assertNotEqual(second["event"]["event_id"], first["event"]["event_id"])
        self.assertNotEqual(second["event"]["managed_id"], first["event"]["managed_id"])
        self.assertEqual(self.next_completion("worker-b")["event"], first["event"])
        self.ack_completion(second)
        self.assertEqual(self.next_completion()["status"], "pending")

    def test_subscription_protects_pending_events_from_retention(self):
        self.assertEqual(self.next_completion()["status"], "pending")
        self.run_command("pass", label="retained")
        event = self.next_completion()
        self.next_completion("worker-b")
        from router.managed import PRUNE_INTERVAL_MS, RETENTION_MS
        future = int(time.time() * 1000) + RETENTION_MS + 1000
        clock = [future]
        with patch("router.managed._now", side_effect=lambda: clock[0]):
            self.control.managed.reconcile()
            self.assertEqual(self.next_completion(), event)
            self.ack_completion(event)
            clock[0] += PRUNE_INTERVAL_MS + 1
            self.control.managed.reconcile()
            self.assertEqual(self.next_completion("worker-b")["event"], event["event"])
            self.ack_completion(event, "worker-b")
            clock[0] += PRUNE_INTERVAL_MS + 1
            self.control.managed.reconcile()
        with self.control.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM completion_events WHERE data IS NOT NULL").fetchone()[0], 0)
        self.ack_completion(event)  # Idempotent even after event retention expires.
        # Old producer evidence cannot change identity or resurrect expired delivery.
        self.control.handle("managed_finish", {"managed_id": event["event"]["managed_id"],
            "finished_at": event["event"]["finished_at"], "elapsed_ms": 1, "exit_code": 0,
            "max_process_rss_bytes": 1, "group_outlived": False,
            "start": self.start_payload(event["event"]["managed_id"])})
        self.assertEqual(self.next_completion()["status"], "pending")

    def test_uncertain_outcome_and_later_evidence_are_distinct_events(self):
        from router.managed import RETENTION_MS
        self.control.handle("managed_start", self.start_payload("managed-late"))
        first = self.next_completion()
        self.assertEqual(first["status"], "uncertain")
        self.control.handle("managed_finish", {"managed_id": "managed-late", "finished_at": int(time.time() * 1000) - RETENTION_MS - 1,
                                               "elapsed_ms": 1, "exit_code": 0, "max_process_rss_bytes": 1,
                                               "group_outlived": False})
        self.assertEqual(self.next_completion(), first)  # Immutable uncertainty evidence.
        self.ack_completion(first)
        resolved = self.next_completion()
        self.assertEqual(resolved["status"], "completed")
        self.assertNotEqual(resolved["event"]["event_id"], first["event"]["event_id"])
        self.ack_completion(resolved)
        self.control.managed._pruned_at = 0
        self.control.managed.reconcile()
        self.control = Controller(settings(self.state.parent / "repo"), {}, self.state)
        self.assertEqual(self.next_completion()["status"], "pending")

    def test_ack_refuses_unknown_consumers_and_skipping_pending_events(self):
        self.next_completion()
        self.run_command("pass")
        first = self.next_completion()
        self.run_command("pass")
        later = self.control.handle("completion_next", {"consumer": "worker-b"})
        self.ack_completion(later, "worker-b")
        second_id = self.next_completion("worker-b")["event"]["event_id"]
        for consumer, event_id in (("missing", first["event"]["event_id"]),
                                   ("worker-a", second_id), ("worker-a", "completion-999999"),
                                   ("worker-a", "invalid")):
            with self.subTest(consumer=consumer, event_id=event_id), self.assertRaises(ValueError):
                self.control.handle("completion_ack", {"consumer": consumer, "event_id": event_id})
        self.assertEqual(self.next_completion(), first)

    def test_blocking_wait_deadline_and_unavailable_transport_are_distinct(self):
        from types import SimpleNamespace

        from router.managed import wait
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        timer = SimpleNamespace(time=time.time, monotonic=lambda: clock[0], sleep=advance)
        with patch("router.managed.time", timer), patch("router.managed.request", side_effect=self.request):
            self.assertEqual(wait("worker-a", 1)["status"], "deadline")
            self.assertEqual(clock[0], 1)
            # Inject real evidence exactly between the wait's two short requests.
            def finish_during_wait(seconds):
                advance(seconds)
                self.control.handle("managed_finish", {"managed_id": "managed-during", "finished_at": 1,
                    "elapsed_ms": 1, "exit_code": 0, "max_process_rss_bytes": 1, "group_outlived": False,
                    "start": self.start_payload("managed-during")})
            timer.sleep = finish_during_wait
            event = wait("worker-a", 1)
            self.assertEqual(event["status"], "completed")
            self.assertEqual(wait("worker-a", 1), event)
            self.daemon_up = False
            self.assertEqual(wait("worker-a", 1)["status"], "unavailable")
        for timeout in (0, -1, float("nan"), float("inf"), 3601, True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                wait("worker-a", timeout)
        envelope = {"status": "completed", "consumer": "worker-a", "host_id": "serving-host"}
        nested = {"event_id": "completion-1", "managed_id": "managed-good", "host_id": "producing-host"}
        broken_events = [None, {**nested, "event_id": "bad\n"}, {**nested, "managed_id": "bad\n"},
                         {**nested, "host_id": None}]
        for broken in (None, [], {"status": "completed"}, {"status": []},
                       *({**envelope, "event": event} for event in broken_events)):
            with self.subTest(broken=broken), patch("router.managed.request", return_value=broken):
                self.assertEqual(wait("worker-a", 5)["status"], "unavailable")
        for timeout, expected in ((1, "deadline"), (30, "unavailable")):
            def deadline_during_request(*args, **kwargs):
                clock[0] += 1
                raise TimeoutError("No complete reply before the transport deadline")
            with self.subTest(timeout=timeout), patch("router.managed.time", timer), \
                    patch("router.managed.request", side_effect=deadline_during_request):
                self.assertEqual(wait("worker-a", timeout)["status"], expected)

    def test_upgrade_backfills_retained_terminal_records_without_replaying_acks(self):
        self.run_command("pass")
        # Simulate the previous schema: managed records exist but there is no delivery ledger.
        with self.control.store.transaction() as db:
            db.execute("DROP TABLE completion_events")
            db.execute("DROP TABLE completion_consumers")
        self.control = Controller(settings(self.state.parent / "repo"), {}, self.state)
        event = self.next_completion()
        self.assertEqual(event["status"], "completed")
        self.ack_completion(event)
        self.control = Controller(settings(self.state.parent / "repo"), {}, self.state)
        self.assertEqual(self.next_completion()["status"], "pending")

    def test_invalid_legacy_identity_does_not_block_delivery_and_original_host_is_preserved(self):
        from router.managed import wait
        for mode in ("terminal", "terminal_missing_start", "terminal_missing_times", "terminal_survivor",
                     "terminal_unknown_group", "finish", "spool"):
            with self.subTest(mode=mode):
                state = self.state / mode
                self.control = Controller(settings(self.state.parent / "repo"), {}, state)
                record = {**self.start_payload("managed-legacy\n"), "host_id": "original-host", "status": "running"}
                if mode.startswith("terminal"):
                    record.update(status="failed", finished_at=int(time.time() * 1000), exit_code=3)
                    if mode != "terminal":
                        record.pop("started_at")
                    if mode == "terminal_missing_times":
                        record["finished_at"] = None
                    if mode in {"terminal_survivor", "terminal_unknown_group"}:
                        record.update(status="exited", exit_code=0)
                    if mode == "terminal_survivor":
                        record["shutdown_uncertain"] = True
                with self.control.store.transaction() as db:
                    db.execute("INSERT INTO managed_processes VALUES(?,?)", (record["managed_id"], json.dumps(record)))
                    db.execute("DROP TABLE completion_events")
                    db.execute("DROP TABLE completion_consumers")
                self.control = Controller(settings(self.state.parent / "repo"), {}, state)
                finish = {"managed_id": record["managed_id"], "finished_at": int(time.time() * 1000), "elapsed_ms": 1,
                          "exit_code": 3, "max_process_rss_bytes": 1, "group_outlived": False,
                          "start": self.start_payload(record["managed_id"])}
                if mode == "finish":
                    self.control.handle("managed_finish", finish)
                elif mode == "spool":
                    directory = completion_directory(state)
                    directory.mkdir()
                    (directory / "managed-legacy.json").write_text(json.dumps(finish))
                with patch("router.managed.request", side_effect=self.request):
                    event = wait("worker-a", 5)
                self.assertEqual(event["status"], "uncertain" if mode in {
                    "terminal_survivor", "terminal_unknown_group"} else "failed")
                self.assertEqual(event["event"]["exit_code"], record.get("exit_code", 3))
                if mode == "terminal_survivor":
                    self.assertIs(event["event"]["group_outlived"], True)
                self.assertNotIn("managed_id", event["event"])  # Unknown metadata must not jam a valid outcome.
                self.assertEqual(event["event"]["host_id"], "original-host")
                self.ack_completion(event)
                self.run_command("pass")
                self.assertEqual(self.next_completion()["status"], "completed")

    def test_retired_starts_cannot_reopen_either_outcome_and_late_finish_can_settle_uncertainty(self):
        from router.managed import RETENTION_MS
        for phase in ("uncertain", "finished"):
            with self.subTest(phase=phase):
                state = self.state / phase
                self.control = Controller(settings(self.state.parent / "repo"), {}, state)
                self.next_completion()
                start = self.start_payload("managed-retired")
                finish = {"managed_id": start["managed_id"], "finished_at": int(time.time() * 1000), "elapsed_ms": 1,
                          "exit_code": 0, "max_process_rss_bytes": 1, "group_outlived": False, "start": start}
                self.control.managed.start(start)
                if phase == "finished":
                    self.control.managed.finish(finish)
                event = self.next_completion()
                self.ack_completion(event)
                with patch("router.managed._now", return_value=int(time.time() * 1000) + RETENTION_MS + 1):
                    self.control.managed.reconcile()
                    self.control = Controller(settings(self.state.parent / "repo"), {}, state)
                    self.control.managed.start(start)
                    directory = completion_directory(state)
                    directory.mkdir()
                    (directory / "start-managed-retired.json").write_text(json.dumps(start))
                    self.assertEqual(self.next_completion()["status"], "pending")
                    with self.control.store.connect() as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0], 0)
                    original_host = self.control.managed.host_id
                    self.control.managed = ManagedProcesses(self.control.store, "serving-host", state)
                    self.control.managed.finish(finish)
                    next_event = self.next_completion()
                    self.assertEqual(next_event["status"], "completed" if phase == "uncertain" else "pending")
                    if phase == "uncertain":
                        self.assertEqual(next_event["host_id"], "serving-host")
                        self.assertEqual(next_event["event"]["host_id"], original_host)
                    with self.control.store.connect() as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0],
                                         1 if phase == "uncertain" else 0)

    def test_subscribed_consumer_can_read_while_reconciliation_is_busy(self):
        self.run_command("pass")
        event = self.next_completion()
        self.control.managed.start({**self.start_payload("managed-busy"), "wrapper_identity": "blocked"})
        entered, release, read = threading.Event(), threading.Event(), threading.Event()
        outcomes = []

        def identity(pid):
            entered.set()
            release.wait(30)
            return "blocked"

        def consume():
            try:
                outcomes.append(self.next_completion())
            finally:
                read.set()

        with patch.object(self.control.managed, "identity", side_effect=identity):
            reconciler = threading.Thread(target=self.control.managed.reconcile)
            consumer = threading.Thread(target=consume)
            reconciler.start()
            try:
                self.assertTrue(entered.wait(15))
                consumer.start()
                self.assertTrue(read.wait(15))  # Must read while reconciliation still owns its write transaction.
                self.assertEqual(outcomes, [event])
            finally:
                release.set()
                reconciler.join(15)
                if consumer.ident is not None:
                    consumer.join(15)

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

    def test_commands_needing_attention_are_listed_first(self):
        self.assertEqual(self.run_command("raise SystemExit(1)", label="failed"), 1)
        for index in range(3):
            self.run_command("pass", label=f"ok-{index}")
        records, _ = self.control.managed.records(1)
        self.assertEqual([record["label"] for record in records], ["failed"])

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
