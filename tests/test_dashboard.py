import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from unittest.mock import patch

from router.dashboard import make_server

RESULT = {"schema_version": 1, "generated_at": 1, "partial": False, "version_mismatch": False, "hosts": []}


@contextmanager
def serving(fetch):
    server = make_server(fetch, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def get(url: str, host: str | None = None) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers["Content-Type"], response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers["Content-Type"], error.read()


class DashboardTests(unittest.TestCase):
    def test_client_disconnects_during_response_do_not_escape(self):
        for error in (BrokenPipeError, ConnectionResetError, PermissionError):
            for fail_on_write in (1, 2):  # Headers, then body.
                with (
                    self.subTest(error=error.__name__, write=fail_on_write),
                    make_server(lambda: RESULT, 0) as server,
                ):
                    client, connection = socket.socketpair()
                    with client, connection:
                        port = server.server_address[1]
                        client.sendall(
                            f"GET /api/fleet HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
                        )
                        client.shutdown(socket.SHUT_WR)
                        sendall = socket.socket.sendall
                        writes = 0
                        injected = False

                        def disconnect(
                            sock, body, *, connection=connection, fail_on_write=fail_on_write,
                            error=error, sendall=sendall,
                        ):
                            nonlocal writes, injected
                            if sock is connection:
                                writes += 1
                                if writes == fail_on_write:
                                    injected = True
                                    raise error("response write failed")
                            return sendall(sock, body)

                        with patch.object(socket.socket, "sendall", disconnect):
                            if error is PermissionError:
                                with self.assertRaises(PermissionError):
                                    server.RequestHandlerClass(connection, ("127.0.0.1", 0), server)
                            else:
                                server.RequestHandlerClass(connection, ("127.0.0.1", 0), server)
                        self.assertTrue(injected)

    def test_serves_page_and_fleet_json(self):
        with serving(lambda: RESULT) as base:
            status, content_type, body = get(base + "/")
            self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
            self.assertIn(b"/api/fleet", body)
            status, content_type, body = get(base + "/api/fleet")
            self.assertEqual((status, content_type, json.loads(body)), (200, "application/json", RESULT))

    def test_fleet_failure_is_a_json_error(self):
        def fail():
            raise ValueError("Unknown fleet host: worker-a")
        with serving(fail) as base:
            status, _, body = get(base + "/api/fleet")
        self.assertEqual((status, json.loads(body)), (503, {"error": "Unknown fleet host: worker-a"}))

    def test_unknown_path_is_not_found(self):
        with serving(lambda: RESULT) as base:
            self.assertEqual(get(base + "/other")[0], 404)

    def test_rejects_non_loopback_host_header(self):
        with serving(lambda: RESULT) as base:
            port = base.rsplit(":", 1)[1]
            self.assertEqual(get(base + "/api/fleet", "attacker.example")[0], 403)
            self.assertEqual(get(base + "/api/fleet", f"attacker.example:{port}")[0], 403)
            self.assertEqual(get(base + "/api/fleet", f"localhost:{port}")[0], 200)

    def test_binds_loopback_only(self):
        server = make_server(lambda: RESULT, 0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
