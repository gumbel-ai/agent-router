"""Cheap hook gate: ordinary parent tool calls load only the standard library."""

import json
import sys
import time

from router.client import request
from router.sanitize import bound_text, strip_terminal_sequences


def text(value: object, limit: int | None = 240) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join("".join(
        character for character in strip_terminal_sequences(value)
        if character.isprintable() or character.isspace()
    ).split())
    return (normalized if limit is None else normalized[:limit]) or None


def bounded_text(value: object, limit: int) -> str | None:
    normalized = text(value, None)
    return bound_text(normalized, limit) if normalized is not None else None


def observe(event: dict) -> dict:
    """Forward structured metadata only; observation failure never blocks Claude."""
    name = event.get("hook_event_name")
    tool = event.get("tool_name")
    inputs = event.get("tool_input") or {}
    response = event.get("tool_response") or {}
    if not isinstance(inputs, dict):
        inputs = {}
    at = int(time.time() * 1000)
    kinds = {"SessionStart": "session_start", "SessionEnd": "session_end", "UserPromptSubmit": "prompt",
             "PreToolUse": "tool_start", "PostToolUse": "tool_end", "PostToolUseFailure": "tool_failure",
             "TaskCreated": "task_created", "TaskCompleted": "task_completed", "Stop": "stop",
             "StopFailure": "stop_failure", "Notification": "notification", "TeammateIdle": "teammate_idle",
             "SubagentStart": "subagent_start", "SubagentStop": "subagent_stop"}
    session_kind = kinds.get(name)
    session_payload = None
    if session_kind:
        session_payload = {"source": "claude", "session_id": event.get("session_id"), "observed_at": at,
                           "kind": session_kind, "cwd": event.get("cwd"), "model": event.get("model"),
                           "parent_session_id": event.get("parent_session_id"), "activity": bounded_text(tool, 240),
                           "tool_use_id": event.get("tool_use_id"),
                           "start_source": event.get("source"),
                           "notification_type": event.get("notification_type"),
                           "background_tasks": event.get("background_tasks"), "task_id": event.get("task_id"),
                           "task_subject": bounded_text(event.get("task_subject"), 500),
                           "teammate_name": event.get("teammate_name"), "agent_id": event.get("agent_id")}
    payload = {"source": "claude", "session_id": event.get("session_id"), "observed_at": at,
               "agent_id": event.get("agent_id"), "agent_type": event.get("agent_type"), "cwd": event.get("cwd")}
    if name == "SessionEnd":
        payload["kind"] = "session_end"
    elif name in {"SubagentStart", "SubagentStop"}:
        payload["kind"] = "start" if name == "SubagentStart" else "stop"
    elif name == "PostToolUse" and tool in {"Agent", "Task"} and isinstance(response, dict) and response.get("agentId"):
        asynchronous = response.get("isAsync") is True or response.get("status") == "async_launched"
        payload.update(kind="launch" if asynchronous else "result", agent_id=response["agentId"],
                       parent_agent_id=event.get("agent_id") or "main",
                       task=bounded_text(inputs.get("description"), 240),
                       agent_type=inputs.get("subagent_type"), requested_model=inputs.get("model"),
                       resolved_model=response.get("resolvedModel"))
    elif name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} and payload["agent_id"]:
        payload.update(kind="activity", activity=bounded_text(tool, 240))
    elif name == "TeammateIdle" and payload["agent_id"]:
        payload.update(kind="idle", activity="teammate idle")
    elif name == "Notification" and payload["agent_id"] and event.get("notification_type") == "agent_needs_input":
        payload.update(kind="needs_input", activity="needs input")
    elif name == "Notification" and payload["agent_id"] and event.get("notification_type") == "agent_completed":
        payload.update(kind="idle", activity="completed notification")
    elif name == "StopFailure" and payload["agent_id"]:
        payload.update(kind="failed", failure_reason="Claude reported that the agent turn failed")
    else:
        payload = None
    return send(session_payload, payload)


