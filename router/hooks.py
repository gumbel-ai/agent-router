"""Claude host adapters. Prompt bodies are never sent to the router."""

import json
import re
import sys

from router.client import request
from router.db import now_ms
from router.dispatch import MUTATIONS, literal_payload
from router.native import text


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
                              "operation": operation, "invocation": payload,
                              "parent_agent_id": event.get("agent_id") or "main"})
        return {}
    except (ValueError, KeyError) as error:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": str(error)}}


def observe(event: dict) -> dict:
    """Forward metadata only; observation failure must not block native work."""
    name = event.get("hook_event_name")
    tool = event.get("tool_name")
    inputs = event.get("tool_input") or {}
    response = event.get("tool_response") or {}
    if not isinstance(inputs, dict):
        return {}
    payload = {"source": "claude", "session_id": event.get("session_id"), "observed_at": now_ms(),
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
        payload.update(kind="launch" if asynchronous else "result", agent_id=response["agentId"], parent_agent_id=event.get("agent_id") or "main",
                       task=text(inputs.get("description")), agent_type=inputs.get("subagent_type"),
                       requested_model=inputs.get("model"), resolved_model=response.get("resolvedModel"))
        # A host-reported per-child estimate is acceptable; never use a parent
        # session's total_cost_usd or its aggregate modelUsage here.
        if isinstance(response.get("costUSD"), (int, float)):
            payload["cost"] = response["costUSD"]
    elif name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} and payload["agent_id"]:
        payload.update(kind="activity", activity=text(tool), file_path=text(inputs.get("file_path"), 2000))
    else:
        return {}
    if payload["agent_id"] and isinstance(main_transcript, str) and not payload.get("transcript"):
        from pathlib import Path
        parent = Path(main_transcript)
        payload["transcript"] = str(parent.parent / parent.stem / "subagents" / f"agent-{payload['agent_id']}.jsonl")
    try:
        request("observe", payload, timeout=.5)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
    return {}


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
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                "additionalContext": f"Agent Router delegation unavailable: {error}. Do not delegate until resolved."}}))
            return
        print(f"Agent Router hook: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
