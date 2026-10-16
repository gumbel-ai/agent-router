"""Fleet configuration, bounded local/SSH queries, and merged read-only views."""

import hashlib
import json
import math
import os
import re
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
from router.redaction import redact_secrets
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
_SAFE_ENVIRONMENT_NAMES = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"})
_MANAGED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MANAGED_PROCESS_FIELDS = frozenset({
    "managed_process_id", "host_id", "boot_id", "pid", "pgid", "process_start_time",
    "creation_identity", "group_creation_identity", "label", "session_id", "agent_id",
    "attribution", "executable", "argument_hash", "argument_count", "cwd",
    "environment_allowlist", "status", "registered_at", "started_at", "last_sample_at",
    "finished_at", "exit_status", "signal", "elapsed_ms", "cpu_pct", "rss_bytes",
    "peak_rss_bytes", "cpu_seconds", "shutdown_uncertain", "shutdown_was_uncertain",
    "descendant_tracking_truncated", "visibility_reason",
    "worker_count",
})
_INFERRED_PROCESS_FIELDS = frozenset({
    "host_id", "boot_id", "pid", "pgid", "process_start_time", "creation_identity",
    "session_id", "agent_id", "attribution", "executable", "status", "observed_at",
    "cpu_pct", "rss_bytes",
})


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
    if value != value.strip() or redact_secrets(value, maximum + 1) != value:
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


def _redact_error(value: str) -> str:
    return redact_secrets(value)


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
    projected = redact_secrets(value, limit)
    if required and not projected.strip():
        raise ValueError(f"Host snapshot has invalid {name}")
    return projected


def _wire_identity(value: object, name: str, limit: int = MAX_IDENTITY_LENGTH,
                   required: bool = False) -> str | None:
    projected = _wire_text(value, name, limit, required)
    if (projected is not None
            and (projected != value or "[REDACTED" in projected.upper()
                 or any(unicodedata.category(character) in {"Cc", "Cf"}
                        for character in projected))):
        raise ValueError(f"Host snapshot has unsafe {name}")
    return projected


