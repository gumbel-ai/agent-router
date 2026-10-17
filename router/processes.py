"""Durable managed-process evidence and privacy-preserving process-tree sampling."""

import ctypes
import json
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from itertools import islice
from pathlib import Path

from router.db import Store, now_ms
from router.dispatch import canonical
from router.managed_completion import MAX_COMPLETION_BYTES, validate_completion
from router.process import creation_identity
from router.redaction import redact_secrets

SAMPLE_INTERVAL_SECONDS = 5.0
MAX_KNOWN_DESCENDANTS = 512
RECENT_HISTORY_MS = 300_000
TERMINAL_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
MAX_TERMINAL_RECORDS = 10_000
MAX_UNCERTAIN_RECORDS = 10_000
MAINTENANCE_INTERVAL_MS = 60 * 60 * 1000
SAFE_ENVIRONMENT_NAMES = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"})
_MANAGED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _required_text(value: object, name: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _optional_text(value: object, name: str, limit: int = 240) -> str | None:
    if value is None:
        return None
    return _required_text(value, name, limit)


def _positive_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _parse_linux_stat(value: str) -> dict:
    """Parse /proc/<pid>/stat without treating spaces or ')' in comm as separators."""
    opening = value.find("(")
    closing = value.rfind(")")
    if opening <= 0 or closing <= opening:
        raise ValueError("Malformed Linux process stat")
    try:
        pid = int(value[:opening].strip())
        fields = value[closing + 1:].split()
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "cpu_ticks": int(fields[11]) + int(fields[12]),
            "process_start_time": fields[19],
        }
    except (IndexError, ValueError) as error:
        raise ValueError("Malformed Linux process stat") from error


def _descendant_pids(processes: list[dict], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(process["ppid"], []).append(process["pid"])
    return _descendant_pids_from_index(children, root_pid)


def _descendant_pids_from_index(children: dict[int, list[int]], root_pid: int) -> set[int]:
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in found and child != root_pid:
                found.add(child)
                pending.append(child)
    return found


def _identity(process: dict) -> str:
    return f'{process["boot_id"]}:{process["pid"]}:{process["process_start_time"]}'


def _start_from_creation_identity(identity: str) -> str:
    if identity.startswith("linux:"):
        return identity.rsplit(":", 1)[-1]
    if identity.startswith("darwin:"):
        return identity.removeprefix("darwin:")
    raise ValueError("Managed process has unsupported creation identity")


def _display_text(value: object, limit: int = 2000) -> str:
    """Make kernel-provided display text safe without invalidating a sample."""
    if not isinstance(value, str):
        return "[unavailable]"
    safe = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in value
    ).strip()
    return redact_secrets(safe or "[unavailable]", limit)


