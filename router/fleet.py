"""Fleet configuration, bounded local/SSH queries, and merged read-only views."""

import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import time
import tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields
from pathlib import Path

from router.capacity import CapacityThresholds, classify
from router.fleet_snapshot import (
    CAPACITY_ALERT_KINDS,
    MAX_SNAPSHOT_BYTES,
    SCHEMA_VERSION,
    AlertHistory,
    capacity_alerts,
)
from router.sanitize import bound_text, sanitize_output_text
from router.validation import (
    HOST_STRING_LIMITS,
    MAX_HOST_DISPLAY_LENGTH,
    MAX_HOST_ID_LENGTH,
    MAX_WIRE_INTEGER,
    is_wire_integer,
)

DEFAULT_CONFIG = Path("~/.agent-router/fleet.toml").expanduser()
MAX_HOSTS = 32
MAX_COLLECTION_RECORDS = 1000
MAX_IDENTITY_LENGTH = MAX_HOST_DISPLAY_LENGTH
MAX_FLEET_CONFIG_BYTES = 64 * 1024
MAX_FLEET_NAME_LENGTH = 120
MAX_FLEET_TARGET_LENGTH = 512
MAX_HOST_TAGS = 32
MAX_HOST_TAG_LENGTH = 120
MAX_FLEET_OUTPUT_BYTES = MAX_HOSTS * (MAX_SNAPSHOT_BYTES + 16 * 1024) + 4096
MINIMUM_CLAUDE_VERSION = (2, 1, 292)
_THRESHOLD_NAMES = {item.name for item in fields(CapacityThresholds)}
_REQUIRED_CAPACITY_FIELDS = (
    "logical_cpus", "load_1m", "load_5m", "cpu_busy_pct", "memory_total_bytes",
    "memory_available_bytes", "swap_used_bytes", "disk_available_bytes", "observed_at",
    "sample_age_ms", "active_claude_sessions", "active_agents", "active_managed_processes",
)
_CAPACITY_DEPENDENCY_SECTIONS = frozenset({
    "session_active_count", "agent_active_count", "managed_process_active_count",
    "lifecycle", "lifecycle_consistency",
})
MIN_SSH_TIMEOUT_SECONDS = 0.1
MAX_SSH_TIMEOUT_SECONDS = 60.0
MIN_REFRESH_SECONDS = 0.1
MAX_REFRESH_SECONDS = 3600.0


@dataclass(frozen=True)
class FleetHost:
    name: str
    transport: str
    target: str | None = None
    tags: tuple[str, ...] = ()
    required: bool = True
    thresholds: CapacityThresholds = field(default_factory=CapacityThresholds)


@dataclass(frozen=True)
class FleetConfig:
    hosts: tuple[FleetHost, ...]
    ssh_timeout_seconds: float = 3
    refresh_seconds: float = 2


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positive_number(value, name: str) -> float:
    if not _is_finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _bounded_positive_number(value: object, name: str, minimum: float,
                             maximum: float) -> float:
    number = _positive_number(value, name)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g} seconds")
    return number


def _threshold_number(value: object, name: str) -> float:
    number = _positive_number(value, name)
    if name in {
        "maximum_cpu_busy_for_available_pct",
        "maximum_cpu_busy_for_saturated_pct",
    } and number > 100:
        raise ValueError(f"{name} must be at most 100")
    return number


def _config_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string up to {maximum} characters")
    if value != value.strip() or sanitize_output_text(value) != value:
        raise ValueError(f"{name} contains unsafe output text")
    return value


