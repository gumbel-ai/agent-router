"""Bounded, explicit retrieval of retained task patches; never applies or launches work."""

import base64
import binascii
import errno
import hashlib
import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from router.correlation import identifier

MAX_PATCH_BYTES = 256 * 1024
MAX_WIRE_BYTES = 512 * 1024
MAX_ATTEMPT = 2**31 - 1
OBJECT_FIELDS = ("snapshot_commit", "snapshot_tree", "final_tree")
ERRORS = frozenset({
    "task_unknown", "attempt_unknown", "attempt_active", "state_unknown", "capture_unavailable",
    "artifact_missing", "artifact_unsafe", "artifact_unstable", "artifact_oversize", "artifact_unavailable",
    "identity_mismatch", "invalid_response", "request_failed", "transport_unavailable", "transport_deadline",
    "ssh_spawn_failed", "ssh_exit_failed", "ssh_deadline", "ssh_output_limit", "ssh_transport_failed", "ssh_interrupted",
    "ssh_cleanup_failed",
})
STOPPED = frozenset({"awaiting_review", "blocked", "failed", "cancelled", "accepted", "integrated", "closed"})
ACTIVE = frozenset({"created", "validating", "preparing", "running", "retrying", "cancelling"})


def _hex(value: object, lengths: tuple[int, ...]) -> bool:
    return (isinstance(value, str) and len(value) in lengths
            and all(character in "0123456789abcdef" for character in value))


def options(payload: dict) -> dict:
    """Validate caller selection before touching a file or transport."""
    if not isinstance(payload, dict) or set(payload) - {"task_id", "attempt_number", "host_id", *OBJECT_FIELDS}:
        raise ValueError("Unknown task-diff option")
    task_id = identifier(payload.get("task_id"), "task_id", 128)
    if task_id in {".", ".."} or "/" in task_id or "\\" in task_id:
        raise ValueError("task_id must be one task identity, not a path")
    attempt = payload.get("attempt_number")
    if type(attempt) is not int or not 1 <= attempt <= MAX_ATTEMPT:
        raise ValueError("attempt_number must be an integer from 1 to 2147483647")
    if "host_id" in payload:
        identifier(payload["host_id"], "host_id", 128)
    for field in OBJECT_FIELDS:
        if field in payload and (not _hex(payload[field], (40, 64)) or not any(c != "0" for c in payload[field])):
            raise ValueError(f"{field} must be a nonzero lowercase Git object ID")
    return dict(payload)


def unavailable(selection: dict, host_id: str | None, code: str) -> dict:
    return {"schema_version": 1, "status": "unavailable", "host_id": host_id,
            "task_id": selection["task_id"], "attempt_number": selection["attempt_number"], "error": {"code": code}}


class _ArtifactError(ValueError):
    pass


