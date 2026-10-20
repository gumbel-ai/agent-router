"""Bounded local launch facts and declared mechanical completion requirements.

No provider probes, credential reads, transcript scans, semantic inference, or acceptance.
"""

import errno
import json
import os
import shutil
import socket
import stat
from pathlib import Path

MAX_JSON_BYTES = 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _open_file(root: Path, relative: str) -> int:
    """Open a declared regular file without following any symlink or waiting on a FIFO."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            mode = os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                raise ValueError(f"{relative} has a non-directory ancestor")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(os.fstat(child).st_mode):
        os.close(child)
        raise ValueError(f"{relative} must be a regular file without symlinks")
    return child


def _writable(root: Path, relative: str) -> bool:
    candidate, existing = root, root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            break
        except PermissionError as error:
            raise ValueError(f"Declared output {relative} is inaccessible") from error
        if stat.S_ISLNK(mode):
            raise ValueError(f"Declared output {relative} contains a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError(f"Declared output {relative} has a non-directory parent")
        if index == len(parts) - 1 and not stat.S_ISREG(mode):
            raise ValueError(f"Declared output {relative} must name a file")
        existing = candidate
    return os.access(existing, os.W_OK | (os.X_OK if existing.is_dir() else 0))


def preflight(task: dict, command: list[str], environment: dict[str, str], root: Path) -> None:
    """Refuse only facts this process can prove, immediately before child launch."""
    if not root.is_dir():
        raise ValueError(f"Launch cwd does not exist: {root}")
    search = os.pathsep.join(str(root / entry) if not Path(entry).is_absolute() else entry
                             for entry in os.get_exec_path(environment))
    executable = command[0]
    selected = str(root / executable) if os.path.dirname(executable) else executable
    if shutil.which(selected, path=search) is None:
        raise ValueError(f"Configured {task['executor']} executable is unavailable: {executable}")
    if task["executor"] == "codex" and environment.get("CODEX_HOME"):
        home = Path(environment["CODEX_HOME"]).expanduser()
        home = root / home if not home.is_absolute() else home
        if home.exists() and not home.is_dir():
            raise ValueError("CODEX_HOME names a file, not a directory")
        # Missing auth.json is not proof of missing authentication. Do not read it.
    launch = task["invocation"].get("launch", {})
    if launch.get("requires_router"):
        state = Path(environment.get("AGENT_ROUTER_STATE_DIR", "~/.agent-router")).expanduser()
        if not state.is_absolute():
            state = root / state
        configured_endpoint = environment.get("AGENT_ROUTER_SOCKET")
        endpoint = (Path(configured_endpoint).expanduser()
                    if configured_endpoint is not None else state / "router.sock")
        if not endpoint.is_absolute():
            endpoint = root / endpoint
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(.01)
            try:
                connection.connect(str(endpoint))
            except TimeoutError:
                pass  # Timeout is uncertain, not proof that a valid launch is impossible.
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ECONNREFUSED, errno.EACCES, errno.ENOTSOCK}:
                    raise ValueError(f"Required Router socket is unavailable: {endpoint}") from error
    for path in launch.get("task_packets", []):
        try:
            os.close(_open_file(root, path))
        except (OSError, ValueError) as error:
            raise ValueError(f"Task packet {path} is not a readable regular file: {error}") from error
    outputs = launch.get("writable_paths", [])
    if outputs and task["executor"] == "claude":
        tools = task.get("permission_profile", {}).get("tools")
        if isinstance(tools, list) and not set(tools) & {"Write", "Edit", "Bash", "NotebookEdit"}:
            raise ValueError("Claude permission profile has no tool capable of writing declared outputs")
    if outputs and task["executor"] == "codex" and task.get("permission_profile", {}).get("sandbox") == "read-only":
        raise ValueError("Codex read-only sandbox cannot write declared outputs")
    for path in outputs:
        if not _writable(root, path):
            raise ValueError(f"Declared output path is not writable: {path}")


def completion(task: dict) -> dict:
    """Check only declared requirements; UNKNOWN never becomes satisfied or accepted."""
    contract = task["invocation"].get("completion")
    if contract is None:
        return {"status": "not_declared", "violations": [], "unchecked": []}
    violations, unchecked = [], []
    root = Path(task["worktree"])
    paths = dict.fromkeys((str(Path(path)) for path in contract.get("files", [])), False)
    paths.update(dict.fromkeys((str(Path(path)) for path in contract.get("json_files", [])), True))
    status_file = contract.get("setup_status_file")
    if status_file is not None:
        status_file = str(Path(status_file))
        paths[status_file] = True
    for path, parse in paths.items():
        try:
            with os.fdopen(_open_file(root, path), "rb") as stream:
                before = os.fstat(stream.fileno())
                raw = stream.read(MAX_JSON_BYTES + 1) if parse else None
                after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                unchecked.append({"path": path, "reason": "changed during read"})
            elif parse and len(raw) > MAX_JSON_BYTES:
                unchecked.append({"path": path, "reason": "JSON exceeds the 1 MiB check limit"})
            elif parse:
                value = json.loads(raw, object_pairs_hook=_unique_object,
                                   parse_constant=_reject_constant)
                if path == status_file:
                    status = value.get("status") if isinstance(value, dict) else None
                    if status == "SETUP_FAILED":
                        violations.append({"path": path, "reason": "explicit setup failure"})
                    elif status != "READY":
                        unchecked.append({"path": path, "reason": "setup status is neither READY nor SETUP_FAILED"})
        except FileNotFoundError:
            violations.append({"path": path, "reason": "required file missing"})
        except (ValueError, UnicodeError):
            violations.append({"path": path, "reason": "invalid JSON or non-regular artifact"})
        except (OSError, RecursionError):
            unchecked.append({"path": path, "reason": "artifact could not be checked"})
    if contract.get("final_response"):
        present = task.get("completion_final_response_present")
        if present is False:
            violations.append({"field": "final_response", "reason": "required final response missing"})
        elif present is not True:
            unchecked.append({"field": "final_response", "reason": "final response evidence unavailable"})
    return {"status": "violated" if violations else "unknown" if unchecked else "satisfied",
            "violations": violations, "unchecked": unchecked, "evidence_source": "stopped_worktree"}


def completion_projection(value: object) -> dict:
    """Bound remote diagnostics; malformed or absent evidence remains UNKNOWN."""
    unknown = {"status": "unknown", "violations": [],
               "unchecked": [{"reason": "remote completion evidence unavailable"}]}
    if not isinstance(value, dict) or set(value) - {"status", "violations", "unchecked", "evidence_source"}:
        return unknown
    for name in ("violations", "unchecked"):
        rows = value.get(name)
        if (not isinstance(rows, list) or len(rows) > 66 or any(
                not isinstance(row, dict) or set(row) - {"path", "field", "reason"}
                or not isinstance(row.get("reason"), str) or not row["reason"]
                or any(not isinstance(text, str) or len(text) > 2048 for text in row.values())
                for row in rows)):
            return unknown
    expected = "violated" if value["violations"] else "unknown" if value["unchecked"] else "satisfied"
    if value.get("status") != expected or value.get("evidence_source") != "stopped_worktree":
        return unknown
    return {"status": expected, "violations": value["violations"], "unchecked": value["unchecked"],
            "evidence_source": "stopped_worktree"}
