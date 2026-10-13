import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from router.snapshot import apply_delta, capture_final, git, prepare


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-router-git-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / ".gitignore").write_text(".venv/\nnode_modules/\n.env\n")
        (self.repo / "tracked.txt").write_text("first\nsecond\nthird\nfourth\nfifth\n")
        (self.repo / "deleted.txt").write_text("delete me\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")

    def test_dirty_snapshot_and_all_or_nothing_integration(self):
        tracked = self.repo / "tracked.txt"
        tracked.write_text(tracked.read_text() + "staged\n")
        git(self.repo, "add", "tracked.txt")
        tracked.write_text(tracked.read_text() + "unstaged\n")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "untracked.txt").write_text("parent new\n")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "ignored.txt").write_text("ignore\n")
        status_before = git(self.repo, "status", "--porcelain")
        index = self.repo / git(self.repo, "rev-parse", "--git-path", "index").strip()
        index_before = hashlib.sha256(index.read_bytes()).digest()
        info = prepare(self.repo, "task-test", self.root / "task-test")
        child = Path(info["worktree"])
        self.addCleanup(lambda: subprocess.run(["git", "worktree", "remove", "--force", str(child)], cwd=self.repo, capture_output=True, check=False))
        self.assertEqual((child / "tracked.txt").read_text(), tracked.read_text())
        self.assertFalse((child / "deleted.txt").exists())
        self.assertFalse((child / "node_modules").exists())
        self.assertEqual(hashlib.sha256(index.read_bytes()).digest(), index_before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), status_before)
        (child / "tracked.txt").write_text("child\n" + (child / "tracked.txt").read_text().split("\n", 1)[1])
        git(child, "add", "tracked.txt")
        git(child, "commit", "-qm", "child commit")
        (child / "tracked.txt").write_text((child / "tracked.txt").read_text() + "uncommitted\n")
        (child / "child.bin").write_bytes(bytes(range(256)))
        (child / "untracked.txt").unlink()
        final = capture_final(self.repo, info, 1, self.root / "task-test")
        patch = Path(final["patch"])
        # A required context change rejects the whole patch, including child.bin.
        tracked.write_text(tracked.read_text().replace("second\n", "parent context\n"))
        with self.assertRaises(ValueError):
            apply_delta(self.repo, patch)
        self.assertFalse((self.repo / "child.bin").exists())
        self.assertEqual(hashlib.sha256(index.read_bytes()).digest(), index_before)
        tracked.write_text((child / "tracked.txt").read_text().replace("child\n", "first\n", 1).removesuffix("uncommitted\n"))
        (self.repo / "unrelated.txt").write_text("later parent edit\n")
        apply_delta(self.repo, patch)
        self.assertEqual((self.repo / "child.bin").read_bytes(), bytes(range(256)))
        self.assertFalse((self.repo / "untracked.txt").exists())
        self.assertEqual((self.repo / "unrelated.txt").read_text(), "later parent edit\n")
        self.assertEqual(hashlib.sha256(index.read_bytes()).digest(), index_before)


if __name__ == '__main__':
    unittest.main()
