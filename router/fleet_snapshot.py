"""Bounded, read-only fleet snapshot composition for one host."""

import hashlib
import json
import sqlite3
import threading

from router.capacity import GIB, RESERVATION_FIELDS, CapacityThresholds, classify
from router.db import now_ms
from router.dispatch import canonical
from router.sanitize import bound_text, sanitize_structure
from router.validation import is_wire_integer

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_ALERTS = 100


def _alert(host_id: str, kind: str, severity: str, evidence: str, entity: dict | None = None,
           first_seen_at: int | None = None, observed_at: int | None = None) -> dict:
    now = now_ms() if observed_at is None else observed_at
    first_seen = now if first_seen_at is None else min(first_seen_at, now)
    identity = canonical(entity or {})
    alert_id = hashlib.sha256(f"{host_id}\0{kind}\0{identity}".encode()).hexdigest()[:24]
    return {"alert_id": alert_id, "host_id": host_id, "severity": severity, "kind": kind,
            "first_seen_at": first_seen, "last_seen_at": now, "entity": entity or {}, "evidence": evidence}


CAPACITY_ALERT_KINDS = frozenset({"host_saturated", "memory_low", "disk_low"})
CAPACITY_DEPENDENCY_SECTIONS = frozenset({
    "session_active_count", "agent_active_count", "managed_process_active_count",
    "lifecycle", "lifecycle_consistency",
})


# Free text written by users or agents; withheld when fleet text sharing is off.
FLEET_TEXT_FIELDS = frozenset({"task", "subject", "failure_reason"})

class AlertHistory:
    """Preserve first-seen time while an alert remains continuously active."""

    def __init__(self):
        self._lock = threading.Lock()
        self._first_seen: dict[tuple[str | None, str], int] = {}

    def track(self, current: list[dict], scopes: list[str] | None = None) -> list[dict]:
        keys = [(scopes[index] if scopes else None, alert["alert_id"]) for index, alert in enumerate(current)]
        active = set(keys)
        with self._lock:
            for key, alert in zip(keys, current, strict=True):
                first = self._first_seen.setdefault(key, alert["first_seen_at"])
                last_seen = alert.get("last_seen_at", alert["first_seen_at"])
                if last_seen < first:
                    # A wall-clock rollback begins a new representable alert epoch.
                    first = last_seen
                    self._first_seen[key] = first
                alert["first_seen_at"] = first
            self._first_seen = {key: value for key, value in self._first_seen.items() if key in active}
        return current


def capacity_alerts(host: dict, capacity: dict, thresholds: CapacityThresholds,
                    observed_at: int | None = None) -> list[dict]:
    result = []
    host_id = host["host_id"]
    if capacity.get("capacity_state") == "saturated":
        result.append(_alert(host_id, "host_saturated", "warning", "; ".join(capacity.get("capacity_reasons", [])),
                             first_seen_at=capacity.get("observed_at"), observed_at=observed_at))
    memory_available = capacity.get("memory_available_bytes")
    if isinstance(memory_available, (int, float)) and memory_available < thresholds.minimum_available_memory_gib * GIB:
        result.append(_alert(host_id, "memory_low", "warning",
                             f"Available memory is below {thresholds.minimum_available_memory_gib:g} GiB",
                             first_seen_at=capacity.get("observed_at"), observed_at=observed_at))
    disk_available = capacity.get("disk_available_bytes")
    if isinstance(disk_available, (int, float)) and disk_available < thresholds.minimum_disk_available_gib * GIB:
        result.append(_alert(host_id, "disk_low", "warning",
                             f"Available disk is below {thresholds.minimum_disk_available_gib:g} GiB",
                             first_seen_at=capacity.get("observed_at"), observed_at=observed_at))
    return result


def alerts(host: dict, capacity: dict, sessions: list[dict], agents: list[dict],
           thresholds: CapacityThresholds, limit: int,
           observed_at: int | None = None) -> list[dict]:
    result = capacity_alerts(host, capacity, thresholds, observed_at)
    if len(result) >= limit:
        return result[:limit]
    host_id = host["host_id"]
    for record in sessions + agents:
        entity = {key: record[key] for key in ("source", "session_id", "parent_session_id", "agent_id") if record.get(key)}
        if record.get("needs_input") or record.get("status") == "needs_input":
            result.append(_alert(host_id, "agent_needs_input", "warning", record.get("visibility_reason") or "Agent needs input",
                                 entity, record.get("last_event_at"), observed_at))
            if len(result) >= limit:
                return result
        if record.get("status") == "unknown":
            result.append(_alert(host_id, "agent_visibility_unknown", "warning", record.get("visibility_reason") or "Lifecycle visibility is uncertain",
                                 entity, record.get("last_event_at"), observed_at))
            if len(result) >= limit:
                return result
    return result


