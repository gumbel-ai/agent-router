"""One request/response per local socket connection; never starts a daemon."""

import json
import socket
from pathlib import Path

from router.dispatch import canonical
from router.paths import socket_path


def request(operation: str, payload: dict, endpoint: Path | None = None, *, timeout: float = 15) -> dict | list:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        try:
            client.connect(str(endpoint or socket_path()))
        except OSError as error:
            raise ValueError("Agent Router is not reachable. Run `router service start` (managed) or `router daemon start` (manual).") from error
        client.sendall((canonical({"operation": operation, "payload": payload}) + "\n").encode())
        with client.makefile("rb") as stream:
            line = stream.readline(4 * 1024 * 1024 + 1)
        if not line or len(line) > 4 * 1024 * 1024:
            raise ValueError("Invalid daemon response")
        response = json.loads(line)
        if not response.get("ok"):
            raise ValueError(response.get("error", "Router request failed"))
        return response["result"]
