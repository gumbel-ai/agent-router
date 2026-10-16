"""Cheap hook gate: ordinary parent tool calls load only the standard library."""

import json
import sys
import time
from pathlib import Path

from router.client import request


def text(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join("".join(character for character in value if character.isprintable() or character.isspace()).split())[:limit] or None


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
                           "parent_session_id": event.get("parent_session_id"), "activity": text(tool),
                           "tool_use_id": event.get("tool_use_id"),
                           "start_source": event.get("source"),
                           "notification_type": event.get("notification_type"),
                           "background_tasks": event.get("background_tasks"), "task_id": event.get("task_id"),
                           "task_subject": text(event.get("task_subject"), 500),
                           "teammate_name": event.get("teammate_name"), "agent_id": event.get("agent_id")}
    payload = {"source": "claude", "session_id": event.get("session_id"), "observed_at": at,
               "agent_id": event.get("agent_id"), "agent_type": event.get("agent_type"), "cwd": event.get("cwd")}
    main_transcript = event.get("transcript_path")
    if name == "SessionEnd":
        payload["kind"] = "session_end"
    elif name in {"SubagentStart", "SubagentStop"}:
        payload["kind"] = "start" if name == "SubagentStart" else "stop"
        if name == "SubagentStop":
            payload["summary"] = text(event.get("last_assistant_message"), 800)
            payload["transcript"] = event.get("agent_transcript_path")
    elif name == "PostToolUse" and tool in {"Agent", "Task"} and isinstance(response, dict) and response.get("agentId"):
        asynchronous = response.get("isAsync") is True or response.get("status") == "async_launched"
        payload.update(kind="launch" if asynchronous else "result", agent_id=response["agentId"],
                       parent_agent_id=event.get("agent_id") or "main", task=text(inputs.get("description")),
                       agent_type=inputs.get("subagent_type"), requested_model=inputs.get("model"),
                       resolved_model=response.get("resolvedModel"))
        if isinstance(response.get("costUSD"), (int, float)):
            payload["cost"] = response["costUSD"]
    elif name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} and payload["agent_id"]:
        payload.update(kind="activity", activity=text(tool), file_path=text(inputs.get("file_path"), 2000))
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
    if payload and payload["agent_id"] and isinstance(main_transcript, str) and not payload.get("transcript"):
        parent = Path(main_transcript)
        payload["transcript"] = str(parent.parent / parent.stem / "subagents" / f"agent-{payload['agent_id']}.jsonl")
    try:
        result = request("lifecycle_observe", {"session": session_payload, "native": payload}, timeout=.05)
        if result.get("accepted") is False:
            reason = text(result.get("reason"), 500) or "Lifecycle observation was not accepted"
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


def main() -> None:
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