def _section(snapshot: dict, key: str, producer, fallback) -> None:
    try:
        value = producer()
        snapshot[key] = value
        if isinstance(value, dict) and value.get("partial") is True:
            snapshot["partial"] = True
            if value.get("error"):
                snapshot.setdefault("errors", {})[key] = bound_text(str(value["error"]), 500)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        snapshot[key] = fallback
        snapshot["partial"] = True
        snapshot.setdefault("errors", {})[key] = bound_text(str(error), 500)


def _collection(snapshot: dict, key: str, producer, limit: int,
                limit_name: str = "history_limit") -> None:
    _section(snapshot, key, lambda: producer(limit + 1), [])
    if len(snapshot[key]) > limit:
        snapshot[key] = snapshot[key][:limit]
        snapshot["partial"] = True
        snapshot.setdefault("errors", {})[key] = (
            f"Collection truncated to {limit_name}={limit}"
        )


def _observed_collection(snapshot: dict, key: str, producer, limit: int,
                         count_error_key: str) -> int | None:
    try:
        records, active = producer(limit + 1)
        if not isinstance(records, list):
            raise TypeError("collection must be a list")
        if active is not None and not is_wire_integer(active):
            raise ValueError("active count must be a nonnegative 64-bit integer")
        snapshot[key] = records[:limit]
        if len(records) > limit:
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})[key] = (
                f"Collection truncated to history_limit={limit}"
            )
        if active is None:
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})[count_error_key] = "Exact active count is unavailable"
        return active
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        snapshot[key] = []
        snapshot["partial"] = True
        snapshot.setdefault("errors", {})[key] = bound_text(str(error), 500)
        snapshot.setdefault("errors", {})[count_error_key] = "Exact active count is unavailable"
        return None


def _invalidate_partial_capacity(snapshot: dict) -> None:
    failed = sorted(CAPACITY_DEPENDENCY_SECTIONS & snapshot.get("errors", {}).keys())
    if not failed:
        return
    capacity = snapshot.get("capacity")
    if not isinstance(capacity, dict):
        return
    reason = "active workload evidence is partial: " + ", ".join(failed)
    existing = capacity.get("capacity_reasons")
    reasons = list(existing) if isinstance(existing, list) else []
    if reason not in reasons:
        reasons.append(reason)
    capacity.update(partial=True, capacity_state="unknown", capacity_reasons=reasons)
    if isinstance(snapshot.get("alerts"), list):
        snapshot["alerts"] = [
            alert for alert in snapshot["alerts"]
            if alert.get("kind") not in CAPACITY_ALERT_KINDS
        ]


