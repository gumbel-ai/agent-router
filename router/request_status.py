"""Read durable request bindings without authorizing, submitting or retrying work."""

import json

from router.correlation import identifier
from router.dispatch import MUTATIONS

MAX_WIRE_BYTES = 16 * 1024
ERRORS = frozenset({"identity_mismatch", "binding_invalid", "invalid_response", "request_failed",
                    "transport_unavailable", "transport_deadline", "ssh_spawn_failed", "ssh_exit_failed",
                    "ssh_deadline", "ssh_output_limit", "ssh_transport_failed", "ssh_interrupted", "ssh_cleanup_failed"})


def options(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"session_id", "request_id", "host_id"}:
        raise ValueError("Unknown request-status option")
    result = {field: identifier(payload.get(field), field) for field in ("session_id", "request_id")}
    if "host_id" in payload:
        result["host_id"] = identifier(payload["host_id"], "host_id", 128)
    return result


def unknown(selection: dict, code: str, host_id: str | None = None) -> dict:
    return {"schema_version": 1, "status": "unknown", "host_id": host_id,
            "session_id": selection["session_id"], "request_id": selection["request_id"], "error": {"code": code}}


def export(store, host_id: str, payload: dict) -> dict:
    selection = options(payload)
    identifier(host_id, "host_id", 128)
    if selection.get("host_id", host_id) != host_id:
        return unknown(selection, "identity_mismatch", host_id)
    result = {"schema_version": 1, "host_id": host_id, "session_id": selection["session_id"],
              "request_id": selection["request_id"]}
    with store.snapshot() as db:
        row = db.execute("SELECT operation,task_id FROM operations WHERE session_id=? AND request_id=?",
                         (selection["session_id"], selection["request_id"])).fetchone()
        if row is None:
            return {**result, "status": "not_found"}
        try:
            task_id = identifier(row["task_id"], "task_id", 128)
        except ValueError:
            return unknown(selection, "binding_invalid", host_id)
        task = db.execute("SELECT session_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row["operation"] not in MUTATIONS or task is None or task[0] != selection["session_id"]:
            return unknown(selection, "binding_invalid", host_id)
        return {**result, "status": "found", "operation": row["operation"], "task_id": task_id}


class RequestStatusProtocolError(ValueError):
    """A bounded request-status reply failed framing, JSON or envelope validation."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate request-status field")
        result[key] = value
    return result


def _number(_value):
    raise ValueError("Request-status numbers must be integers")


def parse_json(raw: bytes):
    if not isinstance(raw, bytes) or len(raw) > MAX_WIRE_BYTES:
        raise RequestStatusProtocolError("Request-status reply exceeds its wire bound")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_float=_number, parse_constant=_number)
    except (ValueError, RecursionError) as error:
        raise RequestStatusProtocolError("Invalid request-status JSON") from error


def decode(raw: bytes, selection: dict) -> dict:
    result = parse_json(raw)
    common = {"schema_version", "host_id", "session_id", "request_id", "status"}
    if (not isinstance(result, dict) or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1
            or any(result.get(field) != selection[field] for field in ("session_id", "request_id"))):
        raise RequestStatusProtocolError("Invalid request-status version or request identity")
    host_id = result.get("host_id")
    if host_id is not None:
        identifier(host_id, "host_id", 128)
    if result.get("status") == "unknown":
        error = result.get("error")
        if (set(result) != common | {"error"} or not isinstance(error, dict) or set(error) != {"code"}
                or not isinstance(error["code"], str) or error["code"] not in ERRORS
                or ("host_id" in selection and host_id not in {None, selection["host_id"]}
                    and error["code"] != "identity_mismatch")):
            raise RequestStatusProtocolError("Invalid request-status error receipt")
        return result
    identifier(host_id, "host_id", 128)
    if selection.get("host_id", host_id) != host_id:
        raise RequestStatusProtocolError("Request-status serving host mismatch")
    if result.get("status") == "not_found" and set(result) == common:
        return result
    if (result.get("status") != "found" or set(result) != common | {"operation", "task_id"}
            or not isinstance(result["operation"], str) or result["operation"] not in MUTATIONS):
        raise RequestStatusProtocolError("Invalid request-status binding")
    identifier(result["task_id"], "task_id", 128)
    return result


def fetch(payload: dict, *, host=None, timeout: float = 15) -> dict:
    from router.client import request
    from router.fleet import SSHQueryError, ssh_stdout

    selection = options(payload)
    if host is not None and ("host_id" not in selection or host.transport not in {"local", "ssh"}):
        raise ValueError("Fleet request-status requires an expected host_id and local or SSH transport")
    try:
        if host is None or host.transport == "local":
            response = request("request_status", selection, timeout=timeout, deadline=True)
            raw = json.dumps(response, separators=(",", ":"), allow_nan=False).encode()
        else:
            raw = ssh_stdout(host, timeout, ["request-status", "--session=" + selection["session_id"],
                                           "--request-id=" + selection["request_id"], "--host-id=" + selection["host_id"], "--json"])
    except SSHQueryError as error:
        if error.code == "exit_failed" and error.exit_code == 1 and error.stdout is not None:
            try:
                result = decode(error.stdout, selection)
            except ValueError:
                return unknown(selection, "invalid_response")
            return result if result["status"] == "unknown" else unknown(selection, "invalid_response")
        code = "ssh_" + error.code
        return unknown(selection, code if code in ERRORS else "transport_unavailable")
    except RequestStatusProtocolError:
        return unknown(selection, "invalid_response")
    except TimeoutError:
        return unknown(selection, "transport_deadline")
    except OSError:
        return unknown(selection, "transport_unavailable")
    except ValueError as error:
        if isinstance(error.__cause__, TimeoutError):
            return unknown(selection, "transport_deadline")
        return unknown(selection, "transport_unavailable" if isinstance(error.__cause__, OSError) else "request_failed")
    try:
        return decode(raw, selection)
    except ValueError:
        return unknown(selection, "invalid_response")
