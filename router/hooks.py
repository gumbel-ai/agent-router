"""Native parent adapters. Prompt bodies are never sent to the router."""

import json
import sys
from functools import partial

from router.client import request
from router.dispatch import literal_payload, mutation_operation
from router.native import identifier
from router.paths import configuration, delegation_target, observed_project


def codex_parent(event: dict, name: str) -> dict:
    """Codex 0.163.0 omits both subagent fields on root events; turn_id is native."""
    if event.get("hook_event_name") != name:
        raise ValueError(f"Codex delegation requires a native {name} event")
    if "agent_id" in event or "agent_type" in event:
        raise ValueError("Codex delegation supports root coordinators only; subagent identity is not root authority")
    return {**event, "session_id": identifier(event.get("session_id"), "session_id"),
            "prompt_id": identifier(event.get("turn_id"), "turn_id")}


def submit(event: dict, *, source: str = "claude") -> dict:
    values, _ = configuration()
    if not delegation_target(event.get("cwd"), values.get("AGENT_ROUTER_REPOSITORIES", {})):
        return {}
    if source == "codex":
        if observed_project(event.get("cwd")) is None:
            return {}
        event = codex_parent(event, "UserPromptSubmit")
    if not event.get("session_id") or not event.get("prompt_id"):
        raise ValueError("Host submission lacks session_id/prompt_id; this host version is unsupported")
    result = request("submit", {"session_id": event["session_id"], "prompt_id": event["prompt_id"],
                                "parent_cli": source, "cwd": event["cwd"]})
    if not result.get("instruction_id"):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": "ROUTER_INSTRUCTION_ID=" + result["instruction_id"]}}


def authorize(event: dict, *, source: str = "claude") -> dict:
    if event.get("tool_name") != "Bash":
        return {}
    try:
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            raise TypeError("Shell tool_input must be an object")
        command = tool_input.get("command")
        operation = mutation_operation(command)
        if operation is None:
            return {}
        if source == "codex":
            event = codex_parent(event, "PreToolUse")
            values, _ = configuration()
            if (observed_project(event.get("cwd")) is None or
                    not delegation_target(event.get("cwd"), values.get("AGENT_ROUTER_REPOSITORIES", {}))):
                raise ValueError("Codex delegation requires an enabled project registered with router init")
        payload = literal_payload(command, operation)
        request("authorize", {"session_id": event["session_id"], "prompt_id": event["prompt_id"],
                              "operation": operation, "invocation": payload,
                              "parent_agent_id": None if source == "codex" else event.get("agent_id") or "main"})
        return {}
    except (ValueError, KeyError, TypeError) as error:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": str(error)}}


def observe(event: dict) -> dict:
    from router.observer_hook import observe as forward
    return forward(event)


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise TypeError("Host hook input must be a JSON object")
        handlers = {"submit": submit, "authorize": authorize, "observe": observe,
                    "codex-submit": partial(submit, source="codex"),
                    "codex-authorize": partial(authorize, source="codex")}
        result = handlers[sys.argv[1]](event)
        print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError, RecursionError) as error:
        if sys.argv[1] == "observe":
            print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
            print("{}")
            return
        if sys.argv[1] in {"submit", "codex-submit"}:
            # An unavailable router must not block unrelated conversation.
            print(f"Agent Router delegation unavailable: {error}", file=sys.stderr)
            print("{}")
            return
        print(f"Agent Router hook: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
