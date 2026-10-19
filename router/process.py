"""POSIX ownership checks, with creation identity rather than PID alone."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class UncertainProcess(RuntimeError):
    pass


class UnconfirmedShutdown(UncertainProcess):
    pass


def verify_shutdown(record: dict) -> None:
    if not record.get("interrupt_first"):
        return
    executor = "Claude" if record.get("executor") == "claude" else "Codex"
    try:
        if Path(record["completion"]).with_suffix(".escalated").exists():
            raise UnconfirmedShutdown(f"{executor} required forced termination; foreground command shutdown is unconfirmed. Operator inspection required.")
        completion = json.loads(Path(record["completion"]).read_text())
        if completion["exit_code"] >= 0:
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise UnconfirmedShutdown(f"{executor} ended abruptly; foreground command shutdown is unconfirmed. Operator inspection required.")


def creation_identity(pid: int) -> str:
    if sys.platform == "linux":
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot}:{stat[19]}"
    if sys.platform == "darwin":
        # Layout from Apple's sys/proc_info.h, PROC_PIDTBSDINFO (3).
        class BSDInfo(ctypes.Structure):
            _fields_ = [("prefix", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16),
                        ("name", ctypes.c_char * 32), ("middle", ctypes.c_uint32 * 6),
                        ("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64)]
        info = BSDInfo()
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            raise OSError(ctypes.get_errno(), "Cannot establish process creation identity")
        return f"darwin:{info.seconds}:{info.microseconds}"
    raise ValueError("Agent Router requires a supported POSIX host (macOS or Linux)")


def owned_state(record: dict) -> str:
    try:
        os.killpg(record["pgid"], 0)
    except ProcessLookupError:
        return "stopped"
    except PermissionError:
        return "uncertain"
    try:
        if creation_identity(record["pid"]) == record["creation_identity"] and os.getpgid(record["pid"]) == record["pgid"]:
            return "live"
    except (OSError, ValueError):
        pass
    return "uncertain"


def supervised_state(record: dict, process: subprocess.Popen | None) -> str:
    if process is not None:
        process.poll()
    state = owned_state(record)
    if state == "uncertain" and process is not None:
        # Exit can occur between poll() and the probe. On Darwin an exiting
        # direct-child leader can yield EPERM until it becomes reapable. While
        # unreaped, its PID and group cannot be reused, so it is still owned.
        if process.poll() is None:
            return "exiting"
        state = owned_state(record)
    if state == "uncertain" and record.get("disposable") and Path(record["completion"]).exists():
        # The disposable gate kills its whole group after recording completion.
        # Reaping the leader can precede disappearance of another killed member.
        limit = time.monotonic() + 3
        while state == "uncertain" and time.monotonic() < limit:
            time.sleep(0.02)
            state = owned_state(record)
    return state


def stop_owned(record: dict, process: subprocess.Popen | None = None) -> None:
    _stop_group(record, process)
    verify_shutdown(record)


def _stop_group(record: dict, process: subprocess.Popen | None = None) -> None:
    state = supervised_state(record, process)
    if state == "stopped":
        return
    if state == "exiting":
        _await_group(record, process)
        return
    if state != "live":
        raise UncertainProcess("Process ownership or liveness is uncertain; no signal sent")
    if record.get("interrupt_first"):
        # Harnesses own foreground commands in separate groups; Ctrl-C lets the
        # harness close them before we terminate the owned CLI group.
        try:
            os.killpg(record["pgid"], signal.SIGINT)
        except ProcessLookupError:
            return
        limit = time.monotonic() + 3
        while time.monotonic() < limit:
            if supervised_state(record, process) == "stopped":
                return
            time.sleep(0.02)
        state = supervised_state(record, process)
        if state == "exiting":
            _await_group(record, process)
            return
        if state != "live":
            raise UncertainProcess("Ownership became uncertain during interruption; artifacts retained")
        # Persist before escalation: a normal exit code from a launcher must not
        # erase forced-shutdown uncertainty, even after a daemon restart.
        try:
            Path(record["completion"]).with_suffix(".escalated").touch(mode=0o600)
        except OSError as error:
            raise UncertainProcess("Cannot record forced termination; no escalation sent, operator inspection required") from error
    try:
        os.killpg(record["pgid"], signal.SIGTERM)
    except ProcessLookupError:
        return
    limit = time.monotonic() + 0.5
    while time.monotonic() < limit:
        if supervised_state(record, process) == "stopped":
            return
        time.sleep(0.02)
    state = supervised_state(record, process)
    if state == "live":
        os.killpg(record["pgid"], signal.SIGKILL)
    elif state not in {"stopped", "exiting"}:
        raise UncertainProcess("Ownership became uncertain during termination; artifacts retained")
    _await_group(record, process)


def _await_group(record: dict, process: subprocess.Popen | None) -> None:
    if process is not None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise UncertainProcess("Owned process group leader did not exit; artifacts retained") from error
    limit = time.monotonic() + 3
    while time.monotonic() < limit:
        if owned_state(record) == "stopped":
            return
        time.sleep(0.02)
    raise UncertainProcess("Cannot verify that the owned process group stopped")
