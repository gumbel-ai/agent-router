"""Claude host adapters. Prompt bodies are never sent to the router."""

import json
import re
import sys

from router.client import request
from router.dispatch import MUTATIONS, literal_payload


def submit(event: dict) -> dict:
    if not event.get("session_id") or not event.get("prompt_id"):
        raise ValueError("Host submission lacks session_id/prompt_id; this host version is unsupported")
    result = request("submit", {"session_id": event["session_id"], "prompt_id": event["prompt_id"],
                                "parent_cli": "claude"})
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
                              "operation": operation, "invocation": payload})
        return {}
    except (ValueError, KeyError) as error:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": str(error)}}


def main() -> None:
    try:
        event = json.load(sys.stdin)
        result = submit(event) if sys.argv[1] == "submit" else authorize(event)
        print(json.dumps(result))
    except (ValueError, KeyError, OSError) as error:
        if sys.argv[1] == "submit":
            # An unavailable router must not block unrelated conversation.
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                "additionalContext": f"Agent Router delegation unavailable: {error}. Do not delegate until resolved."}}))
            return
        print(f"Agent Router hook: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
