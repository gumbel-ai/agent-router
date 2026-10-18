"""Bounded, content-free pages of durable Router correlation metadata."""

import base64
import binascii
import hashlib
import json

from router.db import now_ms
from router.dispatch import canonical
from router.native import identifier
from router.validation import is_wire_integer

MAX_BYTES = 1024 * 1024
ENVELOPE_RESERVE = 16 * 1024
TABLES = (("sessions", "session"), ("observations", "agent"), ("session_tasks", "work_item"),
          ("tasks", "router_task"), ("attempts", "attempt"), ("managed_processes", "managed"),
          ("completion_events", "completion"))
IDENTITIES = ("runtime", "model_provider", "session_id", "agent_id", "parent_session_id",
              "parent_agent_id", "parent_runtime", "router_task_id", "managed_id", "work_item_id", "event_id")
NUMBERS = ("timestamp", "created_at", "updated_at", "started_at", "finished_at", "uncertain_at",
           "attempt_number", "attempts", "review_retries", "continuations")
STATUSES = frozenset({"unknown", "running", "idle", "stopped", "failed", "needs_input", "created", "completed",
                      "validating", "preparing", "retrying", "cancelling", "cancelled", "awaiting_review", "blocked",
                      "accepted", "integrated", "closed", "exited", "signaled", "uncertain", "interrupted"})
EVENTS = frozenset({"session_start", "session_end", "prompt", "stop", "stop_failure", "tool_start", "tool_end",
                    "tool_failure", "notification", "permission_request", "subagent_start", "subagent_stop",
                    "subagent_activity", "teammate_idle", "task_created", "task_completed", "finished", "uncertain",
                    "start", "launch", "result", "activity", "idle", "needs_input", "failed"})


def _position(token: object, scope: str) -> tuple[int, int]:
    if token is None:
        return 0, 0
    if not isinstance(token, str) or len(token) > 512:
        raise ValueError("Invalid correlation cursor")
    try:
        value = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
    except (ValueError, binascii.Error, UnicodeError, RecursionError) as error:
        raise ValueError("Invalid correlation cursor") from error
    if (not isinstance(value, dict) or set(value) != {"version", "host", "table", "row"}
            or type(value["version"]) is not int or value["version"] != 1 or value["host"] != scope
            or type(value["table"]) is not int or not 0 <= value["table"] < len(TABLES)
            or not is_wire_integer(value["row"])):
        raise ValueError("Invalid or foreign correlation cursor")
    if token != _cursor(scope, (value["table"], value["row"])):
        raise ValueError("Non-canonical correlation cursor")
    return value["table"], value["row"]


def _cursor(scope: str, position: tuple[int, int]) -> str:
    return base64.urlsafe_b64encode(canonical({"version": 1, "host": scope,
                                             "table": position[0], "row": position[1]}).encode()).decode()


def _record(kind: str, row, data: dict, host_id: str) -> tuple[dict, list[str]]:
    authoritative = {}
    invalid = []
    result = {"kind": kind, "host_id": data.get("host_id"), "traced": "unknown",
              **dict.fromkeys((*IDENTITIES, *NUMBERS, "status", "lifecycle_event"))}
    for field in ("model_provider", "status", "created_at", "updated_at", "started_at", "finished_at", "uncertain_at"):
        result[field] = data.get(field)
    result.update(runtime=data.get("source"), timestamp=data.get("last_event_at"),
                  lifecycle_event=data.get("last_event"))
    if kind in {"session", "work_item"}:
        result.update(host_id=row["host_id"], runtime=row["source"], session_id=row["session_id"])
        authoritative = {"host_id": row["host_id"], "source": row["source"], "session_id": row["session_id"]}
        if kind == "session":
            result["parent_session_id"] = data.get("parent_session_id")
        if kind == "work_item":
            result["work_item_id"] = row["task_id"]
            result["agent_id"] = data.get("agent_id")
            result["finished_at"] = data.get("completed_at")
            authoritative["task_id"] = row["task_id"]
    elif kind == "agent":
        result.update(session_id=row["session_id"], agent_id=data.get("agent_id"),
                      parent_session_id=row["session_id"], parent_agent_id=data.get("parent_agent_id"))
        authoritative = {"task_id": row["task_id"], "parent_session_id": row["session_id"]}
    elif kind in {"router_task", "attempt"}:
        matching_parent = row["submission_session_id"] == row["parent_session_id"]
        result.update(host_id=data.get("host_id", host_id), runtime=data.get("executor"), session_id=data.get("child_session_id"),
                      parent_session_id=row["parent_session_id"], parent_runtime=row["parent_runtime"] if matching_parent else None,
                      parent_agent_id=data.get("parent_agent_id"), router_task_id=row["task_id"],
                      started_at=data.get("attempt_started_at"))
        if not matching_parent and row["submission_session_id"] is not None:
            invalid.append("parent_runtime")
        authoritative = {"task_id": row["task_id"]}
        if kind == "router_task":
            authoritative["session_id"] = row["parent_session_id"]
        for field in ("attempt_number", "attempts", "review_retries", "continuations"):
            result[field] = data.get(field)
        if kind == "attempt":
            result["attempt_number"] = row["number"]
            authoritative["attempt_number"] = row["number"]
    elif kind == "managed":
        result.update(runtime=None, managed_id=row["managed_id"], session_id=data.get("session_id"),
                      agent_id=data.get("agent_id"))
        authoritative = {"managed_id": row["managed_id"]}
    elif kind == "completion":
        result.update(host_id=row["host_id"], runtime=None, managed_id=row["managed_id"],
                      event_id=f"completion-{row['sequence']}", timestamp=row["created_at"], created_at=row["created_at"],
                      lifecycle_event=row["phase"], session_id=data.get("session_id"), agent_id=data.get("agent_id"))
        authoritative = {"host_id": row["host_id"], "managed_id": row["managed_id"], "event_id": result["event_id"]}
    invalid.extend(field for field, value in authoritative.items() if data.get(field) is not None
                   and (type(data[field]) is not type(value) or data[field] != value))
    for field in ("host_id", *IDENTITIES):
        if result[field] is not None:
            try:
                result[field] = identifier(result[field], field, 128 if field == "host_id" else 240)
            except (ValueError, TypeError):
                result[field] = None
                invalid.append(field)
    for field in NUMBERS:
        if result[field] is not None and not is_wire_integer(result[field]):
            result[field] = None
            invalid.append(field)
    for field, allowed in (("status", STATUSES), ("lifecycle_event", EVENTS)):
        if result[field] is not None and (not isinstance(result[field], str) or result[field] not in allowed):
            result[field] = None
            invalid.append(field)
    return result, invalid


