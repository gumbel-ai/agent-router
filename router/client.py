"""One request/response per local socket connection; never starts a daemon."""

import json
import socket
import time
from pathlib import Path

from router.dispatch import canonical
from router.paths import socket_path


class CompletionBusy(ValueError):
    """A completion request can be retried without changing command ownership."""


class CompletionRefused(ValueError):
    """The daemon rejected an acknowledgment's identity or cursor binding."""


def request(operation: str, payload: dict, endpoint: Path | None = None, *, timeout: float = 15,
            deadline: bool = False) -> dict | list:
    """With deadline=True, timeout bounds the whole request rather than each socket operation."""
    expires = time.monotonic() + timeout if deadline else None

    def remaining() -> float:
        if expires is None:
            return timeout
        budget = expires - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Router request deadline expired")
        return budget

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(remaining())
        try:
            client.connect(str(endpoint or socket_path()))
        except OSError as error:
            raise ValueError("Agent Router is not reachable. Run `router service start` (managed) or `router daemon start` (manual).") from error
        client.settimeout(remaining())
        client.sendall((canonical({"operation": operation, "payload": payload}) + "\n").encode())
        received = bytearray()
        limit = 4 * 1024 * 1024
        bounded_read = operation in {"task_diff", "request_status"}
        if operation == "task_diff":
            from router.task_diff import (
                MAX_WIRE_BYTES,
                TaskDiffProtocolError,
                parse_json,
            )
            limit = MAX_WIRE_BYTES
            protocol_error = TaskDiffProtocolError
            read_name = "task-diff"
        elif operation == "request_status":
            from router.request_status import (
                MAX_WIRE_BYTES,
                RequestStatusProtocolError,
                parse_json,
            )
            limit = MAX_WIRE_BYTES
            protocol_error = RequestStatusProtocolError
            read_name = "request-status"
        while len(received) <= limit:
            client.settimeout(remaining())
            chunk = client.recv(min(65536, limit + 1 - len(received)))
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if bounded_read and newline >= 0 and newline != len(chunk) - 1:
                raise protocol_error(f"Unexpected bytes after {read_name} reply")
            received.extend(chunk if newline < 0 else chunk[:newline + 1])
            if newline >= 0:
                break
        line = bytes(received)
        if bounded_read:
            if len(line) > limit or not line.endswith(b"\n"):
                raise protocol_error(f"Incomplete or oversized {read_name} reply")
            client.settimeout(remaining())
            if client.recv(1):
                raise protocol_error(f"Unexpected bytes after {read_name} reply")
            response = parse_json(line)
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise protocol_error(f"Invalid {read_name} response envelope")
            if response["ok"]:
                if set(response) != {"ok", "result"} or not isinstance(response.get("result"), dict):
                    raise protocol_error(f"Invalid {read_name} success envelope")
                return response["result"]
            if set(response) != {"ok", "error"} or not isinstance(response.get("error"), str):
                raise protocol_error(f"Invalid {read_name} error envelope")
            raise ValueError(f"{read_name} daemon refused the read")
        if operation == "completion_next" and len(line) <= limit and not line.endswith(b"\n"):
            raise ConnectionResetError("Router closed the connection before a complete reply")
        if not line or len(line) > 4 * 1024 * 1024:
            raise ValueError("Invalid daemon response")
        response = json.loads(line)
        if not response.get("ok"):
            if operation in {"completion_next", "completion_ack", "completion_subscribe",
                             "completion_consumer_list", "completion_consumer_remove"} and response.get("retryable") is True:
                raise CompletionBusy(response.get("error", "Completion database is busy; retry delivery"))
            if operation == "completion_ack" and response.get("code") == "acknowledgment_refused":
                raise CompletionRefused(response.get("error", "Completion acknowledgment refused"))
            raise ValueError(response.get("error", "Router request failed"))
        return response["result"]
