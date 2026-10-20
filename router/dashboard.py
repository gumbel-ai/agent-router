"""Local read-only web dashboard over the fleet snapshot."""

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files


def make_server(fetch: Callable[[], dict], port: int) -> ThreadingHTTPServer:
    """Serve the page and `/api/fleet` on loopback only; `fetch` returns one fleet result."""
    page = files("router").joinpath("dashboard.html").read_bytes()
    lock = threading.Lock()  # One fleet query at a time, however many tabs poll.

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # Only loopback names, so a DNS-rebound website cannot read fleet data.
            port = self.server.server_address[1]
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self._send(403, "text/plain; charset=utf-8", b"Forbidden host")
            elif self.path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif self.path == "/api/fleet":
                try:
                    with lock:
                        body, status = fetch(), 200
                except (OSError, ValueError) as error:
                    body, status = {"error": str(error)}, 503
                self._send(status, "application/json", json.dumps(body).encode())
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass  # Polling every few seconds would flood the terminal.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
