"""Durable admission for an existing privileged local or SSH account.

Origin identifiers bind replay and ownership; they do not authenticate the caller.
Only the existing account boundary may call this receiver. No native submission or
authorization is invented, and operator configuration stays on the receiving host.
"""

import hashlib
import uuid
from pathlib import Path

from router.correlation import identifier
from router.db import now_ms
from router.dispatch import canonical
from router.remote_snapshot import MAX_FILES, _paths, import_snapshot

MAX_REQUEST_BYTES = 128 * 1024
IDENTITY_FIELDS = ("receiver_host_id", "origin_host_id", "origin_session_id", "request_id")


def _identity(payload):
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("Remote request requires schema_version 1")
    return {field: identifier(payload.get(field), field, 128 if field.endswith("host_id") else 240)
            for field in IDENTITY_FIELDS}


def _scope(origin):
    identity = canonical([origin["origin_host_id"], origin["origin_session_id"]]).encode()
    return "remote-" + hashlib.sha256(identity).hexdigest()


def origin_metadata(task):
    """Keep cross-host ownership explicit instead of inventing a local native join."""
    if "remote_origin" not in task:
        return None
    origin = task["remote_origin"]
    required = {"origin_host_id", "origin_session_id", "request_id"}
    if not isinstance(origin, dict) or set(origin) - required - {"parent_agent_id"} or required - origin.keys():
        raise ValueError("Invalid remote origin metadata")
    return {field: identifier(value, field, 128 if field.endswith("host_id") else 240)
            for field, value in origin.items()}


