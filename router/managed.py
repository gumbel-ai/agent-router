"""`router run`: launch a command in its own session and process group, keep completion evidence.

Router records what ran (executable name and argument count, never the argument text or
environment), when, and how it ended. It never stops or signals the command itself; the
wrapper only forwards the user's own interrupt to the command's group.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from router.client import request
from router.dispatch import canonical
from router.processes import parent_and_identity
from router.sanitize import bound_text
from router.validation import is_wire_integer

# Finished and uncertain commands stay visible (and keep capacity unknown) this long.
RECENT_MS = 3_600_000
# Records older than this are pruned, checked at most this often.
RETENTION_MS = 7 * 86_400_000
PRUNE_INTERVAL_MS = 60_000
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _now() -> int:
    return int(time.time() * 1000)


def _attribution(session_id: str | None, agent_id: str | None) -> str:
    # Exact only when the caller names both the session and the agent that launched it.
    return "exact" if session_id and agent_id else "session" if session_id else "host"


def completion_directory(state: Path) -> Path:
    return state / "managed"


def run(argv: list[str], label: str | None, session_id: str | None, agent_id: str | None, state: Path) -> int:
    """Run argv to completion and return its exit status (128 + signal when signalled)."""
    if not argv:
        raise ValueError("router run needs a command after --")
    for name, value in (("session", session_id), ("agent", agent_id)):
        if value is not None:
            _identifier(value, name)
    if agent_id is not None and session_id is None:
        raise ValueError("--agent needs --session")
    managed_id = "managed-" + uuid.uuid4().hex
    # Everything that can fail is gathered before launch, so a failure never leaves an
    # untracked command behind.
    metadata = {"managed_id": managed_id, "label": label, "session_id": session_id, "agent_id": agent_id,
                "attribution": _attribution(session_id, agent_id), "wrapper_pid": os.getpid(),
                "wrapper_identity": _identity(os.getpid()), "executable": Path(argv[0]).name,
                "argument_count": len(argv) - 1, "cwd": os.getcwd()}
    started_at, started = _now(), time.monotonic()
    launched: list[int] = []  # The command's PID (its group), once it exists.
    pending: list[int] = []

    def forward(signum, _frame):
        if not launched:
            pending.append(signum)  # Delivered as soon as the command exists.
            return
        try:
            os.killpg(launched[0], signum)  # The user's own interrupt, passed to the command.
        except OSError:
            pass  # The group is already gone.

    # Handlers first, so an interrupt during launch cannot orphan the command.
    previous = {signum: signal.signal(signum, forward) for signum in FORWARDED_SIGNALS}
    try:
        # A new session: the command has no controlling terminal, so a prompt fails instead of
        # stopping it in the background, and the group (pgid == pid) is the command's own.
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
        launched.append(child.pid)
        for signum in pending:
            forward(signum, None)
        start = {**metadata, "pid": child.pid, "pgid": child.pid, "started_at": started_at}
        if not _report("managed_start", start):
            # The daemon may come back while the command runs; leave the start where it looks.
            _write_evidence(state, f"start-{managed_id}.json", start)
        _, status, usage = os.wait4(child.pid, 0)
        child.returncode = code = os.waitstatus_to_exitcode(status)
        # Keep forwarding until the evidence is saved: an interrupt now must not lose it.
        try:
            os.killpg(child.pid, 0)
            survivors = True  # Something else in the command's group outlived it.
        except ProcessLookupError:
            survivors = False
        except PermissionError:
            survivors = True
        finish = {"managed_id": managed_id, "finished_at": _now(),
                  "elapsed_ms": int((time.monotonic() - started) * 1000),
                  "exit_code": code if code >= 0 else None, "signal": -code if code < 0 else None,
                  # Largest single process among the command and its reaped descendants.
                  # ru_maxrss is kilobytes on Linux and bytes on macOS.
                  "max_process_rss_bytes": usage.ru_maxrss * (1024 if sys.platform == "linux" else 1),
                  "group_outlived": survivors, "start": start}
        if not _report("managed_finish", finish):
            # Evidence must survive a daemon that is down; the daemon reads it later.
            _write_evidence(state, f"{managed_id}.json", finish)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return code if code >= 0 else 128 - code


def _write_evidence(state: Path, name: str, evidence: dict) -> None:
    try:
        directory = completion_directory(Path(state))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = directory / f"{name}.tmp"
        temporary.write_text(json.dumps(evidence))
        temporary.replace(directory / name)
    except OSError as error:
        print(f"Agent Router could not keep managed evidence: {error}", file=sys.stderr)


def _identifier(value: object, name: str) -> str:
    # Session and agent IDs are matched exactly elsewhere; refuse rather than rewrite them.
    if not isinstance(value, str) or not value or len(value) > 240 or value != value.strip() \
            or any(not character.isprintable() for character in value):
        raise ValueError(f"Invalid {name} ID")
    return value


def _identity(pid: int) -> str | None:
    try:
        return parent_and_identity(pid)[1]
    except (OSError, ValueError, IndexError):
        return None


def _report(operation: str, payload: dict) -> bool:
    try:
        request(operation, payload, timeout=2)
        return True
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Agent Router managed evidence unavailable: {error}", file=sys.stderr)
        return False


# Field -> kind. Text is bounded; integers must fit the wire; flags must be real booleans.
START_FIELDS = {"managed_id": "id", "label": "text", "session_id": "identifier", "agent_id": "identifier",
                "attribution": "attribution", "pid": "pid", "pgid": "pid", "wrapper_pid": "pid",
                "wrapper_identity": "text", "executable": "text", "argument_count": "count",
                "cwd": "path", "started_at": "count"}
FINISH_FIELDS = {"managed_id": "id", "finished_at": "count", "elapsed_ms": "count", "exit_code": "count",
                 "signal": "pid", "max_process_rss_bytes": "count", "group_outlived": "flag", "start": "start"}


def _checked(payload: object, fields: dict[str, str]) -> dict:
    if not isinstance(payload, dict) or set(payload) - fields.keys():
        raise ValueError("Invalid managed process evidence")
    required = {key for key, kind in fields.items()
                if kind not in {"text", "path", "start", "identifier"}} - {"exit_code", "signal"}
    if required - payload.keys():
        raise ValueError("Incomplete managed process evidence")
    if fields is FINISH_FIELDS and (payload.get("exit_code") is None) == (payload.get("signal") is None):
        raise ValueError("Managed process completion needs exactly one of exit_code and signal")
    checked = {}
    for key, value in payload.items():
        kind = fields[key]
        if kind == "start":
            checked[key] = _checked(value, START_FIELDS)
            continue
        if value is None and key not in required:
            checked[key] = None
            continue
        valid = (
            kind == "id" and isinstance(value, str) and value.startswith("managed-") and len(value) <= 80
            or kind in {"text", "path"} and isinstance(value, str)
            or kind == "identifier" and _valid_identifier(value)
            or kind == "attribution" and isinstance(value, str) and value in {"exact", "session", "host"}
            or kind == "count" and is_wire_integer(value)
            or kind == "pid" and is_wire_integer(value, minimum=1)
            or kind == "flag" and isinstance(value, bool)
        )
        if not valid:
            raise ValueError(f"Invalid managed process {key}")
        checked[key] = bound_text(value, 2000 if kind == "path" else 240) if kind in {"text", "path"} else value
    if fields is START_FIELDS and checked["attribution"] != _attribution(checked.get("session_id"),
                                                                         checked.get("agent_id")):
        raise ValueError("Managed process attribution does not match its session and agent")
    return checked


def _valid_identifier(value: object) -> bool:
    try:
        _identifier(value, "identifier")
        return True
    except ValueError:
        return False


class ManagedProcesses:
    """Daemon-side records of `router run` commands, kept across Router restarts."""

    def __init__(self, store, host_id: str, state: Path, identity=_identity):
        self.store, self.host_id, self.state, self.identity = store, host_id, Path(state), identity
        self._pruned_at = 0

    def start(self, payload: dict) -> dict:
        record = {**_checked(payload, START_FIELDS), "host_id": self.host_id, "status": "running"}
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO managed_processes(managed_id,data) VALUES(?,?)",
                       (record["managed_id"], canonical(record)))
        return {"recorded": True}

    def finish(self, payload: dict) -> dict:
        with self.store.transaction() as db:
            self._finish(db, _checked(payload, FINISH_FIELDS))
        return {"recorded": True}

    def _finish(self, db, finish: dict) -> None:
        row = db.execute("SELECT data FROM managed_processes WHERE managed_id=?",
                         (finish["managed_id"],)).fetchone()
        start = finish.pop("start", None)
        if row is not None:
            record = json.loads(row[0])
        elif start is not None and start["managed_id"] == finish["managed_id"]:
            record = {**start, "host_id": self.host_id, "status": "running"}  # The start never reached the daemon.
        else:
            raise ValueError("Unknown managed process")
        if record.get("status") not in {"running", "unknown"}:
            return  # Completion is recorded once; a repeat cannot rewrite it.
        record.update(finish)
        record.pop("uncertain_at", None)
        record["status"] = ("signaled" if finish.get("signal") else
                            "exited" if finish.get("exit_code") == 0 else "failed")
        record["visibility_reason"] = ("Processes in the command's group outlived it"
                                       if finish.get("group_outlived") else None)
        db.execute("INSERT INTO managed_processes(managed_id,data) VALUES(?,?) "
                   "ON CONFLICT(managed_id) DO UPDATE SET data=excluded.data",
                   (record["managed_id"], canonical(record)))

    def reconcile(self) -> None:
        """Apply completion files; mark running records whose wrapper vanished without evidence."""
        directory = completion_directory(self.state)
        files = sorted(directory.glob("managed-*.json")) if directory.exists() else []
        starts = sorted(directory.glob("start-managed-*.json")) if directory.exists() else []
        with self.store.connect() as db:
            running = db.execute("""SELECT data FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'""").fetchall()
        now = _now()
        if not files and not starts and not running and now - self._pruned_at < PRUNE_INTERVAL_MS:
            return
        consumed, invalid = [], []
        with self.store.transaction() as db:
            for path in starts:  # Starts the daemon missed: the command may still be running.
                try:
                    record = {**_checked(json.loads(path.read_text()), START_FIELDS),
                              "host_id": self.host_id, "status": "running"}
                except OSError:
                    continue
                except (ValueError, TypeError):  # Malformed evidence is set aside, never fatal.
                    invalid.append(path)
                    continue
                db.execute("INSERT OR IGNORE INTO managed_processes(managed_id,data) VALUES(?,?)",
                           (record["managed_id"], canonical(record)))
                consumed.append(path)
            running = db.execute("""SELECT data FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'""").fetchall()
            for path in files:
                try:
                    evidence = json.loads(path.read_text())
                except OSError:
                    continue  # Kept for the next pass.
                except ValueError:
                    invalid.append(path)
                    continue
                try:
                    self._finish(db, _checked(evidence, FINISH_FIELDS))
                except (ValueError, TypeError) as error:
                    print(f"Agent Router set aside invalid completion evidence {path.name}: {error}",
                          file=sys.stderr)
                    invalid.append(path)
                    continue
                consumed.append(path)
            for row in running:
                record = json.loads(row[0])
                # Liveness first: a wrapper that exits now leaves its file for the next pass.
                wrapper = record.get("wrapper_identity")
                if wrapper is not None and self.identity(record["wrapper_pid"]) == wrapper:
                    continue
                current = db.execute("SELECT json_extract(data,'$.status') FROM managed_processes "
                                     "WHERE managed_id=?", (record["managed_id"],)).fetchone()
                if current is None or current[0] != "running" \
                        or (directory / f"{record['managed_id']}.json").exists():
                    continue
                record.pop("group_outlived", None)
                record.update(status="unknown", uncertain_at=_now(),
                              visibility_reason="The router run wrapper ended without completion evidence")
                db.execute("UPDATE managed_processes SET data=? WHERE managed_id=?",
                           (canonical(record), record["managed_id"]))
            db.execute("""DELETE FROM managed_processes WHERE json_extract(data,'$.status')!='running'
                    AND COALESCE(json_extract(data,'$.finished_at'), json_extract(data,'$.uncertain_at'),
                                 json_extract(data,'$.started_at')) < ?""", (_now() - RETENTION_MS,))
        self._pruned_at = now
        for path in consumed:  # Only after the evidence is committed; replaying a file is harmless.
            path.unlink(missing_ok=True)
        for path in invalid:  # Kept for inspection, out of the import path.
            path.replace(path.with_suffix(".invalid"))

    def records(self, limit: int = 100) -> tuple[list[dict], int | None]:
        """Running and recently finished commands (running first), and the active count.

        The count is None while a recent command's outcome is uncertain, so capacity reads
        unknown rather than available.
        """
        self.reconcile()
        cutoff = _now() - RECENT_MS
        hidden = {"wrapper_pid", "wrapper_identity"}
        with self.store.snapshot() as db:  # The page and the count describe one database state.
            rows = [json.loads(row[0]) for row in db.execute("""SELECT data FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'
                       OR COALESCE(json_extract(data,'$.finished_at'), json_extract(data,'$.uncertain_at'))>=?
                    ORDER BY CASE WHEN json_extract(data,'$.status') IN ('failed','signaled','unknown')
                                        OR json_extract(data,'$.group_outlived')=1 THEN 0
                                   WHEN json_extract(data,'$.status')='running' THEN 1 ELSE 2 END,
                             json_extract(data,'$.started_at') DESC LIMIT ?""", (cutoff, limit))]
            running, uncertain = db.execute("""SELECT
                    SUM(json_extract(data,'$.status')='running'),
                    SUM(json_extract(data,'$.status')='unknown' OR json_extract(data,'$.group_outlived')=1)
                    FROM managed_processes WHERE json_extract(data,'$.status')='running'
                       OR COALESCE(json_extract(data,'$.finished_at'), json_extract(data,'$.uncertain_at'))>=?""",
                                          (cutoff,)).fetchone()
        active = None if uncertain else (running or 0)
        return [{key: value for key, value in row.items() if key not in hidden} for row in rows], active