def send(session_payload: dict | None, payload: dict | None) -> dict:
    """Forward one lifecycle observation; failure never blocks the host agent."""
    try:
        result = request("lifecycle_observe", {"session": session_payload, "native": payload}, timeout=.05)
        rejected = result.get("accepted") is False
        terminal_failure = result.get("persisted") is False and result.get("queued") is not True
        if rejected or terminal_failure:
            persistence = result.get("persistence")
            failed = [
                key for key, state in persistence.items() if state == "failed"
            ] if isinstance(persistence, dict) else []
            reason = text(result.get("reason"), 500)
            if reason is None and failed:
                reason = "Lifecycle observation failed for: " + ", ".join(sorted(failed))
            reason = reason or "Lifecycle observation was not accepted"
            print(f"Agent Router observation unavailable: {reason}", file=sys.stderr)
    except ValueError as error:
        if str(error) == "Unknown operation" and payload is not None:
            try:
                request("observe", payload, timeout=.05)
            except (ValueError, KeyError, TypeError, OSError) as fallback_error:
                print(f"Agent Router observation unavailable: {fallback_error}", file=sys.stderr)
        else:
            print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
    except (KeyError, TypeError, OSError) as error:
        print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
    return {}


# Codex hook event -> (session kind, native kind for subagent events). See docs/codex-lifecycle.md.
CODEX_EVENTS = {
    "SessionStart": ("session_start", None),
    "SessionEnd": ("session_end", "session_end"),
    "UserPromptSubmit": ("prompt", "activity"),
    "PreToolUse": ("tool_start", "activity"),
    "PostToolUse": ("tool_end", "activity"),
    "PermissionRequest": ("notification", "needs_input"),
    "SubagentStart": ("subagent_start", "start"),
    "SubagentStop": ("subagent_stop", "idle"),  # Per subagent turn, not its end.
    "Stop": ("stop", None),
    "Interrupt": ("stop", None),
}


def observe_codex(event: dict) -> dict:
    """Map a Codex hook payload onto Router lifecycle events. Content is never forwarded."""
    name = event.get("hook_event_name")
    if name not in CODEX_EVENTS:
        return {}
    session_kind, agent_kind = CODEX_EVENTS[name]
    agent = event.get("agent_id")
    at = int(time.time() * 1000)
    base = {"source": "codex", "session_id": event.get("session_id"), "observed_at": at}
    tool = bounded_text(event.get("tool_name"), 240)
    session_payload = {**base, "kind": session_kind, "cwd": event.get("cwd"), "model": event.get("model"),
                       "agent_id": agent, "tool_use_id": event.get("tool_use_id"), "activity": tool}
    if name == "SessionStart":
        session_payload["start_source"] = event.get("source")
    elif name == "PermissionRequest":
        session_payload["notification_type"] = "permission_prompt"
    elif name == "UserPromptSubmit" and agent is not None:
        # A subagent's prompt is not a new root turn, but it does answer that agent's wait.
        session_payload["kind"] = "subagent_activity"
    payload = None
    if name == "SessionEnd":
        payload = {**base, "kind": "session_end"}
    elif agent_kind is not None and agent is not None:
        payload = {**base, "kind": agent_kind, "agent_id": agent, "agent_type": event.get("agent_type"),
                   "cwd": event.get("cwd"), "resolved_model": event.get("model"),
                   "activity": tool or ("needs input" if agent_kind == "needs_input" else None)}
    return send(session_payload, payload)


def main() -> None:
    if sys.argv[1:] == ["--source", "codex"]:
        try:
            event = json.load(sys.stdin)
            if isinstance(event, dict):
                observe_codex(event)
        except (ValueError, KeyError, TypeError, OSError) as error:
            print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
        print("{}")  # Codex accepts an empty JSON object as a no-op on every event.
        return
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            print("{}")
            return
        relevant = {"SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart", "SubagentStop",
                    "TaskCreated", "TaskCompleted", "TeammateIdle", "PreToolUse", "PostToolUse",
                    "PostToolUseFailure", "Stop", "StopFailure", "Notification"}
        if event.get("hook_event_name") in relevant:
            print(json.dumps(observe(event)))
        else:
            print("{}")
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
        print("{}")


if __name__ == "__main__":
    main()
