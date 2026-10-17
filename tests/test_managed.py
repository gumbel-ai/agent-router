import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from router.cli import main as cli_main
from router.db import Store
from router.host import current_boot_id
from router.managed import run_managed
from router.managed_gate import _write_completion
from router.processes import ProcessMonitor


class ManagedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-managed-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_foreground_command_registers_before_execution_and_records_private_metadata(self):
        marker = self.directory / "ran"
        calls = []
        group_verified = []

        def send(operation, payload, endpoint, timeout):
            calls.append((operation, payload))
            if operation == "managed_process_start":
                self.assertFalse(marker.exists())
                group_verified.append(payload["pgid"] == os.getpgid(payload["pid"]))
            return {"recorded": True}

        secret = "password=orchid"
        with patch("router.managed.request", side_effect=send):
            code = run_managed(
                secret,
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()", secret],
                session_id="S", agent_id="A", directory=self.directory,
            )

        self.assertEqual(code, 0)
        self.assertTrue(marker.exists())
        self.assertEqual([call[0] for call in calls],
                         ["managed_process_start", "managed_process_finish"])
        start = calls[0][1]
        self.assertEqual(start["label"], "password=[REDACTED]")
        self.assertEqual(start["argument_count"], 3)
        self.assertNotIn("orchid", json.dumps(start))
        self.assertEqual(start["environment_allowlist"], {})
        self.assertEqual(group_verified, [True])

    def test_registration_failure_releases_no_user_command(self):
        marker = self.directory / "must-not-exist"
        with patch("router.managed.request", side_effect=ValueError("registration failed")), \
                self.assertRaisesRegex(ValueError, "registration failed"):
            run_managed(
                "blocked", [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                directory=self.directory,
            )
        self.assertFalse(marker.exists())
        self.assertEqual(list((self.directory / "managed").glob("*.completion.json")), [])

    def test_offline_registration_evidence_is_bounded_but_ambiguous_commit_reconciles(self):
        with patch("router.managed.MAX_LOCAL_COMPLETION_FILES", 1), \
                patch("router.managed.request", side_effect=OSError("offline")):
            for number in range(2):
                with self.assertRaisesRegex(OSError, "offline"):
                    run_managed(
                        f"offline-{number}", [sys.executable, "-c", "pass"],
                        directory=self.directory,
                    )
        self.assertEqual(
            len(list((self.directory / "managed").glob("*.completion.json"))), 1,
        )

        reconcile_directory = self.directory / "ambiguous"
        store = Store(reconcile_directory / "router.sqlite3")
        monitor = ProcessMonitor(
            store, reconcile_directory, "host-a", current_boot_id(),
        )

        def committed_without_response(operation, payload, endpoint, timeout):
            if operation == "managed_process_start":
                monitor.register(payload)
                raise OSError("response lost")
            raise AssertionError(operation)

        with patch("router.managed.request", side_effect=committed_without_response), \
                self.assertRaisesRegex(OSError, "response lost"):
            run_managed(
                "ambiguous", [sys.executable, "-c", "pass"],
                directory=reconcile_directory,
            )
        self.assertTrue(monitor.sample_once())
        record = monitor.fleet_records(10)[0][0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_status"], 125)

    def test_managed_cwd_is_redacted_before_bounding_registration_payload(self):
        calls = []
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        cwd = "/" + "x" * 1988 + "/" + secret

        class FakeCwd:
            def resolve(self):
                return cwd

        def send(operation, payload, endpoint, timeout):
            calls.append((operation, payload))
            return {"recorded": True}

        with patch("router.managed.Path.cwd", return_value=FakeCwd()), \
                patch("router.managed.request", side_effect=send):
            self.assertEqual(run_managed(
                "cwd", [sys.executable, "-c", "pass"], directory=self.directory,
            ), 0)
        registered_cwd = calls[0][1]["cwd"]
        self.assertNotIn(secret, registered_cwd)
        self.assertNotIn("ghp_abcdefgh", registered_cwd)
        self.assertIn("[REDACTED]", registered_cwd)

    def test_sigint_before_registration_returns_130_without_releasing_command(self):
        marker = self.directory / "registration-blocked"
        command_marker = self.directory / "must-not-run"
        launcher = f"""
import sys, time
from pathlib import Path
from unittest.mock import patch
from router.cli import main

def blocked(*_args, **_kwargs):
    Path({str(marker)!r}).touch()
    time.sleep(30)

patch('router.managed.request', side_effect=blocked).start()
sys.argv = ['router', 'run', '--label', 'blocked', '--', sys.executable, '-c',
            {f'from pathlib import Path;Path({str(command_marker)!r}).touch()'!r}]
main()
"""
        wrapper = subprocess.Popen([sys.executable, "-c", launcher])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists():
                if wrapper.poll() is not None:
                    break
                time.sleep(.02)
            self.assertTrue(marker.exists())
            wrapper.send_signal(signal.SIGINT)
            self.assertEqual(wrapper.wait(timeout=5), 130)
            self.assertFalse(command_marker.exists())
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait(timeout=5)

    def test_registration_retry_is_idempotent_and_still_precedes_execution(self):
        marker = self.directory / "retried"
        calls = []
        store = Store(self.directory / "router.sqlite3")
        monitor = ProcessMonitor(store, self.directory, "host-a", current_boot_id())

        def send(operation, payload, endpoint, timeout):
            calls.append(operation)
            if operation == "managed_process_start":
                self.assertFalse(marker.exists())
                recorded = monitor.register(payload)
                if calls.count(operation) == 1:
                    raise OSError("response lost")
                return recorded
            return monitor.complete(payload["managed_process_id"])

        with patch("router.managed.request", side_effect=send):
            code = run_managed(
                "retried", [sys.executable, "-c",
                            f"from pathlib import Path;Path({str(marker)!r}).touch()"],
                directory=self.directory,
            )
        self.assertEqual(code, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(calls.count("managed_process_start"), 2)
        rows, _ = monitor.fleet_records(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "stopped")

    def test_nonzero_exit_and_signal_use_shell_compatible_status(self):
        with patch("router.managed.request", return_value={"recorded": True}):
            self.assertEqual(run_managed(
                "failure", [sys.executable, "-c", "raise SystemExit(7)"],
                directory=self.directory,
            ), 7)
            self.assertEqual(run_managed(
                "signal", [sys.executable, "-c", "import os,signal;os.kill(os.getpid(),signal.SIGTERM)"],
                directory=self.directory,
            ), 128 + signal.SIGTERM)

    def test_gate_child_restores_default_signal_dispositions_before_exec(self):
        completion = self.directory / "completion.json"
        release_read, release_write = os.pipe()
        handshake_read, handshake_write = os.pipe()
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).parents[1] / "router" / "managed_gate.py"),
             "supervise", str(release_read), str(handshake_write), str(completion),
             sys.executable, "-c", "import time;time.sleep(30)"],
            pass_fds=(release_read, handshake_write), start_new_session=True,
        )
        os.close(release_read)
        os.close(handshake_write)
        try:
            with os.fdopen(handshake_read, "rb") as stream:
                self.assertGreater(json.loads(stream.readline())["pid"], 0)
            os.write(release_write, b"1")
            os.close(release_write)
            release_write = -1
            time.sleep(.1)
            os.killpg(supervisor.pid, signal.SIGTERM)
            self.assertEqual(supervisor.wait(timeout=5), 128 + signal.SIGTERM)
            self.assertEqual(json.loads(completion.read_text())["exit_code"], -signal.SIGTERM)
        finally:
            if release_write != -1:
                os.close(release_write)
            if supervisor.poll() is None:
                os.killpg(supervisor.pid, signal.SIGKILL)
                supervisor.wait(timeout=5)

    def test_gate_preserves_completion_when_foreground_group_receives_sigquit(self):
        completion = self.directory / "sigquit-completion.json"
        release_read, release_write = os.pipe()
        handshake_read, handshake_write = os.pipe()
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).parents[1] / "router" / "managed_gate.py"),
             "supervise", str(release_read), str(handshake_write), str(completion),
             sys.executable, "-c", "import time;time.sleep(30)"],
            pass_fds=(release_read, handshake_write), start_new_session=True,
        )
        os.close(release_read)
        os.close(handshake_write)
        try:
            with os.fdopen(handshake_read, "rb") as stream:
                self.assertGreater(json.loads(stream.readline())["pid"], 0)
            os.write(release_write, b"1")
            os.close(release_write)
            release_write = -1
            time.sleep(.1)
            os.killpg(supervisor.pid, signal.SIGQUIT)
            self.assertEqual(supervisor.wait(timeout=5), 128 + signal.SIGQUIT)
            self.assertEqual(json.loads(completion.read_text())["signal"], signal.SIGQUIT)
        finally:
            if release_write != -1:
                os.close(release_write)
            if supervisor.poll() is None:
                os.killpg(supervisor.pid, signal.SIGKILL)
                supervisor.wait(timeout=5)

    def test_gate_redacts_failed_executable_and_preserves_status_on_completion_write_error(self):
        gate = str(Path(__file__).parents[1] / "router" / "managed_gate.py")
        secret = "https://user:swordfish@example.test/missing"
        release_read, release_write = os.pipe()
        failed_exec = subprocess.Popen(
            [sys.executable, gate, "exec", str(release_read), secret],
            pass_fds=(release_read,), stderr=subprocess.PIPE, text=True,
        )
        os.close(release_read)
        os.write(release_write, b"1")
        os.close(release_write)
        stderr = failed_exec.communicate(timeout=5)[1]
        self.assertEqual(failed_exec.returncode, 127)
        self.assertNotIn("swordfish", stderr)
        self.assertIn("[REDACTED]", stderr)

        release_read, release_write = os.pipe()
        handshake_read, handshake_write = os.pipe()
        supervisor = subprocess.Popen(
            [sys.executable, gate, "supervise", str(release_read), str(handshake_write),
             str(self.directory / "missing" / "completion.json"),
             sys.executable, "-c", "raise SystemExit(7)"],
            pass_fds=(release_read, handshake_write), stderr=subprocess.PIPE, text=True,
        )
        os.close(release_read)
        os.close(handshake_write)
        with os.fdopen(handshake_read, "rb") as stream:
            self.assertGreater(json.loads(stream.readline())["pid"], 0)
        os.write(release_write, b"1")
        os.close(release_write)
        stderr = supervisor.communicate(timeout=5)[1]
        self.assertEqual(supervisor.returncode, 7)
        self.assertIn("could not persist completion evidence", stderr)

    def test_completion_write_fsyncs_file_and_directory_and_surfaces_directory_failure(self):
        completion = self.directory / "completion.json"
        with patch("router.managed_gate.os.fsync", side_effect=[None, OSError("disk")]) as fsync, \
                self.assertRaisesRegex(OSError, "disk"):
            _write_completion(completion, {"schema_version": 1})
        self.assertEqual(fsync.call_count, 2)
        self.assertTrue(completion.is_file())

    def test_foreground_wrapper_forwards_termination_to_managed_group(self):
        marker = self.directory / "command-started"
        child = (
            "from pathlib import Path;import time;"
            f"Path({str(marker)!r}).touch();time.sleep(30)"
        )
        launcher = (
            "import sys;from pathlib import Path;from unittest.mock import patch;"
            "from router.managed import run_managed;"
            "patch('router.managed.request',return_value={'recorded':True}).start();"
            f"raise SystemExit(run_managed('forwarded',[sys.executable,'-c',{child!r}],"
            f"directory=Path({str(self.directory)!r})))"
        )
        wrapper = subprocess.Popen(
            [sys.executable, "-c", launcher], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists():
                if wrapper.poll() is not None:
                    break
                time.sleep(.02)
            if not marker.exists():
                self.fail(f"managed command did not start: {wrapper.communicate()!r}")
            wrapper.send_signal(signal.SIGTERM)
            stdout, stderr = wrapper.communicate(timeout=5)
            self.assertEqual(wrapper.returncode, 128 + signal.SIGTERM, (stdout, stderr))
            completions = list((self.directory / "managed").glob("*.completion.json"))
            self.assertEqual(len(completions), 1)
            evidence = json.loads(completions[0].read_text())
            self.assertEqual(evidence["signal"], signal.SIGTERM)
            self.assertEqual(evidence["exit_code"], -signal.SIGTERM)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait(timeout=5)

    def test_interactive_command_owns_and_restores_terminal(self):
        command = (
            "import signal,time;"
            "value=open('/dev/tty').readline().strip();"
            "print('ECHO:'+value,flush=True);"
            "signal.signal(signal.SIGCONT,lambda *_:print('CONTINUED',flush=True));"
            "time.sleep(5)"
        )
        launcher = (
            "import os,sys;from pathlib import Path;from unittest.mock import patch;"
            "from router.managed import run_managed;"
            "patch('router.managed.request',return_value={'recorded':True}).start();"
            f"code=run_managed('interactive',[sys.executable,'-c',{command!r}],"
            f"directory=Path({str(self.directory)!r}));"
            "print(f'RESULT:{code}:RESTORED:{os.tcgetpgrp(0)==os.getpgrp()}',flush=True);"
            "raise SystemExit(0 if code==130 else 1)"
        )
        pid, descriptor = pty.fork()
        if pid == 0:
            # Act as a minimal job-control shell so the wrapper is not the
            # session leader and terminal-generated stop signals are meaningful.
            for ignored in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
                signal.signal(ignored, signal.SIG_IGN)
            wrapper = os.fork()
            if wrapper == 0:
                os.setpgid(0, 0)
                os.execv(sys.executable, [sys.executable, "-c", launcher])
            try:
                os.setpgid(wrapper, wrapper)
            except PermissionError:
                pass
            os.tcsetpgrp(0, wrapper)
            _, stopped = os.waitpid(wrapper, os.WUNTRACED)
            if not os.WIFSTOPPED(stopped):
                os._exit(2)
            os.tcsetpgrp(0, os.getpgrp())
            os.write(1, f"SHELL_STOPPED:{os.WSTOPSIG(stopped)}\n".encode())
            os.read(0, 2)
            os.tcsetpgrp(0, wrapper)
            os.killpg(wrapper, signal.SIGCONT)
            _, completed = os.waitpid(wrapper, 0)
            os.tcsetpgrp(0, os.getpgrp())
            os._exit(os.waitstatus_to_exitcode(completed))
        output = b""
        status = None
        try:
            def read_until(marker: bytes, seconds: float = 5) -> None:
                nonlocal output
                deadline = time.monotonic() + seconds
                while marker not in output and time.monotonic() < deadline:
                    if select.select([descriptor], [], [], .1)[0]:
                        try:
                            output += os.read(descriptor, 4096)
                        except OSError:
                            break
                self.assertIn(marker, output)

            os.write(descriptor, b"hello\n")
            read_until(b"ECHO:hello")
            os.write(descriptor, b"\x1a")
            read_until(f"SHELL_STOPPED:{signal.SIGTSTP}".encode())
            os.write(descriptor, b"c\n")
            read_until(b"CONTINUED")
            os.write(descriptor, b"\x03")
            read_until(b"RESULT:")
            _, status = os.waitpid(pid, 0)
            self.assertIn(b"RESULT:130:RESTORED:True", output)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            os.close(descriptor)
            if status is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

    def test_supervisor_handshake_has_a_deadline_and_reaps_stalled_group(self):
        real_popen = subprocess.Popen
        started = []

        def stalled_supervisor(_arguments, *, pass_fds, process_group):
            process = real_popen(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                pass_fds=pass_fds, process_group=process_group,
            )
            started.append(process)
            return process

        with patch("router.managed.subprocess.Popen", side_effect=stalled_supervisor), \
                patch("router.managed.HANDSHAKE_TIMEOUT_SECONDS", .05), \
                self.assertRaisesRegex(TimeoutError, "handshake timed out"):
            run_managed("stalled", [sys.executable, "-c", "raise SystemExit(0)"],
                        directory=self.directory)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())

    def test_agent_requires_session_and_empty_command_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires a command"):
            run_managed("empty", [], directory=self.directory)
        with self.assertRaisesRegex(ValueError, "agent requires"):
            run_managed("bad", ["true"], agent_id="A", directory=self.directory)

    def test_cli_requires_separator_and_preserves_exact_argv(self):
        with patch("sys.argv", ["router", "run", "--label", "suite", "--session", "S",
                                "--agent", "A", "--", "printf", "%s", "a b"]), \
                patch("router.managed.run_managed", return_value=7) as launch, \
                self.assertRaises(SystemExit) as stopped:
            cli_main()
        self.assertEqual(stopped.exception.code, 7)
        launch.assert_called_once_with(
            "suite", ["printf", "%s", "a b"], session_id="S", agent_id="A",
        )

    def test_coordinator_process_registration_uses_versioned_structured_payload(self):
        payload = {"request_version": "1", "managed_process_id": "configured-build"}
        output = io.StringIO()
        with patch("sys.argv", ["router", "process", "register", "--json", json.dumps(payload)]), \
                patch("router.cli.request", return_value={"status": "running"}) as send, \
                redirect_stdout(output):
            cli_main()
        send.assert_called_once_with("configured_process_register", payload)
        self.assertEqual(json.loads(output.getvalue()), {"status": "running"})


if __name__ == "__main__":
    unittest.main()