def compose(controller, thresholds: CapacityThresholds | None = None, history_limit: int = 100) -> dict:
    thresholds = thresholds or CapacityThresholds()
    snapshot = {"schema_version": SCHEMA_VERSION, "partial": False, "generated_at": 0}
    lifecycle_consistency = (controller.lifecycle_consistency()
                             if hasattr(controller, "lifecycle_consistency") else None)
    lifecycle_generation = (controller.lifecycle_generation()
                            if lifecycle_consistency is None and hasattr(controller, "lifecycle_generation") else None)
    active_sessions = _observed_collection(
        snapshot, "sessions", controller.sessions.fleet_records, history_limit,
        "session_active_count",
    )
    active_agents = _observed_collection(
        snapshot, "agents", controller.native.fleet_records, history_limit,
        "agent_active_count",
    )
    _collection(snapshot, "tasks", controller.sessions.tasks, history_limit)
    if hasattr(controller, "router_task_records"):
        _collection(snapshot, "router_tasks", controller.router_task_records, history_limit)
        if not snapshot["router_tasks"] and "router_tasks" not in snapshot.get("errors", {}):
            # Older fleet viewers treat unknown sections as partial; send it only when there is something to show.
            del snapshot["router_tasks"]
    if not getattr(getattr(controller, "settings", None), "fleet_share_text", True):
        # The producing host decides whether user- and agent-written text leaves it.
        for key in ("agents", "tasks", "router_tasks"):
            for record in snapshot.get(key, []):
                for field in FLEET_TEXT_FIELDS & record.keys():
                    record[field] = None
    sessions = snapshot["sessions"]
    agents = snapshot["agents"]
    _section(snapshot, "capacity", lambda: controller.capacity.view(
        thresholds,
        active_claude_sessions=active_sessions,
        active_agents=active_agents,
        active_managed_processes=0,
    ), {})
    snapshot["managed_processes"] = []
    if hasattr(controller, "inferred_process_records"):
        _collection(snapshot, "inferred_processes", controller.inferred_process_records, history_limit)
    else:
        snapshot["inferred_processes"] = []
    if hasattr(controller, "lifecycle_health"):
        lifecycle_error = controller.lifecycle_health()
        if lifecycle_error:
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})["lifecycle"] = lifecycle_error
    if lifecycle_consistency is not None:
        current_consistency = controller.lifecycle_consistency()
        if (lifecycle_consistency[1] or current_consistency[1]
                or current_consistency[0] != lifecycle_consistency[0]):
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})["lifecycle_consistency"] = (
                "Lifecycle observations changed during snapshot composition"
            )
    elif lifecycle_generation is not None:
        current_generation = controller.lifecycle_generation()
        if lifecycle_generation % 2 or current_generation != lifecycle_generation:
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})["lifecycle_consistency"] = (
                "Lifecycle observations changed during snapshot composition"
            )
    _invalidate_partial_capacity(snapshot)
    generated_at = now_ms()
    snapshot["generated_at"] = generated_at
    _section(snapshot, "host", lambda: controller.host.view(generated_at), {})
    capacity = snapshot.get("capacity")
    if isinstance(capacity, dict):
        observed_at = capacity.get("observed_at")
        sample_age_ms = capacity.get("sample_age_ms")
        if (is_wire_integer(observed_at) and observed_at <= generated_at
                and is_wire_integer(sample_age_ms)):
            # The sample was aged before the remaining sections were composed.
            # Bring that age through the final observation boundary so the
            # emitted chronology remains internally consistent.
            capacity["sample_age_ms"] = max(sample_age_ms, generated_at - observed_at)
        reservation_values = [capacity.get(field) for field in RESERVATION_FIELDS]
        if all(value is not None for value in reservation_values):
            capacity["reservation_active"] = capacity["reserved_until"] > generated_at
        interval_seconds = getattr(controller.capacity, "interval_seconds", 5)
        state, reasons = classify(capacity, thresholds, interval_seconds * 3000)
        capacity.update(capacity_state=state, capacity_reasons=reasons)
    if any(key in snapshot.get("errors", {}) for key in ("sessions", "agents")):
        # Alert-worthy records sort first, but a truncated or failed page can
        # still omit some; say so rather than implying the alert list is whole.
        snapshot["partial"] = True
        snapshot["errors"]["alerts"] = "Alerts derived from incomplete session or agent records"
    _collection(snapshot, "alerts", lambda limit: alerts(
        snapshot["host"], snapshot["capacity"], sessions, agents, thresholds, limit,
        generated_at), MAX_ALERTS, "alert_limit")
    if hasattr(controller, "alert_history"):
        snapshot["alerts"] = controller.alert_history.track(snapshot["alerts"])
    return bound(snapshot)


def bound(snapshot: dict, maximum_bytes: int = MAX_SNAPSHOT_BYTES) -> dict:
    sanitized = sanitize_structure(snapshot)
    if sanitized != snapshot:
        sanitized["partial"] = True
        sanitized.setdefault("errors", {})["output"] = (
            "Snapshot values had terminal controls removed at the output boundary"
        )
    snapshot = sanitized
    if len(canonical(snapshot).encode()) <= maximum_bytes:
        return snapshot
    result = dict(snapshot)
    result["partial"] = True
    result.setdefault("errors", {})["output"] = "Snapshot truncated to configured one MiB bound"
    for key in ("tasks", "router_tasks", "agents", "sessions", "alerts"):
        values = list(result.get(key, []))
        while values and len(canonical({**result, key: values}).encode()) > maximum_bytes:
            values = values[:len(values) // 2]
        result[key] = values
    # Fixed metadata should remain far below the limit; guard malformed producers.
    if len(json.dumps(result, separators=(",", ":")).encode()) > maximum_bytes:
        raise ValueError("Snapshot fixed metadata exceeds one MiB")
    return result
