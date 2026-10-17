import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router.daemon import Controller
from router.fleet import _validate_snapshot
from router.fleet_snapshot import compose
from router.managed import completion_directory, run
from router.processes import parent_and_identity


class ManagedRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-managed-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "state"
        self.control = Controller(settings(root / "repo"), {}, self.state)
        self.daemon_up = True

    def request(self, operation, payload, timeout):
        if not self.daemon_up:
            raise OSError("Router daemon is not running")
        return self.control.handle(operation, payload)

    def run_command(self, script, **options):
        with patch("router.managed.request", side_effect=self.request):
            return run([sys.executable, "-c", script], options.get("label"), options.get("session"),
                       options.get("agent"), self.state)

    def managed(self):
        records, _ = self.control.managed.records()
        return {record["label"]: record for record in records}

    def test_completion_evidence_for_exit_codes_signals_and_survivors(self):
        self.assertEqual(self.run_command("pass", label="ok", session="S", agent="A"), 0)
        self.assertEqual(self.run_command("raise SystemExit(3)", label="failed", session="S"), 3)
        self.assertEqual(self.run_command("import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                                          label="signaled"), 128 + signal.SIGTERM)
        survivor = ("import subprocess, sys; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])")
        self.assertEqual(self.run_command(survivor, label="survivor"), 0)
        records = self.managed()
        self.assertEqual({label: (record["status"], record["attribution"]) for label, record in records.items()},
                         {"ok": ("exited", "exact"), "failed": ("failed", "session"),
                          "signaled": ("signaled", "host"), "survivor": ("exited", "host")})
        self.assertEqual(records["signaled"]["signal"], signal.SIGTERM)
        self.assertTrue(records["survivor"]["shutdown_uncertain"])
        self.assertFalse(records["ok"]["shutdown_uncertain"])
        self.assertGreater(records["ok"]["peak_rss_bytes"], 1_000_000)
        self.assertEqual(records["ok"]["executable"], Path(sys.executable).name)
        # Argument text is never kept, only its count and hash.
        self.assertNotIn("SIGTERM", json.dumps(records))

    def test_evidence_survives_a_daemon_that_is_down_at_completion(self):
        def down_after_start(operation, payload, timeout):
            result = self.request(operation, payload, timeout)
            if operation == "managed_start":
                self.daemon_up = False
            return result

        with patch("router.managed.request", side_effect=down_after_start):
            self.assertEqual(run([sys.executable, "-c", "raise SystemExit(4)"], "offline", None, None,
                                 self.state), 4)
        self.assertEqual(len(list(completion_directory(self.state).glob("*.json"))), 1)
        self.assertEqual(self.managed()["offline"]["exit_code"], 4)  # Read back from the completion file.
        self.assertEqual(list(completion_directory(self.state).glob("*.json")), [])

    def test_a_vanished_wrapper_without_evidence_is_uncertain_not_finished(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        live = {"managed_id": "managed-live", "label": "live", "attribution": "host", "pid": os.getpid(),
                "pgid": os.getpgid(0), "wrapper_pid": os.getpid(),
                "wrapper_identity": parent_and_identity(os.getpid())[1], "started_at": int(time.time() * 1000)}
        self.control.handle("managed_start", live)
        self.control.handle("managed_start", {**live, "managed_id": "managed-gone", "label": "gone",
                                              "wrapper_pid": dead.pid, "wrapper_identity": "gone"})
        records = self.managed()
        self.assertEqual((records["live"]["status"], records["gone"]["status"]), ("running", "unknown"))
        self.assertTrue(records["gone"]["shutdown_uncertain"])
        snapshot = _validate_snapshot(json.dumps(compose(self.control)).encode())
        self.assertEqual(snapshot["capacity"]["active_managed_processes"], 1)
        self.assertEqual({record["managed_id"] for record in snapshot["managed_processes"]},
                         {"managed-live", "managed-gone"})
        self.assertFalse({"identity", "wrapper_pid", "wrapper_identity"} & snapshot["managed_processes"][0].keys())

    def test_cli_requires_a_command(self):
        with self.assertRaises(ValueError):
            run([], None, None, None, self.state)


if __name__ == "__main__":
    unittest.main()