class SystemProcessSampler:
    """Read process metadata from kernel APIs without command lines or environments."""

    def __init__(self, boot_id: str):
        self.boot_id = boot_id
        self._previous: tuple[float, dict[str, int]] | None = None
        self.unreadable_pids: set[int] = set()

    def sample(self) -> list[dict]:
        self.unreadable_pids = set()
        if sys.platform.startswith("linux"):
            records = self._linux()
        elif sys.platform == "darwin":
            records = self._darwin()
        else:
            raise OSError("Process sampling is supported only on Linux and macOS")
        current = time.monotonic()
        previous = self._previous
        if previous is not None:
            elapsed = current - previous[0]
            ticks = os.sysconf("SC_CLK_TCK") if sys.platform.startswith("linux") else 1_000_000_000
            if elapsed > 0:
                for record in records:
                    before = previous[1].get(_identity(record))
                    if before is not None:
                        record["cpu_pct"] = max(
                            0.0, (record["cpu_time"] - before) / ticks / elapsed * 100.0,
                        )
        self._previous = (current, {_identity(record): record["cpu_time"] for record in records})
        return records

    def _linux(self) -> list[dict]:
        page_size = os.sysconf("SC_PAGE_SIZE")
        records = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                process = _parse_linux_stat((entry / "stat").read_text())
            except (FileNotFoundError, ProcessLookupError):
                # Processes commonly disappear between individual /proc reads.
                continue
            except (PermissionError, ValueError, IndexError, OSError):
                self.unreadable_pids.add(int(entry.name))
                continue
            try:
                resident_pages = int((entry / "statm").read_text().split()[1])
                rss_bytes = resident_pages * page_size
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (PermissionError, ValueError, IndexError, OSError):
                rss_bytes = None
            try:
                executable = os.readlink(entry / "exe")
            except PermissionError:
                # Kernel comm is a bounded executable name, not command text.
                try:
                    executable = (entry / "comm").read_text().rstrip("\n")
                except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                    executable = "[unavailable]"
            except (FileNotFoundError, ProcessLookupError, OSError):
                executable = "[unavailable]"
            process.update(
                boot_id=self.boot_id,
                creation_identity=f'linux:{self.boot_id}:{process["process_start_time"]}',
                executable=executable,
                cpu_time=process.pop("cpu_ticks"),
                cpu_pct=None,
                rss_bytes=rss_bytes,
            )
            records.append(process)
        return records

    def _darwin(self) -> list[dict]:
        # Layouts are from Apple's sys/proc_info.h. libproc returns structured
        # records, so no process command or environment text is inspected.
        class BSDInfo(ctypes.Structure):
            _fields_ = [
                ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
                ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
                ("svgid", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
                ("pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
            ]

        class TaskInfo(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                ("total_user", ctypes.c_uint64), ("total_system", ctypes.c_uint64),
                ("threads_user", ctypes.c_uint64), ("threads_system", ctypes.c_uint64),
                ("policy", ctypes.c_int32), ("faults", ctypes.c_int32),
                ("pageins", ctypes.c_int32), ("cow_faults", ctypes.c_int32),
                ("messages_sent", ctypes.c_int32), ("messages_received", ctypes.c_int32),
                ("syscalls_mach", ctypes.c_int32), ("syscalls_unix", ctypes.c_int32),
                ("csw", ctypes.c_int32), ("threadnum", ctypes.c_int32),
                ("numrunning", ctypes.c_int32), ("priority", ctypes.c_int32),
            ]

        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        count = lib.proc_listpids(1, 0, None, 0)
        if count <= 0:
            raise OSError(ctypes.get_errno(), "Cannot list processes")
        pids = (ctypes.c_int * (count // ctypes.sizeof(ctypes.c_int) + 32))()
        size = lib.proc_listpids(1, 0, ctypes.byref(pids), ctypes.sizeof(pids))
        if size < 0:
            raise OSError(ctypes.get_errno(), "Cannot list processes")
        records = []
        for pid in pids[:size // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            basic, task = BSDInfo(), TaskInfo()
            if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(basic), ctypes.sizeof(basic)) != ctypes.sizeof(basic):
                self.unreadable_pids.add(pid)
                continue
            if lib.proc_pidinfo(pid, 4, 0, ctypes.byref(task), ctypes.sizeof(task)) != ctypes.sizeof(task):
                self.unreadable_pids.add(pid)
                continue
            started = f"{basic.start_sec}:{basic.start_usec}"
            executable = bytes(basic.name).split(b"\0", 1)[0].decode(errors="replace") or bytes(
                basic.comm,
            ).split(b"\0", 1)[0].decode(errors="replace")
            records.append({
                "pid": pid, "ppid": int(basic.ppid), "pgid": int(basic.pgid),
                "boot_id": self.boot_id, "process_start_time": started,
                "creation_identity": f"darwin:{started}", "executable": executable,
                "cpu_time": int(task.total_user + task.total_system), "cpu_pct": None,
                "rss_bytes": int(task.resident_size),
            })
        return records


class ProcessMonitor:
    def __init__(
        self,
        store: Store,
        directory: Path,
        host_id: str,
        boot_id: str,
        interval_seconds: float = SAMPLE_INTERVAL_SECONDS,
        sampler=None,
        registration_probe=None,
    ):
        self.store = store
        self.directory = directory / "managed"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.host_id = host_id
        self.boot_id = boot_id
        if (not isinstance(interval_seconds, (int, float)) or isinstance(interval_seconds, bool)
                or not math.isfinite(interval_seconds) or interval_seconds <= 0):
            raise ValueError("interval_seconds must be finite and positive")
        self.interval_seconds = interval_seconds
        self.sampler = sampler or SystemProcessSampler(boot_id)
        self.registration_probe = registration_probe or self._verify_registration
        self._last_inferred: list[dict] = []
        self._health = {"partial": True, "error": "Process sampler has not run", "observed_at": None}
        self._health_lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_maintenance_at = 0

    @staticmethod
    def _verify_registration(pid: int, pgid: int, creation: str, group_creation: str) -> None:
        try:
            actual_creation = creation_identity(pid)
            actual_group_creation = creation_identity(pgid)
            actual_group = os.getpgid(pid)
        except (OSError, ValueError) as error:
            raise ValueError("Managed process identity cannot be verified") from error
        if (actual_creation != creation or actual_group_creation != group_creation
                or actual_group != pgid):
            raise ValueError("Managed process identity does not match the live process group")

    def completion_path(self, managed_process_id: str) -> Path:
        if (not isinstance(managed_process_id, str)
                or not _MANAGED_ID.fullmatch(managed_process_id)
                or redact_secrets(managed_process_id, 128) != managed_process_id):
            raise ValueError("Invalid managed_process_id")
        return self.directory / f"{managed_process_id}.completion.json"

    def register(self, payload: dict, *, registration_kind: str = "managed") -> dict:
        if registration_kind not in {"managed", "configured"}:
            raise ValueError("Unknown process registration kind")
        if not isinstance(payload, dict):
            raise ValueError(  # noqa: TRY004 - public daemon request validation
                "Managed process registration must be an object"
            )
        allowed = {
            "request_version",
            "managed_process_id", "host_id", "boot_id", "label", "session_id", "agent_id",
            "pid", "pgid", "process_start_time", "creation_identity",
            "group_creation_identity", "executable", "argument_count", "argument_hash",
            "cwd", "environment_allowlist", "started_at",
        }
        if set(payload) - allowed:
            raise ValueError("Managed process registration contains unknown fields")
        if payload.get("request_version") != "1":
            raise ValueError("Managed process registration requires request_version 1")
        managed_id = payload.get("managed_process_id")
        self.completion_path(managed_id)
        if payload.get("host_id", self.host_id) != self.host_id:
            raise ValueError("Managed process belongs to another host")
        if payload.get("boot_id", self.boot_id) != self.boot_id:
            raise ValueError("Managed process belongs to another boot")
        pid = _positive_integer(payload.get("pid"), "pid")
        pgid = _positive_integer(payload.get("pgid"), "pgid")
        identity = _required_text(payload.get("process_start_time"), "process_start_time", 240)
        creation = _required_text(payload.get("creation_identity"), "creation_identity", 240)
        if _start_from_creation_identity(creation) != identity:
            raise ValueError("process_start_time does not match creation_identity")
        group_creation = _required_text(
            payload.get("group_creation_identity"), "group_creation_identity", 240,
        )
        executable = redact_secrets(
            _required_text(payload.get("executable"), "executable", 2000), 2000,
        )
        label = redact_secrets(_required_text(payload.get("label"), "label", 240), 240)
        cwd = redact_secrets(_required_text(payload.get("cwd"), "cwd", 2000), 2000)
        session_id = _optional_text(payload.get("session_id"), "session_id")
        agent_id = _optional_text(payload.get("agent_id"), "agent_id")
        if any(value is not None and redact_secrets(value, 240) != value
               for value in (session_id, agent_id)):
            raise ValueError("Process attribution identifiers cannot contain credential material")
        if registration_kind == "configured" and (session_id is not None or agent_id is not None):
            raise ValueError("Configured process registration cannot claim session or agent attribution")
        if agent_id is not None and session_id is None:
            raise ValueError("agent_id requires session_id")
        environment = payload.get("environment_allowlist", {})
        if environment not in ({}, []):
            if (not isinstance(environment, list) or any(
                    not isinstance(name, str) or name not in SAFE_ENVIRONMENT_NAMES for name in environment)):
                raise ValueError("environment_allowlist contains an unsupported variable")
            environment = sorted(set(environment))
        else:
            environment = {}
        argument_hash = _required_text(payload.get("argument_hash"), "argument_hash", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", argument_hash):
            raise ValueError("argument_hash must be lowercase SHA-256")
        argument_count = _nonnegative_integer(payload.get("argument_count"), "argument_count")
        registered_at = now_ms()
        started_at = payload.get("started_at", registered_at)
        started_at = _nonnegative_integer(started_at, "started_at")
        if started_at > registered_at + 60_000:
            raise ValueError("started_at is implausibly far in the future")
        attribution = (
            "host" if registration_kind == "configured"
            else "exact" if agent_id is not None
            else "session" if session_id is not None
            else "host"
        )
        record = {
            "managed_process_id": managed_id, "host_id": self.host_id, "boot_id": self.boot_id,
            "pid": pid, "pgid": pgid, "process_start_time": identity,
            "creation_identity": creation, "group_creation_identity": group_creation,
            "label": label, "session_id": session_id, "agent_id": agent_id,
            "attribution": attribution, "executable": executable,
            "argument_hash": argument_hash, "argument_count": argument_count,
            "cwd": cwd, "environment_allowlist": environment,
            "status": "running", "registered_at": registered_at, "started_at": started_at,
            "last_sample_at": None, "finished_at": None, "exit_status": None, "signal": None,
            "elapsed_ms": 0, "cpu_pct": None, "rss_bytes": None, "peak_rss_bytes": None,
            "worker_count": 0,
            "shutdown_uncertain": False, "shutdown_was_uncertain": False,
            "descendant_tracking_truncated": False,
            "visibility_reason": None,
            "_known_descendants": [],
            "_known_descendant_pids": {},
            "_uncertain_since": None,
            "_uncertain_process_live": False,
            "_needs_reconciliation": False,
            "_unverified_group_presence": False,
            "_registration_kind": registration_kind,
        }
        immutable = (
            "host_id", "boot_id", "pid", "pgid", "process_start_time", "creation_identity",
            "group_creation_identity",
            "label", "session_id", "agent_id", "attribution", "executable", "argument_hash",
            "argument_count", "cwd", "environment_allowlist", "started_at",
            "_registration_kind",
        )
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT data FROM managed_processes WHERE managed_process_id=?", (managed_id,),
            ).fetchone()
            if row is not None:
                existing = json.loads(row[0])
                if any(existing.get(key) != record.get(key) for key in immutable):
                    raise ValueError("Conflicting managed process registration")
                return self._view(existing)
            self.registration_probe(pid, pgid, creation, group_creation)
            try:
                db.execute(
                    """INSERT INTO managed_processes(
                        managed_process_id,host_id,boot_id,pid,process_start_time,
                        status,monitor_active,finished_at,sort_at,data
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (managed_id, self.host_id, self.boot_id, pid, identity,
                     record["status"], 1, record["finished_at"], record["registered_at"],
                     canonical(record)),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Process identity is already registered") from error
        with self._health_lock:
            children: dict[int, list[int]] = {}
            for item in self._last_inferred:
                parent = item.get("_parent_pid")
                if isinstance(parent, int):
                    children.setdefault(parent, []).append(item["pid"])
            invalid_pids = {pid, *_descendant_pids_from_index(children, pid)}
            owns_group = registration_kind == "managed" or pid == pgid
            self._last_inferred = [
                item for item in self._last_inferred
                if item.get("pid") not in invalid_pids
                and (not owns_group or item.get("pgid") != pgid)
            ]
        return self._view(record)

    def _put(self, db: sqlite3.Connection, record: dict) -> None:
        monitor_active = int(
            record["status"] == "running"
            or record.get("_uncertain_process_live", False)
            or record.get("_needs_reconciliation", False)
        )
        db.execute(
            """UPDATE managed_processes SET
                host_id=?,boot_id=?,pid=?,process_start_time=?,status=?,monitor_active=?,
                finished_at=?,sort_at=?,data=?
                WHERE managed_process_id=?""",
            (record["host_id"], record["boot_id"], record["pid"],
             record["process_start_time"], record["status"], monitor_active,
             record.get("finished_at"), record.get("last_sample_at") or record["registered_at"],
             canonical(record),
             record["managed_process_id"]),
        )

    def _read_completion(self, record: dict) -> dict | None:
        path = self.completion_path(record["managed_process_id"])
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_COMPLETION_BYTES:
                raise ValueError("Invalid managed process completion file")
            value = os.read(descriptor, MAX_COMPLETION_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(value) > MAX_COMPLETION_BYTES:
            raise ValueError("Managed process completion file is too large")
        try:
            payload = json.loads(value)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid managed process completion file") from error
        return validate_completion(
            payload, started_at=record["started_at"], current_time=now_ms(),
        )

    def complete(self, managed_process_id: str) -> dict:
        self.completion_path(managed_process_id)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT data FROM managed_processes WHERE managed_process_id=?", (managed_process_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown managed_process_id")
        record = json.loads(row[0])
        if record["status"] in {"stopped", "failed"}:
            self.completion_path(managed_process_id).unlink(missing_ok=True)
            self._prune_terminal_history(now_ms())
            return self._view(record)
        completion = self._read_completion(record)
        if completion is None:
            current = self._managed_record(managed_process_id)
            if current["status"] in {"stopped", "failed"}:
                self._prune_terminal_history(now_ms())
                return current
            raise ValueError("Managed process completion evidence is unavailable")
        if not self.sample_once(include_dormant=True):
            with self.store.transaction() as db:
                current = self._get(db, managed_process_id)
                if current["status"] in {"running", "unknown"}:
                    self._merge_completion_evidence(current, completion)
                    current.update(status="unknown", shutdown_uncertain=True,
                                   shutdown_was_uncertain=True,
                                   visibility_reason="Process sampler unavailable during completion",
                                   _uncertain_since=current.get("_uncertain_since") or now_ms(),
                                   _uncertain_process_live=False,
                                   _needs_reconciliation=True)
                    self._put(db, current)
            return self._view(current)
        completed = self._managed_record(managed_process_id)
        if completed["status"] in {"stopped", "failed"}:
            self.completion_path(managed_process_id).unlink(missing_ok=True)
            self._prune_terminal_history(now_ms())
        return completed

    @staticmethod
    def _merge_completion_evidence(record: dict, completion: dict) -> None:
        record.update(
            finished_at=completion["finished_at"], exit_status=completion["exit_status"],
            signal=completion["signal"],
            peak_rss_bytes=max(record.get("peak_rss_bytes") or 0,
                               completion["peak_rss_bytes"]) or None,
            cpu_seconds=completion["cpu_seconds"],
            elapsed_ms=max(0, completion["finished_at"] - record["started_at"]),
        )

    def _get(self, db: sqlite3.Connection, managed_process_id: str) -> dict:
        row = db.execute(
            "SELECT data FROM managed_processes WHERE managed_process_id=?", (managed_process_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown managed_process_id")
        return json.loads(row[0])

    def _managed_record(self, managed_process_id: str) -> dict:
        with self.store.connect() as db:
            record = self._get(db, managed_process_id)
        return self._view(record)

    def reconcile_restart(self) -> None:
        with self.store.transaction() as db:
            rows = db.execute(
                """SELECT data FROM managed_processes WHERE host_id=? AND
                    status IN ('running','unknown')""",
                (self.host_id,),
            ).fetchall()
            for row in rows:
                record = json.loads(row[0])
                record.update(status="unknown", shutdown_uncertain=True, shutdown_was_uncertain=True,
                              visibility_reason="Router restarted; process state is being reconciled",
                              _uncertain_since=record.get("_uncertain_since") or now_ms(),
                              _uncertain_process_live=False,
                              _needs_reconciliation=True)
                self._put(db, record)
        self.sample_once(include_dormant=True)
        self._maintenance(now_ms(), force=True)

    def _prune_terminal_history(self, at: int) -> None:
        cutoff = at - TERMINAL_RETENTION_MS
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT managed_process_id,sort_at FROM managed_processes
                    WHERE host_id=? AND status IN ('stopped','failed')
                    ORDER BY sort_at DESC""",
                (self.host_id,),
            ).fetchall()
            expired = {
                row["managed_process_id"] for index, row in enumerate(rows)
                if index >= MAX_TERMINAL_RECORDS or row["sort_at"] < cutoff
            }
            uncertain_rows = db.execute(
                """SELECT managed_process_id,data FROM managed_processes
                    WHERE host_id=? AND status='unknown' AND
                        coalesce(json_extract(data,'$._uncertain_process_live'),0)=0
                    ORDER BY coalesce(json_extract(data,'$._uncertain_since'),sort_at) DESC""",
                (self.host_id,),
            ).fetchall()
            expired.update(
                row["managed_process_id"] for index, row in enumerate(uncertain_rows)
                if index >= MAX_UNCERTAIN_RECORDS
                or (json.loads(row["data"]).get("_uncertain_since") or at) < cutoff
            )
        if expired:
            with self.store.transaction() as db:
                db.executemany(
                    "DELETE FROM managed_processes WHERE managed_process_id=?",
                    ((managed_id,) for managed_id in expired),
                )
        for managed_id in expired:
            self.completion_path(managed_id).unlink(missing_ok=True)

    def _cleanup_completion_files(self) -> None:
        with self.store.connect() as db:
            records = {
                row["managed_process_id"]: row["status"]
                for row in db.execute(
                    """SELECT managed_process_id,status FROM managed_processes
                        WHERE host_id=?""", (self.host_id,),
                )
            }
        suffix = ".completion.json"
        for path in self.directory.glob(f"*{suffix}"):
            managed_id = path.name.removesuffix(suffix)
            status = records.get(managed_id)
            terminal = status in {"stopped", "failed"}
            if (_MANAGED_ID.fullmatch(managed_id)
                    and (status is None or terminal)):
                path.unlink(missing_ok=True)

    def _maintenance(self, at: int, *, force: bool = False) -> None:
        if (not force and self._last_maintenance_at
                and at - self._last_maintenance_at < MAINTENANCE_INTERVAL_MS):
            return
        self._cleanup_completion_files()
        self._prune_terminal_history(at)
        self._last_maintenance_at = at

    def sample_once(self, *, include_dormant: bool = False) -> bool:
        with self._sample_lock:
            try:
                processes = self.sampler.sample()
                if not isinstance(processes, list) or any(not isinstance(item, dict) for item in processes):
                    raise ValueError("Process sampler returned malformed data")
                unreadable_pids = getattr(self.sampler, "unreadable_pids", set())
                if (not isinstance(unreadable_pids, set)
                        or any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                               for pid in unreadable_pids)):
                    raise ValueError("Process sampler returned malformed unreadable PID evidence")
                self._accept_sample(
                    processes, unreadable_pids, include_dormant=include_dormant,
                )
                return True
            except Exception as error:  # noqa: BLE001 - sampler isolation boundary
                self._set_error(error)
                return False

    def _set_error(self, error: Exception) -> None:
        message = redact_secrets(f"{type(error).__name__}: {error}", 500)
        with self._health_lock:
            self._last_inferred = []
            self._health = {"partial": True, "error": message, "observed_at": now_ms()}

    def _accept_sample(
        self, processes: list[dict], unreadable_pids: set[int], *, include_dormant: bool,
    ) -> None:
        observed_at = now_ms()
        normalized = []
        for process in processes:
            pid = _positive_integer(process.get("pid"), "sample pid")
            ppid = _nonnegative_integer(process.get("ppid"), "sample ppid")
            pgid = _nonnegative_integer(process.get("pgid"), "sample pgid")
            raw_start = process.get("process_start_time")
            if not isinstance(raw_start, (str, int)) or isinstance(raw_start, bool):
                raise ValueError(  # noqa: TRY004 - sampler contract validation
                    "sample process_start_time must be a string or integer"
                )
            start = _required_text(str(raw_start), "sample process_start_time", 240)
            executable = _display_text(process.get("executable"))
            sample_boot = _required_text(process.get("boot_id", self.boot_id), "sample boot_id", 240)
            if sample_boot != self.boot_id:
                raise ValueError("Process sampler returned a process from another boot")
            cpu_pct = process.get("cpu_pct")
            if (cpu_pct is not None and
                    (not isinstance(cpu_pct, (int, float)) or isinstance(cpu_pct, bool)
                     or not math.isfinite(cpu_pct) or cpu_pct < 0)):
                raise ValueError("sample cpu_pct must be finite and non-negative")
            rss_bytes = process.get("rss_bytes")
            if (rss_bytes is not None and
                    (not isinstance(rss_bytes, int) or isinstance(rss_bytes, bool) or rss_bytes < 0)):
                raise ValueError("sample rss_bytes must be a non-negative integer")
            normalized.append({
                "pid": pid, "ppid": ppid, "pgid": pgid,
                "boot_id": sample_boot,
                "process_start_time": start,
                "creation_identity": _required_text(
                    process.get("creation_identity"), "sample creation_identity", 240,
                ),
                "executable": executable,
                "cpu_pct": cpu_pct, "rss_bytes": rss_bytes,
            })
        by_pid = {process["pid"]: process for process in normalized}
        if len(by_pid) != len(normalized):
            raise ValueError("Process sampler returned duplicate PIDs")
        children: dict[int, list[int]] = {}
        groups: dict[int, list[dict]] = {}
        for process in normalized:
            children.setdefault(process["ppid"], []).append(process["pid"])
            groups.setdefault(process["pgid"], []).append(process)
        present = {_identity(process) for process in normalized}
        by_identity = {_identity(process): process for process in normalized}
        completion_paths = list(islice(
            self.directory.glob("*.completion.json"), MAX_TERMINAL_RECORDS + 1,
        ))
        completion_backlog_truncated = len(completion_paths) > MAX_TERMINAL_RECORDS
        completion_ids = [
            path.name.removesuffix(".completion.json")
            for path in completion_paths[:MAX_TERMINAL_RECORDS]
            if _MANAGED_ID.fullmatch(path.name.removesuffix(".completion.json"))
        ]
        with self.store.connect() as db:
            selected = {
                row["managed_process_id"]: row["data"] for row in db.execute(
                    """SELECT managed_process_id,data FROM managed_processes
                        WHERE host_id=? AND status IN ('running','unknown')
                        AND (? OR monitor_active=1)""",
                    (self.host_id, int(include_dormant)),
                )
            }
            for offset in range(0, len(completion_ids), 400):
                chunk = completion_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                selected.update({
                    row["managed_process_id"]: row["data"] for row in db.execute(
                        f"""SELECT managed_process_id,data FROM managed_processes
                            WHERE host_id=? AND status IN ('running','unknown')
                            AND managed_process_id IN ({placeholders})""",
                        (self.host_id, *chunk),
                    )
                })
        records = [(json.loads(value), value) for value in selected.values()]
        terminal_ids = []
        unreadable_leaders = 0
        incomplete_resources = 0
        live_uncertain = 0
        unverified_groups = 0
        for record, _original in records:
            leader_unreadable, resources_incomplete = self._apply_managed_sample(
                record, by_pid, by_identity, children, groups, present,
                unreadable_pids, observed_at,
            )
            unreadable_leaders += int(leader_unreadable)
            incomplete_resources += int(resources_incomplete)
            live_uncertain += int(
                record["status"] == "unknown"
                and record.get("_uncertain_process_live", False)
            )
            unverified_groups += int(record.get("_unverified_group_presence", False))
            if record["status"] in {"stopped", "failed"}:
                terminal_ids.append(record["managed_process_id"])
        changed_records = [
            record for record, original in records if canonical(record) != original
        ]
        if changed_records:
            with self.store.transaction() as db:
                for record in changed_records:
                    self._put(db, record)
        for managed_id in terminal_ids:
            self.completion_path(managed_id).unlink(missing_ok=True)
        self._maintenance(observed_at)
        inferred, unreadable_roots, missing_roots, unmatched_roots = self._infer(
            by_pid, children, groups, unreadable_pids, observed_at,
        )
        with self._health_lock:
            self._last_inferred = inferred
            problems = []
            if unreadable_roots:
                problems.append(
                    f"{len(unreadable_roots)} Claude session root process"
                    f"{'es are' if len(unreadable_roots) != 1 else ' is'} temporarily unreadable"
                )
            if missing_roots:
                problems.append(
                    f"{missing_roots} Claude session"
                    f"{'s lack' if missing_roots != 1 else ' lacks'} process identity evidence"
                )
            if unmatched_roots:
                problems.append(
                    f"{unmatched_roots} Claude session process root"
                    f"{'s do' if unmatched_roots != 1 else ' does'} not match the current process table"
                )
            if unreadable_leaders:
                problems.append(
                    f"{unreadable_leaders} managed process leader"
                    f"{'s are' if unreadable_leaders != 1 else ' is'} temporarily unreadable"
                )
            if incomplete_resources:
                problems.append(
                    f"{incomplete_resources} managed process record"
                    f"{'s have' if incomplete_resources != 1 else ' has'} incomplete resource metrics"
                )
            if live_uncertain:
                problems.append(
                    f"{live_uncertain} managed process shutdown state"
                    f"{'s remain' if live_uncertain != 1 else ' remains'} uncertain while members are live"
                )
            if unverified_groups:
                problems.append(
                    f"{unverified_groups} managed process group"
                    f"{'s have' if unverified_groups != 1 else ' has'} unverified live membership"
                )
            if completion_backlog_truncated:
                problems.append(
                    f"Completion evidence scan is limited to {MAX_TERMINAL_RECORDS} files"
                )
            self._health = {
                "partial": bool(problems),
                "error": "; ".join(problems) if problems else None,
                "observed_at": observed_at,
            }

    def _apply_managed_sample(
        self, record: dict, by_pid: dict[int, dict], by_identity: dict[str, dict],
        children: dict[int, list[int]], groups: dict[int, list[dict]], present: set[str],
        unreadable_pids: set[int], observed_at: int,
    ) -> tuple[bool, bool]:
        leader = by_pid.get(record["pid"])
        leader_matches = bool(
            leader and leader["boot_id"] == record["boot_id"]
            and leader["process_start_time"] == record["process_start_time"]
            and leader["creation_identity"] == record["creation_identity"]
        )
        known_pids = dict(record.get("_known_descendant_pids", {}))
        previous_known = set(record.get("_known_descendants", []))
        unreadable_known = {
            identity for identity in previous_known
            if known_pids.get(identity) in unreadable_pids
        }
        known = (previous_known & present) | unreadable_known
        if record["boot_id"] == self.boot_id and record["pid"] in unreadable_pids:
            record.update(
                status="unknown", shutdown_uncertain=True, shutdown_was_uncertain=True,
                visibility_reason="Managed process leader is temporarily unreadable",
                _uncertain_since=record.get("_uncertain_since") or observed_at,
                _uncertain_process_live=bool(known & present),
                _needs_reconciliation=True,
                _unverified_group_presence=False,
                cpu_pct=None, rss_bytes=None,
            )
            return True, False
        if leader_matches:
            descendants = _descendant_pids_from_index(children, record["pid"])
            known.update(_identity(by_pid[pid]) for pid in descendants if pid in by_pid)
            tracks_group = (
                record.get("_registration_kind") == "managed"
                or record["pid"] == record["pgid"]
            )
            if tracks_group:
                known.update(_identity(process) for process in groups.get(record["pgid"], [])
                             if process["pid"] != record["pid"])
            observed_members = set(known)
            truncated = len(known) > MAX_KNOWN_DESCENDANTS
            if truncated:
                known = set(sorted(known)[-MAX_KNOWN_DESCENDANTS:])
            record["_known_descendants"] = sorted(known)
            record["_known_descendant_pids"] = {
                identity: by_identity[identity]["pid"]
                for identity in known if identity in by_identity
            } | {
                identity: known_pids[identity]
                for identity in unreadable_known if identity in known_pids
            }
            record["descendant_tracking_truncated"] = truncated
            members = [
                leader,
                *(by_identity[identity] for identity in observed_members if identity in by_identity),
            ]
            worker_identities = set(observed_members)
            if record.get("_registration_kind") == "managed" and record["pgid"] != record["pid"]:
                members = [item for item in members if item["pid"] != record["pgid"]]
                worker_identities = {
                    identity for identity in worker_identities
                    if (by_identity.get(identity) or {}).get("pid") != record["pgid"]
                    and known_pids.get(identity) != record["pgid"]
                }
            unavailable_members = {
                identity for identity in worker_identities if identity not in by_identity
            }
            cpu_complete = not unavailable_members and all(
                item.get("cpu_pct") is not None for item in members
            )
            rss_complete = not unavailable_members and all(
                item.get("rss_bytes") is not None for item in members
            )
            cpu_pct = sum(item["cpu_pct"] for item in members) if cpu_complete else None
            rss_bytes = sum(item["rss_bytes"] for item in members) if rss_complete else None
            resources_incomplete = not cpu_complete or not rss_complete
            record.update(
                status="running", last_sample_at=observed_at,
                elapsed_ms=max(0, observed_at - record["started_at"]),
                cpu_pct=cpu_pct, rss_bytes=rss_bytes,
                peak_rss_bytes=max(record.get("peak_rss_bytes") or 0, rss_bytes or 0) or None,
                worker_count=len(worker_identities),
                shutdown_uncertain=False,
                visibility_reason=(
                    "Resource usage is incomplete for one or more managed process members"
                    if resources_incomplete else None
                ),
                _uncertain_since=None, _uncertain_process_live=False,
                _needs_reconciliation=False,
                _unverified_group_presence=False,
            )
            return False, resources_incomplete
        group_leader = by_pid.get(record["pgid"])
        group_member_live = False
        unverified_group_presence = False
        tracks_group = (
            record.get("_registration_kind") == "managed"
            or record["pid"] == record["pgid"]
        )
        if record["boot_id"] == self.boot_id and tracks_group:
            if group_leader is not None:
                group_member_live = (
                    group_leader["creation_identity"] == record["group_creation_identity"]
                )
            else:
                unverified_group_presence = bool(groups.get(record["pgid"]))
        observed_descendant_live = (
            bool(known & present) or bool(unreadable_known) or group_member_live
        )
        descendant_uncertain = (
            observed_descendant_live
            or (record["boot_id"] == self.boot_id
                and record.get("descendant_tracking_truncated", False))
        )
        live_members = {
            identity: by_identity[identity] for identity in known if identity in by_identity
        }
        if group_member_live:
            live_members.update({_identity(item): item for item in groups.get(record["pgid"], [])})
        if record.get("_registration_kind") == "managed" and record["pgid"] != record["pid"]:
            live_members = {
                identity: item for identity, item in live_members.items()
                if item["pid"] != record["pgid"]
            }
        unavailable_members = bool(unreadable_known)
        cpu_complete = bool(live_members) and not unavailable_members and all(
            item.get("cpu_pct") is not None for item in live_members.values()
        )
        rss_complete = bool(live_members) and not unavailable_members and all(
            item.get("rss_bytes") is not None for item in live_members.values()
        )
        cpu_pct = sum(item["cpu_pct"] for item in live_members.values()) if cpu_complete else None
        rss_bytes = sum(item["rss_bytes"] for item in live_members.values()) if rss_complete else None
        resources_incomplete = bool(live_members or unavailable_members) and (
            not cpu_complete or not rss_complete
        )
        try:
            completion = self._read_completion(record)
        except ValueError as error:
            completion = None
            completion_error = str(error)
        else:
            completion_error = None
        if (record["status"] == "unknown" and record.get("_uncertain_since") is not None
                and not record.get("_uncertain_process_live", False)
                and completion is None and not descendant_uncertain
                and not unverified_group_presence):
            record["_needs_reconciliation"] = False
            record["_unverified_group_presence"] = False
            return False, False
        record["last_sample_at"] = observed_at
        record["elapsed_ms"] = max(0, observed_at - record["started_at"])
        record["rss_bytes"] = rss_bytes
        record["cpu_pct"] = cpu_pct
        record["worker_count"] = len(live_members)
        record["peak_rss_bytes"] = max(
            record.get("peak_rss_bytes") or 0, rss_bytes or 0,
        ) or None
        if completion is not None:
            self._merge_completion_evidence(record, completion)
        if completion is not None and not descendant_uncertain and not unverified_group_presence:
            record.update(
                status="stopped" if completion["exit_status"] == 0 else "failed",
                shutdown_uncertain=False, visibility_reason=None,
                _uncertain_since=None, _uncertain_process_live=False,
                _needs_reconciliation=False,
                _unverified_group_presence=False,
            )
        else:
            reason = completion_error or (
                "Managed leader exited while a known descendant or process-group member remains"
                if descendant_uncertain else
                "Process-group membership is unverified after its leader exited"
                if unverified_group_presence else
                "Managed process disappeared without trusted completion evidence"
            )
            record.update(status="unknown", shutdown_uncertain=True, shutdown_was_uncertain=True,
                          visibility_reason=reason,
                          _uncertain_since=record.get("_uncertain_since") or observed_at,
                          _uncertain_process_live=observed_descendant_live,
                          _needs_reconciliation=unverified_group_presence,
                          _unverified_group_presence=unverified_group_presence)
        return False, resources_incomplete

    def _infer(
        self, by_pid: dict[int, dict], children: dict[int, list[int]],
        groups: dict[int, list[dict]], unreadable_pids: set[int], observed_at: int,
    ) -> tuple[list[dict], set[int], int, int]:
        recent_cutoff = observed_at - RECENT_HISTORY_MS
        with self.store.connect() as db:
            selected_sessions = {
                row["session_id"]: row["data"] for row in db.execute(
                    """SELECT session_id,data FROM sessions
                        WHERE host_id=? AND source='claude'
                        AND json_extract(data,'$.status') IN ('running','idle','unknown')
                        AND json_extract(data,'$.last_event_at')>=?""",
                    (self.host_id, recent_cutoff),
                )
            }
            candidate_pids = sorted({*by_pid, *unreadable_pids})
            for offset in range(0, len(candidate_pids), 400):
                chunk = candidate_pids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                selected_sessions.update({
                    row["session_id"]: row["data"] for row in db.execute(
                        f"""SELECT session_id,data FROM sessions
                            WHERE host_id=? AND source='claude'
                            AND json_extract(data,'$.status') IN ('running','idle','unknown')
                            AND json_extract(data,'$.claude_pid') IN ({placeholders})""",
                        (self.host_id, *chunk),
                    )
                })
            sessions = [json.loads(value) for value in selected_sessions.values()]
            managed_records = [json.loads(row[0]) for row in db.execute(
                """SELECT data FROM managed_processes WHERE host_id=? AND
                    monitor_active=1""",
                (self.host_id,),
            )]
            managed = {_identity(record) for record in managed_records}
            managed.update(
                identity
                for record in managed_records
                for identity in record.get("_known_descendants", [])
            )
            managed_groups = set()
            for record in managed_records:
                if record["boot_id"] != self.boot_id:
                    continue
                if (record.get("_registration_kind") != "managed"
                        and record["pid"] != record["pgid"]):
                    continue
                group_leader = by_pid.get(record["pgid"])
                if (group_leader is not None
                        and group_leader["creation_identity"] != record["group_creation_identity"]):
                    continue
                if (group_leader is not None
                        and group_leader["creation_identity"] == record["group_creation_identity"]):
                    managed_groups.add(record["pgid"])
        inferred: dict[str, dict] = {}
        unreadable_roots: set[int] = set()
        missing_roots = 0
        unmatched_roots = 0
        for session in sorted(sessions, key=lambda item: item.get("session_id", "")):
            pid = session.get("claude_pid")
            start = session.get("claude_process_start_time")
            creation = session.get("claude_creation_identity")
            last_event_at = session.get("last_event_at")
            recent = (
                isinstance(last_event_at, int) and not isinstance(last_event_at, bool)
                and last_event_at >= recent_cutoff
            )
            if (not isinstance(pid, int) or isinstance(pid, bool)
                    or not isinstance(start, (str, int)) or not isinstance(creation, str)):
                missing_roots += int(recent)
                continue
            if pid in unreadable_pids:
                if recent:
                    unreadable_roots.add(pid)
                continue
            root = by_pid.get(pid)
            if (root is None or str(root["process_start_time"]) != str(start)
                    or root["creation_identity"] != creation):
                unmatched_roots += int(recent)
                continue
            for member_pid in {pid, *_descendant_pids_from_index(children, pid)}:
                process = by_pid.get(member_pid)
                if (process is None or _identity(process) in managed
                        or process["pgid"] in managed_groups):
                    continue
                identity = _identity(process)
                inferred.setdefault(identity, {
                    "host_id": self.host_id, "boot_id": self.boot_id,
                    "pid": process["pid"], "pgid": process["pgid"],
                    "process_start_time": process["process_start_time"],
                    "creation_identity": process["creation_identity"],
                    "session_id": session["session_id"], "agent_id": None,
                    "attribution": "session", "executable": process["executable"],
                    "status": "running", "observed_at": observed_at,
                    "cpu_pct": process.get("cpu_pct"), "rss_bytes": process.get("rss_bytes"),
                    "_parent_pid": process["ppid"],
                })
        return (
            sorted(inferred.values(), key=lambda item: (item["session_id"], item["pid"])),
            unreadable_roots,
            missing_roots,
            unmatched_roots,
        )

    @staticmethod
    def _view(record: dict, at: int | None = None) -> dict:
        result = {key: value for key, value in record.items() if not key.startswith("_")}
        at = now_ms() if at is None else at
        if result["status"] in {"running", "unknown"}:
            result["elapsed_ms"] = max(result.get("elapsed_ms") or 0, at - result["started_at"])
        return result

    def fleet_records(self, limit: int = 100) -> tuple[list[dict], int]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        at = now_ms()
        cutoff = at - RECENT_HISTORY_MS
        with self.store.connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                """SELECT data FROM managed_processes WHERE host_id=? AND
                    (status='running' OR
                     (status='unknown' AND
                      (coalesce(json_extract(data,'$._uncertain_process_live'),0)=1 OR
                       coalesce(json_extract(data,'$._uncertain_since'),0)>=?)) OR
                     coalesce(finished_at,0)>=?)
                    ORDER BY sort_at DESC LIMIT ?""",
                (self.host_id, cutoff, cutoff, limit),
            ).fetchall()
            active = db.execute(
                """SELECT COUNT(*) FROM managed_processes
                    WHERE host_id=? AND status='running'""",
                (self.host_id,),
            ).fetchone()[0]
        return [self._view(json.loads(row[0]), at) for row in rows], active

    def inferred(self, limit: int = 100) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        if self.health()["observed_at"] is None:
            self.sample_once()
        with self.store.connect() as db:
            managed_records = [json.loads(row[0]) for row in db.execute(
                """SELECT data FROM managed_processes WHERE host_id=? AND
                    monitor_active=1""", (self.host_id,),
            )]
        managed_identities = {
            _identity(record) for record in managed_records
        } | {
            identity for record in managed_records
            for identity in record.get("_known_descendants", [])
        }
        managed_groups = {
            record["pgid"] for record in managed_records
            if record["status"] == "running"
            and (record.get("_registration_kind") == "managed"
                 or record["pid"] == record["pgid"])
        }
        with self._health_lock:
            visible = [
                record for record in self._last_inferred
                if _identity(record) not in managed_identities
                and record["pgid"] not in managed_groups
            ]
            return [
                {key: value for key, value in record.items() if not key.startswith("_")}
                for record in visible[:limit]
            ]

    def health(self) -> dict:
        with self._health_lock:
            return dict(self._health)

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    if self._stop.is_set():
                        raise RuntimeError("Process sampler shutdown is still in progress")
                    return
                self._thread = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="router-process-sampler", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        # Runtime startup performs a synchronous reconciliation sample before
        # starting this periodic worker. Waiting first avoids an immediate,
        # redundant database pass racing other startup work.
        while not self._stop.wait(self.interval_seconds):
            self.sample_once()

    def stop(self) -> None:
        with self._thread_lock:
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout=max(1.0, self.interval_seconds + 0.5))
            if thread is None or not thread.is_alive():
                self._thread = None
            else:
                with self._health_lock:
                    self._health = {
                        "partial": True,
                        "error": "Process sampler did not stop before the shutdown deadline",
                        "observed_at": now_ms(),
                    }
