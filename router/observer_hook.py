"""Cheap hook gate: ordinary parent tool calls load only the standard library."""

import json
import sys


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            print("{}")
            return
        if event.get("agent_id") or event.get("hook_event_name") in {"SubagentStart", "SubagentStop", "SessionEnd"} or (
                event.get("hook_event_name") == "PostToolUse" and event.get("tool_name") in {"Agent", "Task"}):
            from router.hooks import observe
            print(json.dumps(observe(event)))
        else:
            print("{}")
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Agent Router observation unavailable: {error}", file=sys.stderr)
        print("{}")


if __name__ == "__main__":
    main()
