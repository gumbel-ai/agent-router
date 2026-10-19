"""Dirty-working-tree snapshots and parent-controlled, plain patch integration."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from router.child import strip_langfuse_environment


def git(root: Path, *args: str, env: dict | None = None, binary: bool = False) -> str | bytes:
    env = strip_langfuse_environment(os.environ if env is None else env)
    result = subprocess.run(["git", "-C", str(root), *args], env=env, check=False, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "Git operation failed")
    return result.stdout if binary else result.stdout.decode()


def validate_repository(root: Path) -> None:
    if Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root.resolve():
        raise ValueError("parent_worktree must name the Git worktree root")
    git(root, "rev-parse", "--verify", "HEAD")
    if git(root, "ls-files", "-u").strip():
        raise ValueError("Unresolved Git merges are unsupported")
    if any(line.startswith("160000 ") for line in git(root, "ls-files", "--stage").splitlines()):
        raise ValueError("Submodules are unsupported")
    result = subprocess.run(["git", "-C", str(root), "config", "--bool", "core.sparseCheckout"],
                            env=strip_langfuse_environment(os.environ), capture_output=True, check=False)
    if result.stdout.strip() == b"true":
        raise ValueError("Sparse checkouts are unsupported")


def capture_tree(root: Path, scratch: Path) -> str:
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="index-", dir=scratch) as temp:
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(Path(temp) / "index")
        git(root, "read-tree", "HEAD", env=environment)
        git(root, "add", "-A", "--", ".", env=environment)
        return git(root, "write-tree", env=environment).strip()


def prepare(root: Path, task_id: str, directory: Path) -> dict:
    validate_repository(root)
    tree = capture_tree(root, directory)
    environment = os.environ.copy()
    environment.update(GIT_AUTHOR_NAME="Agent Router", GIT_AUTHOR_EMAIL="router@localhost",
                       GIT_COMMITTER_NAME="Agent Router", GIT_COMMITTER_EMAIL="router@localhost")
    commit = git(root, "commit-tree", tree, "-p", "HEAD", "-m", f"Agent Router snapshot {task_id}", env=environment).strip()
    git(root, "update-ref", f"refs/router/snap/{task_id}", commit)
    branch = f"agent/{task_id}"
    worktree = directory / "worktree"
    git(root, "worktree", "add", "-b", branch, str(worktree), commit)
    return {"task_id": task_id, "snapshot_commit": commit, "snapshot_tree": tree, "worktree": str(worktree), "branch": branch}


def capture_final(root: Path, info: dict, attempt: int, directory: Path) -> dict:
    tree = capture_tree(Path(info["worktree"]), directory)
    git(root, "update-ref", f"refs/router/final/{info['task_id']}/{attempt}", tree)
    patch = directory / f"attempt-{attempt}.patch"
    patch.write_bytes(git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", info["snapshot_tree"], tree, binary=True))
    return {"final_tree": tree, "patch": str(patch)}


def apply_delta(root: Path, patch: Path) -> None:
    if not patch.read_bytes():
        return
    git(root, "apply", "--check", str(patch))
    git(root, "apply", str(patch))


def cleanup(root: Path, info: dict) -> None:
    worktree = Path(info["worktree"])
    registered = f"worktree {worktree}\n" in git(root, "worktree", "list", "--porcelain")
    if registered:
        git(root, "worktree", "remove", "--force", str(worktree))
    branch = info["branch"]
    if git(root, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}").strip():
        git(root, "branch", "-D", branch)
    task_id = info["task_id"]
    refs = [f"refs/router/snap/{task_id}"]
    refs += git(root, "for-each-ref", "--format=%(refname)", f"refs/router/final/{task_id}/").splitlines()
    for ref in refs:
        git(root, "update-ref", "-d", ref)


def main() -> None:
    action, source, destination = sys.argv[1:]
    info = json.loads(Path(source).read_text())
    root = Path(info["invocation"]["parent_worktree"])
    directory = Path(info["artifact_dir"])
    if action == "prepare":
        result = prepare(root, info["task_id"], directory)
    else:
        result = capture_final(root, info, info["attempt_number"], directory)
    Path(destination).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
