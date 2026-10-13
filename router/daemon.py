"""Local control plane: durable operations, bounded supervision, no auto-spawn."""

import fcntl
import json
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from router.child import child_environment, launch, opencode_command
from router.config import Settings
from router.db import Store, now_ms
from router.dispatch import MUTATIONS, canonical
from router.native import NativeObserver
from router.process import UncertainProcess, owned_state, stop_owned, supervised_state
from router.snapshot import cleanup, git, validate_repository

ACTIVE = {"created", "validating", "preparing", "running", "retrying", "cancelling"}
DECISIONS = {"awaiting_review", "blocked", "failed", "interrupted"}
LOGGER = logging.getLogger(__name__)


def required_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def public(task: dict, store: Store | None = None) -> dict:
    result = {key: value for key, value in task.items() if key not in {
        "session_id", "invocation", "feedback", "permission_profile", "setup_command", "cleanup_requested"}}
    history = store.history(task["task_id"]) if store is not None else []
    result.update(visibility(task, history))
    if store is not None:
        result["attempt_history"] = history
    return result


def visibility(task: dict, history: list[dict]) -> dict:
    model = task.get("resolved_model")
    attempts = {attempt["attempt_number"]: attempt for attempt in history if "attempt_number" in attempt}
    current = dict(task)
    if task["status"] in {"preparing", "running", "cancelling"} and "attempt_started_at" in task:
        current["duration_ms"] = max(0, now_ms() - task["attempt_started_at"])
    if "attempt_number" in task:
        attempts[task["attempt_number"]] = current
    frames = list(attempts.values()) or [current]
    cost = sum(frame["cost"] for frame in frames) if all("cost" in frame for frame in frames) else None
    elapsed = sum(frame.get("duration_ms", 0) for frame in frames) if any("duration_ms" in frame for frame in frames) else None
    usage = {}
    def add_usage(destination, source):
        for key, value in source.items():
            if isinstance(value, dict):
                add_usage(destination.setdefault(key, {}), value)
            elif isinstance(value, (int, float)):
                destination[key] = destination.get(key, 0) + value
    for frame in frames:
        add_usage(usage, frame.get("usage", {}))
    return {"owner": "router", "control_mode": "control", "parent_session_id": task["session_id"],
            "parent_agent_id": task.get("parent_agent_id"), "task": task["invocation"]["task"], "cwd": task["invocation"]["parent_worktree"],
            "started_at": task.get("attempt_started_at"),
            "elapsed_ms": elapsed,
            "models": [model] if model else [], "model_source": "configured" if model else None,
            "cost": cost, "cost_source": "executor_reported" if cost is not None else "unavailable",
            "cost_note": "Task-total executor estimate, not billed spend" if cost is not None else "Cost unavailable for one or more attempts",
            "price_date": None, "price_source": None, "price_overrides_used": [], "price_sources": {},
            "usage": usage or None, "usage_source": "executor_reported" if usage else "unavailable",
            "activity": task.get("phase"), "visibility_reason": None, "runtime_partial": False}


