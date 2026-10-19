"""Bounded SSH control for Router-owned remote children.

This module is intentionally a transport boundary.  It does not retry a start,
fall back to local execution, or interpret an unavailable host as an empty result.
The remote daemon owns task state; callers preserve the same request identity when
reconnecting and query status before attempting any new action.
"""

import base64
import json
import math
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from router.correlation import identifier
from router.dispatch import canonical
from router.fleet import FleetHost

MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 60.0
REMOTE_OPERATIONS = frozenset({"start", "status", "review", "cancel", "close"})
REVIEW_OUTCOMES = frozenset({"accepted", "rejected", "continue"})
DISPOSITIONS = frozenset({"integrated", "discarded"})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Remote child request contains duplicate fields")
        result[key] = value
    return result


def _reject_number(_value):
    raise ValueError("Remote child request contains a non-integer number")


def parse_request(raw: bytes) -> dict:
    """Parse the SSH request without discarding duplicate or nonfinite fields."""
    result = json.loads(raw, object_pairs_hook=_unique_object,
                        parse_constant=_reject_number, parse_float=_reject_number)
    if not isinstance(result, dict):
        raise TypeError("Remote child request must be an object")
    return result


class RemoteChildError(ValueError):
    """A typed remote-child transport or protocol failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limit: int = 240) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or value != value.strip():
        raise ValueError(f"{field} must be a bounded non-empty string")
    identifier(value, field, limit)
    return value


def _absolute_path(value: object, field: str) -> str:
    value = _text(value, field, 2000)
    path = Path(value)
    if not path.is_absolute() or "\x00" in value:
        raise ValueError(f"{field} must be an absolute path")
    return value


def _receipt(value: object) -> dict:
    if not isinstance(value, dict):
        raise TypeError("snapshot_receipt must be an object")
    required = {"schema_version", "task_id", "snapshot_commit", "snapshot_tree", "object_format",
                "paths", "file_count", "total_bytes", "byte_count", "sha256"}
    if set(value) != required or value.get("schema_version") != 1:
        raise ValueError("snapshot_receipt has an invalid schema")
    _text(value.get("task_id"), "snapshot task_id", 128)
    length = 40 if value.get("object_format") == "sha1" else 64 if value.get("object_format") == "sha256" else 0
    if not length or any(not isinstance(value.get(key), str) or len(value[key]) != length
                         or any(char not in "0123456789abcdef" for char in value[key])
                         for key in ("snapshot_commit", "snapshot_tree")):
        raise ValueError("snapshot_receipt has invalid object IDs")
    return value


def _start_payload(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {
            "session_id", "request_id", "invocation", "remote_repo", "remote_worktree",
            "snapshot_receipt", "snapshot_base64"}:
        raise ValueError("Remote start has invalid fields")
    _text(payload["session_id"], "session_id")
    _text(payload["request_id"], "request_id")
    _absolute_path(payload["remote_repo"], "remote_repo")
    _absolute_path(payload["remote_worktree"], "remote_worktree")
    invocation = payload["invocation"]
    if not isinstance(invocation, dict):
        raise TypeError("Remote start invocation must be an object")
    if set(invocation) - {"request_version", "request_id", "instruction_id", "executor", "model", "task",
                          "parent_repo", "parent_worktree", "mode", "context", "work_item_id",
                          "host", "remote_repo", "remote_worktree"}:
        raise ValueError("Remote start invocation has unknown fields")
    if invocation.get("request_id") != payload["request_id"]:
        raise ValueError("Remote start request identity mismatch")
    _receipt(payload["snapshot_receipt"])
    encoded = payload["snapshot_base64"]
    if not isinstance(encoded, str) or len(encoded) > 4 * ((6 * 1024 * 1024 + 2) // 3):
        raise ValueError("Remote snapshot encoding exceeds its bound")
    try:
        bundle = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError("Remote snapshot encoding is invalid") from error
    receipt = payload["snapshot_receipt"]
    if len(bundle) != receipt["byte_count"]:
        raise ValueError("Remote snapshot bytes do not match the receipt")
    return {**payload, "snapshot_bytes": bundle}


def _control_payload(operation: str, payload: dict) -> dict:
    if operation == "status":
        if not isinstance(payload, dict) or set(payload) != {"session_id", "request_id"}:
            raise ValueError("Remote status has invalid fields")
        _text(payload["session_id"], "session_id")
        _text(payload["request_id"], "request_id")
    elif operation == "review":
        if not isinstance(payload, dict) or set(payload) != {"task_id", "request_id", "expected_attempt", "outcome", "feedback"}:
            raise ValueError("Remote review has invalid fields")
        _text(payload["task_id"], "task_id", 128)
        _text(payload["request_id"], "request_id")
        if type(payload["expected_attempt"]) is not int or payload["expected_attempt"] < 1:
            raise ValueError("Remote review attempt is invalid")
        if payload["outcome"] not in REVIEW_OUTCOMES:
            raise ValueError("Remote review outcome is invalid")
        if payload["outcome"] != "accepted":
            _text(payload["feedback"], "feedback", 2000)
        elif not isinstance(payload["feedback"], str) or payload["feedback"]:
            raise ValueError("Accepted remote review must not include feedback")
    elif operation == "cancel":
        if not isinstance(payload, dict) or set(payload) != {"task_id", "request_id"}:
            raise ValueError("Remote cancel has invalid fields")
        _text(payload["task_id"], "task_id", 128)
        _text(payload["request_id"], "request_id")
    else:
        if not isinstance(payload, dict) or set(payload) != {"task_id", "request_id", "disposition", "keep_artifacts"}:
            raise ValueError("Remote close has invalid fields")
        _text(payload["task_id"], "task_id", 128)
        _text(payload["request_id"], "request_id")
        if payload["disposition"] not in DISPOSITIONS or type(payload["keep_artifacts"]) is not bool:
            raise ValueError("Remote close disposition is invalid")
    return payload


def _command(host: FleetHost, timeout: float) -> list[str]:
    if host.transport != "ssh" or not isinstance(host.target, str) or not host.target:
        raise ValueError("Remote child requires an SSH fleet host")
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, math.ceil(timeout))}", "--", host.target,
            "router", "remote", "--json"]


def _exchange(host: FleetHost, timeout: float, operation: str, payload: dict) -> dict:
    if operation not in REMOTE_OPERATIONS:
        raise ValueError("Unsupported remote child operation")
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS or not math.isfinite(timeout):
        raise ValueError("Remote child timeout is out of bounds")
    request = canonical({"operation": operation, "payload": payload}).encode() + b"\n"
    if len(request) > MAX_REQUEST_BYTES:
        raise ValueError("Remote child request exceeds its wire bound")
    try:
        process = subprocess.Popen(_command(host, timeout), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        raise RemoteChildError("spawn_failed", "Unable to start the remote transport") from error
    deadline = time.monotonic() + timeout
    output, diagnostic = bytearray(), bytearray()
    selector = selectors.DefaultSelector()
    try:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RemoteChildError("transport_failed", "Remote transport pipes unavailable")
        process.stdin.write(request)
        process.stdin.close()
        for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, kind)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteChildError("deadline", "Remote child request exceeded its deadline")
            for key, _events in selector.select(min(remaining, .25)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = output if key.data == "stdout" else diagnostic
                if target is output:
                    if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise RemoteChildError("output_limit", "Remote child response exceeds its wire bound")
                    output.extend(chunk)
                elif len(diagnostic) < 65536:
                    diagnostic.extend(chunk[:65536 - len(diagnostic)])
        process.wait(timeout=max(.001, deadline - time.monotonic()))
    except RemoteChildError:
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemoteChildError("transport_failed", "Remote child transport failed") from error
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
    if process.returncode != 0:
        raise RemoteChildError("exit_failed", "Remote Router rejected the request")
    try:
        response = json.loads(bytes(output), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RemoteChildError("invalid_response", "Remote Router returned invalid JSON") from error
    if not isinstance(response, dict) or set(response) != {"ok", "result"} or response.get("ok") is not True:
        raise RemoteChildError("invalid_response", "Remote Router returned an invalid response envelope")
    if not isinstance(response["result"], dict):
        raise RemoteChildError("invalid_response", "Remote Router returned an invalid result")
    return response["result"]


class RemoteChildTransport:
    def __init__(self, host: FleetHost, timeout: float):
        self.host = host
        self.timeout = timeout

    def start(self, *, session_id: str, request_id: str, invocation: dict, remote_repo: str,
              remote_worktree: str, snapshot_receipt: dict, snapshot_bundle: bytes) -> dict:
        payload = {"session_id": session_id, "request_id": request_id, "invocation": invocation,
                   "remote_repo": remote_repo, "remote_worktree": remote_worktree,
                   "snapshot_receipt": snapshot_receipt,
                   "snapshot_base64": base64.b64encode(snapshot_bundle).decode("ascii")}
        return _exchange(self.host, self.timeout, "start", _start_payload(payload))

    def status(self, session_id: str, request_id: str) -> dict:
        return _exchange(self.host, self.timeout, "status", _control_payload("status", {
            "session_id": session_id, "request_id": request_id}))

    def review(self, payload: dict) -> dict:
        return _exchange(self.host, self.timeout, "review", _control_payload("review", payload))

    def close(self, payload: dict) -> dict:
        return _exchange(self.host, self.timeout, "close", _control_payload("close", payload))

    def cancel(self, payload: dict) -> dict:
        return _exchange(self.host, self.timeout, "cancel", _control_payload("cancel", payload))
