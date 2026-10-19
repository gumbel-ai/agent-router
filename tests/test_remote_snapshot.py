import hashlib
import json
import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from router import remote_snapshot
from router.processes import sample
from router.snapshot import git


class RemoteSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-remote-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.repository("source")
        self.target = self.repository("target")
        self.artifacts = self.root / "artifacts"

    def repository(self, name):
        root = self.root / name
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "user.name", "Example")
        git(root, "config", "user.email", "example@example.invalid")
        (root / "code.txt").write_text(f"{name} initial\n")
        (root / ".gitignore").write_text("ignored.txt\n")
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")
        return root

    def export(self, paths=("code.txt",), task="task-a"):
        return remote_snapshot.export_snapshot(self.source, task, self.artifacts, paths)

    def test_selected_dirty_binary_files_round_trip_without_history_or_index_changes(self):
        history = git(self.source, "rev-parse", "HEAD").strip()
        (self.source / "code.txt").write_text("staged\n")
        git(self.source, "add", "code.txt")
        (self.source / "code.txt").write_text("unstaged\n")
        (self.source / "worker.bin").write_bytes(bytes(range(256)))
        (self.source / "ignored.txt").write_text("synthetic excluded fixture\n")
        index = self.source / git(self.source, "rev-parse", "--git-path", "index").strip()
        before_index = index.read_bytes()
        before_status = git(self.source, "status", "--porcelain")
        receipt = self.export(("code.txt", "worker.bin"))
        self.assertEqual(index.read_bytes(), before_index)
        self.assertEqual(git(self.source, "status", "--porcelain"), before_status)
        self.assertEqual(receipt, self.export(("worker.bin", "code.txt")))
        result = remote_snapshot.import_snapshot(self.target, receipt, self.artifacts / "snapshot.bundle")
        self.assertEqual(result["snapshot_commit"], receipt["snapshot_commit"])
        self.assertEqual(git(self.target, "show", f"{result['snapshot_commit']}:code.txt"), "unstaged\n")
        self.assertEqual(git(self.target, "show", f"{result['snapshot_commit']}:worker.bin", binary=True), bytes(range(256)))
        self.assertEqual(git(self.target, "rev-list", "--parents", result["snapshot_commit"]).split(), [result["snapshot_commit"]])
        self.assertEqual(git(self.target, "ls-tree", "--name-only", result["snapshot_commit"]).splitlines(), ["code.txt", "worker.bin"])
        self.assertNotEqual(git(self.target, "rev-parse", "HEAD").strip(), result["snapshot_commit"])
        checked = subprocess.run(["git", "-C", str(self.target), "cat-file", "-e", history], capture_output=True, check=False)
        self.assertNotEqual(checked.returncode, 0)
        self.assertEqual(remote_snapshot.import_snapshot(self.target, receipt, self.artifacts / "snapshot.bundle"), result)
        unborn = self.root / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-q")
        self.assertEqual(remote_snapshot.import_snapshot(unborn, receipt, self.artifacts / "snapshot.bundle"), result)
        head = subprocess.run(["git", "-C", str(unborn), "rev-parse", "--verify", "--quiet", "HEAD"], capture_output=True, check=False)
        self.assertNotEqual(head.returncode, 0)
        self.assertEqual(git(unborn, "show", f"{result['snapshot_commit']}:code.txt"), "unstaged\n")

    def test_file_admission_and_immutable_task_binding(self):
        (self.source / "ignored.txt").write_text("synthetic excluded fixture\n")
        git(self.source, "add", "-f", "ignored.txt")
        (self.source / "link.txt").symlink_to("code.txt")
        cases = ((), (".",), ("../code.txt",), (".git/config",), ("ignored.txt",), ("link.txt",), ("missing.txt",), ("code.txt", "code.txt"))
        for paths in cases:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.export(paths)
        receipt = self.export()
        git(self.source, "-c", "core.compression=0", "repack", "-adf")
        git(self.source, "prune-packed")
        self.assertEqual(self.export(), receipt)
        (self.source / "code.txt").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.export()
        self.assertEqual(git(self.source, "rev-parse", "refs/router/snap/task-a").strip(), receipt["snapshot_commit"])
        git(self.source, "symbolic-ref", "refs/router/snap/task-symbolic", "refs/heads/main")
        with self.assertRaisesRegex(ValueError, "symbolic ref"):
            self.export(task="task-symbolic")
        with self.assertRaises(ValueError):
            self.export(task="../task-a")

    def test_import_checks_receipt_bundle_bounds_and_ref_binding(self):
        receipt = self.export()
        bundle = self.artifacts / "snapshot.bundle"
        original = bundle.read_bytes()
        for field, value in (("sha256", "0" * 64), ("byte_count", len(original) + 1),
                             ("snapshot_tree", "0" * 40), ("snapshot_commit", "0" * 40),
                             ("paths", ["../code.txt"]), ("schema_version", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                remote_snapshot.import_snapshot(self.target, {**receipt, field: value}, bundle)
        malformed = b"not a bundle\n"
        bundle.write_bytes(malformed)
        with self.assertRaises(ValueError):
            remote_snapshot.import_snapshot(self.target, {**receipt, "byte_count": len(malformed), "sha256": hashlib.sha256(malformed).hexdigest()}, bundle)
        bundle.write_bytes(original)
        with patch.object(remote_snapshot, "MAX_BUNDLE_BYTES", len(original) - 1), self.assertRaises(ValueError):
            remote_snapshot.import_snapshot(self.target, receipt, bundle)
        git(self.target, "update-ref", "refs/router/snap/task-a", "HEAD")
        with self.assertRaisesRegex(ValueError, "already bound"):
            remote_snapshot.import_snapshot(self.target, receipt, bundle)
        self.assertEqual(git(self.target, "rev-parse", "refs/router/snap/task-a").strip(), git(self.target, "rev-parse", "HEAD").strip())

    def test_size_limit_and_symlinked_parent_are_refused(self):
        with patch.object(remote_snapshot, "MAX_FILE_BYTES", 1), self.assertRaises(ValueError):
            self.export()
        directory = self.source / "directory"
        directory.mkdir()
        (directory / "code.txt").write_text("example\n")
        (self.source / "linked-directory").symlink_to(directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.export(("linked-directory/code.txt",))
        os.chmod(self.artifacts, 0o755)
        with self.assertRaises(ValueError):
            self.export()

    def test_concurrent_same_task_export_is_idempotent_and_filters_do_not_execute(self):
        marker = self.root / "filter-ran"
        (self.source / ".gitattributes").write_text("code.txt filter=example\n")
        git(self.source, "config", "filter.example.clean", f"touch {marker}")
        with ThreadPoolExecutor(max_workers=2) as workers:
            receipts = list(workers.map(lambda _number: self.export(), range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertFalse(marker.exists())
        self.assertEqual(git(self.source, "show", f"{receipts[0]['snapshot_commit']}:code.txt"), "source initial\n")

    def test_parented_unsafe_or_extra_object_bundles_are_rejected_before_import(self):
        receipt = self.export()
        original_commit = git(self.source, "cat-file", "commit", receipt["snapshot_commit"], binary=True)
        original_tree = receipt["snapshot_tree"]
        cases = ("parent", "symlink", "extra_objects")
        for case in cases:
            with self.subTest(case=case):
                changed = dict(receipt)
                commit_content = original_commit
                if case == "parent":
                    parent = git(self.source, "rev-parse", "HEAD").strip()
                    commit_content = original_commit.replace(b"\nauthor ", f"\nparent {parent}\nauthor ".encode(), 1)
                elif case == "symlink":
                    blob = git(self.source, "rev-parse", f"{receipt['snapshot_commit']}:code.txt").strip()
                    result = subprocess.run(["git", "-C", str(self.source), "mktree"], input=f"120000 blob {blob}\tcode.txt\n".encode(), capture_output=True, check=True)
                    changed["snapshot_tree"] = result.stdout.decode().strip()
                    commit_content = original_commit.replace(original_tree.encode(), changed["snapshot_tree"].encode(), 1)
                result = subprocess.run(["git", "-C", str(self.source), "hash-object", "-t", "commit", "-w", "--stdin"], input=commit_content, capture_output=True, check=True)
                changed["snapshot_commit"] = result.stdout.decode().strip()
                objects = changed["snapshot_commit"] + "\n"
                if case == "extra_objects":
                    objects += git(self.source, "rev-parse", "HEAD")
                packed = subprocess.run(["git", "-C", str(self.source), "pack-objects", "--stdout", "--revs"], input=objects.encode(), capture_output=True, check=True).stdout
                content = f"# v2 git bundle\n{changed['snapshot_commit']} refs/router/snap/task-a\n\n".encode() + packed
                bundle = self.root / f"{case}.bundle"
                bundle.write_bytes(content)
                changed.update(byte_count=len(content), sha256=hashlib.sha256(content).hexdigest())
                with self.assertRaises(ValueError):
                    remote_snapshot.import_snapshot(self.target, changed, bundle)
                reference = subprocess.run(["git", "-C", str(self.target), "rev-parse", "--verify", "--quiet", "refs/router/snap/task-a"], capture_output=True, check=False)
                self.assertNotEqual(reference.returncode, 0)

    def test_compressed_object_bounds_are_checked_before_git_import(self):
        receipt = self.export()
        original_git = remote_snapshot._git
        fetches = []

        def observe(root, *arguments, **kwargs):
            if "fetch" in arguments:
                fetches.append(arguments)
            return original_git(root, *arguments, **kwargs)

        def packed_object(kind, declared, body):
            header = bytearray([(kind << 4) | (declared & 15)])
            declared >>= 4
            while declared:
                header[-1] |= 128
                header.append(declared & 127)
                declared >>= 7
            return bytes(header) + zlib.compress(body)

        oversized = 2 * remote_snapshot.MAX_FILE_BYTES
        cases = (
            ("declared expansion", 3, packed_object(3, oversized, b"\0" * oversized)),
            ("actual expansion", 3, packed_object(3, 1, b"\0" * oversized)),
            ("object count", 2**32 - 1, b""),
            ("delta object", 3, packed_object(6, 1, b"x")),
        )
        for name, count, objects in cases:
            with self.subTest(case=name):
                payload = b"PACK" + struct.pack(">II", 2, count) + objects
                payload += hashlib.sha1(payload).digest()
                body = f"# v2 git bundle\n{receipt['snapshot_commit']} refs/router/snap/task-a\n\n".encode() + payload
                bundle = self.root / "unsafe-compression.bundle"
                bundle.write_bytes(body)
                changed = {**receipt, "byte_count": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                fetches.clear()
                with patch.object(remote_snapshot, "_git", side_effect=observe), self.assertRaises(ValueError):
                    remote_snapshot.import_snapshot(self.target, changed, bundle)
                self.assertEqual(fetches, [], "Untrusted pack reached Git before its expansion bound was checked")

    def test_local_url_rewrite_cannot_launch_an_external_transport_helper(self):
        receipt = self.export()
        marker = self.root / "external-helper-ran"
        helper = self.root / "external-helper"
        helper.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(1)\n")
        helper.chmod(0o700)
        git(self.target, "config", "protocol.ext.allow", "always")
        git(self.target, "config", f"url.ext::{helper} .insteadOf", tempfile.gettempdir() + "/")
        with self.assertRaises(ValueError):
            remote_snapshot.import_snapshot(self.target, receipt, self.artifacts / "snapshot.bundle")
        self.assertFalse(marker.exists(), "Local URL rewrite escaped the file-only transport boundary")

    def test_export_git_failures_stop_owned_group_and_close_pipes(self):
        helper = self.root / "owned-git-helper.py"
        helper.write_text("""import json, os, signal, sys
ready, release = map(int, sys.argv[1:3])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child_ready, child_publish = os.pipe()
child = os.fork()
if child == 0:
    os.close(child_ready)
    os.close(ready)
    os.close(release)
    os.write(child_publish, b'ready')
    os.close(child_publish)
    while True:
        signal.pause()
os.close(child_publish)
os.read(child_ready, 5)
os.close(child_ready)
os.write(ready, json.dumps([os.getpid(), child]).encode())
os.close(ready)
os.read(release, 1)
os.close(release)
if sys.argv[3] == 'output':
    os.write(1, b'x' * (512 * 1024 + 1))
if sys.argv[3] == 'stderr':
    os.write(2, b'x' * (64 * 1024 + 1))
while True:
    signal.pause()
""")
        original_popen, original_killpg = subprocess.Popen, os.killpg
        for mode in ("deadline", "output", "stderr", "interrupt"):
            with self.subTest(mode=mode), ExitStack() as cleanup:
                ready_read, ready_write = os.pipe()
                release_read, release_write = os.pipe()
                for descriptor in (ready_read, ready_write, release_read, release_write):
                    cleanup.callback(os.close, descriptor)
                launched, identities = [], []

                def stop(process):
                    try:
                        original_killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=15)
                    process.stdout.close()
                    process.stderr.close()

                fixture = (ready_read, ready_write, release_read, release_write, mode, launched, identities)

                def launch(_arguments, fixture=fixture, **options):
                    ready_read, ready_write, release_read, release_write, mode, launched, identities = fixture
                    process = original_popen([sys.executable, str(helper), str(ready_write), str(release_read), mode],
                                             pass_fds=(ready_write, release_read), **options)
                    cleanup.callback(stop, process)
                    launched.append(process)
                    self.assertTrue(select.select([ready_read], [], [], 15)[0], "Owned helper did not publish readiness")
                    identities.extend(json.loads(os.read(ready_read, 4096)))
                    self.assertEqual(identities[0], process.pid)
                    self.assertEqual(os.getpgid(identities[1]), process.pid)
                    os.write(release_write, b"go")
                    return process

                expected = KeyboardInterrupt if mode == "interrupt" else ValueError
                with ExitStack() as boundaries:
                    boundaries.enter_context(patch.object(remote_snapshot.subprocess, "Popen", side_effect=launch))
                    if mode == "deadline":
                        boundaries.enter_context(patch.object(remote_snapshot, "GIT_TIMEOUT", 0))
                    if mode == "interrupt":
                        boundaries.enter_context(patch.object(remote_snapshot.selectors.DefaultSelector, "select", side_effect=KeyboardInterrupt))
                    with self.assertRaises(expected):
                        self.export()
                self.assertEqual(len(launched), 1)
                process = launched[0]
                self.assertIsNotNone(process.poll())
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
                deadline = time.monotonic() + 5
                while set(identities) & sample().keys() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(set(identities) & sample().keys(), "Owned leader or descendant survived Git failure")


if __name__ == "__main__":
    unittest.main()
