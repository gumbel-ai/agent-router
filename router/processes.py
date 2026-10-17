"""Read-only process-tree sampling for Claude and Codex sessions.

Only kernel process metadata is read: parent, group, start time, executable name,
resident memory, and CPU time. Command lines and environments are never read.
"""

import collections
import ctypes
import os
import sys
import threading
import time
from pathlib import Path

# A hook reports this many ancestors; the session's own process is among the nearest few.
MAX_ANCESTORS = 6
# Bound the parent walk when attributing a process to a session root.
MAX_TREE_DEPTH = 64
# CPU percent needs a window long enough for tick-granular counters to mean something.
MIN_CPU_WINDOW_SECONDS = 1.0


def _linux_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _linux_stat(pid: int) -> dict:
    raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    # The executable name sits in parentheses and may itself contain spaces or ')'.
    opening, closing = raw.find("("), raw.rfind(")")
    if opening <= 0 or closing <= opening:
        raise ValueError("Malformed Linux process stat")
    fields = raw[closing + 1:].split()
    return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]),
            "cpu_ticks": int(fields[11]) + int(fields[12]), "start_ticks": int(fields[19]),
            "executable": raw[opening + 1:closing]}


class _BSDInfo(ctypes.Structure):
    # Apple sys/proc_info.h, struct proc_bsdinfo (PROC_PIDTBSDINFO = 3).
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
                ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32), ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32), ("pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


class _TaskInfo(ctypes.Structure):
    # Apple sys/proc_info.h, struct proc_taskinfo (PROC_PIDTASKINFO = 4).
    _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                ("total_user", ctypes.c_uint64), ("total_system", ctypes.c_uint64),
                ("threads_user", ctypes.c_uint64), ("threads_system", ctypes.c_uint64),
                ("policy", ctypes.c_int32), ("faults", ctypes.c_int32), ("pageins", ctypes.c_int32),
                ("cow_faults", ctypes.c_int32), ("messages_sent", ctypes.c_int32),
                ("messages_received", ctypes.c_int32), ("syscalls_mach", ctypes.c_int32),
                ("syscalls_unix", ctypes.c_int32), ("csw", ctypes.c_int32), ("threadnum", ctypes.c_int32),
                ("numrunning", ctypes.c_int32), ("priority", ctypes.c_int32)]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _libproc():
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def _darwin_bsd(lib, pid: int) -> _BSDInfo:
    info = _BSDInfo()
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise OSError(ctypes.get_errno(), f"Cannot read process {pid}")
    return info


def _darwin_name(info: _BSDInfo) -> str:
    name = bytes(info.name).split(b"\0", 1)[0] or bytes(info.comm).split(b"\0", 1)[0]
    return name.decode(errors="replace")


def parent_and_identity(pid: int) -> tuple[int, str]:
    """The parent PID and creation identity (stable across PID reuse) of one process."""
    if sys.platform == "linux":
        stat = _linux_stat(pid)
        return stat["ppid"], f"linux:{_linux_boot_id()}:{stat['start_ticks']}"
    if sys.platform == "darwin":
        info = _darwin_bsd(_libproc(), pid)
        return int(info.ppid), f"darwin:{info.start_sec}:{info.start_usec}"
    raise OSError("Process sampling is supported only on Linux and macOS")


def ancestry() -> list[list]:
    """[[pid, identity], ...] for this process's nearest ancestors, nearest first."""
    chain = []
    pid = os.getppid()
    while pid > 1 and len(chain) < MAX_ANCESTORS:
        try:
            parent, identity = parent_and_identity(pid)
        except (OSError, ValueError, IndexError):
            break  # Unreadable ancestors (other users, hidepid, exited) are outside the session.
        chain.append([pid, identity])
        pid = parent
    return chain


