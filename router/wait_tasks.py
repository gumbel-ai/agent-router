"""Block until Router tasks leave their active states, by polling read-only task status."""

import time
from collections.abc import Callable

from router.client import UnknownTask
from router.client import request as router_request
from router.daemon import ACTIVE
from router.managed import _identifier

POLL_SECONDS = 1
REQUEST_SECONDS = 2
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


def _poll(latest: dict[str, dict], request: Callable, deadline: float, clock: Callable[[], float]) -> bool | None:
    """Refresh each task's latest state in place: True when every read succeeded, False when
    Router could not be read, None when the deadline left no time to finish the round."""
    for task_id in latest:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        try:
            task = request("status", {"task_id": task_id}, timeout=min(REQUEST_SECONDS, remaining), deadline=True)
        except UnknownTask:
            task = None
        except (OSError, ValueError):
            # Unreachable, restarting or briefly failing Router: keep the states already read.
            return False
        latest[task_id] = _snapshot(task_id, task)
    return True


def wait_tasks(task_ids: list[str], mode: str, timeout: float, *, request: Callable | None = None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """Return when any or all tasks are ready, or at the deadline. Never mutates tasks."""
    request = request or router_request
    task_ids = list(dict.fromkeys(task_ids))
    if not 1 <= len(task_ids) <= MAX_TASKS:
        raise ValueError(f"Give 1 to {MAX_TASKS} task IDs")
    for task_id in task_ids:
        if _identifier(task_id, "task").startswith("native-"):
            raise ValueError("wait-tasks accepts Router task IDs only")
    if mode not in {"any", "all"}:
        raise ValueError("mode must be any or all")
    if not 0 < timeout <= MAX_WAIT_SECONDS:
        raise ValueError(f"--timeout must be greater than 0 and at most {MAX_WAIT_SECONDS}")
    deadline = clock() + timeout
    latest = {task_id: {"task_id": task_id, "status": "unknown", "ready": False} for task_id in task_ids}
    available = True
    while True:
        polled = _poll(latest, request, deadline, clock)
        available = available if polled is None else polled
        ready = [task["ready"] for task in latest.values()]
        met = any(ready) if mode == "any" else all(ready)
        remaining = deadline - clock()
        if met or remaining <= 0:
            return {"mode": mode, "condition_met": met, "timed_out": not met,
                    "daemon_unavailable": not met and not available, "tasks": list(latest.values())}
        sleep(min(POLL_SECONDS, remaining))