class Controller:
    def __init__(self, settings: Settings, environment: dict[str, str], directory: Path):
        self.settings = settings
        self.environment = environment
        self.directory = directory.resolve()
        if self.directory in {Path.home().resolve(), Path("/")}:
            raise ValueError("Router state requires a dedicated directory, not home or filesystem root")
        if any(self.directory.is_relative_to(Path(root)) for root in settings.repositories):
            raise ValueError("Router state must be outside every configured parent worktree")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.directory / "router.sqlite3")
        self.native = NativeObserver(self.store, settings.model_prices)
        self.changed = threading.Event()

    def handle(self, operation: str, payload: dict) -> dict | list:
        if not isinstance(payload, dict):
            raise ValueError("Request payload must be an object")  # noqa: TRY004 - public validation contract
        if operation == "submit":
            return self.submit(payload)
        if operation == "authorize":
            return self.authorize(payload)
        if operation == "status":
            return self.status(payload)
        if operation == "observe":
            return self.native.observe(payload)
        if operation not in MUTATIONS:
            raise ValueError("Unknown operation")
        if str(payload.get("task_id", "")).startswith("native-"):
            raise ValueError("Native agent is host-managed; Router cannot control it")
        result = self.mutate(operation, payload)
        self.changed.set()
        return public(result, self.store)

    def submit(self, payload: dict) -> dict:
        session = required_text(payload, "session_id")
        prompt = required_text(payload, "prompt_id")
        with self.store.transaction() as db:
            existing = db.execute("SELECT instruction_id FROM submissions WHERE session_id=? AND prompt_id=?", (session, prompt)).fetchone()
            if existing:
                return {"instruction_id": existing[0]}
            instruction = "instruction-" + uuid.uuid4().hex
            db.execute("INSERT INTO submissions VALUES(?,?,?,?,?)", (instruction, session, prompt, now_ms(), payload.get("parent_cli", "claude")))
            db.execute("INSERT INTO current_submissions VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET instruction_id=excluded.instruction_id", (session, instruction))
            return {"instruction_id": instruction}

    def authorize(self, payload: dict) -> dict:
        session = required_text(payload, "session_id")
        prompt = required_text(payload, "prompt_id")
        operation = required_text(payload, "operation")
        invocation = payload.get("invocation")
        if operation not in MUTATIONS or not isinstance(invocation, dict):
            raise ValueError("Invalid dispatch authorization")
        request_id = required_text(invocation, "request_id")
        encoded = canonical(invocation)
        with self.store.transaction() as db:
            row = db.execute("SELECT instruction_id FROM submissions WHERE session_id=? AND prompt_id=?", (session, prompt)).fetchone()
            current = db.execute("SELECT instruction_id FROM current_submissions WHERE session_id=?", (session,)).fetchone()
            if row is None or current is None or row[0] != current[0]:
                raise ValueError("Dispatch requires the current registered host submission")
            if operation == "start":
                if invocation.get("instruction_id") != row[0]:
                    raise ValueError("instruction_id does not belong to this current host submission")
            else:
                task = self.store.get(required_text(invocation, "task_id"), db)
                if task["session_id"] != session:
                    raise ValueError("Task belongs to another parent session")
            previous = db.execute("SELECT operation,payload FROM authorizations WHERE session_id=? AND request_id=?", (session, request_id)).fetchone()
            if previous:
                if tuple(previous) != (operation, encoded):
                    raise ValueError("request_id was already authorized with another payload")
            else:
                actor = payload.get("parent_agent_id")
                if actor is not None:
                    actor = required_text(payload, "parent_agent_id")
                db.execute("INSERT INTO authorizations(session_id,request_id,operation,payload,instruction_id,parent_agent_id) VALUES(?,?,?,?,?,?)",
                           (session, request_id, operation, encoded, row[0], actor))
        return {"authorized": True, "request_id": request_id}

    def mutate(self, operation: str, payload: dict) -> dict:
        request_id = required_text(payload, "request_id")
        encoded = canonical(payload)
        with self.store.transaction() as db:
            if operation == "start":
                self.envelope(payload)
                row = db.execute("SELECT session_id FROM submissions WHERE instruction_id=?", (payload["instruction_id"],)).fetchone()
                if row is None:
                    raise ValueError("No registered instruction or dispatch authorization")
                session = row[0]
            else:
                task = self.store.get(required_text(payload, "task_id"), db)
                session = task["session_id"]
            replay = db.execute("SELECT operation,payload,task_id FROM operations WHERE session_id=? AND request_id=?", (session, request_id)).fetchone()
            if replay:
                if tuple(replay)[:2] != (operation, encoded):
                    raise ValueError("request_id was already used with another payload")
                task = self.store.get(replay[2], db)
                if operation == "close" and task.get("cleanup_pending"):
                    task["cleanup_requested"] = True
                    self.store.put(db, task)
                return task
            authorization = db.execute("SELECT * FROM authorizations WHERE session_id=? AND request_id=?", (session, request_id)).fetchone()
            if (authorization is None or authorization["consumed"] or authorization["operation"] != operation
                    or authorization["payload"] != encoded):
                raise ValueError("Missing matching single-use dispatch authorization")
            if operation == "start":
                current = db.execute("SELECT instruction_id FROM current_submissions WHERE session_id=?", (session,)).fetchone()
                if current is None or current[0] != payload["instruction_id"]:
                    raise ValueError("Submission has been superseded")
                task = {"task_id": "task-" + uuid.uuid4().hex, "session_id": session,
                        "parent_agent_id": authorization["parent_agent_id"],
                        "instruction_id": payload["instruction_id"], "invocation": payload,
                        "status": "created", "attempts": 0, "review_retries": 0, "continuations": 0,
                        "executor": payload["executor"], "requested_model": payload["model"],
                        "review_history": [], "created_at": now_ms(), "execution_stopped": True,
                        "max_attempts": self.settings.max_attempts}
            elif operation == "review":
                self.review(task, payload)
            elif operation == "cancel":
                if set(payload) - {"task_id", "request_id"}:
                    raise ValueError("Unknown cancel field")
                if task["status"] in {"accepted", "integrated", "closed"}:
                    raise ValueError("Finalize accepted work through close")
                task.update(cancel_requested=True, cancel_requested_at=now_ms())
                if task["status"] in {"created", "retrying", "awaiting_review", "blocked", "failed", "cancelled"} and task["execution_stopped"]:
                    task["status"] = "cancelled"
                else:
                    task["status"] = "cancelling"
            else:
                self.close(task, payload)
            self.store.put(db, task)
            db.execute("UPDATE authorizations SET consumed=1 WHERE session_id=? AND request_id=?", (session, request_id))
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (session, request_id, operation, encoded, task["task_id"]))
            return task

    @staticmethod
    def envelope(payload: dict) -> None:
        allowed = {"request_version", "request_id", "instruction_id", "executor", "model", "task", "parent_repo", "parent_worktree", "mode", "context"}
        if set(payload) - allowed:
            raise ValueError("Unknown invocation field; operator limits are not task inputs")
        for key in allowed - {"context"}:
            required_text(payload, key)
        if payload["request_version"] != "1" or payload["mode"] != "write":
            raise ValueError("Only request_version 1 and write mode are supported")
        if any(not Path(payload[key]).is_absolute() for key in ("parent_repo", "parent_worktree")):
            raise ValueError("Repository and worktree paths must be absolute")
        context = payload.get("context", {})
        if not isinstance(context, dict) or set(context) - {"relevant_paths", "constraints"}:
            raise ValueError("Invalid context")
        if any(not isinstance(values, list) or any(not isinstance(item, str) for item in values) for values in context.values()):
            raise ValueError("Context paths and constraints must be arrays of strings")

    def review(self, task: dict, payload: dict) -> None:
        if set(payload) - {"task_id", "request_id", "expected_attempt", "outcome", "feedback"}:
            raise ValueError("Unknown review field")
        expected = payload.get("expected_attempt")
        if type(expected) is not int or expected != task["attempts"]:
            raise ValueError("Stale expected_attempt")
        if task.get("cancel_requested"):
            raise ValueError("Task is marked for cancellation")
        outcome = required_text(payload, "outcome")
        state = task["status"]
        if outcome == "accepted" and state in {"awaiting_review", "failed"}:
            if not task["execution_stopped"] or not task.get("final_tree"):
                raise ValueError("Acceptance requires verified stopped execution and a current final tree")
            task["status"] = "accepted"
        elif (outcome == "rejected" and state == "awaiting_review") or (outcome == "continue" and state == "blocked"):
            if task["attempts"] >= self.settings.max_attempts:
                raise ValueError("Operator-configured launch ceiling is exhausted")
            task["feedback"] = required_text(payload, "feedback")
            task["status"] = "retrying"
            task["review_retries" if outcome == "rejected" else "continuations"] += 1
        else:
            raise ValueError("Invalid review outcome for task state")
        task["review_history"].append({"attempt": expected, "outcome": outcome, "source_status": state,
                                       "reason": payload.get("feedback", ""), "at": now_ms()})

    @staticmethod
    def close(task: dict, payload: dict) -> None:
        if set(payload) - {"task_id", "request_id", "disposition", "keep_artifacts"}:
            raise ValueError("Unknown close field")
        disposition = payload.get("disposition")
        if disposition not in {"integrated", "discarded"} or type(payload.get("keep_artifacts", False)) is not bool:
            raise ValueError("Invalid final disposition or keep_artifacts")
        if task["status"] in ACTIVE | {"interrupted"} or not task["execution_stopped"]:
            raise ValueError("Cannot close active execution or uncertain liveness")
        if disposition == "integrated" and task["status"] not in {"accepted", "integrated"}:
            raise ValueError("Integration requires prior parent acceptance")
        if task["status"] == "closed":
            raise ValueError("Replay the original close request to retry cleanup")
        keep = payload.get("keep_artifacts", False)
        task.update(status="closed", disposition=disposition, keep_artifacts=keep,
                    cleanup_pending=not keep, cleanup_requested=not keep)

    def status(self, payload: dict) -> dict | list:
        if set(payload) - {"task_id", "filter", "native_offset"}:
            raise ValueError("Unknown status field")
        offset = payload.get("native_offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("native_offset must be a nonnegative integer")
        if payload.get("task_id"):
            if str(payload["task_id"]).startswith("native-"):
                return self.native.status(payload["task_id"])
            return public(self.store.get(payload["task_id"]), self.store)
        if payload.get("filter") not in {None, "needs_decision"}:
            raise ValueError("Unknown status filter")
        tasks = [task for task in self.store.all() if task["status"] != "closed"]
        if payload.get("filter") == "needs_decision":
            tasks = [task for task in tasks if task["status"] in DECISIONS or
                     (task["status"] == "awaiting_review" and task["attempts"] >= self.settings.max_attempts)]
        keys = {"task_id", "status", "attempts", "review_retries", "continuations", "last_event_at",
                "executor", "requested_model", "resolved_model", "failure_reason", "execution_outcome", "max_attempts", "worktree"}
        rows = []
        for task in tasks:
            row = {key: value for key, value in task.items() if key in keys}
            row.update(visibility(task, self.store.history(task["task_id"])))
            rows.append(row)
        if payload.get("filter") is None:
            rows.extend(self.native.status(offset=offset))
        return rows


class PhaseStopped(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason


class Runtime:
    def __init__(self, controller: Controller):
        self.control = controller
        self.store = controller.store
        self.workers: dict[str, threading.Thread] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()

    def start(self) -> None:
        # In serve(), the single-instance daemon lock is already held here.
        self.control.native.reconcile_restart()
        for task in self.store.all():
            if task["status"] != "closed" and task["max_attempts"] != self.control.settings.max_attempts:
                self.store.update(task["task_id"], lambda t: t.update(max_attempts=self.control.settings.max_attempts))
            if task["status"] in {"validating", "preparing", "running", "cancelling"}:
                record = task.get("process")
                if record and (owned_state(record) == "live" or
                               (owned_state(record) == "stopped" and Path(record["completion"]).exists())):
                    self.spawn(task, recovered=True)
                elif task.get("cancel_requested") and (not record or owned_state(record) == "stopped"):
                    self.store.update(task["task_id"], lambda t: t.update(status="cancelled", execution_stopped=True))
                else:
                    self.store.update(task["task_id"], lambda t: t.update(status="interrupted", execution_stopped=False,
                                                                         failure_reason="Restart could not establish execution outcome"))
        self.scheduler = threading.Thread(target=self.schedule, name="router-admission", daemon=True)
        self.scheduler.start()

    def spawn(self, task: dict, recovered: bool = False, cleaning: bool = False) -> None:
        target = self.clean if cleaning else self.work
        thread = threading.Thread(target=target, args=(task["task_id"],) if cleaning else (task["task_id"], recovered),
                                  name=task["task_id"], daemon=True)
        with self.lock:
            self.workers[task["task_id"]] = thread
        thread.start()

    def schedule(self) -> None:
        while not self.stopping.is_set():
            tasks = self.store.all()
            reserved = {task["task_id"] for task in tasks if task["status"] in {"interrupted", "cancelling"}
                        and not task["execution_stopped"]}
            for task in tasks:
                with self.lock:
                    occupied = set(self.workers) | reserved
                    eligible = task["task_id"] not in self.workers and (
                        task["status"] == "cancelling" or len(occupied) < self.control.settings.concurrency_cap)
                if not eligible:
                    continue
                if task["status"] in {"created", "retrying", "cancelling"}:
                    self.spawn(task)
                elif task["status"] == "closed" and task.get("cleanup_requested"):
                    self.store.update(task["task_id"], lambda t: t.update(cleanup_requested=False))
                    self.spawn(task, cleaning=True)
            self.control.changed.wait(0.1)
            self.control.changed.clear()

    def retired(self, task_id: str) -> None:
        with self.lock:
            self.workers.pop(task_id, None)
        self.control.changed.set()

    def validate(self, task: dict) -> dict:
        invocation = task["invocation"]
        if invocation["executor"] != "opencode":
            raise ValueError("Only the OpenCode executor is supported; no fallback")
        model = self.control.settings.models.get(invocation["model"])
        if model is None:
            raise ValueError("Unknown configured model alias; no fallback")
        root = Path(invocation["parent_worktree"]).resolve()
        repo = Path(invocation["parent_repo"]).resolve()
        validate_repository(root)
        common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        configured = self.control.settings.repositories.get(str(root))
        if configured is None or repo not in {root, common.parent}:
            raise ValueError("Repository/worktree is not an operator-configured target")
        credential_file = Path(self.control.environment.get("AGENT_ROUTER_ENV_FILE", "~/.agent-router/.env")).expanduser().resolve()
        if credential_file.is_relative_to(root):
            relative = str(credential_file.relative_to(root))
            if git(root, "ls-files", "--", relative).strip():
                raise ValueError("The provider env file must not be tracked")
            ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", relative], check=False)
            if ignored.returncode != 0:
                raise ValueError("The provider env file must be ignored when inside the repository")
        permission_file = root / "opencode.json"
        try:
            permission = json.loads(permission_file.read_text())["permission"]
        except (OSError, ValueError, KeyError) as error:
            raise ValueError("Repository requires opencode.json with an explicit non-interactive permission profile") from error
        def noninteractive(value):
            return all(noninteractive(v) for v in value.values()) if isinstance(value, dict) else value in {"allow", "deny"}
        if not isinstance(permission, dict) or not permission or not noninteractive(permission):
            raise ValueError("Permission profile must explicitly use allow/deny, never ask")
        return {"executor": "opencode", "requested_model": invocation["model"], "resolved_model": model,
                "setup_command": configured["setup_command"], "permission_profile": permission,
                "child_session_id": "ses" + uuid.uuid4().hex}

    def phase(self, task_id: str, name: str, command: list[str], cwd: Path, *, collecting: bool = False) -> dict:
        task = self.store.get(task_id)
        started = now_ms()
        directory = Path(task["artifact_dir"]) / f"attempt-{task['attempt_number']}" / name
        environment = self.control.environment.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            environment.pop(key, None)
        if "worktree" in task and name in {"setup", "child"}:
            environment = child_environment(environment, task)
        process = None
        def register(record):
            def persist(current):
                if current.get("cancel_requested") and not collecting:
                    raise PhaseStopped("cancelled")
                if not collecting and now_ms() >= current["deadline_at"]:
                    raise PhaseStopped("router-timeout")
                current.update(process=record, phase=name, phase_started_at=started, execution_stopped=False)
                if name == "child":
                    current["attempts"] = current["attempt_number"]
                    current["status"] = "running"
            self.store.update(task_id, persist)
        process, record = launch(command, cwd, environment, directory, register)
        with self.lock:
            self.processes[task_id] = process
        try:
            return self.observe(task_id, record, process, collecting=collecting)
        finally:
            if process.poll() is not None:
                with self.lock:
                    self.processes.pop(task_id, None)

    def observe(self, task_id: str, record: dict, process=None, *, collecting: bool = False) -> dict:
        offset = self.store.get(task_id).get("event_offset", 0)
        while True:
            task = self.store.get(task_id)
            offset = self.events(task, record, offset)
            state = supervised_state(record, process)
            if state == "stopped":
                self.events(task, record, offset, final=True)
                completion = Path(record["completion"])
                if not completion.exists():
                    if task.get("cancel_requested"):
                        raise PhaseStopped("cancelled")
                    raise UncertainProcess("Execution stopped without reliable completion evidence")
                self.store.update(task_id, lambda t: t.update(execution_stopped=True))
                return json.loads(completion.read_text())
            if state != "live":
                raise UncertainProcess("Process creation identity or liveness is uncertain")
            cancel = task.get("cancel_requested") and (not collecting or task.get("cancel_requested_at", 0) > task["phase_started_at"])
            timeout = not collecting and now_ms() >= task["deadline_at"]
            if cancel or timeout:
                stop_owned(record, process)
                self.store.update(task_id, lambda t: t.update(execution_stopped=True))
                raise PhaseStopped("cancelled" if cancel else "router-timeout")
            time.sleep(0.05)

    def events(self, task: dict, record: dict, offset: int, *, final: bool = False) -> int:
        if task.get("phase") != "child":
            return offset
        values = []
        with Path(record["stdout"]).open("rb") as stream:
            stream.seek(offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line or (not line.endswith(b"\n") and not final):
                    stream.seek(start)
                    break
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        values.append(event)
                except (ValueError, UnicodeError):
                    continue
            next_offset = stream.tell()
        if next_offset == offset:
            return offset
        def update(current):
            current["event_offset"] = next_offset
            if values:
                current["last_event_at"] = now_ms()
            for event in values:
                part = event.get("part", {})
                if not isinstance(part, dict):
                    continue
                if event.get("type") == "text" and isinstance(part.get("text"), str):
                    current["summary"] = (current.get("summary", "") + part["text"])[-8000:]
                if event.get("type") == "error":
                    current["execution_error"] = True
                if event.get("type") == "step_finish":
                    if isinstance(part.get("cost"), (int, float)):
                        current["cost"] = current.get("cost", 0) + float(part["cost"])
                    usage = current.setdefault("usage", {})
                    for key, value in part.get("tokens", {}).items():
                        if isinstance(value, (int, float)):
                            usage[key] = usage.get(key, 0) + value
                        elif isinstance(value, dict):
                            nested = usage.setdefault(key, {})
                            for subkey, amount in value.items():
                                nested[subkey] = nested.get(subkey, 0) + amount
        self.store.update(task["task_id"], update)
        return next_offset

    def snapshot_phase(self, task_id: str, action: str) -> None:
        task = self.store.get(task_id)
        directory = Path(task["artifact_dir"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = directory / f"{action}-{task['attempt_number']}.input.json"
        destination = directory / f"{action}-{task['attempt_number']}.result.json"
        # Contains task instructions, but never the provider environment.
        source.write_text(canonical(task))
        helper = str(Path(__file__).with_name("snapshot.py"))
        result = self.phase(task_id, action, [sys.executable, helper, action, str(source), str(destination)],
                            Path(task["invocation"]["parent_worktree"]), collecting=action == "capture")
        if result["exit_code"] != 0 or not destination.exists():
            raise ValueError(f"Git {action} failed; inspect the phase stderr artifact")
        data = json.loads(destination.read_text())
        self.store.update(task_id, lambda t: t.update(data))

    def work(self, task_id: str, recovered: bool = False) -> None:
        try:
            self.execute(task_id, recovered)
        finally:
            self.retired(task_id)

    def execute(self, task_id: str, recovered: bool = False) -> None:
        outcome = "completed"
        reason = None
        try:
            task = self.store.get(task_id)
            if not recovered and task["status"] not in {"created", "retrying", "cancelling"}:
                return
            if task["status"] == "cancelling" and not recovered:
                record = task.get("process")
                if record:
                    with self.lock:
                        process = self.processes.get(task_id)
                    stop_owned(record, process)
                    with self.lock:
                        self.processes.pop(task_id, None)
                self.store.update(task_id, lambda t: t.update(status="cancelled", execution_stopped=True))
                return
            if not recovered:
                if task["status"] == "created":
                    self.store.update(task_id, lambda t: t.update(status="validating"))
                    validated = self.validate(task)
                    self.store.update(task_id, lambda t: t.update(validated))
                def begin(current):
                    if current.get("cancel_requested"):
                        raise PhaseStopped("cancelled")
                    if current["attempts"] >= self.control.settings.max_attempts:
                        raise ValueError("Launch ceiling exhausted")
                    current.update(status="preparing", attempt_number=current["attempts"] + 1,
                                   attempt_started_at=now_ms(), deadline_at=now_ms() + int(self.control.settings.attempt_timeout_seconds * 1000),
                                   artifact_dir=str(self.control.directory / "tasks" / task_id),
                                   max_attempts=self.control.settings.max_attempts)
                    for key in ("final_tree", "patch", "execution_outcome", "failure_reason", "exit_code", "last_event_at", "summary", "cost", "usage", "execution_error", "event_offset", "process", "phase", "phase_started_at"):
                        current.pop(key, None)
                self.store.update(task_id, begin)
                task = self.store.get(task_id)
                if "snapshot_commit" not in task:
                    self.store.update(task_id, lambda t: t.update(worktree=str(Path(t["artifact_dir"]) / "worktree"), branch=f"agent/{task_id}"))
                    self.snapshot_phase(task_id, "prepare")
            else:
                task = self.store.get(task_id)
                phase = task["phase"]
                result = self.observe(task_id, task["process"], collecting=phase == "capture")
                if result["exit_code"] and phase != "child":
                    raise ValueError(f"Recovered {phase} failed with exit code {result['exit_code']}")
                if phase in {"prepare", "capture"}:
                    result_file = Path(task["artifact_dir"]) / f"{phase}-{task['attempt_number']}.result.json"
                    self.store.update(task_id, lambda t: t.update(json.loads(result_file.read_text())))
                if phase == "setup":
                    self.store.update(task_id, lambda t: t.update(setup_done=True))
                if phase in {"child", "capture"}:
                    if phase == "child":
                        self.store.update(task_id, lambda t: t.update(exit_code=result["exit_code"]))
                    task = self.store.get(task_id)
                    failed = task.get("execution_error") or (phase == "child" and result["exit_code"] != 0)
                    outcome = task.get("pending_outcome", "failed" if failed else "completed")
                    reason = task.get("pending_reason", "Executor failed" if failed else None)
                    self.finish(task_id, outcome, reason, already_captured=phase == "capture")
                    return
            task = self.store.get(task_id)
            if not task.get("setup_done"):
                result = self.phase(task_id, "setup", ["/bin/sh", "-c", task["setup_command"]], Path(task["worktree"]))
                if result["exit_code"]:
                    self.store.update(task_id, lambda t: t.update(exit_code=result["exit_code"]))
                    raise ValueError("Dependency setup failed; child was not launched")
                self.store.update(task_id, lambda t: t.update(setup_done=True))
            task = self.store.get(task_id)
            result = self.phase(task_id, "child", opencode_command(self.control.settings.opencode_command, task), Path(task["worktree"]))
            self.store.update(task_id, lambda t: t.update(exit_code=result["exit_code"]))
            if result["exit_code"] or self.store.get(task_id).get("execution_error"):
                outcome, reason = "failed", "Executor failed; inspect stdout/stderr artifacts"
        except PhaseStopped as stopped:
            outcome = "cancelled" if stopped.reason == "cancelled" else "failed"
            reason = stopped.reason
        except UncertainProcess as error:
            message = str(error)
            self.store.update(task_id, lambda t: t.update(status="interrupted", execution_stopped=False, failure_reason=message))
            self.store.save_attempt(self.store.get(task_id))
            return
        except Exception as error:
            LOGGER.exception("Task phase failed: %s", task_id)
            error_kind = type(error).__name__
            task = self.store.get(task_id)
            if not task["execution_stopped"]:
                try:
                    with self.lock:
                        process = self.processes.get(task_id)
                    stop_owned(task["process"], process)
                    with self.lock:
                        self.processes.pop(task_id, None)
                    self.store.update(task_id, lambda t: t.update(execution_stopped=True))
                except UncertainProcess:
                    self.store.update(task_id, lambda t: t.update(status="interrupted", failure_reason=error_kind))
                    return
            outcome, reason = "failed", str(error)
        self.finish(task_id, outcome, reason)

    def finish(self, task_id: str, outcome: str, reason: str | None, already_captured: bool = False) -> None:
        task = self.store.get(task_id)
        self.store.update(task_id, lambda t: t.update(pending_outcome=outcome, pending_reason=reason))
        if task.get("snapshot_commit") and Path(task["worktree"]).exists() and not already_captured:
            try:
                self.snapshot_phase(task_id, "capture")
            except UncertainProcess as error:
                message = str(error)
                self.store.update(task_id, lambda t: t.update(status="interrupted", execution_stopped=False, failure_reason=message))
                return
            except PhaseStopped:
                pass  # Explicit cancellation preserves the established child outcome.
            except Exception:
                LOGGER.exception("Final-tree capture failed: %s", task_id)
                outcome, reason = "failed", "Final-tree capture failed; worktree retained"
        def finalize(current):
            current.update(status={"completed": "awaiting_review", "failed": "failed", "cancelled": "cancelled"}[outcome],
                           execution_stopped=True,
                           duration_ms=now_ms() - current.get("attempt_started_at", current["created_at"]))
            if "attempt_number" in current:
                current["execution_outcome"] = outcome
            if current.get("cancel_requested"):
                current["status"] = "cancelled"
            if reason:
                current["failure_reason"] = reason
            current.pop("pending_outcome", None)
            current.pop("pending_reason", None)
        self.store.update(task_id, finalize)
        self.store.save_attempt(self.store.get(task_id))

    def clean(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
            if "worktree" in task:
                cleanup(Path(task["invocation"]["parent_worktree"]), task)
            self.store.update(task_id, lambda t: t.update(cleanup_pending=False, cleanup_error=None, artifacts_removed_at=now_ms()))
        except Exception as error:
            LOGGER.exception("Task cleanup failed: %s", task_id)
            message = str(error)
            self.store.update(task_id, lambda t: t.update(cleanup_error=message))
        finally:
            self.retired(task_id)

    def stop(self) -> None:
        self.stopping.set()
        self.control.changed.set()
        self.scheduler.join(timeout=2)
        for task in self.store.all():
            if task["status"] in ACTIVE:
                def cancel(current):
                    current.update(cancel_requested=True, cancel_requested_at=now_ms())
                    if current["status"] in {"created", "retrying"} and current["execution_stopped"]:
                        current["status"] = "cancelled"
                self.store.update(task["task_id"], cancel)
        with self.lock:
            workers = list(self.workers.values())
        deadline = time.monotonic() + 10
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(15)
        try:
            line = self.rfile.readline(1024 * 1024 + 1)
            if not line:
                return
            if len(line) > 1024 * 1024:
                raise ValueError("Request exceeds one MiB")
            message = json.loads(line)
            operation, payload = message["operation"], message["payload"]
            if operation == "daemon_stop":
                result = {"stopping": True}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                result = self.server.control.handle(operation, payload)
            response = {"ok": True, "result": result}
        except (ValueError, KeyError, OSError) as error:
            response = {"ok": False, "error": str(error)}
        except Exception:
            LOGGER.exception("Control request failed")
            response = {"ok": False, "error": "Internal router error; inspect the operator log"}
        try:
            self.wfile.write((canonical(response) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # The durable operation survives a disconnected client.


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(settings: Settings, environment: dict[str, str], directory: Path, endpoint: Path) -> None:
    controller = Controller(settings, environment, directory)
    directory.chmod(0o700)
    endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "daemon.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Agent Router is already running") from error
        if endpoint.exists():
            import stat
            endpoint_info = endpoint.lstat()
            if not stat.S_ISSOCK(endpoint_info.st_mode) or endpoint_info.st_uid != os.getuid():
                raise ValueError("Socket path exists and is not a socket; refusing to overwrite it")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(endpoint))
                except ConnectionRefusedError:
                    pass  # An owned, stale socket is safe to replace.
                else:
                    raise ValueError("A daemon is already listening on this socket")
            endpoint.unlink()
        logging.basicConfig(filename=directory / "router.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        runtime = Runtime(controller)
        with Server(str(endpoint), Handler) as server:
            server.control = controller
            endpoint.chmod(0o600)
            runtime.start()
            print(f"Agent Router listening on {endpoint}", flush=True)
            try:
                server.serve_forever(poll_interval=0.1)
            finally:
                runtime.stop()
                endpoint.unlink(missing_ok=True)
