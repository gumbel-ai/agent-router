"""Content-free fleet correlation pages over the configured local/SSH transport."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from router.correlation import (
    IDENTITIES,
    MAX_BYTES,
    NUMBERS,
    TABLES,
    _position,
    identifier,
    validate_metadata,
)
from router.db import now_ms
from router.dispatch import canonical
from router.fleet import (
    MAX_FLEET_OUTPUT_BYTES,
    MAX_HOSTS,
    FleetConfig,
    SSHQueryError,
    ssh_stdout,
)
from router.validation import is_wire_integer

KINDS = frozenset(kind for _, kind in TABLES)
DIAGNOSTIC_FIELDS = frozenset(("host_id", *IDENTITIES, *NUMBERS, "status", "lifecycle_event", "record", "source", "task_id"))


def _require(condition: bool, field: str) -> None:
    if not condition:
        raise ValueError(f"Invalid correlation response field: {field}")


def _page(raw: bytes, limit: int, cursor: str | None) -> dict:
    _require(len(raw) <= MAX_BYTES, "bytes")
    try:
        page = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("Invalid correlation response JSON") from error
    _require(isinstance(page, dict), "envelope")
    _require(type(page.get("schema_version")) is int and page["schema_version"] == 1, "schema_version")
    host_id = identifier(page.get("host_id"), "host_id", 128)
    _require(is_wire_integer(page.get("generated_at")), "generated_at")
    _require(page.get("status_scope") == "recorded", "status_scope")
    _require(type(page.get("partial")) is bool, "partial")
    _require(is_wire_integer(page.get("invalid_records")) and page["invalid_records"] <= limit, "invalid_records")
    _require(bool(page["invalid_records"]) == page["partial"], "partial")
    records = page.get("records")
    _require(isinstance(records, list) and len(records) <= limit, "records")
    projected, wire_errors = [], []
    wire_error_count = 0

    def note(section, index, fields):
        nonlocal wire_error_count
        wire_error_count += 1
        if len(wire_errors) < 10:
            wire_errors.append({"section": section, "index": index, "fields": fields})

    for index, record in enumerate(records):
        kind = record.get("kind") if isinstance(record, dict) else None
        if not isinstance(kind, str) or kind not in KINDS:
            note("records", index, ["kind"])
            continue
        invalid = []
        fields = ("host_id", *IDENTITIES, *NUMBERS, "status", "lifecycle_event")
        safe = {"kind": kind, "traced": "unknown", **{field: record.get(field) for field in fields}}
        if record.get("traced", "unknown") != "unknown":
            invalid.append("traced")
        invalid.extend(validate_metadata(safe))
        if safe["host_id"] not in {None, host_id}:
            note("records", index, ["host_id"])
            continue
        if invalid:
            note("records", index, invalid)
        projected.append(safe)
    errors = page.get("errors")
    _require(isinstance(errors, list) and len(errors) <= min(10, page["invalid_records"]), "errors")
    safe_errors = []
    for index, error in enumerate(errors):
        try:
            _require(isinstance(error, dict), "errors")
            kind, row, fields = error.get("kind"), error.get("row"), error.get("fields")
            _require(isinstance(kind, str) and kind in KINDS and is_wire_integer(row, minimum=1), "errors.identity")
            _require(isinstance(fields, list) and 0 < len(fields) <= len(DIAGNOSTIC_FIELDS)
                     and all(isinstance(field, str) and field in DIAGNOSTIC_FIELDS for field in fields), "errors.fields")
        except ValueError:
            note("errors", index, ["diagnostic"])
            continue
        safe_errors.append({"kind": kind, "row": row, "fields": fields})
    _require("next_cursor" in page, "next_cursor")
    scope = hashlib.sha256(host_id.encode()).hexdigest()
    previous = _position(cursor, scope)
    next_cursor = page["next_cursor"]
    if next_cursor is not None:
        _require(_position(next_cursor, scope) > previous, "next_cursor")
    result = {"schema_version": 1, "host_id": host_id, "generated_at": page["generated_at"],
              "status_scope": "recorded", "partial": page["partial"] or bool(wire_error_count),
              "invalid_records": page["invalid_records"], "wire_error_count": wire_error_count, "wire_errors": wire_errors,
              "records": projected, "errors": safe_errors, "next_cursor": next_cursor}
    _require(len(canonical(result).encode()) <= MAX_BYTES, "projected_bytes")
    return result


def query_correlation(config: FleetConfig, *, only: str | None = None, limit: int = 100,
                      cursor: str | None = None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Correlation limit must be an integer from 1 to 100")
    if cursor is not None and (only is None or not isinstance(cursor, str) or not 0 < len(cursor) <= 512):
        raise ValueError("A bounded correlation cursor requires --host NAME")
    selected = [host for host in config.hosts if only is None or host.name == only]
    if not 1 <= len(selected) <= MAX_HOSTS:
        raise ValueError("Correlation requires one to 32 selected configured hosts")
    for host in selected:
        identifier(host.name, "configured host name", 120)
    options = {"limit": limit, **({"cursor": cursor} if cursor is not None else {})}

    def query(host):
        wrapper = {"name": host.name, "host_id": None, "ok": False}
        try:
            if host.transport == "local":
                from router.client import request
                raw = json.dumps(request("correlation", options, timeout=config.ssh_timeout_seconds, deadline=True),
                                 separators=(",", ":")).encode()
            else:
                arguments = ["correlation", "--json", "--limit", str(limit)]
                if cursor is not None:
                    arguments.extend(["--cursor", cursor])
                raw = ssh_stdout(host, config.ssh_timeout_seconds, arguments)
        except SSHQueryError as error:
            detail = {"code": error.code}
            for field in ("exit_code", "errno"):
                if getattr(error, field) is not None:
                    detail[field] = getattr(error, field)
            return {**wrapper, "error": detail}
        except TimeoutError:
            return {**wrapper, "error": {"code": "deadline"}}
        except OSError as error:
            return {**wrapper, "error": {"code": "transport_failed", "errno": error.errno}}
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError, RecursionError, UnicodeError):
            return {**wrapper, "error": {"code": "invalid_response"}}
        except ValueError as error:
            cause = error.__cause__
            return {**wrapper, "error": ({"code": "unavailable", "errno": cause.errno}
                                         if isinstance(cause, OSError) else {"code": "request_failed"})}
        try:
            page = _page(raw, limit, cursor)
        except ValueError as error:
            return {**wrapper, "error": {"code": "invalid_response", "detail": str(error)}}
        return {**wrapper, "ok": True, "host_id": page["host_id"], "page": page}

    responses = {}
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(query, host): host.name for host in selected}
        for future in as_completed(futures):
            responses[futures[future]] = future.result()
    hosts = [responses[host.name] for host in selected]
    identities = {}
    for host in hosts:
        if host["ok"]:
            identities.setdefault(host["host_id"], []).append(host)
    for group in identities.values():
        if len(group) > 1:
            for host in group:
                host.pop("page")
                host.update(ok=False, error={"code": "duplicate_host_id"})
    result = {"schema_version": 1, "generated_at": now_ms(), "status_scope": "recorded", "hosts": hosts,
              "partial": any(not host["ok"] or host["page"]["partial"] for host in hosts)}
    if len(canonical(result).encode()) > MAX_FLEET_OUTPUT_BYTES:
        raise ValueError("Fleet correlation exceeds the bounded output size")
    return result
