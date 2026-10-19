"""Router records sessions and commands only inside projects that opt in with a marker file."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router import observer_hook
from router.daemon import Controller
from router.managed import completion_directory, run
from router.paths import PROJECT_MARKER, PROJECT_MARKER_CONTENT, observed_project


def mark(directory: Path, content: str | bytes = json.dumps(PROJECT_MARKER_CONTENT)) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / PROJECT_MARKER
    marker.write_bytes(content) if isinstance(content, bytes) else marker.write_text(content)


class MarkerLookupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ar-scope-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_nearest_marked_ancestor_is_the_project(self):
        mark(self.root / "outer")
        mark(self.root / "outer" / "inner")
        for cwd, expected in (
                (self.root / "outer", self.root / "outer"),
                (self.root / "outer" / "src" / "deep", self.root / "outer"),
                (self.root / "outer" / "inner" / "src", self.root / "outer" / "inner"),
                (self.root / "outer" / "missing" / "dir", self.root / "outer"),  # Need not exist.
                (self.root / "elsewhere", None)):
            with self.subTest(cwd=cwd):
                self.assertEqual(observed_project(str(cwd)), expected)

    def test_the_real_location_decides_not_the_spelling(self):
        project = self.root / "observed"
        mark(project)
        (self.root / "outside").mkdir()
        (self.root / "into").symlink_to(project / "src", target_is_directory=True)
        (project / "src").mkdir()
        (project / "escape").symlink_to(self.root / "outside", target_is_directory=True)
        for cwd, expected in ((project / ".." / "outside", None), (project / "escape", None),
                              (self.root / "into", project), (self.root / "outside" / ".." / "observed", project)):
            with self.subTest(cwd=cwd):
                self.assertEqual(observed_project(str(cwd)), expected)

    def test_unusable_working_directories_are_outside_every_project(self):
        mark(self.root)
        for cwd in (None, "", "relative/path", 7):
            with self.subTest(cwd=cwd):
                self.assertIsNone(observed_project(cwd))

    def test_a_malformed_marker_is_an_error_not_a_silent_opt_out(self):
        for content in ("", "not json", "[]", '{"observe": false}', '{"observe": 1}',
                        '{"observe": true, "extra": 1}', '{"observe": true}' + " " * 64, b"\xff"):
            with self.subTest(content=content):
                mark(self.root, content)
                with self.assertRaises(ValueError) as caught:
                    observed_project(str(self.root / "src"))
                self.assertIn(str(self.root / PROJECT_MARKER), str(caught.exception))  # Names what to fix.

    def test_a_marker_that_is_not_a_regular_file_is_an_error(self):
        for name, make in (("directory", lambda marker: marker.mkdir()),
                           ("dangling", lambda marker: marker.symlink_to(self.root / "missing")),
                           ("fifo", os.mkfifo)):  # Opening it must not wait for a writer.
            with self.subTest(name=name):
                project = self.root / name
                project.mkdir()
                make(project / PROJECT_MARKER)
                with self.assertRaises(ValueError):
                    observed_project(str(project))


class LifecycleScopeTests(unittest.TestCase):
    """Real Claude and Codex hook mappings through the daemon's queued lifecycle writer."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ar-scope-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "project"
        mark(self.project)
        self.other = self.root / "other"
        self.other.mkdir()
        timeout = patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", 10)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.start(self.root / "state")
        self.clock = int(time.time() * 1000) - 10_000

    def start(self, state: Path) -> None:
        self.control = Controller(settings(self.root / "repo"), {}, state)
        self.control.start_lifecycle_writer()
        self.addCleanup(self.control.stop_lifecycle_writer)

    def hook(self, runtime, name, cwd, session="S", **fields):
        self.clock += 10
        responses = []

        def record(operation, body, timeout):
            responses.append(self.control.handle(operation, body))
            return responses[-1]

        event = {"hook_event_name": name, "session_id": session, "cwd": str(cwd), **fields}
        mapping = observer_hook.observe_codex if runtime == "codex" else observer_hook.observe
        stderr = io.StringIO()
        with patch("router.observer_hook.request", side_effect=record), \
                patch("router.observer_hook.time.time", return_value=self.clock / 1000), \
                redirect_stderr(stderr):
            mapping(event)
        return responses, stderr.getvalue()

    def sessions(self):
        return {row["session_id"]: row for row in self.control.handle("snapshot", {})["sessions"]}

    def rows(self, table):
        with closing(sqlite3.connect(self.control.directory / "router.sqlite3")) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_sessions_outside_observed_projects_leave_no_record(self):
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                session = f"{source}-other"
                for name, fields in (("SessionStart", {"source": "startup"}),
                                     ("SubagentStart", {"agent_id": "child", "agent_type": "worker"}),
                                     ("SessionEnd", {})):
                    responses, stderr = self.hook(source, name, self.other / "src", session, **fields)
                    self.assertEqual(stderr, "")  # Not recording is not an observation failure.
                    for response in responses:
                        self.assertTrue(response["accepted"], response)
                        self.assertEqual(set(response["persistence"].values()), {"not_observed"})
                self.assertNotIn(session, self.sessions())
        for table in ("sessions", "observations", "native_session_watermarks"):
            self.assertEqual(self.rows(table), 0, table)

    def test_an_admitted_session_is_followed_after_it_leaves_the_project(self):
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                session = f"{source}-moving"
                self.hook(source, "SessionStart", self.project / "src", session, source="startup")
                self.hook(source, "SubagentStart", self.project, session, agent_id="child", agent_type="worker")
                # A restarted daemon still knows the session belongs to an observed project.
                self.control.stop_lifecycle_writer()
                self.start(self.root / "state")
                responses, _ = self.hook(source, "SessionEnd", self.other, session)
                self.assertTrue(all(set(r["persistence"].values()) == {"persisted"} for r in responses), responses)
                self.assertEqual(self.sessions()[session]["status"], "stopped")
                agents = [row for row in self.control.native.status() if row["parent_session_id"] == session]
                self.assertEqual(len(agents), 1)
                # The session end closed its agent: Codex agents stop with it, Claude's become unknown.
                self.assertEqual(agents[0]["status"], "stopped" if source == "codex" else "unknown")

    def test_a_malformed_marker_cannot_strand_an_admitted_session(self):
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                session = f"{source}-strand"
                broken = self.root / f"broken-{source}"
                mark(broken, "{}")
                self.hook(source, "SessionStart", self.project, session, source="startup")
                _, stderr = self.hook(source, "SessionEnd", broken, session)
                self.assertEqual(stderr, "")
                self.assertEqual(self.sessions()[session]["status"], "stopped")

    def test_admission_survives_first_events_that_write_no_session_row(self):
        at = int(time.time() * 1000)
        first_events = {
            # A tool completion without a correlation ID creates no session record.
            "untracked-tool": {"session": {"source": "claude", "session_id": "untracked-tool", "kind": "tool_end",
                                           "observed_at": at, "cwd": str(self.project)}},
            # A native-only session end writes only a watermark.
            "native-end": {"native": {"source": "codex", "session_id": "native-end", "kind": "session_end",
                                      "observed_at": at, "cwd": str(self.project)}}}
        for session, payload in first_events.items():
            with self.subTest(session=session):
                self.control.handle("lifecycle_observe", payload)
        self.control.stop_lifecycle_writer()
        self.start(self.root / "state")
        for session, payload in first_events.items():
            with self.subTest(session=session):
                source = next(iter(payload.values()))["source"]
                response = self.control.handle("lifecycle_observe", {"session": {
                    "source": source, "session_id": session, "kind": "session_start", "observed_at": at + 10,
                    "cwd": str(self.other)}})
                self.assertEqual(response["persistence"], {"session": "persisted"})
                self.assertIn(session, self.sessions())

    def test_one_sessions_scope_error_keeps_another_sessions_event(self):
        mark(self.other, "{}")
        self.hook("claude", "SessionStart", self.project, "known", source="startup")
        at = int(time.time() * 1000)
        with redirect_stderr(io.StringIO()):
            response = self.control.handle("lifecycle_observe", {
                "session": {"source": "claude", "session_id": "known", "kind": "session_end", "observed_at": at,
                            "cwd": str(self.other)},
                "native": {"source": "claude", "session_id": "new", "agent_id": "a", "kind": "start",
                           "observed_at": at, "cwd": str(self.other)}})
        self.assertEqual(response["persistence"], {"session": "persisted", "native": "failed"})
        self.assertEqual(self.sessions()["known"]["status"], "stopped")

    def test_a_malformed_component_keeps_its_valid_sibling(self):
        at = int(time.time() * 1000)
        with redirect_stderr(io.StringIO()):
            response = self.control.handle("lifecycle_observe", {
                "session": {"source": "claude", "session_id": "valid", "kind": "session_start", "observed_at": at,
                            "cwd": str(self.project)},
                "native": {"source": "claude", "session_id": [], "agent_id": "a", "kind": "start",
                           "observed_at": at, "cwd": str(self.project)}})
        self.assertEqual(response["persistence"], {"session": "persisted", "native": "failed"})
        self.assertIn("valid", self.sessions())

    def test_admission_keeps_the_writers_database_budget(self):
        locked = sqlite3.connect(self.control.directory / "router.sqlite3", timeout=1)
        self.addCleanup(locked.close)
        locked.execute("BEGIN IMMEDIATE")  # Another writer holds the database.
        started = time.monotonic()
        with redirect_stderr(io.StringIO()):
            response = self.control.handle("lifecycle_observe", {"session": {
                "source": "claude", "session_id": "contended", "kind": "session_start",
                "observed_at": int(time.time() * 1000), "cwd": str(self.project)}})
        elapsed = time.monotonic() - started
        locked.rollback()
        self.assertEqual(response["persistence"], {"session": "failed"})
        # The writer's one-second budget, not SQLite's ten-second default.
        self.assertLess(elapsed, 5)

    def test_one_request_cannot_admit_another_sessions_event(self):
        at = int(time.time() * 1000)
        response = self.control.handle("lifecycle_observe", {
            "session": {"source": "claude", "session_id": "inside", "kind": "session_start", "observed_at": at,
                        "cwd": str(self.project)},
            "native": {"source": "claude", "session_id": "outside", "agent_id": "a", "kind": "start",
                       "observed_at": at, "cwd": str(self.other)}})
        self.assertEqual(response["persistence"], {"session": "persisted", "native": "not_observed"})
        self.assertEqual(self.rows("observations"), 0)
        self.assertEqual(set(self.sessions()), {"inside"})

    def test_a_malformed_marker_fails_the_observation_visibly(self):
        mark(self.other, '{"observe": false}')
        responses, stderr = self.hook("claude", "SubagentStart", self.other, agent_id="a", agent_type="worker")
        self.assertFalse(responses[0]["accepted"])
        self.assertEqual(set(responses[0]["persistence"].values()), {"failed"})
        # The hook names the marker, not a generic write failure.
        self.assertIn(f"project scope could not be checked: {self.other / PROJECT_MARKER}", stderr)
        self.assertEqual(self.control.lifecycle_failures, 1)
        self.assertEqual(self.rows("sessions"), 0)

    def test_direct_observation_endpoints_apply_the_same_scope(self):
        at = int(time.time() * 1000)
        for operation, event in (
                ("session_observe", {"source": "claude", "session_id": "direct", "kind": "session_start"}),
                ("observe", {"source": "claude", "session_id": "direct", "agent_id": "a", "kind": "start"})):
            with self.subTest(operation=operation):
                outside = self.control.handle(operation, {**event, "observed_at": at, "cwd": str(self.other)})
                self.assertEqual(outside["observed"], False)
                self.assertEqual(self.rows("sessions") + self.rows("observations"), 0)
        self.control.handle("session_observe", {"source": "claude", "session_id": "direct", "kind": "session_start",
                                                "observed_at": at, "cwd": str(self.project)})
        self.assertIn("direct", self.sessions())


class ManagedScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ar-scope-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.addCleanup(os.chdir, os.getcwd())

    def run_in(self, directory: Path, script: str, **options):
        os.chdir(directory)
        with patch("router.managed.request", side_effect=AssertionError("must not report")) as reporter, \
                redirect_stderr(io.StringIO()) as stderr:
            code = run([sys.executable, "-c", script], "label", None, None, self.state, **options)
        return code, reporter, stderr.getvalue()

    def test_a_command_outside_every_project_runs_but_is_not_recorded(self):
        outside = self.root / "outside"
        outside.mkdir()
        code, reporter, stderr = self.run_in(outside, "raise SystemExit(4)", work_item_id="batch-job")
        self.assertEqual(code, 4)
        reporter.assert_not_called()
        self.assertIn("not recording", stderr)
        self.assertIn("no completion event will follow", stderr)
        self.assertFalse(completion_directory(self.state).exists())

    def test_a_malformed_marker_stops_before_the_command_starts(self):
        mark(self.root / "broken", "{}")
        ran = self.root / "ran"
        with self.assertRaises(ValueError):
            self.run_in(self.root / "broken", f"open({str(ran)!r}, 'w').close()")
        self.assertFalse(ran.exists())


if __name__ == "__main__":
    unittest.main()
