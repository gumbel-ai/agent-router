"""`router run`: launch a command in its own process group and keep completion evidence.

Router records what ran (executable name, argument count and hash, never the argument text
or environment), when, and how it ended. It never stops or signals the command itself; the
wrapper only forwards the user's own interrupt to the command's group.
"""

import hashlib
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

# Finished and uncertain commands stay visible this long.
RECENT_MS = 3_600_000


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
    managed_id = "managed-" + uuid.uuid4().hex
    started_at = _now()
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, process_group=0)
    try:
        identity = parent_and_identity(child.pid)[1]
    except (OSError, ValueError, IndexError):
        identity = None  # Already exited; the completion record still says what happened.
    start = {"managed_id": managed_id, "label": label, "session_id": session_id, "agent_id": agent_id,
             "attribution": _attribution(session_id, agent_id), "pid": child.pid, "pgid": child.pid,
             "identity": identity, "wrapper_pid": os.getpid(),
             "wrapper_identity": parent_and_identity(os.getpid())[1],
             "executable": Path(argv[0]).name, "argument_count": len(argv) - 1,
             "argument_hash": hashlib.sha256(canonical({"argv": argv[1:]}).encode()).hexdigest(),
             "cwd": os.getcwd(), "started_at": started_at}
    _report("managed_start", start)

    def forward(signum, _frame):
        try:
            os.killpg(child.pid, signum)  # The user's own interrupt, passed to the command.
        except ProcessLookupError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    _, status, usage = os.wait4(child.pid, 0)
    child.returncode = os.waitstatus_to_exitcode(status)
    finished_at = _now()
    try:
        os.killpg(child.pid, 0)
        survivors = True  # Something else in the command's group outlived it.
    except ProcessLookupError:
        survivors = False
    except PermissionError:
        survivors = True
    code = child.returncode
    finish = {"managed_id": managed_id, "finished_at": finished_at, "elapsed_ms": finished_at - started_at,
              "exit_code": code if code >= 0 else None, "signal": -code if code < 0 else None,
              # ru_maxrss is kilobytes on Linux and bytes on macOS.
              "peak_rss_bytes": usage.ru_maxrss * (1024 if sys.platform == "linux" else 1),
              "shutdown_uncertain": survivors}
    if not _report("managed_finish", finish):
        # Evidence must survive a daemon that is down; the daemon reads it later.
        directory = completion_directory(Path(state))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = directory / f"{managed_id}.tmp"
        temporary.write_text(json.dumps(finish))
        temporary.replace(directory / f"{managed_id}.json")
    return code if code >= 0 else 128 - code


def _report(operation: str, payload: dict) -> bool:
    try:
        request(operation, payload, timeout=2)
        return True
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Agent Router managed evidence unavailable: {error}", file=sys.stderr)
        return False


class ManagedProcesses:
    """Daemon-side records of `router run` commands, kept across Router restarts."""

    START_FIELDS = frozenset({"managed_id", "label", "session_id", "agent_id", "attribution", "pid", "pgid",
                    "identity", "wrapper_pid", "wrapper_identity", "executable", "argument_count",
                    "argument_hash", "cwd", "started_at"})
    FINISH_FIELDS = frozenset({"managed_id", "finished_at", "elapsed_ms", "exit_code", "signal", "peak_rss_bytes",
                     "shutdown_uncertain"})

    def __init__(self, store, host_id: str, state: Path, identity=parent_and_identity):
        self.store, self.host_id, self.state, self.identity = store, host_id, Path(state), identity

    @staticmethod
    def _checked(payload: dict, fields: frozenset[str]) -> dict:
        if set(payload) - fields or not isinstance(payload.get("managed_id"), str) \
                or not payload["managed_id"].startswith("managed-") or len(payload["managed_id"]) > 80:
            raise ValueError("Invalid managed process evidence")
        for key, value in payload.items():
            if isinstance(value, str):
                payload[key] = bound_text(value, 2000 if key == "cwd" else 240)
            elif value is not None and not isinstance(value, (int, bool)):
                raise ValueError(f"Invalid managed process {key}")
        return payload

    def start(self, payload: dict) -> dict:
        record = {**self._checked(dict(payload), self.START_FIELDS), "host_id": self.host_id, "status": "running"}
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO managed_processes(managed_id,data) VALUES(?,?)",
                       (record["managed_id"], canonical(record)))
        return {"recorded": True}

    def finish(self, payload: dict) -> dict:
        finish = self._checked(dict(payload), self.FINISH_FIELDS)
        with self.store.transaction() as db:
            row = db.execute("SELECT data FROM managed_processes WHERE managed_id=?",
                             (finish["managed_id"],)).fetchone()
            if row is None:
                raise ValueError("Unknown managed process")
            record = self._complete(json.loads(row[0]), finish)
            db.execute("UPDATE managed_processes SET data=? WHERE managed_id=?",
                       (canonical(record), record["managed_id"]))
        return {"recorded": True}

    @staticmethod
    def _complete(record: dict, finish: dict) -> dict:
        record.update(finish)
        record["status"] = ("signaled" if finish.get("signal") else
                            "exited" if finish.get("exit_code") == 0 else "failed")
        if finish.get("shutdown_uncertain"):
            record["visibility_reason"] = "Processes in the command's group outlived it"
        return record

    def _wrapper_alive(self, record: dict) -> bool:
        try:
            return self.identity(record["wrapper_pid"])[1] == record["wrapper_identity"]
        except (OSError, ValueError, IndexError):
            return False

    def reconcile(self) -> None:
        """Apply completion files and mark records whose wrapper vanished without evidence."""
        directory = completion_directory(self.state)
        with self.store.transaction() as db:
            for row in db.execute("""SELECT data FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'""").fetchall():
                record = json.loads(row[0])
                completion = directory / f"{record['managed_id']}.json"
                if completion.exists():
                    try:
                        finish = self._checked(json.loads(completion.read_text()), self.FINISH_FIELDS)
                    except (OSError, ValueError) as error:
                        record.update(status="unknown", shutdown_uncertain=True, uncertain_at=_now(),
                                      visibility_reason=bound_text(f"Unreadable completion evidence: {error}", 500))
                    else:
                        record = self._complete(record, finish)
                        completion.unlink(missing_ok=True)
                elif not self._wrapper_alive(record):
                    record.update(status="unknown", shutdown_uncertain=True, uncertain_at=_now(),
                                  visibility_reason="The router run wrapper ended without completion evidence")
                else:
                    continue
                db.execute("UPDATE managed_processes SET data=? WHERE managed_id=?",
                           (canonical(record), record["managed_id"]))

    def records(self, limit: int = 100) -> tuple[list[dict], int]:
        """Running and recently finished commands (running first) and the running count."""
        self.reconcile()
        cutoff = _now() - RECENT_MS
        hidden = {"identity", "wrapper_pid", "wrapper_identity", "uncertain_at"}
        with self.store.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute("""SELECT data FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'
                       OR COALESCE(json_extract(data,'$.finished_at'), json_extract(data,'$.uncertain_at'))>=?
                    ORDER BY json_extract(data,'$.status')!='running',
                             json_extract(data,'$.started_at') DESC LIMIT ?""", (cutoff, limit))]
            running = db.execute("""SELECT COUNT(*) FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'""").fetchone()[0]
        return [{key: value for key, value in row.items() if key not in hidden} for row in rows], running
