"""Claude host adapters. Prompt bodies are never sent to the router."""

import json
import re
import sys

from router.client import request
from router.dispatch import MUTATIONS, literal_payload
from router.paths import configuration, delegation_target


def submit(event: dict) -> dict:
    values, _ = configuration()
    if not delegation_target(event.get("cwd"), values.get("AGENT_ROUTER_REPOSITORIES", {})):
        return {}
    if not event.get("session_id") or not event.get("prompt_id"):
        raise ValueError("Host submission lacks session_id/prompt_id; this host version is unsupported")
    result = request("submit", {"session_id": event["session_id"], "prompt_id": event["prompt_id"],
                                "parent_cli": "claude", "cwd": event["cwd"]})
    if not result.get("instruction_id"):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": "ROUTER_INSTRUCTION_ID=" + result["instruction_id"]}}


def authorize(event: dict) -> dict:
    command = event.get("tool_input", {}).get("command", "")
    if event.get("tool_name") != "Bash" or not re.search(r"\brouter\s+(start|review|cancel|close)\b", command):
        return {}
    try:
        match = re.match(r"router (start|review|cancel|close) --json ", command)
        if not match or match[1] not in MUTATIONS:
            raise ValueError("Use a standalone canonical router command; no pipelines or substitutions")
        operation = match[1]
        payload = literal_payload(command, operation)
        request("authorize", {"session_id": event["session_id"], "prompt_id": event["prompt_id"],
                              "operation": operation, "invocation": payload,
                              "parent_agent_id": event.get("agent_id") or "main"})
        return {}
    except (ValueError, KeyError) as error:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": str(error)}}


def observe(event: dict) -> dict:
    from router.observer_hook import observe as forward
    return forward(event)


def main() -> None:
    try:
        event = json.load(sys.stdin)
        handlers = {"submit": submit, "authorize": authorize, "observe": observe}
        result = handlers[sys.argv[1]](event)
        print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError) as error:
        if sys.argv[1] == "observe":
            print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
            print("{}")
            return
        if sys.argv[1] == "submit":
            # An unavailable router must not block unrelated conversation.
            print(f"Agent Router delegation unavailable: {error}", file=sys.stderr)
            print("{}")
            return
        print(f"Agent Router hook: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
