"""One request/response per local socket connection; never starts a daemon."""

import json
import socket
import time
from pathlib import Path

from router.dispatch import canonical
from router.paths import socket_path


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
        while len(received) <= limit:
            client.settimeout(remaining())
            chunk = client.recv(min(65536, limit + 1 - len(received)))
            if not chunk:
                break
            newline = chunk.find(b"\n")
            received.extend(chunk if newline < 0 else chunk[:newline + 1])
            if newline >= 0:
                break
        line = bytes(received)
        if not line or len(line) > 4 * 1024 * 1024:
            raise ValueError("Invalid daemon response")
        response = json.loads(line)
        if not response.get("ok"):
            raise ValueError(response.get("error", "Router request failed"))
        return response["result"]
