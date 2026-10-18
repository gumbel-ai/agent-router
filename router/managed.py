"""`router run`: launch a command in its own session and process group, keep completion evidence.

Router records what ran (executable name and argument count, never the argument text or
environment), when, and how it ended. It never stops or signals the command itself; the
wrapper only forwards the user's own interrupt to the command's group.
"""

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from router.client import CompletionBusy, request
from router.dispatch import canonical
from router.paths import observed_project
from router.processes import parent_and_identity
from router.sanitize import bound_text
from router.validation import is_wire_integer

# Finished and uncertain commands stay visible (and keep capacity unknown) this long.
RECENT_MS = 3_600_000
# Records older than this are pruned, checked at most this often.
RETENTION_MS = 7 * 86_400_000
PRUNE_INTERVAL_MS = 60_000
WAIT_POLL_SECONDS = .25
MAX_WAIT_SECONDS = 3600
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
    # Router records only commands run inside an observed project; others still run, unrecorded.
    recorded = observed_project(os.getcwd()) is not None
    if not recorded:
        print("Agent Router: not recording this command; it is outside every observed project",
              file=sys.stderr)
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
        if recorded and not _report("managed_start", start):
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
        if recorded and not _report("managed_finish", finish):
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


def wait(consumer: str, timeout: float) -> dict:
    """Block locally; each daemon request is short and never changes command ownership."""
    _identifier(consumer, "consumer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
            or not 0 < timeout <= MAX_WAIT_SECONDS:
        raise ValueError(f"timeout must be finite and greater than zero, at most {MAX_WAIT_SECONDS} seconds")
    deadline = time.monotonic() + timeout
    last_error = None
    diagnosed = False
    saw_pending = False
    subscription = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if not saw_pending and not diagnosed:
                print("Agent Router completion wait unavailable: deadline reached before reading completion evidence",
                      file=sys.stderr, flush=True)
            return {"status": "unavailable" if last_error or not saw_pending else "deadline", "consumer": consumer}
        try:
            if subscription is None:
                binding = request("completion_subscribe", {"consumer": consumer}, timeout=min(2, remaining), deadline=True)
                if not isinstance(binding, dict) or binding.get("consumer") != consumer \
                        or not _valid_identifier(binding.get("subscription_id")) or not _valid_identifier(binding.get("host_id")):
                    raise ValueError("Invalid completion subscription response")
                subscription = binding["subscription_id"]
                last_error = None
                continue
            result = request("completion_next", {"consumer": consumer, "subscription_id": subscription},
                             timeout=min(2, remaining), deadline=True)
            if not isinstance(result, dict) or result.get("status") not in {
                "pending", "completed", "failed", "uncertain"
            } or result.get("consumer") != consumer or not _valid_identifier(result.get("host_id")) \
                    or (result["status"] != "pending" and (
                        not isinstance(result.get("event"), dict)
                        or not _valid_identifier(result["event"].get("event_id"))
                        or ("managed_id" in result["event"] and not _valid_identifier(result["event"]["managed_id"]))
                        or not _valid_identifier(result["event"].get("host_id")))):
                raise ValueError("Invalid completion response")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
            timed_out = isinstance(error, TimeoutError) or isinstance(error.__cause__, TimeoutError)
            if timed_out and saw_pending and last_error is None and time.monotonic() >= deadline:
                return {"status": "deadline", "consumer": consumer}
            # A lost admission reply is uncertain: do not recreate a concurrently removed subscription.
            transient = isinstance(error, CompletionBusy) or (subscription is not None and (
                isinstance(error, (TimeoutError, ConnectionError)) or isinstance(error.__cause__, TimeoutError)))
            if not diagnosed or not transient:
                print(f"Agent Router completion wait {'retrying' if transient else 'unavailable'}: "
                      f"{bound_text(str(error) or type(error).__name__, 300)}", file=sys.stderr, flush=True)
                diagnosed = True
            if not transient:
                return {"status": "unavailable", "consumer": consumer}
            last_error = error
            time.sleep(min(WAIT_POLL_SECONDS, max(0, deadline - time.monotonic())))
            continue
        last_error = None
        if result["status"] != "pending":
            return result
        saw_pending = True
        if time.monotonic() >= deadline:
            return {"status": "deadline", "consumer": consumer}
        time.sleep(min(WAIT_POLL_SECONDS, max(0, deadline - time.monotonic())))


class ManagedProcesses:
    """Daemon-side records of `router run` commands, kept across Router restarts."""

    def __init__(self, store, host_id: str, state: Path, identity=_identity):
        self.store, self.host_id, self.state, self.identity = store, host_id, Path(state), identity
        self._pruned_at = 0
        self._reconcile_lock = threading.Lock()
        # Upgrade existing retained evidence without changing the managed-record format.
        with self.store.transaction() as db:
            for row in db.execute("SELECT data FROM managed_processes WHERE json_extract(data,'$.status')!='running'"):
                self._completion(db, json.loads(row[0]))

    def _completion(self, db, record: dict) -> None:
        fields = {"host_id", "managed_id", "session_id", "agent_id", "attribution", "status",
                  "exit_code", "signal", "finished_at", "uncertain_at", "group_outlived"}
        event = {key: value for key, value in record.items() if key in fields}
        if "group_outlived" not in event and isinstance(record.get("shutdown_uncertain"), bool):
            event["group_outlived"] = record["shutdown_uncertain"]
        # Omit malformed identity without losing its known process outcome.
        if not _valid_identifier(event.get("managed_id")):
            event.pop("managed_id", None)
        phase = "uncertain" if record["status"] == "unknown" else "finished"
        status = ("failed" if record["status"] in {"failed", "signaled"} else
                  "completed" if record["status"] == "exited" and event.get("group_outlived") is False else "uncertain")
        timestamp = next((record[key] for key in ("finished_at", "uncertain_at", "started_at")
                          if is_wire_integer(record.get(key))), _now())
        db.execute("""INSERT OR IGNORE INTO completion_events(managed_id,host_id,phase,created_at,data)
                    VALUES(?,?,?,?,?)""", (record["managed_id"], record["host_id"], phase,
                    timestamp, canonical({"status": status, "event": event})))

    def subscribe(self, payload: dict) -> dict:
        if set(payload) != {"consumer"}:
            raise ValueError("completion_subscribe needs only consumer")
        consumer = _identifier(payload["consumer"], "consumer")
        with self.store.connect(timeout=.1) as db:
            row = db.execute("SELECT subscription_id FROM completion_consumers WHERE name=?", (consumer,)).fetchone()
        if row is None:
            with self.store.transaction(timeout=.1) as db:
                db.execute("INSERT OR IGNORE INTO completion_consumers(name,subscription_id) VALUES(?,?)",
                           (consumer, uuid.uuid4().hex))
                row = db.execute("SELECT subscription_id FROM completion_consumers WHERE name=?", (consumer,)).fetchone()
        return {"consumer": consumer, "subscription_id": row[0], "host_id": self.host_id}

    def list_consumers(self, payload: dict) -> dict:
        if set(payload) - {"limit", "offset"}:
            raise ValueError("consumer list accepts only limit and offset")
        limit, offset = payload.get("limit", 100), payload.get("offset", 0)
        if not is_wire_integer(limit) or not 1 <= limit <= 1000 or not is_wire_integer(offset) or offset < 0:
            raise ValueError("consumer list needs limit 1..1000 and a nonnegative offset")
        with self.store.connect(timeout=.1) as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM completion_consumers").fetchone()[0]
            rows = db.execute("""SELECT name,cursor,
                    (SELECT COUNT(*) FROM completion_events WHERE data IS NOT NULL AND sequence>c.cursor) AS pending
                    FROM completion_consumers c ORDER BY name LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        return {"consumers": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset,
                "host_id": self.host_id}

    def remove_consumer(self, payload: dict) -> dict:
        if set(payload) != {"consumer"}:
            raise ValueError("consumer remove needs only consumer")
        consumer = _identifier(payload["consumer"], "consumer")
        with self.store.transaction(timeout=.1) as db:
            db.execute("DELETE FROM completion_consumers WHERE name=?", (consumer,))
        return {"consumer": consumer, "removed": True, "host_id": self.host_id}

    def next_completion(self, payload: dict) -> dict:
        if set(payload) not in ({"consumer"}, {"consumer", "subscription_id"}):
            raise ValueError("completion_next needs consumer and optional subscription_id")
        consumer = _identifier(payload["consumer"], "consumer")
        subscription = (_identifier(payload["subscription_id"], "subscription") if "subscription_id" in payload else
                        self.subscribe({"consumer": consumer})["subscription_id"])

        def pending():
            with self.store.connect(timeout=.1) as db:
                db.execute("BEGIN")
                row = db.execute("SELECT cursor,subscription_id FROM completion_consumers WHERE name=?", (consumer,)).fetchone()
                if row is None or row["subscription_id"] != subscription:
                    raise ValueError("Completion consumer subscription was removed or replaced; start a new wait to resubscribe")
                return db.execute("""SELECT sequence,data FROM completion_events
                        WHERE data IS NOT NULL AND sequence>? ORDER BY sequence LIMIT 1""", (row["cursor"],)).fetchone()

        row = pending()
        if row is None:
            self.reconcile(blocking=False, timeout=.1)
            row = pending()
        result = json.loads(row["data"]) if row else {"status": "pending"}
        if row:
            result["event"]["event_id"] = f"completion-{row['sequence']}"
        return {**result, "consumer": consumer, "host_id": self.host_id}

    def acknowledge(self, payload: dict) -> dict:
        if set(payload) != {"consumer", "event_id"}:
            raise ValueError("completion_ack needs consumer and event_id")
        consumer = _identifier(payload["consumer"], "consumer")
        event_id = payload["event_id"]
        suffix = event_id.removeprefix("completion-") if isinstance(event_id, str) else ""
        if not suffix.isascii() or not suffix.isdigit() or len(suffix) > 19 \
                or not 0 < int(suffix) <= 2**63 - 1 or event_id != f"completion-{int(suffix)}":
            raise ValueError("Invalid completion event ID")
        sequence = int(suffix)
        with self.store.transaction(timeout=.1) as db:
            row = db.execute("SELECT cursor FROM completion_consumers WHERE name=?", (consumer,)).fetchone()
            if row is None:
                raise ValueError("Unknown completion consumer; wait first")
            if db.execute("SELECT 1 FROM completion_events WHERE sequence=?", (sequence,)).fetchone() is None:
                raise ValueError("Unknown completion event ID")
            if sequence > row["cursor"]:
                pending = db.execute("SELECT MIN(sequence) FROM completion_events WHERE data IS NOT NULL AND sequence>?",
                                     (row["cursor"],)).fetchone()[0]
                if sequence != pending:
                    raise ValueError("Acknowledge the oldest pending event first")
                db.execute("UPDATE completion_consumers SET cursor=? WHERE name=?", (sequence, consumer))
        return {"acknowledged": True, "event_id": event_id, "consumer": consumer, "host_id": self.host_id}

    def start(self, payload: dict) -> dict:
        with self.store.transaction() as db:
            self._start(db, payload)
        return {"recorded": True}

    def _start(self, db, payload: dict) -> None:
        record = {**_checked(payload, START_FIELDS), "host_id": self.host_id, "status": "running"}
        db.execute("""INSERT OR IGNORE INTO managed_processes(managed_id,data)
                    SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM completion_events WHERE managed_id=?)""",
                   (record["managed_id"], canonical(record), record["managed_id"]))

    def finish(self, payload: dict) -> dict:
        with self.store.transaction() as db:
            self._finish(db, _checked(payload, FINISH_FIELDS))
        return {"recorded": True}

    def _finish(self, db, finish: dict) -> None:
        if db.execute("SELECT 1 FROM completion_events WHERE managed_id=? AND phase='finished'",
                      (finish["managed_id"],)).fetchone():
            return  # Final evidence remains final even after its payload has expired.
        row = db.execute("SELECT data FROM managed_processes WHERE managed_id=?",
                         (finish["managed_id"],)).fetchone()
        start = finish.pop("start", None)
        if row is not None:
            record = json.loads(row[0])
        elif start is not None and start["managed_id"] == finish["managed_id"]:
            previous = db.execute("SELECT host_id FROM completion_events WHERE managed_id=? AND phase='uncertain'",
                                  (finish["managed_id"],)).fetchone()
            record = {**start, "host_id": previous[0] if previous else self.host_id, "status": "running"}
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
        self._completion(db, record)

    def reconcile(self, *, blocking: bool = True, timeout: float = 10) -> None:
        # Concurrent readers must not import or rename the same spool file at once.
        if not self._reconcile_lock.acquire(blocking=blocking):
            return
        try:
            self._reconcile(timeout=timeout)
        finally:
            self._reconcile_lock.release()

    def _reconcile(self, *, timeout: float = 10) -> None:
        """Apply completion files; mark running records whose wrapper vanished without evidence."""
        directory = completion_directory(self.state)
        files = sorted(directory.glob("managed-*.json")) if directory.exists() else []
        starts = sorted(directory.glob("start-managed-*.json")) if directory.exists() else []
        with self.store.connect(timeout=timeout) as db:
            running = db.execute("""SELECT data FROM managed_processes
                    WHERE json_extract(data,'$.status')='running'""").fetchall()
        now = _now()
        if not files and not starts and not running and now - self._pruned_at < PRUNE_INTERVAL_MS:
            return
        consumed, invalid = [], []
        with self.store.transaction(timeout=timeout) as db:
            for path in starts:  # Starts the daemon missed: the command may still be running.
                try:
                    self._start(db, json.loads(path.read_text()))
                except OSError:
                    continue
                except (ValueError, TypeError):  # Malformed evidence is set aside, never fatal.
                    invalid.append(path)
                    continue
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
                self._completion(db, record)
            # Every registered consumer protects everything after its durable cursor.
            # Keep identity tombstones so expired evidence can never acquire a new event ID.
            db.execute("""UPDATE completion_events SET data=NULL WHERE data IS NOT NULL AND created_at < ? AND sequence <=
                    COALESCE((SELECT MIN(cursor) FROM completion_consumers),
                             (SELECT MAX(sequence) FROM completion_events))""", (_now() - RETENTION_MS,))
            db.execute("""DELETE FROM managed_processes WHERE json_extract(data,'$.status')!='running'
                    AND COALESCE(json_extract(data,'$.finished_at'), json_extract(data,'$.uncertain_at'),
                                 json_extract(data,'$.started_at')) < ?
                    AND NOT EXISTS (SELECT 1 FROM completion_events e
                        WHERE e.managed_id=managed_processes.managed_id AND e.data IS NOT NULL)""", (_now() - RETENTION_MS,))
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
