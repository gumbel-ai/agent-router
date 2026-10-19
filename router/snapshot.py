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


def prepare_imported(root: Path, task_id: str, directory: Path, snapshot: dict) -> dict:
    """Prepare only the verified imported commit, never the receiving checkout."""
    from router.remote_snapshot import MAX_FILE_BYTES, _git, _oid, _task

    _task(task_id)
    if not isinstance(snapshot, dict) or set(snapshot) != {"task_id", "snapshot_commit", "snapshot_tree"}:
        raise ValueError("Invalid imported snapshot identity")
    _task(snapshot["task_id"])
    length = len(snapshot["snapshot_commit"]) if isinstance(snapshot["snapshot_commit"], str) else 0
    if length not in {40, 64} or any(not _oid(snapshot[field], length) for field in ("snapshot_commit", "snapshot_tree")):
        raise ValueError("Invalid imported snapshot object identity")
    commit, tree = snapshot["snapshot_commit"], snapshot["snapshot_tree"]
    reference = f"refs/router/snap/{snapshot['task_id']}"
    if (_git(root, "rev-parse", "--verify", reference).decode().strip() != commit
            or _git(root, "show", "-s", "--format=%P", commit).strip()
            or _git(root, "rev-parse", commit + "^{tree}").decode().strip() != tree):
        raise ValueError("Imported snapshot is missing, changed or not parentless")
    _git(root, "update-ref", f"refs/router/snap/{task_id}", commit, "0" * length)
    branch, worktree = f"agent/{task_id}", directory / "worktree"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Checkout and git add apply attributes, filters and encoding conversions.
    # A verified blob's bytes must reach the child unchanged instead.
    _git(root, "worktree", "add", "--no-checkout", "-b", branch, str(worktree), commit)
    _git(worktree, "read-tree", commit)
    for path, mode, object_id, size in _imported_files(root, commit):
        content = _git(root, "cat-file", "blob", object_id, limit=MAX_FILE_BYTES)
        if len(content) != size:
            raise ValueError("Imported snapshot blob size changed")
        destination = worktree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
        destination.chmod(0o755 if mode == b"100755" else 0o644)
    return {"task_id": task_id, "snapshot_commit": commit, "snapshot_tree": tree,
            "worktree": str(worktree), "branch": branch}


def _imported_files(root: Path, commit: str) -> list:
    from router.remote_snapshot import (
        MAX_FILE_BYTES,
        MAX_FILES,
        MAX_TOTAL_BYTES,
        _git,
        _path,
    )

    files, total = [], 0
    for entry in _git(root, "ls-tree", "-rzl", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id, size = metadata.split()
        path = _path(raw_path.decode("utf-8"))
        if mode not in {b"100644", b"100755"} or kind != b"blob" or not size.isdigit() or int(size) > MAX_FILE_BYTES:
            raise ValueError("Imported snapshot contains an unsupported object")
        total += int(size)
        if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
            raise ValueError("Imported snapshot tree exceeds its byte or file bound")
        files.append((path, mode, object_id.decode("ascii"), int(size)))
    return files


def capture_imported(root: Path, info: dict, attempt: int, directory: Path) -> dict:
    """Capture only declared result files as raw bytes; never run clean filters."""
    from router.remote_snapshot import (
        MAX_FILE_BYTES,
        MAX_FILES,
        MAX_PATCH_BYTES,
        MAX_TOTAL_BYTES,
        _git,
        _git_with_excludes,
        _path,
        _paths,
        _read_file,
    )

    original = _paths(info.get("remote_paths"))
    additional = _paths(info["remote_result_paths"]) if info.get("remote_result_paths") else []
    selected = sorted(set(original) | set(additional))
    if len(selected) > MAX_FILES:
        raise ValueError("Remote result selection exceeds its file bound")
    worktree = Path(info["worktree"])
    inventory = _git_with_excludes(worktree, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    visible = {_path(value.decode("utf-8")) for value in inventory if value}
    if not visible.issubset(selected):
        raise ValueError("Remote result contains undeclared nonignored files; worktree retained")
    ignored = _git_with_excludes(worktree, "check-ignore", "--no-index", "-z", "--stdin",
                                 data=b"\0".join(path.encode() for path in selected) + b"\0", statuses=(0, 1))
    if ignored:
        raise ValueError("Remote result selection includes an ignored file; worktree retained")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    total, records = 0, []
    with tempfile.TemporaryDirectory(prefix="remote-index-", dir=directory) as temporary:
        environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(worktree, "read-tree", "--empty", environment=environment)
        for path in selected:
            try:
                content, mode = _read_file(worktree, path, MAX_FILE_BYTES)
            except ValueError as error:
                if isinstance(error.__cause__, FileNotFoundError):
                    continue  # An explicitly selected file was deleted or never produced.
                raise
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Remote result files exceed the total byte bound")
            blob = _git(worktree, "hash-object", "--no-filters", "-w", "--stdin", data=content).strip()
            file_mode = b"100755" if mode & 0o100 else b"100644"
            records.append(file_mode + b" " + blob + b"\t" + path.encode() + b"\0")
        _git(worktree, "update-index", "-z", "--index-info", data=b"".join(records), environment=environment)
        tree = _git(worktree, "write-tree", environment=environment).decode().strip()
    _git(root, "update-ref", f"refs/router/final/{info['task_id']}/{attempt}", tree)
    patch = directory / f"attempt-{attempt}.patch"
    patch.write_bytes(_git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", info["snapshot_tree"], tree,
                           limit=MAX_PATCH_BYTES))
    return {"final_tree": tree, "patch": str(patch)}


def capture_final(root: Path, info: dict, attempt: int, directory: Path) -> dict:
    if "remote_snapshot" in info:
        return capture_imported(root, info, attempt, directory)
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
        result = (prepare_imported(root, info["task_id"], directory, info["remote_snapshot"])
                  if "remote_snapshot" in info else prepare(root, info["task_id"], directory))
    else:
        result = capture_final(root, info, info["attempt_number"], directory)
    Path(destination).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
