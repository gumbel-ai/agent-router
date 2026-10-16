"""Fleet configuration, bounded local/SSH queries, and merged read-only views."""

import hashlib
import json
import math
import os
import selectors
import subprocess
import time
import tomllib
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

DEFAULT_CONFIG = Path("~/.agent-router/fleet.toml").expanduser()
MAX_HOSTS = 32
MAX_COLLECTION_RECORDS = 1000
MAX_HOST_ID_LENGTH = 128
MAX_ROUTER_VERSION_LENGTH = 120
MAX_IDENTITY_LENGTH = 240
MINIMUM_CLAUDE_VERSION = (2, 1, 292)
_THRESHOLD_NAMES = {item.name for item in fields(CapacityThresholds)}


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


def _positive_number(value, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def load_fleet_config(path: Path = DEFAULT_CONFIG) -> FleetConfig:
    try:
        values = tomllib.loads(path.read_text())
    except OSError as error:
        raise ValueError(f"Cannot read fleet configuration {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Invalid fleet TOML: {error}") from error
    allowed = {"schema_version", "ssh_timeout_seconds", "refresh_seconds", "capacity_defaults", "host"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("Unknown fleet configuration fields: " + ", ".join(sorted(unknown)))
    if values.get("schema_version") != 1:
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
        name, transport = host.get("name"), host.get("transport")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Each fleet host needs a unique non-empty name")
        if transport not in {"local", "ssh"}:
            raise ValueError(f"Host {name} transport must be local or ssh")
        target = host.get("target")
        if transport == "ssh" and (not isinstance(target, str) or not target.strip()):
            raise ValueError(f"SSH host {name} requires target")
        if transport == "local":
            local_count += 1
            if target is not None:
                raise ValueError(f"Local host {name} cannot set target")
        tags = host.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags):
            raise ValueError(f"Host {name} tags must be non-empty strings")
        required = host.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(f"Host {name} required must be true or false")  # noqa: TRY004 - public config error
        threshold_values = {**defaults, **{key: value for key, value in host.items() if key in _THRESHOLD_NAMES}}
        for key, value in threshold_values.items():
            _positive_number(value, key)
        thresholds = CapacityThresholds(**threshold_values)
        if (thresholds.maximum_cpu_busy_for_saturated_pct < thresholds.maximum_cpu_busy_for_available_pct
                or thresholds.maximum_load_per_logical_cpu_for_saturated < thresholds.maximum_load_per_logical_cpu_for_available
                or thresholds.minimum_available_memory_gib_for_saturated > thresholds.minimum_available_memory_gib
                or thresholds.minimum_disk_available_gib_for_saturated > thresholds.minimum_disk_available_gib):
            raise ValueError(f"Host {name} saturated thresholds must be more severe than available thresholds")
        result.append(FleetHost(name=name, transport=transport, target=target, tags=tuple(tags), required=required,
                                thresholds=thresholds))
        names.add(name)
    if local_count > 1:
        raise ValueError("fleet.toml can define only one local host")
    return FleetConfig(tuple(result), _positive_number(values.get("ssh_timeout_seconds", 3), "ssh_timeout_seconds"),
                       _positive_number(values.get("refresh_seconds", 2), "refresh_seconds"))


def _redact_error(value: str) -> str:
    return redact_secrets(value)


def _nonnegative_number(value: object, name: str, maximum: float | None = None) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Host snapshot {name} must be a finite nonnegative number")
    if maximum is not None and value > maximum:
        raise ValueError(f"Host snapshot {name} exceeds {maximum:g}")


def _validate_snapshot(payload: bytes) -> dict:
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Host snapshot exceeds one MiB")
    try:
        snapshot = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Host returned invalid snapshot JSON") from error
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("schema_version"), int)
            or isinstance(snapshot.get("schema_version"), bool) or snapshot["schema_version"] != SCHEMA_VERSION):
        raise ValueError("Host returned an unsupported snapshot schema")
    generated_at = snapshot.get("generated_at")
    if not isinstance(generated_at, int) or isinstance(generated_at, bool) or generated_at < 0:
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
    if (len(host["host_id"]) > MAX_HOST_ID_LENGTH
            or len(host["router_version"]) > MAX_ROUTER_VERSION_LENGTH
            or any(len(host[key]) > MAX_IDENTITY_LENGTH for key in host_string_fields[1:])):
        raise ValueError("Host snapshot identity fields are too long")
    observed_at = host.get("observed_at")
    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at < 0:
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
    for collection, required in identity_fields.items():
        for record in snapshot[collection]:
            if any(not isinstance(record.get(key), str) or not record[key]
                   or len(record[key]) > MAX_IDENTITY_LENGTH for key in required):
                raise ValueError(f"Host snapshot has invalid {collection} identity")
            if record["host_id"] != host["host_id"]:
                raise ValueError(f"Host snapshot has mismatched {collection} host identity")
    for alert in snapshot["alerts"]:
        if (not isinstance(alert.get("alert_id"), str) or not alert["alert_id"]
                or len(alert["alert_id"]) > MAX_IDENTITY_LENGTH):
            raise ValueError("Host snapshot has invalid alert identity")
        if alert.get("host_id") != host["host_id"]:
            raise ValueError("Host snapshot alert has mismatched host identity")
        for key in ("severity", "kind", "evidence"):
            value = alert.get(key)
            if not isinstance(value, str) or not value or len(value) > 1000:
                raise ValueError(f"Host snapshot alert has invalid {key}")
        for key in ("first_seen_at", "last_seen_at"):
            value = alert.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Host snapshot alert has invalid {key}")
        if alert["first_seen_at"] > alert["last_seen_at"]:
            raise ValueError("Host snapshot alert timestamps are out of order")
        if not isinstance(alert.get("entity"), dict):
            raise ValueError("Host snapshot alert has invalid entity")  # noqa: TRY004 - public wire validation
    if capacity:
        logical_cpus = capacity.get("logical_cpus")
        if logical_cpus is not None and (not isinstance(logical_cpus, int) or isinstance(logical_cpus, bool) or logical_cpus <= 0):
            raise ValueError("Host snapshot logical_cpus must be a positive integer")
        for key in ("load_1m", "load_5m", "memory_total_bytes", "memory_available_bytes",
                    "swap_used_bytes", "disk_available_bytes", "sample_age_ms", "observed_at",
                    "active_claude_sessions", "active_agents", "active_managed_processes"):
            if key in capacity and capacity[key] is not None:
                _nonnegative_number(capacity[key], key)
        for key in ("reserved_until", "reserved_cpu_slots", "reserved_memory_bytes"):
            if capacity.get(key) is not None and (
                    not isinstance(capacity[key], int) or isinstance(capacity[key], bool) or capacity[key] < 0):
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
    return snapshot