def _project_record(record: dict, allowed: frozenset[str], required: tuple[str, ...],
                    collection: str, host_id: str) -> dict:
    projected = {}
    changed = 0
    omitted = len(set(record) - allowed)
    identity_fields = {
        "host_id", "source", "session_id", "parent_session_id", "agent_id",
        "parent_agent_id", "task_id", "managed_id", "claude_process_start_time",
        "claude_creation_identity",
    }
    text_fields = {
        "host_id", "source", "session_id", "parent_session_id", "agent_id", "parent_agent_id", "task_id",
        "status", "cwd", "model", "coordinator_role", "visibility_reason", "activity",
        "last_event", "task", "agent_type", "requested_model", "resolved_model", "model_source",
        "executor", "owner", "control_mode", "cost_source", "cost_note", "usage_source",
        "price_source", "price_date", "subject", "teammate_name", "failure_reason", "label", "command",
        "worktree", "last_observed_status", "managed_id", "transcript_error",
        "claude_process_start_time", "claude_creation_identity", "process_visibility_reason",
    }
    positive_integer_fields = {"pid", "pgid", "claude_pid"}
    nonnegative_integer_fields = {
        "started_at", "last_event_at", "finished_at", "duration_ms", "observation_age_ms",
        "elapsed_ms", "inflight_tool_count", "created_at", "completed_at",
    }
    boolean_fields = {"needs_input", "runtime_partial", "usage_partial", "transcript_pending",
                      "tool_visibility_partial"}
    number_fields = {"cost"}
    usage_invalid = False
    session_process_fields = {
        "claude_pid", "claude_process_start_time", "claude_creation_identity",
        "process_visibility_reason",
    }
    for key in allowed & record.keys():
        value = record[key]
        field_name = (
            f"{collection} process {key}"
            if collection == "sessions" and key in session_process_fields
            else f"{collection} {key}"
        )
        if key in text_fields:
            limit = 2000 if key in {"cwd", "worktree"} else 500 if key in {
                "visibility_reason", "cost_note", "failure_reason", "subject", "transcript_error",
            } else 240
            if key in identity_fields:
                projected[key] = _wire_identity(
                    value, field_name, limit, key in required,
                )
            else:
                projected[key] = _wire_text(value, field_name, limit)
                changed += projected[key] != value
        elif key in positive_integer_fields:
            if value is not None and not is_wire_integer(value, minimum=1):
                raise ValueError(f"Host snapshot has invalid {field_name}")
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
        elif key in number_fields:
            if value is not None and (not _is_finite_number(value) or value < 0 or value > 1e12):
                raise ValueError(f"Host snapshot has invalid {collection} {key}")
            projected[key] = value
        elif key == "usage" and value is None:
            projected[key] = None
        elif key == "usage" and isinstance(value, dict):
            usage = {}
            known_usage = {"input_tokens", "output_tokens", "cache_read_input_tokens",
                           "cache_creation_input_tokens"}
            omitted += len(set(value) - known_usage)
            for name in known_usage:
                if name not in value:
                    continue
                amount = value[name]
                if (not isinstance(amount, int) or isinstance(amount, bool)
                        or not 0 <= amount <= MAX_WIRE_INTEGER):
                    usage_invalid = True
                    continue
                usage[name] = amount
            if sum(usage.values()) > MAX_WIRE_INTEGER:
                usage_invalid = True
            projected[key] = None if usage_invalid else usage
        elif key in {"models", "price_overrides_used"}:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"Host snapshot has invalid {collection} {key}")
            if len(value) > 64:
                raise ValueError(f"Host snapshot has too many {collection} {key} values")
            projected[key] = [_wire_text(item, f"{collection} {key}", 240, True) for item in value]
            changed += projected[key] != value
        else:
            raise ValueError(f"Host snapshot has invalid {collection} {key}")
    if any(not isinstance(projected.get(key), str) or not projected[key]
           or len(projected[key]) > MAX_IDENTITY_LENGTH for key in required):
        raise ValueError(f"Host snapshot has invalid {collection} identity")
    if any(projected[key] != record[key] for key in required):
        raise ValueError(f"Host snapshot has unsafe {collection} identity")
    if projected.get("host_id") != host_id:
        raise ValueError(f"Host snapshot has mismatched {collection} host identity")
    if usage_invalid:
        projected.update(usage=None, usage_source="unavailable", usage_partial=True,
                         _projection_partial=True)
        if projected.get("cost_source") != "host_reported_estimate":
            projected.update(cost=None, cost_source="unavailable",
                             cost_note="Reported usage is invalid or exceeds the supported bound")
        changed += 1
    projected["_projection_changes"] = changed
    projected["_projection_omissions"] = omitted
    return projected


def _add_projection_error(errors: dict, section: str, *, changed: int = 0,
                          omitted: int = 0) -> None:
    details = []
    if changed:
        details.append(f"{changed} value(s) changed for output safety")
    if omitted:
        details.append(f"{omitted} unsupported field(s) omitted")
    if not details:
        return
    evidence = "Projection: " + "; ".join(details)
    existing = errors.get(section)
    errors[section] = redact_secrets(
        f"{evidence}; {existing}" if existing else evidence,
        500,
    )


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


def _process_text(record: dict, key: str, maximum: int = MAX_IDENTITY_LENGTH,
                  required: bool = True, reject_secrets: bool = False) -> str | None:
    value = record.get(key)
    if value is None and not required:
        return None
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"Host snapshot process has invalid {key}")
    if reject_secrets and redact_secrets(value, maximum) != value:
        raise ValueError(f"Host snapshot process has unsafe {key}")
    return value


