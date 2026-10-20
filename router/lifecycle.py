"""Canonical, side-effect-free dispatch schemas, shared by hooks and the daemon."""

from pathlib import Path

from router.correlation import identifier

START_FIELDS = frozenset({"request_version", "request_id", "instruction_id", "executor", "model", "task",
                          "parent_repo", "parent_worktree", "mode", "context", "work_item_id",
                          "host", "remote_repo", "remote_worktree", "launch", "completion"})
REVIEW_FIELDS = frozenset({"task_id", "request_id", "expected_attempt", "outcome", "feedback"})
CLOSE_FIELDS = frozenset({"task_id", "request_id", "disposition", "keep_artifacts"})
CANCEL_FIELDS = frozenset({"task_id", "request_id"})


def required_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def fields(operation: str, payload: dict, allowed: frozenset[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        label = "invocation" if operation == "start" else operation
        count = f"{min(len(extra), 999)}+" if len(extra) > 999 else str(len(extra))
        raise ValueError(f"Unknown {label} fields ({count}); router {operation} allows "
                         f"{', '.join(sorted(allowed))}. Remove unsupported fields")


def relative_path(value: object, field: str) -> None:
    if (not isinstance(value, str) or not value or len(value) > 2048 or "\x00" in value
            or Path(value).is_absolute() or ".." in Path(value).parts
            or len(Path(value).parts) > 32 or not Path(value).parts):
        raise ValueError(f"{field} must be a nonempty worktree-relative path without '..'")


def path_list(value: object, field: str) -> None:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError(f"{field} must be an array of at most 32 paths")
    for path in value:
        relative_path(path, field)


def validate_contracts(payload: dict) -> None:
    if {"launch", "completion"} & set(payload) and payload["executor"] not in {"codex", "claude"}:
        raise ValueError("launch/completion contracts currently support only Codex and Claude")
    for name, allowed in (("launch", {"task_packets", "writable_paths", "requires_router"}),
                          ("completion", {"files", "json_files", "final_response", "setup_status_file"})):
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError(f"start {name} must be an object allowing {', '.join(sorted(allowed))}")
        for key in ("task_packets", "writable_paths", "files", "json_files"):
            if key in value:
                path_list(value[key], f"start {name}.{key}")
        if "final_response" in value and type(value["final_response"]) is not bool:
            raise ValueError("start completion.final_response must be boolean")
        if "setup_status_file" in value:
            relative_path(value["setup_status_file"], "start completion.setup_status_file")
        if "requires_router" in value and type(value["requires_router"]) is not bool:
            raise ValueError("start launch.requires_router must be boolean")


def start(payload: dict) -> None:
    fields("start", payload, START_FIELDS)
    for key in ("request_version", "request_id", "instruction_id", "executor", "model", "task",
                "parent_repo", "parent_worktree", "mode"):
        required_text(payload, key)
    if "work_item_id" in payload:
        identifier(payload["work_item_id"], "work_item_id")
    if payload["request_version"] != "1" or payload["mode"] != "write":
        raise ValueError("Only request_version 1 and write mode are supported")
    if any(not Path(payload[key]).is_absolute() for key in ("parent_repo", "parent_worktree")):
        raise ValueError("Repository and worktree paths must be absolute")
    if {"host", "remote_repo", "remote_worktree"} & set(payload):
        required_text(payload, "host")
        for key in ("remote_repo", "remote_worktree"):
            if not Path(required_text(payload, key)).is_absolute():
                raise ValueError("Remote dispatch requires absolute remote_repo and remote_worktree")
    context = payload.get("context", {})
    if not isinstance(context, dict) or set(context) - {"relevant_paths", "constraints"}:
        raise ValueError("Invalid context")
    if any(not isinstance(values, list) or any(not isinstance(item, str) for item in values)
           for values in context.values()):
        raise ValueError("Context paths and constraints must be arrays of strings")
    validate_contracts(payload)


def review(payload: dict) -> None:
    fields("review", payload, REVIEW_FIELDS)
    if type(payload.get("expected_attempt")) is not int or payload["expected_attempt"] < 0:
        raise ValueError("Stale expected_attempt; expected_attempt must be an integer")
    outcome = required_text(payload, "outcome")
    if outcome not in {"accepted", "rejected", "continue"}:
        raise ValueError("Invalid review outcome for task state; outcome must be accepted, rejected or continue")
    if outcome != "accepted":
        required_text(payload, "feedback")
    elif "feedback" in payload and not isinstance(payload["feedback"], str):
        raise ValueError("feedback must be a string")


def close(payload: dict) -> None:
    fields("close", payload, CLOSE_FIELDS)
    if (payload.get("disposition") not in ("integrated", "discarded")
            or type(payload.get("keep_artifacts", False)) is not bool):
        raise ValueError("Invalid final disposition or keep_artifacts; disposition must be integrated or "
                         "discarded and keep_artifacts must be boolean")


def validate(operation: str, payload: dict) -> None:
    """Validate without authorizing, looking up tasks, changing input, or doing I/O."""
    if not isinstance(payload, dict):
        raise ValueError(f"router {operation}: request payload must be an object")  # noqa: TRY004 - public schema contract
    validators = {"start": start, "review": review, "close": close,
                  "cancel": lambda value: fields("cancel", value, CANCEL_FIELDS)}
    if operation not in validators:
        raise ValueError("Unknown operation")
    try:
        required_text(payload, "request_id")
        if operation != "start":
            task_id = required_text(payload, "task_id")
            if task_id.startswith("native-"):
                raise ValueError("Native agent is host-managed; Router cannot control it")
        validators[operation](payload)
    except ValueError as error:
        raise ValueError(f"router {operation}: {error}. Correct the named field before retrying") from error