class RemoteReceiver:
    def __init__(self, control, validate):
        self.control = control
        self.validate = validate

    def _selection(self, payload):
        if not isinstance(payload, dict) or not payload:
            raise ValueError("Remote request must be an object")
        origin = _identity(payload)
        if origin["receiver_host_id"] != self.control.host.host_id:
            raise ValueError("Remote request targets another receiving host")
        return origin

    def _lookup(self, db, origin):
        return db.execute("SELECT operation,payload,task_id FROM remote_operations "
                          "WHERE origin_host_id=? AND origin_session_id=? AND request_id=?",
                          tuple(origin[field] for field in IDENTITY_FIELDS[1:])).fetchone()

    def _result(self, db, origin, row):
        common = {"schema_version": 1, **origin}
        if row is None:
            return {**common, "status": "not_found"}
        task = self.control.store.get(row["task_id"], db)
        expected = {field: origin[field] for field in IDENTITY_FIELDS[1:]}
        owner = origin_metadata(task)
        if (row["operation"] != "start" or owner is None or any(owner.get(field) != value for field, value in expected.items())
                or task["session_id"] != _scope(origin)):
            raise ValueError("Remote operation has an invalid task ownership binding")
        return {**common, "status": "found", "operation": "start", "task_id": task["task_id"],
                "task_status": task["status"], "snapshot_commit": task["remote_snapshot"]["snapshot_commit"],
                "snapshot_tree": task["remote_snapshot"]["snapshot_tree"]}

    def lookup(self, payload):
        origin = self._selection(payload)
        if set(payload) != {"schema_version", *IDENTITY_FIELDS}:
            raise ValueError("Unknown remote lookup field")
        with self.control.store.snapshot() as db:
            return self._result(db, origin, self._lookup(db, origin))

    def start(self, payload, bundle_path):
        origin = self._selection(payload)
        allowed = {"schema_version", *IDENTITY_FIELDS, "operation", "repository", "executor", "model",
                   "task", "snapshot", "context", "work_item_id", "parent_agent_id", "result_paths"}
        required = allowed - {"context", "work_item_id", "parent_agent_id", "result_paths"}
        if set(payload) - allowed or required - payload.keys() or payload["operation"] != "start":
            raise ValueError("Invalid remote start fields or operation")
        if not isinstance(payload["snapshot"], dict) or not payload["snapshot"]:
            raise ValueError("Remote start needs a snapshot receipt")
        for field in ("executor", "model"):
            identifier(payload[field], field)
        text = payload["task"]
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 64 * 1024:
            raise ValueError("Remote task must be nonempty text within 64 KiB")
        for field in ("parent_agent_id", "work_item_id"):
            if field in payload:
                identifier(payload[field], field)
        initial_paths = _paths(payload["snapshot"].get("paths"))
        result_paths = _paths(payload["result_paths"]) if "result_paths" in payload else []
        if len(set(initial_paths) | set(result_paths)) > MAX_FILES:
            raise ValueError("Remote result selection exceeds its file bound")
        repository = payload["repository"]
        if not isinstance(repository, str) or not Path(repository).is_absolute():
            raise ValueError("Remote repository must be an absolute configured path")
        root = Path(repository).resolve()
        context = payload.get("context", {})
        if (not isinstance(context, dict) or set(context) - {"relevant_paths", "constraints"}
                or any(not isinstance(values, list) or len(values) > 128
                       or any(not isinstance(value, str) or len(value.encode()) > 4096 for value in values)
                       for values in context.values())):
            raise ValueError("Invalid bounded remote context")
        try:
            encoded = canonical(payload)
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("Remote start must contain bounded JSON values") from error
        if len(encoded.encode()) > MAX_REQUEST_BYTES:
            raise ValueError("Remote start exceeds its 128 KiB request bound")
        with self.control.store.snapshot() as db:
            previous = self._lookup(db, origin)
            if previous is not None:
                if (previous["operation"], previous["payload"]) != ("start", encoded):
                    raise ValueError("Remote request_id was already used with another payload")
                return self._result(db, origin, previous)
        if not self.control.settings.delegation_enabled:
            raise ValueError("Receiving host delegation is not configured")
        if str(root) not in self.control.settings.repositories:
            raise ValueError("Remote repository is not an operator-configured target")
        invocation = {"executor": payload["executor"], "model": payload["model"], "task": text,
                      "parent_repo": str(root), "parent_worktree": str(root), "mode": "write"}
        if context:
            invocation["context"] = context
        task = {"task_id": "task-" + uuid.uuid4().hex, "session_id": _scope(origin),
                "parent_agent_id": payload.get("parent_agent_id"), "invocation": invocation,
                "remote_origin": {field: origin[field] for field in IDENTITY_FIELDS[1:]},
                "status": "created", "attempts": 0, "review_retries": 0, "continuations": 0,
                "executor": payload["executor"], "requested_model": payload["model"],
                "review_history": [], "created_at": now_ms(), "execution_stopped": True,
                "max_attempts": self.control.settings.max_attempts}
        if "work_item_id" in payload:
            task["work_item_id"] = invocation["work_item_id"] = payload["work_item_id"]
        if "parent_agent_id" in payload:
            task["remote_origin"]["parent_agent_id"] = payload["parent_agent_id"]
        # Admission only rejects invalid operator configuration. Runtime.execute
        # remains the sole binder of attempt configuration and native session IDs.
        self.validate(task)
        # Git/import work cannot hold a write transaction or block maintenance admission.
        task["remote_snapshot"] = import_snapshot(root, payload["snapshot"], Path(bundle_path))
        task["remote_paths"] = initial_paths
        task["remote_result_paths"] = result_paths
        with self.control.lifecycle_lock, self.control.store.transaction() as db:
            previous = self._lookup(db, origin)
            if previous is not None:
                if (previous["operation"], previous["payload"]) != ("start", encoded):
                    raise ValueError("Remote request_id was already used with another payload")
                return self._result(db, origin, previous)
            if self.control.draining:
                raise ValueError("Receiving Router is stopping for maintenance")
            self.control.store.put(db, task)
            db.execute("INSERT INTO remote_operations VALUES(?,?,?,?,?,?)",
                       (origin["origin_host_id"], origin["origin_session_id"], origin["request_id"],
                        "start", encoded, task["task_id"]))
            result = self._result(db, origin, self._lookup(db, origin))
        self.control.changed.set()
        return result
