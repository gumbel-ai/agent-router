import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router import observer_hook
from router.daemon import Controller
from router.fleet import _validate_snapshot
from router.fleet_snapshot import compose
from router.processes import SessionProcesses, ancestry, parent_and_identity, sample


def process(pid, ppid, identity=None, cpu_ns=0, rss=1000, executable="tool"):
    return {"pid": pid, "ppid": ppid, "pgid": pid, "identity": identity or f"id-{pid}",
            "executable": executable, "started_at": 1, "rss_bytes": rss, "cpu_ns": cpu_ns}


class SamplerTests(unittest.TestCase):
    def test_real_sampler_sees_this_process_and_its_parent(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        processes = sample()
        me = processes[os.getpid()]
        self.assertEqual(processes[child.pid]["ppid"], os.getpid())
        self.assertEqual(me["identity"], parent_and_identity(os.getpid())[1])
        self.assertGreater(me["rss_bytes"], 0)
        self.assertGreater(me["started_at"], 1_000_000_000_000)  # Milliseconds since the epoch.
        chain = ancestry()
        self.assertEqual(chain[0], [os.getppid(), processes[os.getppid()]["identity"]])

    def test_processes_are_attributed_to_their_nearest_live_session_root(self):
        tree = {1: process(1, 0), 10: process(10, 1), 11: process(11, 10, rss=50), 12: process(12, 11),
                20: process(20, 10), 21: process(21, 20), 30: process(30, 1)}
        sessions = SessionProcesses(sampler=lambda: tree)
        roots = [{"source": "claude", "session_id": "outer", "pid": 10, "identity": "id-10"},
                 {"source": "codex", "session_id": "inner", "pid": 20, "identity": "id-20"},
                 {"source": "claude", "session_id": "reused", "pid": 30, "identity": "old-process"}]
        records = sessions.records("host", roots, 100)
        owners = {record["pid"]: record["session_id"] for record in records}
        # Nested sessions keep their own processes; a reused PID is not a session's process.
        self.assertEqual(owners, {10: "outer", 11: "outer", 12: "outer", 20: "inner", 21: "inner"})
        self.assertTrue(all(record["attribution"] == "session" for record in records))
        self.assertEqual([record["pid"] for record in sessions.records("host", roots, 2)], [10, 20])

    def test_cpu_percent_comes_from_the_change_since_the_previous_sample(self):
        samples = iter([{10: process(10, 1, cpu_ns=0)}, {10: process(10, 1, cpu_ns=500_000_000)}])
        sessions = SessionProcesses(sampler=lambda: next(samples))
        roots = [{"source": "claude", "session_id": "s", "pid": 10, "identity": "id-10"}]
        with patch("router.processes.time.monotonic", side_effect=[100.0, 101.0]):
            self.assertIsNone(sessions.records("host", roots, 10)[0]["cpu_pct"])
            self.assertEqual(sessions.records("host", roots, 10)[0]["cpu_pct"], 50.0)


class SessionProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-processes-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.control = Controller(settings(root / "repo"), {}, root / "state")
        self.at = int(time.time() * 1000) - 10_000

    def observe(self, chain, kind="prompt"):
        self.at += 10
        self.control.sessions.observe({"source": "claude", "session_id": "S", "kind": kind,
                                       "observed_at": self.at, "process_ancestors": chain})

    def test_session_process_is_the_ancestor_shared_by_successive_hooks(self):
        claude, terminal = [2128, "darwin:1:2"], [2041, "darwin:1:1"]
        self.observe([[9001, "darwin:3:1"], claude, terminal])
        self.assertEqual(self.control.sessions.process_roots(), [])  # One chain cannot tell shell from agent.
        self.observe("not a chain")
        self.observe([[9002, "darwin:3:2"], claude, terminal])
        self.assertEqual(self.control.sessions.process_roots(),
                         [{"source": "claude", "session_id": "S", "pid": 2128, "identity": "darwin:1:2"}])
        self.observe([[9003, "darwin:3:3"], claude, terminal], kind="session_end")
        self.assertEqual(self.control.sessions.process_roots(), [])
        view = self.control.handle("snapshot", {})["sessions"][0]
        self.assertFalse({"process_chain", "process_root"} & view.keys())

    def test_inferred_processes_reach_the_fleet_snapshot(self):
        chain = ancestry()
        self.observe([[999_999, "elsewhere"], *chain])
        self.observe(chain)
        snapshot = _validate_snapshot(json.dumps(compose(self.control)).encode())
        [own] = [record for record in snapshot["inferred_processes"] if record["pid"] == chain[0][0]]
        self.assertEqual((own["attribution"], own["source"], own["session_id"]), ("session", "claude", "S"))
        self.assertGreater(own["rss_bytes"], 0)
        bad = compose(self.control)
        bad["inferred_processes"][0]["cpu_pct"] = -1
        with self.assertRaises(ValueError):
            _validate_snapshot(json.dumps(bad).encode())

    def test_hooks_send_process_identities_and_survive_without_them(self):
        sent = []
        with patch("router.observer_hook.request", side_effect=lambda operation, body, timeout: sent.append(body)
                   or {}):
            observer_hook.send({"kind": "prompt"}, None)
            with patch("router.observer_hook.ancestry", side_effect=PermissionError):
                observer_hook.send({"kind": "prompt"}, None)
        self.assertEqual(sent[0]["session"]["process_ancestors"], ancestry())
        self.assertNotIn("process_ancestors", sent[1]["session"])


if __name__ == "__main__":
    unittest.main()
