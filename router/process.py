"""POSIX ownership checks, with creation identity rather than PID alone."""

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class UncertainProcess(RuntimeError):
    pass


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
        # Exit can occur between poll() and the probe. On Darwin an unreaped
        # direct-child leader can yield EPERM; reap it before judging liveness.
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            return state
        return owned_state(record)
    return state


def stop_owned(record: dict, process: subprocess.Popen | None = None) -> None:
    state = supervised_state(record, process)
    if state == "stopped":
        return
    if state != "live":
        raise UncertainProcess("Process ownership or liveness is uncertain; no signal sent")
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
    elif state != "stopped":
        raise UncertainProcess("Ownership became uncertain during termination; artifacts retained")
    if process is not None:
        process.wait(timeout=5)
    limit = time.monotonic() + 3
    while time.monotonic() < limit:
        if owned_state(record) == "stopped":
            return
        time.sleep(0.02)
    raise UncertainProcess("Cannot verify that the owned process group stopped")
