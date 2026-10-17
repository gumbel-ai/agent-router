"""Foreground managed-command launcher with durable observation evidence."""

import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from router.client import request
from router.dispatch import canonical
from router.paths import socket_path, state_dir
from router.process import creation_identity
from router.redaction import redact_secrets

MAX_HANDSHAKE_BYTES = 1024
HANDSHAKE_TIMEOUT_SECONDS = 5
REGISTRATION_ATTEMPTS = 3
COMPLETION_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_LOCAL_COMPLETION_FILES = 10_000


def _bounded_text(value: str | None, name: str, limit: int = 240) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{name} must be a non-empty string")
    if len(normalized) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return normalized


def _start_time(identity: str) -> str:
    if identity.startswith("linux:"):
        return identity.rsplit(":", 1)[-1]
    if identity.startswith("darwin:"):
        return identity.removeprefix("darwin:")
    raise ValueError("Managed command has unsupported process identity")


def _forward_signals(pgid: int):
    previous = {}

    def forward(signum, _frame):
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass

    for forwarded in (
        signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP,
    ):
        previous[forwarded] = signal.signal(forwarded, forward)
    return previous


def _read_handshake(descriptor: int) -> bytes:
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
    result = bytearray()
    while b"\n" not in result and len(result) <= MAX_HANDSHAKE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise TimeoutError("Managed command supervisor handshake timed out")
        chunk = os.read(descriptor, MAX_HANDSHAKE_BYTES + 1 - len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result).split(b"\n", 1)[0]


def _stop_supervisor(supervisor: subprocess.Popen) -> None:
    try:
        supervisor.wait(timeout=.5)
        return
    except subprocess.TimeoutExpired:
        pass
    for forwarded in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(supervisor.pid, forwarded)
        except ProcessLookupError:
            return
        try:
            supervisor.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue


def _prune_completion_files(directory: Path) -> None:
    cutoff_ns = time.time_ns() - COMPLETION_RETENTION_SECONDS * 1_000_000_000
    candidates = []
    for path in directory.glob("managed-*.completion.json"):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            path.unlink(missing_ok=True)
            continue
        candidates.append((metadata.st_mtime_ns, path.name, path))
    candidates.sort(reverse=True)
    expired = {
        path for index, (modified_ns, _name, path) in enumerate(candidates)
        if index >= MAX_LOCAL_COMPLETION_FILES or modified_ns < cutoff_ns
    }
    for path in expired:
        path.unlink(missing_ok=True)