def _validate_process(record: dict, host: dict, managed: bool) -> None:
    allowed = _MANAGED_PROCESS_FIELDS if managed else _INFERRED_PROCESS_FIELDS
    if set(record) - allowed:
        raise ValueError("Host snapshot process contains unknown fields")
    _process_text(record, "host_id")
    _process_text(record, "boot_id")
    _process_text(record, "process_start_time")
    _process_text(record, "executable", 2000)
    if record["host_id"] != host["host_id"] or record["boot_id"] != host["boot_id"]:
        raise ValueError("Host snapshot process identity does not match its host")
    for key in ("pid", "pgid"):
        value = record.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"Host snapshot process has invalid {key}")
    if record.get("attribution") not in {"exact", "session", "host"}:
        raise ValueError("Host snapshot process has invalid attribution")
    if record.get("status") not in {"running", "stopped", "failed", "unknown"}:
        raise ValueError("Host snapshot process has invalid status")
    for key in ("session_id", "agent_id"):
        _process_text(record, key, required=False, reject_secrets=True)
    creation_identity = _process_text(record, "creation_identity", reject_secrets=True)
    platform = host["platform"]
    if platform == "linux":
        expected_identity = f"linux:{host['boot_id']}:{record['process_start_time']}"
    elif platform == "macos":
        expected_identity = f"darwin:{record['process_start_time']}"
    else:
        raise ValueError("Host snapshot process uses an unsupported platform")
    if creation_identity != expected_identity:
        raise ValueError("Host snapshot process has mismatched creation identity")
    attribution = record["attribution"]
    session_id = record.get("session_id")
    agent_id = record.get("agent_id")
    if (attribution == "exact" and (session_id is None or agent_id is None)):
        raise ValueError("Host snapshot exact process attribution lacks session or agent evidence")
    if attribution == "session" and (session_id is None or agent_id is not None):
        raise ValueError("Host snapshot session process attribution lacks session evidence")
    if attribution == "host" and (session_id is not None or agent_id is not None):
        raise ValueError("Host snapshot host process attribution claims session or agent evidence")
    for key in ("cpu_pct", "rss_bytes", "peak_rss_bytes", "elapsed_ms", "cpu_seconds"):
        if record.get(key) is not None:
            _nonnegative_number(record[key], f"process {key}")
    for key in ("started_at", "registered_at", "last_sample_at", "finished_at", "observed_at"):
        if record.get(key) is not None:
            value = record[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Host snapshot process has invalid {key}")
    if managed:
        _process_text(record, "managed_process_id", reject_secrets=True)
        if not _MANAGED_ID.fullmatch(record["managed_process_id"]):
            raise ValueError("Host snapshot process has invalid managed_process_id")
        group_identity = _process_text(
            record, "group_creation_identity", reject_secrets=True,
        )
        expected_group_prefix = (
            f"linux:{host['boot_id']}:" if platform == "linux" else "darwin:"
        )
        if not group_identity.startswith(expected_group_prefix) or (
                group_identity == expected_group_prefix):
            raise ValueError("Host snapshot process has invalid group creation identity")
        _process_text(record, "label")
        _process_text(record, "cwd", 2000)
        _process_text(record, "visibility_reason", 1000, required=False)
        argument_hash = _process_text(record, "argument_hash")
        if len(argument_hash) != 64 or any(character not in "0123456789abcdef" for character in argument_hash):
            raise ValueError("Host snapshot process has invalid argument_hash")
        argument_count = record.get("argument_count")
        if not isinstance(argument_count, int) or isinstance(argument_count, bool) or argument_count < 0:
            raise ValueError("Host snapshot process has invalid argument_count")
        worker_count = record.get("worker_count")
        if (not isinstance(worker_count, int) or isinstance(worker_count, bool)
                or worker_count < 0):
            raise ValueError("Host snapshot process has invalid worker_count")
        if any(record.get(key) is None for key in ("registered_at", "started_at", "elapsed_ms")):
            raise ValueError("Host snapshot managed process lacks lifecycle timestamps")
        for key in ("shutdown_uncertain", "shutdown_was_uncertain",
                    "descendant_tracking_truncated"):
            if not isinstance(record.get(key), bool):
                raise ValueError(  # noqa: TRY004 - public wire validation
                    f"Host snapshot process has invalid {key}"
                )
        exit_status = record.get("exit_status")
        if (exit_status is not None and
                (not isinstance(exit_status, int) or isinstance(exit_status, bool)
                 or exit_status < 0)):
            raise ValueError("Host snapshot process has invalid exit_status")
        process_signal = record.get("signal")
        if (process_signal is not None and
                (not isinstance(process_signal, int) or isinstance(process_signal, bool)
                 or process_signal <= 0)):
            raise ValueError("Host snapshot process has invalid signal")
        if exit_status is not None and process_signal is not None:
            raise ValueError("Host snapshot process cannot have both exit_status and signal")
        status = record["status"]
        finished_at = record.get("finished_at")
        completion_present = finished_at is not None or exit_status is not None or process_signal is not None
        completion_valid = (
            finished_at is not None and ((exit_status is not None) != (process_signal is not None))
        )
        if completion_present and not completion_valid:
            raise ValueError("Host snapshot process has incomplete completion evidence")
        if status == "running" and (completion_present or record["shutdown_uncertain"]):
            raise ValueError("Host snapshot running process has contradictory completion evidence")
        if status == "stopped" and not (
            completion_valid and exit_status == 0 and process_signal is None
            and not record["shutdown_uncertain"]
        ):
            raise ValueError("Host snapshot stopped process has contradictory completion evidence")
        if status == "failed" and not (
            completion_valid and (process_signal is not None or exit_status > 0)
            and not record["shutdown_uncertain"]
        ):
            raise ValueError("Host snapshot failed process has contradictory completion evidence")
        if status == "unknown" and not record["shutdown_uncertain"]:
            raise ValueError("Host snapshot unknown process lacks shutdown uncertainty")
        if finished_at is not None and finished_at < record.get("started_at", 0):
            raise ValueError("Host snapshot process completion predates its start")
        allowlist = record.get("environment_allowlist")
        if (allowlist not in ({}, [])
                and (not isinstance(allowlist, list)
                     or len(allowlist) > len(_SAFE_ENVIRONMENT_NAMES)
                     or any(not isinstance(name, str) or name not in _SAFE_ENVIRONMENT_NAMES
                            for name in allowlist)
                     or len(set(allowlist)) != len(allowlist))):
            raise ValueError("Host snapshot process has invalid environment_allowlist")
        for key, limit in (("label", 240), ("executable", 2000), ("cwd", 2000),
                           ("visibility_reason", 1000)):
            if record.get(key) is not None:
                record[key] = redact_secrets(record[key], limit)
    else:
        if attribution != "session" or session_id is None or agent_id is not None:
            raise ValueError("Inferred process lacks session attribution")
        if record["status"] != "running":
            raise ValueError("Inferred process must be running")
        if record.get("observed_at") is None:
            raise ValueError("Host snapshot inferred process lacks observed_at")
        record["executable"] = redact_secrets(record["executable"], 2000)


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
    top_level_fields = frozenset({
        "schema_version", "generated_at", "partial", "host", "capacity", "sessions",
        "agents", "tasks", "managed_processes", "inferred_processes", "alerts", "errors",
    })
    top_level_omissions = len(set(snapshot) - top_level_fields)
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
    collections = ("sessions", "agents", "tasks", "managed_processes", "inferred_processes", "alerts")
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
    }
    required_record_fields = {
        "sessions": frozenset({"status", "started_at", "last_event_at"}),
        "agents": frozenset({"status", "started_at", "last_event_at"}),
        "tasks": frozenset({"status", "created_at", "last_event_at"}),
    }
    status_domains = {
        "sessions": frozenset({"running", "idle", "stopped", "unknown"}),
        "agents": frozenset({"running", "idle", "stopped", "unknown", "needs_input", "failed"}),
        "tasks": frozenset({"created", "completed"}),
    }
    # Schema-v1 forward compatibility is projection, not passthrough: preserve
    # known fields and deliberately drop unknown additions from newer peers.
    record_fields = {
        "sessions": frozenset({"host_id", "source", "session_id", "parent_session_id", "status", "cwd",
                               "started_at", "last_event_at", "finished_at", "model", "coordinator_role",
                               "visibility_reason", "activity", "needs_input", "last_event",
                               "observation_age_ms", "inflight_tool_count", "tool_visibility_partial",
                               "claude_pid", "claude_process_start_time", "claude_creation_identity",
                               "process_visibility_reason"}),
        "agents": frozenset({"host_id", "source", "parent_session_id", "parent_agent_id", "agent_id",
                             "task_id", "executor", "owner", "control_mode", "task", "agent_type",
                             "requested_model", "resolved_model", "models", "model_source", "status",
                             "last_observed_status", "started_at", "finished_at", "duration_ms", "last_event_at",
                             "runtime_partial", "cwd", "worktree", "activity", "cost", "cost_source",
                             "cost_note", "usage", "usage_source", "price_date", "price_source",
                             "price_overrides_used", "usage_partial", "transcript_pending",
                             "transcript_error", "visibility_reason", "observation_age_ms", "elapsed_ms"}),
        "tasks": frozenset({"host_id", "source", "session_id", "task_id", "status", "created_at",
                            "last_event_at", "completed_at", "subject", "teammate_name", "agent_id"}),
    }
    projected_collections = {}
    collection_projection_partial = False
    collection_projection_evidence: dict[str, tuple[int, int]] = {}
    for collection, required in identity_fields.items():
        for record in snapshot[collection]:
            if not required_record_fields[collection].issubset(record):
                raise ValueError(f"Host snapshot has incomplete {collection} record")
            if record["status"] not in status_domains[collection]:
                raise ValueError(f"Host snapshot has invalid {collection} status")
        projected_collections[collection] = [
            _project_record(record, record_fields[collection], required, collection, host["host_id"])
            for record in snapshot[collection]
        ]
    for collection in identity_fields:
        records = projected_collections[collection]
        changed = 0
        omitted = 0
        for record in records:
            changed += int(record.pop("_projection_changes", 0))
            omitted += int(record.pop("_projection_omissions", 0))
            changed += bool(record.pop("_projection_partial", False))
        collection_projection_evidence[collection] = changed, omitted
        collection_projection_partial |= bool(changed or omitted)
    for record in snapshot["sessions"]:
        process_reason = record.get("process_visibility_reason")
        if (process_reason is not None
                and (not isinstance(process_reason, str) or not process_reason
                     or len(process_reason) > 1000
                     or any(ord(character) < 32 or ord(character) == 127
                            for character in process_reason)
                     or redact_secrets(process_reason, 1000) != process_reason)):
            raise ValueError("Host snapshot session has invalid process visibility reason")
        process_evidence = (
            record.get("claude_pid"), record.get("claude_process_start_time"),
            record.get("claude_creation_identity"),
        )
        if any(value is not None for value in process_evidence):
            pid, start_time, identity = process_evidence
            if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                    or not isinstance(start_time, str) or not start_time
                    or len(start_time) > MAX_IDENTITY_LENGTH
                    or not isinstance(identity, str) or not identity
                    or len(identity) > MAX_IDENTITY_LENGTH
                    or any(ord(character) < 32 or ord(character) == 127
                           for value in (start_time, identity) for character in value)):
                raise ValueError("Host snapshot session has invalid process evidence")
            if (redact_secrets(start_time, MAX_IDENTITY_LENGTH) != start_time
                    or redact_secrets(identity, MAX_IDENTITY_LENGTH) != identity):
                raise ValueError("Host snapshot session has unsafe process evidence")
            expected_identity = (
                f"linux:{host['boot_id']}:{start_time}"
                if host["platform"] == "linux"
                else f"darwin:{start_time}"
                if host["platform"] == "macos"
                else None
            )
            if identity != expected_identity:
                raise ValueError("Host snapshot session has mismatched process evidence")
    for collection, managed in (("managed_processes", True), ("inferred_processes", False)):
        projected_records = []
        changed = 0
        for record in snapshot[collection]:
            projected_record = dict(record)
            _validate_process(projected_record, host, managed=managed)
            changed += projected_record != record
            projected_records.append(projected_record)
        projected_collections[collection] = projected_records
        collection_projection_evidence[collection] = changed, 0
        collection_projection_partial |= bool(changed)
    projected_alerts = []
    alert_projection_changes = 0
    alert_projection_omissions = 0
    alert_fields = frozenset({
        "alert_id", "host_id", "severity", "kind", "first_seen_at", "last_seen_at", "entity", "evidence",
    })
    alert_entity_fields = frozenset({
        "source", "session_id", "parent_session_id", "agent_id", "task_id", "configured_host",
        "managed_process_id",
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
        alert_projection_changes += sum(
            projected_alert[key] != alert[key] for key in ("severity", "kind", "evidence")
        )
        alert_projection_omissions += len(set(alert) - alert_fields)
        alert_projection_omissions += len(set(alert["entity"]) - alert_entity_fields)
        projected_alerts.append(projected_alert)
    capacity_fields = frozenset({
        "logical_cpus", "load_1m", "load_5m", "cpu_busy_pct", "memory_total_bytes",
        "memory_available_bytes", "swap_used_bytes", "disk_available_bytes", "observed_at", "partial",
        "error", "sample_age_ms", "active_claude_sessions", "active_agents", "active_managed_processes",
        "reserved_by", "reserved_until", "reserved_cpu_slots", "reserved_memory_bytes", "reservation_source",
        "reservation_active", "capacity_state", "capacity_reasons",
    })
    capacity_projection_omissions = len(set(capacity) - capacity_fields)
    capacity_projection_changes = 0
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
            original = projected_capacity[key]
            projected_capacity[key] = _wire_text(original, f"capacity {key}", 500 if key == "error" else 240)
            capacity_projection_changes += projected_capacity[key] != original
    if projected_capacity.get("capacity_reasons") is not None:
        original_reasons = projected_capacity["capacity_reasons"]
        projected_capacity["capacity_reasons"] = [
            _wire_text(reason, "capacity reason", 500, True) for reason in projected_capacity["capacity_reasons"]
        ]
        capacity_projection_changes += projected_capacity["capacity_reasons"] != original_reasons
    projected_errors = {}
    errors = snapshot.get("errors", {})
    if not isinstance(errors, dict):
        raise TypeError("Host snapshot has invalid errors")
    recognized_error_sections = {
        "host", "sessions", "agents", "tasks", "managed_processes", "inferred_processes",
        "capacity", "alerts", "lifecycle", "lifecycle_consistency", "output", "projection",
        "session_active_count", "agent_active_count", "managed_process_active_count",
        "processes", "session_processes",
    }
    for key, value in errors.items():
        if key in recognized_error_sections:
            projected_errors[key] = _wire_text(value, f"snapshot error {key}", 500, True)
    unknown_error_count = len(set(errors) - recognized_error_sections)
    if unknown_error_count:
        projected_errors["other"] = f"{unknown_error_count} unrecognized error section(s) omitted"
    for section, (changed, omitted) in collection_projection_evidence.items():
        _add_projection_error(projected_errors, section, changed=changed, omitted=omitted)
    _add_projection_error(
        projected_errors, "alerts", changed=alert_projection_changes,
        omitted=alert_projection_omissions,
    )
    _add_projection_error(
        projected_errors, "capacity", changed=capacity_projection_changes,
        omitted=capacity_projection_omissions,
    )
    _add_projection_error(projected_errors, "projection", omitted=top_level_omissions)
    host_fields = frozenset({*host_string_fields, "observed_at"})
    projected_host = {"host_id": _wire_identity(
        host["host_id"], "host host_id", MAX_HOST_ID_LENGTH, True,
    )}
    host_projection_changes = 0
    for key in host_string_fields:
        if key == "host_id":
            continue
        projected_host[key] = _wire_text(
            host[key], f"host {key}",
            HOST_STRING_LIMITS[key],
            True,
        )
        host_projection_changes += projected_host[key] != host[key]
    _add_projection_error(
        projected_errors, "host", changed=host_projection_changes,
        omitted=len(set(host) - host_fields),
    )
    projected_host["observed_at"] = observed_at
    projected = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at,
                 "partial": bool(snapshot["partial"] or projected_capacity.get("partial") is True
                                 or projected_capacity.get("error") or projected_errors
                                 or collection_projection_partial),
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
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
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
    if failure:
        raise ValueError(failure)
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() or f"ssh exited {process.returncode}"
        raise ValueError(_redact_error(detail))
    return _validate_snapshot(bytes(stdout))


def _prefix_identities(snapshot: dict) -> dict:
    host_id = snapshot["host"]["host_id"]
    for collection, keys in (("sessions", ("source", "session_id")),
                             ("agents", ("source", "parent_session_id", "agent_id")),
                             ("tasks", ("source", "session_id", "task_id")),
                             ("managed_processes", ("managed_process_id",)),
                             ("inferred_processes", ("boot_id", "pid", "process_start_time"))):
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
                    "error": _redact_error(str(error))}

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
        record.pop("_received_at")
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
            # during wire validation. Recompute against the later fleet timestamp so
            # a reservation that legitimately expired in transit becomes inactive.
            capacity["reservation_active"] = capacity["reserved_until"] > generated_at
        state, reasons = classify(capacity, configured_hosts[record["name"]].thresholds)
        capacity.update(capacity_state=state, capacity_reasons=reasons)
        snapshot["alerts"] = [
            alert for alert in snapshot["alerts"] if alert.get("kind") not in CAPACITY_ALERT_KINDS
        ]
        if snapshot["host"] and capacity:
            snapshot["alerts"].extend(capacity_alerts(
                snapshot["host"], capacity, configured_hosts[record["name"]].thresholds,
                generated_at,
            ))
        if isinstance(sample_age, (int, float)) and sample_age > max(15_000, config.refresh_seconds * 3000):
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
        alert_history.track([
            alert
            for record in result["hosts"]
            for alert in record["alerts"] + (record.get("snapshot", {}).get("alerts", []) if record["ok"] else [])
        ])
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