def load_fleet_config(path: Path = DEFAULT_CONFIG) -> FleetConfig:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_FLEET_CONFIG_BYTES + 1)
        if len(payload) > MAX_FLEET_CONFIG_BYTES:
            raise ValueError(f"fleet.toml exceeds {MAX_FLEET_CONFIG_BYTES // 1024} KiB")
        values = tomllib.loads(payload.decode())
    except OSError as error:
        raise ValueError(f"Cannot read fleet configuration {path}: {error}") from error
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Invalid fleet TOML: {error}") from error
    allowed = {"schema_version", "ssh_timeout_seconds", "refresh_seconds", "capacity_defaults", "host"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("Unknown fleet configuration fields: " + ", ".join(sorted(unknown)))
    schema_version = values.get("schema_version")
    if (not isinstance(schema_version, int) or isinstance(schema_version, bool)
            or schema_version != 1):
        raise ValueError("fleet.toml requires schema_version = 1")
    defaults = values.get("capacity_defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - _THRESHOLD_NAMES:
        raise ValueError("Invalid capacity_defaults fields")
    hosts = values.get("host")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("fleet.toml requires at least one [[host]]")
    if len(hosts) > MAX_HOSTS:
        raise ValueError(f"fleet.toml supports at most {MAX_HOSTS} hosts")
    result = []
    names = set()
    local_count = 0
    common_host_fields = {"name", "transport", "target", "tags", "required"}
    for host in hosts:
        if not isinstance(host, dict) or set(host) - common_host_fields - _THRESHOLD_NAMES:
            raise ValueError("Invalid host configuration fields")
        name = _config_text(host.get("name"), "Fleet host name", MAX_FLEET_NAME_LENGTH)
        transport = host.get("transport")
        if name in names:
            raise ValueError("Each fleet host needs a unique name")
        if transport not in {"local", "ssh"}:
            raise ValueError(f"Host {name} transport must be local or ssh")
        target = host.get("target")
        if transport == "ssh":
            target = _config_text(target, f"SSH host {name} target", MAX_FLEET_TARGET_LENGTH)
        if transport == "local":
            local_count += 1
            if target is not None:
                raise ValueError(f"Local host {name} cannot set target")
        tags = host.get("tags", [])
        if not isinstance(tags, list) or len(tags) > MAX_HOST_TAGS:
            raise ValueError(f"Host {name} supports at most {MAX_HOST_TAGS} tags")
        tags = [
            _config_text(tag, f"Host {name} tag", MAX_HOST_TAG_LENGTH)
            for tag in tags
        ]
        required = host.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(f"Host {name} required must be true or false")  # noqa: TRY004 - public config error
        threshold_values = {**defaults, **{key: value for key, value in host.items() if key in _THRESHOLD_NAMES}}
        for key, value in threshold_values.items():
            _threshold_number(value, key)
        thresholds = CapacityThresholds(**threshold_values)
        if (thresholds.maximum_cpu_busy_for_saturated_pct <= thresholds.maximum_cpu_busy_for_available_pct
                or thresholds.maximum_load_per_logical_cpu_for_saturated <= thresholds.maximum_load_per_logical_cpu_for_available
                or thresholds.minimum_available_memory_gib_for_saturated >= thresholds.minimum_available_memory_gib
                or thresholds.minimum_disk_available_gib_for_saturated >= thresholds.minimum_disk_available_gib):
            raise ValueError(f"Host {name} saturated thresholds must be more severe than available thresholds")
        result.append(FleetHost(name=name, transport=transport, target=target, tags=tuple(tags), required=required,
                                thresholds=thresholds))
        names.add(name)
    if local_count > 1:
        raise ValueError("fleet.toml can define only one local host")
    return FleetConfig(
        tuple(result),
        _bounded_positive_number(
            values.get("ssh_timeout_seconds", 3), "ssh_timeout_seconds",
            MIN_SSH_TIMEOUT_SECONDS, MAX_SSH_TIMEOUT_SECONDS,
        ),
        _bounded_positive_number(
            values.get("refresh_seconds", 2), "refresh_seconds",
            MIN_REFRESH_SECONDS, MAX_REFRESH_SECONDS,
        ),
    )


def _error_text(value: str) -> str:
    return bound_text(value)


def _nonnegative_number(value: object, name: str, maximum: float | None = None) -> None:
    if not _is_finite_number(value) or value < 0:
        raise ValueError(f"Host snapshot {name} must be a finite nonnegative number")
    if maximum is not None and value > maximum:
        raise ValueError(f"Host snapshot {name} exceeds {maximum:g}")


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON number {value}")


def _wire_text(value: object, name: str, limit: int = 1000, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"Host snapshot has invalid {name}")
    projected = bound_text(value, limit)
    if required and not projected.strip():
        raise ValueError(f"Host snapshot has invalid {name}")
    return projected


def _wire_identity(value: object, name: str, limit: int = MAX_IDENTITY_LENGTH,
                   required: bool = False) -> str | None:
    projected = _wire_text(value, name, limit, required)
    if (projected is not None
            and (projected != value
                 or any(unicodedata.category(character) in {"Cc", "Cf"}
                        for character in projected))):
        raise ValueError(f"Host snapshot has unsafe {name}")
    return projected


# Router task lifecycle states (router/daemon.py); terminal ones stay visible for a while.
ROUTER_TASK_STATUSES = frozenset({
    "created", "validating", "preparing", "running", "retrying", "cancelling", "awaiting_review",
    "blocked", "failed", "interrupted", "accepted", "integrated", "cancelled", "closed",
})


def _project_record(record: dict, allowed: frozenset[str], required: tuple[str, ...],
                    collection: str, host_id: str) -> dict:
    projected = {}
    if collection == "agents" and record.get("model_source") == "transcript":
        # Older peers may report transcript-derived models; keep only host-reported ones.
        record = {**record, "resolved_model": None, "model_source": None}
    identity_fields = {
        "host_id", "source", "session_id", "parent_session_id", "agent_id",
        "parent_agent_id", "task_id", "managed_id",
    }
    text_fields = {
        "host_id", "source", "session_id", "parent_session_id", "agent_id", "parent_agent_id", "task_id",
        "status", "cwd", "model", "coordinator_role", "visibility_reason", "activity",
        "last_event", "task", "agent_type", "requested_model", "resolved_model", "model_source",
        "executor", "owner", "control_mode", "subject", "teammate_name", "failure_reason", "label",
        "command", "worktree", "last_observed_status", "managed_id",
    }
    positive_integer_fields = {"pid", "pgid"}
    nonnegative_integer_fields = {
        "started_at", "last_event_at", "finished_at", "duration_ms", "observation_age_ms",
        "elapsed_ms", "inflight_tool_count", "created_at", "completed_at", "attempts",
    }
    boolean_fields = {"needs_input", "runtime_partial",
                      "tool_visibility_partial", "cleanup_pending", "shutdown_unconfirmed",
                      "parent_reference_omitted", "cleanup_failed"}
    for key in allowed & record.keys():
        value = record[key]
        if key in text_fields:
            limit = 2000 if key in {"cwd", "worktree"} else 500 if key in {
                "visibility_reason", "failure_reason", "subject",
            } else 240
            if key in identity_fields:
                projected[key] = _wire_identity(
                    value, f"{collection} {key}", limit, key in required,
                )
            else:
                projected[key] = _wire_text(value, f"{collection} {key}", limit)
        elif key in positive_integer_fields:
            if value is not None and not is_wire_integer(value, minimum=1):
                raise ValueError(f"Host snapshot has invalid {collection} {key}")
            projected[key] = value
        elif key in nonnegative_integer_fields:
            if value is not None and not is_wire_integer(value):
                raise ValueError(f"Host snapshot has invalid {collection} {key}")
            projected[key] = value
        elif key == "exit_code":
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                      or not -MAX_WIRE_INTEGER <= value <= MAX_WIRE_INTEGER):
                raise ValueError(f"Host snapshot has invalid {collection} {key}")
            projected[key] = value
        elif key in boolean_fields:
            if not isinstance(value, bool):
                raise ValueError(f"Host snapshot has invalid {collection} {key}")
            projected[key] = value
        else:
            raise ValueError(f"Host snapshot has invalid {collection} {key}")
    if any(not isinstance(projected.get(key), str) or not projected[key]
           or len(projected[key]) > MAX_IDENTITY_LENGTH for key in required):
        raise ValueError(f"Host snapshot has invalid {collection} identity")
    if any(projected[key] != record[key] for key in required):
        raise ValueError(f"Host snapshot has unsafe {collection} identity")
    if projected.get("host_id") != host_id:
        raise ValueError(f"Host snapshot has mismatched {collection} host identity")
    return projected


def _capacity_semantic_reasons(capacity: dict, collections: dict[str, list[dict]],
                               errors: dict, generated_at: int) -> list[str]:
    reasons = []
    if capacity.get("capacity_state") == "unknown":
        existing = capacity.get("capacity_reasons")
        if isinstance(existing, list) and existing and all(isinstance(item, str) for item in existing):
            reasons.extend(existing)
        else:
            reasons.append("source reported unknown capacity")
    if capacity.get("partial") is True:
        existing = capacity.get("capacity_reasons")
        if isinstance(existing, list) and existing and all(isinstance(item, str) for item in existing):
            reasons.extend(existing)
        else:
            reasons.append("capacity sample is partial")
    missing = [field for field in _REQUIRED_CAPACITY_FIELDS if capacity.get(field) is None]
    if missing:
        reasons.append("capacity data is incomplete: " + ", ".join(missing))
    if ((capacity.get("error") or "capacity" in errors)
            and not any("unavailable" in reason for reason in reasons)):
        reasons.append("capacity data is unavailable")
    failed_dependencies = sorted(_CAPACITY_DEPENDENCY_SECTIONS & errors.keys())
    if failed_dependencies:
        reasons.append("active workload evidence is partial: " + ", ".join(failed_dependencies))

    observed_at = capacity.get("observed_at")
    sample_age = capacity.get("sample_age_ms")
    if is_wire_integer(observed_at) and is_wire_integer(sample_age):
        if observed_at > generated_at:
            reasons.append("capacity observation is later than snapshot generation")
        elif sample_age < generated_at - observed_at:
            reasons.append("capacity sample age contradicts its observation timestamp")
    memory_total = capacity.get("memory_total_bytes")
    memory_available = capacity.get("memory_available_bytes")
    if (is_wire_integer(memory_total) and is_wire_integer(memory_available)
            and memory_available > memory_total):
        reasons.append("available memory exceeds total memory")

    visible_minimums = {
        "active_claude_sessions": sum(
            record.get("status") == "running" or record.get("needs_input") is True
            for record in collections["sessions"]
        ),
        "active_agents": sum(
            record.get("status") in {"running", "needs_input"}
            for record in collections["agents"]
        ),
        "active_managed_processes": sum(
            record.get("status") == "running"
            for record in collections["managed_processes"]
        ),
    }
    contradicted = [
        field for field, minimum in visible_minimums.items()
        if is_wire_integer(capacity.get(field)) and capacity[field] < minimum
    ]
    if contradicted:
        reasons.append("active workload totals contradict visible records: " + ", ".join(contradicted))

    reservation_values = [capacity.get(field) for field in (
        "reserved_by", "reserved_until", "reserved_cpu_slots", "reserved_memory_bytes", "reservation_source",
    )]
    reservation_active = capacity.get("reservation_active")
    if ((any(value is not None for value in reservation_values)
         and (not all(value is not None for value in reservation_values)
              or not isinstance(reservation_active, bool)))
            or (not any(value is not None for value in reservation_values)
                and reservation_active not in (None, False))):
        reasons.append("reservation data is incomplete")
    elif all(value is not None for value in reservation_values):
        expected_active = capacity["reserved_until"] > generated_at
        if reservation_active is not expected_active:
            reasons.append("reservation state contradicts its expiry")
    return list(dict.fromkeys(reasons))


def _validate_snapshot(payload: bytes) -> dict:
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Host snapshot exceeds one MiB")
    try:
        snapshot = json.loads(payload, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Host returned invalid snapshot JSON") from error
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("schema_version"), int)
            or isinstance(snapshot.get("schema_version"), bool) or snapshot["schema_version"] != SCHEMA_VERSION):
        raise ValueError("Host returned an unsupported snapshot schema")
    generated_at = snapshot.get("generated_at")
    if not is_wire_integer(generated_at):
        raise ValueError("Host snapshot has invalid generated_at")
    host = snapshot.get("host")
    host_string_fields = (
        "host_id", "hostname", "boot_id", "platform", "architecture",
        "router_version", "claude_version",
    )
    if not isinstance(host, dict) or any(
            not isinstance(host.get(key), str) or not host[key].strip()
            for key in host_string_fields):
        raise ValueError("Host snapshot lacks host identity")
    if any(len(host[key]) > HOST_STRING_LIMITS[key] for key in host_string_fields):
        raise ValueError("Host snapshot identity fields are too long")
    observed_at = host.get("observed_at")
    if not is_wire_integer(observed_at):
        raise ValueError("Host snapshot identity has invalid observed_at")
    if not isinstance(snapshot.get("partial"), bool):
        raise ValueError("Host snapshot has invalid partial flag")  # noqa: TRY004 - public wire validation
    capacity = snapshot.get("capacity")
    # Router tasks were added within schema v1; older peers omit the section.
    snapshot.setdefault("router_tasks", [])
    collections = ("sessions", "agents", "tasks", "router_tasks", "managed_processes", "inferred_processes", "alerts")
    if not isinstance(capacity, dict) or any(
            not isinstance(snapshot.get(key), list) or any(not isinstance(item, dict) for item in snapshot[key])
            for key in collections):
        raise ValueError("Host snapshot has invalid section types")
    if any(len(snapshot[key]) > MAX_COLLECTION_RECORDS for key in collections):
        raise ValueError("Host snapshot contains too many records")
    identity_fields = {
        "sessions": ("host_id", "source", "session_id"),
        "agents": ("host_id", "source", "parent_session_id", "agent_id"),
        "tasks": ("host_id", "source", "session_id", "task_id"),
        "router_tasks": ("host_id", "task_id"),
    }
    required_record_fields = {
        "sessions": frozenset({"status", "started_at", "last_event_at"}),
        "agents": frozenset({"status", "started_at", "last_event_at"}),
        "tasks": frozenset({"status", "created_at", "last_event_at"}),
        "router_tasks": frozenset({"status", "executor", "created_at", "last_event_at"}),
    }
    status_domains = {
        "sessions": frozenset({"running", "idle", "stopped", "unknown"}),
        "agents": frozenset({"running", "idle", "stopped", "unknown", "needs_input", "failed"}),
        "tasks": frozenset({"created", "completed"}),
        "router_tasks": ROUTER_TASK_STATUSES,
    }
    # Schema-v1 forward compatibility is projection, not passthrough: preserve
    # known fields and deliberately drop unknown additions from newer peers.
    record_fields = {
        "sessions": frozenset({"host_id", "source", "session_id", "parent_session_id", "status", "cwd",
                               "started_at", "last_event_at", "finished_at", "model", "coordinator_role",
                               "visibility_reason", "activity", "needs_input", "last_event",
                               "observation_age_ms", "inflight_tool_count", "tool_visibility_partial"}),
        "agents": frozenset({"host_id", "source", "parent_session_id", "parent_agent_id", "agent_id",
                             "task_id", "executor", "owner", "control_mode", "task", "agent_type",
                             "requested_model", "resolved_model", "model_source", "status",
                             "last_observed_status", "started_at", "finished_at", "duration_ms", "last_event_at",
                             "runtime_partial", "cwd", "activity", "visibility_reason",
                             "observation_age_ms", "elapsed_ms"}),
        "tasks": frozenset({"host_id", "source", "session_id", "task_id", "status", "created_at",
                            "last_event_at", "completed_at", "subject", "teammate_name", "agent_id"}),
        "router_tasks": frozenset({"host_id", "task_id", "status", "executor", "requested_model", "resolved_model",
                                   "attempts", "parent_session_id", "parent_agent_id", "task", "activity",
                                   "failure_reason", "created_at", "started_at", "elapsed_ms", "last_event_at",
                                   "cleanup_pending", "cleanup_failed", "shutdown_unconfirmed",
                                   "parent_reference_omitted"}),
        "managed_processes": frozenset({"host_id", "managed_id", "status", "label", "pid", "pgid",
                                        "started_at", "finished_at", "exit_code", "visibility_reason"}),
        "inferred_processes": frozenset({"host_id", "pid", "pgid", "status", "label", "started_at",
                                         "visibility_reason"}),
    }
    projected_collections = {}
    rejected_router_tasks = 0
    for collection, required in identity_fields.items():
        if collection == "router_tasks":
            # One odd Router task must not hide the host's sessions, agents, and capacity.
            projected_collections[collection] = []
            for record in snapshot[collection]:
                try:
                    if (not required_record_fields[collection].issubset(record)
                            or record["status"] not in status_domains[collection]):
                        raise ValueError("invalid Router task record")
                    projected_collections[collection].append(_project_record(
                        record, record_fields[collection], required, collection, host["host_id"]))
                except (ValueError, TypeError):
                    rejected_router_tasks += 1
            continue
        for record in snapshot[collection]:
            if not required_record_fields[collection].issubset(record):
                raise ValueError(f"Host snapshot has incomplete {collection} record")
            if record["status"] not in status_domains[collection]:
                raise ValueError(f"Host snapshot has invalid {collection} status")
        projected_collections[collection] = [
            _project_record(record, record_fields[collection], required, collection, host["host_id"])
            for record in snapshot[collection]
        ]
    for collection in ("managed_processes", "inferred_processes"):
        projected_collections[collection] = [
            _project_record(record, record_fields[collection], ("host_id",), collection, host["host_id"])
            for record in snapshot[collection]
        ]
    projected_alerts = []
    alert_entity_fields = frozenset({
        "source", "session_id", "parent_session_id", "agent_id", "task_id", "configured_host",
    })
    for alert in snapshot["alerts"]:
        alert_id = _wire_identity(alert.get("alert_id"), "alert identity", required=True)
        alert_host_id = _wire_identity(alert.get("host_id"), "alert host identity", required=True)
        if alert_host_id != host["host_id"]:
            raise ValueError("Host snapshot alert has mismatched host identity")
        for key in ("severity", "kind", "evidence"):
            value = alert.get(key)
            if not isinstance(value, str) or not value or len(value) > 1000:
                raise ValueError(f"Host snapshot alert has invalid {key}")
        for key in ("first_seen_at", "last_seen_at"):
            value = alert.get(key)
            if not is_wire_integer(value):
                raise ValueError(f"Host snapshot alert has invalid {key}")
        if alert["first_seen_at"] > alert["last_seen_at"]:
            raise ValueError("Host snapshot alert timestamps are out of order")
        if alert["last_seen_at"] > generated_at:
            raise ValueError("Host snapshot alert is later than snapshot generation")
        if not isinstance(alert.get("entity"), dict):
            raise ValueError("Host snapshot alert has invalid entity")  # noqa: TRY004 - public wire validation
        entity = {key: _wire_identity(value, f"alert entity {key}", 240, True)
                  for key, value in alert["entity"].items()
                  if key in alert_entity_fields}
        projected_alert = {"alert_id": alert_id, "host_id": alert_host_id,
                           "severity": _wire_text(alert["severity"], "alert severity", 40, True),
                           "kind": _wire_text(alert["kind"], "alert kind", 120, True),
                           "first_seen_at": alert["first_seen_at"], "last_seen_at": alert["last_seen_at"],
                           "entity": entity,
                           "evidence": _wire_text(alert["evidence"], "alert evidence", 1000, True)}
        projected_alerts.append(projected_alert)
    capacity_fields = frozenset({
        "logical_cpus", "load_1m", "load_5m", "cpu_busy_pct", "memory_total_bytes",
        "memory_available_bytes", "swap_used_bytes", "disk_available_bytes", "observed_at", "partial",
        "error", "sample_age_ms", "active_claude_sessions", "active_agents", "active_managed_processes",
        "reserved_by", "reserved_until", "reserved_cpu_slots", "reserved_memory_bytes", "reservation_source",
        "reservation_active", "capacity_state", "capacity_reasons",
    })
    if "partial" in capacity and not isinstance(capacity["partial"], bool):
        raise ValueError("Host snapshot has invalid capacity partial flag")
    if capacity:
        logical_cpus = capacity.get("logical_cpus")
        if logical_cpus is not None and (
                not isinstance(logical_cpus, int) or isinstance(logical_cpus, bool)
                or not 0 < logical_cpus <= 1_000_000 or not _is_finite_number(logical_cpus)):
            raise ValueError("Host snapshot logical_cpus must be a positive finite integer")
        for key in ("load_1m", "load_5m"):
            if key in capacity and capacity[key] is not None:
                _nonnegative_number(capacity[key], key, MAX_WIRE_INTEGER)
        for key in ("memory_total_bytes", "memory_available_bytes", "swap_used_bytes",
                    "disk_available_bytes", "sample_age_ms", "observed_at", "active_claude_sessions",
                    "active_agents", "active_managed_processes", "reserved_until", "reserved_cpu_slots",
                    "reserved_memory_bytes"):
            if capacity.get(key) is not None and not is_wire_integer(capacity[key]):
                raise ValueError(f"Host snapshot has invalid {key}")
        for key in ("reserved_by", "reservation_source"):
            if capacity.get(key) is not None and (
                    not isinstance(capacity[key], str) or not capacity[key] or len(capacity[key]) > 240):
                raise ValueError(f"Host snapshot has invalid {key}")
        if "reservation_active" in capacity and not isinstance(capacity["reservation_active"], bool):
            raise ValueError("Host snapshot has invalid reservation_active")
        if capacity.get("cpu_busy_pct") is not None:
            _nonnegative_number(capacity["cpu_busy_pct"], "cpu_busy_pct", 100)
        if "capacity_state" in capacity and capacity["capacity_state"] not in {
                "available", "busy", "saturated", "unknown"}:
            raise ValueError("Host snapshot has invalid capacity_state")
        reasons = capacity.get("capacity_reasons")
        if reasons is not None and (not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons)):
            raise ValueError("Host snapshot has invalid capacity_reasons")
        if reasons is not None and (len(reasons) > 32 or any(len(item) > 500 for item in reasons)):
            raise ValueError("Host snapshot has too many or oversized capacity_reasons")
    projected_capacity = {key: value for key, value in capacity.items() if key in capacity_fields}
    for key in ("reserved_by", "reservation_source", "error"):
        if key in projected_capacity and projected_capacity[key] is not None:
            projected_capacity[key] = _wire_text(projected_capacity[key], f"capacity {key}",
                                                 500 if key == "error" else 240)
    if projected_capacity.get("capacity_reasons") is not None:
        projected_capacity["capacity_reasons"] = [
            _wire_text(reason, "capacity reason", 500, True) for reason in projected_capacity["capacity_reasons"]
        ]
    projected_errors = {}
    errors = snapshot.get("errors", {})
    if not isinstance(errors, dict):
        raise TypeError("Host snapshot has invalid errors")
    recognized_error_sections = {
        "host", "sessions", "agents", "tasks", "router_tasks", "managed_processes", "inferred_processes",
        "capacity", "alerts", "lifecycle", "lifecycle_consistency", "output", "projection",
        "session_active_count", "agent_active_count", "managed_process_active_count",
    }
    for key, value in errors.items():
        if key in recognized_error_sections:
            projected_errors[key] = _wire_text(value, f"snapshot error {key}", 500, True)
    unknown_error_count = len(set(errors) - recognized_error_sections)
    if unknown_error_count:
        projected_errors["other"] = f"{unknown_error_count} unrecognized error section(s) omitted"
    if rejected_router_tasks:
        projected_errors["router_tasks"] = bound_text(
            f"{rejected_router_tasks} invalid Router task record(s) omitted"
            + (f"; {projected_errors['router_tasks']}" if "router_tasks" in projected_errors else ""), 500)
    projected_host = {"host_id": _wire_identity(
        host["host_id"], "host host_id", MAX_HOST_ID_LENGTH, True,
    )}
    for key in host_string_fields:
        if key == "host_id":
            continue
        projected_host[key] = _wire_text(
            host[key], f"host {key}",
            HOST_STRING_LIMITS[key],
            True,
        )
    projected_host["observed_at"] = observed_at
    projected = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at,
                 "partial": bool(snapshot["partial"] or projected_capacity.get("partial") is True
                                 or projected_capacity.get("error") or projected_errors),
                 "host": projected_host, "capacity": projected_capacity,
                 **projected_collections, "alerts": projected_alerts}
    if projected_errors:
        projected["errors"] = projected_errors
    semantic_reasons = _capacity_semantic_reasons(
        projected_capacity, projected_collections, projected_errors, generated_at,
    )
    if semantic_reasons:
        projected["partial"] = True
        projected_capacity.update(
            partial=True, capacity_state="unknown", capacity_reasons=semantic_reasons,
        )
    return projected


