import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager

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


def get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers["Content-Type"], response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read()


class DashboardTests(unittest.TestCase):
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

    def test_binds_loopback_only(self):
        server = make_server(lambda: RESULT, 0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