def _signature(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _read_patch(directory: Path, task_id: str, attempt: int) -> bytes:
    try:
        with ExitStack() as stack:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(directory, directory_flags)
            stack.callback(os.close, descriptor)
            for component in ("tasks", task_id):
                descriptor = os.open(component, directory_flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
            filename = f"attempt-{attempt}.patch"
            patch = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            stack.callback(os.close, patch)
            before = os.fstat(patch)
            if not stat.S_ISREG(before.st_mode):
                raise _ArtifactError("artifact_unsafe")
            if before.st_size > MAX_PATCH_BYTES:
                raise _ArtifactError("artifact_oversize")
            content = bytearray()
            while len(content) <= MAX_PATCH_BYTES:
                chunk = os.read(patch, min(65536, MAX_PATCH_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_PATCH_BYTES:
                raise _ArtifactError("artifact_oversize")
            after = os.fstat(patch)
            current = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
            if (_signature(before) != _signature(after) or _signature(after) != _signature(current)
                    or len(content) != before.st_size):
                raise _ArtifactError("artifact_unstable")
            return bytes(content)
    except OSError as error:
        code = ("artifact_missing" if error.errno == errno.ENOENT else
                "artifact_unsafe" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "artifact_unavailable")
        raise _ArtifactError(code) from error


def export(store, directory: Path, host_id: str, payload: dict) -> dict:
    selection = options(payload)
    identifier(host_id, "host_id", 128)

    def failed(code):
        return unavailable(selection, host_id, code)

    if selection.get("host_id", host_id) != host_id:
        return failed("identity_mismatch")
    task_id, attempt = selection["task_id"], selection["attempt_number"]
    with store.snapshot() as db:
        task_row = db.execute("SELECT data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task_row is None:
            return failed("task_unknown")
        frame_row = db.execute("SELECT data FROM attempts WHERE task_id=? AND number=?", (task_id, attempt)).fetchone()
        try:
            task = json.loads(task_row[0])
            frame = json.loads(frame_row[0]) if frame_row else None
        except (ValueError, TypeError, RecursionError):
            return failed("capture_unavailable")
        if not isinstance(task, dict) or task.get("task_id") != task_id:
            return failed("capture_unavailable")
        current = task.get("attempt_number") == attempt
        evidence = task if current else frame
        if isinstance(evidence, dict):
            status = evidence.get("status")
            if isinstance(status, str) and status in ACTIVE:
                return failed("attempt_active")
            if evidence.get("execution_stopped") is not True or not isinstance(status, str) or status not in STOPPED:
                return failed("state_unknown")
        if frame is None:
            return failed("attempt_unknown")
        if (not isinstance(frame, dict) or frame.get("task_id") != task_id
                or type(frame.get("attempt_number")) is not int or frame["attempt_number"] != attempt):
            return failed("capture_unavailable")
        if (type(task.get("attempt_number")) is not int or not attempt <= task["attempt_number"] <= MAX_ATTEMPT):
            return failed("capture_unavailable")
        if (frame.get("execution_stopped") is not True or not isinstance(frame.get("status"), str)
                or frame["status"] not in STOPPED):
            return failed("state_unknown")
        if any(not _hex(frame.get(field), (40, 64)) or not any(c != "0" for c in frame[field]) for field in OBJECT_FIELDS):
            return failed("capture_unavailable")
        if len({len(frame[field]) for field in OBJECT_FIELDS}) != 1:
            return failed("capture_unavailable")
        if current and any(task.get(field) != frame[field] for field in OBJECT_FIELDS):
            return failed("capture_unavailable")
        if any(selection.get(field, frame[field]) != frame[field] for field in OBJECT_FIELDS):
            return failed("identity_mismatch")
        artifact_dir = directory / "tasks" / task_id
        expected_patch = artifact_dir / f"attempt-{attempt}.patch"
        if frame.get("artifact_dir") != str(artifact_dir) or frame.get("patch") != str(expected_patch):
            return failed("artifact_unsafe")
        if current and (task.get("artifact_dir") != str(artifact_dir) or task.get("patch") != str(expected_patch)):
            return failed("artifact_unsafe")
        try:
            content = _read_patch(directory, task_id, attempt)
        except _ArtifactError as error:
            return failed(str(error))
        if bool(content) != (frame["snapshot_tree"] != frame["final_tree"]):
            return failed("capture_unavailable")
    return {"schema_version": 1, "status": "ok", "host_id": host_id, "task_id": task_id,
            "attempt_number": attempt, **{field: frame[field] for field in OBJECT_FIELDS},
            "byte_count": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "patch_base64": base64.b64encode(content).decode("ascii")}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate task-diff field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("Task-diff numbers must be integers")


class TaskDiffProtocolError(ValueError):
    """A bounded task-diff reply failed raw framing, JSON or envelope validation."""


def parse_json(raw: bytes):
    """Keep raw bounds and duplicate evidence before any local reserialization."""
    if not isinstance(raw, bytes) or len(raw) > MAX_WIRE_BYTES:
        raise TaskDiffProtocolError("Task-diff response exceeds its wire bound")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant, parse_float=_constant)
    except (ValueError, RecursionError) as error:
        raise TaskDiffProtocolError("Invalid task-diff JSON") from error


def decode(raw: bytes, payload: dict) -> dict:
    selection = options(payload)
    result = parse_json(raw)
    common = {"schema_version", "status", "host_id", "task_id", "attempt_number"}
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise ValueError("Invalid task-diff version")
    if (result.get("task_id") != selection["task_id"] or type(result.get("attempt_number")) is not int
            or result["attempt_number"] != selection["attempt_number"]):
        raise ValueError("Task-diff task or attempt identity mismatch")
    host_id = result.get("host_id")
    if host_id is not None:
        identifier(host_id, "host_id", 128)
    if result.get("status") == "unavailable":
        if (set(result) != common | {"error"} or not isinstance(result["error"], dict)
                or set(result["error"]) != {"code"} or not isinstance(result["error"]["code"], str)
                or result["error"]["code"] not in ERRORS):
            raise ValueError("Invalid task-diff error receipt")
        if ("host_id" in selection and host_id not in {None, selection["host_id"]}
                and result["error"]["code"] != "identity_mismatch"):
            raise ValueError("Task-diff serving host mismatch")
        return result
    if result.get("status") != "ok" or set(result) != common | {*OBJECT_FIELDS, "byte_count", "sha256", "patch_base64"}:
        raise ValueError("Invalid task-diff receipt")
    identifier(host_id, "host_id", 128)
    if selection.get("host_id", host_id) != host_id:
        raise ValueError("Task-diff serving host mismatch")
    for field in OBJECT_FIELDS:
        if (not _hex(result[field], (40, 64)) or not any(c != "0" for c in result[field])
                or selection.get(field, result[field]) != result[field]):
            raise ValueError("Task-diff snapshot or final identity mismatch")
    if len({len(result[field]) for field in OBJECT_FIELDS}) != 1:
        raise ValueError("Inconsistent task-diff object IDs")
    size, encoded = result["byte_count"], result["patch_base64"]
    if type(size) is not int or not 0 <= size <= MAX_PATCH_BYTES or not _hex(result["sha256"], (64,)):
        raise ValueError("Invalid task-diff byte count or digest")
    if bool(size) != (result["snapshot_tree"] != result["final_tree"]):
        raise ValueError("Task-diff empty patch contradicts its tree identities")
    if not isinstance(encoded, str) or len(encoded) != 4 * ((size + 2) // 3):
        raise ValueError("Invalid task-diff encoded size")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Invalid task-diff base64") from error
    if (len(content) != size or base64.b64encode(content).decode("ascii") != encoded
            or hashlib.sha256(content).hexdigest() != result["sha256"]):
        raise ValueError("Task-diff content does not match its receipt")
    return result


def fetch(payload: dict, *, host=None, timeout: float = 15) -> dict:
    from router.client import request
    from router.fleet import SSHQueryError, ssh_stdout

    selection = options(payload)
    if host is not None and "host_id" not in selection:
        raise ValueError("Fleet task-diff requires the expected serving host_id")
    if host is not None and host.transport not in {"local", "ssh"}:
        raise ValueError("Unsupported task-diff transport")
    try:
        if host is None or host.transport == "local":
            response = request("task_diff", selection, timeout=timeout, deadline=True)
            raw = json.dumps(response, separators=(",", ":"), allow_nan=False).encode()
        else:
            arguments = ["task-diff", selection["task_id"], "--attempt", str(selection["attempt_number"]), "--json"]
            for key in ("host_id", *OBJECT_FIELDS):
                if key in selection:
                    arguments.extend(["--" + key.replace("_", "-"), selection[key]])
            raw = ssh_stdout(host, timeout, arguments)
    except SSHQueryError as error:
        if error.code == "exit_failed" and error.exit_code == 1 and error.stdout is not None:
            try:
                refusal = decode(error.stdout, selection)
            except ValueError:
                return unavailable(selection, None, "invalid_response")
            if refusal["status"] == "unavailable":
                return refusal
            return unavailable(selection, None, "invalid_response")
        code = "ssh_" + error.code
        return unavailable(selection, None, code if code in ERRORS else "transport_unavailable")
    except TimeoutError:
        return unavailable(selection, None, "transport_deadline")
    except OSError:
        return unavailable(selection, None, "transport_unavailable")
    except TaskDiffProtocolError:
        return unavailable(selection, None, "invalid_response")
    except ValueError as error:
        return unavailable(selection, None, "transport_unavailable" if isinstance(error.__cause__, OSError) else "request_failed")
    try:
        return decode(raw, selection)
    except ValueError:
        return unavailable(selection, None, "invalid_response")