def ssh_snapshot(host: FleetHost, timeout: float) -> dict:
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, math.ceil(timeout))}",
               "-n", "--", host.target, "router", "snapshot", "--json"]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        raise ValueError(str(error)) from error
    selector = None
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    failure = None
    try:
        if process.stdout is None or process.stderr is None:
            raise ValueError("ssh output pipes unavailable")
        selector = selectors.DefaultSelector()
        streams = {process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"}
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"snapshot timed out after {timeout:g}s"
                break
            for key, _ in selector.select(remaining):
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if streams[key.fd] == "stdout":
                    keep = MAX_SNAPSHOT_BYTES + 1 - len(stdout)
                    if keep > 0:
                        stdout.extend(chunk[:keep])
                    if len(stdout) > MAX_SNAPSHOT_BYTES:
                        failure = "Host snapshot exceeds one MiB"
                        break
                elif len(stderr) < 65_536:
                    stderr.extend(chunk[:65_536 - len(stderr)])
            if failure:
                break
    except (OSError, ValueError) as error:
        failure = str(error)
    finally:
        if failure:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                pass
        if selector is not None:
            selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        leader_reaped = True
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            leader_reaped = False  # The group is SIGKILLed below already.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
            if failure is None:
                failure = "ssh process did not exit after snapshot collection"
        if failure and leader_reaped:
            _kill_remaining_group(process.pid)
    if failure:
        raise ValueError(failure)
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() or f"ssh exited {process.returncode}"
        raise ValueError(_error_text(detail))
    return _validate_snapshot(bytes(stdout))


def _kill_remaining_group(pgid: int, grace_seconds: float = .5) -> None:
    """After a failed collection, SIGKILL group members that outlived SIGTERM.

    The leader exiting proves nothing about descendants such as a ProxyCommand.
    """
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            os.killpg(pgid, 0)
        except (OSError, AttributeError):
            return  # No members remain (or no process groups on this platform).
        if time.monotonic() >= deadline:
            break
        time.sleep(.01)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _prefix_identities(snapshot: dict) -> dict:
    host_id = snapshot["host"]["host_id"]
    for collection, keys in (("sessions", ("source", "session_id")),
                             ("agents", ("source", "parent_session_id", "agent_id")),
                             ("tasks", ("source", "session_id", "task_id")),
                             ("router_tasks", ("task_id",))):
        for record in snapshot.get(collection, []):
            if isinstance(record, dict):
                parts = [host_id] + [str(record.get(key) or "unknown") for key in keys]
                record["fleet_id"] = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return snapshot


def _fleet_alert(record: dict, kind: str, evidence: str, at: int) -> dict:
    host_id = record.get("snapshot", {}).get("host", {}).get("host_id")
    identity = host_id or record["name"]
    alert_id = hashlib.sha256(f"{identity}\0{kind}".encode()).hexdigest()[:24]
    return {"alert_id": alert_id, "host_id": host_id, "severity": "warning", "kind": kind,
            "first_seen_at": at, "last_seen_at": at, "entity": {"configured_host": record["name"]},
            "evidence": evidence}


def _bound_fleet_result(result: dict) -> dict:
    """Keep the aggregate response bounded while preserving explicit omission evidence."""
    def rendered_size() -> int:
        return len(json.dumps(result, indent=2).encode())

    if rendered_size() <= MAX_FLEET_OUTPUT_BYTES:
        return result
    for record in reversed(result["hosts"]):
        if not record.get("ok") or "snapshot" not in record:
            continue
        record["alerts"].append(_fleet_alert(
            record, "host_snapshot_omitted",
            "Host snapshot omitted to enforce the aggregate fleet output bound",
            result["generated_at"],
        ))
        record.pop("snapshot")
        record.update(
            ok=False,
            error="Host snapshot omitted to enforce the aggregate fleet output bound",
        )
        result["partial"] = True
        if rendered_size() <= MAX_FLEET_OUTPUT_BYTES:
            return result
    raise ValueError("Fleet metadata exceeds the aggregate output bound")


def _supported_claude_version(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(part.isascii() and part.isdigit() for part in parts) \
        and tuple(map(int, parts)) >= MINIMUM_CLAUDE_VERSION


def query_fleet(config: FleetConfig, local_snapshot=None, only: str | None = None,
                alert_history: AlertHistory | None = None) -> dict:
    selected = [host for host in config.hosts if only is None or host.name == only]
    if only is not None and not selected:
        raise ValueError(f"Unknown fleet host: {only}")
    def query(host: FleetHost) -> dict:
        started = time.monotonic()
        wrapper = {"name": host.name, "transport": host.transport, "target": host.target,
                   "tags": list(host.tags), "required": host.required, "alerts": []}
        try:
            if host.transport == "local":
                if local_snapshot is None:
                    from router.client import request
                    snapshot = request("snapshot", {}, timeout=config.ssh_timeout_seconds)
                else:
                    snapshot = local_snapshot(host.thresholds)
                snapshot = _validate_snapshot(json.dumps(snapshot, separators=(",", ":")).encode())
            else:
                snapshot = ssh_snapshot(host, config.ssh_timeout_seconds)
            received_at = time.monotonic()
            return {**wrapper, "ok": True, "latency_ms": round((received_at - started) * 1000),
                    "snapshot": snapshot, "_started_at": started, "_received_at": received_at}
        except (OSError, ValueError, KeyError, TypeError, RecursionError, UnicodeError, OverflowError) as error:
            return {**wrapper, "ok": False, "latency_ms": round((time.monotonic() - started) * 1000),
                    "error": _error_text(str(error))}

    records = {}
    with ThreadPoolExecutor(max_workers=min(len(selected), MAX_HOSTS)) as pool:
        futures = {pool.submit(query, host): host.name for host in selected}
        for future in as_completed(futures):
            records[futures[future]] = future.result()
    hosts = [records[host.name] for host in selected]
    collection_finished = time.monotonic()
    configured_hosts = {host.name: host for host in selected}
    generated_at = int(time.time() * 1000)
    identities: dict[str, list[dict]] = {}
    for record in hosts:
        if record["ok"]:
            identities.setdefault(record["snapshot"]["host"]["host_id"], []).append(record)
    for records_with_identity in identities.values():
        if len(records_with_identity) == 1:
            record = records_with_identity[0]
            _prefix_identities(record["snapshot"])
            if len(json.dumps(record["snapshot"], separators=(",", ":")).encode()) > MAX_SNAPSHOT_BYTES:
                record.pop("snapshot")
                record.pop("_started_at", None)
                record.pop("_received_at", None)
                record.update(ok=False, error="Host snapshot exceeds one MiB after fleet identity augmentation")
            continue
        configured = ", ".join(record["name"] for record in records_with_identity)
        for record in records_with_identity:
            snapshot = record["snapshot"]
            snapshot["partial"] = True
            snapshot.setdefault("errors", {})["host_identity"] = (
                f"Duplicate host_id reported by configured hosts: {configured}"
            )
            record["alerts"].append(_fleet_alert(
                record, "duplicate_host_identity",
                f"Duplicate host identity is also reported by: {configured}", generated_at,
            ))
            if len(json.dumps(snapshot, separators=(",", ":")).encode()) > MAX_SNAPSHOT_BYTES:
                record.pop("snapshot")
                record.pop("_started_at", None)
                record.pop("_received_at", None)
                record.update(ok=False, error="Host snapshot exceeds one MiB after identity-collision annotation")
    versions = {record["snapshot"]["host"].get("router_version") for record in hosts if record["ok"]}
    version_mismatch = len(versions) > 1
    for record in hosts:
        if not record["ok"]:
            record["alerts"].append(_fleet_alert(record, "host_offline", record["error"], generated_at))
            continue
        snapshot = record["snapshot"]
        capacity = snapshot.get("capacity", {})
        sample_age = capacity.get("sample_age_ms")
        received_at = record.pop("_received_at")
        started_at = record.pop("_started_at")
        if isinstance(sample_age, (int, float)):
            sample_age = min(
                MAX_WIRE_INTEGER,
                math.ceil(sample_age + max(0, (collection_finished - started_at) * 1000)),
            )
            capacity["sample_age_ms"] = sample_age
        reservation_values = [capacity.get(field) for field in (
            "reserved_by", "reserved_until", "reserved_cpu_slots", "reserved_memory_bytes", "reservation_source")]
        if all(value is not None for value in reservation_values):
            # The producer's consistency was checked against its own generated_at
            # during wire validation. The local host shares the viewer's clock; a
            # remote's clock is advanced only by time since receipt (SSH setup
            # precedes generation), so neither viewer skew nor handshake time can
            # expire a remote reservation early.
            if configured_hosts[record["name"]].transport == "local":
                source_now = generated_at
            else:
                source_now = snapshot["generated_at"] + max(0, (collection_finished - received_at) * 1000)
            capacity["reservation_active"] = capacity["reserved_until"] > source_now
        # One threshold for classification and alerting, so a stale sample is
        # never "unknown" without also marking the snapshot partial.
        stale_after_ms = max(15_000, config.refresh_seconds * 3000)
        state, reasons = classify(capacity, configured_hosts[record["name"]].thresholds, stale_after_ms)
        capacity.update(capacity_state=state, capacity_reasons=reasons)
        snapshot["alerts"] = [
            alert for alert in snapshot["alerts"] if alert.get("kind") not in CAPACITY_ALERT_KINDS
        ]
        if snapshot["host"] and capacity:
            # Reclassified on the viewer's clock, so these belong on the wrapper; the
            # nested snapshot keeps only producer-timed alerts (generation chronology).
            record["alerts"].extend(capacity_alerts(
                snapshot["host"], capacity, configured_hosts[record["name"]].thresholds,
                generated_at,
            ))
        if isinstance(sample_age, (int, float)) and sample_age > stale_after_ms:
            record["alerts"].append(_fleet_alert(record, "host_snapshot_stale",
                                                   "Host capacity sample is stale", generated_at))
            snapshot["partial"] = True
        if version_mismatch:
            record["alerts"].append(_fleet_alert(record, "router_version_mismatch",
                                                   "Fleet hosts report different Router versions", generated_at))
        claude_version = snapshot["host"]["claude_version"]
        if not _supported_claude_version(claude_version):
            record["alerts"].append(_fleet_alert(
                record, "unsupported_claude_version",
                f"Claude Code {claude_version} is below or does not match the supported minimum 2.1.292",
                generated_at,
            ))
    result = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at,
              "partial": any(not host["ok"] or host["snapshot"].get("partial") for host in hosts),
              "version_mismatch": version_mismatch, "hosts": hosts}
    _bound_fleet_result(result)
    if alert_history is not None:
        # Scope by the trusted configured name; a remote chooses its own alert_id.
        tracked = [
            (record["name"], alert)
            for record in result["hosts"]
            for alert in record["alerts"] + (record.get("snapshot", {}).get("alerts", []) if record["ok"] else [])
        ]
        alert_history.track([alert for _, alert in tracked], [name for name, _ in tracked])
    return result


def fleet_exit_ok(result: dict, strict: bool) -> bool:
    valid = [host for host in result["hosts"] if host["ok"]]
    if not valid:
        return False
    return not strict or all(host["ok"] or not host["required"] for host in result["hosts"])


def capacity_projection(result: dict) -> dict:
    projected = {key: value for key, value in result.items() if key != "hosts"}
    projected["hosts"] = []
    for record in result["hosts"]:
        host = {key: value for key, value in record.items() if key != "snapshot"}
        if record["ok"]:
            source = record["snapshot"]
            host["snapshot"] = {"host": source["host"], "capacity": source["capacity"],
                                "partial": source["partial"], "errors": source.get("errors", {})}
        projected["hosts"].append(host)
    return projected