def ssh_snapshot(host: FleetHost, timeout: float) -> dict:
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, math.ceil(timeout))}",
               "-n", "--", host.target, "router", "snapshot", "--json"]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise ValueError(str(error)) from error
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise ValueError("ssh output pipes unavailable")
    selector = selectors.DefaultSelector()
    streams = {process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"}
    for stream in (process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    failure = None
    try:
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
    finally:
        if failure and process.poll() is None:
            process.kill()
        cleanup_deadline = time.monotonic() + 1
        while selector.get_map() and time.monotonic() < cleanup_deadline:
            events = selector.select(min(.05, cleanup_deadline - time.monotonic()))
            if not events and process.poll() is not None:
                break
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                elif streams[key.fd] == "stderr" and len(stderr) < 65_536:
                    stderr.extend(chunk[:65_536 - len(stderr)])
        selector.close()
        process.stdout.close()
        process.stderr.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
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
                             ("tasks", ("source", "session_id", "task_id"))):
        for record in snapshot.get(collection, []):
            if isinstance(record, dict):
                parts = [host_id] + [str(record.get(key) or "unknown") for key in keys]
                record["fleet_id"] = ":".join(parts)
    return snapshot


def _fleet_alert(record: dict, kind: str, evidence: str, at: int) -> dict:
    host_id = record.get("snapshot", {}).get("host", {}).get("host_id")
    identity = host_id or record["name"]
    alert_id = hashlib.sha256(f"{identity}\0{kind}".encode()).hexdigest()[:24]
    return {"alert_id": alert_id, "host_id": host_id, "severity": "warning", "kind": kind,
            "first_seen_at": at, "last_seen_at": at, "entity": {"configured_host": record["name"]},
            "evidence": evidence}


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
            capacity = snapshot.get("capacity")
            if isinstance(capacity, dict):
                state, reasons = classify(capacity, host.thresholds)
                capacity.update(capacity_state=state, capacity_reasons=reasons)
                local_alerts = [item for item in snapshot["alerts"]
                                if item.get("kind") not in CAPACITY_ALERT_KINDS]
                if snapshot["host"] and capacity:
                    local_alerts.extend(capacity_alerts(snapshot["host"], capacity, host.thresholds))
                snapshot["alerts"] = local_alerts
            snapshot = _prefix_identities(snapshot)
            if len(json.dumps(snapshot, separators=(",", ":")).encode()) > MAX_SNAPSHOT_BYTES:
                raise ValueError("Host snapshot exceeds one MiB after fleet identity augmentation")
            return {**wrapper, "ok": True, "latency_ms": round((time.monotonic() - started) * 1000),
                    "snapshot": snapshot}
        except (OSError, ValueError, KeyError, TypeError, RecursionError, UnicodeError, OverflowError) as error:
            return {**wrapper, "ok": False, "latency_ms": round((time.monotonic() - started) * 1000),
                    "error": _redact_error(str(error))}

    records = {}
    with ThreadPoolExecutor(max_workers=min(len(selected), MAX_HOSTS)) as pool:
        futures = {pool.submit(query, host): host.name for host in selected}
        for future in as_completed(futures):
            records[futures[future]] = future.result()
    hosts = [records[host.name] for host in selected]
    generated_at = int(time.time() * 1000)
    versions = {record["snapshot"]["host"].get("router_version") for record in hosts if record["ok"]}
    version_mismatch = len(versions) > 1
    for record in hosts:
        if not record["ok"]:
            record["alerts"].append(_fleet_alert(record, "host_offline", record["error"], generated_at))
            continue
        snapshot = record["snapshot"]
        sample_age = snapshot.get("capacity", {}).get("sample_age_ms")
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
    if alert_history is not None:
        alert_history.track([
            alert
            for record in hosts
            for alert in record["alerts"] + (record.get("snapshot", {}).get("alerts", []) if record["ok"] else [])
        ])
    return {"schema_version": SCHEMA_VERSION, "generated_at": generated_at,
            "partial": any(not host["ok"] or host["snapshot"].get("partial") for host in hosts),
            "version_mismatch": version_mismatch, "hosts": hosts}


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
                                "partial": source["partial"]}
        projected["hosts"].append(host)
    return projected