def export(store, host_id: str, options: dict) -> dict:
    """Read one inventory page; cursors never subscribe to or acknowledge events."""
    if set(options) - {"limit", "cursor"}:
        raise ValueError("Unknown correlation option")
    limit = options.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Correlation limit must be an integer from 1 to 100")
    host_id = identifier(host_id, "host_id", 128)
    scope = hashlib.sha256(host_id.encode()).hexdigest()
    table_index, after = _position(options.get("cursor"), scope)
    position = (table_index, after)
    result = {"schema_version": 1, "host_id": host_id, "generated_at": now_ms(), "status_scope": "recorded",
              "partial": False, "records": [], "next_cursor": None, "errors": [], "invalid_records": 0}
    used = 0
    remaining = limit
    with store.snapshot() as db:
        db.execute("PRAGMA query_only=ON")
        for index in range(table_index, len(TABLES)):
            table, kind = TABLES[index]
            query = f"SELECT t.rowid AS position,t.* FROM {table} t WHERE t.rowid>? ORDER BY t.rowid LIMIT ?"
            if kind in {"router_task", "attempt"}:
                owner = "t" if kind == "router_task" else "p"
                join = "" if kind == "router_task" else "LEFT JOIN tasks p ON p.task_id=t.task_id"
                query = f"""SELECT t.rowid AS position,t.*,{owner}.session_id AS parent_session_id,
                    s.parent_cli AS parent_runtime,s.session_id AS submission_session_id FROM {table} t {join}
                    LEFT JOIN submissions s ON s.instruction_id=CASE WHEN json_valid({owner}.data)
                        THEN json_extract({owner}.data,'$.instruction_id') END
                    WHERE t.rowid>? ORDER BY t.rowid LIMIT ?"""
            rows = db.execute(query, (after if index == table_index else 0, remaining + 1)).fetchall()
            for row in rows:
                if remaining == 0:
                    result["next_cursor"] = _cursor(scope, position)
                    return result
                remaining -= 1
                try:
                    data = json.loads(row["data"]) if row["data"] is not None else {}
                    if not isinstance(data, dict):
                        raise TypeError("Correlation record must be an object")
                    if kind == "completion":
                        data = {**data.get("event", {}), "status": data.get("status")}
                    record, invalid = _record(kind, row, data, host_id)
                except (ValueError, TypeError, RecursionError, UnicodeError):
                    record, invalid = None, ["record"]
                if record is not None and record["host_id"] not in {None, host_id}:
                    record, invalid = None, ["host_id"]
                encoded_size = len(canonical(record).encode()) if record is not None else 0
                if used + encoded_size > MAX_BYTES - ENVELOPE_RESERVE:
                    result["next_cursor"] = _cursor(scope, position)
                    return result
                position = (index, row["position"])
                if invalid:
                    result["partial"] = True
                    result["invalid_records"] += 1
                    if len(result["errors"]) < 10:
                        result["errors"].append({"kind": kind, "row": row["position"], "fields": invalid})
                if record is not None:
                    result["records"].append(record)
                    used += encoded_size
    return result
