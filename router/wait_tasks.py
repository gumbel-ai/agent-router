"""Block until Router tasks leave their active states, by polling read-only task status."""

import time
from collections.abc import Callable

from router.client import UnknownTask
from router.client import request as router_request
from router.daemon import ACTIVE

POLL_SECONDS = 1
MAX_WAIT_SECONDS = 3600
MAX_TASKS = 100


def _snapshot(task_id: str, task: dict | None) -> dict:
    if task is None:
        return {"task_id": task_id, "status": "missing", "ready": True}
    result = {"task_id": task_id, "status": task["status"], "attempts": task.get("attempts"),
              "ready": task["status"] not in ACTIVE or task.get("cleanup_error") is not None}
    for key in ("failure_reason", "cleanup_error"):
        if task.get(key) is not None:
            result[key] = task[key]
    return result


def _poll(task_ids: list[str], request: Callable, deadline: float, clock: Callable[[], float]) -> list[dict] | None:
    """One status read per task; None when the daemon could not be reached."""
    snapshots = []
    for task_id in task_ids:
        try:
            task = request("status", {"task_id": task_id}, timeout=max(.1, min(2, deadline - clock())), deadline=True)
        except UnknownTask:
            task = None
        except (OSError, ValueError):
            # Unreachable, restarting or briefly failing daemon: keep the previous states.
            return None
        snapshots.append(_snapshot(task_id, task))
    return snapshots


def wait_tasks(task_ids: list[str], mode: str, timeout: float, *, request: Callable | None = None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """Return when any or all tasks are ready, or at the deadline. Never mutates tasks."""
    request = request or router_request
    task_ids = list(dict.fromkeys(task_ids))
    if not 1 <= len(task_ids) <= MAX_TASKS:
        raise ValueError(f"Give 1 to {MAX_TASKS} task IDs")
    if any(not task_id or task_id.startswith("native-") for task_id in task_ids):
        raise ValueError("wait-tasks accepts Router task IDs only")
    if mode not in {"any", "all"}:
        raise ValueError("mode must be any or all")
    if not 0 < timeout <= MAX_WAIT_SECONDS:
        raise ValueError(f"--timeout must be greater than 0 and at most {MAX_WAIT_SECONDS}")
    deadline = clock() + timeout
    latest = [{"task_id": task_id, "status": "unknown", "ready": False} for task_id in task_ids]
    while True:
        snapshots = _poll(task_ids, request, deadline, clock)
        if snapshots is not None:
            latest = snapshots
            ready = [task["ready"] for task in latest]
            if any(ready) if mode == "any" else all(ready):
                return {"mode": mode, "condition_met": True, "timed_out": False, "daemon_unavailable": False,
                        "tasks": latest}
        if clock() + POLL_SECONDS > deadline:
            return {"mode": mode, "condition_met": False, "timed_out": True,
                    "daemon_unavailable": snapshots is None, "tasks": latest}
        sleep(POLL_SECONDS)
