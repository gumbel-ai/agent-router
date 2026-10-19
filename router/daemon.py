"""Local control plane: durable operations, bounded supervision, no auto-spawn."""

import fcntl
import json
import logging
import os
import queue
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from router import claude_tracing
from router.capacity import CapacitySampler
from router.child import (
    OPENCODE_LANGFUSE_PLUGIN,
    TRACING_UNAVAILABLE,
    child_environment,
    executor_command,
    langfuse_credentials_available,
    launch,
    strip_langfuse_environment,
)
from router.claude_executor import collect as collect_claude
from router.claude_executor import public_usage as claude_public_usage
from router.claude_executor import (
    resolve_executable,
    validate_command,
    validate_persistence,
)
from router.claude_executor import validate as validate_claude
from router.claude_executor import version as claude_version
from router.client import CompletionRefused
from router.codex import collect as collect_codex
from router.codex import price_metadata
from router.config import Settings
from router.correlation import identifier as correlation_identifier
from router.db import Store, now_ms
from router.dispatch import MUTATIONS, canonical
from router.fleet_snapshot import AlertHistory
from router.host import HostIdentity
from router.managed import ManagedProcesses
from router.native import SOURCES, NativeObserver, identifier, safe_text
from router.paths import observed_project
from router.process import (
    UncertainProcess,
    UnconfirmedShutdown,
    owned_state,
    stop_owned,
    supervised_state,
    verify_shutdown,
)
from router.processes import SessionProcesses
from router.sessions import SessionObserver
from router.snapshot import cleanup, git, validate_repository

ACTIVE = {"created", "validating", "preparing", "running", "retrying", "cancelling"}
DECISIONS = {"awaiting_review", "blocked", "failed", "interrupted"}
LOGGER = logging.getLogger(__name__)
LIFECYCLE_STOP_TIMEOUT_SECONDS = 2.5
LIFECYCLE_ACK_TIMEOUT_SECONDS = .04
# Lost lifecycle evidence keeps snapshots partial for this long, not forever.
LIFECYCLE_PROBLEM_WINDOW_MS = 60_000


def _lifecycle_result(queued: bool, persistence: dict[str, str],
                      reason: str | None = None) -> dict:
    """Build the additive lifecycle acknowledgement contract."""
    states = dict(persistence)
    # An event outside every observed project is deliberately not recorded; that is not a failure.
    persisted = bool(states) and all(state in {"persisted", "not_observed"} for state in states.values())
    partial = any(state == "persisted" for state in states.values()) and not persisted
    result = {
        "accepted": persisted,
        "queued": queued,
        "persisted": persisted,
        "partial": partial,
        "persistence": states,
    }
    if reason is not None:
        result["reason"] = reason
    return result


def blocks_maintenance(task: dict) -> bool:
    return (task["status"] in ACTIVE or not task["execution_stopped"] or
            bool(task.get("cleanup_requested") or task.get("cleanup_pending") and task.get("cleanup_error") is None))


def required_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def public(task: dict, store: Store | None = None, history: list[dict] | None = None) -> dict:
    result = {key: value for key, value in task.items() if key not in {
        "session_id", "invocation", "feedback", "permission_profile", "setup_command", "cleanup_requested", "token_prices",
        "codex_usage_baseline", "codex_usage_total", "codex_baseline_complete", "claude_baseline", "claude_total", "claude_executable", "_claude_tracing"}}
    attempt_history = store.history(task["task_id"]) if history is None and store is not None else history or []
    result.update(visibility(task, attempt_history))
    if store is not None or history is not None:
        if task.get("executor") == "claude":
            attempt_history = [{**frame, **claude_public_usage(frame)} for frame in attempt_history]
        result["attempt_history"] = [{key: value for key, value in frame.items()
                                      if key not in {"claude_baseline", "claude_total", "claude_executable", "_claude_tracing"}}
                                     for frame in attempt_history]
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
    if task.get("executor") == "claude":
        frames = [{**frame, **claude_public_usage(frame)} for frame in frames]
    cost = sum(frame["cost"] for frame in frames) if all(frame.get("cost") is not None for frame in frames) else None
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
    complete = all(frame.get("usage_complete", bool(frame.get("usage"))) for frame in frames)
    if task.get("executor") == "claude":
        accounting = claude_public_usage({"usage": usage, "usage_complete": complete})
        usage, complete = accounting["usage"], accounting["usage_complete"]
    return {"owner": "router", "control_mode": "control", "parent_session_id": task["session_id"],
            "parent_agent_id": task.get("parent_agent_id"), "task": task["invocation"]["task"], "cwd": task["invocation"]["parent_worktree"],
            "started_at": task.get("attempt_started_at"),
            "elapsed_ms": elapsed,
            "models": [model] if model else [], "model_source": "configured" if model else None,
            "cost": cost, "cost_source": task.get("cost_source", "executor_reported") if cost is not None else "unavailable",
            "cost_note": task.get("cost_note", "Task-total executor estimate, not billed spend") if cost is not None else
                "Cost unavailable for one or more attempts" + (": " + task["cost_note"] if task.get("cost_note") else ""),
            "price_date": task.get("price_date"), "price_source": task.get("price_source"),
            "price_overrides_used": task.get("price_overrides_used", []), "price_sources": {},
            "usage": usage or None, "usage_source": "executor_reported" if usage else "unavailable",
            "usage_complete": complete,
            "cleanup_pending": bool(task.get("cleanup_pending", False)), "cleanup_error": task.get("cleanup_error"),
            "shutdown_unconfirmed": bool(task.get("shutdown_unconfirmed", False)),
            "activity": task.get("executor_activity", task.get("phase")), "visibility_reason": None, "runtime_partial": False}


