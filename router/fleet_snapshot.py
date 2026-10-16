"""Bounded, read-only fleet snapshot composition for one host."""

import hashlib
import json
import sqlite3
import threading

from router.capacity import GIB, CapacityThresholds
from router.db import now_ms
from router.dispatch import canonical

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 1024 * 1024


def _alert(host_id: str, kind: str, severity: str, evidence: str, entity: dict | None = None,
           first_seen_at: int | None = None) -> dict:
    now = now_ms()
    identity = canonical(entity or {})
    alert_id = hashlib.sha256(f"{host_id}\0{kind}\0{identity}".encode()).hexdigest()[:24]
    return {"alert_id": alert_id, "host_id": host_id, "severity": severity, "kind": kind,
            "first_seen_at": first_seen_at or now, "last_seen_at": now, "entity": entity or {}, "evidence": evidence}


CAPACITY_ALERT_KINDS = frozenset({"host_saturated", "memory_low", "disk_low"})


class AlertHistory:
    """Preserve first-seen time while an alert remains continuously active."""

    def __init__(self):
        self._lock = threading.Lock()
        self._first_seen: dict[str, int] = {}

    def track(self, current: list[dict]) -> list[dict]:
        active = {alert["alert_id"] for alert in current}
        with self._lock:
            for alert in current:
                first = self._first_seen.setdefault(alert["alert_id"], alert["first_seen_at"])
                alert["first_seen_at"] = first
            self._first_seen = {key: value for key, value in self._first_seen.items() if key in active}
        return current


def capacity_alerts(host: dict, capacity: dict, thresholds: CapacityThresholds) -> list[dict]:
    result = []
    host_id = host["host_id"]
    if capacity.get("capacity_state") == "saturated":
        result.append(_alert(host_id, "host_saturated", "warning", "; ".join(capacity.get("capacity_reasons", [])),
                             first_seen_at=capacity.get("observed_at")))
    memory_available = capacity.get("memory_available_bytes")
    if isinstance(memory_available, (int, float)) and memory_available < thresholds.minimum_available_memory_gib * GIB:
        result.append(_alert(host_id, "memory_low", "warning",
                             f"Available memory is below {thresholds.minimum_available_memory_gib:g} GiB",
                             first_seen_at=capacity.get("observed_at")))
    disk_available = capacity.get("disk_available_bytes")
    if isinstance(disk_available, (int, float)) and disk_available < thresholds.minimum_disk_available_gib * GIB:
        result.append(_alert(host_id, "disk_low", "warning",
                             f"Available disk is below {thresholds.minimum_disk_available_gib:g} GiB",
                             first_seen_at=capacity.get("observed_at")))
    return result


def alerts(host: dict, capacity: dict, sessions: list[dict], agents: list[dict],
           thresholds: CapacityThresholds) -> list[dict]:
    result = capacity_alerts(host, capacity, thresholds)
    host_id = host["host_id"]
    for record in sessions + agents:
        entity = {key: record[key] for key in ("source", "session_id", "parent_session_id", "agent_id") if record.get(key)}
        if record.get("needs_input") or record.get("status") == "needs_input":
            result.append(_alert(host_id, "agent_needs_input", "warning", record.get("visibility_reason") or "Agent needs input",
                                 entity, record.get("last_event_at")))
        if record.get("status") == "unknown":
            result.append(_alert(host_id, "agent_visibility_unknown", "warning", record.get("visibility_reason") or "Lifecycle visibility is uncertain",
                                 entity, record.get("last_event_at")))
    return result[:100]


def _section(snapshot: dict, key: str, producer, fallback) -> None:
    try:
        value = producer()
        snapshot[key] = value
        if isinstance(value, dict) and value.get("partial") is True:
            snapshot["partial"] = True
            if value.get("error"):
                snapshot.setdefault("errors", {})[key] = str(value["error"])[:500]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        snapshot[key] = fallback
        snapshot["partial"] = True
        snapshot.setdefault("errors", {})[key] = str(error)[:500]


def compose(controller, thresholds: CapacityThresholds | None = None, history_limit: int = 100) -> dict:
    generated_at = now_ms()
    thresholds = thresholds or CapacityThresholds()
    snapshot = {"schema_version": SCHEMA_VERSION, "partial": False, "generated_at": generated_at}
    _section(snapshot, "host", lambda: controller.host.view(generated_at), {})
    _section(snapshot, "sessions", lambda: controller.sessions.sessions(history_limit), [])
    _section(snapshot, "agents", lambda: controller.native.snapshot(history_limit), [])
    _section(snapshot, "tasks", lambda: controller.sessions.tasks(history_limit), [])
    sessions = snapshot["sessions"]
    agents = snapshot["agents"]
    _section(snapshot, "capacity", lambda: controller.capacity.view(
        thresholds,
        active_claude_sessions=sum(
            item.get("status") == "running" or item.get("needs_input") is True for item in sessions),
        active_agents=sum(item.get("status") in {"running", "needs_input"} for item in agents),
        active_managed_processes=0,
    ), {})
    snapshot["managed_processes"] = []
    snapshot["inferred_processes"] = []
    _section(snapshot, "alerts", lambda: alerts(
        snapshot["host"], snapshot["capacity"], sessions, agents, thresholds), [])
    if hasattr(controller, "alert_history"):
        snapshot["alerts"] = controller.alert_history.track(snapshot["alerts"])
    if hasattr(controller, "lifecycle_health"):
        lifecycle_error = controller.lifecycle_health()
        if lifecycle_error:
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})["lifecycle"] = lifecycle_error
    return bound(snapshot)


def bound(snapshot: dict, maximum_bytes: int = MAX_SNAPSHOT_BYTES) -> dict:
    if len(canonical(snapshot).encode()) <= maximum_bytes:
        return snapshot
    result = dict(snapshot)
    result["partial"] = True
    result.setdefault("errors", {})["output"] = "Snapshot truncated to configured one MiB bound"
    for key in ("tasks", "agents", "sessions", "alerts"):
        values = list(result.get(key, []))
        while values and len(canonical({**result, key: values}).encode()) > maximum_bytes:
            values = values[:len(values) // 2]
        result[key] = values
    # Fixed metadata should remain far below the limit; guard malformed producers.
    if len(json.dumps(result, separators=(",", ":")).encode()) > maximum_bytes:
        raise ValueError("Snapshot fixed metadata exceeds one MiB")
    return result