def _wait_for_supervisor(
    supervisor: subprocess.Popen, terminal: tuple[int, int] | None,
) -> tuple[int, int] | None:
    """Mirror managed-group job-control stops to the shell-visible wrapper."""
    options = os.WUNTRACED | getattr(os, "WCONTINUED", 0)
    while True:
        _, status = os.waitpid(supervisor.pid, options)
        if os.WIFSTOPPED(status):
            _restore_terminal(terminal)
            handler = signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            try:
                os.kill(os.getpid(), signal.SIGTSTP)
            finally:
                signal.signal(signal.SIGTSTP, handler)
            terminal = _terminal_foreground_group(supervisor.pid)
            try:
                os.killpg(supervisor.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        elif os.WIFCONTINUED(status):
            continue
        else:
            supervisor.returncode = os.waitstatus_to_exitcode(status)
            return terminal


def _terminal_foreground_group(pgid: int) -> tuple[int, int] | None:
    """Give an interactive managed group the terminal until it finishes."""
    try:
        descriptor = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    if not os.isatty(descriptor):
        return None
    original = os.tcgetpgrp(descriptor)
    if original != os.getpgrp():
        return None
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(descriptor, pgid)
    finally:
        signal.signal(signal.SIGTTOU, previous)
    return descriptor, original


def _restore_terminal(terminal: tuple[int, int] | None) -> None:
    if terminal is None:
        return
    descriptor, original = terminal
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(descriptor, original)
    except OSError:
        pass
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _register(payload: dict, endpoint: Path) -> None:
    error = None
    for attempt in range(REGISTRATION_ATTEMPTS):
        try:
            request("managed_process_start", payload, endpoint, timeout=5)
            return
        except (OSError, ValueError) as current:
            error = current
            if attempt + 1 < REGISTRATION_ATTEMPTS:
                time.sleep(.05)
    raise error


def run_managed(label: str, command: list[str], session_id: str | None = None,
                agent_id: str | None = None, *, directory: Path | None = None,
                endpoint: Path | None = None) -> int:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("router run requires a command after --")
    label = _bounded_text(label, "label")
    session_id = _bounded_text(session_id, "session")
    agent_id = _bounded_text(agent_id, "agent")
    if agent_id is not None and session_id is None:
        raise ValueError("--agent requires --session")
    directory = (directory or state_dir()).expanduser().resolve()
    managed_directory = directory / "managed"
    managed_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    managed_directory.chmod(0o700)
    _prune_completion_files(managed_directory)
    managed_process_id = "managed-" + uuid.uuid4().hex
    completion = managed_directory / f"{managed_process_id}.completion.json"
    release_read, release_write = os.pipe()
    handshake_read, handshake_write = os.pipe()
    supervisor = None
    terminal = None
    previous = None
    try:
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("managed_gate.py")),
             "supervise", str(release_read), str(handshake_write), str(completion), *command],
            pass_fds=(release_read, handshake_write), process_group=0,
        )
        os.close(release_read)
        release_read = -1
        os.close(handshake_write)
        handshake_write = -1
        line = _read_handshake(handshake_read)
        os.close(handshake_read)
        handshake_read = -1
        if not line or len(line) > MAX_HANDSHAKE_BYTES:
            raise ValueError("Managed command supervisor did not provide process identity")
        handshake = json.loads(line)
        pid = handshake.get("pid") if isinstance(handshake, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("Managed command supervisor provided invalid process identity")
        argument_hash = hashlib.sha256(
            b"agent-router-managed-argv-v1\0" + canonical({"arguments": command[1:]}).encode()
        ).hexdigest()
        identity = creation_identity(pid)
        payload = {
            "request_version": "1",
            "managed_process_id": managed_process_id,
            "label": redact_secrets(label, 240),
            "session_id": session_id,
            "agent_id": agent_id,
            "pid": pid,
            "pgid": supervisor.pid,
            "process_start_time": _start_time(identity),
            "creation_identity": identity,
            "group_creation_identity": creation_identity(supervisor.pid),
            "executable": redact_secrets(Path(command[0]).name, 240),
            "argument_count": max(0, len(command) - 1),
            "argument_hash": argument_hash,
            "cwd": redact_secrets(str(Path.cwd().resolve()), 2000),
            "environment_allowlist": {},
            "started_at": int(time.time() * 1000),
        }
        _register(payload, endpoint or socket_path())
        terminal = _terminal_foreground_group(supervisor.pid)
        previous = _forward_signals(supervisor.pid)
        os.write(release_write, b"1")
    except BaseException as error:
        _restore_terminal(terminal)
        if previous is not None:
            for forwarded, handler in previous.items():
                signal.signal(forwarded, handler)
        if release_write != -1:
            os.close(release_write)
            release_write = -1
        if supervisor is not None:
            _stop_supervisor(supervisor)
        if isinstance(error, ValueError):
            completion.unlink(missing_ok=True)
        else:
            try:
                _prune_completion_files(managed_directory)
            except OSError as cleanup_error:
                error.add_note(
                    "Managed completion retention cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
        raise
    finally:
        for descriptor in (release_read, handshake_read, handshake_write):
            if descriptor != -1:
                os.close(descriptor)
    os.close(release_write)
    try:
        terminal = _wait_for_supervisor(supervisor, terminal)
    finally:
        _restore_terminal(terminal)
        for forwarded, handler in previous.items():
            signal.signal(forwarded, handler)
    completion_durable = completion.is_file()
    try:
        request("managed_process_finish", {"managed_process_id": managed_process_id},
                endpoint or socket_path(), timeout=5)
    except (OSError, ValueError) as error:
        if completion_durable:
            message = "completion evidence exists locally but Router has not reconciled it yet"
        else:
            message = "completion evidence could not be confirmed by Router"
        print(f"router run: {message}: {redact_secrets(str(error), 500)}", file=sys.stderr)
    if supervisor.returncode is None:
        raise RuntimeError("Managed command supervisor did not report an exit status")
    return supervisor.returncode