def sample() -> dict[int, dict]:
    """Every visible process: pid, ppid, pgid, identity, executable, started_at, rss_bytes, cpu_ns."""
    processes = {}
    if sys.platform == "linux":
        boot_id = _linux_boot_id()
        ticks = os.sysconf("SC_CLK_TCK")
        page = os.sysconf("SC_PAGE_SIZE")
        boot_seconds = next((int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                             if line.split()[:1] == ["btime"]), None)
        if boot_seconds is None:
            raise OSError("Linux /proc/stat has no btime")
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = _linux_stat(int(entry.name))
                rss = int((entry / "statm").read_text().split()[1]) * page
                if _linux_stat(stat["pid"])["start_ticks"] != stat["start_ticks"]:
                    continue  # The PID was reused between reads; the measurements are another process's.
            except (OSError, ValueError, IndexError):
                continue  # Processes exit between reads, and some are not readable.
            processes[stat["pid"]] = {
                "pid": stat["pid"], "ppid": stat["ppid"], "pgid": stat["pgid"],
                "identity": f"linux:{boot_id}:{stat['start_ticks']}", "executable": stat["executable"],
                "started_at": (boot_seconds * 1000) + stat["start_ticks"] * 1000 // ticks,
                "rss_bytes": rss, "cpu_ns": stat["cpu_ticks"] * 1_000_000_000 // ticks,
            }
        return processes
    if sys.platform == "darwin":
        lib = _libproc()
        timebase = _Timebase()
        ctypes.CDLL("/usr/lib/libSystem.dylib").mach_timebase_info(ctypes.byref(timebase))
        size = lib.proc_listpids(1, 0, None, 0)
        if size <= 0:
            raise OSError(ctypes.get_errno(), "Cannot list processes")
        pids = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 64))()
        size = lib.proc_listpids(1, 0, ctypes.byref(pids), ctypes.sizeof(pids))
        if size <= 0:
            raise OSError(ctypes.get_errno(), "Cannot list processes")
        for pid in pids[:size // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            task = _TaskInfo()
            try:
                info = _darwin_bsd(lib, pid)
            except OSError:
                continue
            if lib.proc_pidinfo(pid, 4, 0, ctypes.byref(task), ctypes.sizeof(task)) != ctypes.sizeof(task):
                continue
            try:
                again = _darwin_bsd(lib, pid)
            except OSError:
                continue
            if (again.start_sec, again.start_usec) != (info.start_sec, info.start_usec):
                continue  # The PID was reused between reads; the measurements are another process's.
            # Task CPU times are Mach absolute units (nanoseconds only on Intel).
            cpu_ns = (task.total_user + task.total_system) * timebase.numer // timebase.denom
            processes[pid] = {
                "pid": pid, "ppid": int(info.ppid), "pgid": int(info.pgid),
                "identity": f"darwin:{info.start_sec}:{info.start_usec}", "executable": _darwin_name(info),
                "started_at": info.start_sec * 1000 + info.start_usec // 1000,
                "rss_bytes": int(task.resident_size), "cpu_ns": int(cpu_ns),
            }
        return processes
    raise OSError("Process sampling is supported only on Linux and macOS")


class SessionProcesses:
    """Attribute sampled processes to the session whose process they descend from."""

    def __init__(self, sampler=sample):
        self.sampler = sampler
        self._baselines: collections.deque[tuple[float, dict[tuple[int, str], int]]] = collections.deque(maxlen=3)
        # Snapshots can run concurrently; a sample and its CPU baseline must stay paired.
        self._lock = threading.Lock()

    def records(self, host_id: str, roots: list[dict], limit: int) -> list[dict]:
        """roots: [{"source", "session_id", "pid", "identity"}]; at most `limit` records, roots first."""
        with self._lock:
            processes = self.sampler()
            now = time.monotonic()
            # PID and start time together: processes can share a start tick.
            cpu = {(process["pid"], process["identity"]): process["cpu_ns"] for process in processes.values()}
            # Compare with the newest baseline at least a full window old, so watchers polling
            # moments apart still measure over a meaningful interval.
            previous = next((entry for entry in reversed(self._baselines)
                             if now - entry[0] >= MIN_CPU_WINDOW_SECONDS), None)
            if not self._baselines or now - self._baselines[-1][0] >= MIN_CPU_WINDOW_SECONDS:
                self._baselines.append((now, cpu))
        # A root counts only while its PID still belongs to the same process.
        live_roots = {}
        for root in roots:  # Most recently active first: it keeps a process two sessions share.
            if processes.get(root["pid"], {}).get("identity") == root["identity"]:
                live_roots.setdefault(root["pid"], root)
        attributed = []
        for process in processes.values():
            pid, depth = process["pid"], 0
            while pid not in live_roots and pid in processes and depth < MAX_TREE_DEPTH:
                pid, depth = processes[pid]["ppid"], depth + 1
            if pid in live_roots:
                attributed.append((depth, process, live_roots[pid]))
        attributed.sort(key=lambda item: (item[0], -item[1]["rss_bytes"]))
        records = []
        for _, process, root in attributed[:limit]:
            cpu_pct = None
            key = (process["pid"], process["identity"])
            if previous is not None and key in previous[1]:
                used = process["cpu_ns"] - previous[1][key]
                cpu_pct = round(max(0.0, used / ((now - previous[0]) * 1e9) * 100), 1)
                if cpu_pct > 100 * (os.cpu_count() or 1):
                    cpu_pct = None  # Counters read mid-sweep can disagree; report unknown, not nonsense.
            records.append({
                "host_id": host_id, "pid": process["pid"], "pgid": process["pgid"], "status": "running",
                "label": process["executable"], "started_at": process["started_at"],
                "attribution": "session", "source": root["source"], "session_id": root["session_id"],
                "cpu_pct": cpu_pct, "rss_bytes": process["rss_bytes"],
            })
        return records