def require_state_directory(settings: Settings, directory: Path) -> Path:
    resolved = directory.resolve()
    if resolved in {Path.home().resolve(), Path("/")}:
        raise ValueError("Router state requires a dedicated directory, not home or filesystem root")
    if any(resolved.is_relative_to(Path(root)) for root in settings.repositories):
        raise ValueError("Router state must be outside every configured parent worktree")
    return resolved


class Controller:
    def __init__(self, settings: Settings, environment: dict[str, str], directory: Path):
        self.settings = settings
        self.environment = environment
        self.directory = require_state_directory(settings, directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.host = HostIdentity(self.directory)
        self.store = Store(self.directory / "router.sqlite3", self.host.host_id)
        with self.store.transaction() as db:
            db.execute("INSERT INTO hosts(host_id,data) VALUES(?,?) ON CONFLICT(host_id) DO UPDATE SET data=excluded.data",
                       (self.host.host_id, canonical(self.host.view(now_ms()))))
        self.native = NativeObserver(self.store, self.host.host_id)
        self.sessions = SessionObserver(self.store, self.host.host_id)
        self.session_processes = SessionProcesses()
        self.managed = ManagedProcesses(self.store, self.host.host_id, self.directory)
        self.capacity = CapacitySampler(self.directory)
        self.alert_history = AlertHistory()
        self.lifecycle_queue: queue.Queue[tuple[dict, threading.Event, dict] | None] = queue.Queue(maxsize=1024)
        self.lifecycle_thread: threading.Thread | None = None
        self.lifecycle_stopping = False
        self.lifecycle_stop_deadline: float | None = None
        self.lifecycle_failures = 0
        self.lifecycle_dropped = 0
        self.lifecycle_problem_at: int | None = None
        self.lifecycle_pending = 0
        self.lifecycle_direct_active = 0
        self.lifecycle_health_lock = threading.Lock()
        self._lifecycle_generation = 0
        self.changed = threading.Event()
        self.lifecycle_lock = threading.Lock()
        self.draining = False

    def start_lifecycle_writer(self) -> None:
        with self.lifecycle_lock:
            if self.lifecycle_thread is not None:
                return
            self.lifecycle_stopping = False
            self.lifecycle_stop_deadline = None
            self.lifecycle_thread = threading.Thread(
                target=self._write_lifecycle, name="router-lifecycle", daemon=True,
            )
            self.lifecycle_thread.start()

    def _write_lifecycle(self) -> None:
        while True:
            work = self.lifecycle_queue.get()
            completed = None
            scope_error = None
            try:
                if work is None:
                    return
                payload, completed, outcome = work
                admitted = set()
                # Admission is per session: one request's components may name different sessions,
                # and one component's malformed identity or scope error must not discard another's.
                sessions: dict[tuple, list[str]] = {}
                for key in outcome["persistence"]:
                    event = payload[key]
                    identity = (event.get("source"), event.get("session_id")) if isinstance(event, dict) else None
                    if identity is None or not all(isinstance(part, str) for part in identity):
                        outcome["persistence"][key] = "failed"
                        LOGGER.warning("Lifecycle %s observation has no valid source and session_id", key)
                        with self.lifecycle_health_lock:
                            self.lifecycle_failures += 1
                            self.lifecycle_problem_at = now_ms()
                        continue
                    sessions.setdefault(identity, []).append(key)
                for keys in sessions.values():
                    try:
                        in_scope = self.in_scope([payload[key] for key in keys], self._lifecycle_write_timeout())
                    except (sqlite3.Error, ValueError, OSError) as error:
                        scope_error = str(error)
                        LOGGER.warning("Lifecycle observation project scope could not be checked: %s", error)
                        with self.lifecycle_health_lock:
                            self.lifecycle_failures += 1
                            self.lifecycle_problem_at = now_ms()
                        for key in keys:
                            outcome["persistence"][key] = "failed"
                        continue
                    for key in keys:
                        if in_scope:
                            admitted.add(key)
                        else:
                            outcome["persistence"][key] = "not_observed"
                for key, observer in (("session", self.sessions), ("native", self.native)):
                    event = payload.get(key)
                    if key not in admitted:
                        continue
                    try:
                        observer.observe(event, timeout=self._lifecycle_write_timeout())
                    except (sqlite3.Error, ValueError, KeyError, TypeError, OSError, OverflowError) as error:
                        outcome["persistence"][key] = "failed"
                        LOGGER.warning("Lifecycle %s observation could not be persisted: %s", key, error)
                        with self.lifecycle_health_lock:
                            self.lifecycle_failures += 1
                            self.lifecycle_problem_at = now_ms()
                    # Protect the long-lived writer from unexpected observer bugs.
                    except Exception:
                        outcome["persistence"][key] = "failed"
                        LOGGER.exception("Unexpected lifecycle %s observation failure", key)
                        with self.lifecycle_health_lock:
                            self.lifecycle_failures += 1
                            self.lifecycle_problem_at = now_ms()
                    else:
                        outcome["persistence"][key] = "persisted"
            # One malformed work item must not kill the writer.
            except Exception:
                LOGGER.exception("Unexpected lifecycle work item failure")
                with self.lifecycle_health_lock:
                    self.lifecycle_failures += 1
                    self.lifecycle_problem_at = now_ms()
            finally:
                if work is not None:
                    for key, state in outcome["persistence"].items():
                        if state == "pending":
                            outcome["persistence"][key] = "failed"
                    failed = [key for key, state in outcome["persistence"].items() if state == "failed"]
                    reason = None
                    if failed:
                        prefix = ("Lifecycle observation partially persisted; failed components: "
                                  if any(state == "persisted" for state in outcome["persistence"].values())
                                  else "Lifecycle observation could not be persisted; failed components: ")
                        reason = prefix + ", ".join(failed)
                        if scope_error is not None:
                            reason += "; project scope could not be checked: " + scope_error
                    terminal = _lifecycle_result(False, outcome["persistence"], reason)
                    # No clear(): a request thread that timed out may be reading
                    # outcome["persistence"] concurrently; update() swaps it whole.
                    outcome.update(terminal)
                    with self.lifecycle_health_lock:
                        self._lifecycle_generation += 1
                        self.lifecycle_pending -= 1
                if completed is not None:
                    completed.set()
                self.lifecycle_queue.task_done()
            if self.lifecycle_stopping and self.lifecycle_queue.empty():
                return

    def _lifecycle_write_timeout(self) -> float:
        if not self.lifecycle_stopping:
            return 1
        with self.lifecycle_health_lock:
            pending = max(1, self.lifecycle_pending)
        deadline = self.lifecycle_stop_deadline
        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else 0.0
        # Share the remaining shutdown window across the queued work. Each
        # item may contain three writes (session admission and two components),
        # so no single contended write consumes the entire hard stop budget.
        # SQLite still gets a nonzero opportunity.
        return max(.001, min(1.0, remaining / (pending * 3)))

    def stop_lifecycle_writer(self) -> None:
        with self.lifecycle_lock:
            thread = self.lifecycle_thread
            if thread is None:
                return
            signal_writer = not self.lifecycle_stopping
            self.lifecycle_stopping = True
            if signal_writer:
                self.lifecycle_stop_deadline = time.monotonic() + LIFECYCLE_STOP_TIMEOUT_SECONDS
        # Admission is closed under lifecycle_lock. The writer also exits after
        # draining an empty queue, so a full queue never requires a blocking put.
        deadline = self.lifecycle_stop_deadline or time.monotonic()
        if signal_writer:
            try:
                self.lifecycle_queue.put_nowait(None)
            except queue.Full:
                LOGGER.warning(
                    "Lifecycle writer is still draining; %d observations remain queued",
                    self.lifecycle_queue.qsize(),
                )
        thread.join(timeout=max(0, deadline - time.monotonic()))
        if thread.is_alive():
            with self.lifecycle_health_lock:
                pending = self.lifecycle_pending
            LOGGER.warning(
                "Lifecycle writer did not stop within %.1f seconds; %d queued observations remain pending",
                LIFECYCLE_STOP_TIMEOUT_SECONDS, pending,
            )
            return
        with self.lifecycle_lock:
            if self.lifecycle_thread is thread:
                self.lifecycle_thread = None

    def lifecycle_health(self) -> str | None:
        with self.lifecycle_health_lock:
            failures, dropped, pending = self.lifecycle_failures, self.lifecycle_dropped, self.lifecycle_pending
            problem_at = self.lifecycle_problem_at
        recent = problem_at is not None and now_ms() - problem_at <= LIFECYCLE_PROBLEM_WINDOW_MS
        if not pending and not recent:
            return None
        return ("Lifecycle observations not persisted: "
                f"{failures} write failures, {dropped} queue overflows, {pending} queued writes pending")

    def lifecycle_generation(self) -> int:
        with self.lifecycle_health_lock:
            return self._lifecycle_generation

    def lifecycle_consistency(self) -> tuple[int, int]:
        """Return a token and the number of lifecycle writes still in flight."""
        with self.lifecycle_health_lock:
            return (self._lifecycle_generation,
                    self.lifecycle_pending + self.lifecycle_direct_active)

    def in_scope(self, events: list[dict], timeout: float = 10) -> bool:
        """Record a session once it is seen inside an observed project (see observed_project).

        events are one session's. Admission is stored durably, so later events follow the
        session wherever its working directory goes (even past a malformed marker) and
        leaving the project cannot strand it as active. Sessions recorded before admission
        was stored are recognised by their lifecycle rows.
        """
        source, session_id = events[0].get("source"), events[0].get("session_id")
        key = (self.host.host_id, source, session_id)
        with self.store.connect(timeout) as db:
            if (db.execute("SELECT 1 FROM observed_sessions WHERE host_id=? AND source=? AND session_id=?",
                           key).fetchone()
                    or db.execute("SELECT 1 FROM sessions WHERE host_id=? AND source=? AND session_id=?",
                                  key).fetchone()
                    # The agent_id term lets SQLite use the partial observations_identity index.
                    or db.execute("""SELECT 1 FROM observations WHERE json_extract(data,'$.host_id')=?
                            AND json_extract(data,'$.source')=? AND session_id=?
                            AND json_extract(data,'$.agent_id') IS NOT NULL""", key).fetchone()):
                return True
        if not any(observed_project(event.get("cwd")) is not None for event in events):
            return False
        if source in SOURCES and isinstance(session_id, str):
            # Malformed identities are not stored; the observer rejects them with its own error.
            try:
                identifier(session_id, "session_id")
            except ValueError:
                return True
            with self.store.transaction(timeout) as db:
                db.execute("""INSERT INTO observed_sessions(host_id,source,session_id,admitted_at)
                        VALUES(?,?,?,?) ON CONFLICT DO NOTHING""", (*key, now_ms()))
        return True

    def _observe_lifecycle_direct(self, observer, payload: dict) -> dict:
        # The compatibility endpoints remain synchronous, but must participate
        # in the same snapshot consistency protocol as the queued hook path.
        with self.lifecycle_health_lock:
            self.lifecycle_direct_active += 1
            self._lifecycle_generation += 1
        try:
            if not self.in_scope([payload]):
                return {"observed": False, "reason": "Session is outside every observed project"}
            return observer.observe(payload)
        finally:
            with self.lifecycle_health_lock:
                self.lifecycle_direct_active -= 1
                self._lifecycle_generation += 1

    def busy(self) -> int:
        return sum(blocks_maintenance(task) for task in self.store.all())

    def handle(self, operation: str, payload: dict) -> dict | list:
        if not isinstance(payload, dict):
            raise ValueError("Request payload must be an object")  # noqa: TRY004 - public validation contract
        if operation == "submit":
            return self.submit(payload)
        if operation == "authorize":
            return self.authorize(payload)
        if operation == "status":
            return self.status(payload)
        if operation == "health":
            from importlib.metadata import version
            return {"version": version("agent-router"), "state_dir": str(self.directory),
                    "delegation_enabled": self.settings.delegation_enabled,
                    "active_tasks": self.busy()}
        if operation == "managed_start":
            return self.managed.start(payload)
        if operation == "managed_finish":
            return self.managed.finish(payload)
        if operation == "completion_next":
            return self.managed.next_completion(payload)
        if operation == "completion_subscribe":
            return self.managed.subscribe(payload)
        if operation == "completion_consumer_list":
            return self.managed.list_consumers(payload)
        if operation == "completion_consumer_remove":
            return self.managed.remove_consumer(payload)
        if operation == "completion_ack":
            return self.managed.acknowledge(payload)
        if operation == "observe":
            return self._observe_lifecycle_direct(self.native, payload)
        if operation == "session_observe":
            return self._observe_lifecycle_direct(self.sessions, payload)
        if operation == "lifecycle_observe":
            if set(payload) - {"session", "native"}:
                raise ValueError("Unknown lifecycle observation field")
            if not any(payload.get(key) is not None for key in ("session", "native")):
                raise ValueError("Lifecycle observation is empty")
            completed = threading.Event()
            requested = {
                key: "pending" for key in ("session", "native")
                if payload.get(key) is not None
            }
            outcome = {"persistence": dict(requested)}
            with self.lifecycle_lock:
                if (self.lifecycle_stopping or self.lifecycle_thread is None
                        or not self.lifecycle_thread.is_alive()):
                    return _lifecycle_result(
                        False, dict.fromkeys(requested, "rejected"),
                        "Lifecycle observation writer is stopping",
                    )
                with self.lifecycle_health_lock:
                    self.lifecycle_pending += 1
                    self._lifecycle_generation += 1
                try:
                    self.lifecycle_queue.put_nowait((dict(payload), completed, outcome))
                except queue.Full:
                    with self.lifecycle_health_lock:
                        self.lifecycle_pending -= 1
                        self._lifecycle_generation += 1
                        self.lifecycle_dropped += 1
                        self.lifecycle_problem_at = now_ms()
                    return _lifecycle_result(
                        False, dict.fromkeys(requested, "rejected"),
                        "Lifecycle observation queue is full",
                    )
            if not completed.wait(LIFECYCLE_ACK_TIMEOUT_SECONDS):
                return _lifecycle_result(
                    True, outcome["persistence"],
                    "Lifecycle observation is queued but was not durably persisted before the acknowledgement timeout",
                )
            return dict(outcome)
        if operation == "correlation":
            from router.correlation import export
            return export(self.store, self.host.host_id, payload)
        if operation == "task_diff":
            from router.task_diff import export
            return export(self.store, self.directory, self.host.host_id, payload)
        if operation == "request_status":
            from router.request_status import export
            return export(self.store, self.host.host_id, payload)
        if operation == "snapshot":
            if set(payload) - {"history_limit"}:
                raise ValueError("Unknown snapshot option")
            history_limit = payload.get("history_limit", 100)
            if not isinstance(history_limit, int) or isinstance(history_limit, bool) or not 1 <= history_limit <= 1000:
                raise ValueError("history_limit must be an integer from 1 to 1000")
            from router.fleet_snapshot import compose
            return compose(self, history_limit=history_limit)
        if operation not in MUTATIONS:
            raise ValueError("Unknown operation")
        if str(payload.get("task_id", "")).startswith("native-"):
            raise ValueError("Native agent is host-managed; Router cannot control it")
        if not self.settings.delegation_enabled and (operation == "start" or
                operation == "review" and payload.get("outcome") in {"rejected", "continue"}):
            raise ValueError("External delegation is not configured; run router init in the project")
        with self.lifecycle_lock:
            if self.draining:
                raise ValueError("Router is stopping for maintenance; retry after it restarts")
            result = self.mutate(operation, payload)
        self.changed.set()
        return public(result, self.store)

    def submit(self, payload: dict) -> dict:
        from router.paths import delegation_target
        if "cwd" in payload and (not self.settings.delegation_enabled or not delegation_target(payload["cwd"], self.settings.repositories)):
            return {"instruction_id": None}
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
                if "work_item_id" in payload:
                    task["work_item_id"] = payload["work_item_id"]
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
        allowed = {"request_version", "request_id", "instruction_id", "executor", "model", "task", "parent_repo", "parent_worktree", "mode", "context", "work_item_id"}
        if set(payload) - allowed:
            raise ValueError("Unknown invocation field; operator limits are not task inputs")
        for key in allowed - {"context", "work_item_id"}:
            required_text(payload, key)
        if "work_item_id" in payload:
            correlation_identifier(payload["work_item_id"], "work_item_id")
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
            with self.store.snapshot() as db:
                task = self.store.get(payload["task_id"], db)
                history = self.store.history(task["task_id"], db)
            return public(task, history=history)
        if payload.get("filter") not in {None, "needs_decision"}:
            raise ValueError("Unknown status filter")
        with self.store.snapshot() as db:
            tasks = [task for task in self.store.all(db) if task["status"] != "closed" or task.get("cleanup_pending")]
            histories = {task["task_id"]: self.store.history(task["task_id"], db) for task in tasks}
        if payload.get("filter") == "needs_decision":
            tasks = [task for task in tasks if task["status"] in DECISIONS or task.get("cleanup_error") is not None or
                     (task["status"] == "awaiting_review" and task["attempts"] >= self.settings.max_attempts)]
        keys = {"task_id", "status", "attempts", "review_retries", "continuations", "last_event_at",
                "executor", "requested_model", "resolved_model", "failure_reason", "execution_outcome", "max_attempts", "worktree", "work_item_id"}
        rows = []
        for task in tasks:
            row = {key: value for key, value in task.items() if key in keys}
            row.update(visibility(task, histories[task["task_id"]]))
            rows.append(row)
        if payload.get("filter") is None:
            rows.extend(self.native.status(offset=offset))
        return rows

    def inferred_process_records(self, limit: int = 100) -> list[dict]:
        """Processes descending from live Claude and Codex sessions, attributed to that session."""
        return self.session_processes.records(self.host.host_id, self.sessions.process_roots(), limit)

    def router_task_records(self, limit: int = 100) -> list[dict]:
        """Lifecycle-only fleet projection of Router-dispatched tasks: open or recently changed."""
        cutoff = now_ms() - 300_000
        with self.store.snapshot() as db:
            tasks = [task for task in self.store.all(db)
                     if task["status"] != "closed" or task.get("cleanup_pending") or task["updated_at"] >= cutoff]
            # Active work first, then tasks waiting on a decision, then recently finished ones.
            tasks.sort(key=lambda task: (task["status"] not in ACTIVE,
                                         task["status"] not in DECISIONS and not task.get("shutdown_unconfirmed")
                                         and task.get("cleanup_error") is None,
                                         -task["updated_at"]))
            tasks = tasks[:limit]
            histories = {task["task_id"]: self.store.history(task["task_id"], db) for task in tasks}
        records = []
        for task in tasks:
            view = visibility(task, histories[task["task_id"]])
            # Parent references are accepted unbounded at dispatch; omit one the fleet wire
            # cannot carry rather than let it invalidate the host's whole snapshot.
            parents = {}
            for key, value in (("parent_session_id", task["session_id"]),
                               ("parent_agent_id", task.get("parent_agent_id"))):
                try:
                    parents[key] = None if value is None else identifier(value, key)
                except (TypeError, ValueError):
                    parents[key] = None
                    parents["parent_reference_omitted"] = True
            elapsed = view["elapsed_ms"]
            records.append({
                "host_id": self.host.host_id, "task_id": task["task_id"], "status": task["status"],
                "executor": safe_text(task["executor"], 240), "requested_model": safe_text(task.get("requested_model"), 240),
                "resolved_model": safe_text(task.get("resolved_model"), 240), "attempts": task["attempts"],
                **parents, "task": safe_text(view["task"], 240), "activity": safe_text(view["activity"], 240),
                "failure_reason": safe_text(task.get("failure_reason"), 500),
                "created_at": task["created_at"], "started_at": view["started_at"],
                # A clock step back can make a stored duration negative; never send that.
                "elapsed_ms": max(0, int(elapsed)) if elapsed is not None else None,
                "last_event_at": task["updated_at"], "cleanup_pending": view["cleanup_pending"],
                "cleanup_failed": task.get("cleanup_error") is not None,
                "shutdown_unconfirmed": view["shutdown_unconfirmed"],
                **({"work_item_id": task["work_item_id"]} if "work_item_id" in task else {}),
            })
        return records


def opencode_tracing_availability(environment: dict[str, str]) -> dict:
    """Tracing availability and reason for an OpenCode child launched with this environment."""
    available = langfuse_credentials_available(environment)
    return {"available": available,
            "reason": ("OpenCode plugin export and Router correlation verified via OpenTelemetry resource attributes"
                       if available else
                       "OpenCode route is verified, but required Langfuse credentials are unavailable")}


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
        self.control.sessions.reconcile_restart()
        self.control.start_lifecycle_writer()
        self.control.capacity.start()
        for task in self.store.all():
            if task["status"] == "closed" and task.get("cleanup_pending") and task.get("cleanup_error") is None:
                self.store.update(task["task_id"], lambda t: t.update(cleanup_requested=True))
            if self.control.settings.max_attempts is not None and task["status"] != "closed" and task["max_attempts"] != self.control.settings.max_attempts:
                self.store.update(task["task_id"], lambda t: t.update(max_attempts=self.control.settings.max_attempts))
            if task["status"] in {"validating", "preparing", "running", "cancelling"}:
                record = task.get("process")
                if record and (owned_state(record) == "live" or
                               (owned_state(record) == "stopped" and Path(record["completion"]).exists())):
                    self.spawn(task, recovered=True)
                elif task.get("cancel_requested") and (not record or owned_state(record) == "stopped"):
                    if record and record.get("interrupt_first"):
                        self.store.update_with_attempt(task["task_id"], lambda t: t.update(status="interrupted", execution_stopped=False,
                            shutdown_unconfirmed=True, failure_reason=f"{t['executor'].capitalize()} shutdown evidence missing after restart; operator inspection required"))
                    else:
                        self.store.update(task["task_id"], lambda t: t.update(status="cancelled", execution_stopped=True))
                else:
                    self.store.update_with_attempt(task["task_id"], lambda t: t.update(
                        status="interrupted", execution_stopped=False,
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
                        task["status"] == "cancelling" or task["status"] == "closed" and task.get("cleanup_requested") or
                        len(occupied) < (self.control.settings.concurrency_cap or 0))
                if not eligible:
                    continue
                if task["status"] in {"created", "retrying", "cancelling"}:
                    self.spawn(task)
                elif task["status"] == "closed" and task.get("cleanup_requested"):
                    self.store.update(task["task_id"], lambda t: t.update(cleanup_requested=False, cleanup_error=None))
                    self.spawn(task, cleaning=True)
            self.control.changed.wait(0.1)
            self.control.changed.clear()

    def retired(self, task_id: str) -> None:
        with self.lock:
            self.workers.pop(task_id, None)
        self.control.changed.set()

    def validate(self, task: dict) -> dict:
        invocation = task["invocation"]
        executor = invocation["executor"]
        if executor not in {"opencode", "codex", "claude"}:
            raise ValueError("Unsupported executor; no fallback")
        if not getattr(self.control.settings, executor + "_command"):
            raise ValueError(f"{executor} executor is not configured; no fallback")
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
        tracing = {}
        tracing_requested = "tracing" in configured
        if tracing_requested:
            if configured["tracing"] != "requested":
                raise ValueError("Repository tracing must be exactly \"requested\" when present")
            if executor in TRACING_UNAVAILABLE:
                # Recorded, never silently dropped: these children run with their
                # user/plugin configuration disabled, so credentials are stripped.
                tracing = {"tracing": {"requested": True, "available": False,
                                        "reason": TRACING_UNAVAILABLE[executor]}}
        if executor == "codex":
            sandbox = configured.get("codex_sandbox")
            if sandbox not in {"read-only", "workspace-write"}:
                raise ValueError("Project requires an explicit Codex sandbox; run router init --executor codex")
            if not model.startswith("openai/") or not model.removeprefix("openai/").strip():
                raise ValueError("Codex requires an exact openai/model ID; no provider fallback")
        credential_file = Path(self.control.environment.get("AGENT_ROUTER_ENV_FILE", "~/.agent-router/.env")).expanduser().resolve()
        if credential_file.is_relative_to(root):
            relative = str(credential_file.relative_to(root))
            if git(root, "ls-files", "--", relative).strip():
                raise ValueError("The provider env file must not be tracked")
            ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
                                     env=strip_langfuse_environment(os.environ), check=False)
            if ignored.returncode != 0:
                raise ValueError("The provider env file must be ignored when inside the repository")
        if executor == "claude":
            validate_command(self.control.settings.claude_command)
            validate_persistence(self.control.environment)
            try:
                permission = json.loads(configured.get("claude_permissions", "null"))
            except (ValueError, RecursionError) as error:
                raise ValueError("Project Claude permissions are not valid JSON") from error
            profile = validate_claude(model, permission)
            validated = {"executor": executor, "requested_model": invocation["model"], "resolved_model": model,
                         "setup_command": configured["setup_command"], "permission_profile": profile,
                         "child_session_id": str(uuid.uuid4()), **tracing}
            if "claude_tracing" in configured:
                validated.update(claude_tracing.configure(configured["claude_tracing"], {**task, **validated},
                                                          self.control.host.host_id, root,
                                                          langfuse_credentials_available(self.control.environment)))
            return validated
        if executor == "codex":
            return {"executor": executor, "requested_model": invocation["model"], "resolved_model": model,
                    "setup_command": configured["setup_command"], "permission_profile": {"sandbox": sandbox},
                    **price_metadata(model, self.control.settings.model_prices), **tracing}
        permission_file = root / "opencode.json"
        try:
            permission = json.loads(permission_file.read_text())["permission"]
        except (OSError, ValueError, KeyError) as error:
            raise ValueError("Repository requires opencode.json with an explicit non-interactive permission profile") from error
        def noninteractive(value):
            return all(noninteractive(v) for v in value.values()) if isinstance(value, dict) else value in {"allow", "deny"}
        if not isinstance(permission, dict) or not permission or not noninteractive(permission):
            raise ValueError("Permission profile must explicitly use allow/deny, never ask")
        child_session_id = "ses" + uuid.uuid4().hex
        if tracing_requested:
            tracing = {"tracing": {"requested": True, **opencode_tracing_availability(self.control.environment),
                                    "plugin": OPENCODE_LANGFUSE_PLUGIN,
                                    "correlation": {"session_id": child_session_id,
                                                     "router_task_id": task["task_id"],
                                                     "host_id": self.control.host.host_id}}}
        return {"executor": "opencode", "requested_model": invocation["model"], "resolved_model": model,
                "setup_command": configured["setup_command"], "permission_profile": permission,
                "child_session_id": child_session_id, **tracing}

    def phase(self, task_id: str, name: str, command: list[str], cwd: Path, *, collecting: bool = False) -> dict:
        task = self.store.get(task_id)
        phase_environment = self.control.environment.copy()
        tracing = task.get("tracing")
        requested = isinstance(tracing, dict) and tracing.get("requested") is True
        # The child registration below sets attempts to attempt_number. After that,
        # the tracing record is this launch's evidence: capture and recovery keep it.
        if requested and task["attempts"] != task["attempt_number"]:
            if not self._tracing_requested(task):
                # A task can outlive a repository config reload. Fail closed for
                # the next launch and remove the stale child opt-in.
                self.store.update(task_id, lambda current: current.pop("tracing", None))
            elif name == "child" and task["executor"] == "opencode":
                # Rejection/resume skips validate(); record what this child receives.
                availability = opencode_tracing_availability(phase_environment)
                self.store.update(task_id, lambda current: current["tracing"].update(availability))
            task = self.store.get(task_id)
        started = now_ms()
        directory = Path(task["artifact_dir"]) / f"attempt-{task['attempt_number']}" / name
        environment = strip_langfuse_environment(phase_environment)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            environment.pop(key, None)
        if "worktree" in task and name in {"setup", "child"}:
            environment = child_environment(
                phase_environment if name == "child" else environment,
                task,
                include_tracing_credentials=name == "child",
            )
        process = None
        def register(record):
            record["interrupt_first"] = name == "child" and task["executor"] in {"codex", "claude"}
            if record["interrupt_first"]:
                record["executor"] = task["executor"]
            if name == "version":
                record["disposable"] = True
                record["deadline_at"] = min(task["deadline_at"], started + 5000)
            def persist(current):
                if current.get("cancel_requested") and not collecting:
                    raise PhaseStopped("cancelled")
                if not collecting and now_ms() >= min(current["deadline_at"], record.get("deadline_at", current["deadline_at"])):
                    raise PhaseStopped("router-timeout")
                current.update(process=record, phase=name, phase_started_at=started, execution_stopped=False)
                if name == "child":
                    current["attempts"] = current["attempt_number"]
                    current["status"] = "running"
            self.store.update(task_id, persist)
        process, record = launch(command, cwd, environment, directory, register, disposable=name == "version")
        with self.lock:
            self.processes[task_id] = process
        try:
            return self.observe(task_id, record, process, collecting=collecting)
        finally:
            if process.poll() is not None:
                with self.lock:
                    self.processes.pop(task_id, None)

    def _tracing_requested(self, task: dict) -> bool:
        root = Path(task["invocation"]["parent_worktree"]).resolve()
        configured = self.control.settings.repositories.get(str(root))
        if not isinstance(configured, dict):
            return False
        if "_claude_tracing" in task:
            return "claude_tracing" in configured
        return configured.get("tracing") == "requested"

    def observe(self, task_id: str, record: dict, process=None, *, collecting: bool = False) -> dict:
        offset = self.store.get(task_id).get("event_offset", 0)
        while True:
            task = self.store.get(task_id)
            offset = self.events(task, record, offset)
            state = supervised_state(record, process)
            if state == "stopped":
                self.events(task, record, offset, final=True)
                completion = Path(record["completion"])
                verify_shutdown(record)
                if not completion.exists():
                    if task.get("cancel_requested"):
                        raise PhaseStopped("cancelled")
                    raise UncertainProcess("Execution stopped without reliable completion evidence")
                self.store.update(task_id, lambda t: t.update(execution_stopped=True))
                return json.loads(completion.read_text())
            if state != "live":
                raise UncertainProcess("Process creation identity or liveness is uncertain")
            cancel = task.get("cancel_requested") and (not collecting or task.get("cancel_requested_at", 0) > task["phase_started_at"])
            timeout = not collecting and now_ms() >= min(task["deadline_at"], record.get("deadline_at", task["deadline_at"]))
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
                except (ValueError, UnicodeError, RecursionError):
                    continue
            next_offset = stream.tell()
        if next_offset == offset:
            return offset
        def update(current):
            current["event_offset"] = next_offset
            if values:
                current["last_event_at"] = now_ms()
            for event in values:
                if current["executor"] == "claude":
                    collect_claude(current, event)
                    continue
                if current["executor"] == "codex":
                    collect_codex(current, event)
                    continue
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
                    if current["executor"] == "claude":
                        current["claude_baseline"] = current.get("claude_total", {"schema_version": 1, "models": {},
                            "complete": not current["attempts"], "cost_complete": not current["attempts"]})
                        current.pop("executor_version", None)
                        current.pop("claude_executable", None)
                    if current["executor"] == "codex":
                        current["codex_usage_baseline"] = current.get("codex_usage_total", {})
                        current["codex_baseline_complete"] = not current["attempts"] or current.get("usage_complete", False)
                    current.update(status="preparing", attempt_number=current["attempts"] + 1,
                                   attempt_started_at=now_ms(), deadline_at=now_ms() + int(self.control.settings.attempt_timeout_seconds * 1000),
                                   artifact_dir=str(self.control.directory / "tasks" / task_id),
                                   max_attempts=self.control.settings.max_attempts)
                    for key in ("final_tree", "patch", "execution_outcome", "failure_reason", "exit_code", "last_event_at", "summary", "cost", "usage", "usage_complete", "cost_note", "cost_source", "executor_completed", "thread_observed", "executor_activity", "execution_error", "event_offset", "process", "phase", "phase_started_at"):
                        current.pop(key, None)
                self.store.update(task_id, begin)
                if task["executor"] == "claude":
                    executable = resolve_executable(self.control.settings.claude_command, self.control.environment,
                                                    Path(task["invocation"]["parent_worktree"]))
                    self.store.update(task_id, lambda t: t.update(claude_executable=executable))
            else:
                task = self.store.get(task_id)
                phase = task["phase"]
                result = self.observe(task_id, task["process"], collecting=phase == "capture")
                if result["exit_code"] and phase not in {"child", "version"}:
                    raise ValueError(f"Recovered {phase} failed with exit code {result['exit_code']}")
                if phase == "version":
                    version = claude_version(Path(task["process"]["stdout"]), result["exit_code"])
                    self.store.update(task_id, lambda t: t.update(executor_version=version))
                if phase in {"prepare", "capture"}:
                    result_file = Path(task["artifact_dir"]) / f"{phase}-{task['attempt_number']}.result.json"
                    self.store.update(task_id, lambda t: t.update(json.loads(result_file.read_text())))
                if phase == "setup":
                    self.store.update(task_id, lambda t: t.update(setup_done=True))
                if phase in {"child", "capture"}:
                    if phase == "child":
                        self.store.update(task_id, lambda t: t.update(exit_code=result["exit_code"]))
                    task = self.store.get(task_id)
                    failed = self.child_failed(task) or (phase == "child" and result["exit_code"] != 0)
                    outcome = task.get("pending_outcome", "failed" if failed else "completed")
                    reason = task.get("pending_reason", "Executor failed" if failed else None)
                    self.finish(task_id, outcome, reason, already_captured=phase == "capture")
                    return
            task = self.store.get(task_id)
            if task["executor"] == "claude":
                executable = task.get("claude_executable")
                if not isinstance(executable, str) or not Path(executable).is_absolute() or recovered and not task.get("executor_version"):
                    raise ValueError("Claude attempt lacks its inspected executable/version binding; refusing recovered launch")
            if task["executor"] == "claude" and not task.get("executor_version"):
                result = self.phase(task_id, "version", [task["claude_executable"], "--version"],
                                    Path(task["invocation"]["parent_worktree"]))
                task = self.store.get(task_id)
                version = claude_version(Path(task["process"]["stdout"]), result["exit_code"])
                self.store.update(task_id, lambda t: t.update(executor_version=version))
            if "snapshot_commit" not in task:
                self.store.update(task_id, lambda t: t.update(worktree=str(Path(t["artifact_dir"]) / "worktree"), branch=f"agent/{task_id}"))
                self.snapshot_phase(task_id, "prepare")
            task = self.store.get(task_id)
            if not task.get("setup_done"):
                result = self.phase(task_id, "setup", ["/bin/sh", "-c", task["setup_command"]], Path(task["worktree"]))
                if result["exit_code"]:
                    self.store.update(task_id, lambda t: t.update(exit_code=result["exit_code"]))
                    raise ValueError("Dependency setup failed; child was not launched")
                self.store.update(task_id, lambda t: t.update(setup_done=True))
            task = self.store.get(task_id)
            result = self.phase(task_id, "child", executor_command(self.control.settings, task), Path(task["worktree"]))
            self.store.update(task_id, lambda t: t.update(exit_code=result["exit_code"]))
            if result["exit_code"] or self.child_failed(self.store.get(task_id)):
                outcome, reason = "failed", "Executor failed; inspect stdout/stderr artifacts"
        except PhaseStopped as stopped:
            outcome = "cancelled" if stopped.reason == "cancelled" else "failed"
            reason = stopped.reason
        except UncertainProcess as error:
            message = str(error)
            unconfirmed = isinstance(error, UnconfirmedShutdown)
            self.store.update_with_attempt(task_id, lambda t: t.update(
                status="interrupted", execution_stopped=False, failure_reason=message,
                shutdown_unconfirmed=unconfirmed or t.get("shutdown_unconfirmed", False)))
            return
        except Exception as error:
            LOGGER.exception("Task phase failed: %s", task_id)
            task = self.store.get(task_id)
            if not task["execution_stopped"]:
                try:
                    with self.lock:
                        process = self.processes.get(task_id)
                    stop_owned(task["process"], process)
                    with self.lock:
                        self.processes.pop(task_id, None)
                    self.store.update(task_id, lambda t: t.update(execution_stopped=True))
                except UncertainProcess as stopping_error:
                    message = str(stopping_error)
                    unconfirmed = isinstance(stopping_error, UnconfirmedShutdown)
                    self.store.update_with_attempt(task_id, lambda t: t.update(status="interrupted", execution_stopped=False,
                        failure_reason=message, shutdown_unconfirmed=unconfirmed or t.get("shutdown_unconfirmed", False)))
                    return
            outcome, reason = "failed", str(error)
        self.finish(task_id, outcome, reason)

    @staticmethod
    def child_failed(task: dict) -> bool:
        return bool(task.get("execution_error") or task["executor"] in {"codex", "claude"} and
                    (not task.get("executor_completed") or not task.get("thread_observed")))

    def finish(self, task_id: str, outcome: str, reason: str | None, already_captured: bool = False) -> None:
        task = self.store.get(task_id)
        self.store.update(task_id, lambda t: t.update(pending_outcome=outcome, pending_reason=reason))
        if task.get("snapshot_commit") and Path(task["worktree"]).exists() and not already_captured:
            try:
                self.snapshot_phase(task_id, "capture")
            except UncertainProcess as error:
                message = str(error)
                self.store.update_with_attempt(task_id, lambda t: t.update(
                    status="interrupted", execution_stopped=False, failure_reason=message))
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
        self.store.update_with_attempt(task_id, finalize)

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
        self.control.stop_lifecycle_writer()
        self.control.capacity.stop()
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
                if not isinstance(payload, dict) or set(payload) - {"only_if_idle"}:
                    raise ValueError("Invalid daemon stop options")
                with self.server.control.lifecycle_lock:
                    if payload.get("only_if_idle") and self.server.control.busy():
                        raise ValueError("Finish or explicitly cancel active Router tasks before maintenance")
                    self.server.control.draining = True
                result = {"stopping": True}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                result = self.server.control.handle(operation, payload)
            response = {"ok": True, "result": result}
        except sqlite3.OperationalError as error:
            if operation in {"completion_next", "completion_ack", "completion_subscribe",
                             "completion_consumer_list", "completion_consumer_remove"} and \
                    getattr(error, "sqlite_errorcode", 0) & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                response = {"ok": False, "error": "Completion database is busy; retry the same delivery request",
                            "retryable": True}
            else:
                LOGGER.exception("Control request database failure")
                response = {"ok": False, "error": "Router database is unavailable; inspect the operator log"}
        except CompletionRefused as error:
            response = {"ok": False, "error": str(error), "code": "acknowledgment_refused"}
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
    # Reject unsafe state roots before changing permissions or creating the lock.
    require_state_directory(settings, directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "daemon.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Agent Router is already running") from error
        controller = Controller(settings, environment, directory)
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
