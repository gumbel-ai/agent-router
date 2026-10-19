import base64
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from router.cli import main
from router.daemon import Controller
from router.db import Store


class TaskDiffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ar-task-diff-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.control = object.__new__(Controller)
        self.control.directory = self.root
        self.control.host = SimpleNamespace(host_id="host-a")
        self.control.store = Store(self.root / "router.sqlite3")
        self.artifacts = self.root / "tasks" / "task-example"
        self.artifacts.mkdir(parents=True)
        self.patch = self.artifacts / "attempt-1.patch"
        self.content = b"diff --git a/example.bin b/example.bin\nGIT binary patch\n\x00\xff"
        self.patch.write_bytes(self.content)
        self.task = {
            "task_id": "task-example", "session_id": "session-a", "status": "awaiting_review",
            "attempts": 1, "attempt_number": 1, "execution_stopped": True,
            "execution_outcome": "completed", "artifact_dir": str(self.artifacts),
            "snapshot_commit": "a" * 40, "snapshot_tree": "b" * 40, "final_tree": "c" * 40,
            "patch": str(self.patch), "invocation": {"task": "private instruction fixture"},
        }
        with self.control.store.transaction() as db:
            self.control.store.put(db, self.task)
            self.control.store.save_attempt(self.task, db)

    def read(self, **options):
        payload = {"task_id": "task-example", "attempt_number": 1, **options}
        try:
            return self.control.handle("task_diff", payload)
        except ValueError as error:
            self.fail(f"Task diff read must return an explicit receipt: {error}")

    def persist(self, current, frame):
        with self.control.store.transaction() as db:
            self.control.store.put(db, current)
            self.control.store.save_attempt(frame, db)

    def cli(self, arguments, local=None):
        output, error = io.StringIO(), io.StringIO()
        local = local or (lambda operation, payload, **kwargs: self.control.handle(operation, payload))
        with patch("sys.argv", ["router", *arguments]), patch("router.client.request", side_effect=local), \
                redirect_stdout(output), redirect_stderr(error):
            try:
                main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code
        return code, output.getvalue(), error.getvalue()

    def test_exact_stopped_attempt_returns_binary_bytes_without_mutating_records(self):
        before = (self.control.store.get("task-example"), self.control.store.history("task-example"))
        result = self.read()
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["host_id"], result["task_id"], result["attempt_number"]),
                         ("host-a", "task-example", 1))
        self.assertEqual(base64.b64decode(result["patch_base64"], validate=True), self.content)
        self.assertEqual(result["sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(result["byte_count"], len(self.content))
        self.assertEqual((self.control.store.get("task-example"), self.control.store.history("task-example")), before)
        self.assertNotIn("private instruction fixture", str(result))
        self.assertNotIn(str(self.root), str(result))

    def test_missing_patch_is_not_an_empty_delta(self):
        self.patch.write_bytes(b"")
        self.task["final_tree"] = self.task["snapshot_tree"]
        with self.control.store.transaction() as db:
            self.control.store.put(db, self.task)
            self.control.store.save_attempt(self.task, db)
        empty = self.read()
        self.assertEqual((empty["status"], empty["byte_count"], empty["patch_base64"]), ("ok", 0, ""))
        self.patch.unlink()
        missing = self.read()
        self.assertEqual((missing["status"], missing.get("error", {}).get("code")), ("unavailable", "artifact_missing"))

    def test_current_state_overrides_saved_frame_and_retained_attempts_stay_exact(self):
        for change, expected in (({"status": "running", "execution_stopped": False}, "attempt_active"),
                                 ({"status": "interrupted", "execution_stopped": False}, "state_unknown"),
                                 ({"status": "awaiting_review", "execution_stopped": 1}, "state_unknown"),
                                 ({"status": ["awaiting_review"]}, "state_unknown"),
                                 ({"final_tree": "d" * 40}, "capture_unavailable"),
                                 ({"attempt_number": True}, "capture_unavailable")):
            with self.subTest(change=change):
                self.persist({**self.task, **change}, self.task)
                self.assertEqual(self.read().get("error", {}).get("code"), expected)
        for status in ("failed", "cancelled", "closed"):
            with self.subTest(stopped=status):
                frame = {**self.task, "status": status}
                self.persist(frame, frame)
                self.assertEqual(self.read()["status"], "ok")
        later = {**self.task, "attempt_number": 2, "attempts": 2, "status": "running", "execution_stopped": False}
        for field in ("final_tree", "patch"):
            later.pop(field)
        self.persist(later, self.task)
        self.assertEqual(base64.b64decode(self.read()["patch_base64"]), self.content)
        self.assertEqual(self.read(attempt_number=2).get("error", {}).get("code"), "attempt_active")
        self.assertEqual(self.read(attempt_number=3).get("error", {}).get("code"), "attempt_unknown")
        self.assertEqual(self.read(task_id="task-missing").get("error", {}).get("code"), "task_unknown")
        for field, value in (("host_id", "host-b"), ("snapshot_commit", "d" * 40),
                             ("snapshot_tree", "d" * 40), ("final_tree", "d" * 40)):
            with self.subTest(identity=field):
                self.assertEqual(self.read(**{field: value}).get("error", {}).get("code"), "identity_mismatch")
        for change in ({"snapshot_commit": None}, {"final_tree": "c" * 64}, {"artifact_dir": str(self.root)},
                       {"patch": str(self.root / "unrelated.patch")}):
            with self.subTest(frame=change):
                self.persist(later, {**self.task, **change})
                self.assertEqual(self.read()["status"], "unavailable")

    def test_task_and_attempt_are_read_from_one_database_snapshot(self):
        original = self.control.store.connect
        advanced = False

        @contextmanager
        def interleaved(*args, **kwargs):
            nonlocal advanced
            with original(*args, **kwargs) as db:
                def execute(query, arguments=()):
                    nonlocal advanced
                    if query == "SELECT data FROM attempts WHERE task_id=? AND number=?":
                        advanced = True
                        later = {**self.task, "status": "running", "attempt_number": 2, "execution_stopped": False}
                        self.persist(later, {**self.task, "final_tree": "d" * 40})
                    return db.execute(query, arguments)
                yield SimpleNamespace(execute=execute, commit=db.commit, rollback=db.rollback)

        with patch.object(self.control.store, "connect", interleaved):
            result = self.read()
        self.assertTrue(advanced)
        self.assertEqual((result["status"], result.get("final_tree")), ("ok", "c" * 40))
        self.assertEqual(self.control.store.get("task-example")["attempt_number"], 2)

    def test_artifact_bounds_owned_paths_and_read_stability(self):
        from router.task_diff import MAX_PATCH_BYTES
        self.patch.write_bytes(b"x" * MAX_PATCH_BYTES)
        self.assertEqual(self.read()["byte_count"], MAX_PATCH_BYTES)
        self.patch.write_bytes(b"x" * (MAX_PATCH_BYTES + 1))
        self.assertEqual(self.read().get("error", {}).get("code"), "artifact_oversize")
        self.patch.unlink()
        outside = self.root / "unrelated.patch"
        outside.write_bytes(self.content)
        for kind in ("symlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.patch.symlink_to(outside)
                elif kind == "directory":
                    self.patch.mkdir()
                else:
                    os.mkfifo(self.patch)
                try:
                    self.assertEqual(self.read().get("error", {}).get("code"), "artifact_unsafe")
                finally:
                    self.patch.rmdir() if kind == "directory" else self.patch.unlink()
        self.patch.write_bytes(self.content)
        for directory in (self.artifacts, self.root / "tasks"):
            moved = self.root / "retained"
            directory.rename(moved)
            directory.symlink_to(moved, target_is_directory=True)
            try:
                self.assertEqual(self.read().get("error", {}).get("code"), "artifact_unsafe")
            finally:
                directory.unlink()
                moved.rename(directory)
        real_read = os.read
        for mutation in ("append", "replace", "remove"):
            self.patch.write_bytes(self.content)
            changed = False

            def read_and_change(descriptor, size, mutation=mutation):
                nonlocal changed
                data = real_read(descriptor, size)
                if not changed:
                    changed = True
                    if mutation == "append":
                        with self.patch.open("ab") as stream:
                            stream.write(b"extra")
                    elif mutation == "replace":
                        replacement = self.artifacts / "replacement.patch"
                        replacement.write_bytes(self.content)
                        replacement.replace(self.patch)
                    else:
                        self.patch.unlink()
                return data
            with self.subTest(mutation=mutation), patch("router.task_diff.os.read", side_effect=read_and_change):
                result = self.read()
            self.assertEqual(result["status"], "unavailable")
            self.assertIn(result.get("error", {}).get("code"), {"artifact_unstable", "artifact_missing"})

    def test_empty_patch_must_agree_with_capture_tree_identity(self):
        for content, final_tree, expected in ((b"", "c" * 40, "unavailable"),
                                               (self.content, "b" * 40, "unavailable"),
                                               (b"", "b" * 40, "ok")):
            with self.subTest(content=bool(content), same_tree=final_tree == "b" * 40):
                self.patch.write_bytes(content)
                frame = {**self.task, "final_tree": final_tree}
                self.persist(frame, frame)
                self.assertEqual(self.read()["status"], expected)

    def test_wire_validates_exact_identity_schema_and_binary_content(self):
        from router.task_diff import decode
        selection = {"task_id": "task-example", "attempt_number": 1, "host_id": "host-a"}
        receipt = self.read()
        raw = lambda value: json.dumps(value, separators=(",", ":")).encode()
        self.assertEqual(decode(raw(receipt), selection), receipt)
        sha256_objects = {**receipt, **{field: value * 64 for field, value in
                                      (("snapshot_commit", "a"), ("snapshot_tree", "b"), ("final_tree", "c"))}}
        self.assertEqual(decode(raw(sha256_objects), selection), sha256_objects)
        for field in ("snapshot_commit", "snapshot_tree", "final_tree"):
            with self.subTest(expected=field), self.assertRaises(ValueError):
                decode(raw(receipt), {**selection, field: "d" * 40})
        changes = ({"schema_version": True}, {"schema_version": 2}, {"host_id": "host-b"},
                   {"host_id": "host\na"}, {"task_id": "task-other"}, {"attempt_number": True},
                   {"attempt_number": 2}, {"byte_count": True}, {"byte_count": -1},
                   {"byte_count": 256 * 1024 + 1}, {"byte_count": len(self.content) - 1},
                   {"sha256": "d" * 64}, {"sha256": []}, {"patch_base64": "!!!="},
                   {"patch_base64": receipt["patch_base64"] + "\n"}, {"snapshot_commit": "0" * 40},
                   {"snapshot_tree": "B" * 40}, {"final_tree": "c" * 64},
                   {"final_tree": receipt["snapshot_tree"]}, {"prompt": "private instruction fixture"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode(raw({**receipt, **change}), selection)
        for malformed in (b"", b"{}", raw(receipt) + b"{}", raw(receipt)[:-1],
                          b'{"schema_version":1,"schema_version":1}', b'{"schema_version":NaN}',
                          b"x" * (512 * 1024 + 1)):
            with self.subTest(malformed=malformed[:50]), self.assertRaises(ValueError):
                decode(malformed, selection)
        for key in receipt:
            missing = dict(receipt)
            missing.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                decode(raw(missing), selection)
        self.patch.write_bytes(b"a")
        one_byte = self.read()
        self.assertEqual(one_byte["patch_base64"], "YQ==")
        for encoded in ("YR==", "YQ=A", "\u00e9Q=="):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                decode(raw({**one_byte, "patch_base64": encoded}), selection)
        mismatch = self.read(host_id="host-b")
        self.assertEqual(decode(raw(mismatch), {**selection, "host_id": "host-b"}), mismatch)
        for change in ({"error": {"code": []}}, {"error": {"code": "unknown"}}, {"host_id": "host-b"},
                       {"patch_base64": "private instruction fixture"}):
            failed = {**self.read(task_id="task-missing"), **change}
            with self.subTest(error=change), self.assertRaises(ValueError):
                decode(raw(failed), {**selection, "task_id": "task-missing"})

    def test_cli_exit_codes_and_selected_host_errors_leave_task_unchanged(self):
        from router.client import request as actual_request
        from router.task_diff import MAX_WIRE_BYTES
        before = (self.control.store.get("task-example"), self.control.store.history("task-example"), self.patch.read_bytes())
        config = self.root / "fleet.toml"
        config.write_text('schema_version=1\n[[host]]\nname="worker-a"\ntransport="local"\n')
        local = ["task-diff", "task-example", "--attempt", "1", "--json"]
        fleet = ["fleet", *local, "--host", "worker-a", "--host-id", "host-a", "--config", str(config)]
        for arguments in (local, fleet):
            code, output, error = self.cli(arguments)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "ok")
            for failure, expected in ((ValueError("private instruction fixture"), 1),
                                      (TimeoutError(), 1), (KeyboardInterrupt(), 130)):
                def failed(*args, failure=failure, **kwargs):
                    raise failure
                code, output, error = self.cli(arguments, failed)
                self.assertEqual(code, expected, error)
                self.assertNotIn("private instruction fixture", output + error)
                if expected == 1:
                    self.assertEqual(json.loads(output)["status"], "unavailable")
                else:
                    self.assertEqual(output, "")
            for invalid in ("0", "-1", "2147483648", "nan"):
                bad = list(arguments)
                bad[bad.index("--attempt") + 1] = invalid
                code, _, error = self.cli(bad, lambda *args, **kwargs: self.fail("Invalid selection reached transport"))
                self.assertEqual(code, 2, error)
        receipt = json.dumps(self.read(), separators=(",", ":")).encode()
        valid = b'{"ok":true,"result":' + receipt + b'}\n'
        exact_bound = b" " * (MAX_WIRE_BYTES - len(valid)) + valid
        refusal = b'{"ok":false,"error":"private instruction fixture"}\n'
        cases = [("valid", valid, 0, None), ("exact-bound", exact_bound, 0, None),
                 ("oversize", b" " * (MAX_WIRE_BYTES + 1 - len(valid)) + valid, 1, "invalid_response"),
                 ("empty", b"", 1, "invalid_response"), ("list", b"[]\n", 1, "invalid_response"),
                 ("null", b"null\n", 1, "invalid_response"), ("scalar", b"true\n", 1, "invalid_response"),
                 ("missing-ok", b"{}\n", 1, "invalid_response"),
                 ("missing-result", b'{"ok":true}\n', 1, "invalid_response"),
                 ("missing-error", b'{"ok":false}\n', 1, "invalid_response"),
                 ("integer-ok", valid.replace(b'"ok":true', b'"ok":1'), 1, "invalid_response"),
                 ("string-ok", valid.replace(b'"ok":true', b'"ok":"false"'), 1, "invalid_response"),
                 ("extra-field", valid.replace(b'{"ok":true', b'{"extra":1,"ok":true'), 1, "invalid_response"),
                 ("duplicate-envelope", valid.replace(b'"ok":true', b'"ok":false,"ok":true'), 1, "invalid_response"),
                 ("duplicate-receipt", valid.replace(b'"schema_version":1', b'"schema_version":2,"schema_version":1'), 1, "invalid_response"),
                 ("nonfinite", valid.replace(b'{"ok":true', b'{"extra":NaN,"ok":true'), 1, "invalid_response"),
                 ("overflow-number", valid.replace(b'"schema_version":1', b'"schema_version":1e309'), 1, "invalid_response"),
                 ("deep", b"[" * 2000 + b"0" + b"]" * 2000 + b"\n", 1, "invalid_response"),
                 ("nonterminated", valid[:-1], 1, "invalid_response"),
                 ("truncated", valid[:-2] + b"\n", 1, "invalid_response"),
                 ("remainder", valid + b"{}", 1, "invalid_response"),
                 ("invalid-error", b'{"ok":false,"error":{"private":"private instruction fixture"}}\n', 1, "invalid_response"),
                 ("refusal", refusal, 1, "request_failed"), ("timeout", TimeoutError(), 1, "transport_deadline"),
                 ("interrupt", KeyboardInterrupt(), 130, None),
                 ("split-valid", [valid[:len(valid) // 2], valid[len(valid) // 2:]], 0, None),
                 ("split-terminator", [valid[:-1], b"\n"], 0, None),
                 ("split-json-tail", [valid, b"{}"], 1, "invalid_response"),
                 ("split-space-tail", [valid, b" "], 1, "invalid_response"),
                 ("split-newline-tail", [valid, b"\n"], 1, "invalid_response"),
                 ("split-nul-tail", [valid, b"\x00"], 1, "invalid_response"),
                 ("split-oversize-tail", [valid, b"x" * (MAX_WIRE_BYTES + 1)], 1, "invalid_response"),
                 ("exact-bound-tail", [exact_bound, b"{}"], 1, "invalid_response"),
                 ("split-refusal-tail", [refusal, b"{}"], 1, "invalid_response"),
                 ("nonclosing-after-line", [valid, TimeoutError()], 1, "transport_deadline"),
                 ("refusal-nonclosing", [refusal, TimeoutError()], 1, "transport_deadline"),
                 ("reset-after-line", [valid, ConnectionResetError()], 1, "transport_unavailable"),
                 ("io-error-after-line", [valid, OSError()], 1, "transport_unavailable"),
                 ("interrupt-after-line", [valid, KeyboardInterrupt()], 130, None),
                 ("remaining-budget", valid, 0, None),
                 ("remaining-expired", valid, 1, "transport_deadline")]
        clock_steps = {"remaining-budget": [0, 1, 2, 3, 4], "remaining-expired": [0, 1, 2, 3, 15]}

        class ReplySocket:
            def __init__(self, content):
                self.chunks = list(content) if isinstance(content, list) else [content]
                self.timeouts = []
                self.read_sizes = []
                self.eof_reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

            def connect(self, endpoint):
                pass

            def sendall(self, content):
                message = json.loads(content)
                self.assert_read = message["operation"]

            def recv(self, count):
                self.read_sizes.append(count)
                if not self.chunks:
                    self.eof_reads += 1
                    return b""
                content = self.chunks[0]
                if isinstance(content, BaseException):
                    raise content
                chunk = content[:count]
                self.chunks[0] = content[len(chunk):]
                if not self.chunks[0]:
                    self.chunks.pop(0)
                if not chunk:
                    self.eof_reads += 1
                return chunk

        def socket_read(operation, payload, **kwargs):
            return actual_request(operation, payload, self.root / "fixture.sock", **kwargs)

        for name, content, expected, expected_error in cases:
            boundary = ReplySocket(content)
            with self.subTest(local_envelope=name), ExitStack() as stack:
                stack.enter_context(patch("router.client.socket.socket", return_value=boundary))
                if name in clock_steps:
                    clock = iter(clock_steps[name])
                    stack.enter_context(patch("router.client.time", SimpleNamespace(monotonic=lambda clock=clock: next(clock))))
                try:
                    code, output, error = self.cli(local, socket_read)
                except (AttributeError, KeyError) as failure:
                    self.fail(f"Malformed local reply escaped the task-diff protocol boundary: {type(failure).__name__}")
                self.assertEqual(code, expected, error)
                self.assertEqual(boundary.assert_read, "task_diff")
                self.assertNotIn("private instruction fixture", output + error)
                if expected == 130:
                    self.assertEqual(output, "")
                else:
                    result = json.loads(output)
                    self.assertEqual(result["status"], "ok" if expected == 0 else "unavailable")
                    if expected_error is not None:
                        self.assertEqual(result.get("error", {}).get("code"), expected_error)
                    if expected == 0 or name == "refusal":
                        self.assertEqual(boundary.eof_reads, 1)
                        self.assertEqual(boundary.read_sizes[-1], 1)
                    if name == "remaining-budget":
                        self.assertEqual(boundary.timeouts, [14, 13, 12, 11])
                    if name == "split-oversize-tail":
                        self.assertEqual(sum(len(chunk) for chunk in boundary.chunks), MAX_WIRE_BYTES)
                    if name == "exact-bound-tail":
                        self.assertEqual(boundary.chunks, [b"}"])
        self.assertEqual(self.patch.read_bytes(), before[2])
        code, _, error = self.cli([*fleet[:fleet.index("--host") + 1], "missing", *fleet[fleet.index("--host") + 2:]])
        self.assertEqual(code, 2, error)
        self.patch.unlink()
        code, output, _ = self.cli(local)
        self.assertEqual((code, json.loads(output).get("error", {}).get("code")), (1, "artifact_missing"))
        self.assertEqual((self.control.store.get("task-example"), self.control.store.history("task-example")), before[:2])

    def test_selected_ssh_host_refusals_and_malformed_replies_are_explicit(self):
        from router.fleet import FleetHost, SSHQueryError
        from router.task_diff import fetch
        selection = {"task_id": "task-example", "attempt_number": 1, "host_id": "host-a"}
        host = FleetHost("worker-a", "ssh", target="example-host")
        success = json.dumps(self.read()).encode()
        refusal = json.dumps(self.read(task_id="task-missing")).encode()
        for value, code in ((b"", "invalid_response"), (b"{}trailing", "invalid_response"),
                            (b"[" * 2000, "invalid_response"),
                            (SSHQueryError("deadline", "private instruction fixture"), "ssh_deadline"),
                            (SSHQueryError("exit_failed", "private instruction fixture", exit_code=255), "ssh_exit_failed"),
                            (SSHQueryError("exit_failed", "", exit_code=1, stdout=success), "invalid_response"),
                            (SSHQueryError("exit_failed", "", exit_code=1, stdout=b""), "invalid_response"),
                            (TimeoutError(), "transport_deadline"), (OSError(), "transport_unavailable")):
            with self.subTest(code=code, value=type(value).__name__), \
                    patch("router.fleet.ssh_stdout", return_value=value, side_effect=value if isinstance(value, Exception) else None):
                result = fetch(selection, host=host)
                self.assertEqual((result["status"], result.get("error", {}).get("code")), ("unavailable", code))
                self.assertNotIn("private instruction fixture", str(result))
        failure = SSHQueryError("exit_failed", "", exit_code=1, stdout=refusal)
        with patch("router.fleet.ssh_stdout", side_effect=failure):
            result = fetch({**selection, "task_id": "task-missing"}, host=host)
            self.assertEqual(result.get("error", {}).get("code"), "task_unknown")
        with patch("router.fleet.ssh_stdout", return_value=success) as transport:
            self.assertEqual(fetch(selection, host=host)["status"], "ok")
            arguments = transport.call_args.args[2]
            self.assertEqual(arguments[:4], ["task-diff", "task-example", "--attempt", "1"])
            self.assertIn("--host-id", arguments)
        for invalid in ({"attempt_number": True}, {"task_id": "../outside"}, {"host_id": "unsafe\nidentity"},
                        {"snapshot_commit": "bad"}, {"path": str(self.patch)}):
            with self.subTest(invalid=invalid), patch("router.fleet.ssh_stdout") as transport, self.assertRaises(ValueError):
                fetch({**selection, **invalid}, host=host)
            transport.assert_not_called()

    def test_binary_capture_producer_and_empty_retained_attempt_reach_local_cli(self):
        from router.snapshot import capture_final, cleanup, git, prepare
        repo = self.root / "example-project"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test")
        (repo / "tracked.txt").write_text("baseline\n")
        (repo / "deleted.txt").write_text("delete me\n")
        (repo / ".gitignore").write_text("ignored/\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Initial fixture")
        (repo / "tracked.txt").write_text("baseline\nstaged\n")
        git(repo, "add", "tracked.txt")
        (repo / "tracked.txt").write_text("baseline\nstaged\nunstaged\n")
        index = repo / git(repo, "rev-parse", "--git-path", "index").strip()
        before = index.read_bytes()
        info = prepare(repo, "task-example", self.artifacts)
        self.addCleanup(cleanup, repo, info)
        empty = capture_final(repo, info, 1, self.artifacts)
        first = {**self.task, **info, **empty}
        self.persist(first, first)
        child = Path(info["worktree"])
        (child / "tracked.txt").write_text("child staged\n")
        git(child, "add", "tracked.txt")
        (child / "tracked.txt").write_text("child staged\nchild unstaged\n")
        (child / "deleted.txt").unlink()
        (child / "new.bin").write_bytes(bytes(range(256)))
        (child / "ignored").mkdir()
        (child / "ignored" / "data.txt").write_text("ignored fixture")
        captured = capture_final(repo, info, 2, self.artifacts)
        current = {**first, **captured, "attempt_number": 2, "attempts": 2}
        self.persist(current, current)
        expected = Path(captured["patch"]).read_bytes()
        self.assertIn(b"GIT binary patch", expected)
        self.assertIn(b"deleted file", expected)
        self.assertIn(b"child staged", expected)
        self.assertIn(b"child unstaged", expected)
        self.assertNotIn(b"ignored fixture", expected)
        arguments = ["task-diff", "task-example", "--attempt", "2", "--json",
                     "--snapshot-commit", info["snapshot_commit"], "--snapshot-tree", info["snapshot_tree"],
                     "--final-tree", captured["final_tree"]]
        code, output, error = self.cli(arguments)
        self.assertEqual(code, 0, error)
        self.assertEqual(base64.b64decode(json.loads(output)["patch_base64"]), expected)
        retained = self.read(attempt_number=1)
        self.assertEqual((retained["byte_count"], retained["snapshot_tree"], retained["final_tree"]),
                         (0, info["snapshot_tree"], info["snapshot_tree"]))
        self.assertEqual(index.read_bytes(), before)

    def test_two_daemons_and_ssh_reader_preserve_host_state_through_failure_and_restart(self):
        from router.client import request as router_request
        from router.fleet import FleetHost
        from router.snapshot import capture_final, cleanup, git, prepare
        from router.task_diff import fetch
        popen = subprocess.Popen
        processes, environments, endpoints, states, stores, patches = [], [], [], [], [], []
        launcher = ("import sys,router.host; router.host.stable_host_id=lambda directory:sys.argv[3]; "
                    "from pathlib import Path; from router.config import Settings; "
                    "from router.daemon import serve; serve(Settings(),{},Path(sys.argv[1]),Path(sys.argv[2]))")

        def stop(process, endpoint):
            if process.poll() is None:
                try:
                    router_request("daemon_stop", {}, endpoint, timeout=15, deadline=True)
                    process.wait(timeout=15)
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=15)
            process.communicate()

        def start(number):
            state, endpoint, environment = states[number], endpoints[number], environments[number]
            process = popen([sys.executable, "-c", launcher, str(state), str(endpoint), f"host-{number}"],
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            processes.append((process, endpoint))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    self.fail("Simulated daemon exited: " + error.decode(errors="replace")[-1000:])
                try:
                    router_request("health", {}, endpoint, timeout=.5, deadline=True)
                    return process
                except (ValueError, OSError):
                    time.sleep(.02)
            self.fail("Simulated daemon did not start within 15 seconds")

        try:
            for number in range(2):
                state, endpoint = self.root / f"state-{number}", self.root / f"host-{number}.sock"
                artifacts = state / "tasks" / "task-example"
                artifacts.mkdir(parents=True)
                repo = self.root / f"example-project-{number}"
                repo.mkdir()
                git(repo, "init", "-q")
                git(repo, "config", "user.email", "test@example.invalid")
                git(repo, "config", "user.name", "Test")
                (repo / "code.txt").write_text("baseline\n")
                git(repo, "add", ".")
                git(repo, "commit", "-qm", "Initial fixture")
                info = prepare(repo, "task-example", artifacts)
                self.addCleanup(cleanup, repo, info)
                (Path(info["worktree"]) / "code.txt").write_text(f"host {number} retained code\n")
                (Path(info["worktree"]) / "code.bin").write_bytes(bytes(range(256)) + bytes([number]))
                captured = capture_final(repo, info, 1, artifacts)
                patches.append(Path(captured["patch"]).read_bytes())
                store = Store(state / "router.sqlite3")
                stores.append(store)
                task = {**self.task, **info, **captured, "artifact_dir": str(artifacts)}
                with store.transaction() as db:
                    store.put(db, task)
                    store.save_attempt(task, db)
                home = self.root / f"home-{number}"
                home.mkdir()
                environment = {key: value for key, value in os.environ.items()
                               if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME"}}
                environment.update(HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                                   PYTHONPYCACHEPREFIX=str(self.root / "pycache"), AGENT_ROUTER_STATE_DIR=str(state),
                                   AGENT_ROUTER_SOCKET=str(endpoint), AGENT_ROUTER_ENV_FILE=str(self.root / "missing.env"))
                states.append(state)
                endpoints.append(endpoint)
                environments.append(environment)
                start(number)
            remote_processes = []

            def remote(command, **kwargs):
                arguments = shlex.split(" ".join(command[command.index("--") + 2:]))
                self.assertEqual(arguments[:2], ["router", "task-diff"])
                self.assertEqual(command[command.index("--") + 1], "example-host")
                process = popen([sys.executable, "-m", "router.cli", *arguments[1:]], env=environments[1], **kwargs)
                remote_processes.append(process)
                return process

            options = {"task_id": "task-example", "attempt_number": 1, "host_id": "host-1"}
            host = FleetHost("worker-a", "ssh", target="example-host")
            before = [(store.get("task-example"), store.history("task-example")) for store in stores]
            local = lambda operation, payload, **kwargs: router_request(operation, payload, endpoints[0], **kwargs)
            code, output, error = self.cli(["task-diff", "task-example", "--attempt", "1", "--json"], local)
            self.assertEqual(code, 0, error)
            self.assertEqual((json.loads(output)["host_id"], base64.b64decode(json.loads(output)["patch_base64"])),
                             ("host-0", patches[0]))
            with patch("router.fleet.subprocess.Popen", side_effect=remote):
                first = fetch(options, host=host)
                self.assertEqual((first["host_id"], base64.b64decode(first["patch_base64"])),
                                 ("host-1", patches[1]))
                wrong = fetch({**options, "host_id": "host-other"}, host=host)
                self.assertEqual((wrong["host_id"], wrong.get("error", {}).get("code")), ("host-1", "identity_mismatch"))
                stop(*processes[1])
                start(1)
                self.assertEqual(fetch(options, host=host), first)
                stop(*processes[-1])
                self.assertEqual(fetch(options, host=host)["status"], "unavailable")
            self.assertTrue(all(process.poll() is not None for process in remote_processes))
            sleeping = []

            def sleeping_reader(*args, **kwargs):
                process = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
                sleeping.append(process)
                return process
            config = self.root / "ssh.toml"
            config.write_text('schema_version=1\n[[host]]\nname="worker-a"\ntransport="ssh"\ntarget="example-host"\n')
            with patch("router.fleet.subprocess.Popen", side_effect=sleeping_reader), \
                    patch("selectors.DefaultSelector.select", side_effect=KeyboardInterrupt):
                code, output, error = self.cli(["fleet", "task-diff", "task-example", "--attempt", "1", "--json",
                                                "--host", "worker-a", "--host-id", "host-1", "--config", str(config)])
            self.assertEqual((code, output), (130, ""), error)
            self.assertTrue(all(process.poll() is not None for process in sleeping))
            after = [(store.get("task-example"), store.history("task-example")) for store in stores]
            self.assertEqual(after, before)
        finally:
            for process, endpoint in reversed(processes):
                stop(process, endpoint)
