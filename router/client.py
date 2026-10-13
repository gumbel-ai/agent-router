"""One request/response per local socket connection; never starts a daemon."""

import json
import socket
from pathlib import Path

from router.config import socket_path
from router.dispatch import canonical


def request(operation: str, payload: dict, endpoint: Path | None = None) -> dict | list:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        try:
            client.connect(str(endpoint or socket_path()))
        except OSError as error:
            raise ValueError("Agent Router is not reachable. Run `router daemon start`.") from error
        client.sendall((canonical({"operation": operation, "payload": payload}) + "\n").encode())
        with client.makefile("rb") as stream:
            line = stream.readline(4 * 1024 * 1024 + 1)
        if not line or len(line) > 4 * 1024 * 1024:
            raise ValueError("Invalid daemon response")
        response = json.loads(line)
        if not response.get("ok"):
            raise ValueError(response.get("error", "Router request failed"))
        return response["result"]
