import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router.cli import main
from router.client import request as router_request
from router.daemon import Controller, Handler, Runtime, Server
from router.db import now_ms
from router.dispatch import canonical


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-correlation-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "example-project"
        self.project.mkdir()
        (self.project / ".agent-router.json").write_text('{"observe": true}\n')
        self.router = Controller(settings(self.project), {}, self.root / "state")
        self.at = now_ms() - 2000

    def session(self, identity="session-a", source="codex"):
        self.router.handle("session_observe", {"source": source, "session_id": identity,
                                               "kind": "session_start", "observed_at": self.at,
                                               "cwd": str(self.project)})

    def export(self, **options):
        return self.router.handle("correlation", options)

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

    def test_export_keeps_exact_identities_and_excludes_content_without_writes(self):
        self.session()
        self.router.native.observe({"source": "codex", "session_id": "session-a", "agent_id": "worker-a",
                                    "parent_agent_id": "parent-a", "kind": "start", "observed_at": self.at,
                                    "task": "fixture-private-content", "activity": "fixture-private-content"})
        self.router.sessions.observe({"source": "codex", "session_id": "session-a", "kind": "task_created",
                                      "task_id": "batch-job", "observed_at": self.at + 1,
                                      "task_subject": "fixture-private-content"})
        instruction = self.router.handle("submit", {"session_id": "parent  session", "prompt_id": "prompt-a",
                                                     "parent_cli": "claude"})["instruction_id"]
        invocation = {"request_version": "1", "request_id": "request-a", "instruction_id": instruction,
                      "executor": "opencode", "model": "test", "task": "fixture-private-content",
                      "parent_repo": str(self.project), "parent_worktree": str(self.project), "mode": "write"}
        self.router.handle("authorize", {"session_id": "parent  session", "prompt_id": "prompt-a",
                                          "operation": "start", "invocation": invocation})
        task = self.router.handle("start", invocation)
        self.router.store.update(task["task_id"], lambda record: record.update(
            child_session_id="child  session", attempt_number=1, attempts=1, attempt_started_at=self.at,
            summary="fixture-private-content", tool_contents="fixture-private-content",
            secret="fixture-secret-value", cost=123, usage={"input_tokens": 123}))
        self.router.store.save_attempt(self.router.store.get(task["task_id"]))
        managed = {"managed_id": "managed-batch-job", "label": "fixture-private-content", "session_id": "session  a",
                   "agent_id": "worker  a", "attribution": "exact", "wrapper_pid": 1,
                   "wrapper_identity": "fixture-identity", "pid": 2, "pgid": 2,
                   "executable": "codex", "argument_count": 1, "started_at": self.at}
        self.router.managed.start(managed)
        self.router.managed.finish({"managed_id": managed["managed_id"], "finished_at": self.at + 1,
                                    "elapsed_ms": 1, "exit_code": 0, "group_outlived": False,
                                    "max_process_rss_bytes": 0})
        self.router.managed.subscribe({"consumer": "worker-a"})
        with self.router.store.connect() as db:
            before = list(db.execute("SELECT name,cursor,subscription_id FROM completion_consumers"))
        result = self.export()
        encoded = canonical(result)
        for content in ("fixture-private-content", "fixture-secret-value", str(self.project), "invocation",
                        "argument_count", "executable", '"cost"', '"usage"'):
            self.assertNotIn(content, encoded)
        self.assertEqual(result["host_id"], self.router.host.host_id)
        records = {record["kind"]: record for record in result["records"]}
        self.assertEqual(set(records), {"session", "agent", "work_item", "router_task", "attempt", "managed", "completion"})
        self.assertEqual(records["session"]["session_id"], "session-a")
        self.assertEqual(records["session"]["runtime"], "codex")
        self.assertEqual(records["agent"]["agent_id"], "worker-a")
        self.assertEqual(records["agent"]["parent_agent_id"], "parent-a")
        self.assertEqual(records["work_item"]["work_item_id"], "batch-job")
        self.assertEqual(records["router_task"]["session_id"], "child  session")
        self.assertEqual(records["router_task"]["parent_session_id"], "parent  session")
        self.assertEqual(records["router_task"]["parent_runtime"], "claude")
        self.assertEqual(records["router_task"]["router_task_id"], task["task_id"])
        self.assertEqual(records["attempt"]["attempt_number"], 1)
        self.assertIsNone(records["managed"].get("runtime"), "An executable or label cannot establish runtime")
        self.assertEqual(records["completion"]["event_id"], "completion-1")
        self.assertFalse(result["partial"], result["errors"])
        for kind in ("managed", "completion"):
            self.assertEqual(records[kind]["session_id"], "session  a")
            self.assertEqual(records[kind]["agent_id"], "worker  a")
        self.assertTrue(all(record["traced"] == "unknown" for record in result["records"]))
        paged, cursor = [], None
        for _ in range(len(result["records"])):
            page = self.export(limit=1, cursor=cursor)
            paged.extend(page["records"])
            cursor = page["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(paged, result["records"])
        with self.router.store.transaction() as db:
            db.execute("UPDATE completion_events SET data=NULL")
        tombstone = next(record for record in self.export()["records"] if record["kind"] == "completion")
        self.assertEqual((tombstone["event_id"], tombstone["managed_id"], tombstone["lifecycle_event"]),
                         ("completion-1", managed["managed_id"], "finished"))
        self.assertIsNone(tombstone["status"])
        self.assertIsNone(tombstone["finished_at"])
        with self.router.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT name,cursor,subscription_id FROM completion_consumers")],
                             [tuple(row) for row in before])

    def test_pages_survive_restart_and_reject_invalid_or_foreign_cursors(self):
        for number in range(5):
            self.session(f"session-{number}")
        first = self.export(limit=2)
        self.assertEqual(len(first["records"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        restarted = Controller(settings(self.project), {}, self.root / "state")
        records = list(first["records"])
        cursor = first["next_cursor"]
        while cursor is not None:
            page = restarted.handle("correlation", {"limit": 2, "cursor": cursor})
            records.extend(page["records"])
            cursor = page["next_cursor"]
        self.assertEqual({record["session_id"] for record in records}, {f"session-{number}" for number in range(5)})
        self.assertEqual(len(records), 5)
        other = Controller(settings(self.project), {}, self.root / "other-state")
        with patch.object(other.host, "host_id", "different-host"), self.assertRaises(ValueError):
            other.handle("correlation", {"cursor": first["next_cursor"]})
        for options in ({"limit": 0}, {"limit": True}, {"limit": 101}, {"limit": 1.0}, {"limit": "1"},
                        {"cursor": "invalid"}, {"cursor": {}}, {"extra": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.export(**options)

    def test_cli_exports_json_and_reports_transport_failures(self):
        self.session()
        def request(operation, payload, **options):
            self.assertEqual(operation, "correlation")
            self.assertTrue(options.get("deadline"))
            return router_request(operation, payload, endpoint, **options)
        output = io.StringIO()
        with self.wire() as endpoint, patch("sys.argv", ["router", "correlation", "--json", "--limit", "1"]), \
                patch("router.cli.request", side_effect=request), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())["records"][0]["session_id"], "session-a")
        for failure, code in ((ValueError("fixture transport unavailable"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(failure=type(failure).__name__), \
                    patch("sys.argv", ["router", "correlation", "--json"]), \
                    patch("router.cli.request", side_effect=failure), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, code)

    def test_unknown_runtime_and_invalid_metadata_remain_visible_without_content(self):
        self.session()
        with self.router.store.transaction() as db:
            for identity, data in (("future-session", {"status": "idle", "last_event_at": self.at}),
                                   ("broken-session", {"status": "fixture-private-content", "last_event_at": True,
                                                       "parent_session_id": "invalid\nidentity"})):
                db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (self.router.host.host_id, "example-runtime",
                           identity, canonical({**data, "prompt": "fixture-private-content"})))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (self.router.host.host_id, "codex", "corrupt", "[]"))
        # A reader must work while another connection holds the writer reservation.
        with self.router.store.transaction():
            result = self.export()
        self.assertTrue(result["partial"])
        self.assertEqual(result["invalid_records"], 2)
        records = {record["session_id"]: record for record in result["records"]}
        self.assertEqual(records["future-session"]["runtime"], "example-runtime")
        self.assertIsNone(records["future-session"]["model_provider"])
        self.assertIsNone(records["future-session"]["parent_session_id"])
        self.assertIsNone(records["broken-session"]["status"])
        self.assertIsNone(records["broken-session"]["timestamp"])
        self.assertNotIn("fixture-private-content", canonical(result))
        self.assertEqual(records["session-a"]["runtime"], "codex")
        with self.assertRaises(ValueError) as caught:
            Runtime(self.router).validate({"invocation": {"executor": "example-runtime"}})
        self.assertEqual(str(caught.exception), "Unsupported executor; no fallback")

    def test_durable_keys_win_over_contradictory_metadata_in_every_record_kind(self):
        host = self.router.host.host_id
        with self.router.store.transaction() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (host, "codex", "session-a", canonical({
                "host_id": "contradictory", "source": "contradictory", "session_id": "contradictory", "status": "idle"})))
            db.execute("INSERT INTO observations VALUES(?,?,?)", ("native-a", "session-a", canonical({
                "host_id": host, "source": "codex", "task_id": "contradictory", "agent_id": "worker-a",
                "parent_session_id": "contradictory", "status": "idle"})))
            db.execute("INSERT INTO session_tasks VALUES(?,?,?,?,?)", (host, "codex", "session-a", "batch-job", canonical({
                "host_id": host, "source": "codex", "session_id": "session-a", "task_id": "contradictory",
                "status": "completed"})))
            db.execute("INSERT INTO tasks VALUES(?,?,?)", ("task-a", "parent-session", canonical({
                "task_id": "contradictory", "session_id": "contradictory", "executor": "codex", "status": "interrupted"})))
            db.execute("INSERT INTO attempts VALUES(?,?,?)", ("task-a", 3, canonical({
                "task_id": "contradictory", "attempt_number": 2, "executor": "codex", "status": "failed"})))
            db.execute("INSERT INTO managed_processes VALUES(?,?)", ("managed-a", canonical({
                "managed_id": "contradictory", "host_id": host, "status": "running"})))
            db.execute("""INSERT INTO completion_events(managed_id,host_id,phase,created_at,data)
                          VALUES(?,?,?,?,?)""", ("managed-a", host, "finished", self.at, canonical({
                "status": "failed", "event": {"managed_id": "contradictory", "host_id": "contradictory",
                                                "event_id": "contradictory"}})))
        result = self.export()
        self.assertTrue(result["partial"])
        self.assertEqual(result["invalid_records"], 7)
        self.assertNotIn("contradictory", canonical(result))
        records = {record["kind"]: record for record in result["records"]}
        expected = {"session": {"runtime": "codex", "session_id": "session-a"},
                    "agent": {"session_id": "session-a", "parent_session_id": "session-a"},
                    "work_item": {"work_item_id": "batch-job"},
                    "router_task": {"router_task_id": "task-a", "parent_session_id": "parent-session", "status": "interrupted"},
                    "attempt": {"router_task_id": "task-a", "attempt_number": 3},
                    "managed": {"managed_id": "managed-a"},
                    "completion": {"event_id": "completion-1", "managed_id": "managed-a"}}
        for kind, identities in expected.items():
            with self.subTest(kind=kind):
                for field, value in identities.items():
                    self.assertEqual(records[kind][field], value)

    def test_output_byte_bound_defers_the_next_record_without_skipping_it(self):
        from router.correlation import MAX_BYTES
        token = "\U0001f600" * 239
        with self.router.store.transaction() as db:
            for number in range(100):
                identity = token[:240 - len(str(number))] + str(number)
                record = {"host_id": self.router.host.host_id, "source": token, "agent_id": token,
                          "parent_session_id": identity, "parent_agent_id": token, "status": "idle"}
                db.execute("INSERT INTO observations VALUES(?,?,?)", (f"native-{number}", identity, canonical(record)))
        seen = []
        cursor = None
        while True:
            page = self.export(cursor=cursor)
            self.assertLessEqual(len(canonical(page).encode()), MAX_BYTES)
            self.assertTrue(page["records"])
            self.assertFalse(page["partial"])
            seen.extend(record["session_id"] for record in page["records"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), 100)
        self.assertEqual(len(set(seen)), 100)

    def test_audited_attribute_shapes_require_exact_runtime_host_and_native_identity(self):
        for runtime in ("claude", "codex"):
            self.session("shared-session", runtime)
            self.router.native.observe({"source": runtime, "session_id": "shared-session", "agent_id": "worker-a",
                                        "kind": "start", "observed_at": self.at})
        self.session("unknown", "codex")
        records = self.export()["records"]
        session_keys = {(record["runtime"], record["host_id"], record["session_id"])
                        for record in records if record["kind"] == "session"}
        codex_child_keys = {(record["runtime"], record["host_id"], record["agent_id"])
                            for record in records if record["kind"] == "agent" and record["runtime"] == "codex"}
        # Collector identity and runtime are supplied by analysis, not invented by a plugin.
        host_map = {"collector-a": self.router.host.host_id, "collector-b": "different-host"}
        cases = [
            ("claude", "collector-a", {"session_id": "shared-session"}, {}, True),
            ("claude", "collector-a", {}, {"session_id": "shared-session"}, True),
            ("codex", "collector-a", {"sessionId": "shared-session"}, {}, True),
            ("codex", "collector-a", {}, {"codex.thread_id": "shared-session"}, True),
            # A child can share its root trace; its observation thread ID identifies it.
            ("codex", "collector-a", {"sessionId": "shared-session"}, {"codex.thread_id": "worker-a"}, True),
            ("codex", "collector-a", {}, {"codex.thread_id": "unknown"}, False),
            ("codex", "collector-a", {}, {}, False),
            ("codex", "collector-b", {"sessionId": "shared-session"}, {}, False),
            ("codex", "unmapped", {"sessionId": "shared-session"}, {}, False),
            ("openai", "collector-a", {"sessionId": "shared-session"}, {}, False),
            # Claude subagent basenames and adjacent times supply no native identity.
            ("claude", "collector-a", {}, {"transcript_basename": "worker-a.jsonl", "timestamp": self.at}, False),
        ]
        for runtime, collector, attributes, metadata, expected in cases:
            with self.subTest(runtime=runtime, collector=collector, metadata=metadata):
                identity = (metadata.get("session_id", attributes.get("session_id")) if runtime == "claude" else
                            metadata.get("codex.thread_id", attributes.get("sessionId")) if runtime == "codex" else None)
                if runtime == "codex" and identity == "unknown":
                    identity = None  # The audited parser's reserved missing-ID value.
                key = (runtime, host_map.get(collector), identity)
                self.assertEqual(key in session_keys | codex_child_keys, expected)
        for kind, offset in (("session_end", 1), ("session_start", 2)):
            self.router.sessions.observe({"source": "codex", "session_id": "shared-session", "kind": kind,
                                          "observed_at": self.at + offset})
        resumed = next(record for record in self.export()["records"]
                       if record["kind"] == "session" and record["runtime"] == "codex")
        self.assertEqual((resumed["runtime"], resumed["host_id"], resumed["session_id"]),
                         ("codex", self.router.host.host_id, "shared-session"))
        self.assertEqual((resumed["lifecycle_event"], resumed["timestamp"]), ("session_start", self.at + 2))

    def test_database_failure_is_an_explicit_export_error(self):
        with self.router.store.transaction() as db:
            db.execute("DROP TABLE attempts")
        with self.wire() as endpoint, self.assertLogs("router.daemon", level="ERROR") as logs, \
                self.assertRaises(ValueError) as caught:
            router_request("correlation", {}, endpoint, deadline=True)
        self.assertEqual(str(caught.exception), "Router database is unavailable; inspect the operator log")
        self.assertIs(logs.records[0].exc_info[0], sqlite3.OperationalError)

    def seed_parent_link(self, instruction="instruction-a", **claims):
        task = {"task_id": "task-a", "session_id": "parent-a", "instruction_id": instruction,
                "executor": "codex", "status": "failed", **claims}
        attempt = {"task_id": "task-a", "attempt_number": 1, "executor": "codex", "status": "failed", **claims}
        with self.router.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO submissions VALUES(?,?,?,?,?)", ("instruction-a", "parent-a", "prompt-a", self.at, "claude"))
            db.execute("INSERT OR REPLACE INTO submissions VALUES(?,?,?,?,?)", ("instruction-b", "parent-b", "prompt-b", self.at, "codex"))
            db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?)", ("task-a", "parent-a", canonical(task)))
            db.execute("INSERT OR REPLACE INTO attempts VALUES(?,?,?)", ("task-a", 1, canonical(attempt)))

    def test_parent_runtime_requires_the_submission_to_match_the_durable_parent(self):
        for instruction, expected, partial in (("instruction-a", "claude", False),
                                               ("instruction-b", None, True), ("missing", None, False)):
            self.seed_parent_link(instruction)
            with self.subTest(instruction=instruction):
                result = self.export()
                self.assertEqual(result["partial"], partial)
                self.assertEqual(len(result["records"]), 2)
                for record in result["records"]:
                    self.assertEqual(record["parent_session_id"], "parent-a")
                    self.assertEqual(record["parent_runtime"], expected)
                if partial:
                    self.assertEqual(result["invalid_records"], 2)
                    self.assertTrue(all("parent_runtime" in error["fields"] for error in result["errors"]))

    def test_task_host_claims_are_not_relabelled_as_the_producing_host(self):
        for claim in ("foreign-host", {"invalid": "host"}, "invalid\nhost", None, self.router.host.host_id):
            self.seed_parent_link(host_id=claim)
            with self.subTest(claim=claim):
                result = self.export()
                if claim == "foreign-host":
                    self.assertTrue(result["partial"])
                    self.assertEqual(result["records"], [])
                    self.assertEqual(result["invalid_records"], 2)
                elif isinstance(claim, dict) or claim == "invalid\nhost":
                    self.assertTrue(result["partial"])
                    self.assertEqual(len(result["records"]), 2)
                    self.assertTrue(all(record["host_id"] is None for record in result["records"]))
                else:
                    self.assertFalse(result["partial"])
                    self.assertTrue(all(record["host_id"] == claim for record in result["records"]))


if __name__ == "__main__":
    unittest.main()
